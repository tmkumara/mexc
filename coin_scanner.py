"""
Fetches top N zero-fee USDT-margined perpetual contracts from MEXC,
sorted by 24h volume. Refreshed every COIN_REFRESH_HOURS hours.
"""

import logging
from mexc_client import get_all_contracts, get_tickers
from config import EXCLUDE_COINS, TOP_N_COINS

logger = logging.getLogger(__name__)

_cached_coins: list[str] = []


def _get_fee(contract: dict, *fields: str) -> float:
    for f in fields:
        v = contract.get(f)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 1.0


def get_zero_fee_coins() -> list[str]:
    """
    Return top N zero-fee USDT perpetuals by 24h volume,
    excluding coins in EXCLUDE_COINS.
    Falls back to top-volume USDT coins if no zero-fee contracts found.
    """
    global _cached_coins

    try:
        contracts = get_all_contracts()
        tickers   = get_tickers()

        if not contracts:
            logger.warning("No contracts returned from MEXC API")
            return _cached_coins

        usdt_active = []
        zero_fee    = []

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

        def vol(sym: str) -> float:
            t = tickers.get(sym, {})
            try:
                return float(t.get("volume24", 0) or 0)
            except (ValueError, TypeError):
                return 0.0

        pool = zero_fee if zero_fee else usdt_active
        if not pool:
            logger.warning("No coins found, keeping previous list")
            return _cached_coins

        pool.sort(key=vol, reverse=True)
        _cached_coins = pool[:TOP_N_COINS]

        if not zero_fee:
            logger.warning(f"No zero-fee coins found — using top volume instead: {_cached_coins}")
        else:
            logger.info(f"Zero-fee coins refreshed ({len(_cached_coins)}): {_cached_coins}")

        return _cached_coins

    except Exception as e:
        logger.error(f"Error scanning coins: {e}", exc_info=True)
        return _cached_coins


def get_cached_coins() -> list[str]:
    if not _cached_coins:
        return get_zero_fee_coins()
    return _cached_coins
