"""Configuration for the US stock scanner.

All runtime-tunable parameters live here.

Notes
-----
- Use environment variables in Zeabur to override settings.
- Keep defaults conservative to avoid Yahoo rate-limit & Zeabur OOM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


def _env_str(name: str, default: str | None = None) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return val


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError as e:
        raise RuntimeError(f"Invalid int env var {name}={val!r}") from e


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except ValueError as e:
        raise RuntimeError(f"Invalid float env var {name}={val!r}") from e


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    TG_TOKEN: str
    TG_CHAT_ID: str

    # --- Files/State ---
    DB_PATH: str
    WATCHLIST_PATH: str

    # --- Main loop ---
    POLL_INTERVAL_SEC: int
    TRADING_DAY_ANCHOR: str
    TRADING_DAY_DETECT_PERIOD: str

    # --- Logging ---
    LOG_LEVEL: str

    # --- Yahoo download controls ---
    YF_DOWNLOAD_CHUNK_SIZE: int
    YF_DOWNLOAD_MAX_RETRIES: int
    YF_DOWNLOAD_BACKOFF_BASE_SEC: float

    # --- Two-pass periods ---
    YF_PERIOD_FIRST_PASS: str
    YF_PERIOD_SECOND_PASS: str
    YF_PERIOD_BENCHMARK: str

    # --- Benchmark selection ---
    BENCHMARK_SYMBOLS: Tuple[str, str]
    BENCHMARK_WEIGHT_20D: float
    BENCHMARK_WEIGHT_60D: float

    # --- Hard filters ---
    MIN_PRICE_USD: float
    MIN_ADV5_DOLLAR: float

    # --- RS gates ---
    RS_RANK_TOP_PCT: float
    MTF_GATE_MIN_HITS: int

    # Optional metadata filtering (slower)
    ENABLE_YF_TYPE_FILTER: bool
    TYPE_FILTER_MAX_CONCURRENCY: int

    # --- Trend ---
    SMA_FAST: int
    SMA_MID: int
    SMA_SLOW: int
    TREND_CHECK_WINDOW: int
    TREND_MIN_TRUE: int
    SMA_SLOW_TOL_RATIO: float

    # --- VCP ---
    VCP_MIN_BARS: int
    SD5_VS_SD20: float
    SD5_VS_SD60: float
    SD20_VS_SD60: float

    # --- Power Play ---
    PP_LOOKBACK: int
    PP_BREAKOUT_LOOKBACK: int
    PP_VOL_SURGE_MULT: float
    PP_CONSOL_MIN_BARS: int
    PP_MAX_PULLBACK_PCT: float

    # --- Rebound ---
    REBOUND_LOOKBACK: int
    REBOUND_COOLDOWN_RATIO: float
    REBOUND_SUPPORT_MIN: float
    REBOUND_THRESHOLD_METHOD: str
    REBOUND_PAST_MAX_FIXED: float
    REBOUND_PAST_MAX_QUANTILE: float

    # --- Output limits ---
    MOMENTUM_TOP_PCT: float
    MAX_PER_CATEGORY: int
    MAX_TOTAL_RESULTS: int

    # --- TradingView export (Telegram attachment) ---
    TV_EXPORT_FILENAME_PREFIX: str
    TV_SYMBOL_PREFIX: str
    TV_SYMBOL_SUFFIX: str
    TV_EXPORT_INCLUDE_EMPTY_SECTIONS: bool

    # --- TradingView per-symbol exchange prefixes (NASDAQ/NYSE) ---
    # Only used when TV_SYMBOL_PREFIX is empty.
    TV_EXCHANGE_AUTO_PREFIX: bool
    TV_EXCHANGE_CACHE_MAX_AGE_DAYS: int
    TV_EXCHANGE_RESOLVE_CONCURRENCY: int
    TV_EXCHANGE_RESOLVE_MAX_RETRIES: int
    TV_EXCHANGE_RESOLVE_BACKOFF_BASE_SEC: float

    # Optional: convert Yahoo class share tickers (BRK-B) to TradingView style (BRK.B).
    TV_CONVERT_YAHOO_DASH_TO_DOT: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            TG_TOKEN=_env_str("TG_TOKEN"),
            TG_CHAT_ID=_env_str("TG_CHAT_ID"),
            DB_PATH=os.getenv("DB_PATH", "/app/data/state.db"),
            WATCHLIST_PATH=os.getenv("WATCHLIST_PATH", "/app/watchlist.txt"),
            POLL_INTERVAL_SEC=_env_int("POLL_INTERVAL_SEC", 600),
            TRADING_DAY_ANCHOR=os.getenv("TRADING_DAY_ANCHOR", "QQQ"),
            TRADING_DAY_DETECT_PERIOD=os.getenv("TRADING_DAY_DETECT_PERIOD", "7d"),
            LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
            YF_DOWNLOAD_CHUNK_SIZE=_env_int("YF_DOWNLOAD_CHUNK_SIZE", 200),
            YF_DOWNLOAD_MAX_RETRIES=_env_int("YF_DOWNLOAD_MAX_RETRIES", 3),
            YF_DOWNLOAD_BACKOFF_BASE_SEC=_env_float("YF_DOWNLOAD_BACKOFF_BASE_SEC", 1.5),
            YF_PERIOD_FIRST_PASS=os.getenv("YF_PERIOD_FIRST_PASS", "6mo"),
            YF_PERIOD_SECOND_PASS=os.getenv("YF_PERIOD_SECOND_PASS", "1y"),
            YF_PERIOD_BENCHMARK=os.getenv("YF_PERIOD_BENCHMARK", "6mo"),
            BENCHMARK_SYMBOLS=(
                os.getenv("BENCHMARK_SYMBOL_1", "QQQ"),
                os.getenv("BENCHMARK_SYMBOL_2", "^GSPC"),
            ),
            BENCHMARK_WEIGHT_20D=_env_float("BENCHMARK_WEIGHT_20D", 0.3),
            BENCHMARK_WEIGHT_60D=_env_float("BENCHMARK_WEIGHT_60D", 0.7),
            MIN_PRICE_USD=_env_float("MIN_PRICE_USD", 15.0),
            MIN_ADV5_DOLLAR=_env_float("MIN_ADV5_DOLLAR", 10_000_000.0),
            RS_RANK_TOP_PCT=_env_float("RS_RANK_TOP_PCT", 0.10),
            MTF_GATE_MIN_HITS=_env_int("MTF_GATE_MIN_HITS", 3),
            ENABLE_YF_TYPE_FILTER=_env_bool("ENABLE_YF_TYPE_FILTER", False),
            TYPE_FILTER_MAX_CONCURRENCY=_env_int("TYPE_FILTER_MAX_CONCURRENCY", 5),
            SMA_FAST=_env_int("SMA_FAST", 20),
            SMA_MID=_env_int("SMA_MID", 50),
            SMA_SLOW=_env_int("SMA_SLOW", 100),
            TREND_CHECK_WINDOW=_env_int("TREND_CHECK_WINDOW", 20),
            TREND_MIN_TRUE=_env_int("TREND_MIN_TRUE", 15),
            SMA_SLOW_TOL_RATIO=_env_float("SMA_SLOW_TOL_RATIO", 1.0),
            VCP_MIN_BARS=_env_int("VCP_MIN_BARS", 125),
            SD5_VS_SD20=_env_float("SD5_VS_SD20", 0.65),
            SD5_VS_SD60=_env_float("SD5_VS_SD60", 0.55),
            SD20_VS_SD60=_env_float("SD20_VS_SD60", 0.75),
            PP_LOOKBACK=_env_int("PP_LOOKBACK", 125),
            PP_BREAKOUT_LOOKBACK=_env_int("PP_BREAKOUT_LOOKBACK", 55),
            PP_VOL_SURGE_MULT=_env_float("PP_VOL_SURGE_MULT", 2.5),
            PP_CONSOL_MIN_BARS=_env_int("PP_CONSOL_MIN_BARS", 5),
            PP_MAX_PULLBACK_PCT=_env_float("PP_MAX_PULLBACK_PCT", 8.0),
            REBOUND_LOOKBACK=_env_int("REBOUND_LOOKBACK", 60),
            REBOUND_COOLDOWN_RATIO=_env_float("REBOUND_COOLDOWN_RATIO", 0.5),
            REBOUND_SUPPORT_MIN=_env_float("REBOUND_SUPPORT_MIN", -2.0),
            REBOUND_THRESHOLD_METHOD=os.getenv("REBOUND_THRESHOLD_METHOD", "quantile"),
            REBOUND_PAST_MAX_FIXED=_env_float("REBOUND_PAST_MAX_FIXED", 20.0),
            REBOUND_PAST_MAX_QUANTILE=_env_float("REBOUND_PAST_MAX_QUANTILE", 0.9),
            MOMENTUM_TOP_PCT=_env_float("MOMENTUM_TOP_PCT", 0.10),
            MAX_PER_CATEGORY=_env_int("MAX_PER_CATEGORY", 20),
            MAX_TOTAL_RESULTS=_env_int("MAX_TOTAL_RESULTS", 80),
            TV_EXPORT_FILENAME_PREFIX=os.getenv("TV_EXPORT_FILENAME_PREFIX", "US-RS_"),
            TV_SYMBOL_PREFIX=os.getenv("TV_SYMBOL_PREFIX", ""),
            TV_SYMBOL_SUFFIX=os.getenv("TV_SYMBOL_SUFFIX", ""),
            TV_EXPORT_INCLUDE_EMPTY_SECTIONS=_env_bool("TV_EXPORT_INCLUDE_EMPTY_SECTIONS", True),

            # TradingView exchange prefix resolution
            TV_EXCHANGE_AUTO_PREFIX=_env_bool("TV_EXCHANGE_AUTO_PREFIX", True),
            TV_EXCHANGE_CACHE_MAX_AGE_DAYS=_env_int("TV_EXCHANGE_CACHE_MAX_AGE_DAYS", 30),
            TV_EXCHANGE_RESOLVE_CONCURRENCY=_env_int("TV_EXCHANGE_RESOLVE_CONCURRENCY", 5),
            TV_EXCHANGE_RESOLVE_MAX_RETRIES=_env_int("TV_EXCHANGE_RESOLVE_MAX_RETRIES", 2),
            TV_EXCHANGE_RESOLVE_BACKOFF_BASE_SEC=_env_float("TV_EXCHANGE_RESOLVE_BACKOFF_BASE_SEC", 1.2),
            TV_CONVERT_YAHOO_DASH_TO_DOT=_env_bool("TV_CONVERT_YAHOO_DASH_TO_DOT", False),
        )


CONFIG = Config.from_env()
