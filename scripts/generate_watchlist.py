"""Generate NASDAQ+NYSE watchlist (exclude ETF/ADR/etc.)

This is an *independent* script that does NOT depend on the main scanner.

Data source:
- Nasdaq Trader Symbol Directory
  - https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  - https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

Output:
- watchlist.txt: one ticker per line (yfinance-friendly; '.' -> '-')

Usage:
    python scripts/generate_watchlist.py --output watchlist.txt --debug-csv scripts/watchlist_debug.csv

Notes:
- Filtering is best-effort: it uses ETF flags + name keyword heuristics to remove
  ADR/ETF/warrants/rights/units/preferred.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


BAD_NAME_PATTERNS = [
    r"\bETF\b",
    r"\bETN\b",
    r"\bTRUST\b",
    r"\bFUND\b",
    r"\bINDEX\b",
    r"\bSPDR\b",
    r"\biSHARES\b",
    r"\bVANGUARD\b",
    r"\bADR\b",
    r"\bADS\b",
    r"DEPOSITARY",
    r"\bPREFERRED\b",
    r"\bPFD\b",
    r"\bPREF\b",
    r"\bWARRANT\b",
    r"\bRIGHTS\b",
    r"\bRIGHT\b",
    r"\bUNIT\b",
    r"\bNOTES\b",
    r"\bBOND\b",
    r"\bDEBENTURE\b",
    r"\bSERIES\b\s+[A-Z0-9]+",
    r"\bCONVERTIBLE\b",
]
BAD_NAME_RE = re.compile("|".join(f"(?:{p})" for p in BAD_NAME_PATTERNS), re.IGNORECASE)


def _download_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _yahoo_symbol(sym: str) -> str:
    # yfinance uses '-' for class shares like BRK-B.
    sym = sym.strip().upper()
    sym = sym.replace(".", "-")
    return sym


@dataclass(frozen=True)
class Row:
    symbol: str
    name: str
    exchange: str
    etf_flag: str
    test_issue: str


def _parse_nasdaq_listed(text: str) -> List[Row]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # header line starts with Symbol|
    out: List[Row] = []
    for ln in lines:
        if ln.startswith("Symbol|") or ln.startswith("File Creation Time"):
            continue
        parts = ln.split("|")
        if len(parts) < 7:
            continue
        sym = parts[0]
        name = parts[1]
        test_issue = parts[3]
        etf = parts[6]
        out.append(Row(symbol=sym, name=name, exchange="NASDAQ", etf_flag=etf, test_issue=test_issue))
    return out


def _parse_other_listed(text: str) -> List[Row]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: List[Row] = []
    for ln in lines:
        if ln.startswith("ACT Symbol|") or ln.startswith("File Creation Time"):
            continue
        parts = ln.split("|")
        if len(parts) < 7:
            continue
        sym = parts[0]
        name = parts[1]
        exch = parts[2]
        etf = parts[4]
        test_issue = parts[6]
        out.append(Row(symbol=sym, name=name, exchange=exch, etf_flag=etf, test_issue=test_issue))
    return out


def _reason_to_exclude(row: Row) -> Optional[str]:
    sym = row.symbol.strip()
    if not sym or " " in sym or "$" in sym:
        return "bad_symbol"
    if row.test_issue.strip().upper() == "Y":
        return "test_issue"
    if row.etf_flag.strip().upper() == "Y":
        return "etf_flag"
    if BAD_NAME_RE.search(row.name or ""):
        return "name_keyword"
    return None


def generate_watchlist(
    *,
    include_nasdaq: bool,
    include_other: bool,
    other_exchanges: Sequence[str],
) -> Tuple[List[str], List[Tuple[str, str, str, str]]]:
    """Return (tickers, debug_rows)."""

    rows: List[Row] = []
    if include_nasdaq:
        rows.extend(_parse_nasdaq_listed(_download_text(NASDAQ_LISTED_URL)))
    if include_other:
        rows.extend(_parse_other_listed(_download_text(OTHER_LISTED_URL)))

    exch_keep = {x.strip().upper() for x in other_exchanges}

    tickers: List[str] = []
    debug: List[Tuple[str, str, str, str]] = []  # symbol, exchange, name, reason

    for r in rows:
        # Filter otherlisted exchanges
        if r.exchange not in {"NASDAQ"}:
            if r.exchange.strip().upper() not in exch_keep:
                debug.append((r.symbol, r.exchange, r.name, "exchange_filtered"))
                continue

        reason = _reason_to_exclude(r)
        if reason:
            debug.append((r.symbol, r.exchange, r.name, reason))
            continue

        t = _yahoo_symbol(r.symbol)
        tickers.append(t)

    # Deduplicate + sort for stability
    tickers = sorted(set(tickers))
    return tickers, debug


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="Output watchlist path")
    parser.add_argument("--debug-csv", default=None, help="Optional debug CSV path")
    parser.add_argument("--include-nasdaq", action="store_true", default=True)
    parser.add_argument("--include-other", action="store_true", default=True)
    parser.add_argument(
        "--other-exchanges",
        default="N",
        help="Comma-separated otherlisted exchanges to keep (default: N=NYSE). Use e.g. N,A,P to also include AMEX/ARCA.",
    )

    args = parser.parse_args()
    exchanges = [x.strip() for x in str(args.other_exchanges).split(",") if x.strip()]

    tickers, debug = generate_watchlist(
        include_nasdaq=bool(args.include_nasdaq),
        include_other=bool(args.include_other),
        other_exchanges=exchanges,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(tickers) + "\n", encoding="utf-8")

    if args.debug_csv:
        dpath = Path(args.debug_csv)
        dpath.parent.mkdir(parents=True, exist_ok=True)
        with dpath.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "exchange", "name", "reason"])
            w.writerows(debug)

    print(f"Generated {len(tickers)} tickers -> {out_path}")
    if args.debug_csv:
        print(f"Debug rows: {len(debug)} -> {args.debug_csv}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
