import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ───────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))

# ── Telegram ───────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass optional API ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ──────────────────────────────────────────────────────
QUOTE_CURRENCY: str       = os.getenv("QUOTE_CURRENCY", "USDT")
CRYPTO_FUTURES_ONLY: bool = os.getenv("CRYPTO_FUTURES_ONLY", "true").lower() == "true"

EXCLUDE_COINS: set[str] = {
    coin.strip().upper()
    for coin in os.getenv("EXCLUDE_COINS", "BTC_USDT,ETH_USDT,SOL_USDT,XAUT_USDT").split(",")
    if coin.strip()
}

TOP_N_COINS: int               = int(os.getenv("TOP_N_COINS", "80"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_POOL_MIN_SELECTED: int    = int(os.getenv("COIN_POOL_MIN_SELECTED", "20"))
COIN_REFRESH_HOURS: int        = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking ─────────────────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool        = os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
COIN_RANK_CANDIDATE_MULTIPLIER: int    = int(os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4"))
COIN_RANK_MAX_CANDIDATES: int          = int(os.getenv("COIN_RANK_MAX_CANDIDATES", str(TOP_N_COINS * 4)))
COIN_RANK_TIMEFRAME: str               = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int             = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int                 = int(os.getenv("COIN_RANK_WORKERS", "4"))
COIN_RANK_MIN_LAST_PRICE: float        = float(os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.001"))
COIN_RANK_MIN_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20"))
COIN_RANK_MAX_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MAX_RANGE_PCT", "60.0"))
COIN_RANK_MAX_ABS_MOVE_PCT: float      = float(os.getenv("COIN_RANK_MAX_ABS_MOVE_PCT", "8.0"))
COIN_RANK_VOLUME_WEIGHT: float         = float(os.getenv("COIN_RANK_VOLUME_WEIGHT",     "0.35"))
COIN_RANK_VOLATILITY_WEIGHT: float     = float(os.getenv("COIN_RANK_VOLATILITY_WEIGHT", "0.30"))
COIN_RANK_TREND_WEIGHT: float          = float(os.getenv("COIN_RANK_TREND_WEIGHT",      "0.20"))
COIN_RANK_LIQUIDITY_WEIGHT: float      = float(os.getenv("COIN_RANK_LIQUIDITY_WEIGHT",  "0.15"))
COIN_RANK_OVEREXTENSION_PENALTY: float = float(os.getenv("COIN_RANK_OVEREXTENSION_PENALTY", "0.25"))
COIN_RANK_LOW_ACTIVITY_PENALTY: float  = float(os.getenv("COIN_RANK_LOW_ACTIVITY_PENALTY",  "0.20"))

SMART_RANKING_LOOKBACK_MINUTES: int = int(os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240"))
SMART_RANKING_MIN_VOLUME_USD: float  = float(os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
SMART_RANKING_TOP_N: int             = int(os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS)))
MIN_24H_VOLUME_USD: float            = float(os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
MAX_SPREAD_PCT: float                = float(os.getenv("MAX_SPREAD_PCT", "0.35"))
MIN_PRICE_CHANGE_24H_PCT: float      = float(os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0"))

# ── Strategy: Simple Supertrend Pullback v1 ─────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Simple Supertrend Pullback v1",
)

TREND_TF: str = os.getenv("TREND_TF", "15m")
ENTRY_TF: str = os.getenv("ENTRY_TF", "5m")

TREND_KLINE_COUNT: int = int(os.getenv("TREND_KLINE_COUNT", "260"))
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "120"))

TREND_EMA_PERIOD: int = int(os.getenv("TREND_EMA_PERIOD", "200"))
ENTRY_EMA_PERIOD: int = int(os.getenv("ENTRY_EMA_PERIOD", "20"))

RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN: float = float(os.getenv("RSI_LONG_MIN", "50"))
RSI_LONG_MAX: float = float(os.getenv("RSI_LONG_MAX", "68"))
RSI_SHORT_MIN: float = float(os.getenv("RSI_SHORT_MIN", "32"))
RSI_SHORT_MAX: float = float(os.getenv("RSI_SHORT_MAX", "50"))

ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

TREND_SUPERTREND_ATR_PERIOD: int = int(os.getenv("TREND_SUPERTREND_ATR_PERIOD", "10"))
TREND_SUPERTREND_MULTIPLIER: float = float(os.getenv("TREND_SUPERTREND_MULTIPLIER", "3.0"))

ENTRY_SUPERTREND_ATR_PERIOD: int = int(os.getenv("ENTRY_SUPERTREND_ATR_PERIOD", "10"))
ENTRY_SUPERTREND_MULTIPLIER: float = float(os.getenv("ENTRY_SUPERTREND_MULTIPLIER", "2.0"))

VOLUME_MA_PERIOD: int = int(os.getenv("VOLUME_MA_PERIOD", "20"))
MIN_VOLUME_MULTIPLIER: float = float(os.getenv("MIN_VOLUME_MULTIPLIER", "1.2"))

PULLBACK_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_LOOKBACK_BARS", "3"))

MAX_EMA_DISTANCE_PCT: float = float(os.getenv("MAX_EMA_DISTANCE_PCT", "0.003"))

MAX_CONFIRMATION_CANDLE_ATR: float = float(os.getenv("MAX_CONFIRMATION_CANDLE_ATR", "1.8"))

SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("SL_ATR_BUFFER_MULTIPLIER", "0.10"))

TARGET_ROI_PCT: float = float(os.getenv("TARGET_ROI_PCT", "15.0"))
MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "10.0"))

LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))

TP_PRICE_PCT: float = TARGET_ROI_PCT / 100.0 / LEVERAGE
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE

MIN_RR: float = float(os.getenv("MIN_RR", "1.5"))

SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "3"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES", "60"))

MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "2"))

MAX_ACTIVE_LONG_SIGNALS: int = int(os.getenv("MAX_ACTIVE_LONG_SIGNALS", "1"))
MAX_ACTIVE_SHORT_SIGNALS: int = int(os.getenv("MAX_ACTIVE_SHORT_SIGNALS", "1"))

SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "1"))
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "240"))

SIGNAL_EXPIRE_HOURS: int = int(os.getenv("SIGNAL_EXPIRE_HOURS", "6"))

SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# ── BTC market safety filter ─────────────────────────────────────────
ENABLE_BTC_FILTER: bool = os.getenv("ENABLE_BTC_FILTER", "true").lower() == "true"
BTC_FILTER_SYMBOL: str = os.getenv("BTC_FILTER_SYMBOL", "BTC_USDT")
BTC_FILTER_TF: str = os.getenv("BTC_FILTER_TF", "15m")
BTC_MAX_OPPOSING_MOVE_PCT: float = float(os.getenv("BTC_MAX_OPPOSING_MOVE_PCT", "0.20"))
BTC_MAX_SINGLE_CANDLE_MOVE_PCT: float = float(os.getenv("BTC_MAX_SINGLE_CANDLE_MOVE_PCT", "0.60"))
BTC_MAX_THREE_CANDLE_MOVE_PCT: float = float(os.getenv("BTC_MAX_THREE_CANDLE_MOVE_PCT", "1.20"))

# ── Fee / slippage estimates (backtest only) ─────────────────────────
ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.01"))

# ── Dry run ────────────────────────────────────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
DRY_RUN_SAVE_SIGNALS: bool = os.getenv("DRY_RUN_SAVE_SIGNALS", "false").lower() == "true"

# ── Scheduler ──────────────────────────────────────────────────────
OUTCOME_CHECK_MINUTES: int = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
COIN_REFRESH_CRON_HOURS: str = os.getenv("COIN_REFRESH_CRON_HOURS", f"*/{COIN_REFRESH_HOURS}")

SCHEDULER_MISFIRE_GRACE_SECONDS: int = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "30"))
SCHEDULER_MAX_INSTANCES: int = int(os.getenv("SCHEDULER_MAX_INSTANCES", "1"))

# ── Log ────────────────────────────────────────────────────────────
LOG_FILE: str = os.getenv("LOG_FILE", "mexc_bot.log")
ENABLE_LOG_BACKUP_ON_START: bool = os.getenv("ENABLE_LOG_BACKUP_ON_START", "true").lower() == "true"
LOG_BACKUP_DIR: str = os.getenv("LOG_BACKUP_DIR", "logs/archive")

# ── MEXC REST API ──────────────────────────────────────────────────
MEXC_BASE_URL: str = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")

# ── Database ───────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "signals.db")

# ── Candle minutes (derived from ENTRY_TF) ──────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(ENTRY_TF, 5))))

# ── MEXC interval map ──────────────────────────────────────────────
MEXC_INTERVAL_MAP: dict[str, str] = {
    "1m":  "Min1",
    "3m":  "Min3",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "8h":  "Hour8",
    "1d":  "Day1",
}
