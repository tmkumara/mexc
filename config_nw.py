import os
from dotenv import load_dotenv

load_dotenv()

# ── Strategy: Nadaraya-Watson Kernel MTF (backtest research only) ────────
STRATEGY_NAME: str = os.getenv("NW_STRATEGY_NAME", "Nadaraya-Watson Kernel MTF (research)")

NW_TREND_TF: str = os.getenv("NW_TREND_TF", "4h")
NW_ENTRY_TF: str = os.getenv("NW_ENTRY_TF", "1h")

NW_TREND_KLINE_COUNT: int = int(os.getenv("NW_TREND_KLINE_COUNT", "260"))
NW_ENTRY_KLINE_COUNT: int = int(os.getenv("NW_ENTRY_KLINE_COUNT", "500"))

NW_TREND_EMA_PERIOD: int = int(os.getenv("NW_TREND_EMA_PERIOD", "200"))

# Nadaraya-Watson rational quadratic kernel params (Pine Script defaults).
NW_LOOKBACK_WINDOW: float = float(os.getenv("NW_LOOKBACK_WINDOW", "8.0"))       # Pine "h"
NW_RELATIVE_WEIGHTING: float = float(os.getenv("NW_RELATIVE_WEIGHTING", "8.0"))  # Pine "r"
# Bars summed per kernel estimate -- a numerical truncation bound, not a
# Pine parameter. Kernel weight decays fast with distance under the
# default h/r, so bars beyond this contribute negligibly (see
# tests/test_nw_kernel.py's truncation-accuracy test).
NW_KERNEL_SUM_WINDOW: int = int(os.getenv("NW_KERNEL_SUM_WINDOW", "300"))
# How many of the most recent candles may contain the slope-flip bar --
# a genuine kernel turn is a single-bar event, and requiring it to be the
# exact latest candle was found empirically to reject ~91% of all
# evaluations (diagnostic run, 60-day/47-symbol pool, 2026-07-16). Widened
# so a recent-but-not-instantaneous flip still qualifies, provided the
# latest bar's slope hasn't already reversed (see NW_KERNEL_SUM_WINDOW's
# sibling check in _detect_nw_signal's "still holding" condition).
NW_CONFIRMATION_LOOKBACK_BARS: int = int(os.getenv("NW_CONFIRMATION_LOOKBACK_BARS", "5"))

# Structural swing lookup, used only to anchor the SL (see Task 5).
NW_SWING_FRACTAL_BARS: int = int(os.getenv("NW_SWING_FRACTAL_BARS", "5"))
NW_SWING_LOOKBACK_BARS: int = int(os.getenv("NW_SWING_LOOKBACK_BARS", "20"))

NW_RSI_PERIOD: int = int(os.getenv("NW_RSI_PERIOD", "14"))
NW_RSI_LONG_MIN: float = float(os.getenv("NW_RSI_LONG_MIN", "45"))
NW_RSI_LONG_MAX: float = float(os.getenv("NW_RSI_LONG_MAX", "75"))
NW_RSI_SHORT_MIN: float = float(os.getenv("NW_RSI_SHORT_MIN", "25"))
NW_RSI_SHORT_MAX: float = float(os.getenv("NW_RSI_SHORT_MAX", "55"))

NW_VOLUME_MA_PERIOD: int = int(os.getenv("NW_VOLUME_MA_PERIOD", "20"))
NW_MIN_VOLUME_MULTIPLIER: float = float(os.getenv("NW_MIN_VOLUME_MULTIPLIER", "1.0"))

NW_ATR_PERIOD: int = int(os.getenv("NW_ATR_PERIOD", "14"))
# Buffer beyond the structural swing level used to place the SL.
NW_SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("NW_SL_ATR_BUFFER_MULTIPLIER", "0.5"))

LEVERAGE: int = int(os.getenv("NW_LEVERAGE", "20"))
TARGET_ROI_PCT: float = float(os.getenv("NW_TARGET_ROI_PCT", "15.0"))
TP_PRICE_PCT: float = TARGET_ROI_PCT / 100.0 / LEVERAGE
MAX_SL_ROI_PCT: float = float(os.getenv("NW_MAX_SL_ROI_PCT", "20.0"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE
MIN_RR: float = float(os.getenv("NW_MIN_RR", "1.5"))

# ── BTC market safety filter (4H variant) ─────────────────────────────
NW_ENABLE_BTC_FILTER: bool = os.getenv("NW_ENABLE_BTC_FILTER", "true").lower() == "true"
NW_BTC_FILTER_SYMBOL: str = os.getenv("NW_BTC_FILTER_SYMBOL", "BTC_USDT")
NW_BTC_FILTER_TF: str = os.getenv("NW_BTC_FILTER_TF", "4h")
NW_BTC_MAX_OPPOSING_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_OPPOSING_MOVE_PCT", "0.20"))
NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT", "0.60"))
NW_BTC_MAX_THREE_CANDLE_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_THREE_CANDLE_MOVE_PCT", "1.20"))

# ── Backtest-only: outcome expiry + fee/slippage estimates ────────────
NW_SIGNAL_EXPIRE_HOURS: float = float(os.getenv("NW_SIGNAL_EXPIRE_HOURS", "48"))

ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("NW_ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("NW_ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("NW_ESTIMATED_SLIPPAGE_PCT", "0.01"))
