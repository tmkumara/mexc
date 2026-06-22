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
QUOTE_CURRENCY: str     = os.getenv("QUOTE_CURRENCY", "USDT")
CRYPTO_FUTURES_ONLY: bool = os.getenv("CRYPTO_FUTURES_ONLY", "true").lower() == "true"

EXCLUDE_COINS: set[str] = {
    coin.strip().upper()
    for coin in os.getenv("EXCLUDE_COINS", "BTC_USDT,ETH_USDT,SOL_USDT,XAUT_USDT").split(",")
    if coin.strip()
}

TOP_N_COINS: int            = int(os.getenv("TOP_N_COINS", "50"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "20000000"))
COIN_REFRESH_HOURS: int     = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking ─────────────────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool        = os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
COIN_RANK_CANDIDATE_MULTIPLIER: int    = int(os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4"))
COIN_RANK_MAX_CANDIDATES: int          = int(os.getenv("COIN_RANK_MAX_CANDIDATES", str(TOP_N_COINS * 4)))
COIN_RANK_TIMEFRAME: str               = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int             = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int                 = int(os.getenv("COIN_RANK_WORKERS", "4"))
COIN_RANK_MIN_LAST_PRICE: float        = float(os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.001"))
COIN_RANK_MIN_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20"))
COIN_RANK_MAX_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MAX_RANGE_PCT", "10.0"))
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

# ── Strategy ───────────────────────────────────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "MTF Trend Pullback + Volume Confirmation + WebSocket Trigger",
)

# Timeframes: 1D macro | 4H main | 1H setup | 15m entry
MACRO_TF: str = os.getenv("MACRO_TF", "1d")
MAIN_TF: str  = os.getenv("MAIN_TF",  "4h")
SETUP_TF: str = os.getenv("SETUP_TF", "1h")
ENTRY_TF: str = os.getenv("ENTRY_TF", "15m")

# Kline counts per timeframe
MACRO_KLINE_COUNT: int = int(os.getenv("MACRO_KLINE_COUNT", "250"))
MAIN_KLINE_COUNT: int  = int(os.getenv("MAIN_KLINE_COUNT",  "250"))
SETUP_KLINE_COUNT: int = int(os.getenv("SETUP_KLINE_COUNT", "100"))
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "100"))

# EMA periods per timeframe
EMA_MACRO: int       = int(os.getenv("EMA_MACRO",       "200"))   # 1D
EMA_MAIN: int        = int(os.getenv("EMA_MAIN",        "200"))   # 4H
EMA_SETUP: int       = int(os.getenv("EMA_SETUP",       "50"))    # 1H
EMA_ENTRY_FAST: int  = int(os.getenv("EMA_ENTRY_FAST",  "20"))    # 15m
EMA_ENTRY_SLOW: int  = int(os.getenv("EMA_ENTRY_SLOW",  "50"))    # 15m

# Volume
VOLUME_PERIOD: int          = int(os.getenv("VOLUME_PERIOD",     "20"))
VOLUME_MULTIPLIER: float    = float(os.getenv("VOLUME_MULTIPLIER", "1.3"))

# ATR
ATR_PERIOD: int             = int(os.getenv("ATR_PERIOD", "14"))

# Support/Resistance lookback (in 15m candles)
SR_LOOKBACK_CANDLES: int    = int(os.getenv("SR_LOOKBACK_CANDLES", "30"))

# Risk / reward
MIN_RR: float               = float(os.getenv("MIN_RR", "2.0"))
MAX_RR: float               = float(os.getenv("MAX_RR", "4.0"))
SL_ATR_MULTIPLIER: float    = float(os.getenv("SL_ATR_MULTIPLIER", "1.2"))

# Minimum score to create an armed setup
MIN_SETUP_SCORE: float      = float(os.getenv("MIN_SETUP_SCORE", "75"))

# Entry zone width around pullback level (±ATR × this multiplier)
ENTRY_ZONE_ATR_MULTIPLIER: float = float(os.getenv("ENTRY_ZONE_ATR_MULTIPLIER", "0.25"))

# Late signal protection: price must not have moved beyond this % from trigger
MAX_ENTRY_DISTANCE_PCT: float = float(os.getenv("MAX_ENTRY_DISTANCE_PCT", "0.20"))

# Armed setups expire after this many minutes
ARMED_SETUP_EXPIRE_MINUTES: int = int(os.getenv("ARMED_SETUP_EXPIRE_MINUTES", "60"))

# ── Scheduler ──────────────────────────────────────────────────────
SETUP_SCAN_CRON_MINUTES: str     = os.getenv("SETUP_SCAN_CRON_MINUTES",  "*/15")
TRIGGER_CHECK_SECONDS: int       = int(os.getenv("TRIGGER_CHECK_SECONDS", "2"))
OUTCOME_CHECK_MINUTES: int       = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
COIN_REFRESH_CRON_HOURS: str     = os.getenv("COIN_REFRESH_CRON_HOURS",  f"*/{COIN_REFRESH_HOURS}")

SIGNALS_PER_SCAN: int            = int(os.getenv("SIGNALS_PER_SCAN",        "3"))
MAX_CONCURRENT_SIGNALS: int      = int(os.getenv("MAX_CONCURRENT_SIGNALS",  "5"))
SIGNAL_COOLDOWN_MINUTES: int     = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "120"))
SIGNAL_EXPIRE_HOURS: int         = int(os.getenv("SIGNAL_EXPIRE_HOURS",     "6"))
SCAN_WORKERS: int                = int(os.getenv("SCAN_WORKERS",            "4"))

# ── WebSocket price tracker ────────────────────────────────────────
MEXC_WS_URL: str                 = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")
WS_SYMBOLS_PER_CONNECTION: int   = int(os.getenv("WS_SYMBOLS_PER_CONNECTION", "30"))
WS_RECONNECT_SECONDS: int        = int(os.getenv("WS_RECONNECT_SECONDS",      "5"))
WS_PING_INTERVAL_SECONDS: int    = int(os.getenv("WS_PING_INTERVAL_SECONDS",  "20"))
WS_PING_TIMEOUT_SECONDS: int     = int(os.getenv("WS_PING_TIMEOUT_SECONDS",   "10"))
WS_SUBSCRIBE_DELAY_SECONDS: float = float(os.getenv("WS_SUBSCRIBE_DELAY_SECONDS", "0.1"))

# ── Trade params ───────────────────────────────────────────────────
LEVERAGE: int   = int(os.getenv("LEVERAGE", "20"))

# ── APScheduler ────────────────────────────────────────────────────
SCHEDULER_MISFIRE_GRACE_SECONDS: int = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "30"))
SCHEDULER_MAX_INSTANCES: int         = int(os.getenv("SCHEDULER_MAX_INSTANCES",         "1"))

# ── Log ────────────────────────────────────────────────────────────
LOG_FILE: str                    = os.getenv("LOG_FILE",              "mexc_bot.log")
ENABLE_LOG_BACKUP_ON_START: bool = os.getenv("ENABLE_LOG_BACKUP_ON_START", "true").lower() == "true"
LOG_BACKUP_DIR: str              = os.getenv("LOG_BACKUP_DIR",        "logs/archive")

# ── MEXC REST API ──────────────────────────────────────────────────
MEXC_BASE_URL: str = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")

# ── Database ───────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "signals.db")

# ── Candle minutes (derived from ENTRY_TF) ─────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(ENTRY_TF, 15))))

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
