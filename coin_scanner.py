"""
Phase 2 Smart Coin Scanner.

Purpose:
    - Build the coin pool using liquidity + recent movement quality.
    - Avoid very flat coins.
    - Avoid coins already over-pumped / over-dumped.
    - Reduce API waste by scanning better-ranked symbols first.

Sources:
    1. CoinGlass OI candidates when COINGLASS_API_KEY exists.
    2. MEXC 24h volume fallback.
    3. MEXC 5m candles for Phase 2 activity ranking.
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
)

logger = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"

_cached_coins: list[str] = []
_cached_scores: list[dict] = []
_last_refresh_at: datetime | None = None


# ── safe conversion helpers ───────────────────────────────────────

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def _ticker_volume_usd(ticker: dict) -> float:
    """
    MEXC ticker fields can differ. Try common quote-volume fields first.
    """
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
        v = ticker.get(key)
        vol = _to_float(v, 0.0)
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


# ── candidate fetchers ────────────────────────────────────────────

def _fetch_coinglass_candidates() -> list[tuple[str, float]]:
    """
    Returns:
        [(symbol, oi_score_raw), ...]
    """
    if not COINGLASS_API_KEY:
        return []

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
                val = _to_float(item.get(key), 0.0)
                if val > 0:
                    return val
            return 0.0

        items.sort(key=_oi, reverse=True)

        max_candidates = min(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER, COIN_RANK_MAX_CANDIDATES)
        rows: list[tuple[str, float]] = []

        for item in items:
            coin = (item.get("symbol") or item.get("baseSymbol") or "").upper().strip()
            if not coin:
                continue

            symbol = f"{coin}_USDT"
            if symbol in EXCLUDE_COINS:
                continue

            rows.append((symbol, _oi(item)))

            if len(rows) >= max_candidates:
                break

        logger.info(f"[COIN-RANK] CoinGlass candidates={len(rows)}")
        return rows

    except Exception as e:
        logger.error(f"CoinGlass fetch error: {e}")
        return []


def _fetch_mexc_volume_candidates(tickers: dict[str, dict]) -> list[tuple[str, float]]:
    max_candidates = min(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER, COIN_RANK_MAX_CANDIDATES)
    rows: list[tuple[str, float]] = []

    for symbol, ticker in tickers.items():
        if not symbol.endswith("_USDT") or symbol in EXCLUDE_COINS:
            continue

        last_price = _ticker_last_price(ticker)
        if last_price < COIN_RANK_MIN_LAST_PRICE:
            continue

        vol_usd = _ticker_volume_usd(ticker)
        if vol_usd < COIN_POOL_MIN_VOLUME_USD:
            continue

        rows.append((symbol, vol_usd))

    rows.sort(key=lambda x: x[1], reverse=True)
    rows = rows[:max_candidates]

    logger.info(
        f"[COIN-RANK] MEXC volume candidates={len(rows)} "
        f"(min volume ${COIN_POOL_MIN_VOLUME_USD / 1_000_000:.0f}M)"
    )
    return rows


def _validate_active_contracts(symbols: list[str]) -> list[str]:
    try:
        contracts = get_all_contracts()
        active = {c["symbol"] for c in contracts if c.get("state") in (0, None)}
        validated = [symbol for symbol in symbols if symbol in active]

        removed = len(symbols) - len(validated)
        if removed:
            logger.info(f"[COIN-RANK] filtered inactive contracts={removed}")

        return validated

    except Exception as e:
        logger.warning(f"[COIN-RANK] contract validation skipped: {e}")
        return symbols


# ── ranking model ─────────────────────────────────────────────────

def _rank_one_symbol(symbol: str, ticker: dict, raw_priority: float, max_raw_priority: float) -> dict | None:
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

        # Volume score uses log scale so giant coins do not dominate everything.
        volume_score = min(math.log10(max(volume_usd, 1.0)) / 10.0, 1.0)

        # Volatility score prefers active-but-not-insane movement.
        volatility_score = _normalize_score(
            avg_candle_range_pct,
            COIN_RANK_MIN_RANGE_PCT / 2.0,
            1.25,
        )

        # Trend activity score rewards directional movement, but overextension gets penalized below.
        trend_score = _normalize_score(abs_move_pct, 0.30, 4.50)

        # Liquidity / OI priority score. If MEXC fallback is used, raw priority is volume.
        priority_score = raw_priority / max_raw_priority if max_raw_priority > 0 else 0.0
        priority_score = max(0.0, min(1.0, priority_score))

        score = (
            volume_score * COIN_RANK_VOLUME_WEIGHT
            + volatility_score * COIN_RANK_VOLATILITY_WEIGHT
            + trend_score * COIN_RANK_TREND_WEIGHT
            + priority_score * COIN_RANK_LIQUIDITY_WEIGHT
        )

        penalty = 0.0

        # Avoid symbols that already made a large one-way move over the ranking window.
        if abs_move_pct > COIN_RANK_MAX_ABS_MOVE_PCT:
            penalty += COIN_RANK_OVEREXTENSION_PENALTY

        # Very low candle activity usually creates fake/noisy signals.
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
        logger.debug(f"[COIN-RANK] {symbol} ranking failed: {e}")
        return None


def _smart_rank_candidates(candidates: list[tuple[str, float]], tickers: dict[str, dict]) -> list[dict]:
    if not candidates:
        return []

    # Deduplicate while preserving highest raw priority.
    priority_by_symbol: dict[str, float] = {}
    for symbol, raw_priority in candidates:
        if symbol in EXCLUDE_COINS:
            continue
        priority_by_symbol[symbol] = max(priority_by_symbol.get(symbol, 0.0), raw_priority)

    symbols = list(priority_by_symbol.keys())
    symbols = _validate_active_contracts(symbols)

    max_candidates = min(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER, COIN_RANK_MAX_CANDIDATES)
    symbols = symbols[:max_candidates]

    if not symbols:
        return []

    max_raw_priority = max((priority_by_symbol.get(symbol, 0.0) for symbol in symbols), default=1.0)

    ranked: list[dict] = []

    logger.info(
        f"[COIN-RANK] ranking {len(symbols)} candidates "
        f"using {COIN_RANK_KLINE_COUNT}x {COIN_RANK_TIMEFRAME} candles"
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

    try:
        tickers = get_tickers()
    except Exception as e:
        logger.error(f"[COIN-RANK] ticker fetch failed: {e}")
        tickers = {}

    raw_candidates = _fetch_coinglass_candidates()
    if not raw_candidates:
        logger.info("[COIN-RANK] no CoinGlass data — using MEXC volume candidates")
        raw_candidates = _fetch_mexc_volume_candidates(tickers)

    if not raw_candidates:
        logger.warning("[COIN-RANK] no candidates fetched — keeping previous cache")
        return _cached_coins

    if ENABLE_SMART_COIN_RANKING and tickers:
        ranked = _smart_rank_candidates(raw_candidates, tickers)
        if ranked:
            selected = ranked[:TOP_N_COINS]
            _cached_scores = selected
            _cached_coins = [row["symbol"] for row in selected]
        else:
            logger.warning("[COIN-RANK] smart ranking returned empty — using raw fallback")
            symbols = _validate_active_contracts([symbol for symbol, _ in raw_candidates])
            _cached_scores = [{"symbol": s, "score": 0.0} for s in symbols[:TOP_N_COINS]]
            _cached_coins = symbols[:TOP_N_COINS]
    else:
        symbols = _validate_active_contracts([symbol for symbol, _ in raw_candidates])
        _cached_scores = [{"symbol": s, "score": 0.0} for s in symbols[:TOP_N_COINS]]
        _cached_coins = symbols[:TOP_N_COINS]

    _last_refresh_at = datetime.now(timezone.utc)

    if _cached_coins:
        top_preview = [
            f"{row['symbol'].replace('_USDT', '')}:{row.get('score', 0)}"
            for row in _cached_scores[:10]
        ]
        logger.info(f"[COIN-RANK] selected {len(_cached_coins)} coins | top={top_preview}")
    else:
        logger.warning("[COIN-RANK] no coins selected — keeping previous cache")

    return _cached_coins


def get_cached_coins() -> list[str]:
    if not _cached_coins:
        return refresh_coin_list()
    return _cached_coins


def get_cached_coin_scores() -> list[dict]:
    return list(_cached_scores)


def get_last_refresh_at() -> datetime | None:
    return _last_refresh_at