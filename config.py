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

TOP_N_COINS: int = int(os.getenv("TOP_N_COINS", "150"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_REFRESH_HOURS: int = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking / coin_scanner.py compatibility ────────────
ENABLE_SMART_COIN_RANKING: bool = (
    os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
)

COIN_RANK_CANDIDATE_MULTIPLIER: int = int(
    os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4")
)

COIN_RANK_MAX_CANDIDATES: int = int(
    os.getenv(
        "COIN_RANK_MAX_CANDIDATES",
        str(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER),
    )
)

COIN_RANK_TIMEFRAME: str = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int = int(os.getenv("COIN_RANK_WORKERS", "4"))

COIN_RANK_MIN_LAST_PRICE: float = float(
    os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.000001")
)

COIN_RANK_MIN_RANGE_PCT: float = float(
    os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20")
)

COIN_RANK_MAX_RANGE_PCT: float = float(
    os.getenv("COIN_RANK_MAX_RANGE_PCT", "18.0")
)

COIN_RANK_MAX_ABS_MOVE_PCT: float = float(
    os.getenv("COIN_RANK_MAX_ABS_MOVE_PCT", "12.0")
)

COIN_RANK_VOLUME_WEIGHT: float = float(
    os.getenv("COIN_RANK_VOLUME_WEIGHT", "0.35")
)

COIN_RANK_VOLATILITY_WEIGHT: float = float(
    os.getenv("COIN_RANK_VOLATILITY_WEIGHT", "0.30")
)

COIN_RANK_TREND_WEIGHT: float = float(
    os.getenv("COIN_RANK_TREND_WEIGHT", "0.20")
)

COIN_RANK_LIQUIDITY_WEIGHT: float = float(
    os.getenv("COIN_RANK_LIQUIDITY_WEIGHT", "0.15")
)

COIN_RANK_OVEREXTENSION_PENALTY: float = float(
    os.getenv("COIN_RANK_OVEREXTENSION_PENALTY", "0.25")
)

COIN_RANK_LOW_ACTIVITY_PENALTY: float = float(
    os.getenv("COIN_RANK_LOW_ACTIVITY_PENALTY", "0.20")
)

# Older compatibility names.
SMART_RANKING_LOOKBACK_MINUTES: int = int(
    os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240")
)

SMART_RANKING_MIN_VOLUME_USD: float = float(
    os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD))
)

SMART_RANKING_TOP_N: int = int(
    os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS))
)

MIN_24H_VOLUME_USD: float = float(
    os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD))
)

MAX_SPREAD_PCT: float = float(
    os.getenv("MAX_SPREAD_PCT", "0.35")
)

MIN_PRICE_CHANGE_24H_PCT: float = float(
    os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0")
)

# ── EMA + CCI Strategy ────────────────────────────────────────────
STRATEGY_TF: str = os.getenv("STRATEGY_TF", "1h")

TREND_TF: str = STRATEGY_TF
ENTRY_TF: str = STRATEGY_TF

STRATEGY_KLINE_COUNT: int = int(os.getenv("STRATEGY_KLINE_COUNT", "260"))

EMA_FAST: int = int(os.getenv("EMA_FAST", "10"))
EMA_SLOW: int = int(os.getenv("EMA_SLOW", "20"))
CCI_LENGTH: int = int(os.getenv("CCI_LENGTH", "20"))
# bars to look back for recent lowest low / highest high (SL placement)
SL_LOOKBACK: int = int(os.getenv("SL_LOOKBACK", "20"))

# ── Risk management ───────────────────────────────────────────────
REWARD_RATIO: float = float(os.getenv("REWARD_RATIO", "1.6"))

MIN_TP_ROI_PCT: float = float(os.getenv("MIN_TP_ROI_PCT", "20.0"))

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))

# Kept for compatibility with old report/status references.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = int(
    os.getenv(
        "SIGNAL_COOLDOWN_MINUTES",
        "60" if STRATEGY_TF == "1h" else "240",
    )
)

SIGNAL_EXPIRE_HOURS: int = int(
    os.getenv(
        "SIGNAL_EXPIRE_HOURS",
        "10" if STRATEGY_TF == "1h" else "24",
    )
)

MAX_CONCURRENT_SIGNALS: int = int(
    os.getenv("MAX_CONCURRENT_SIGNALS", "20")
)

SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "*/5")

SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "10"))

SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "8"))

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