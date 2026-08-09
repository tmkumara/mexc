"""
Breakeven-aware single-TP/SL outcome determination for Precision Pullback
Scalper v1's pending signals.

Same-candle tie-break, checked in this order every candle: (1) the
CURRENT stop (original SL, or entry_price once breakeven has triggered)
-- if hit, closes the trade; (2) TP -- if hit, closes as a win; (3) only
if neither hit, check whether the breakeven trigger price is reached for
the first time this candle and move the stop to entry_price. This order
means a single wild candle that spans both the breakeven trigger and the
original SL is conservatively treated as a full loss, matching the
SL-first tie-break convention used everywhere else in this bot.
"""

from __future__ import annotations

import pandas as pd


def check_tp_sl_with_breakeven(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    breakeven_trigger_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff. Returns None while
    still open, else:
    {"status": "win"|"loss"|"breakeven", "pnl_roi_pct": float,
     "breakeven_triggered_at": Timestamp|None, "closed_at": Timestamp}

    pnl_roi_pct is the raw price-move percent (not leverage-scaled -- the
    caller applies LEVERAGE). A "breakeven" close realizes exactly 0.0%
    (fees/slippage are not modelled here, matching how the rest of the
    bot treats ESTIMATED_*_FEE_PCT as backtest-only/informational).
    """
    current_sl = sl_price
    breakeven_triggered_at = None

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= current_sl) if direction == "LONG" else (high >= current_sl)
        if sl_hit:
            status = "loss" if current_sl == sl_price else "breakeven"
            pnl = 0.0 if status == "breakeven" else (
                (current_sl - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - current_sl) / entry_price * 100.0
            )
            return {
                "status": status, "pnl_roi_pct": round(pnl, 4),
                "breakeven_triggered_at": breakeven_triggered_at, "closed_at": ts,
            }

        tp_hit = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        if tp_hit:
            pnl = (
                (tp_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - tp_price) / entry_price * 100.0
            )
            return {
                "status": "win", "pnl_roi_pct": round(pnl, 4),
                "breakeven_triggered_at": breakeven_triggered_at, "closed_at": ts,
            }

        if breakeven_triggered_at is None:
            reached = (high >= breakeven_trigger_price) if direction == "LONG" else (low <= breakeven_trigger_price)
            if reached:
                current_sl = entry_price
                breakeven_triggered_at = ts

    return None
