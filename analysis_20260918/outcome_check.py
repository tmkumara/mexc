"""
Plain single-TP/SL outcome determination for Zero-Lag MTF Pullback v1's
fired signals. No breakeven step in this strategy version (see the design
spec's "Relationship to prior work" section) -- same-candle tie-break, SL
checked before TP, matching the convention used everywhere else in this
bot.
"""

from __future__ import annotations

import pandas as pd


def check_tp_sl(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff. Each candle, in order:
    (1) SL hit -> "loss"; (2) TP hit -> "win" (SL-first tie-break on a
    single wild candle that spans both). Returns None while open, else
    {"status": "win"|"loss", "pnl_roi_pct": float, "closed_at": Timestamp}.
    pnl_roi_pct is the raw price-move percent (not leverage-scaled -- the
    caller applies LEVERAGE).
    """
    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= sl_price) if direction == "LONG" else (high >= sl_price)
        if sl_hit:
            pnl = (
                (sl_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - sl_price) / entry_price * 100.0
            )
            return {"status": "loss", "pnl_roi_pct": round(pnl, 4), "closed_at": ts}

        tp_hit = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        if tp_hit:
            pnl = (
                (tp_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - tp_price) / entry_price * 100.0
            )
            return {"status": "win", "pnl_roi_pct": round(pnl, 4), "closed_at": ts}

    return None
