"""Confirmed 4H RSI divergence detection shared by replay and inference."""
from dataclasses import dataclass

import pandas as pd

from review_toolkit.strategy.market_structure.swings import ConfirmedPivots
from review_toolkit.strategy.market_structure.models import Candle
from review_toolkit.strategy.market_structure.structure import validate_bar

CONFIRM_BARS = 6


@dataclass(frozen=True)
class Setup:
    side: int
    first: object
    second: object
    first_rsi: float
    second_rsi: float
    middle: float
    index: int


def wilder_rsi(closes, period=14):
    changes = closes.diff()
    if len(changes) <= period:
        return pd.Series(float("nan"), index=closes.index)
    gain = changes.clip(lower=0).iloc[1:]
    loss = (-changes.clip(upper=0)).iloc[1:]
    averages = []
    for values in (gain, loss):
        seed = values.iloc[:period].mean()
        seeded = pd.concat([pd.Series([seed], index=values.index[period-1:period]), values.iloc[period:]])
        averages.append(seeded.ewm(alpha=1/period, adjust=False).mean())
    up, down = averages
    rsi = (100 - 100 / (1 + up / down)).mask((up == 0) & (down == 0), 50)
    return rsi.reindex(closes.index)


def discover(bars, symbol, rsi=None):
    rsi = wilder_rsi(bars.close) if rsi is None else rsi
    pivots = ConfirmedPivots(3, 3)
    previous = {}
    pending = {}
    previous_bar = None
    for index, row in enumerate(bars.itertuples()):
        candle = Candle(symbol, row.Index.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)
        validate_bar(candle, previous=previous_bar)
        previous_bar = candle
        stamp = candle.closed_at
        for side, setup in list(pending.items()):
            if index > setup.index + CONFIRM_BARS or side * (candle.close - setup.second.price) < 0:
                del pending[side]
                continue
            if side * (candle.close - setup.middle) > 0:
                del pending[side]
                yield "price_confirmed", stamp, setup
        pivot = pivots.update(candle)
        if pivot is None:
            continue
        first = previous.get(pivot.kind)
        previous[pivot.kind] = pivot
        if first is None:
            continue
        first_rsi, second_rsi = rsi.loc[first.pivot_timestamp], rsi.loc[pivot.pivot_timestamp]
        side = 1 if pivot.kind == "low" else -1
        if not (side * (pivot.price - first.price) < 0 and side * (second_rsi - first_rsi) > 0):
            continue
        between = bars.loc[(bars.index > first.pivot_timestamp) & (bars.index < pivot.pivot_timestamp)]
        middle = between.high.max() if side == 1 else between.low.min()
        setup = Setup(side, first, pivot, first_rsi, second_rsi, middle, index)
        yield "divergence", stamp, setup
        # Require a fresh price crossing after the alert. Don't backdate confirmed entries.
        pending.pop(side, None)
        if side * (candle.close - middle) <= 0:
            pending[side] = setup
