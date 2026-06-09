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
QUOTE_CURRENCY: str = os.getenv("QUOTE_CURRENCY", "USDT")
CRYPTO_FUTURES_ONLY: bool = os.getenv("CRYPTO_FUTURES_ONLY", "true").lower() == "true"

EXCLUDE_COINS: set[str] = {
    coin.strip().upper()
    for coin in os.getenv(
        "EXCLUDE_COINS",
        "BTC_USDT,ETH_USDT,SOL_USDT,XAUT_USDT",
    ).split(",")
    if coin.strip()
}

TOP_N_COINS: int = int(os.getenv("TOP_N_COINS", "80"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_REFRESH_HOURS: int = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking / coin_scanner.py compatibility ────────────
ENABLE_SMART_COIN_RANKING: bool = os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
COIN_RANK_CANDIDATE_MULTIPLIER: int = int(os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4"))
COIN_RANK_MAX_CANDIDATES: int = int(os.getenv("COIN_RANK_MAX_CANDIDATES", str(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER)))
COIN_RANK_TIMEFRAME: str = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int = int(os.getenv("COIN_RANK_WORKERS", "4"))
COIN_RANK_MIN_LAST_PRICE: float = float(os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.000001"))
COIN_RANK_MIN_RANGE_PCT: float = float(os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20"))
COIN_RANK_MAX_RANGE_PCT: float = float(os.getenv("COIN_RANK_MAX_RANGE_PCT", "18.0"))
COIN_RANK_MAX_ABS_MOVE_PCT: float = float(os.getenv("COIN_RANK_MAX_ABS_MOVE_PCT", "12.0"))
COIN_RANK_VOLUME_WEIGHT: float = float(os.getenv("COIN_RANK_VOLUME_WEIGHT", "0.35"))
COIN_RANK_VOLATILITY_WEIGHT: float = float(os.getenv("COIN_RANK_VOLATILITY_WEIGHT", "0.30"))
COIN_RANK_TREND_WEIGHT: float = float(os.getenv("COIN_RANK_TREND_WEIGHT", "0.20"))
COIN_RANK_LIQUIDITY_WEIGHT: float = float(os.getenv("COIN_RANK_LIQUIDITY_WEIGHT", "0.15"))
COIN_RANK_OVEREXTENSION_PENALTY: float = float(os.getenv("COIN_RANK_OVEREXTENSION_PENALTY", "0.25"))
COIN_RANK_LOW_ACTIVITY_PENALTY: float = float(os.getenv("COIN_RANK_LOW_ACTIVITY_PENALTY", "0.20"))

# Older compatibility names used by some coin_scanner versions.
SMART_RANKING_LOOKBACK_MINUTES: int = int(os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240"))
SMART_RANKING_MIN_VOLUME_USD: float = float(os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
SMART_RANKING_TOP_N: int = int(os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS)))
MIN_24H_VOLUME_USD: float = float(os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
MAX_SPREAD_PCT: float = float(os.getenv("MAX_SPREAD_PCT", "0.35"))
MIN_PRICE_CHANGE_24H_PCT: float = float(os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0"))

# ── MTF SMC Strategy — Timeframes ─────────────────────────────────
STRATEGY_NAME: str = os.getenv("STRATEGY_NAME", "MTF SMC Sweep + OB Retest")

# 1D = macro regime | 4H = trend | 1H = structure | 15m = entry
MACRO_TF: str    = os.getenv("MACRO_TF",    "1d")
HTF_TREND_TF: str = os.getenv("HTF_TREND_TF", "4h")
STRUCTURE_TF: str = os.getenv("STRUCTURE_TF", "1h")
ENTRY_TF: str    = os.getenv("ENTRY_TF",    "15m")

# TREND_TF kept for backward compat — equals STRUCTURE_TF
TREND_TF: str = STRUCTURE_TF

# Kline counts per timeframe
MACRO_KLINE_COUNT: int     = int(os.getenv("MACRO_KLINE_COUNT",     "220"))
HTF_KLINE_COUNT: int       = int(os.getenv("HTF_KLINE_COUNT",       "220"))
STRUCTURE_KLINE_COUNT: int = int(os.getenv("STRUCTURE_KLINE_COUNT", "220"))
ENTRY_KLINE_COUNT: int     = int(os.getenv("ENTRY_KLINE_COUNT",     "220"))
MONITOR_KLINE_COUNT: int   = int(os.getenv("MONITOR_KLINE_COUNT",   "80"))

# TREND_KLINE_COUNT kept for backward compat — equals STRUCTURE_KLINE_COUNT
TREND_KLINE_COUNT: int = STRUCTURE_KLINE_COUNT

# MTF alignment gate
REQUIRE_MTF_ALIGNMENT: bool = os.getenv("REQUIRE_MTF_ALIGNMENT", "true").lower() == "true"

# Swing / structure detection
SWING_LEFT: int = int(os.getenv("SWING_LEFT", "3"))
SWING_RIGHT: int = int(os.getenv("SWING_RIGHT", "2"))
STRUCTURE_LOOKBACK: int = int(os.getenv("STRUCTURE_LOOKBACK", "160"))
ENTRY_LOOKBACK: int = int(os.getenv("ENTRY_LOOKBACK", "180"))
SWEEP_LOOKBACK: int = int(os.getenv("SWEEP_LOOKBACK", "18"))

# Displacement candle
AVG_BODY_PERIOD: int = int(os.getenv("AVG_BODY_PERIOD", "20"))
DISPLACEMENT_BODY_MULTIPLIER: float = float(os.getenv("DISPLACEMENT_BODY_MULTIPLIER", "1.35"))
DISPLACEMENT_CLOSE_POSITION: float = float(os.getenv("DISPLACEMENT_CLOSE_POSITION", "0.65"))

# Freshness controls — how many ENTRY_TF candles back a setup component can be
# With 15m candles: 6 = 90m, 10 = 150m, 12 = 3h
MAX_DISPLACEMENT_AGE_CANDLES: int = int(os.getenv("MAX_DISPLACEMENT_AGE_CANDLES", "6"))
MAX_SWEEP_AGE_CANDLES: int        = int(os.getenv("MAX_SWEEP_AGE_CANDLES",        "10"))
MAX_OB_AGE_CANDLES: int           = int(os.getenv("MAX_OB_AGE_CANDLES",           "12"))

# Order block
ORDER_BLOCK_LOOKBACK: int = int(os.getenv("ORDER_BLOCK_LOOKBACK", "12"))
MAX_SIGNAL_CANDLE_BODY_PCT: float = float(os.getenv("MAX_SIGNAL_CANDLE_BODY_PCT", "1.20"))
PENDING_SETUP_EXPIRE_CANDLES: int = int(os.getenv("PENDING_SETUP_EXPIRE_CANDLES", "24"))
MAX_PENDING_SETUPS_PER_SYMBOL: int = int(os.getenv("MAX_PENDING_SETUPS_PER_SYMBOL", "1"))

# HTF trend filter (4H EMA50/200)
ENABLE_HTF_FILTER: bool = os.getenv("ENABLE_HTF_FILTER", "true").lower() == "true"
HTF_EMA_FAST: int = int(os.getenv("HTF_EMA_FAST", "50"))
HTF_EMA_SLOW: int = int(os.getenv("HTF_EMA_SLOW", "200"))
HTF_EMA_SLOPE_LOOKBACK: int = int(os.getenv("HTF_EMA_SLOPE_LOOKBACK", "3"))

# Entry EMA alignment filter (on ENTRY_TF)
ENABLE_ENTRY_EMA_FILTER: bool = os.getenv("ENABLE_ENTRY_EMA_FILTER", "true").lower() == "true"
EMA_FAST_FILTER: int = int(os.getenv("EMA_FAST_FILTER", "20"))
EMA_SLOW_FILTER: int = int(os.getenv("EMA_SLOW_FILTER", "50"))

# ATR filter
ENABLE_ATR_FILTER: bool = os.getenv("ENABLE_ATR_FILTER", "true").lower() == "true"
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))
MIN_ATR_PCT: float = float(os.getenv("MIN_ATR_PCT", "0.18"))
MAX_ATR_PCT: float = float(os.getenv("MAX_ATR_PCT", "2.50"))
ATR_SL_MULTIPLIER: float = float(os.getenv("ATR_SL_MULTIPLIER", "0.25"))

# Volume confirmation
ENABLE_VOLUME_FILTER: bool = os.getenv("ENABLE_VOLUME_FILTER", "true").lower() == "true"
VOLUME_LOOKBACK: int = int(os.getenv("VOLUME_LOOKBACK", "20"))
MIN_VOLUME_MULTIPLIER: float = float(os.getenv("MIN_VOLUME_MULTIPLIER", "1.05"))

# BTC market regime filter
ENABLE_BTC_FILTER: bool = os.getenv("ENABLE_BTC_FILTER", "true").lower() == "true"
BTC_SYMBOL: str = os.getenv("BTC_SYMBOL", "BTC_USDT")
BTC_TF: str = os.getenv("BTC_TF", "15m")
BTC_EMA_PERIOD: int = int(os.getenv("BTC_EMA_PERIOD", "50"))
BTC_KLINE_COUNT: int = int(os.getenv("BTC_KLINE_COUNT", "100"))

# RR / SL controls
MIN_STRUCTURE_RR: float = float(os.getenv("MIN_STRUCTURE_RR", "1.50"))
MAX_STRUCTURE_RR: float = float(os.getenv("MAX_STRUCTURE_RR", "3.00"))
REWARD_RATIO: float = float(os.getenv("REWARD_RATIO", "1.50"))
SL_BUFFER_PCT: float = float(os.getenv("SL_BUFFER_PCT", "0.05"))
TP_BUFFER_PCT: float = float(os.getenv("TP_BUFFER_PCT", "0.02"))
MIN_SL_PCT: float = float(os.getenv("MIN_SL_PCT", "0.20"))
MAX_SL_PCT: float = float(os.getenv("MAX_SL_PCT", "1.25"))
MIN_SETUP_SCORE: float = float(os.getenv("MIN_SETUP_SCORE", "88"))

# Setup quality / stale setup controls
MAX_OB_DISTANCE_ATR: float = float(os.getenv("MAX_OB_DISTANCE_ATR", "5.0"))
MAX_OB_DISTANCE_PCT: float = float(os.getenv("MAX_OB_DISTANCE_PCT", "4.0"))
EXPIRE_IF_PRICE_AWAY_ATR: float = float(os.getenv("EXPIRE_IF_PRICE_AWAY_ATR", "5.0"))
EXPIRE_IF_PRICE_AWAY_PCT: float = float(os.getenv("EXPIRE_IF_PRICE_AWAY_PCT", "4.0"))
REVALIDATE_BEFORE_FIRE: bool = os.getenv("REVALIDATE_BEFORE_FIRE", "true").lower() == "true"
OB_ENTRY_QUALITY_CHECK: bool = os.getenv("OB_ENTRY_QUALITY_CHECK", "true").lower() == "true"

# MSS / confirmation-break entry controls
REQUIRE_MSS_BREAK_ENTRY: bool = os.getenv("REQUIRE_MSS_BREAK_ENTRY", "true").lower() == "true"
MSS_BREAK_LOOKBACK_CANDLES: int = int(os.getenv("MSS_BREAK_LOOKBACK_CANDLES", "4"))
MSS_BREAK_BUFFER_PCT: float = float(os.getenv("MSS_BREAK_BUFFER_PCT", "0.01"))

# ATR stop floor
ENABLE_ATR_STOP_FLOOR: bool = os.getenv("ENABLE_ATR_STOP_FLOOR", "true").lower() == "true"
ATR_STOP_FLOOR_MULTIPLIER: float = float(os.getenv("ATR_STOP_FLOOR_MULTIPLIER", "0.75"))

# Trend candle confirmation before firing (uses ENTRY_TF by default)
REQUIRE_TREND_CANDLE_CONFIRMATION: bool = os.getenv("REQUIRE_TREND_CANDLE_CONFIRMATION", "true").lower() == "true"
TREND_CONFIRM_TF: str = os.getenv("TREND_CONFIRM_TF", "15m")
TREND_CONFIRM_KLINE_COUNT: int = int(os.getenv("TREND_CONFIRM_KLINE_COUNT", "20"))

# ── Legacy EMA/CCI names kept only so old imports do not fail ─────
EMA_FAST: int = int(os.getenv("EMA_FAST", "10"))
EMA_SLOW: int = int(os.getenv("EMA_SLOW", "20"))
CCI_LENGTH: int = int(os.getenv("CCI_LENGTH", "20"))
CCI_MIN_ABS: float = float(os.getenv("CCI_MIN_ABS", "50.0"))
SL_LOOKBACK: int = int(os.getenv("SL_LOOKBACK", "20"))
BOS_LOOKBACK: int = int(os.getenv("BOS_LOOKBACK", "20"))
DOUBLE_LOOKBACK: int = int(os.getenv("DOUBLE_LOOKBACK", "40"))
DOUBLE_TOLERANCE_PCT: float = float(os.getenv("DOUBLE_TOLERANCE_PCT", "1.5"))
PATTERN_MIN_SCORE: int = int(os.getenv("PATTERN_MIN_SCORE", "2"))
STRATEGY_KLINE_COUNT: int = int(os.getenv("STRATEGY_KLINE_COUNT", "260"))

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "120"))
SIGNAL_EXPIRE_HOURS: int = int(os.getenv("SIGNAL_EXPIRE_HOURS", "6"))
MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "5"))
SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "*/5")
SETUP_MONITOR_MINUTES: int = int(os.getenv("SETUP_MONITOR_MINUTES", "1"))
OUTCOME_CHECK_MINUTES: int = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "2"))
SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# Setup saving limits / correlation guard
MAX_NEW_SETUPS_PER_SCAN: int = int(os.getenv("MAX_NEW_SETUPS_PER_SCAN", "4"))
MAX_SETUPS_SAME_DIRECTION_PER_SCAN: int = int(os.getenv("MAX_SETUPS_SAME_DIRECTION_PER_SCAN", "2"))
MAX_WAITING_SETUPS_TOTAL: int = int(os.getenv("MAX_WAITING_SETUPS_TOTAL", "40"))
MAX_WAITING_SETUPS_SAME_DIRECTION: int = int(os.getenv("MAX_WAITING_SETUPS_SAME_DIRECTION", "20"))
SETUP_MONITOR_LIMIT: int = int(os.getenv("SETUP_MONITOR_LIMIT", "25"))

