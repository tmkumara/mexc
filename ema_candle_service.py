"""Read-only validated candle service for the standalone EMA bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd

from ema_candles import validate_candles


SUPPORTED_SYMBOLS = frozenset({
    "SOL_USDT",
    "XRP_USDT",
    "DOGE_USDT",
    "LINK_USDT",
    "AVAX_USDT",
})


class CandleClient(Protocol):
    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
    ) -> pd.DataFrame:
        """Fetch a complete validated candle range."""


@dataclass(frozen=True)
class EmaCandleHistory:
    """Separate completed candle datasets indexed by candle-open UTC time."""

    candles_15m: pd.DataFrame
    candles_1h: pd.DataFrame


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware")
    return value


class EmaCandleService:
    """Thin service that fetches and validates both EMA timeframes."""

    def __init__(self, client: CandleClient) -> None:
        if client is None or not callable(getattr(client, "fetch_candles", None)):
            raise TypeError("client must provide fetch_candles()")
        self.client = client

    def fetch_history(
        self,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> EmaCandleHistory:
        """Return complete 15m and 1h frames for inclusive UTC boundaries.

        DataFrame indexes represent candle OPEN timestamps. The client is
        called once per timeframe; failures are propagated without returning a
        partial result or retrying at this service layer.
        """
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError(f"unsupported symbol: {symbol}")
        start = _require_utc_datetime(start_at, "start_at")
        end = _require_utc_datetime(end_at, "end_at")
        if start >= end:
            raise ValueError("start_at must be before end_at")

        candles_15m = validate_candles(
            self.client.fetch_candles(symbol, "15m", start, end),
            "15m",
        )
        candles_1h = validate_candles(
            self.client.fetch_candles(symbol, "1h", start, end),
            "1h",
        )
        return EmaCandleHistory(candles_15m=candles_15m, candles_1h=candles_1h)
