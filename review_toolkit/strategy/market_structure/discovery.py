"""Trend-independent discovery of confirmed swing and consolidation breaks."""

from dataclasses import dataclass
from datetime import datetime

from .models import FOUR_HOURS, StructureSnapshot
from .structure import Parameters, validate_bar
from .swings import ConfirmedPivots, WilderATR


RANGE_EDGE_FRACTION = 0.25


@dataclass
class DiscoveryLevel:
    symbol: str
    kind: str
    direction: str
    price: float
    pivot_timestamp: datetime
    confirmation_timestamp: datetime
    activation_timestamp: datetime
    reference_pivot_timestamp: datetime | None
    swing_move: float | None
    confirmation_atr: float | None
    swing_move_atr: float | None
    touches: int = 0
    last_touch_at: datetime | None = None
    first_break_timestamp: datetime | None = None


@dataclass
class DiscoveryRange:
    symbol: str
    lower: float
    upper: float
    lower_pivot_timestamp: datetime
    upper_pivot_timestamp: datetime
    formation_start: datetime
    activation_timestamp: datetime
    expiry_timestamp: datetime
    width_atr: float | None
    formation_pivots: str
    ended_at: datetime | None = None
    outcome: str = "active"
    first_break_timestamp: datetime | None = None


@dataclass(frozen=True)
class DiscoveryEvent:
    symbol: str
    classification: str
    direction: str
    break_timestamp: datetime
    broken_level: float
    pivot_timestamp: datetime
    confirmation_timestamp: datetime
    activation_timestamp: datetime
    previous_close: float
    break_close: float
    atr: float | None
    swing_move_atr: float | None
    age_bars: int
    prior_touches: int
    distance_from_previous_close: float
    distance_from_previous_close_atr: float | None
    break_distance_atr: float | None
    prior_external_state: str
    prior_internal_state: str
    prior_external_boundary: float | None
    relative_to_external_boundary: str
    range_lower: float | None
    range_upper: float | None
    range_width_atr: float | None


def atr_ratio(value, atr):
    return value / atr if value is not None and atr else None


class BreakDiscovery:
    def __init__(self, parameters=Parameters()):
        self.pivots = ConfirmedPivots(parameters.pivot_left, parameters.pivot_right)
        self.atr = WilderATR(parameters.atr_period)
        self.candles = []
        self.levels = []
        self.active_levels = {}
        self.ranges = []
        self.active_range = None
        self.events = []
        self.latest_by_kind = {}

    def accept_pivot(self, candle, swing, atr):
        kind = swing.kind
        reference = self.latest_by_kind.get("low" if kind == "high" else "high")
        movement = abs(swing.price - reference.price) if reference else None
        level = DiscoveryLevel(
            candle.symbol, kind, "up" if kind == "high" else "down", swing.price,
            swing.pivot_timestamp, swing.confirmation_timestamp, candle.closed_at,
            reference.pivot_timestamp if reference else None, movement, atr, atr_ratio(movement, atr),
        )
        self.levels.append(level)
        self.active_levels[level.pivot_timestamp] = level
        self.latest_by_kind[kind] = level
        if self.active_range is None:
            self.try_range(candle, atr)

    def try_range(self, candle, atr):
        recent = self.levels[-4:]
        if len(recent) < 4 or any(a.kind == b.kind for a, b in zip(recent, recent[1:])):
            return
        highs = [level for level in recent if level.kind == "high"]
        lows = [level for level in recent if level.kind == "low"]
        upper = max(highs, key=lambda level: level.price)
        lower = min(lows, key=lambda level: level.price)
        width = upper.price - lower.price
        if (width <= 0 or abs(highs[0].price - highs[1].price) > RANGE_EDGE_FRACTION * width
                or abs(lows[0].price - lows[1].price) > RANGE_EDGE_FRACTION * width):
            return
        start = recent[0].pivot_timestamp
        duration = candle.closed_at - start
        formation = self.candles[-(duration // FOUR_HOURS):]
        if any(not lower.price <= bar.close <= upper.price for bar in formation):
            return
        active = DiscoveryRange(
            candle.symbol, lower.price, upper.price, lower.pivot_timestamp, upper.pivot_timestamp,
            start, candle.closed_at, candle.closed_at + duration, atr_ratio(width, atr),
            "|".join(level.pivot_timestamp.isoformat() for level in recent),
        )
        self.ranges.append(active)
        self.active_range = active

    def record_break(self, candle, level, atr, external, internal, box=None):
        old_close = self.candles[-2].close
        distance = abs(old_close - level.price)
        external_state = external.state.value if external else "unknown"
        boundary = None
        if external_state.startswith("bullish"):
            boundary = external.external_high
        elif external_state.startswith("bearish"):
            boundary = external.external_low
        relation = "unavailable"
        if boundary is not None:
            relation = "above" if level.price > boundary else "below" if level.price < boundary else "equal"
        classification = (f"RANGE_BREAK_{level.direction.upper()}" if box
                          else f"SWING_{level.kind.upper()}_BREAK")
        activated = box.activation_timestamp if box else level.activation_timestamp
        self.events.append(DiscoveryEvent(
            candle.symbol, classification, level.direction, candle.closed_at, level.price,
            level.pivot_timestamp, level.confirmation_timestamp, activated, old_close, candle.close,
            atr, level.swing_move_atr, (candle.closed_at - activated) // FOUR_HOURS, level.touches,
            distance, atr_ratio(distance, atr), atr_ratio(abs(candle.close - level.price), atr),
            external_state, internal.state.value if internal else "unknown", boundary, relation,
            box.lower if box else None, box.upper if box else None, box.width_atr if box else None,
        ))

    def check_range(self, candle, atr, external, internal):
        box = self.active_range
        if box is None:
            return
        if (candle.closed_at >= box.expiry_timestamp
                and (candle.closed_at > box.expiry_timestamp or box.lower <= candle.close <= box.upper)):
            box.outcome = "expired"
            box.ended_at = box.expiry_timestamp
        elif candle.close > box.upper or candle.close < box.lower:
            pivot = box.upper_pivot_timestamp if candle.close > box.upper else box.lower_pivot_timestamp
            self.record_break(candle, self.active_levels[pivot], atr, external, internal, box)
            box.outcome = "broken"
            box.ended_at = box.first_break_timestamp = candle.closed_at
        if box.ended_at is not None:
            self.active_range = None

    def update(self, candle, external: StructureSnapshot | None = None,
               internal: StructureSnapshot | None = None):
        previous = self.candles[-1] if self.candles else None
        validate_bar(candle, previous=previous)
        self.candles.append(candle)
        atr = self.atr.update(candle)
        self.check_range(candle, atr, external, internal)
        for stamp, level in list(self.active_levels.items()):
            broke = (candle.close > level.price if level.kind == "high" else candle.close < level.price)
            if broke:
                self.record_break(candle, level, atr, external, internal)
                level.first_break_timestamp = candle.closed_at
                del self.active_levels[stamp]
            elif candle.low <= level.price <= candle.high:
                if level.last_touch_at != candle.closed_at - FOUR_HOURS:
                    level.touches += 1
                level.last_touch_at = candle.closed_at
        pivot = self.pivots.update(candle)
        if pivot:
            self.accept_pivot(candle, pivot, atr)
