"""Read-only MEXC Futures historical candle client for the EMA bot."""

from __future__ import annotations

from datetime import datetime
import json
import re
import time
from typing import Any

import pandas as pd
import requests

from ema_candles import find_missing_candles, validate_candles
from ema_mexc_candles import parse_mexc_futures_candles


_INTERVALS = {"15m": "Min15", "1h": "Min60"}
_TIMEFRAME_DELTAS = {"15m": pd.Timedelta(minutes=15), "1h": pd.Timedelta(hours=1)}
_MAX_EXPECTED_CANDLES_PER_REQUEST = 1900
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+_USDT$")
_RETRYABLE_STATUS_CODES = {429}


class EmaMexcClient:
    """Small public REST client with bounded retries and strict coverage checks."""

    def __init__(
        self,
        base_url: str = "https://contract.mexc.com/api/v1",
        *,
        timeout: tuple[float, float] = (5.0, 15.0),
        retries: int = 2,
        retry_delay: float = 0.5,
        session: Any | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in timeout)
        ):
            raise ValueError("timeout must be a positive (connect, read) tuple")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer")
        if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or retry_delay < 0:
            raise ValueError("retry_delay must be non-negative")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = float(retry_delay)
        self.session = session if session is not None else requests.Session()

    @staticmethod
    def _boundary(value: datetime, timeframe: str, field_name: str) -> pd.Timestamp:
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be a valid datetime") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(f"{field_name} must be UTC-aware")
        timestamp = timestamp.tz_convert("UTC")
        if timeframe == "15m":
            aligned = timestamp.minute % 15 == 0
        else:
            aligned = timestamp.minute == 0
        if not aligned or timestamp.second != 0 or timestamp.microsecond != 0:
            raise ValueError(f"{field_name} is not aligned to {timeframe}")
        return timestamp

    @staticmethod
    def _validate_inputs(symbol: str, timeframe: str) -> None:
        if not isinstance(symbol, str) or not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol must use the MEXC format like BTC_USDT")
        if timeframe not in _INTERVALS:
            raise ValueError("unsupported timeframe; expected 15m or 1h")

    def _request(self, url: str, params: dict[str, object]) -> object:
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.retries:
                    raise RuntimeError("MEXC request failed after retries") from exc
                self._sleep_before_retry()
                continue

            status = int(response.status_code)
            retryable = status in _RETRYABLE_STATUS_CODES or status >= 500
            if retryable:
                if attempt >= self.retries:
                    raise RuntimeError(f"MEXC retryable HTTP failure after retries: {status}")
                self._sleep_before_retry()
                continue
            if status >= 400:
                raise ValueError(f"MEXC HTTP {status} request failure")
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError("MEXC response was not valid JSON") from exc

        raise RuntimeError("MEXC request failed unexpectedly")

    def _sleep_before_retry(self) -> None:
        if self.retry_delay:
            time.sleep(self.retry_delay)

    @staticmethod
    def _windows(start: pd.Timestamp, end: pd.Timestamp, timeframe: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        step = _TIMEFRAME_DELTAS[timeframe]
        windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        current = start
        span = step * (_MAX_EXPECTED_CANDLES_PER_REQUEST - 1)
        while current <= end:
            window_end = min(current + span, end)
            windows.append((current, window_end))
            if window_end == end:
                break
            current = window_end
        return windows

    def _fetch_window(
        self,
        symbol: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        payload = self._request(
            f"{self.base_url}/contract/kline/{symbol}",
            {
                "interval": _INTERVALS[timeframe],
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
            },
        )
        candles = parse_mexc_futures_candles(payload, timeframe)
        if len(candles) and (candles.index[0] < start or candles.index[-1] > end):
            raise ValueError("MEXC response extends beyond requested range")
        missing = find_missing_candles(candles, timeframe, start.to_pydatetime(), end.to_pydatetime())
        if missing:
            raise ValueError("MEXC response does not cover requested range")
        return candles

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
    ) -> pd.DataFrame:
        """Fetch one fully covered inclusive UTC candle range."""
        self._validate_inputs(symbol, timeframe)
        start = self._boundary(start_at, timeframe, "start_at")
        end = self._boundary(end_at, timeframe, "end_at")
        if end < start:
            raise ValueError("end_at must not precede start_at")

        windows = self._windows(start, end, timeframe)
        frames: list[pd.DataFrame] = []
        seen: set[pd.Timestamp] = set()
        for position, (window_start, window_end) in enumerate(windows):
            frame = self._fetch_window(symbol, timeframe, window_start, window_end)
            planned_overlap = window_start if position > 0 else None
            for timestamp in frame.index:
                if timestamp in seen and timestamp != planned_overlap:
                    raise ValueError("unexpected duplicate candle timestamp across windows")
            if planned_overlap is not None:
                frame = frame.drop(index=planned_overlap)
            seen.update(frame.index)
            frames.append(frame)

        merged = pd.concat(frames).sort_index()
        merged = validate_candles(merged, timeframe)
        missing = find_missing_candles(merged, timeframe, start.to_pydatetime(), end.to_pydatetime())
        if missing:
            raise ValueError("MEXC merged response does not cover requested range")
        expected_count = len(pd.date_range(start=start, end=end, freq=_TIMEFRAME_DELTAS[timeframe]))
        if len(merged) != expected_count:
            raise ValueError("MEXC merged response has an unexpected candle count")
        return merged
