import time
import requests
import pandas as pd
from config import MEXC_BASE_URL

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

INTERVAL_MAP = {
    "1m":  "Min1",
    "3m":  "Min3",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "1d":  "Day1",
}


def _get(path: str, params: dict = None, retries: int = 3) -> dict:
    url = f"{MEXC_BASE_URL}{path}"

    for attempt in range(retries):
        try:
            response = SESSION.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if data.get("success") is False:
                raise ValueError(f"MEXC API error: {data.get('message')}")

            return data

        except Exception:
            if attempt == retries - 1:
                raise

            time.sleep(2 ** attempt)

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


def get_klines(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
    """
    Fetch OHLCV klines for a futures symbol.

    Supported app intervals:
        1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d

    MEXC interval examples:
        Min1, Min3, Min5, Min15, Min30, Min60, Hour4, Day1

    Returns DataFrame with:
        open, high, low, close, volume
    """
    mexc_interval = INTERVAL_MAP.get(interval)

    if not mexc_interval:
        raise ValueError(
            f"Unsupported interval: {interval}. "
            f"Supported intervals: {', '.join(INTERVAL_MAP.keys())}"
        )

    data = _get(
        f"/contract/kline/{symbol}",
        params={
            "interval": mexc_interval,
            "count": count,
        },
    )

    raw = data.get("data", {})

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