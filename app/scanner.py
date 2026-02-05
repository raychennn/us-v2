"""Market scanning logic.

The scanner runs in two passes for performance/stability:
- Pass 1: download short period for all tickers -> apply hard filters -> rank by 60D RS to form candidate pool
- Pass 2: download longer period for candidate pool -> compute multi-timeframe RS gates + technical filters -> produce categorized output

All price series use Yahoo Finance adjusted prices (auto_adjust=True).
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import Config
from .indicators import (
    mrs,
    rel_return_vs_benchmark,
    last_adv_dollar,
    trend_ok,
    vcp_ok,
    power_play,
    rebound_ok,
)
from .yahoo_data import download_ohlcv_in_chunks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanOutput:
    trading_day: str
    benchmark: str
    message: str
    categories: Dict[str, List[str]]


def read_watchlist(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"watchlist not found: {path}. Please generate it via scripts/generate_watchlist.py")

    tickers: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tickers.append(s)

    # Dedup while preserving order
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def _close(df: pd.DataFrame) -> pd.Series:
    return df["Close"].dropna()


def _volume(df: pd.DataFrame) -> pd.Series:
    return df["Volume"].dropna()


def get_latest_trading_day(anchor: str, cfg: Config) -> str:
    """Return latest daily bar date (YYYY-MM-DD) for anchor symbol."""
    res = download_ohlcv_in_chunks(
        [anchor],
        cfg.TRADING_DAY_DETECT_PERIOD,
        chunk_size=1,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )
    df = res.data.get(anchor)
    if df is None or df.empty:
        raise RuntimeError(f"Unable to detect trading day: no data for {anchor}")
    d = df.index[-1].date()
    return d.isoformat()


def choose_benchmark(cfg: Config) -> Tuple[str, pd.Series]:
    """Choose the stronger benchmark between (QQQ, ^GSPC) by weighted 20D/60D returns."""
    b1, b2 = cfg.BENCHMARK_SYMBOLS
    res = download_ohlcv_in_chunks(
        [b1, b2],
        cfg.YF_PERIOD_BENCHMARK,
        chunk_size=2,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )
    if b1 not in res.data or b2 not in res.data:
        raise RuntimeError("Failed to download benchmark data")

    s1 = _close(res.data[b1])
    s2 = _close(res.data[b2])

    # Align to common index
    s1a, s2a = s1.align(s2, join="inner")
    if len(s1a) < 61:
        raise RuntimeError("Benchmark data too short")

    def score(series: pd.Series) -> float:
        r20 = float(series.iloc[-1] / series.iloc[-21] - 1.0) if len(series) >= 21 else 0.0
        r60 = float(series.iloc[-1] / series.iloc[-61] - 1.0) if len(series) >= 61 else 0.0
        return cfg.BENCHMARK_WEIGHT_20D * r20 + cfg.BENCHMARK_WEIGHT_60D * r60

    sc1 = score(s1a)
    sc2 = score(s2a)

    chosen = b1 if sc1 >= sc2 else b2
    chosen_series = s1 if chosen == b1 else s2
    logger.info("Benchmark chosen: %s (score=%.4f vs %.4f)", chosen, sc1, sc2)
    return chosen, chosen_series


def _median_tail(series: pd.Series, n: int = 3) -> float:
    s = series.dropna().tail(n)
    if s.empty:
        return float("nan")
    return float(s.median())


def scan_market(cfg: Config) -> ScanOutput:
    """Run full scan and return formatted Telegram message."""

    tickers = read_watchlist(cfg.WATCHLIST_PATH)
    if not tickers:
        raise RuntimeError("watchlist is empty")

    # trading day (from anchor)
    trading_day = get_latest_trading_day(cfg.TRADING_DAY_ANCHOR, cfg)

    benchmark, bench_close = choose_benchmark(cfg)

    # --- Pass 1: download short period for all tickers ---
    res1 = download_ohlcv_in_chunks(
        tickers,
        cfg.YF_PERIOD_FIRST_PASS,
        chunk_size=cfg.YF_DOWNLOAD_CHUNK_SIZE,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )

    # Ensure benchmark series is aligned to pass1 date index for return calculations
    # If pass1 benchmark period differs, download benchmark again for pass1 period.
    bench_pass1_res = download_ohlcv_in_chunks(
        [benchmark],
        cfg.YF_PERIOD_FIRST_PASS,
        chunk_size=1,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )
    bench_pass1 = _close(bench_pass1_res.data.get(benchmark, pd.DataFrame()))
    if bench_pass1.empty:
        raise RuntimeError("benchmark data empty for pass1")

    rows = []
    skipped_short = 0
    for t, df in res1.data.items():
        c = _close(df)
        v = _volume(df)
        if len(c) < 61 or len(v) < 6:
            skipped_short += 1
            continue
        last_close = float(c.iloc[-1])
        adv5 = last_adv_dollar(c, v, 5)
        if adv5 is None:
            continue
        if last_close < cfg.MIN_PRICE_USD:
            continue
        if adv5 < cfg.MIN_ADV5_DOLLAR:
            continue

        rr60 = rel_return_vs_benchmark(c, bench_pass1, 60)
        rr60_last = float(rr60.dropna().iloc[-1]) if not rr60.dropna().empty else float("nan")
        if pd.isna(rr60_last):
            continue

        rows.append((t, rr60_last, last_close, adv5))

    if not rows:
        raise RuntimeError("No tickers left after hard filters. Check watchlist & thresholds.")

    df1 = pd.DataFrame(rows, columns=["ticker", "rr60", "close", "adv5"]).sort_values("rr60", ascending=False)
    top_n = max(1, int(len(df1) * cfg.RS_RANK_TOP_PCT))
    candidate = df1.head(top_n)["ticker"].tolist()

    logger.info("Pass1: tickers=%d, after_filters=%d, candidates=%d, failed_download=%d, skipped_short=%d", len(tickers), len(df1), len(candidate), len(res1.failed), skipped_short)

    # --- Pass 2: download longer period only for candidate pool ---
    res2 = download_ohlcv_in_chunks(
        candidate,
        cfg.YF_PERIOD_SECOND_PASS,
        chunk_size=min(cfg.YF_DOWNLOAD_CHUNK_SIZE, 150),
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )

    bench_pass2_res = download_ohlcv_in_chunks(
        [benchmark],
        cfg.YF_PERIOD_SECOND_PASS,
        chunk_size=1,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )
    bench_pass2 = _close(bench_pass2_res.data.get(benchmark, pd.DataFrame()))
    if bench_pass2.empty:
        raise RuntimeError("benchmark data empty for pass2")

    # Build per-symbol RS scores (for cross-sectional ranking)
    scores_1d: Dict[str, float] = {}
    scores_5d: Dict[str, float] = {}
    scores_20d: Dict[str, float] = {}
    scores_60d: Dict[str, float] = {}

    # Keep per-symbol computed series for later checks
    computed: Dict[str, Dict[str, object]] = {}

    for t, df in res2.data.items():
        c = _close(df)
        v = _volume(df)
        if len(c) < 125 or len(v) < 125:
            continue
        # Align with benchmark
        c_al, b_al = c.align(bench_pass2, join="inner")
        if len(c_al) < 125:
            continue

        log_rs = (c_al.apply(lambda x: math.log(x))) - (b_al.apply(lambda x: math.log(x)))
        m5 = mrs(log_rs, 5)
        m20 = mrs(log_rs, 20)
        m60 = mrs(log_rs, 60)

        rr1 = (c_al.pct_change() - b_al.pct_change()).iloc[-1]
        scores_1d[t] = float(rr1)
        scores_5d[t] = _median_tail(m5, 3)
        scores_20d[t] = _median_tail(m20, 3)
        scores_60d[t] = _median_tail(m60, 3)

        computed[t] = {
            "close": c_al,
            "volume": v.reindex(c_al.index),
            "m5": m5,
            "m20": m20,
            "m60": m60,
        }

    if not computed:
        raise RuntimeError("No candidates have sufficient data in pass2")

    # Cross-sectional gates within candidate pool
    def top_set(score_map: Dict[str, float]) -> set[str]:
        s = pd.Series(score_map).dropna().sort_values(ascending=False)
        if s.empty:
            return set()
        n = max(1, int(len(s) * cfg.RS_RANK_TOP_PCT))
        return set(s.head(n).index.tolist())

    gate_1d = top_set(scores_1d)
    gate_5d = top_set(scores_5d)
    gate_20d = top_set(scores_20d)
    gate_60d = top_set(scores_60d)

    # Rebound threshold (cross-sectional on past_max of MRS20)
    past_maxes = {}
    for t, bag in computed.items():
        m20 = bag["m20"]
        window = m20.dropna().tail(cfg.REBOUND_LOOKBACK)
        if not window.empty:
            past_maxes[t] = float(window.max())

    if cfg.REBOUND_THRESHOLD_METHOD.lower() == "fixed":
        rebound_threshold = cfg.REBOUND_PAST_MAX_FIXED
    else:
        s = pd.Series(past_maxes).dropna()
        rebound_threshold = float(s.quantile(cfg.REBOUND_PAST_MAX_QUANTILE)) if not s.empty else cfg.REBOUND_PAST_MAX_FIXED

    # Momentum top set (among tech-passed later)

    # Run technical checks + categorize
    cats = {"⭐️": [], "🧱": [], "♻️": [], "🪢": [], "🍄": []}

    # Precompute momentum ranks based on 5D score
    score5_series = pd.Series(scores_5d).dropna().sort_values(ascending=False)

    tech_passed_list: List[str] = []
    per_symbol = {}

    for t, bag in computed.items():
        hits = 0
        if t in gate_1d:
            hits += 1
        if t in gate_5d:
            hits += 1
        if t in gate_20d:
            hits += 1
        if t in gate_60d:
            hits += 1

        mtf_ok = hits >= cfg.MTF_GATE_MIN_HITS

        close = bag["close"]
        vol = bag["volume"]
        vcp = vcp_ok(
            close,
            min_bars=cfg.VCP_MIN_BARS,
            sd5_vs_sd20=cfg.SD5_VS_SD20,
            sd5_vs_sd60=cfg.SD5_VS_SD60,
            sd20_vs_sd60=cfg.SD20_VS_SD60,
        )
        pp = power_play(
            close,
            vol,
            lookback=cfg.PP_LOOKBACK,
            breakout_lookback=cfg.PP_BREAKOUT_LOOKBACK,
            vol_surge_mult=cfg.PP_VOL_SURGE_MULT,
            consol_min_bars=cfg.PP_CONSOL_MIN_BARS,
            max_pullback_pct=cfg.PP_MAX_PULLBACK_PCT,
        )
        tech_strength = (1 if vcp else 0) + (1 if pp.ok else 0)
        tech_ok = tech_strength >= 1
        if tech_ok:
            tech_passed_list.append(t)

        trend = trend_ok(
            close,
            sma_fast=cfg.SMA_FAST,
            sma_mid=cfg.SMA_MID,
            sma_slow=cfg.SMA_SLOW,
            check_window=cfg.TREND_CHECK_WINDOW,
            min_true=cfg.TREND_MIN_TRUE,
            slow_tol_ratio=cfg.SMA_SLOW_TOL_RATIO,
        )

        reb_ok, reb_past_max, reb_cur = rebound_ok(
            bag["m20"],
            lookback=cfg.REBOUND_LOOKBACK,
            past_max_threshold=rebound_threshold,
            cooldown_ratio=cfg.REBOUND_COOLDOWN_RATIO,
            support_min=cfg.REBOUND_SUPPORT_MIN,
        )

        per_symbol[t] = {
            "hits": hits,
            "mtf_ok": mtf_ok,
            "trend": trend,
            "vcp": vcp,
            "pp": pp,
            "tech_strength": tech_strength,
            "tech_ok": tech_ok,
            "reb_ok": reb_ok,
            "reb_past_max": reb_past_max,
            "reb_cur": reb_cur,
            "score5": scores_5d.get(t),
        }

    # Momentum set among tech-passed
    tech_score5 = score5_series.loc[score5_series.index.intersection(tech_passed_list)]
    mom_n = max(1, int(len(tech_score5) * cfg.MOMENTUM_TOP_PCT)) if len(tech_score5) else 0
    momentum_set = set(tech_score5.head(mom_n).index.tolist()) if mom_n > 0 else set()

    # Categorize by priority
    used: set[str] = set()

    def add(cat: str, t: str) -> None:
        if t in used:
            return
        if len(cats[cat]) >= cfg.MAX_PER_CATEGORY:
            return
        cats[cat].append(t)
        used.add(t)

    for t, m in per_symbol.items():
        if m["mtf_ok"] and m["reb_ok"] and m["tech_ok"]:
            add("⭐️", t)

    for t, m in per_symbol.items():
        if m["mtf_ok"] and m["tech_ok"]:
            add("🧱", t)

    for t, m in per_symbol.items():
        if m["reb_ok"] and m["tech_ok"]:
            add("♻️", t)

    for t, m in per_symbol.items():
        if m["tech_strength"] == 2:
            add("🪢", t)

    for t, m in per_symbol.items():
        if m["tech_ok"] and (t in momentum_set):
            add("🍄", t)

    # Compose message
    total = sum(len(v) for v in cats.values())

    lines: List[str] = []
    lines.append(f"🕒 US Stock Scan | {trading_day}")
    lines.append(f"Benchmark: {benchmark}")
    lines.append(f"Universe: {len(tickers)} | Pass1 after filters: {len(df1)} | Candidates: {len(candidate)} | Output: {total}")
    if res1.failed:
        lines.append(f"⚠️ Pass1 missing: {len(res1.failed)}")
    if res2.failed:
        lines.append(f"⚠️ Pass2 missing: {len(res2.failed)}")
    lines.append("")

    def fmt_symbol(t: str) -> str:
        m = per_symbol.get(t, {})
        hits = m.get("hits")
        ts = m.get("tech_strength")
        mom = "🚀" if t in momentum_set else ""
        return f"{t} | MTF:{hits}/4 | Tech:{ts} {mom}".strip()

    for cat in ["⭐️", "🧱", "♻️", "🪢", "🍄"]:
        items = cats[cat]
        if not items:
            continue
        lines.append(f"{cat} ({len(items)})")
        for i, t in enumerate(items, 1):
            lines.append(f"{i}. {fmt_symbol(t)}")
        lines.append("")

    message = "\n".join(lines).strip()
    return ScanOutput(trading_day=trading_day, benchmark=benchmark, message=message, categories=cats)


def check_symbol(cfg: Config, symbol: str) -> str:
    """Single-symbol diagnostic (best-effort)."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise RuntimeError("symbol is empty")

    benchmark, _ = choose_benchmark(cfg)

    res = download_ohlcv_in_chunks(
        [symbol, benchmark],
        cfg.YF_PERIOD_SECOND_PASS,
        chunk_size=2,
        auto_adjust=True,
        max_retries=cfg.YF_DOWNLOAD_MAX_RETRIES,
        backoff_base_sec=cfg.YF_DOWNLOAD_BACKOFF_BASE_SEC,
    )

    df = res.data.get(symbol)
    bdf = res.data.get(benchmark)
    if df is None or df.empty:
        return f"❌ {symbol}: Yahoo 無法取得資料（可能 ticker 錯誤或已下市）"
    if bdf is None or bdf.empty:
        return f"❌ benchmark {benchmark}: 無法取得資料"

    c = _close(df)
    v = _volume(df)
    bc = _close(bdf)

    c_al, bc_al = c.align(bc, join="inner")
    v_al = v.reindex(c_al.index)

    last_close = float(c_al.iloc[-1])
    adv5 = last_adv_dollar(c_al, v_al, 5)

    # RS/MRS
    log_rs = (c_al.apply(lambda x: math.log(x))) - (bc_al.apply(lambda x: math.log(x)))
    m5 = mrs(log_rs, 5)
    m20 = mrs(log_rs, 20)
    m60 = mrs(log_rs, 60)
    rr1 = float((c_al.pct_change() - bc_al.pct_change()).iloc[-1])

    # Technical
    tr_ok = trend_ok(
        c_al,
        sma_fast=cfg.SMA_FAST,
        sma_mid=cfg.SMA_MID,
        sma_slow=cfg.SMA_SLOW,
        check_window=cfg.TREND_CHECK_WINDOW,
        min_true=cfg.TREND_MIN_TRUE,
        slow_tol_ratio=cfg.SMA_SLOW_TOL_RATIO,
    )
    vc_ok = vcp_ok(
        c_al,
        min_bars=cfg.VCP_MIN_BARS,
        sd5_vs_sd20=cfg.SD5_VS_SD20,
        sd5_vs_sd60=cfg.SD5_VS_SD60,
        sd20_vs_sd60=cfg.SD20_VS_SD60,
    )
    pp = power_play(
        c_al,
        v_al,
        lookback=cfg.PP_LOOKBACK,
        breakout_lookback=cfg.PP_BREAKOUT_LOOKBACK,
        vol_surge_mult=cfg.PP_VOL_SURGE_MULT,
        consol_min_bars=cfg.PP_CONSOL_MIN_BARS,
        max_pullback_pct=cfg.PP_MAX_PULLBACK_PCT,
    )

    lines = []
    lines.append(f"🔎 Check {symbol}")
    lines.append(f"Benchmark: {benchmark}")
    lines.append("")

    def ok(flag: bool) -> str:
        return "✅" if flag else "❌"

    lines.append(f"{ok(last_close >= cfg.MIN_PRICE_USD)} 價格 >= {cfg.MIN_PRICE_USD}: {last_close:.2f}")
    if adv5 is None:
        lines.append("❌ ADV5 無法計算（資料不足）")
    else:
        lines.append(f"{ok(adv5 >= cfg.MIN_ADV5_DOLLAR)} ADV5$ >= {cfg.MIN_ADV5_DOLLAR:,.0f}: {adv5:,.0f}")

    lines.append("")
    lines.append(f"RR_1D (vs bench): {rr1*100:.2f}%")
    lines.append(f"MRS_5:  {float(m5.dropna().iloc[-1]) if not m5.dropna().empty else float('nan'):.2f}")
    lines.append(f"MRS_20: {float(m20.dropna().iloc[-1]) if not m20.dropna().empty else float('nan'):.2f}")
    lines.append(f"MRS_60: {float(m60.dropna().iloc[-1]) if not m60.dropna().empty else float('nan'):.2f}")
    lines.append("(RS Rank/Top% gate 需要全市場橫截面計算，因此 /check 只顯示數值)")

    lines.append("")
    lines.append(f"{ok(tr_ok)} Trend")
    lines.append(f"{ok(vc_ok)} VCP")
    lines.append(f"{ok(pp.ok)} PowerPlay" + (f" | breakout={pp.breakout_day} vol_ratio={pp.vol_ratio:.2f}" if pp.vol_ratio else ""))

    return "\n".join(lines).strip()
