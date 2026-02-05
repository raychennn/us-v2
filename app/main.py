"""Telegram bot entrypoint.

Key reliability notes:
- DO NOT wrap Application.run_polling() inside asyncio.run().
  python-telegram-bot manages its own event loop.
- Background tasks MUST be cancelled on shutdown to avoid:
  "Task was destroyed but it is pending!"
- When deployed as a Web Service on platforms that require an HTTP listener
  (health checks), we start a tiny stdlib HTTP server on $PORT.

Security note:
- Avoid logging full Telegram request URLs (they contain the bot token).
  We set httpx/httpcore loggers to WARNING by default.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from telegram import InputFile, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from .config import CONFIG
from .exchange_prefix import resolve_tv_prefix_map
from .scanner import ScanOutput, check_symbol, get_latest_trading_day, scan_market
from .storage import add_scan_history, init_db, kv_get, kv_set
from .tv_watchlist import build_filename, build_watchlist_text


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, CONFIG.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # Prevent leaking Telegram bot token in INFO logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)
_scan_lock = asyncio.Lock()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence request logs to avoid noise.
        return


def _start_health_server_from_env() -> Optional[ThreadingHTTPServer]:
    """Start a tiny HTTP server on $PORT if present.

    Some platforms (including Zeabur Web Service mode) expect your container to
    listen on $PORT. If you deploy this as a Worker/Background service, you can
    ignore this server.
    """

    port_str = os.getenv("PORT", "").strip()
    if not port_str:
        return None

    try:
        port = int(port_str)
    except ValueError:
        logger.warning("Invalid PORT=%r; health server disabled", port_str)
        return None

    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health server listening on 0.0.0.0:%s", port)
    return server


def _split_message(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        # +1 for newline
        add = len(line) + 1
        if size + add > limit and buf:
            parts.append("\n".join(buf))
            buf = [line]
            size = add
        else:
            buf.append(line)
            size += add
    if buf:
        parts.append("\n".join(buf))
    return parts


async def _send_text(app: Application, text: str) -> None:
    for chunk in _split_message(text):
        await app.bot.send_message(
            chat_id=CONFIG.TG_CHAT_ID,
            text=chunk,
            disable_web_page_preview=True,
        )


async def _send_tradingview_watchlist(app: Application, out: ScanOutput) -> None:
    """Send a TradingView-importable watchlist file as a Telegram document.

    The file content is generated from scan categories.
    Import path: TradingView Watchlist -> Import list.
    """

    try:
        filename = build_filename(out.trading_day, prefix=CONFIG.TV_EXPORT_FILENAME_PREFIX)

        # TradingView symbol prefixing strategy:
        # 1) If TV_SYMBOL_PREFIX is set, apply it globally (legacy behavior)
        # 2) Else, (optional) resolve per-symbol exchange prefix (NASDAQ:/NYSE:)
        per_symbol_prefix: dict[str, str] | None = None
        if not CONFIG.TV_SYMBOL_PREFIX and CONFIG.TV_EXCHANGE_AUTO_PREFIX:
            # Only resolve prefixes for symbols that actually appear in the output.
            flat = [t for items in out.categories.values() for t in items]
            per_symbol_prefix = await resolve_tv_prefix_map(flat, cfg=CONFIG, db_path=CONFIG.DB_PATH)

        content = build_watchlist_text(
            out.categories,
            sym_prefix=CONFIG.TV_SYMBOL_PREFIX,
            sym_suffix=CONFIG.TV_SYMBOL_SUFFIX,
            per_symbol_prefix=per_symbol_prefix,
            convert_yahoo_dash_to_dot=CONFIG.TV_CONVERT_YAHOO_DASH_TO_DOT,
            include_empty_sections=CONFIG.TV_EXPORT_INCLUDE_EMPTY_SECTIONS,
        )

        bio = io.BytesIO(content.encode("utf-8"))
        caption_lines = [
            f"📎 TradingView 匯入檔：{filename}",
            "匯入：TradingView Watchlist → Import list。",
        ]

        if CONFIG.TV_SYMBOL_PREFIX:
            caption_lines.append("已套用全域前綴/後綴：TV_SYMBOL_PREFIX / TV_SYMBOL_SUFFIX")
        elif CONFIG.TV_EXCHANGE_AUTO_PREFIX:
            caption_lines.append("已自動加入交易所前綴（NASDAQ:/NYSE:）")
            caption_lines.append("可用 TV_EXCHANGE_AUTO_PREFIX=false 關閉")
        else:
            caption_lines.append("需要前綴/後綴可設：TV_SYMBOL_PREFIX / TV_SYMBOL_SUFFIX")

        if CONFIG.TV_CONVERT_YAHOO_DASH_TO_DOT:
            caption_lines.append("已啟用 class share 轉換：BRK-B → BRK.B")

        caption = "\n".join(caption_lines)

        await app.bot.send_document(
            chat_id=CONFIG.TG_CHAT_ID,
            document=InputFile(bio, filename=filename),
            caption=caption,
        )
    except Exception:
        logger.exception("Failed to send TradingView watchlist file")
        await app.bot.send_message(
            chat_id=CONFIG.TG_CHAT_ID,
            text="⚠️ TradingView 檔案產生/傳送失敗（已記錄 log）。",
            disable_web_page_preview=True,
        )


async def _send_scan_output(app: Application, out: ScanOutput) -> None:
    """Send scan text + TradingView attachment."""

    await _send_text(app, out.message)
    await _send_tradingview_watchlist(app, out)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📌 指令\n"
        "/now - 立即掃描一次（會附 TradingView 匯入檔）\n"
        "/check TICKER - 單股逐項檢查（顯示數值與技術面）\n"
        "/help - 顯示此說明\n"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _scan_lock:
        await update.message.reply_text("收到！開始掃描…", disable_web_page_preview=True)
        try:
            out = scan_market(CONFIG)
            add_scan_history(CONFIG.DB_PATH, out.trading_day, out.benchmark, out.message)
            await _send_scan_output(context.application, out)
        except Exception as e:
            logger.exception("/now scan failed")
            await update.message.reply_text(f"❌ 掃描失敗：{e}")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("用法：/check TICKER 例如 /check AAPL")
        return
    symbol = context.args[0].strip().upper()
    try:
        msg = check_symbol(CONFIG, symbol)
        await update.message.reply_text(msg, disable_web_page_preview=True)
    except Exception as e:
        logger.exception("/check failed")
        await update.message.reply_text(f"❌ /check 失敗：{e}")


async def _auto_loop(app: Application) -> None:
    """Poll for new trading day and run scan once per new day."""

    key = "last_trading_day"
    try:
        while True:
            try:
                latest = get_latest_trading_day(CONFIG.TRADING_DAY_ANCHOR, CONFIG)
                last = kv_get(CONFIG.DB_PATH, key)
                if last != latest:
                    logger.info("New trading day detected: %s (prev=%s)", latest, last)
                    async with _scan_lock:
                        out = scan_market(CONFIG)
                        kv_set(CONFIG.DB_PATH, key, latest)
                        add_scan_history(CONFIG.DB_PATH, out.trading_day, out.benchmark, out.message)
                        await _send_scan_output(app, out)
                else:
                    logger.debug("No new trading day. latest=%s", latest)
            except Exception:
                # Never silent-fail
                logger.exception("Auto loop error")

            await asyncio.sleep(CONFIG.POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        logger.info("Auto loop cancelled, exiting gracefully.")
        raise


async def _post_init(app: Application) -> None:
    """PTB hook: called after initialization, within the running event loop."""

    # Print basic environment info to logs for Zeabur debugging
    try:
        import telegram

        logger.info("Python=%s", sys.version.replace("\n", " "))
        logger.info("TZ=%s", os.getenv("TZ", "(not set)"))
        logger.info("python-telegram-bot=%s", getattr(telegram, "__version__", "?"))
    except Exception:
        logger.exception("Failed to log environment info")

    # Start health server (optional)
    server = _start_health_server_from_env()
    app.bot_data["health_server"] = server

    # Start background loop and keep reference for graceful shutdown
    tasks: list[asyncio.Task] = []
    app.bot_data["bg_tasks"] = tasks
    tasks.append(app.create_task(_auto_loop(app)))


async def _post_shutdown(app: Application) -> None:
    """PTB hook: cancel background tasks and stop health server."""

    tasks = app.bot_data.get("bg_tasks", [])
    for t in tasks:
        t.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    server = app.bot_data.get("health_server")
    if server is not None:
        server.shutdown()
        server.server_close()


def main() -> None:
    _setup_logging()
    init_db(CONFIG.DB_PATH)

    application = (
        ApplicationBuilder()
        .token(CONFIG.TG_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("now", cmd_now))
    application.add_handler(CommandHandler("check", cmd_check))

    # PTB manages its own event loop. Do NOT wrap in asyncio.run().
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
