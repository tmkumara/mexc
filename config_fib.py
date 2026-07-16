import os
from dotenv import load_dotenv

load_dotenv()

# ── Strategy: Fibonacci MTF (backtest research only) ─────────────────
STRATEGY_NAME: str = os.getenv("FIB_STRATEGY_NAME", "Fibonacci MTF Pullback (research)")

TREND_TF: str = os.getenv("FIB_TREND_TF", "4h")
ENTRY_TF: str = os.getenv("FIB_ENTRY_TF", "1h")

TREND_KLINE_COUNT: int = int(os.getenv("FIB_TREND_KLINE_COUNT", "260"))
ENTRY_KLINE_COUNT: int = int(os.getenv("FIB_ENTRY_KLINE_COUNT", "150"))

TREND_EMA_PERIOD: int = int(os.getenv("FIB_TREND_EMA_PERIOD", "200"))

SWING_FRACTAL_BARS: int = int(os.getenv("FIB_SWING_FRACTAL_BARS", "5"))
SWING_LOOKBACK_BARS: int = int(os.getenv("FIB_SWING_LOOKBACK_BARS", "60"))
ZONE_LOOKBACK_BARS: int = int(os.getenv("FIB_ZONE_LOOKBACK_BARS", "10"))
ZONE_LOWER: float = float(os.getenv("FIB_ZONE_LOWER", "0.5"))
ZONE_UPPER: float = float(os.getenv("FIB_ZONE_UPPER", "0.618"))
TP_EXTENSION_LEVEL: float = float(os.getenv("FIB_TP_EXTENSION_LEVEL", "1.272"))
SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("FIB_SL_ATR_BUFFER_MULTIPLIER", "0.15"))

RSI_PERIOD: int = int(os.getenv("FIB_RSI_PERIOD", "14"))
RSI_LONG_MIN: float = float(os.getenv("FIB_RSI_LONG_MIN", "50"))
RSI_LONG_MAX: float = float(os.getenv("FIB_RSI_LONG_MAX", "68"))
RSI_SHORT_MIN: float = float(os.getenv("FIB_RSI_SHORT_MIN", "32"))
RSI_SHORT_MAX: float = float(os.getenv("FIB_RSI_SHORT_MAX", "50"))

VOLUME_MA_PERIOD: int = int(os.getenv("FIB_VOLUME_MA_PERIOD", "20"))
MIN_VOLUME_MULTIPLIER: float = float(os.getenv("FIB_MIN_VOLUME_MULTIPLIER", "1.3"))

ATR_PERIOD: int = int(os.getenv("FIB_ATR_PERIOD", "14"))

LEVERAGE: int = int(os.getenv("FIB_LEVERAGE", "20"))
MAX_SL_ROI_PCT: float = float(os.getenv("FIB_MAX_SL_ROI_PCT", "10.0"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE
MIN_RR: float = float(os.getenv("FIB_MIN_RR", "1.5"))

# ── BTC market safety filter (4H variant) ─────────────────────────────
ENABLE_BTC_FILTER: bool = os.getenv("FIB_ENABLE_BTC_FILTER", "true").lower() == "true"
BTC_FILTER_SYMBOL: str = os.getenv("FIB_BTC_FILTER_SYMBOL", "BTC_USDT")
BTC_FILTER_TF: str = os.getenv("FIB_BTC_FILTER_TF", "4h")
BTC_MAX_OPPOSING_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_OPPOSING_MOVE_PCT", "0.20"))
BTC_MAX_SINGLE_CANDLE_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_SINGLE_CANDLE_MOVE_PCT", "0.60"))
BTC_MAX_THREE_CANDLE_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_THREE_CANDLE_MOVE_PCT", "1.20"))

# ── Backtest-only: outcome expiry + fee/slippage estimates ────────────
# 1H-based swings resolve slower than the live bot's 5m scalp strategy,
# so the expiry window is generously longer (48x 1H bars = 48h).
SIGNAL_EXPIRE_HOURS: float = float(os.getenv("FIB_SIGNAL_EXPIRE_HOURS", "48"))

ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("FIB_ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("FIB_ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("FIB_ESTIMATED_SLIPPAGE_PCT", "0.01"))
