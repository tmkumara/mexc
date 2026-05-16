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
TOP_N_COINS:              int   = 80
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── EMA + VWAP Pullback Scalping Strategy ─────────────────────────
# Trend timeframe confirms market direction.
# Entry timeframe gives the actual scalping entry.
TREND_TF: str = "15m"
ENTRY_TF: str = "5m"

TREND_KLINE_COUNT: int = 200
ENTRY_KLINE_COUNT: int = 300

# 15m trend filter
TREND_EMA_PERIOD: int = 50

# 5m entry structure
EMA_FAST_PERIOD: int = 9
EMA_SLOW_PERIOD: int = 21

# 5m volume confirmation
VOLUME_SMA_PERIOD: int = 20
MIN_VOLUME_RATIO: float = 1.10

# Entry quality filters
MAX_ENTRY_DISTANCE_FROM_EMA_PCT: float = 0.20
MAX_SIGNAL_CANDLE_BODY_PCT: float = 0.45

# ── Dynamic ATR SL / TP ───────────────────────────────────────────
DYNAMIC_RISK_ENABLED: bool = True

ATR_PERIOD: int = 14
SL_ATR_MULTIPLIER: float = 1.2

# Safety limits for SL distance as percentage of entry price
MIN_SL_PCT: float = 0.25
MAX_SL_PCT: float = 1.50

# Dynamic RR by signal quality
RR_WEAK: float = 1.2
RR_GOOD: float = 1.5
RR_STRONG: float = 2.0

SCORE_GOOD_MIN: float = 65.0
SCORE_STRONG_MIN: float = 80.0

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# These are kept for compatibility with existing reports/bot display.
# Actual TP/SL is now dynamically calculated in strategy.py.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60
SIGNAL_EXPIRE_HOURS:     int = 6
MAX_CONCURRENT_SIGNALS:  int = 10

# 5m entry timeframe:
# - scan every 1 minute to catch new 5m candle closes quickly
# - outcome checker runs every 1 minute
# - candle size is 5 minutes
SCAN_CRON_MINUTES:     str = "*/1"
SIGNALS_PER_SCAN:      int = 3
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

# Keep workers modest to avoid MEXC rate limits when scanning 80 coins
SCAN_WORKERS:          int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"