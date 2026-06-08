"""
In-memory candle cache for REST-seeded and WebSocket-updated OHLCV data.

Purpose:
    - Seed initial candles from REST.
    - Update the latest candle from WebSocket push messages.
    - Detect candle close when a newer candle timestamp arrives.
    - Return pandas DataFrames to the existing strategy without changing
      strategy internals immediately.

Design:
    Cache key = (symbol, interval)

    Example:
        ("BTC_USDT", "Min5")
        ("BTC_USDT", "Min30")

Important:
    MEXC Futures kline WebSocket sends updates for the active candle.
    A candle is considered closed only when a newer candle timestamp appears.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# Keep cache keys stable between app timeframes (5m/15m/1h) and MEXC WS
# intervals (Min5/Min15/Min60). This lets strategy.py ask for ENTRY_TF while
# the WebSocket client stores MEXC interval names.
INTERVAL_ALIASES = {
    "1m": "Min1",
    "Min1": "Min1",
    "3m": "Min3",
    "Min3": "Min3",
    "5m": "Min5",
    "Min5": "Min5",
    "15m": "Min15",
    "Min15": "Min15",
    "30m": "Min30",
    "Min30": "Min30",
    "1h": "Min60",
    "60m": "Min60",
    "Min60": "Min60",
    "4h": "Hour4",
    "Hour4": "Hour4",
    "8h": "Hour8",
    "Hour8": "Hour8",
    "1d": "Day1",
    "Day1": "Day1",
}



@dataclass(frozen=True)
class CandleCloseEvent:
    """
    Returned when update_from_ws() detects that the previous candle is closed.
    """

    symbol: str
    interval: str
    closed_timestamp: pd.Timestamp
    closed_candle: dict[str, float]


@dataclass(frozen=True)
class CandleUpdateResult:
    """
    Result of updating the candle cache from a WebSocket candle.
    """

    symbol: str
    interval: str
    timestamp: pd.Timestamp
    is_new_candle: bool
    closed_event: CandleCloseEvent | None = None


class CandleCache:
    """
    Thread-safe in-memory OHLCV cache.

    This class is intentionally simple and independent from the strategy.
    Later, strategy.py can read candles from this cache instead of calling REST.
    """

    def __init__(self, limit: int = 60):
        if limit < 5:
            raise ValueError("Candle cache limit must be at least 5")

        self.limit = limit
        self._data: dict[tuple[str, str], pd.DataFrame] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol).strip().upper()

    @staticmethod
    def _normalize_interval(interval: str) -> str:
        raw = str(interval).strip()
        return INTERVAL_ALIASES.get(raw, INTERVAL_ALIASES.get(raw.lower(), raw))

    @staticmethod
    def _key(symbol: str, interval: str) -> tuple[str, str]:
        return (
            CandleCache._normalize_symbol(symbol),
            CandleCache._normalize_interval(interval),
        )

    @staticmethod
    def _normalize_timestamp(value: Any) -> pd.Timestamp:
        """
        Convert MEXC timestamp into pandas Timestamp.

        Supported inputs:
            - seconds timestamp: 1710000000
            - milliseconds timestamp: 1710000000000
            - datetime
            - pandas Timestamp
            - string datetime
        """
        if isinstance(value, pd.Timestamp):
            return value.tz_localize(None) if value.tzinfo else value

        if isinstance(value, datetime):
            ts = pd.Timestamp(value)
            return ts.tz_localize(None) if ts.tzinfo else ts

        if isinstance(value, (int, float)):
            # MEXC kline 't' is seconds.
            # If a millisecond timestamp appears, handle it safely.
            if value > 10_000_000_000:
                return pd.to_datetime(value, unit="ms")
            return pd.to_datetime(value, unit="s")

        ts = pd.to_datetime(value)

        if getattr(ts, "tzinfo", None):
            ts = ts.tz_localize(None)

        return ts

    @staticmethod
    def _empty_df() -> pd.DataFrame:
        df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        df.index.name = "timestamp"
        return df

    @staticmethod
    def _prepare_seed_df(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return CandleCache._empty_df()

        prepared = df.copy()

        for col in REQUIRED_COLUMNS:
            if col not in prepared.columns:
                if col == "volume":
                    prepared[col] = 0.0
                else:
                    raise ValueError(f"Missing required candle column: {col}")

        prepared = prepared[REQUIRED_COLUMNS].copy()
        prepared.index = pd.to_datetime(prepared.index)
        prepared.index = prepared.index.tz_localize(None) if prepared.index.tz is not None else prepared.index

        for col in REQUIRED_COLUMNS:
            prepared[col] = prepared[col].astype(float)

        prepared.sort_index(inplace=True)
        prepared = prepared[~prepared.index.duplicated(keep="last")]

        return prepared

    @staticmethod
    def _candle_to_row(candle: dict[str, Any]) -> tuple[pd.Timestamp, dict[str, float]]:
        """
        Convert a generic kline dictionary into timestamp + OHLCV row.

        Accepted input keys:
            timestamp/time/t/windowstart
            open/o/ro
            high/h/rh
            low/l/rl
            close/c/rc
            volume/v/q/a
        """
        timestamp_value = (
            candle.get("timestamp")
            or candle.get("time")
            or candle.get("t")
            or candle.get("windowstart")
            or candle.get("windowStart")
        )

        if timestamp_value is None:
            raise ValueError(f"Candle timestamp missing: {candle}")

        timestamp = CandleCache._normalize_timestamp(timestamp_value)

        open_value = candle.get("open", candle.get("o", candle.get("ro")))
        high_value = candle.get("high", candle.get("h", candle.get("rh")))
        low_value = candle.get("low", candle.get("l", candle.get("rl")))
        close_value = candle.get("close", candle.get("c", candle.get("rc")))
        volume_value = candle.get("volume", candle.get("v", candle.get("q", candle.get("a", 0.0))))

        values = {
            "open": float(open_value),
            "high": float(high_value),
            "low": float(low_value),
            "close": float(close_value),
            "volume": float(volume_value or 0.0),
        }

        return timestamp, values

    def seed(self, symbol: str, interval: str, candles: pd.DataFrame) -> int:
        """
        Seed cache from REST candles.

        Returns:
            Number of candles stored.
        """
        key = self._key(symbol, interval)
        prepared = self._prepare_seed_df(candles).tail(self.limit)

        with self._lock:
            self._data[key] = prepared

        logger.info(
            "[CACHE] Seeded %s %s candles=%s",
            key[0],
            key[1],
            len(prepared),
        )

        return len(prepared)

    def update_from_ws(
        self,
        symbol: str,
        interval: str,
        candle: dict[str, Any],
    ) -> CandleUpdateResult:
        """
        Update cache from a WebSocket candle.

        Returns:
            CandleUpdateResult.

        Candle close detection:
            If incoming timestamp > previous latest timestamp,
            the previous latest candle is considered closed.
        """
        key = self._key(symbol, interval)
        timestamp, row = self._candle_to_row(candle)

        with self._lock:
            df = self._data.get(key)

            if df is None or df.empty:
                df = self._empty_df()
                df.loc[timestamp, REQUIRED_COLUMNS] = [row[col] for col in REQUIRED_COLUMNS]
                df.sort_index(inplace=True)
                self._data[key] = df.tail(self.limit)

                return CandleUpdateResult(
                    symbol=key[0],
                    interval=key[1],
                    timestamp=timestamp,
                    is_new_candle=True,
                    closed_event=None,
                )

            latest_timestamp = df.index[-1]
            closed_event = None
            is_new_candle = timestamp > latest_timestamp

            if timestamp < latest_timestamp:
                # Old update. Ignore safely.
                return CandleUpdateResult(
                    symbol=key[0],
                    interval=key[1],
                    timestamp=timestamp,
                    is_new_candle=False,
                    closed_event=None,
                )

            if timestamp > latest_timestamp:
                closed_row = df.iloc[-1]

                closed_event = CandleCloseEvent(
                    symbol=key[0],
                    interval=key[1],
                    closed_timestamp=latest_timestamp,
                    closed_candle={
                        "open": float(closed_row["open"]),
                        "high": float(closed_row["high"]),
                        "low": float(closed_row["low"]),
                        "close": float(closed_row["close"]),
                        "volume": float(closed_row["volume"]),
                    },
                )

            df.loc[timestamp, REQUIRED_COLUMNS] = [row[col] for col in REQUIRED_COLUMNS]
            df.sort_index(inplace=True)
            df = df[~df.index.duplicated(keep="last")]
            self._data[key] = df.tail(self.limit)

            return CandleUpdateResult(
                symbol=key[0],
                interval=key[1],
                timestamp=timestamp,
                is_new_candle=is_new_candle,
                closed_event=closed_event,
            )

    def get_candles(self, symbol: str, interval: str, limit: int | None = None) -> pd.DataFrame:
        """
        Return candles as a DataFrame copy.

        This keeps strategy code safe from accidental mutation.
        """
        key = self._key(symbol, interval)

        with self._lock:
            df = self._data.get(key)

            if df is None:
                return self._empty_df()

            result = df.copy()

        if limit is not None:
            result = result.tail(limit)

        return result

    def is_ready(self, symbol: str, interval: str, min_count: int = 50) -> bool:
        key = self._key(symbol, interval)

        with self._lock:
            df = self._data.get(key)
            return df is not None and len(df) >= min_count

    def count(self, symbol: str, interval: str) -> int:
        key = self._key(symbol, interval)

        with self._lock:
            df = self._data.get(key)
            return 0 if df is None else len(df)

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._data.keys())

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {
                f"{symbol}:{interval}": len(df)
                for (symbol, interval), df in self._data.items()
            }

    def clear(self) -> None:
        with self._lock:
            self._data.clear()