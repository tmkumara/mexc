"""
Coin selection: top N coins by open interest from CoinGlass API.
Falls back to MEXC 24h volume ranking when no API key is configured.
Refreshed every COIN_REFRESH_HOURS hours.

RSI pre-filter runs every scan cycle (every 5 min) to narrow the
100-coin pool down to coins with RSI extremes before full analysis.
"""

import logging
import pandas_ta as ta
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from mexc_client import get_all_contracts, get_tickers, get_klines
from config import (
    EXCLUDE_COINS, TOP_N_COINS, COINGLASS_API_KEY,
    COIN_POOL_MIN_VOLUME_USD,
    RSI_PREFILTER_OVERSOLD, RSI_PREFILTER_OVERBOUGHT,
    RSI_PREFILTER_BARS, PREFILTER_WORKERS,
    SWEEP_TF, RSI_PERIOD,
)

logger = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"

_cached_coins: list[str] = []


# ── CoinGlass source ──────────────────────────────────────────────

def _fetch_coinglass_coins() -> list[str]:
    """Return top coins by aggregated open interest from CoinGlass."""
    try:
        r = requests.get(
            f"{COINGLASS_BASE}/open_interest",
            headers={"coinglassSecret": COINGLASS_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        if str(data.get("code")) != "0":
            logger.warning(f"CoinGlass API: {data.get('msg', 'unknown error')}")
            return []

        items = data.get("data", [])
        if not items:
            return []

        def _oi(item: dict) -> float:
            for key in ("openInterest", "oi", "usdtOI", "oiUsd"):
                v = item.get(key)
                if v is not None:
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        items.sort(key=_oi, reverse=True)

        symbols = []
        for item in items:
            coin = (item.get("symbol") or item.get("baseSymbol") or "").upper().strip()
            if not coin:
                continue
            mexc_sym = f"{coin}_USDT"
            if mexc_sym in EXCLUDE_COINS:
                continue
            symbols.append(mexc_sym)
            if len(symbols) >= TOP_N_COINS * 2:
                break

        logger.info(f"CoinGlass OI: fetched {len(symbols)} coins")
        return symbols

    except Exception as e:
        logger.error(f"CoinGlass fetch error: {e}")
        return []


# ── MEXC fallback ─────────────────────────────────────────────────

def _fetch_mexc_coins() -> list[str]:
    """Fallback: top USDT perps by 24h volume from MEXC, above min volume."""
    try:
        tickers = get_tickers()
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("_USDT") or sym in EXCLUDE_COINS:
                continue
            vol = 0.0
            for key in ("amount24", "volume24", "vol24", "volume"):
                v = t.get(key)
                if v is not None:
                    try:
                        vol = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            if vol < COIN_POOL_MIN_VOLUME_USD:
                continue
            rows.append((sym, vol))

        rows.sort(key=lambda x: x[1], reverse=True)
        symbols = [sym for sym, _ in rows[:TOP_N_COINS]]
        logger.info(f"MEXC volume fallback: {len(symbols)} coins (min vol ${COIN_POOL_MIN_VOLUME_USD/1e6:.0f}M)")
        return symbols

    except Exception as e:
        logger.error(f"MEXC top coins error: {e}")
        return []


# ── Public API ────────────────────────────────────────────────────

def refresh_coin_list() -> list[str]:
    """Fetch a fresh coin list, validate against active MEXC contracts, cache it."""
    global _cached_coins

    raw = _fetch_coinglass_coins() if COINGLASS_API_KEY else []
    if not raw:
        logger.info("No CoinGlass data — using MEXC volume ranking")
        raw = _fetch_mexc_coins()

    # Validate against active MEXC contracts
    try:
        contracts = get_all_contracts()
        active = {c["symbol"] for c in contracts if c.get("state") in (0, None)}
        validated = [s for s in raw if s in active]
        if len(validated) < len(raw):
            logger.debug(f"Filtered {len(raw) - len(validated)} inactive contracts")
        raw = validated
    except Exception as e:
        logger.warning(f"Contract validation skipped: {e}")

    coins = raw[:TOP_N_COINS]

    if coins:
        _cached_coins = coins
        logger.info(
            f"Coin pool updated ({len(_cached_coins)}): "
            f"{[s.replace('_USDT','') for s in _cached_coins[:10]]}..."
        )
    else:
        logger.warning("No coins fetched — keeping previous cache")

    return _cached_coins


def get_cached_coins() -> list[str]:
    """Return cached coin list, refreshing if empty."""
    if not _cached_coins:
        return refresh_coin_list()
    return _cached_coins


# ── RSI pre-filter ────────────────────────────────────────────────

def rsi_prefilter(coins: list[str]) -> list[str]:
    """
    Fast Phase-1 filter: fetch 15M RSI for every coin in parallel.
    Returns coins where RSI < RSI_PREFILTER_OVERSOLD (LONG candidates)
    or RSI > RSI_PREFILTER_OVERBOUGHT (SHORT candidates).
    Coins that error are excluded to avoid wasting Phase-2 time.
    """
    def _check(symbol: str) -> str | None:
        try:
            df = get_klines(symbol, SWEEP_TF, count=RSI_PREFILTER_BARS)
            if df.empty or len(df) < RSI_PERIOD + 1:
                return None
            rsi = ta.rsi(df["close"], length=RSI_PERIOD)
            val = rsi.iloc[-2]   # last completed bar
            if pd.isna(val):
                return None
            val = float(val)
            if val < RSI_PREFILTER_OVERSOLD or val > RSI_PREFILTER_OVERBOUGHT:
                return symbol
            return None
        except Exception:
            return None

    hot: list[str] = []
    with ThreadPoolExecutor(max_workers=PREFILTER_WORKERS) as ex:
        futures = {ex.submit(_check, s): s for s in coins}
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                hot.append(result)

    logger.info(
        f"[RSI-FILTER] {len(coins)} → {len(hot)} coins "
        f"(RSI <{RSI_PREFILTER_OVERSOLD} or >{RSI_PREFILTER_OVERBOUGHT})"
    )
    return hot
