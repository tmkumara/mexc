"""One-off helper: print a coin pool as a space-separated symbol list, for
feeding into backtest scripts' --symbols argument. Reuses coin_scanner
functions read-only -- does not write to any cache file the live bot
depends on.

Default mode: the live-bot-style ranked pool (coin_scanner.refresh_coin_list(),
i.e. TOP_N_COINS / volume-ranked, same as the running bot uses).

--max mode: the full universe of valid active USDT-quoted crypto futures
contracts (coin_scanner._fetch_valid_futures_symbols(), before any
volume-ranking/TOP_N truncation is applied) -- for backtest research that
wants maximum symbol breadth rather than the live bot's curated subset.
Still applies the same crypto-only/active-contract/EXCLUDE_COINS filtering
coin_scanner already uses, just skips the ranking/limit step.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coin_scanner import refresh_coin_list, _fetch_valid_futures_symbols
from mexc_client import get_tickers

if __name__ == "__main__":
    if "--max" in sys.argv:
        tickers = get_tickers()
        symbols = sorted(_fetch_valid_futures_symbols(tickers))
    else:
        symbols = refresh_coin_list()
    print(" ".join(symbols))
