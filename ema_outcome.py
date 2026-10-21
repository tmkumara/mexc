"""Pure TP/SL outcome evaluation for standalone EMA signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite

import pandas as pd

from ema_indicators import is_completed_candle
from ema_signal import EmaSignal


@dataclass(frozen=True)
class OutcomeResult:
    status: str
    closed_at: datetime | None
    exit_price: float | None
    gross_roi_pct: float | None
    reason: str


def _utc_datetime(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pending(reason: str) -> OutcomeResult:
    return OutcomeResult("PENDING", None, None, None, reason)


def _gross_roi(signal: EmaSignal, exit_price: float) -> float:
    if signal.direction == "LONG":
        price_return = (exit_price - signal.entry_price) / signal.entry_price
    else:
        price_return = (signal.entry_price - exit_price) / signal.entry_price
    return price_return * signal.leverage * 100.0


def _expected_opens(confirmation_close: datetime, expiry_at: datetime) -> list[datetime]:
    opens: list[datetime] = []
    current = confirmation_close
    while current + timedelta(minutes=15) <= expiry_at:
        opens.append(current)
        current += timedelta(minutes=15)
    return opens


def evaluate_outcome(
    signal: EmaSignal,
    candles: pd.DataFrame,
    evaluation_timestamp: datetime,
    expiry_at: datetime,
) -> OutcomeResult:
    """Replay eligible completed candles and return a deterministic outcome."""
    if not isinstance(signal, EmaSignal):
        raise ValueError("signal must be an EmaSignal")
    required_columns = {"open", "high", "low", "close"}
    if not required_columns.issubset(candles.columns):
        raise ValueError("candles must contain open, high, low, close columns")
    if not isinstance(candles.index, pd.DatetimeIndex) or not candles.index.is_monotonic_increasing:
        raise ValueError("candles must use an ordered DatetimeIndex")
    if candles.index.has_duplicates:
        raise ValueError("candles must not contain duplicate timestamps")

    evaluation = _utc_datetime(evaluation_timestamp, "evaluation_timestamp")
    expiry = _utc_datetime(expiry_at, "expiry_at")
    confirmation_close = signal.confirmation_close_at.astimezone(timezone.utc)
    if expiry <= confirmation_close:
        raise ValueError("expiry_at must be after confirmation_close_at")

    rows: list[tuple[datetime, datetime, float, float, float, float]] = []
    for index, row in candles.iterrows():
        candle_open = _utc_datetime(index, "candle timestamp")
        candle_close = candle_open + timedelta(minutes=15)
        if candle_open < confirmation_close or candle_close > expiry:
            continue
        if not is_completed_candle(candle_open, pd.Timedelta(minutes=15), evaluation, 90):
            continue
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if not all(isfinite(value) for value in (high, low, close)):
            raise ValueError("candle OHLC values must be finite")
        rows.append((candle_open, candle_close, high, low, close, float(row["open"])))

    if not rows and len(candles) > 0:
        return _pending("insufficient_history")

    for candle_open, candle_close, high, low, close, _opening in rows:
        if signal.direction == "LONG":
            sl_hit = low <= signal.sl_price
            tp_hit = high >= signal.tp_price
        else:
            sl_hit = high >= signal.sl_price
            tp_hit = low <= signal.tp_price

        if sl_hit and tp_hit:
            return OutcomeResult(
                "LOSS",
                candle_close,
                signal.sl_price,
                _gross_roi(signal, signal.sl_price),
                "both_hit_sl_first",
            )
        if sl_hit:
            return OutcomeResult(
                "LOSS",
                candle_close,
                signal.sl_price,
                _gross_roi(signal, signal.sl_price),
                "sl_hit",
            )
        if tp_hit:
            return OutcomeResult(
                "WIN",
                candle_close,
                signal.tp_price,
                _gross_roi(signal, signal.tp_price),
                "tp_hit",
            )

    if not rows:
        return _pending("insufficient_history")
    if evaluation < expiry:
        return _pending("awaiting_expiry")
    rows_by_open = {row[0]: row for row in rows}
    expected_opens = _expected_opens(confirmation_close, expiry)
    if not expected_opens or any(open_at not in rows_by_open for open_at in expected_opens):
        return _pending("insufficient_history")
    final_open, final_close, _high, _low, close, _opening = rows[-1]
    return OutcomeResult(
        "TIMEOUT",
        final_close,
        close,
        _gross_roi(signal, close),
        "expiry_reached",
    )
