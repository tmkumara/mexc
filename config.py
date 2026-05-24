import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ──────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))   # Sri Lanka Time (UTC+5:30)

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass optional API ────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}

TOP_N_COINS: int = int(os.getenv("TOP_N_COINS", "80"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_REFRESH_HOURS: int = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Backward compatibility for existing coin_scanner.py ───────────
# Keep these because coin_scanner.py imports them directly.
ENABLE_SMART_COIN_RANKING: bool = (
    os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
)

COIN_RANK_CANDIDATE_MULTIPLIER: int = int(
    os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4")
)

SMART_RANKING_LOOKBACK_MINUTES: int = int(
    os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240")
)

SMART_RANKING_MIN_VOLUME_USD: float = float(
    os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD))
)

SMART_RANKING_TOP_N: int = int(
    os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS))
)

# Optional compatibility names used by older scanner versions.
MIN_24H_VOLUME_USD: float = float(
    os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD))
)

MAX_SPREAD_PCT: float = float(
    os.getenv("MAX_SPREAD_PCT", "0.35")
)

MIN_PRICE_CHANGE_24H_PCT: float = float(
    os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0")
)

# ── Fresh Trend Meter + Stoch MTM Strategy ────────────────────────
# Use "1h" for more signals, "4h" for fewer but more stable signals.
STRATEGY_TF: str = os.getenv("STRATEGY_TF", "1h")

TREND_TF: str = STRATEGY_TF
ENTRY_TF: str = STRATEGY_TF

STRATEGY_KLINE_COUNT: int = int(os.getenv("STRATEGY_KLINE_COUNT", "260"))

# Trend Meter approximation:
# Line 1 = EMA13/EMA21
# Line 2 = EMA34/EMA55
# Line 3 = close vs EMA200
TREND_EMA_FAST_1: int = int(os.getenv("TREND_EMA_FAST_1", "13"))
TREND_EMA_SLOW_1: int = int(os.getenv("TREND_EMA_SLOW_1", "21"))

TREND_EMA_FAST_2: int = int(os.getenv("TREND_EMA_FAST_2", "34"))
TREND_EMA_SLOW_2: int = int(os.getenv("TREND_EMA_SLOW_2", "55"))

TREND_EMA_FILTER: int = int(os.getenv("TREND_EMA_FILTER", "200"))

# Stoch MTM / SMI settings.
STOCH_MTM_LENGTH: int = int(os.getenv("STOCH_MTM_LENGTH", "10"))
STOCH_MTM_SMOOTH_1: int = int(os.getenv("STOCH_MTM_SMOOTH_1", "3"))
STOCH_MTM_SMOOTH_2: int = int(os.getenv("STOCH_MTM_SMOOTH_2", "10"))
STOCH_MTM_SIGNAL: int = int(os.getenv("STOCH_MTM_SIGNAL", "5"))

STOCH_MTM_UPPER: float = float(os.getenv("STOCH_MTM_UPPER", "40.0"))
STOCH_MTM_LOWER: float = float(os.getenv("STOCH_MTM_LOWER", "-40.0"))

# Entry quality filters.
REQUIRE_CLOSED_CROSS: bool = (
    os.getenv("REQUIRE_CLOSED_CROSS", "true").lower() == "true"
)

MAX_CROSS_LOOKBACK_CANDLES: int = int(
    os.getenv("MAX_CROSS_LOOKBACK_CANDLES", "1")
)

MIN_ABS_MTM_AFTER_CROSS: float = float(
    os.getenv("MIN_ABS_MTM_AFTER_CROSS", "35.0")
)

# ── Choppy market avoidance filters ───────────────────────────────
# ADX filter: avoid weak/sideways trends.
MIN_ADX: float = float(os.getenv("MIN_ADX", "22.0"))
ADX_PERIOD: int = int(os.getenv("ADX_PERIOD", "14"))

# EMA spread filter: avoid compressed/choppy EMA structure.
# Defaults are selected based on strategy timeframe.
_DEFAULT_MIN_EMA_SPREAD = "0.25" if STRATEGY_TF == "1h" else "0.40"
MIN_EMA_SPREAD_PCT: float = float(
    os.getenv("MIN_EMA_SPREAD_PCT", _DEFAULT_MIN_EMA_SPREAD)
)

# ATR volatility filter: avoid very flat markets.
MIN_ATR_PCT: float = float(os.getenv("MIN_ATR_PCT", "0.45"))

# Trigger candle quality filter: avoid doji/wick-only candles.
MIN_TRIGGER_BODY_RATIO: float = float(
    os.getenv("MIN_TRIGGER_BODY_RATIO", "0.35")
)

# Avoid abnormal big candle entries after movement already happened.
MAX_TRIGGER_RANGE_ATR_MULTIPLIER: float = float(
    os.getenv("MAX_TRIGGER_RANGE_ATR_MULTIPLIER", "2.2")
)

# Higher timeframe confirmation.
# For 1h signals, 4h must not be opposite.
# For 4h signals, 1d must not be opposite.
ENABLE_HTF_CONFIRMATION: bool = (
    os.getenv("ENABLE_HTF_CONFIRMATION", "true").lower() == "true"
)

HTF_TF: str = os.getenv(
    "HTF_TF",
    "4h" if STRATEGY_TF == "1h" else "1d"
)

HTF_KLINE_COUNT: int = int(os.getenv("HTF_KLINE_COUNT", "260"))

# ── Risk management ───────────────────────────────────────────────
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

ATR_SL_MULTIPLIER: float = float(
    os.getenv("ATR_SL_MULTIPLIER", "1.25")
)

REWARD_RATIO: float = float(
    os.getenv("REWARD_RATIO", "1.6")
)

MIN_SL_PCT: float = float(
    os.getenv("MIN_SL_PCT", "0.25")
)

MAX_SL_PCT: float = float(
    os.getenv("MAX_SL_PCT", "2.50")
)

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))

# Kept for compatibility with old report/status references.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = int(
    os.getenv(
        "SIGNAL_COOLDOWN_MINUTES",
        "180" if STRATEGY_TF == "1h" else "480",
    )
)

SIGNAL_EXPIRE_HOURS: int = int(
    os.getenv(
        "SIGNAL_EXPIRE_HOURS",
        "10" if STRATEGY_TF == "1h" else "24",
    )
)

MAX_CONCURRENT_SIGNALS: int = int(
    os.getenv("MAX_CONCURRENT_SIGNALS", "10")
)

# Direct signal scan. For 1h, every 5 min is fine because strategy uses only
# completed candles and cooldown prevents duplicates.
SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "*/5")

SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "3"))

# Keep modest to avoid MEXC rate limits.
SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = int(
    os.getenv("OUTCOME_CHECK_MINUTES", "1")
)

CANDLE_MINUTES: int = int(
    os.getenv(
        "CANDLE_MINUTES",
        "60" if STRATEGY_TF == "1h" else "240",
    )
)

# Kept for compatibility with older DB/status references.
SETUP_MONITOR_MINUTES: int = int(
    os.getenv("SETUP_MONITOR_MINUTES", "1")
)

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = os.getenv(
    "MEXC_BASE_URL",
    "https://contract.mexc.com/api/v1",
)

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")