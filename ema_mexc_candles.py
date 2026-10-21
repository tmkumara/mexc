"""Pure adapter for MEXC Futures historical kline responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from ema_candles import validate_candles


_REQUIRED_ARRAYS = ("time", "open", "high", "low", "close", "vol")


def _array(data: Mapping[str, object], field: str) -> list[object]:
    value = data.get(field)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"required candle array {field!r} is malformed")
    return list(value)


def parse_mexc_futures_candles(payload: object, timeframe: str) -> pd.DataFrame:
    """Convert one MEXC Futures kline response into the EMA candle contract.

    MEXC's documented REST response stores parallel arrays under ``data``.
    ``time`` is Unix seconds and the required market fields are ``open``,
    ``high``, ``low``, ``close``, and ``vol``.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("MEXC response envelope must be a mapping")
    if payload.get("success") is False:
        raise ValueError(f"MEXC API reported failure: {payload.get('message', 'unknown error')}")
    if "success" in payload and payload["success"] is not True:
        raise ValueError("MEXC response envelope has an invalid success value")
    if "data" not in payload:
        raise ValueError("MEXC response is missing response data")
    data = payload["data"]
    if not isinstance(data, Mapping):
        raise ValueError("MEXC response data must be a mapping")

    arrays: dict[str, list[object]] = {}
    for field in _REQUIRED_ARRAYS:
        if field not in data:
            raise ValueError(f"required candle array {field!r} is missing")
        arrays[field] = _array(data, field)

    lengths = {field: len(values) for field, values in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError("MEXC candle arrays must have the same length")

    try:
        timestamps = pd.DatetimeIndex(pd.to_datetime(arrays["time"], unit="s", utc=True, errors="raise"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("MEXC candle timestamps are invalid Unix seconds") from exc

    if timestamps.has_duplicates:
        raise ValueError("MEXC candle timestamps contain duplicates")
    if len(timestamps) > 1:
        if timestamps.is_monotonic_decreasing:
            for field in arrays:
                arrays[field].reverse()
            timestamps = timestamps[::-1]
        elif not timestamps.is_monotonic_increasing:
            raise ValueError("MEXC candle timestamps are not chronological")

    frame = pd.DataFrame(
        {
            "open": arrays["open"],
            "high": arrays["high"],
            "low": arrays["low"],
            "close": arrays["close"],
            "volume": arrays["vol"],
        },
        index=timestamps,
    )
    return validate_candles(frame, timeframe)
