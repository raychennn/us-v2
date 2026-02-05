"""Yahoo Finance data access.

We use yfinance with auto_adjust=True so all prices are adjusted for
splits/dividends (Adjusted Close semantics).

This module provides chunked downloads + retry/backoff to be Zeabur-friendly.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

import pandas as pd
import yfinance as yf


logger = logging.getLogger(__name__)


OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class DownloadResult:
    data: Dict[str, pd.DataFrame]
    failed: List[str]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    # Ensure required columns exist and index is sorted & tz-naive.
    df = df.copy()
    df = df.sort_index()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert(None)
    for c in OHLCV_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[OHLCV_COLS]


def _split_multi(df: pd.DataFrame, tickers: Sequence[str]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    if df.empty:
        return out

    # yfinance returns:
    # - single ticker: columns = Open/High/Low/Close/Adj Close/Volume
    # - multi ticker: columns = MultiIndex [field, ticker]
    if isinstance(df.columns, pd.MultiIndex):
        # df[field][ticker]
        for t in tickers:
            if t in df.columns.get_level_values(1):
                sub = df.xs(t, axis=1, level=1, drop_level=False)
                # After xs with drop_level=False, columns still MultiIndex: (field, ticker)
                sub.columns = sub.columns.get_level_values(0)
                out[t] = _normalize_df(sub)
    else:
        # single
        t = tickers[0]
        out[t] = _normalize_df(df)
    return out


def download_ohlcv_chunk(
    tickers: Sequence[str],
    period: str,
    *,
    auto_adjust: bool = True,
    max_retries: int = 3,
    backoff_base_sec: float = 1.5,
) -> DownloadResult:
    failed: List[str] = []
    for attempt in range(1, max_retries + 1):
        try:
            df = yf.download(
                tickers=list(tickers),
                period=period,
                interval="1d",
                auto_adjust=auto_adjust,
                group_by="column",
                progress=False,
                threads=True,
            )
            data = _split_multi(df, tickers)
            missing = [t for t in tickers if t not in data or data[t].empty]
            if missing:
                logger.warning("Yahoo missing data for %d/%d tickers (attempt %d/%d): %s", len(missing), len(tickers), attempt, max_retries, ",".join(missing[:10]))
                failed = missing
            else:
                failed = []
            # Return partial data even if some missing
            return DownloadResult(data=data, failed=failed)
        except Exception:
            logger.exception("Yahoo download failed (attempt %d/%d) for chunk size=%d", attempt, max_retries, len(tickers))
            if attempt < max_retries:
                time.sleep(backoff_base_sec ** attempt)
            continue

    # All retries exhausted
    return DownloadResult(data={}, failed=list(tickers))


def download_ohlcv_in_chunks(
    tickers: Sequence[str],
    period: str,
    *,
    chunk_size: int,
    auto_adjust: bool,
    max_retries: int,
    backoff_base_sec: float,
) -> DownloadResult:
    all_data: Dict[str, pd.DataFrame] = {}
    failed_all: List[str] = []

    tickers_list = list(tickers)
    for i in range(0, len(tickers_list), chunk_size):
        chunk = tickers_list[i : i + chunk_size]
        res = download_ohlcv_chunk(
            chunk,
            period,
            auto_adjust=auto_adjust,
            max_retries=max_retries,
            backoff_base_sec=backoff_base_sec,
        )
        all_data.update(res.data)
        failed_all.extend(res.failed)
        # small pacing to reduce rate-limit pressure
        time.sleep(0.2)

    # Remove duplicates in failed list
    failed_unique = sorted(set(failed_all))
    return DownloadResult(data=all_data, failed=failed_unique)
