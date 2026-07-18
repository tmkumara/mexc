"""
backtest/fetch_data.py — download historical 1m klines from MEXC Futures
for the backtester, with pagination, rate-limit-friendly sleeps, and a gap
report.

MUST be run somewhere that can reach contract.mexc.com (the production
server, per CLAUDE.md -- NOT this sandbox, whose egress policy blocks
that host). See backtest/README.md.

Usage:
    python backtest/fetch_data.py --symbols BTC_USDT,ETH_USDT,SOL_USDT \\
        --months 6 --out backtest/data

    # Or derive the top-3 most-frequently-traded symbols from the bot's
    # own signals.db (run this on the server, where that DB is real):
    python backtest/fetch_data.py --from-db --top-n 3 --months 6

Output:
    backtest/data/<SYMBOL>_1m.parquet   -- columns: open, high, low, close, volume
                                            indexed by UTC timestamp (naive)
    backtest/data/<SYMBOL>_gaps.json    -- any missing-minute ranges found
    backtest/data/fetch_report.json     -- summary across all symbols
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from mexc_client import _get, _parse_kline_response  # reuse the exact parsing/retry logic the live bot relies on
from config import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("fetch_data")

DATA_DIR = Path(__file__).resolve().parent / "data"

# Conservative per-request window -- stays well under any documented MEXC
# kline row cap regardless of how many bars a given window actually
# contains, at the cost of more requests. Override with --chunk-minutes.
DEFAULT_CHUNK_MINUTES = 1440  # 1 day of 1m candles per request
DEFAULT_SLEEP_SECONDS = 0.35   # spacing between requests to stay well under MEXC's public rate limit


def top_symbols_from_db(top_n: int, db_path: str = DB_PATH) -> list[str]:
    """Read the bot's own signals.db and return the top_n most-frequently
    traded symbols. Only meaningful when run on a host with the real DB
    (i.e. the production server)."""
    import sqlite3

    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"{db_path} not found -- --from-db only works where the bot's real "
            f"signals.db lives (the production server). Pass --symbols instead."
        )
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT symbol FROM signals").fetchall()
    finally:
        con.close()
    counts = Counter(r[0] for r in rows)
    top = [sym for sym, _ in counts.most_common(top_n)]
    if not top:
        raise ValueError(f"{db_path} has no signals yet -- pass --symbols instead.")
    return top


def _fetch_chunk(symbol: str, start: int, end: int, retries: int = 5) -> pd.DataFrame:
    data = _get(
        f"/contract/kline/{symbol}",
        params={"interval": "Min1", "start": start, "end": end},
        retries=retries,
    )
    raw = data.get("data", {})
    return _parse_kline_response(raw)


def fetch_symbol(
    symbol: str,
    months: int,
    chunk_minutes: int = DEFAULT_CHUNK_MINUTES,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
) -> pd.DataFrame:
    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp())

    chunk_seconds = chunk_minutes * 60
    windows = []
    cursor = start_ts
    while cursor < end_ts:
        w_end = min(cursor + chunk_seconds, end_ts)
        windows.append((cursor, w_end))
        cursor = w_end

    logger.info("[%s] fetching %d windows (%d months, %dm chunks)", symbol, len(windows), months, chunk_minutes)

    frames = []
    for i, (w_start, w_end) in enumerate(windows):
        try:
            df = _fetch_chunk(symbol, w_start, w_end)
        except Exception as e:
            logger.error("[%s] window %d/%d (%s-%s) failed after retries: %s",
                         symbol, i + 1, len(windows), w_start, w_end, e)
            df = pd.DataFrame()

        if not df.empty:
            frames.append(df)

        if (i + 1) % 20 == 0 or i == len(windows) - 1:
            logger.info("[%s] progress %d/%d windows, %d bars so far",
                        symbol, i + 1, len(windows), sum(len(f) for f in frames))

        time.sleep(sleep_seconds)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames)
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)
    return combined


def find_gaps(df: pd.DataFrame, expected_seconds: int = 60) -> list[dict]:
    """Return a list of {start, end, missing_bars} for every place
    consecutive timestamps differ by more than expected_seconds."""
    if df.empty or len(df) < 2:
        return []

    idx = df.index.to_series()
    diffs = idx.diff().dt.total_seconds()

    gaps = []
    for ts, delta in zip(idx.index[1:], diffs.iloc[1:]):
        if pd.isna(delta) or delta == expected_seconds:
            continue
        prev_ts = ts - pd.Timedelta(seconds=delta)
        missing_bars = int(delta / expected_seconds) - 1
        gaps.append({
            "start": prev_ts.isoformat(),
            "end": ts.isoformat(),
            "gap_seconds": delta,
            "missing_bars": missing_bars,
        })
    return gaps


def save_symbol(symbol: str, df: pd.DataFrame, out_dir: Path, strict: bool) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}_1m.parquet"
    df.to_parquet(path)

    gaps = find_gaps(df)
    gaps_path = out_dir / f"{symbol}_gaps.json"
    gaps_path.write_text(json.dumps(gaps, indent=2))

    if gaps:
        total_missing = sum(g["missing_bars"] for g in gaps)
        logger.warning("[%s] %d gap(s) found, %d missing 1m bars total -- see %s",
                       symbol, len(gaps), total_missing, gaps_path)
        if strict:
            raise AssertionError(f"{symbol}: {len(gaps)} gap(s) found in 1m klines (--strict)")
    else:
        logger.info("[%s] no gaps -- %d consecutive 1m bars", symbol, len(df))

    return {
        "symbol": symbol,
        "bars": len(df),
        "start": df.index.min().isoformat() if not df.empty else None,
        "end": df.index.max().isoformat() if not df.empty else None,
        "gap_count": len(gaps),
        "missing_bars_total": sum(g["missing_bars"] for g in gaps),
        "parquet_path": str(path),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", type=str, default="",
                        help="Comma-separated MEXC futures symbols, e.g. BTC_USDT,ETH_USDT,SOL_USDT")
    parser.add_argument("--from-db", action="store_true",
                        help="Derive symbols from the bot's own signals.db (top-n by trade frequency). "
                             "Only useful on the production server.")
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--chunk-minutes", type=int, default=DEFAULT_CHUNK_MINUTES)
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--out", type=str, default=str(DATA_DIR))
    parser.add_argument("--strict", action="store_true",
                        help="Fail if any gaps are found instead of just logging/reporting them.")
    args = parser.parse_args()

    if args.from_db:
        symbols = top_symbols_from_db(args.top_n)
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        parser.error("Pass --symbols BTC_USDT,ETH_USDT,SOL_USDT or --from-db")
        return

    logger.info("Fetching %d months of 1m klines for: %s", args.months, symbols)

    out_dir = Path(args.out)
    report = {"symbols": [], "fetched_at": datetime.now(timezone.utc).isoformat(), "months": args.months}

    for symbol in symbols:
        df = fetch_symbol(symbol, args.months, args.chunk_minutes, args.sleep_seconds)
        if df.empty:
            logger.error("[%s] no data fetched -- skipping save", symbol)
            report["symbols"].append({"symbol": symbol, "bars": 0, "error": "no data fetched"})
            continue
        report["symbols"].append(save_symbol(symbol, df, out_dir, args.strict))

    report_path = out_dir / "fetch_report.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Done. Report: %s", report_path)


if __name__ == "__main__":
    main()
