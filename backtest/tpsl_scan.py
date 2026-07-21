"""
Quick TP/SL asymmetry scan -- NOT a full walk-forward re-optimization.

Fixes the confluence/regime params to what the votes=2 walk-forward run
already found reasonable for each symbol (see backtest/reports_votes2/
phase4_report.md), then sweeps SCALPER_V3_TARGET_ROI_PCT /
SCALPER_V3_MAX_SL_ROI_PCT (the flat TP/SL sizing in scalper_v3_strategy.
_calc_tp_sl) across several ratios, backtesting the FULL 6-month history
in one pass per ratio (no train/test split, no grid search) for a fast
directional read on whether shrinking/widening the TP relative to the SL
moves win rate and profit factor in a useful direction.

This is deliberately cheap (a handful of single-param backtests instead
of 243-combo grids x windows) precisely because it's exploratory -- any
ratio that looks promising here should go through the same walk-forward
optimize.py treatment as everything else before being trusted.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import scalper_v3_strategy as v3s
from backtest.engine import BacktestParams, run_backtest, compute_metrics
from backtest.optimize import load_symbol_data

# (label, tp_roi_pct, sl_roi_pct)
RATIOS = [
    ("1:1 (current)", 10.0, 10.0),
    ("1:2 (small TP, wide SL)", 5.0, 10.0),
    ("1:3 (small TP, wide SL)", 5.0, 15.0),
    ("1.5:1 (wide TP, tight SL)", 15.0, 10.0),
    ("2:1 (wide TP, tight SL)", 20.0, 10.0),
    ("3:1 (wide TP, tight SL)", 30.0, 10.0),
    ("4:1 (wide TP, tight SL)", 40.0, 10.0),
]

# Most-recently-recommended combo per symbol from the votes=2 walk-forward run
SYMBOL_COMBOS = {
    "WLD_USDT": {"atr_mult": 2.5, "entry_zone": 0.55, "adx_min": 26, "chop_max": 50, "min_strength": 2},
    "XRP_USDT": {"atr_mult": 2.0, "entry_zone": 0.55, "adx_min": 22, "chop_max": 45, "min_strength": 2},
}


def main():
    for symbol, combo in SYMBOL_COMBOS.items():
        print(f"\n{'=' * 70}\n{symbol}\n{'=' * 70}")
        df_1m = load_symbol_data(symbol, "5m")

        kwargs = v3s.scalper_kwargs()
        kwargs.update({k: v for k, v in combo.items() if k != "min_strength"})

        for label, tp_roi, sl_roi in RATIOS:
            cfg.SCALPER_V3_TARGET_ROI_PCT = tp_roi
            cfg.SCALPER_V3_MAX_SL_ROI_PCT = sl_roi
            cfg.SCALPER_V3_TP_PRICE_PCT = tp_roi / 100.0 / cfg.LEVERAGE
            cfg.SCALPER_V3_MAX_SL_PRICE_PCT = sl_roi / 100.0 / cfg.LEVERAGE

            params = BacktestParams(
                scalper_kwargs=kwargs, min_strength=combo["min_strength"],
                min_regime_votes=2, entry_timeframe="5m", warmup_bars=250,
            )
            result = run_backtest(df_1m, symbol, params)
            m = compute_metrics(result.trades, params.initial_equity)

            pf = m["profit_factor"]
            pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
            print(
                f"  {label:<28} trades={m['total_trades']:>4}  "
                f"WR={m['win_rate']:>6.2f}%  PF={pf_str:>8}  "
                f"maxDD={m['max_drawdown_pct']:>7.2f}%  "
                f"totalReturn={m['total_return_pct']:>8.2f}%"
            )


if __name__ == "__main__":
    main()
