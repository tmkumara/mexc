"""
Plain TP/SL outcome determination for pending signals (no breakeven trail).

Same-candle tie-break: if both TP and SL are touched within one candle,
the stop is treated as hit first (conservative assumption). This is the
one deliberate behavioral difference from outcome_replay.py's
breakeven-aware replay, which is not used by this strategy because
breakeven is disabled for v1 (see the design spec's resolved ambiguity 2).
"""

from __future__ import annotations

import pandas as pd


def check_tp_sl(
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> str | None:
    """
    Returns "win", "loss", or None (still pending).

    LONG:  TP hit when high >= tp_price; SL hit when low <= sl_price
    SHORT: TP hit when low <= tp_price;  SL hit when high >= sl_price

    The final row of `df` is assumed to be the still-forming candle and is
    never evaluated, matching the completed-candle-only convention used
    everywhere else in this strategy.
    """
    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        hit_sl = (low <= sl_price) if direction == "LONG" else (high >= sl_price)
        if hit_sl:
            return "loss"

        hit_tp = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        if hit_tp:
            return "win"

    return None
