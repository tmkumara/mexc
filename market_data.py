"""
Market data provider for the MEXC signal bot.

Purpose:
    - Provide a single candle access layer for strategy/main.
    - Prefer WebSocket CandleCache when enough candles are available.
    - Fall back to REST when cache is not ready or missing.

Important:
    The strategy usually requests 220 candles for trend and entry analysis.
    If the cache contains only 60 candles, returning cache data would break
    market-structure detection and produce 0 setups.

Usage:
    from market_data import get_market_klines as get_klines

    df = get_klines("BTC_USDT", "5m", count=100)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from candle_cache import CandleCache
from mexc_client import get_klines as get_rest_klines
from config import MEXC_INTERVAL_MAP

logger = logging.getLogger(__name__)

_CANDLE_CACHE: Optional[CandleCache] = None


def set_candle_cache(cache: CandleCache | None) -> None:
    """
    Register the runtime CandleCache.

    main.py should call this once after creating CandleCache.
    """
    global _CANDLE_CACHE
    _CANDLE_CACHE = cache

    if cache is None:
        logger.info("[MARKET-DATA] Candle cache disabled")
    else:
        logger.info("[MARKET-DATA] Candle cache registered")


def get_candle_cache() -> CandleCache | None:
    """
    Return the registered CandleCache, if available.
    """
    return _CANDLE_CACHE


def _to_mexc_interval(app_interval: str) -> str | None:
    """
    Convert app interval to MEXC interval.

    Example:
        5m  -> Min5
        30m -> Min30
    """
    return MEXC_INTERVAL_MAP.get(app_interval)


def get_market_klines(
    symbol: str,
    interval: str,
    count: int = 100,
    *,
    allow_rest_fallback: bool = True,
) -> pd.DataFrame:
    """
    Return OHLCV candles.

    Priority:
        1. CandleCache only if it has at least the requested candle count.
        2. REST fallback using mexc_client.get_klines().

    Why strict count check matters:
        strategy.py requests 220 candles for structure analysis.
        Returning only 60 cached candles makes the structure look incomplete,
        causing the strategy to return 0 setups.

    Args:
        symbol:
            MEXC futures symbol, e.g. BTC_USDT.
        interval:
            App interval, e.g. 5m, 30m, 1h.
        count:
            Number of candles required.
        allow_rest_fallback:
            If False, return empty DataFrame when cache is not ready.

    Returns:
        pandas DataFrame indexed by timestamp with:
            open, high, low, close, volume
    """
    mexc_interval = _to_mexc_interval(interval)

    if _CANDLE_CACHE is not None and mexc_interval:
        cached = _CANDLE_CACHE.get_candles(
            symbol=symbol,
            interval=mexc_interval,
            limit=count,
        )

        cached_count = 0 if cached is None else len(cached)

        if cached is not None and not cached.empty and cached_count >= count:
            logger.debug(
                "[MARKET-DATA] Cache hit %s %s candles=%s requested=%s",
                symbol,
                interval,
                cached_count,
                count,
            )
            return cached.tail(count).copy()

        logger.debug(
            "[MARKET-DATA] Cache not ready %s %s cached=%s requested=%s",
            symbol,
            interval,
            cached_count,
            count,
        )

    if not allow_rest_fallback:
        return pd.DataFrame()

    logger.debug(
        "[MARKET-DATA] REST fallback %s %s count=%s",
        symbol,
        interval,
        count,
    )

    return get_rest_klines(symbol, interval, count=count)