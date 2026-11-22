"""Incremental external structure with close-confirmed breaks."""

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite

from .choch import qualify
from .models import (
    FOUR_HOURS, MarketTrend, ProtectedLevel, StructureBreak, StructureSnapshot, StructureState,
)
from .swings import ConfirmedPivots, WilderATR


@dataclass(frozen=True)
class Parameters:
    pivot_left: int = 3
    pivot_right: int = 3
    atr_period: int = 14
    minimum_swing_atr: float = 1.5
    minimum_body_atr: float = 0.8
    minimum_break_atr: float = 0.2
    minimum_body_ratio: float = 0.6
    minimum_close_position: float = 0.7

    def __post_init__(self):
        for value in (self.pivot_left, self.pivot_right, self.atr_period):
            if type(value) is not int or value < 1:
                raise ValueError("Pivot widths and ATR period must be positive integers")
        for value in (self.minimum_swing_atr, self.minimum_body_atr, self.minimum_break_atr):
            if not isfinite(value) or value < 0:
                raise ValueError("ATR thresholds must be finite and nonnegative")
        for value in (self.minimum_body_ratio, self.minimum_close_position):
            if not 0 <= value <= 1:
                raise ValueError("Candle ratios must be in [0, 1]")


def validate_bar(candle, interval=FOUR_HOURS, previous=None):
    timestamp = candle.timestamp
    minutes_since_midnight = timestamp.hour * 60 + timestamp.minute
    if (timestamp.utcoffset() != timedelta(0) or timestamp.second or timestamp.microsecond
            or minutes_since_midnight % (interval // timedelta(minutes=1))):
        raise ValueError(f"Candle timestamp is not UTC aligned: {timestamp}")
    if previous is not None and (
        candle.symbol != previous.symbol or timestamp - previous.timestamp != interval
    ):
        raise ValueError(f"Symbol change, duplicate, out-of-order candle or gap at {timestamp}")
    prices = candle.open, candle.high, candle.low, candle.close
    if (not all(isfinite(price) and price > 0 for price in prices)
            or candle.high != max(prices) or candle.low != min(prices)
            or not isfinite(candle.volume) or candle.volume < 0):
        raise ValueError(f"Invalid OHLCV at {timestamp}")


class StructureDetector:
    scope = "external"

    def __init__(self, parameters=Parameters()):
        self.parameters = parameters
        self.atr = WilderATR(parameters.atr_period)
        self.pivots = ConfirmedPivots(parameters.pivot_left, parameters.pivot_right)
        self.previous = None
        self.trend = MarketTrend.UNKNOWN
        self.protected = None
        self.boundary = None
        self.last_break_close = None
        self.trend_started_at = None
        self.continuations = 0
        self.last_transition = None
        self.confirmed_swings = []
        self.structural_swings = {}
        self.active_swings = []
        self.structure_events = []
        self.choch_events = []
        self.broken_swings = set()
        self.closed_through = set()
        self.snapshots = []

    @property
    def external_swings(self):
        return self.structural_swings

    @property
    def state(self):
        if self.trend == MarketTrend.UNKNOWN:
            return StructureState.UNKNOWN
        protection = "protected" if self.protected else "unprotected"
        return StructureState(f"{self.trend.value}_{protection}")

    @property
    def external_high(self):
        return self.boundary if self.trend == MarketTrend.BULLISH else (
            self.protected.swing if self.protected else None)

    @property
    def external_low(self):
        return self.boundary if self.trend == MarketTrend.BEARISH else (
            self.protected.swing if self.protected else None)

    def accept_swing(self, swing, atr):
        if not atr:
            return
        if not self.active_swings:
            self.active_swings.append(swing)
        else:
            last = self.active_swings[-1]
            if last.kind == swing.kind:
                more_extreme = swing.price > last.price if swing.kind == "high" else swing.price < last.price
                if not more_extreme:
                    return
                anchor = self.active_swings[-2] if len(self.active_swings) > 1 else None
            else:
                anchor = last
            if anchor is not None:
                move = swing.price - anchor.price if swing.kind == "high" else anchor.price - swing.price
                if move <= 0 or move < self.parameters.minimum_swing_atr * atr:
                    return
            if last.kind == swing.kind:
                self.active_swings[-1] = swing
            else:
                self.active_swings.append(swing)
        self.advance_boundary(swing)

    def advance_boundary(self, swing):
        if self.boundary is None or swing.kind != self.boundary.kind:
            return
        sign = 1 if swing.kind == "high" else -1
        if (sign * swing.price > sign * self.boundary.price
                and sign * swing.price >= sign * self.last_break_close
                and swing.pivot_timestamp > self.boundary.pivot_timestamp):
            self.boundary = swing
            self.structural_swings.setdefault(swing, swing.confirmation_timestamp)

    def crosses(self, candle, swing):
        if swing.kind == "high":
            return self.previous.close <= swing.price < candle.close
        return self.previous.close >= swing.price > candle.close

    def record_break(self, candle, kind, direction, swing, previous_trend, atr,
                     broken_protection=None):
        protected = self.protected.swing if self.protected else None
        associated = broken_protection or self.protected
        self.broken_swings.add(swing)
        self.structure_events.append(StructureBreak(
            kind, direction, swing.price, swing.pivot_timestamp, swing.confirmation_timestamp,
            candle.closed_at, candle.close, previous_trend, self.trend, self.continuations,
            protected.price if protected else None, protected.pivot_timestamp if protected else None,
            candle.symbol, self.scope, f"{self.scope.upper()}_{kind.upper()}", self.state,
            associated.swing.price if associated else None,
            associated.established_at if associated else None, atr,
        ))

    def trend_setup(self, recent, highs, lows):
        if len(recent) != 4:
            return MarketTrend.UNKNOWN
        if highs[-1].price > highs[0].price and lows[-1].price > lows[0].price:
            return MarketTrend.BULLISH
        if highs[-1].price < highs[0].price and lows[-1].price < lows[0].price:
            return MarketTrend.BEARISH
        return MarketTrend.UNKNOWN

    def evaluate_break(self, candle, atr):
        prior = self.trend
        if self.protected and self.crosses(candle, self.protected.swing):
            broken_protection = self.protected
            swing = broken_protection.swing
            direction = MarketTrend.BEARISH if prior == MarketTrend.BULLISH else MarketTrend.BULLISH
            if self.scope == "external":
                self.choch_events.append(qualify(
                    candle, atr, direction, broken_protection, prior, self.continuations, self.parameters,
                ))
            self.trend = MarketTrend.UNKNOWN
            self.protected = None
            self.boundary = None
            self.last_break_close = None
            self.trend_started_at = None
            self.continuations = 0
            self.last_transition = candle.timestamp
            self.record_break(candle, "choch", direction, swing, prior, atr, broken_protection)
            return

        recent = self.active_swings[-4:]
        highs = [swing for swing in recent if swing.kind == "high"]
        lows = [swing for swing in recent if swing.kind == "low"]
        direction = prior
        target = self.boundary
        if target is None or target in self.broken_swings or not self.crosses(candle, target):
            # An unprotected bias can be superseded by a fully new opposite setup.
            # This establishes a trend without inventing a broken protected level.
            if self.protected or (self.trend_started_at and recent[0].pivot_timestamp < self.trend_started_at):
                return
            direction = self.trend_setup(recent, highs, lows)
            if direction in (prior, MarketTrend.UNKNOWN):
                return
            target = highs[-1] if direction == MarketTrend.BULLISH else lows[-1]
            if (target in self.broken_swings or not self.crosses(candle, target)
                    or (self.last_transition and target.pivot_timestamp <= self.last_transition)):
                return

        origins = lows if direction == MarketTrend.BULLISH else highs
        origin = origins[-1] if origins else None
        if (origin and origin.pivot_timestamp > target.pivot_timestamp
                and origin not in self.broken_swings
                and origin not in self.closed_through
                and (self.protected is None
                     or origin.pivot_timestamp > self.protected.swing.pivot_timestamp)):
            self.protected = ProtectedLevel(origin, candle.closed_at)
            self.structural_swings.setdefault(origin, candle.closed_at)

        self.trend = direction
        self.boundary = target
        self.last_break_close = candle.close
        self.structural_swings.setdefault(target, candle.closed_at)
        if direction != prior:
            self.trend_started_at = candle.closed_at
            self.continuations = 0
            self.record_break(candle, "trend_established", direction, target, prior, atr)
        else:
            self.continuations += 1
            self.record_break(candle, "bos", direction, target, prior, atr)

    def update(self, candle):
        """Consume one already closed 4H candle in chronological order."""
        validate_bar(candle, previous=self.previous)
        atr = self.atr.update(candle)
        confirmed = self.pivots.update(candle)
        event_count = len(self.choch_events)
        if self.previous:
            # Only the latest swing on each side can be selected as a new leg origin.
            for swing in self.active_swings[-2:]:
                if self.crosses(candle, swing):
                    self.closed_through.add(swing)
            self.evaluate_break(candle, atr)
        if confirmed:
            self.confirmed_swings.append(confirmed)
            self.accept_swing(confirmed, atr)
        self.previous = candle
        high, low = self.external_high, self.external_low
        self.snapshots.append(StructureSnapshot(
            candle.closed_at, self.state, high.price if high else None, low.price if low else None,
            self.boundary is not None and self.boundary not in self.broken_swings,
        ))
        return self.choch_events[-1] if len(self.choch_events) > event_count else None
