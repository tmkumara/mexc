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

# ── Strategy: Liquidation-Aware 1m Scalp (v14) ──────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Liquidation-Aware 1m Scalp (v14)",
)

# ── Base signal (1m EMA/RSI/VWAP/volume) ────────────────────────────
SCALP_TF: str               = os.getenv("SCALP_TF", "1m")
SCALP_KLINE_COUNT: int      = int(os.getenv("SCALP_KLINE_COUNT", "100"))
EMA_FAST: int                = int(os.getenv("EMA_FAST", "9"))
EMA_MID: int                 = int(os.getenv("EMA_MID", "21"))
EMA_SLOW: int                = int(os.getenv("EMA_SLOW", "50"))
RSI_PERIOD: int               = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN: float           = float(os.getenv("RSI_LONG_MIN", "50"))
RSI_LONG_MAX: float           = float(os.getenv("RSI_LONG_MAX", "68"))
RSI_SHORT_MIN: float          = float(os.getenv("RSI_SHORT_MIN", "32"))
RSI_SHORT_MAX: float          = float(os.getenv("RSI_SHORT_MAX", "50"))
SCALP_VOLUME_MIN_MULT: float  = float(os.getenv("SCALP_VOLUME_MIN_MULT", "1.3"))
SCALP_VOLUME_MA_BARS: int     = int(os.getenv("SCALP_VOLUME_MA_BARS", "20"))

# ── Profit target / risk (price move = margin target / leverage) ───
TARGET_MARGIN_PROFIT: float  = float(os.getenv("TARGET_MARGIN_PROFIT", "0.135"))
MIN_RR: float                 = float(os.getenv("MIN_RR", "1.5"))
MAX_SL_PRICE_PCT: float       = float(os.getenv("MAX_SL_PRICE_PCT", "0.0045"))
BREAKEVEN_TRIGGER_PCT: float  = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.5"))

# ── Liquidation cluster estimator (see liq_estimator.py) ────────────
_LEVERAGE_TIERS_DEFAULT = "10:0.20,20:0.25,25:0.20,50:0.20,75:0.10,100:0.05"
LEVERAGE_TIERS: dict[int, float] = {}
for _pair in os.getenv("LEVERAGE_TIERS", _LEVERAGE_TIERS_DEFAULT).split(","):
    _lev_str, _weight_str = _pair.split(":")
    LEVERAGE_TIERS[int(_lev_str)] = float(_weight_str)

MMR_BUFFER: float            = float(os.getenv("MMR_BUFFER", "0.006"))
BUCKET_PCT: float             = float(os.getenv("BUCKET_PCT", "0.0005"))
CLUSTER_DECAY: float          = float(os.getenv("CLUSTER_DECAY", "0.97"))
CLUSTER_LOOKAROUND: float     = float(os.getenv("CLUSTER_LOOKAROUND", "0.02"))
CLUSTER_MIN_PERCENTILE: float = float(os.getenv("CLUSTER_MIN_PERCENTILE", "90"))
OI_POLL_SEC: int              = int(os.getenv("OI_POLL_SEC", "60"))

# ── Funding filter ───────────────────────────────────────────────────
FUNDING_EXTREME: float        = float(os.getenv("FUNDING_EXTREME", "0.0004"))

# ── Armed-setup lifetime (in 1m bars) ────────────────────────────────
SCALP_ARM_MAX_AGE_BARS: int   = int(os.getenv("SCALP_ARM_MAX_AGE_BARS", "10"))

# ── Scan cadence ─────────────────────────────────────────────────────
SCALP_SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCALP_SCAN_INTERVAL_MINUTES", "1"))

# ── Trade params ───────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))

# ── Scheduler ──────────────────────────────────────────────────────
OUTCOME_CHECK_MINUTES: int   = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
COIN_REFRESH_CRON_HOURS: str = os.getenv("COIN_REFRESH_CRON_HOURS", f"*/{COIN_REFRESH_HOURS}")

SIGNALS_PER_SCAN: int        = int(os.getenv("SIGNALS_PER_SCAN",        "1"))
MAX_CONCURRENT_SIGNALS: int  = int(os.getenv("MAX_CONCURRENT_SIGNALS",  "5"))
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "240"))
SIGNAL_EXPIRE_HOURS: int     = int(os.getenv("SIGNAL_EXPIRE_HOURS",     "4"))
SCAN_WORKERS: int            = int(os.getenv("SCAN_WORKERS",            "4"))

# Daily signal cap
MAX_DAILY_SIGNALS: int              = int(os.getenv("MAX_DAILY_SIGNALS",              "3"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int   = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES",   "180"))

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

# ── Candle minutes (derived from SCALP_TF) ─────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(SCALP_TF, 1))))

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
