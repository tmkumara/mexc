"""
Breakeven-aware outcome replay for pending signals.

Replays every closed candle since a signal's entry against its TP/SL,
applying a single-step breakeven trail: once price reaches
breakeven_trigger_pct of the way from entry to TP, the ACTIVE stop for
all SUBSEQUENT candles becomes entry_price (breakeven) instead of the
original sl_price. The candle where the trigger is reached is itself
still evaluated against the ORIGINAL stop -- if a single candle's range
would hit both the original SL and the trigger level, the original SL
takes precedence for that candle, since intra-bar ordering can't be
determined from OHLC data alone.

Caller (main.py's check_outcomes) is responsible for: fetching the
candle DataFrame, persisting a newly-detected trigger timestamp, and
sending the breakeven Telegram notification.
"""

from __future__ import annotations

import pandas as pd


def replay_outcome(
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
    existing_trigger_ts,
    breakeven_trigger_pct: float,
) -> tuple[str | None, "pd.Timestamp | None", bool]:
    """
    Returns (outcome, newly_triggered_at, closed_at_breakeven):
      outcome            -- "win" | "loss" | None (still pending)
      newly_triggered_at -- candle timestamp if the breakeven trigger
                            fired during THIS call and existing_trigger_ts
                            was None, else None
      closed_at_breakeven -- True if outcome == "loss" and the stop that
                             was hit was the breakeven price (entry_price),
                             not the original sl_price
    """
    trigger_price = entry_price + breakeven_trigger_pct * (tp_price - entry_price)
    trigger_ts = existing_trigger_ts
    newly_triggered_at = None

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high  = float(df["high"].iloc[i])
        low   = float(df["low"].iloc[i])
        open_ = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])

        active_sl = entry_price if (trigger_ts is not None and ts > trigger_ts) else sl_price

        hit_tp = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        hit_sl = (low <= active_sl) if direction == "LONG" else (high >= active_sl)

        if hit_tp and hit_sl:
            outcome = "win" if (
                (direction == "LONG"  and close >= open_) or
                (direction == "SHORT" and close <= open_)
            ) else "loss"
            return outcome, newly_triggered_at, (outcome == "loss" and active_sl == entry_price)

        if hit_tp:
            return "win", newly_triggered_at, False

        if hit_sl:
            return "loss", newly_triggered_at, (active_sl == entry_price)

        if trigger_ts is None:
            reached = (high >= trigger_price) if direction == "LONG" else (low <= trigger_price)
            if reached:
                trigger_ts = ts
                newly_triggered_at = ts

    return None, newly_triggered_at, False
