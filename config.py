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

# ── Fresh Trend Meter + Stoch MTM Strategy ────────────────────────
# Use "1h" for more signals, "4h" for fewer but more stable signals.
STRATEGY_TF: str = os.getenv("STRATEGY_TF", "1h")
TREND_TF:    str = STRATEGY_TF
ENTRY_TF:    str = STRATEGY_TF

STRATEGY_KLINE_COUNT: int = 260

# Trend Meter approximation:
# Line 1 = EMA13/EMA21
# Line 2 = EMA34/EMA55
# Line 3 = close vs EMA200
TREND_EMA_FAST_1: int = 13
TREND_EMA_SLOW_1: int = 21
TREND_EMA_FAST_2: int = 34
TREND_EMA_SLOW_2: int = 55
TREND_EMA_FILTER: int = 200

# Stoch MTM / SMI settings.
STOCH_MTM_LENGTH: int = 10
STOCH_MTM_SMOOTH_1: int = 3
STOCH_MTM_SMOOTH_2: int = 10
STOCH_MTM_SIGNAL: int = 5
STOCH_MTM_UPPER: float = 40.0
STOCH_MTM_LOWER: float = -40.0

# Entry quality filters.
REQUIRE_CLOSED_CROSS: bool = True
MAX_CROSS_LOOKBACK_CANDLES: int = 1
MIN_ABS_MTM_AFTER_CROSS: float = 35.0

# Risk management.
ATR_PERIOD: int = 14
ATR_SL_MULTIPLIER: float = 1.25
REWARD_RATIO: float = 1.6
MIN_SL_PCT: float = 0.25
MAX_SL_PCT: float = 2.50

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 180 if STRATEGY_TF == "1h" else 480
SIGNAL_EXPIRE_HOURS:     int = 10 if STRATEGY_TF == "1h" else 24
MAX_CONCURRENT_SIGNALS:  int = 10

# Direct signal scan. For 1h, every 5 min is fine because strategy uses only
# completed candles and cooldown prevents duplicates.
SETUP_SCAN_CRON_MINUTES: str = "*/5"
SIGNALS_PER_SCAN: int = 3
SCAN_WORKERS: int = 4

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES: int = 60 if STRATEGY_TF == "1h" else 240

# Kept for compatibility with older DB/status references.
SETUP_MONITOR_MINUTES: int = 1

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")