# Debug monitor logs
SETUP_MONITOR_LOG_DETAILS: bool = os.getenv("SETUP_MONITOR_LOG_DETAILS", "true").lower() == "true"

# APScheduler controls
SCHEDULER_MISFIRE_GRACE_SECONDS: int = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "30"))
SCHEDULER_MAX_INSTANCES: int = int(os.getenv("SCHEDULER_MAX_INSTANCES", "1"))

# Derive CANDLE_MINUTES from ENTRY_TF so outcome checking uses the right interval.
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "8h": 480, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv(
    "CANDLE_MINUTES",
    str(_TF_MINUTES.get(ENTRY_TF, 15)),
))

# ── Log rotation / restart backup ─────────────────────────────────
LOG_FILE: str = os.getenv("LOG_FILE", "mexc_bot.log")
ENABLE_LOG_BACKUP_ON_START: bool = os.getenv("ENABLE_LOG_BACKUP_ON_START", "true").lower() == "true"
LOG_BACKUP_DIR: str = os.getenv("LOG_BACKUP_DIR", "logs/archive")

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")

# ── WebSocket candle cache ────────────────────────────────────────
ENABLE_WS_CANDLE_CACHE: bool = os.getenv("ENABLE_WS_CANDLE_CACHE", "true").lower() == "true"
MEXC_WS_URL: str = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")
CANDLE_CACHE_LIMIT: int = int(os.getenv("CANDLE_CACHE_LIMIT", "320"))
WS_MAX_SYMBOLS: int = int(os.getenv("WS_MAX_SYMBOLS", "80"))
WS_SEED_KLINE_COUNT: int = int(os.getenv("WS_SEED_KLINE_COUNT", "260"))
WS_DYNAMIC_REFRESH_SECONDS: int = int(os.getenv("WS_DYNAMIC_REFRESH_SECONDS", "300"))
WS_RECONNECT_DELAY_SECONDS: int = int(os.getenv("WS_RECONNECT_DELAY_SECONDS", "5"))
WS_PING_INTERVAL_SECONDS: int = int(os.getenv("WS_PING_INTERVAL_SECONDS", "20"))
WS_PING_TIMEOUT_SECONDS: int = int(os.getenv("WS_PING_TIMEOUT_SECONDS", "10"))
WS_APP_HEARTBEAT_ENABLED: bool = os.getenv("WS_APP_HEARTBEAT_ENABLED", "true").lower() == "true"
WS_APP_HEARTBEAT_SECONDS: int = int(os.getenv("WS_APP_HEARTBEAT_SECONDS", "15"))
WS_SUBSCRIBE_DELAY_SECONDS: float = float(os.getenv("WS_SUBSCRIBE_DELAY_SECONDS", "0.05"))
WS_SUBSCRIBE_BATCH_SIZE: int = int(os.getenv("WS_SUBSCRIBE_BATCH_SIZE", "20"))
WS_SUBSCRIBE_BATCH_PAUSE_SECONDS: float = float(os.getenv("WS_SUBSCRIBE_BATCH_PAUSE_SECONDS", "1.0"))
WS_TEST_SYMBOLS: list[str] = [
    s.strip().upper()
    for s in os.getenv("WS_TEST_SYMBOLS", "BTC_USDT").split(",")
    if s.strip()
]

MEXC_INTERVAL_MAP: dict[str, str] = {
    "1m": "Min1",
    "3m": "Min3",
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h": "Min60",
    "4h": "Hour4",
    "8h": "Hour8",
    "1d": "Day1",
}
