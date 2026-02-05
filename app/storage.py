"""SQLite storage for small state (e.g., last completed trading day).

We intentionally keep DB simple to be Zeabur-friendly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional


logger = logging.getLogger(__name__)


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    _ensure_dir(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trading_day TEXT NOT NULL,
              benchmark TEXT NOT NULL,
              message TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )

        # Cache for TradingView per-symbol exchange prefixes (e.g. NASDAQ: / NYSE:)
        # to avoid repeatedly fetching metadata from Yahoo/yfinance.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tv_exchange_cache (
              symbol TEXT PRIMARY KEY,
              tv_prefix TEXT NOT NULL,
              raw_exchange TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )


def tv_exchange_cache_get(
    db_path: str,
    symbol: str,
    *,
    max_age_days: int,
) -> Optional[str]:
    """Get cached TradingView exchange prefix for a symbol.

    Args:
        db_path: SQLite file path.
        symbol: Raw ticker (Yahoo format).
        max_age_days: Max age in days; stale entries are ignored.

    Returns:
        Prefix string like "NASDAQ:" / "NYSE:", or None.
    """

    sym = symbol.strip().upper()
    if not sym:
        return None

    with connect(db_path) as conn:
        cur = conn.execute(
            "SELECT tv_prefix, updated_at FROM tv_exchange_cache WHERE symbol=?",
            (sym,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        tv_prefix, updated_at = row

    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return None

    age_days = (datetime.utcnow() - ts).days
    if age_days > max_age_days:
        return None
    return str(tv_prefix)


def tv_exchange_cache_set(
    db_path: str,
    symbol: str,
    tv_prefix: str,
    *,
    raw_exchange: str | None,
    max_retries: int = 3,
) -> None:
    """Upsert TradingView exchange prefix cache.

    We keep this best-effort; failures should not crash the bot.
    """

    sym = symbol.strip().upper()
    if not sym:
        return

    now = datetime.utcnow().isoformat()
    for attempt in range(1, max_retries + 1):
        try:
            with connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO tv_exchange_cache(symbol, tv_prefix, raw_exchange, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                      tv_prefix=excluded.tv_prefix,
                      raw_exchange=excluded.raw_exchange,
                      updated_at=excluded.updated_at;
                    """,
                    (sym, tv_prefix, raw_exchange, now),
                )
            return
        except sqlite3.OperationalError:
            # e.g. database is locked; retry a few times.
            if attempt >= max_retries:
                logger.warning("tv_exchange_cache write failed after retries (symbol=%s)", sym)
                return
            time.sleep(0.05 * attempt)


def kv_get(db_path: str, key: str) -> Optional[str]:
    with connect(db_path) as conn:
        cur = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def kv_set(db_path: str, key: str, value: str) -> None:
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO kv_store(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
            """,
            (key, value, now),
        )


def add_scan_history(db_path: str, trading_day: str, benchmark: str, message: str) -> None:
    now = datetime.utcnow().isoformat()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_history(trading_day, benchmark, message, created_at) VALUES (?, ?, ?, ?)",
            (trading_day, benchmark, message, now),
        )
