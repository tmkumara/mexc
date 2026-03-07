import logging
from mexc_client import get_all_contracts, get_tickers
from config import EXCLUDE_COINS, TOP_N_COINS

logger = logging.getLogger(__name__)

_cached_coins: list[str] = []


def get_zero_fee_coins() -> list[str]:
    """
    Fetch all MEXC futures contracts, filter for zero-fee USDT-margined
    perpetuals (excluding BTC/ETH/SOL), and return top N by 24h volume.
    """
    global _cached_coins

    try:
        contracts = get_all_contracts()
        tickers = get_tickers()

        zero_fee = []
        for c in contracts:
            symbol = c.get("symbol", "")
            if not symbol.endswith("_USDT"):
                continue
            if symbol in EXCLUDE_COINS:
                continue
            # Check for zero maker and taker fees
            maker_fee = float(c.get("makerFee", 1))
            taker_fee = float(c.get("takerFee", 1))
            if maker_fee != 0.0 or taker_fee != 0.0:
                continue
            # Must be active (state 0 = listed)
            if c.get("state", 1) != 0:
                continue
            zero_fee.append(symbol)

        # Sort by 24h volume descending
        def vol(sym):
            t = tickers.get(sym, {})
            try:
                return float(t.get("volume24", 0) or 0)
            except (ValueError, TypeError):
                return 0.0

        zero_fee.sort(key=vol, reverse=True)
        top = zero_fee[:TOP_N_COINS]

        if top:
            _cached_coins = top
            logger.info(f"Zero-fee coins refreshed: {top}")
        else:
            logger.warning("No zero-fee coins found, keeping previous list")

        return _cached_coins

    except Exception as e:
        logger.error(f"Error scanning coins: {e}")
        return _cached_coins


def get_cached_coins() -> list[str]:
    if not _cached_coins:
        return get_zero_fee_coins()
    return _cached_coins
