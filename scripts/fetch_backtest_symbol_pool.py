"""One-off helper: print the current live-bot-style coin pool as a
space-separated symbol list, for feeding into backtest scripts' --symbols
argument. Reuses coin_scanner.refresh_coin_list() read-only -- does not
write to any cache file the live bot depends on."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coin_scanner import refresh_coin_list

if __name__ == "__main__":
    symbols = refresh_coin_list()
    print(" ".join(symbols))
