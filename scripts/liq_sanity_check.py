"""
Offline diagnostic: pull real MEXC data for a few known-liquid pairs, feed
each symbol's LiqEstimator from a handful of ticker snapshots, and print
the estimated clusters plus whether a signal/veto would fire right now.

Honest limitation: this samples open interest only a few seconds apart
(OI_SAMPLES times), whereas live usage samples every OI_POLL_SEC over
hours. Treat the printed clusters as a plumbing/sanity check, not a
realistic heatmap -- run the bot live for a while before trusting the
cluster shape.

Run: python scripts/liq_sanity_check.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mexc_client import get_klines, get_ticker
from strategy import _base_signal, _evaluate_liquidity, get_estimator
from config import SCALP_TF, SCALP_KLINE_COUNT

SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
OI_SAMPLES = 3
OI_SAMPLE_GAP_SECONDS = 2


def check_symbol(symbol: str) -> None:
    print(f"\n=== {symbol} ===")

    try:
        estimator = get_estimator(symbol)
        funding = 0.0
        for i in range(OI_SAMPLES):
            ticker = get_ticker(symbol)
            if ticker is None:
                print("  ticker: fetch failed")
                return
            estimator.on_oi_sample(ticker["hold_vol"] * ticker["fair_price"], ticker["fair_price"])
            estimator.decay_clusters()
            funding = ticker["funding_rate"]
            if i < OI_SAMPLES - 1:
                time.sleep(OI_SAMPLE_GAP_SECONDS)

        df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
        if df is None or df.empty or len(df) < 60:
            print("  klines: insufficient candles")
            return
        window = df.iloc[:-1]

        direction = _base_signal(window)
        price = float(window["close"].iloc[-1])
        print(f"  price={price:.6g} funding={funding * 100:.4f}% base_signal={direction}")

        clusters = estimator.significant_clusters(price)
        print(f"  significant clusters near price: {len(clusters)}")
        for bucket_price, side, magnitude in sorted(clusters, key=lambda c: c[0])[:10]:
            print(f"    {bucket_price:.6g} {side} magnitude={magnitude:,.0f}")

        if direction is None:
            print("  no base signal -- nothing to evaluate")
            return

        ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)
        if ok:
            print(f"  SIGNAL {direction} entry={price:.6g} tp={tp:.6g} sl={sl:.6g} -- {reason}")
        else:
            print(f"  VETO {direction} -- {reason}")
    except Exception as e:
        print(f"  error: {e}")
        return


if __name__ == "__main__":
    for sym in SYMBOLS:
        check_symbol(sym)
