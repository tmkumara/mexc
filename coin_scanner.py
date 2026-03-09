import logging
from mexc_client import get_all_contracts, get_tickers
from config import EXCLUDE_COINS, TOP_N_COINS

logger = logging.getLogger(__name__)

_cached_coins: list[str] = []


def _get_fee(contract: dict, *field_names: str) -> float:
    """Try multiple fee field name variants; return 1.0 (non-zero) if none found."""
    for name in field_names:
        v = contract.get(name)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 1.0


def get_zero_fee_coins() -> list[str]:
    """
    Fetch all MEXC futures contracts, filter for zero-fee USDT-margined
    perpetuals (excluding BTC/ETH/SOL), and return top N by 24h volume.
    Falls back to top-volume USDT coins if no zero-fee contracts exist.
    """
    global _cached_coins

    try:
        contracts = get_all_contracts()
        tickers = get_tickers()

        if not contracts:
            logger.warning("No contracts returned from MEXC API")
            return _cached_coins

        # Log first contract keys once to aid debugging
        logger.debug(f"Sample contract keys: {list(contracts[0].keys())}")

        usdt_active = []
        zero_fee = []
        for c in contracts:
            symbol = c.get("symbol", "")
            if not symbol.endswith("_USDT"):
                continue
            if symbol in EXCLUDE_COINS:
                continue
            # Accept both state==0 (listed) and missing state field
            state = c.get("state")
            if state is not None and state != 0:
                continue
            usdt_active.append(symbol)
            # Try both naming conventions used across MEXC API versions
            maker_fee = _get_fee(c, "makerFeeRate", "makerFee")
            taker_fee = _get_fee(c, "takerFeeRate", "takerFee")
            if maker_fee == 0.0 and taker_fee == 0.0:
                zero_fee.append(symbol)

        logger.info(
            f"Contracts: {len(contracts)} total, "
            f"{len(usdt_active)} active USDT, "
            f"{len(zero_fee)} zero-fee"
        )

        def vol(sym):
            t = tickers.get(sym, {})
            try:
                return float(t.get("volume24", 0) or 0)
            except (ValueError, TypeError):
                return 0.0

        if zero_fee:
            zero_fee.sort(key=vol, reverse=True)
            top = zero_fee[:TOP_N_COINS]
            _cached_coins = top
            logger.info(f"Zero-fee coins refreshed: {top}")
        elif usdt_active:
            # MEXC has no zero-fee contracts; fall back to top-volume coins
            usdt_active.sort(key=vol, reverse=True)
            top = usdt_active[:TOP_N_COINS]
            _cached_coins = top
            logger.warning(
                f"No zero-fee coins on MEXC — tracking top {TOP_N_COINS} "
                f"by volume instead: {top}"
            )
        else:
            logger.warning("No active USDT contracts found, keeping previous list")

        return _cached_coins

    except Exception as e:
        logger.error(f"Error scanning coins: {e}", exc_info=True)
        return _cached_coins


def get_cached_coins() -> list[str]:
    if not _cached_coins:
        return get_zero_fee_coins()
    return _cached_coins
