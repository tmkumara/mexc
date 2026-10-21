"""Independent signal model for EMA Trend Pullback + RSI Reset v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Literal, Mapping


STRATEGY_ID = "ema_trend_pullback"
STRATEGY_VERSION = "v1"
Direction = Literal["LONG", "SHORT"]


def _utc_datetime(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime-like value")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class EmaSignal:
    symbol: str
    direction: Direction
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    tp_roi_pct: float
    sl_roi_pct: float
    rr: float
    strategy_id: str
    strategy_version: str
    setup_key: str
    confirmation_candle_open: datetime
    confirmation_close_at: datetime
    pivot_timestamp: datetime
    rsi_reset_timestamp: datetime
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.setup_key:
            raise ValueError("setup_key is required")
        if not self.strategy_id or not self.strategy_version:
            raise ValueError("strategy_id and strategy_version are required")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive")
        if self.direction == "LONG":
            valid_geometry = self.sl_price < self.entry_price < self.tp_price
        else:
            valid_geometry = self.tp_price < self.entry_price < self.sl_price
        if not valid_geometry:
            raise ValueError("invalid LONG/SHORT price geometry")
        for field_name in (
            "confirmation_candle_open",
            "confirmation_close_at",
            "pivot_timestamp",
            "rsi_reset_timestamp",
            "generated_at",
        ):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be UTC-aware")
            if value.utcoffset() != timedelta(0):
                raise ValueError(f"{field_name} must be UTC")
        if self.confirmation_close_at != self.confirmation_candle_open + timedelta(minutes=15):
            raise ValueError("confirmation_close_at must be 15 minutes after confirmation_candle_open")


def to_ema_signal(evaluation_result: Mapping[str, object], generated_at: datetime) -> EmaSignal:
    """Convert one accepted EMA evaluator result into an independent signal."""
    if not isinstance(evaluation_result, Mapping):
        raise ValueError("evaluation_result must be a mapping")
    if evaluation_result.get("accepted") is not True:
        raise ValueError("evaluation result must have accepted=True")

    required = (
        "symbol",
        "direction",
        "entry",
        "tp",
        "sl",
        "leverage",
        "gross_tp_roi_pct",
        "gross_sl_roi_pct",
        "rr",
        "strategy_id",
        "strategy_version",
        "confirmation_candle_open",
        "confirmation_close_at",
        "pivot_timestamp",
        "rsi_reset_timestamp",
        "setup_key",
    )
    missing = [field for field in required if field not in evaluation_result]
    if missing:
        raise ValueError(f"missing required evaluation fields: {', '.join(missing)}")

    symbol = evaluation_result["symbol"]
    direction = evaluation_result["direction"]
    strategy_id = evaluation_result["strategy_id"]
    strategy_version = evaluation_result["strategy_version"]
    setup_key = evaluation_result["setup_key"]
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol is required")
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")
    if not isinstance(strategy_id, str) or not strategy_id:
        raise ValueError("strategy_id is required")
    if not isinstance(strategy_version, str) or not strategy_version:
        raise ValueError("strategy_version is required")
    if not isinstance(setup_key, str) or not setup_key:
        raise ValueError("setup_key is required")

    entry = _finite_number(evaluation_result["entry"], "entry")
    tp = _finite_number(evaluation_result["tp"], "tp")
    sl = _finite_number(evaluation_result["sl"], "sl")
    if direction == "LONG" and not sl < entry < tp:
        raise ValueError("invalid LONG price geometry")
    if direction == "SHORT" and not tp < entry < sl:
        raise ValueError("invalid SHORT price geometry")

    leverage_value = evaluation_result["leverage"]
    if isinstance(leverage_value, bool):
        raise ValueError("leverage must be a positive integer")
    try:
        leverage = int(leverage_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("leverage must be a positive integer") from exc
    if leverage <= 0 or leverage != leverage_value:
        raise ValueError("leverage must be a positive integer")

    tp_roi = _finite_number(evaluation_result["gross_tp_roi_pct"], "gross_tp_roi_pct")
    sl_roi = _finite_number(evaluation_result["gross_sl_roi_pct"], "gross_sl_roi_pct")
    rr = _finite_number(evaluation_result["rr"], "rr")
    if tp_roi < 0 or sl_roi < 0 or rr <= 0:
        raise ValueError("ROI values must be non-negative and rr must be positive")

    confirmation_open = _utc_datetime(
        evaluation_result["confirmation_candle_open"], "confirmation_candle_open"
    )
    confirmation_close = _utc_datetime(
        evaluation_result["confirmation_close_at"], "confirmation_close_at"
    )
    if confirmation_close != confirmation_open + timedelta(minutes=15):
        raise ValueError("confirmation_close_at must be 15 minutes after confirmation_candle_open")
    pivot_timestamp = _utc_datetime(evaluation_result["pivot_timestamp"], "pivot_timestamp")
    rsi_reset_timestamp = _utc_datetime(
        evaluation_result["rsi_reset_timestamp"], "rsi_reset_timestamp"
    )
    created_at = _utc_datetime(generated_at, "generated_at")

    return EmaSignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        tp_price=tp,
        sl_price=sl,
        leverage=leverage,
        tp_roi_pct=tp_roi,
        sl_roi_pct=sl_roi,
        rr=rr,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        setup_key=setup_key,
        confirmation_candle_open=confirmation_open,
        confirmation_close_at=confirmation_close,
        pivot_timestamp=pivot_timestamp,
        rsi_reset_timestamp=rsi_reset_timestamp,
        generated_at=created_at,
    )
