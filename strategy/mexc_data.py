"""
Async MEXC futures OHLCV fetcher with exponential-backoff retry.
Uses the same endpoint as mexc_client.py but with aiohttp for concurrency.
"""

import asyncio
import logging
import random

import aiohttp
import pandas as pd

from config import MEXC_BASE_URL

logger = logging.getLogger(__name__)

INTERVAL_MAP = {
    "1m":  "Min1",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "1d":  "Day1",
}


async def fetch_ohlcv(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str = "5m",
    limit: int = 200,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles for a MEXC futures symbol asynchronously.

    Returns a DataFrame with columns open/high/low/close/volume, indexed by
    a UTC-aware DatetimeIndex sorted ascending.  Returns empty DataFrame on
    failure after 3 attempts.
    """
    mexc_interval = INTERVAL_MAP.get(interval, "Min5")
    url = f"{MEXC_BASE_URL}/contract/kline/{symbol}"
    params = {"interval": mexc_interval, "count": limit}

    for attempt in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, params=params, timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if not data.get("success"):
                    raise ValueError(f"MEXC API error: {data.get('message')}")

                raw = data.get("data", {})
                if not raw or "time" not in raw:
                    return pd.DataFrame()

                vol_data = (
                    raw.get("realVolume")
                    or raw.get("vol")
                    or raw.get("volume")
                    or []
                )
                df = pd.DataFrame({
                    "timestamp": raw["time"],
                    "open":   [float(x) for x in raw["realOpen"]],
                    "high":   [float(x) for x in raw["realHigh"]],
                    "low":    [float(x) for x in raw["realLow"]],
                    "close":  [float(x) for x in raw["realClose"]],
                    "volume": (
                        [float(x) for x in vol_data]
                        if vol_data
                        else [0.0] * len(raw["time"])
                    ),
                })
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
                df.set_index("timestamp", inplace=True)
                df.sort_index(inplace=True)
                return df

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt == 2:
                logger.error(f"fetch_ohlcv failed for {symbol}/{interval}: {e}")
                return pd.DataFrame()
            wait = 2 ** attempt + random.uniform(0, 1)
            logger.debug(f"fetch_ohlcv retry {attempt + 1} for {symbol}: {e}")
            await asyncio.sleep(wait)

    return pd.DataFrame()
