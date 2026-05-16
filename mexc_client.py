import time
import random
import requests
import pandas as pd
from requests.adapters import HTTPAdapter
from config import MEXC_BASE_URL

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

# Increase HTTP connection pool size to avoid urllib3 pool warnings.
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

# MEXC Futures does not support Min3 directly.
# For "3m", we fetch 1m candles and resample to 3m in Python.
INTERVAL_MAP = {
    "1m":  "Min1",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "1d":  "Day1",
}


def _is_rate_limit_error(error: Exception) -> bool:
    msg = str(error).lower()
    return (
        "too frequent" in msg
        or "rate limit" in msg
        or "429" in msg
        or "try again later" in msg
    )


def _get(path: str, params: dict = None, retries: int = 5) -> dict:
    url = f"{MEXC_BASE_URL}{path}"

    for attempt in range(retries):
        try:
            response = SESSION.get(url, params=params, timeout=15)
            response.raise_for_status()

            data = response.json()

            if data.get("success") is False:
                raise ValueError(f"MEXC API error: {data.get('message')}")

            return data

        except Exception as e:
            if attempt == retries - 1:
                raise

            if _is_rate_limit_error(e):
                sleep_seconds = min(3 + attempt * 2 + random.uniform(0.3, 1.5), 12)
            else:
                sleep_seconds = min(1 + attempt + random.uniform(0.2, 0.8), 6)

            time.sleep(sleep_seconds)

    return {}


def get_all_contracts() -> list[dict]:
    """Return all MEXC futures contract details."""
    data = _get("/contract/detail")
    return data.get("data", [])


def get_tickers() -> dict[str, dict]:
    """Return a dict of symbol -> ticker data."""
    data = _get("/contract/ticker")

    result = {}

    for ticker in data.get("data", []):
        symbol = ticker.get("symbol")
        if symbol:
            result[symbol] = ticker

    return result


def _parse_kline_response(raw: dict) -> pd.DataFrame:
    if not raw or "time" not in raw:
        return pd.DataFrame()

    required_fields = ["time", "realOpen", "realHigh", "realLow", "realClose"]

    for field in required_fields:
        if field not in raw:
            return pd.DataFrame()

    vol_data = raw.get("realVolume") or raw.get("vol") or raw.get("volume") or []

    timestamps = raw["time"]
    row_count = len(timestamps)

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": [float(x) for x in raw["realOpen"]],
        "high": [float(x) for x in raw["realHigh"]],
        "low": [float(x) for x in raw["realLow"]],
        "close": [float(x) for x in raw["realClose"]],
        "volume": (
            [float(x) for x in vol_data]
            if vol_data and len(vol_data) == row_count
            else [0.0] * row_count
        ),
    })

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)

    return df


def _resample_to_3m(df_1m: pd.DataFrame, count: int) -> pd.DataFrame:
    """
    Convert 1m candles to 3m candles.

    OHLCV aggregation:
        open   = first open
        high   = max high
        low    = min low
        close  = last close
        volume = sum volume
    """
    if df_1m.empty:
        return pd.DataFrame()

    df_3m = df_1m.resample(
        "3min",
        label="right",
        closed="right",
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })

    df_3m.dropna(inplace=True)
    df_3m.sort_index(inplace=True)

    return df_3m.tail(count)


def get_klines(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
    """
    Fetch OHLCV klines for a futures symbol.

    Supported app intervals:
        1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d
    """

    if interval == "3m":
        fetch_count = count * 3 + 10

        data = _get(
            f"/contract/kline/{symbol}",
            params={
                "interval": "Min1",
                "count": fetch_count,
            },
        )

        raw = data.get("data", {})
        df_1m = _parse_kline_response(raw)

        return _resample_to_3m(df_1m, count)

    mexc_interval = INTERVAL_MAP.get(interval)

    if not mexc_interval:
        raise ValueError(
            f"Unsupported interval: {interval}. "
            f"Supported intervals: 1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d"
        )

    data = _get(
        f"/contract/kline/{symbol}",
        params={
            "interval": mexc_interval,
            "count": count,
        },
    )

    raw = data.get("data", {})

    return _parse_kline_response(raw)


def get_current_price(symbol: str) -> float | None:
    """Get the latest price for a symbol."""
    try:
        data = _get("/contract/ticker", params={"symbol": symbol})
        tickers = data.get("data", [])

        if isinstance(tickers, list):
            for ticker in tickers:
                if ticker.get("symbol") == symbol:
                    return float(ticker["lastPrice"])

        if isinstance(tickers, dict):
            price = float(tickers.get("lastPrice", 0))
            return price or None

    except Exception:
        return None

    return None