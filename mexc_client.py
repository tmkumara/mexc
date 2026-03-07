import time
import requests
import pandas as pd
from config import MEXC_BASE_URL

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

INTERVAL_MAP = {
    "1m":  "Min1",
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
            r = SESSION.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("success") is False:
                raise ValueError(f"MEXC API error: {data.get('message')}")
            return data
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def get_all_contracts() -> list[dict]:
    """Return all MEXC futures contract details."""
    data = _get("/contract/detail")
    return data.get("data", [])


def get_tickers() -> dict[str, dict]:
    """Return a dict of symbol -> ticker data (24h vol, last price, etc.)."""
    data = _get("/contract/ticker")
    result = {}
    for t in data.get("data", []):
        result[t["symbol"]] = t
    return result


def get_klines(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
    """
    Fetch OHLCV klines for a futures symbol.
    interval: '1m', '5m', '15m', '1h', etc.
    Returns DataFrame with columns: timestamp, open, high, low, close, volume
    """
    mexc_interval = INTERVAL_MAP.get(interval)
    if not mexc_interval:
        raise ValueError(f"Unsupported interval: {interval}")

    data = _get(f"/contract/kline/{symbol}", params={
        "interval": mexc_interval,
        "count": count,
    })

    raw = data.get("data", {})
    if not raw or "time" not in raw:
        return pd.DataFrame()

    df = pd.DataFrame({
        "timestamp": raw["time"],
        "open":      [float(x) for x in raw["realOpen"]],
        "high":      [float(x) for x in raw["realHigh"]],
        "low":       [float(x) for x in raw["realLow"]],
        "close":     [float(x) for x in raw["realClose"]],
        "volume":    [float(x) for x in raw["realVolume"]],
    })
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    return df


def get_current_price(symbol: str) -> float | None:
    """Get the latest price for a symbol."""
    try:
        data = _get(f"/contract/ticker", params={"symbol": symbol})
        tickers = data.get("data", [])
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("symbol") == symbol:
                    return float(t["lastPrice"])
        elif isinstance(tickers, dict):
            return float(tickers.get("lastPrice", 0)) or None
    except Exception:
        return None
