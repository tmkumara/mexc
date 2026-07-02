"""
Order Block detection (Smart Money Concepts) from OHLCV candles.

An Order Block is the last opposite-color candle before an impulsive move
that breaks market structure (BOS/CHoCH) with displacement (a large ATR-
relative move or a Fair Value Gap).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Swing:
    bar_index: int
    price: float
    kind: str          # "high" or "low"


@dataclass
class StructureEvent:
    bar_index: int
    direction: str      # "LONG" or "SHORT" -- direction of the break
    kind: str           # "BOS" or "CHoCH"


@dataclass
class OrderBlock:
    direction: str
    low: float
    high: float
    formed_at_bar: int
    event_bar_index: int
    structure_event: str  # "BOS" or "CHoCH"


def find_swings(df: pd.DataFrame, length: int = 6) -> list[Swing]:
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    n = len(df)
    swings: list[Swing] = []
    for i in range(length, n - length):
        window_high = highs[i - length: i + length + 1]
        if highs[i] == window_high.max():
            swings.append(Swing(bar_index=i, price=float(highs[i]), kind="high"))
        window_low = lows[i - length: i + length + 1]
        if lows[i] == window_low.min():
            swings.append(Swing(bar_index=i, price=float(lows[i]), kind="low"))
    return swings


def detect_bos_choch(df: pd.DataFrame, swings: list[Swing]) -> list[StructureEvent]:
    closes = df["close"].astype(float).to_numpy()
    n = len(df)

    last_swing_high: Swing | None = None
    last_swing_low: Swing | None = None
    trend: str | None = None
    events: list[StructureEvent] = []

    swings_by_bar: dict[int, list[Swing]] = {}
    for s in swings:
        swings_by_bar.setdefault(s.bar_index, []).append(s)

    for i in range(n):
        for s in swings_by_bar.get(i, []):
            if s.kind == "high":
                last_swing_high = s
            else:
                last_swing_low = s

        if (last_swing_high is not None
                and i > last_swing_high.bar_index
                and closes[i] > last_swing_high.price):
            kind = "BOS" if trend == "LONG" else "CHoCH"
            events.append(StructureEvent(bar_index=i, direction="LONG", kind=kind))
            trend = "LONG"
            last_swing_high = None
        elif (last_swing_low is not None
                and i > last_swing_low.bar_index
                and closes[i] < last_swing_low.price):
            kind = "BOS" if trend == "SHORT" else "CHoCH"
            events.append(StructureEvent(bar_index=i, direction="SHORT", kind=kind))
            trend = "SHORT"
            last_swing_low = None

    return events


def _has_fair_value_gap(df: pd.DataFrame, start: int, end: int, direction: str) -> bool:
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    for k in range(start + 1, end):
        if direction == "LONG" and lows[k + 1] > highs[k - 1]:
            return True
        if direction == "SHORT" and highs[k + 1] < lows[k - 1]:
            return True
    return False


def find_order_blocks(
    df: pd.DataFrame,
    structure_events: list[StructureEvent],
    atr: pd.Series,
    displacement_atr_mult: float = 1.5,
) -> list[OrderBlock]:
    opens = df["open"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    highs = df["high"].astype(float).to_numpy()
    atr_arr = atr.to_numpy(dtype=float)

    obs: list[OrderBlock] = []
    for event in structure_events:
        i = event.bar_index
        j = i
        if event.direction == "LONG":
            while j > 0 and closes[j] >= opens[j]:
                j -= 1
            if j == 0 and closes[j] >= opens[j]:
                continue
        else:
            while j > 0 and closes[j] <= opens[j]:
                j -= 1
            if j == 0 and closes[j] <= opens[j]:
                continue

        atr_at_break = atr_arr[i] if not pd.isna(atr_arr[i]) else 0.0
        if atr_at_break <= 0:
            continue

        move = abs(closes[i] - closes[j])
        has_fvg = _has_fair_value_gap(df, j, i, event.direction)

        if move < displacement_atr_mult * atr_at_break and not has_fvg:
            continue

        obs.append(OrderBlock(
            direction=event.direction,
            low=float(lows[j]),
            high=float(highs[j]),
            formed_at_bar=j,
            event_bar_index=i,
            structure_event=event.kind,
        ))

    return obs
