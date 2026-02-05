"""TradingView watchlist export helpers.

This project sends scan results to Telegram. Many users also want to import the
scan output into TradingView as a watchlist.

TradingView watchlist import supports *sections* when a line starts with
"###" (e.g. "###Tech"). Lines after a section header are treated as symbols.

We keep this module stdlib-only to avoid bloating requirements.txt.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Mapping, Sequence


# (emoji, human label) pairs used for TradingView section headers.
# Keep the emoji as the dict keys to match scanner output categories.
CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("⭐️", "STAR"),
    ("🧱", "BRICK"),
    ("♻️", "RECYCLE"),
    ("🪢", "KNOT"),
    ("🍄", "MUSHROOM"),
)


def yymmdd_from_iso(trading_day_iso: str) -> str:
    """Convert YYYY-MM-DD to YYMMDD.

    Args:
        trading_day_iso: Date string like "2026-02-05".

    Returns:
        A compact date string like "260205".

    Raises:
        ValueError: If the input is not a valid YYYY-MM-DD.
    """

    dt = datetime.strptime(trading_day_iso, "%Y-%m-%d")
    return dt.strftime("%y%m%d")


def build_filename(trading_day_iso: str, *, prefix: str = "US-RS_") -> str:
    """Build the TradingView import filename.

    Args:
        trading_day_iso: Date string like "2026-02-05".
        prefix: Filename prefix.

    Returns:
        Filename like "US-RS_260205.txt".
    """

    return f"{prefix}{yymmdd_from_iso(trading_day_iso)}.txt"


_CLASS_SHARE_RE = re.compile(r"^([A-Z0-9]+)-([A-Z])$")


def _normalize_for_tradingview(symbol: str, *, convert_yahoo_dash_to_dot: bool) -> str:
    """Normalize symbols for TradingView import.

    Yahoo Finance / yfinance often represents class shares as `BRK-B`, while
    TradingView commonly uses `BRK.B`.

    This conversion is **optional** and intentionally conservative to avoid
    altering tickers that legitimately contain dashes.
    """

    s = symbol.strip().upper()
    if not s:
        return ""

    if convert_yahoo_dash_to_dot:
        m = _CLASS_SHARE_RE.match(s)
        if m is not None:
            s = f"{m.group(1)}.{m.group(2)}"
    return s


def _format_symbol(
    symbol: str,
    *,
    sym_prefix: str = "",
    sym_suffix: str = "",
    per_symbol_prefix: Mapping[str, str] | None = None,
    convert_yahoo_dash_to_dot: bool = False,
) -> str:
    """Format a raw ticker into a TradingView import symbol."""

    s = _normalize_for_tradingview(symbol, convert_yahoo_dash_to_dot=convert_yahoo_dash_to_dot)
    if not s:
        return ""

    prefix = sym_prefix
    if per_symbol_prefix is not None:
        # Per-symbol prefix takes precedence (e.g. NASDAQ: / NYSE:), but allow
        # callers to pass raw tickers in various cases.
        prefix = per_symbol_prefix.get(s) or per_symbol_prefix.get(symbol.strip().upper()) or prefix

    if prefix:
        s = f"{prefix}{s}"
    if sym_suffix:
        s = f"{s}{sym_suffix}"
    return s


def build_watchlist_text(
    categories: Mapping[str, Sequence[str]],
    *,
    sym_prefix: str = "",
    sym_suffix: str = "",
    per_symbol_prefix: Mapping[str, str] | None = None,
    convert_yahoo_dash_to_dot: bool = False,
    include_empty_sections: bool = True,
    header_with_count: bool = True,
) -> str:
    """Build a TradingView-importable watchlist text.

    File format (per TradingView community behavior):
    - A line starting with "###" becomes a section title.
    - Each subsequent line is interpreted as a symbol.

    Notes:
    - Avoid adding extra comments in the file; non-symbol lines may show up as
      invalid tickers.

    Args:
        categories: Mapping from category emoji (e.g. "⭐️") to a list of tickers.
        sym_prefix: Optional prefix for every symbol, e.g. "BINANCE:".
        sym_suffix: Optional suffix for every symbol, e.g. ".P".
        per_symbol_prefix: Optional mapping for per-symbol prefixes (e.g. NASDAQ:/NYSE:).
            When provided, it overrides ``sym_prefix`` for symbols present in the mapping.
        convert_yahoo_dash_to_dot: If True, convert conservative class-share tickers
            from Yahoo style (e.g. ``BRK-B``) to TradingView style (``BRK.B``).
        include_empty_sections: If False, drop empty categories.
        header_with_count: If True, append "(N)" to section titles.

    Returns:
        UTF-8 text content ending with a newline.
    """

    lines: list[str] = []

    for emoji, label in CATEGORY_ORDER:
        items = list(categories.get(emoji, ()))
        if not items and not include_empty_sections:
            continue

        title = f"{emoji} {label}"
        if header_with_count:
            title = f"{title} ({len(items)})"

        lines.append(f"###{title}")

        for t in items:
            tv_symbol = _format_symbol(
                t,
                sym_prefix=sym_prefix,
                sym_suffix=sym_suffix,
                per_symbol_prefix=per_symbol_prefix,
                convert_yahoo_dash_to_dot=convert_yahoo_dash_to_dot,
            )
            if tv_symbol:
                lines.append(tv_symbol)

        # Visual spacing between sections.
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
