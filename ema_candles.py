"""Validation and completeness helpers for standalone EMA candle data."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd


_COLUMNS = ["open", "high", "low", "close", "volume"]
_TIMEFRAMES = {"15m": pd.Timedelta(minutes=15), "1h": pd.Timedelta(hours=1)}


def _duration(timeframe: str) -> pd.Timedelta:
    if timeframe not in _TIMEFRAMES:
        raise ValueError("unsupported timeframe; expected 15m or 1h")
    return _TIMEFRAMES[timeframe]


def _utc_index(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("candle index must be a DatetimeIndex")
    try:
        normalized = pd.DatetimeIndex(pd.to_datetime(index, utc=True, errors="raise"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("candle timestamps must be valid") from exc
    if normalized.hasnans:
        raise ValueError("candle timestamps must be valid")
    if not normalized.is_monotonic_increasing:
        raise ValueError("candle timestamps must be ascending")
    if normalized.has_duplicates:
        raise ValueError("candle timestamps must not contain duplicates")
    return normalized


def _aligned(index: pd.DatetimeIndex, timeframe: str) -> bool:
    if len(index) == 0:
        return True
    if timeframe == "15m":
        return bool((index.minute % 15 == 0).all()) and bool((index.second == 0).all()) and bool((index.microsecond == 0).all())
    return bool((index.minute == 0).all()) and bool((index.second == 0).all()) and bool((index.microsecond == 0).all())


def validate_candles(candles: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Validate and return a copied, UTC-normalized OHLCV candle frame."""
    _duration(timeframe)
    if not isinstance(candles, pd.DataFrame):
        raise TypeError("candles must be a pandas DataFrame")
    missing = [column for column in _COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"candles missing required columns: {', '.join(missing)}")

    index = _utc_index(candles.index)
    if not _aligned(index, timeframe):
        raise ValueError("candle timestamps are not aligned to the timeframe")

    frame = candles.loc[:, _COLUMNS].copy(deep=True)
    frame.index = index
    try:
        for column in _COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("candle values must be numeric") from exc

    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("candle OHLCV values must be finite")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("candle prices must be positive")
    if (frame["volume"] < 0).any():
        raise ValueError("candle volume must be non-negative")
    if (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
        or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise ValueError("invalid OHLC relationship")
    return frame


def _boundary(value: datetime, timeframe: str, field_name: str) -> pd.Timestamp:
    if not isinstance(value, (datetime, pd.Timestamp)):
        raise ValueError(f"{field_name} must be a datetime")
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    timestamp = timestamp.tz_convert("UTC")
    if not _aligned(pd.DatetimeIndex([timestamp]), timeframe):
        raise ValueError(f"{field_name} is not aligned to the timeframe")
    return timestamp


def find_missing_candles(
    candles: pd.DataFrame,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> list[datetime]:
    """Return missing UTC candle-open timestamps in an inclusive range."""
    duration = _duration(timeframe)
    normalized = validate_candles(candles, timeframe)
    start = _boundary(start_at, timeframe, "start_at")
    end = _boundary(end_at, timeframe, "end_at")
    if end < start:
        raise ValueError("end_at must not precede start_at")

    expected = pd.date_range(start=start, end=end, freq=duration)
    present = set(normalized.index)
    return [timestamp.to_pydatetime() for timestamp in expected if timestamp not in present]
