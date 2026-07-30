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


def check_target_ladder(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    tp3_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
    close_fracs: tuple[float, float, float] = (0.5, 0.3, 0.2),
    move_sl_to_breakeven_after_t1: bool = True,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff, SL-first same-candle
    tie-break (same convention as check_tp_sl). Realizes close_fracs[n] of
    the position at each target in sequence (T1 then T2 then T3, one stage
    per candle); reaching T3 fully closes. Moves the stop to entry_price
    once T1 fills, if move_sl_to_breakeven_after_t1.

    Returns None while still open, else:
    {"status": "win"|"loss", "pnl_roi_pct": float,
     "t1_hit_at": Timestamp|None, "t2_hit_at": Timestamp|None,
     "closed_at": Timestamp, "final_stage": 0-3}
    pnl_roi_pct is the price-move percent sum, NOT leverage-scaled --
    the caller applies LEVERAGE, matching how check_tp_sl leaves leverage
    scaling to main.py's _calculate_pnl_roi. status is "loss" only when
    SL is hit before T1 ever fills; every other close realizes >= 0%
    since T1+ has already locked in profit on part of the position.
    """
    targets = [tp1_price, tp2_price, tp3_price]
    current_sl = sl_price
    stage = 0
    remaining = 1.0
    realized_pct = 0.0
    t1_hit_at = None
    t2_hit_at = None

    def _price_move_pct(exit_price: float) -> float:
        if direction == "LONG":
            return (exit_price - entry_price) / entry_price * 100.0
        return (entry_price - exit_price) / entry_price * 100.0

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= current_sl) if direction == "LONG" else (high >= current_sl)
        if sl_hit:
            realized_pct += remaining * _price_move_pct(current_sl)
            status = "loss" if stage == 0 else "win"
            return {
                "status": status, "pnl_roi_pct": round(realized_pct, 4),
                "t1_hit_at": t1_hit_at, "t2_hit_at": t2_hit_at,
                "closed_at": ts, "final_stage": stage,
            }

        target_hit = (high >= targets[stage]) if direction == "LONG" else (low <= targets[stage])
        if target_hit and stage < 3:
            realized_pct += close_fracs[stage] * _price_move_pct(targets[stage])
            remaining -= close_fracs[stage]
            if stage == 0:
                t1_hit_at = ts
                if move_sl_to_breakeven_after_t1:
                    current_sl = entry_price
            elif stage == 1:
                t2_hit_at = ts
            stage += 1

            if stage == 3:
                return {
                    "status": "win", "pnl_roi_pct": round(realized_pct, 4),
                    "t1_hit_at": t1_hit_at, "t2_hit_at": t2_hit_at,
                    "closed_at": ts, "final_stage": 3,
                }

    return None
