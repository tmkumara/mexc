"""
Smart MEXC Futures-only coin scanner.

Purpose:
    - Build the coin pool using MEXC futures contract symbols only.
    - Avoid symbols not listed on MEXC futures.
    - Avoid stock-token futures and commodity/index futures when CRYPTO_FUTURES_ONLY=True.
    - Rank futures symbols using liquidity + recent activity.
    - Backfill pool to COIN_POOL_MIN_SELECTED if smart ranking yields too few.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from mexc_client import get_all_contracts, get_tickers, get_klines
from config import (
    EXCLUDE_COINS,
    TOP_N_COINS,
    COINGLASS_API_KEY,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    ENABLE_SMART_COIN_RANKING,
    COIN_RANK_CANDIDATE_MULTIPLIER,
    COIN_RANK_MAX_CANDIDATES,
    COIN_RANK_TIMEFRAME,
    COIN_RANK_KLINE_COUNT,
    COIN_RANK_WORKERS,
    COIN_RANK_MIN_LAST_PRICE,
    COIN_RANK_MIN_RANGE_PCT,
    COIN_RANK_MAX_RANGE_PCT,
    COIN_RANK_MAX_ABS_MOVE_PCT,
    COIN_RANK_VOLUME_WEIGHT,
    COIN_RANK_VOLATILITY_WEIGHT,
    COIN_RANK_TREND_WEIGHT,
    COIN_RANK_LIQUIDITY_WEIGHT,
    COIN_RANK_OVEREXTENSION_PENALTY,
    COIN_RANK_LOW_ACTIVITY_PENALTY,
    QUOTE_CURRENCY,
    CRYPTO_FUTURES_ONLY,
)

logger = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"

_cached_coins: list[str] = []
_cached_scores: list[dict] = []
_last_refresh_at: datetime | None = None
_cached_valid_futures: set[str] = set()


# ── safe conversion helpers ───────────────────────────────────────

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def _ticker_volume_usd(ticker: dict) -> float:
    for key in (
        "amount24",
        "amount",
        "turnover24",
        "turnover",
        "volume24",
        "vol24",
        "quoteVolume",
        "volume",
    ):
        vol = _to_float(ticker.get(key), 0.0)
        if vol > 0:
            return vol
    return 0.0


def _ticker_last_price(ticker: dict) -> float:
    for key in ("lastPrice", "last", "fairPrice", "indexPrice"):
        price = _to_float(ticker.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def _normalize_score(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    return max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))


# ── futures contract filtering ────────────────────────────────────

# Keywords that identify non-crypto contracts (stocks, indices, commodities, metals).
# Checked against the full symbol string (upper-case, e.g. "NAS100_USDT").
_NON_CRYPTO_KEYWORDS = (
    # Equity / index futures
    "STOCK",
    "INDEX",
    "ETF",
    "NASDAQ",
    "NYSE",
    "NAS100",
    "NAS",       # catches NAS100 variants
    "SPX",
    "DJI",
    # Commodity / energy
    "CRUDE",
    "USOIL",
    "BRENT",
    "OIL",
    # Precious metals
    "GOLD",
    "SILVER",
    "XAU",
    "XAG",
    "XPT",
    "XPD",
)


def _is_crypto_symbol(symbol: str) -> bool:
    """
    Returns False for MEXC stock/index/commodity style contracts.
    Always returns True when CRYPTO_FUTURES_ONLY=False.
    """
    if not CRYPTO_FUTURES_ONLY:
        return True

    upper = symbol.upper()
    return not any(keyword in upper for keyword in _NON_CRYPTO_KEYWORDS)


def _is_contract_active(contract: dict) -> bool:
    symbol = str(contract.get("symbol") or "").upper().strip()

    if not symbol:
        return False

    if not symbol.endswith(f"_{QUOTE_CURRENCY}"):
        return False

    if symbol in EXCLUDE_COINS:
        return False

    if not _is_crypto_symbol(symbol):
        return False

    state = contract.get("state")

    if state is not None:
        try:
            if int(state) != 0:
                return False
        except Exception:
            state_text = str(state).lower().strip()
            if state_text not in ("0", "online", "enabled", "normal", "trading"):
                return False

    status = contract.get("status")

    if status is not None:
        status_text = str(status).lower().strip()
        if status_text in {
            "offline",
            "delisted",
            "suspend",
            "suspended",
            "disabled",
            "false",
        }:
            return False

    return True


def _fetch_valid_futures_symbols(tickers: dict[str, dict]) -> set[str]:
    global _cached_valid_futures

    try:
        contracts = get_all_contracts()
    except Exception as e:
        logger.error("[COIN-FILTER] failed to fetch futures contracts: %s", e)
        return _cached_valid_futures

    total_usdt_contracts = sum(
        1 for c in contracts
        if str(c.get("symbol", "")).upper().strip().endswith(f"_{QUOTE_CURRENCY}")
    )

    # Count how many are blocked as non-crypto
    non_crypto_blocked = sum(
        1 for c in contracts
        if str(c.get("symbol", "")).upper().strip().endswith(f"_{QUOTE_CURRENCY}")
        and not _is_crypto_symbol(str(c.get("symbol", "")).upper().strip())
    )

    contract_symbols = {
        str(contract.get("symbol")).upper().strip()
        for contract in contracts
        if _is_contract_active(contract)
    }

    ticker_symbols = {
        str(symbol).upper().strip()
        for symbol in tickers.keys()
        if str(symbol).upper().strip().endswith(f"_{QUOTE_CURRENCY}")
        and _is_crypto_symbol(str(symbol).upper().strip())
    }

    if ticker_symbols:
        valid = contract_symbols & ticker_symbols
    else:
        valid = contract_symbols

    valid = {
        symbol
        for symbol in valid
        if symbol
        and symbol not in EXCLUDE_COINS
        and _is_crypto_symbol(symbol)
    }

    _cached_valid_futures = valid

    logger.info(
        "[COIN-FILTER] contracts_total=%s usdt_contracts=%s non_crypto_blocked=%s "
        "active_%s_futures=%s tickers_crypto=%s valid=%s excluded=%s crypto_only=%s",
        len(contracts),
        total_usdt_contracts,
        non_crypto_blocked,
        QUOTE_CURRENCY,
        len(contract_symbols),
        len(ticker_symbols),
        len(valid),
        len(EXCLUDE_COINS),
        CRYPTO_FUTURES_ONLY,
    )

    return valid


def _filter_valid_futures(symbols: list[str], valid_futures: set[str]) -> list[str]:
    filtered = []
    seen = set()

    for symbol in symbols:
        symbol = str(symbol).upper().strip()

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)

        if not _is_crypto_symbol(symbol):
            continue

        if symbol not in valid_futures:
            continue

        filtered.append(symbol)

    removed = len(seen) - len(filtered)

    if removed:
        logger.info("[COIN-FILTER] removed non-futures/invalid symbols=%s", removed)

    return filtered


# ── candidate fetchers ────────────────────────────────────────────

def _fetch_coinglass_candidates(valid_futures: set[str]) -> list[tuple[str, float]]:
    if not COINGLASS_API_KEY:
        return []

    try:
        response = requests.get(
            f"{COINGLASS_BASE}/open_interest",
            headers={"coinglassSecret": COINGLASS_API_KEY},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        if str(data.get("code")) != "0":
            logger.warning("[COIN-RANK] CoinGlass API: %s", data.get("msg", "unknown error"))
            return []

        items = data.get("data", [])
        if not items:
            return []

        def _oi(item: dict) -> float:
            for key in ("openInterest", "oi", "usdtOI", "oiUsd"):
                val = _to_float(item.get(key), 0.0)
                if val > 0:
                    return val
            return 0.0

        items.sort(key=_oi, reverse=True)

        max_candidates = min(
            TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER,
            COIN_RANK_MAX_CANDIDATES,
        )

        rows: list[tuple[str, float]] = []

        for item in items:
            coin = (item.get("symbol") or item.get("baseSymbol") or "").upper().strip()

            if not coin:
                continue

            symbol = f"{coin}_{QUOTE_CURRENCY}"

            if symbol not in valid_futures:
                continue

            if not _is_crypto_symbol(symbol):
                continue

            rows.append((symbol, _oi(item)))

            if len(rows) >= max_candidates:
                break

        logger.info("[COIN-RANK] CoinGlass futures candidates=%s", len(rows))
        return rows

    except Exception as e:
        logger.error("[COIN-RANK] CoinGlass fetch error: %s", e)
        return []


def _fetch_mexc_volume_candidates(
    tickers: dict[str, dict],
    valid_futures: set[str],
) -> list[tuple[str, float]]:
    max_candidates = min(
        TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER,
        COIN_RANK_MAX_CANDIDATES,
    )

    total_checked = 0
    passed_volume = 0
    rows: list[tuple[str, float]] = []

    for symbol, ticker in tickers.items():
        symbol = str(symbol).upper().strip()

        if symbol not in valid_futures:
            continue

        if not _is_crypto_symbol(symbol):
            continue

        total_checked += 1

        last_price = _ticker_last_price(ticker)

        if last_price < COIN_RANK_MIN_LAST_PRICE:
            continue

        vol_usd = _ticker_volume_usd(ticker)

        if vol_usd < COIN_POOL_MIN_VOLUME_USD:
            continue

        passed_volume += 1
        rows.append((symbol, vol_usd))

    rows.sort(key=lambda x: x[1], reverse=True)
    rows = rows[:max_candidates]

    logger.info(
        "[COIN-RANK] MEXC volume scan: checked=%s passed_volume=%s (min=$%.0f) candidates=%s",
        total_checked,
        passed_volume,
        COIN_POOL_MIN_VOLUME_USD,
        len(rows),
    )

    return rows


# ── ranking model ─────────────────────────────────────────────────

def _rank_one_symbol(
    symbol: str,
    ticker: dict,
    raw_priority: float,
    max_raw_priority: float,
) -> dict | None:
    try:
        last_price = _ticker_last_price(ticker)
        volume_usd = _ticker_volume_usd(ticker)

        if last_price < COIN_RANK_MIN_LAST_PRICE:
            return None

        if volume_usd < COIN_POOL_MIN_VOLUME_USD:
            return None

        df = get_klines(symbol, COIN_RANK_TIMEFRAME, count=COIN_RANK_KLINE_COUNT)

        if df is None or df.empty or len(df) < 12:
            return None

        completed = df.iloc[:-1].copy()

        if completed.empty or len(completed) < 10:
            return None

        first_open = float(completed["open"].iloc[0])
        last_close = float(completed["close"].iloc[-1])
        high = float(completed["high"].astype(float).max())
        low = float(completed["low"].astype(float).min())

        if first_open <= 0 or last_close <= 0 or low <= 0:
            return None

        lookback_move_pct = (last_close - first_open) / first_open * 100.0
        abs_move_pct = abs(lookback_move_pct)
        range_pct = (high - low) / low * 100.0

        if range_pct < COIN_RANK_MIN_RANGE_PCT:
            return None

        if range_pct > COIN_RANK_MAX_RANGE_PCT:
            return None

        candle_ranges = (
            (completed["high"].astype(float) - completed["low"].astype(float))
            / completed["close"].astype(float)
            * 100.0
        )

        avg_candle_range_pct = float(candle_ranges.mean())

        volume_score = min(math.log10(max(volume_usd, 1.0)) / 10.0, 1.0)

        volatility_score = _normalize_score(
            avg_candle_range_pct,
            COIN_RANK_MIN_RANGE_PCT / 2.0,
            1.25,
        )

        trend_score = _normalize_score(abs_move_pct, 0.30, 4.50)

        priority_score = raw_priority / max_raw_priority if max_raw_priority > 0 else 0.0
        priority_score = max(0.0, min(1.0, priority_score))

        score = (
            volume_score * COIN_RANK_VOLUME_WEIGHT
            + volatility_score * COIN_RANK_VOLATILITY_WEIGHT
            + trend_score * COIN_RANK_TREND_WEIGHT
            + priority_score * COIN_RANK_LIQUIDITY_WEIGHT
        )

        penalty = 0.0

        if abs_move_pct > COIN_RANK_MAX_ABS_MOVE_PCT:
            penalty += COIN_RANK_OVEREXTENSION_PENALTY

        if avg_candle_range_pct < COIN_RANK_MIN_RANGE_PCT:
            penalty += COIN_RANK_LOW_ACTIVITY_PENALTY

        final_score = max(score - penalty, 0.0)

        return {
            "symbol": symbol,
            "score": round(final_score, 2),
            "volume_usd": round(volume_usd, 2),
            "last_price": last_price,
            "range_pct": round(range_pct, 3),
            "avg_candle_range_pct": round(avg_candle_range_pct, 3),
            "lookback_move_pct": round(lookback_move_pct, 3),
            "volume_score": round(volume_score, 3),
            "volatility_score": round(volatility_score, 3),
            "trend_score": round(trend_score, 3),
            "priority_score": round(priority_score, 3),
            "penalty": round(penalty, 2),
        }

    except Exception as e:
        logger.debug("[COIN-RANK] %s ranking failed: %s", symbol, e)
        return None


def _smart_rank_candidates(
    candidates: list[tuple[str, float]],
    tickers: dict[str, dict],
    valid_futures: set[str],
) -> list[dict]:
    if not candidates:
        return []

    priority_by_symbol: dict[str, float] = {}

    for symbol, raw_priority in candidates:
        symbol = str(symbol).upper().strip()

        if symbol not in valid_futures:
            continue

        if not _is_crypto_symbol(symbol):
            continue

        priority_by_symbol[symbol] = max(
            priority_by_symbol.get(symbol, 0.0),
            raw_priority,
        )

    symbols = list(priority_by_symbol.keys())
    symbols = _filter_valid_futures(symbols, valid_futures)

    max_candidates = min(
        TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER,
        COIN_RANK_MAX_CANDIDATES,
    )

    symbols = symbols[:max_candidates]

    if not symbols:
        return []

    max_raw_priority = max(
        (priority_by_symbol.get(symbol, 0.0) for symbol in symbols),
        default=1.0,
    )

    ranked: list[dict] = []

    logger.info(
        "[COIN-RANK] ranking %s futures candidates using %sx %s candles",
        len(symbols),
        COIN_RANK_KLINE_COUNT,
        COIN_RANK_TIMEFRAME,
    )

    with ThreadPoolExecutor(max_workers=COIN_RANK_WORKERS) as executor:
        futures = {
            executor.submit(
                _rank_one_symbol,
                symbol,
                tickers.get(symbol, {}),
                priority_by_symbol.get(symbol, 0.0),
                max_raw_priority,
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                ranked.append(result)

    ranked.sort(key=lambda row: row["score"], reverse=True)

    return ranked


# ── public API ────────────────────────────────────────────────────

def refresh_coin_list() -> list[str]:
    global _cached_coins, _cached_scores, _last_refresh_at

    logger.info(
        "[COIN-RANK] config: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f "
        "COINGLASS=%s EXCLUDED=%s CRYPTO_ONLY=%s",
        TOP_N_COINS,
        COIN_POOL_MIN_SELECTED,
        COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
        len(EXCLUDE_COINS),
        CRYPTO_FUTURES_ONLY,
    )

    try:
        tickers = get_tickers()
    except Exception as e:
        logger.error("[COIN-RANK] ticker fetch failed: %s", e)
        tickers = {}

    valid_futures = _fetch_valid_futures_symbols(tickers)

    if not valid_futures:
        logger.warning("[COIN-RANK] no valid futures symbols found — keeping previous cache")
        return _cached_coins

    raw_candidates = _fetch_coinglass_candidates(valid_futures)

    if not raw_candidates:
        logger.info("[COIN-RANK] no CoinGlass futures data — using MEXC futures volume candidates")
        raw_candidates = _fetch_mexc_volume_candidates(tickers, valid_futures)

    if not raw_candidates:
        logger.warning("[COIN-RANK] no candidates fetched — keeping previous cache")
        return _cached_coins

    if ENABLE_SMART_COIN_RANKING and tickers:
        ranked = _smart_rank_candidates(raw_candidates, tickers, valid_futures)

        if ranked:
            selected = ranked[:TOP_N_COINS]
            _cached_scores = selected
            _cached_coins = [row["symbol"] for row in selected]
        else:
            logger.warning("[COIN-RANK] smart ranking returned empty — using raw futures fallback")
            symbols = _filter_valid_futures([symbol for symbol, _ in raw_candidates], valid_futures)
            _cached_scores = [{"symbol": s, "score": 0.0} for s in symbols[:TOP_N_COINS]]
            _cached_coins = symbols[:TOP_N_COINS]
    else:
        symbols = _filter_valid_futures([symbol for symbol, _ in raw_candidates], valid_futures)
        _cached_scores = [{"symbol": s, "score": 0.0} for s in symbols[:TOP_N_COINS]]
        _cached_coins = symbols[:TOP_N_COINS]

    # Backfill to COIN_POOL_MIN_SELECTED if smart ranking returned too few
    if len(_cached_coins) < COIN_POOL_MIN_SELECTED:
        existing_set = set(_cached_coins)
        needed = COIN_POOL_MIN_SELECTED - len(_cached_coins)
        backfill_count = 0
        for sym, _ in raw_candidates:
            if sym not in existing_set:
                _cached_coins.append(sym)
                _cached_scores.append({"symbol": sym, "score": 0.0, "note": "backfill"})
                existing_set.add(sym)
                backfill_count += 1
                if backfill_count >= needed:
                    break
        if backfill_count:
            logger.info(
                "[COIN-RANK] backfilled %d coins to meet COIN_POOL_MIN_SELECTED=%d (pool=%d)",
                backfill_count,
                COIN_POOL_MIN_SELECTED,
                len(_cached_coins),
            )

    _last_refresh_at = datetime.now(timezone.utc)

    if _cached_coins:
        top_preview = [
            f"{row['symbol'].replace('_USDT', '')}:{row.get('score', 0):.2f}"
            for row in _cached_scores[:10]
        ]
        logger.info(
            "[COIN-RANK] selected=%s/%s coins | top10=%s",
            len(_cached_coins),
            TOP_N_COINS,
            top_preview,
        )
    else:
        logger.warning("[COIN-RANK] no coins selected — keeping previous cache")

    return list(_cached_coins)


def get_cached_coins() -> list[str]:
    if not _cached_coins:
        return refresh_coin_list()

    return list(_cached_coins)


def get_cached_coin_scores() -> list[dict]:
    return list(_cached_scores)


def get_last_refresh_at() -> datetime | None:
    return _last_refresh_at


def get_cached_valid_futures() -> set[str]:
    return set(_cached_valid_futures)
