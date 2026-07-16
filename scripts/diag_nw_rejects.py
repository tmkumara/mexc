"""Diagnostic: walk the same as-of backtest loop as backtest_nw_strategy.py
but aggregate reject_sink counts across all symbols/bars instead of
recording trades, to find which gate in evaluate_symbol_nw is the
bottleneck. ProcessPoolExecutor per symbol, same proven-reliable pattern as
the backtest harness itself.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nw_strategy
import config_nw
from backtest_simple_strategy import get_klines_extended
from backtest_nw_strategy import _with_forming_row, _find_as_of_index, _TF_MINUTES


def diag_symbol(symbol: str, days: int, df_btc_full: pd.DataFrame) -> Counter:
    counts: Counter = Counter()

    df_4h_full = get_klines_extended(symbol, config_nw.NW_TREND_TF, days)
    df_1h_full = get_klines_extended(symbol, config_nw.NW_ENTRY_TF, days)

    if df_4h_full.empty or df_1h_full.empty:
        counts["no_history_fetched"] += 1
        return counts

    min_start = max(
        config_nw.NW_TREND_EMA_PERIOD + 5,
        config_nw.NW_SWING_LOOKBACK_BARS + config_nw.NW_SWING_FRACTAL_BARS + config_nw.NW_VOLUME_MA_PERIOD + 15,
    )

    original_get_market_klines = nw_strategy.get_market_klines
    candle_minutes = _TF_MINUTES[config_nw.NW_ENTRY_TF]
    trend_tf_minutes = _TF_MINUTES[config_nw.NW_TREND_TF]
    btc_tf_minutes = _TF_MINUTES[config_nw.NW_BTC_FILTER_TF]

    try:
        for i in range(min_start, len(df_1h_full) - 1):
            ts = df_1h_full.index[i]
            eval_time = ts + pd.Timedelta(minutes=candle_minutes)
            trend_idx = _find_as_of_index(df_4h_full, eval_time - pd.Timedelta(minutes=trend_tf_minutes))
            btc_idx = (
                _find_as_of_index(df_btc_full, eval_time - pd.Timedelta(minutes=btc_tf_minutes))
                if not df_btc_full.empty else None
            )
            if trend_idx is None or trend_idx < config_nw.NW_TREND_EMA_PERIOD + 5:
                continue

            as_of_1h = _with_forming_row(df_1h_full, i, config_nw.NW_ENTRY_KLINE_COUNT)
            as_of_4h = _with_forming_row(df_4h_full, trend_idx, config_nw.NW_TREND_KLINE_COUNT)
            as_of_btc = _with_forming_row(df_btc_full, btc_idx, config_nw.NW_TREND_KLINE_COUNT) if btc_idx is not None else None

            def _fake(sym, interval, count=100, _1h=as_of_1h, _4h=as_of_4h, _btc=as_of_btc):
                if sym == config_nw.NW_BTC_FILTER_SYMBOL and interval == config_nw.NW_BTC_FILTER_TF:
                    return _btc if _btc is not None else pd.DataFrame()
                if interval == config_nw.NW_ENTRY_TF:
                    return _1h
                if interval == config_nw.NW_TREND_TF:
                    return _4h
                return pd.DataFrame()

            nw_strategy.get_market_klines = _fake

            sink: dict = {}
            sig = nw_strategy.evaluate_symbol_nw(symbol, reject_sink=sink)
            if sig is not None:
                counts["FIRED"] += 1
            for k, v in sink.items():
                counts[k] += v
    finally:
        nw_strategy.get_market_klines = original_get_market_klines

    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    df_btc_full = get_klines_extended(config_nw.NW_BTC_FILTER_SYMBOL, config_nw.NW_BTC_FILTER_TF, args.days)

    total: Counter = Counter()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(diag_symbol, symbol, args.days, df_btc_full): symbol
            for symbol in args.symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                counts = future.result()
                total.update(counts)
                print(f"[{symbol}] {dict(counts)}", flush=True)
            except Exception as e:
                print(f"[{symbol}] FAILED: {e}", flush=True)

    print("\n" + "=" * 60)
    print("AGGREGATE REJECT REASON COUNTS:")
    grand_total = sum(total.values())
    for reason, count in total.most_common():
        pct = count / grand_total * 100 if grand_total else 0.0
        print(f"  {reason:30s} {count:6d}  ({pct:5.1f}%)")


if __name__ == "__main__":
    sys.exit(main() or 0)
