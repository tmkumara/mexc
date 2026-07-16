"""One-off diagnostic: rerun backtest_symbol for specific symbols and print
every trade's full geometry (entry/tp/sl/rr), not just the aggregate
report, to inspect outlier RR values."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_nw
from backtest_simple_strategy import get_klines_extended
from backtest_nw_strategy import backtest_symbol

if __name__ == "__main__":
    symbols = sys.argv[1:]
    df_btc_full = get_klines_extended(config_nw.NW_BTC_FILTER_SYMBOL, config_nw.NW_BTC_FILTER_TF, 60)
    for symbol in symbols:
        trades = backtest_symbol(symbol, 60, df_btc_full)
        for t in trades:
            risk = abs(t.entry_price - t.sl_price)
            reward = abs(t.tp_price - t.entry_price)
            print(
                f"{t.symbol:15s} {t.direction:5s} entry={t.entry_price:.8g} "
                f"tp={t.tp_price:.8g} sl={t.sl_price:.8g} rr={t.rr:.2f} "
                f"risk_dist={risk:.8g} reward_dist={reward:.8g} "
                f"risk_pct={risk/t.entry_price*100:.5f}% outcome={t.outcome}"
            )
