"""Indicators and pattern checks.

The implementation intentionally avoids heavy third-party TA libraries.
We rely on pandas/numpy only (already required by yfinance).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window, min_periods=window).mean()


def mrs(log_rs: pd.Series, window: int) -> pd.Series:
    """MRS as percent deviation of log_rs above its SMA(window)."""
    ma = sma(log_rs, window)
    # exp(log_rs - ma) - 1 converts log-diff back to relative ratio
    out = (np.exp(log_rs - ma) - 1.0) * 100.0
    return out


def rel_return(close: pd.Series, periods: int) -> pd.Series:
    """Simple return over N periods."""
    return close / close.shift(periods) - 1.0


def rel_return_vs_benchmark(sym_close: pd.Series, bench_close: pd.Series, periods: int) -> pd.Series:
    """Return(sym, N) - Return(bench, N) aligned on shared index."""
    sym, bench = sym_close.align(bench_close, join="inner")
    return rel_return(sym, periods) - rel_return(bench, periods)


def last_adv_dollar(close: pd.Series, volume: pd.Series, days: int = 5) -> float | None:
    """Average dollar volume over last N days."""
    if len(close) < days or len(volume) < days:
        return None
    dv = (close * volume).rolling(days, min_periods=days).mean()
    val = dv.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def trend_ok(close: pd.Series, *, sma_fast: int, sma_mid: int, sma_slow: int, check_window: int, min_true: int, slow_tol_ratio: float) -> bool:
    """Basic uptrend confirmation using moving average ordering."""
    if len(close) < max(sma_fast, sma_mid, sma_slow) + check_window:
        return False

    s_fast = sma(close, sma_fast)
    s_mid = sma(close, sma_mid)
    s_slow = sma(close, sma_slow)

    cond = (close > s_fast) & (s_fast > s_mid) & (s_mid > s_slow)
    recent = cond.tail(check_window)
    if recent.sum() < min_true:
        return False

    # extra guard using previous day
    prev_close = close.iloc[-2]
    prev_fast = s_fast.iloc[-2]
    prev_slow = s_slow.iloc[-2]
    if any(pd.isna(x) for x in [prev_close, prev_fast, prev_slow]):
        return False
    return (prev_close > prev_fast) and (prev_fast > prev_slow * slow_tol_ratio)


def vcp_ok(close: pd.Series, *, min_bars: int, sd5_vs_sd20: float, sd5_vs_sd60: float, sd20_vs_sd60: float) -> bool:
    """Volatility contraction via rolling std ratios of daily returns."""
    if len(close) < max(60, min_bars) + 1:
        return False

    r = close.pct_change()
    sd5 = r.rolling(5, min_periods=5).std()
    sd20 = r.rolling(20, min_periods=20).std()
    sd60 = r.rolling(60, min_periods=60).std()

    a = float(sd5.iloc[-1])
    b = float(sd20.iloc[-1])
    c = float(sd60.iloc[-1])
    if any(math.isnan(x) or x <= 0 for x in [a, b, c]):
        return False

    return (a / b <= sd5_vs_sd20) and (a / c <= sd5_vs_sd60) and (b / c <= sd20_vs_sd60)


@dataclass(frozen=True)
class PowerPlayResult:
    ok: bool
    breakout_day: str | None
    breakout_close: float | None
    vol_ratio: float | None


def power_play(
    close: pd.Series,
    volume: pd.Series,
    *,
    lookback: int,
    breakout_lookback: int,
    vol_surge_mult: float,
    consol_min_bars: int,
    max_pullback_pct: float,
) -> PowerPlayResult:
    """Best-effort Power Play detector.

    Heuristic:
    - Find most recent breakout day within `breakout_lookback` where close hits new high
      vs prior window, and volume is surged.
    - After breakout, pullback must be within max_pullback_pct.

    Returns a small struct mainly for debugging/Telegram.
    """

    if len(close) < max(lookback, breakout_lookback) + 2:
        return PowerPlayResult(False, None, None, None)

    c = close.dropna()
    v = volume.reindex(c.index).dropna()
    if len(c) < max(lookback, breakout_lookback) + 2:
        return PowerPlayResult(False, None, None, None)

    vol_ma = v.rolling(50, min_periods=50).mean()

    # Identify breakout candidates within last breakout_lookback bars
    recent_idx = c.index[-breakout_lookback:]
    breakout_candidates = []
    for idx in recent_idx:
        # Prior high excluding today
        pos = c.index.get_loc(idx)
        if isinstance(pos, slice):
            continue
        if pos < 20:
            continue
        start = max(0, pos - breakout_lookback)
        prior = c.iloc[start:pos]
        if len(prior) < 20:
            continue
        prior_high = prior.max()
        if float(c.loc[idx]) < float(prior_high):
            continue
        vol = float(v.loc[idx])
        vm = float(vol_ma.loc[idx]) if not pd.isna(vol_ma.loc[idx]) else float(v.iloc[max(0, pos - 50):pos].mean())
        if vm <= 0:
            continue
        ratio = vol / vm
        if ratio >= vol_surge_mult:
            breakout_candidates.append((idx, float(c.loc[idx]), ratio))

    if not breakout_candidates:
        return PowerPlayResult(False, None, None, None)

    # Pick the most recent breakout
    b_idx, b_close, b_ratio = breakout_candidates[-1]

    # Consolidation check: need at least N bars after breakout (including today)
    after = c.loc[b_idx:]
    if len(after) < consol_min_bars:
        return PowerPlayResult(False, str(b_idx.date()), b_close, b_ratio)

    min_after = float(after.min())
    pullback_pct = (b_close - min_after) / b_close * 100.0
    if pullback_pct > max_pullback_pct:
        return PowerPlayResult(False, str(b_idx.date()), b_close, b_ratio)

    return PowerPlayResult(True, str(b_idx.date()), b_close, b_ratio)


def rebound_ok(
    mrs_series: pd.Series,
    *,
    lookback: int,
    past_max_threshold: float,
    cooldown_ratio: float,
    support_min: float,
) -> Tuple[bool, float | None, float | None]:
    """Return (ok, past_max, current)."""
    if len(mrs_series) < lookback:
        return False, None, None
    window = mrs_series.dropna().tail(lookback)
    if window.empty:
        return False, None, None

    past_max = float(window.max())
    current = float(window.iloc[-1])

    ok = (past_max >= past_max_threshold) and (current <= past_max * cooldown_ratio) and (current >= support_min)
    return ok, past_max, current
