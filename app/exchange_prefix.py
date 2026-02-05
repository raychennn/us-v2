"""TradingView exchange prefix resolver (NASDAQ/NYSE).

TradingView watchlists can import symbols with an exchange prefix, e.g.:

- ``NASDAQ:AAPL``
- ``NYSE:IBM``

This module resolves per-symbol prefixes using Yahoo Finance metadata via
``yfinance`` and caches results in SQLite to keep scans fast and Zeabur-friendly.

Design goals
------------
- Best-effort: never crash the bot if metadata resolution fails.
- Efficient: use caching + bounded concurrency.
- Minimal deps: relies only on existing project deps (yfinance + stdlib).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Mapping

import yfinance as yf

from .config import Config
from .storage import tv_exchange_cache_get, tv_exchange_cache_set


logger = logging.getLogger(__name__)


# Yahoo/yfinance exchange codes (and common names) -> TradingView exchange prefixes.
# Reference: yfinance typically returns "NMS" for NASDAQ and "NYQ" for NYSE.
_EXCHANGE_TO_TV_PREFIX: dict[str, str] = {
    # NASDAQ
    "NMS": "NASDAQ:",
    "NAS": "NASDAQ:",
    "NGM": "NASDAQ:",
    "NCM": "NASDAQ:",
    "NASDAQ": "NASDAQ:",
    "NASDAQGS": "NASDAQ:",
    "NASDAQGM": "NASDAQ:",
    "NASDAQCM": "NASDAQ:",
    # NYSE
    "NYQ": "NYSE:",
    "NYS": "NYSE:",
    "NYSE": "NYSE:",
}


@dataclass(frozen=True)
class ResolveResult:
    """Resolution result for one symbol."""

    symbol: str
    tv_prefix: str | None
    raw_exchange: str | None


def _normalize_exchange(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s or None


def _exchange_to_tv_prefix(raw_exchange: str | None) -> str | None:
    exch = _normalize_exchange(raw_exchange)
    if exch is None:
        return None
    return _EXCHANGE_TO_TV_PREFIX.get(exch)


def _fetch_exchange_from_yfinance(symbol: str) -> str | None:
    """Fetch raw exchange identifier from yfinance (blocking).

    We try ``fast_info`` first (cheaper), then fall back to ``get_info``.
    """

    t = yf.Ticker(symbol)

    # fast_info is available in yfinance 0.2+
    try:
        fi = getattr(t, "fast_info", None)
        if isinstance(fi, Mapping):
            # common keys: exchange / exchangeName / fullExchangeName
            for k in ("exchange", "exchangeName", "fullExchangeName"):
                v = fi.get(k)
                if v:
                    return str(v)
    except Exception:
        # best-effort: ignore and try the slower path
        logger.debug("fast_info failed for %s", symbol, exc_info=True)

    try:
        info = t.get_info()
        if isinstance(info, Mapping):
            for k in ("exchange", "exchangeName", "fullExchangeName"):
                v = info.get(k)
                if v:
                    return str(v)
    except Exception:
        logger.debug("get_info failed for %s", symbol, exc_info=True)

    return None


def _resolve_one_blocking(symbol: str, *, max_retries: int, backoff_base_sec: float) -> ResolveResult:
    """Resolve one symbol in a blocking context with retry/backoff."""

    sym = symbol.strip().upper()
    if not sym:
        return ResolveResult(symbol=symbol, tv_prefix=None, raw_exchange=None)

    last_exc: Exception | None = None
    raw: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = _fetch_exchange_from_yfinance(sym)
            tv_prefix = _exchange_to_tv_prefix(raw)
            return ResolveResult(symbol=sym, tv_prefix=tv_prefix, raw_exchange=raw)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(backoff_base_sec**attempt)

    if last_exc is not None:
        logger.warning("Exchange resolve failed for %s: %s", sym, last_exc)
    return ResolveResult(symbol=sym, tv_prefix=None, raw_exchange=raw)


async def resolve_tv_prefix_map(
    symbols: Iterable[str],
    *,
    cfg: Config,
    db_path: str,
) -> dict[str, str]:
    """Resolve TradingView exchange prefixes for symbols.

    Returns a mapping from raw ticker (uppercased) to TradingView prefix string
    (e.g. ``NASDAQ:`` / ``NYSE:``). Symbols without a known mapping are omitted.

    Resolution uses:
    1) SQLite cache (fresh within ``cfg.TV_EXCHANGE_CACHE_MAX_AGE_DAYS``)
    2) Best-effort yfinance metadata fetch for cache misses
    """

    # Dedupe while preserving order
    uniq: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        sym = s.strip().upper()
        if not sym or sym in seen:
            continue
        uniq.append(sym)
        seen.add(sym)

    out: dict[str, str] = {}
    misses: list[str] = []
    for sym in uniq:
        cached = tv_exchange_cache_get(db_path, sym, max_age_days=cfg.TV_EXCHANGE_CACHE_MAX_AGE_DAYS)
        if cached:
            out[sym] = cached
        else:
            misses.append(sym)

    if not misses:
        return out

    sem = asyncio.Semaphore(cfg.TV_EXCHANGE_RESOLVE_CONCURRENCY)
    updates: list[ResolveResult] = []

    async def worker(sym: str) -> None:
        async with sem:
            res = await asyncio.to_thread(
                _resolve_one_blocking,
                sym,
                max_retries=cfg.TV_EXCHANGE_RESOLVE_MAX_RETRIES,
                backoff_base_sec=cfg.TV_EXCHANGE_RESOLVE_BACKOFF_BASE_SEC,
            )
            updates.append(res)

    try:
        async with asyncio.TaskGroup() as tg:
            for sym in misses:
                tg.create_task(worker(sym))
    except Exception:
        # TaskGroup collects and re-raises; we don't want to crash sending.
        logger.exception("Unexpected error while resolving exchange prefixes")

    for res in updates:
        if res.tv_prefix:
            out[res.symbol] = res.tv_prefix
            try:
                tv_exchange_cache_set(db_path, res.symbol, res.tv_prefix, raw_exchange=res.raw_exchange)
            except Exception:
                # Cache failures should not affect runtime behavior.
                logger.debug("Failed to write tv_exchange_cache for %s", res.symbol, exc_info=True)

    return out
