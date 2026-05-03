"""
RSI-based coin selection: fetches 4h RSI for all zero-fee USDT perpetuals,
returns the top N most oversold (LONG) and top N most overbought (SHORT).
Refreshed every COIN_REFRESH_HOURS hours.
"""

import logging
import pandas_ta as ta

from mexc_client import get_all_contracts, get_tickers, get_klines
from config import (
    EXCLUDE_COINS,
    RSI_HTF, RSI_PERIOD_HTF,
    RSI_OVERSOLD_MAX, RSI_OVERBOUGHT_MIN, RSI_TOP_N_EACH,
)

logger = logging.getLogger(__name__)

# Cache: {symbol: "LONG" | "SHORT"}
_cached_coins: dict[str, str] = {}


def _get_fee(contract: dict, *fields: str) -> float:
    for f in fields:
        v = contract.get(f)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 1.0


def _get_all_zero_fee_symbols() -> list[str]:
    """Return all zero-fee USDT perpetual symbols (full list, no volume cap)."""
    try:
        contracts = get_all_contracts()
        if not contracts:
            return []

        zero_fee = []
        usdt_active = []
        for c in contracts:
            symbol = c.get("symbol", "")
            if not symbol.endswith("_USDT"):
                continue
            if symbol in EXCLUDE_COINS:
                continue
            state = c.get("state")
            if state is not None and state != 0:
                continue
            usdt_active.append(symbol)
            maker = _get_fee(c, "makerFeeRate", "makerFee")
            taker = _get_fee(c, "takerFeeRate", "takerFee")
            if maker == 0.0 and taker == 0.0:
                zero_fee.append(symbol)

        logger.info(
            f"Contracts: {len(contracts)} total | "
            f"{len(usdt_active)} active USDT | "
            f"{len(zero_fee)} zero-fee"
        )
        return zero_fee if zero_fee else usdt_active

    except Exception as e:
        logger.error(f"Error fetching contracts: {e}", exc_info=True)
        return []


def get_rsi_ranked_coins() -> dict[str, str]:
    """
    Fetch 4h RSI for all zero-fee USDT coins, return top RSI_TOP_N_EACH
    most oversold ({symbol: "LONG"}) and top RSI_TOP_N_EACH most overbought
    ({symbol: "SHORT"}).  Falls back to previous cache on error.
    """
    global _cached_coins

    symbols = _get_all_zero_fee_symbols()
    if not symbols:
        logger.warning("No symbols found, keeping cached list")
        return _cached_coins

    # Need at least RSI_PERIOD_HTF + 1 candles; fetch 50 for reliability
    kline_count = RSI_PERIOD_HTF + 36

    rsi_values: dict[str, float] = {}
    for symbol in symbols:
        try:
            df = get_klines(symbol, RSI_HTF, count=kline_count)
            if df.empty or len(df) < RSI_PERIOD_HTF + 1:
                continue
            rsi_series = ta.rsi(df["close"], length=RSI_PERIOD_HTF)
            rsi_val = rsi_series.iloc[-2]   # last completed candle
            if rsi_val is not None and not (rsi_val != rsi_val):  # not NaN
                rsi_values[symbol] = float(rsi_val)
        except Exception as e:
            logger.debug(f"RSI fetch failed for {symbol}: {e}")
            continue

    if not rsi_values:
        logger.warning("Could not compute RSI for any symbol, keeping cached list")
        return _cached_coins

    # Sort ascending for LONG (oversold = low RSI), descending for SHORT
    sorted_asc  = sorted(rsi_values.items(), key=lambda x: x[1])
    sorted_desc = sorted(rsi_values.items(), key=lambda x: x[1], reverse=True)

    long_candidates  = [(s, r) for s, r in sorted_asc  if r < RSI_OVERSOLD_MAX]
    short_candidates = [(s, r) for s, r in sorted_desc if r > RSI_OVERBOUGHT_MIN]

    selected: dict[str, str] = {}
    for symbol, rsi in long_candidates[:RSI_TOP_N_EACH]:
        selected[symbol] = "LONG"
        logger.info(f"  RSI LONG  candidate: {symbol} RSI={rsi:.1f}")

    for symbol, rsi in short_candidates[:RSI_TOP_N_EACH]:
        if symbol not in selected:   # avoid direction conflict
            selected[symbol] = "SHORT"
            logger.info(f"  RSI SHORT candidate: {symbol} RSI={rsi:.1f}")

    if not selected:
        logger.warning(
            f"No coins meet RSI thresholds "
            f"(oversold<{RSI_OVERSOLD_MAX} or overbought>{RSI_OVERBOUGHT_MIN}). "
            f"Keeping previous cache."
        )
        return _cached_coins

    _cached_coins = selected
    logger.info(f"RSI-ranked coins updated ({len(selected)}): {selected}")
    return _cached_coins


def get_cached_coins() -> dict[str, str]:
    """Return cached RSI-ranked coin dict, fetching fresh if empty."""
    if not _cached_coins:
        return get_rsi_ranked_coins()
    return _cached_coins
