"""Data shared by the incremental detector and historical replay."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


FOUR_HOURS = timedelta(hours=4)


class MarketTrend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    UNKNOWN = "unknown"


class StructureState(str, Enum):
    UNKNOWN = "unknown"
    BULLISH_UNPROTECTED = "bullish_unprotected"
    BEARISH_UNPROTECTED = "bearish_unprotected"
    BULLISH_PROTECTED = "bullish_protected"
    BEARISH_PROTECTED = "bearish_protected"


@dataclass(frozen=True)
class StructureSnapshot:
    timestamp: datetime
    state: StructureState
    external_high: float | None
    external_low: float | None
    continuation_armed: bool


class RejectionReason(str, Enum):
    MISSING_ATR = "missing_or_zero_atr"
    WRONG_BODY_DIRECTION = "wrong_body_direction"
    WEAK_DISPLACEMENT = "weak_displacement"
    SHALLOW_BREAK = "shallow_break"
    POOR_BODY_QUALITY = "poor_body_quality"
    WEAK_CLOSE_POSITION = "weak_close_position"
    INSUFFICIENT_TREND_HISTORY = "insufficient_trend_history"


@dataclass(frozen=True)
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def closed_at(self):
        return self.timestamp + FOUR_HOURS


@dataclass(frozen=True)
class SwingPoint:
    kind: str
    price: float
    pivot_timestamp: datetime
    confirmation_timestamp: datetime


@dataclass(frozen=True)
class ProtectedLevel:
    swing: SwingPoint
    established_at: datetime


@dataclass(frozen=True)
class StructureBreak:
    kind: str
    direction: MarketTrend
    broken_level: float
    pivot_timestamp: datetime
    pivot_confirmation_timestamp: datetime
    break_timestamp: datetime
    break_close: float
    previous_trend: MarketTrend
    new_structure_state: MarketTrend
    continuation_count: int
    protected_level: float | None
    protected_pivot_timestamp: datetime | None
    symbol: str
    scope: str
    classification: str
    resulting_state: StructureState
    associated_protected_level: float | None
    protected_since: datetime | None
    atr: float | None


@dataclass(frozen=True)
class CHoCHEvent:
    event_id: str
    symbol: str
    timeframe: str
    direction: MarketTrend
    event_timestamp: datetime
    pivot_timestamp: datetime
    pivot_confirmation_timestamp: datetime
    protected_level: float
    protected_since: datetime
    previous_trend: MarketTrend
    new_structure_state: MarketTrend
    break_close: float
    continuation_count: int
    atr: float | None
    body_atr_ratio: float | None
    break_atr_ratio: float | None
    body_ratio: float
    close_position: float
    strong_choch: bool
    rejection_reasons: tuple[RejectionReason, ...]
    scope: str = "external"
    classification: str = "EXTERNAL_CHOCH"
