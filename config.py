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
TOP_N_COINS:              int   = 40
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── Nadaraya-Watson Rational Quadratic Kernel Strategy ────────────
# TradingView settings:
# Source:                  Close
# Lookback Window:          32
# Relative Weighting:       25
# Start Regression at Bar:  233
# Smooth Colors:            True
# Lag:                      7
# Timeframe:                15 minutes
NWE_H:           float = 32.0
NWE_ALPHA:       float = 25.0
NWE_SIZE:        int   = 233
NWE_LAG:         int   = 7
NWE_SMOOTH:      bool  = True
NWE_TF:          str   = "15m"
NWE_KLINE_COUNT: int   = 300

# ── Supertrend filter ─────────────────────────────────────────────
# TradingView common settings:
# ATR Length: 10
# Factor:     3
# Timeframe:  same as NWE_TF
SUPERTREND_ENABLED:    bool  = True
SUPERTREND_ATR_LENGTH: int   = 10
SUPERTREND_FACTOR:     float = 3.0

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE:     int   = 20
TP_ROI_PCT:   float = 5.0
SL_ROI_PCT:   float = 5.0
REWARD_RATIO: float = TP_ROI_PCT / SL_ROI_PCT

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 120
SIGNAL_EXPIRE_HOURS:     int = 8
MAX_CONCURRENT_SIGNALS:  int = 10

# For 15-minute candles:
# - scan every 5 minutes to catch newly closed 15m candles quickly
# - outcome checker checks every 5 minutes
# - candle size is 15 minutes
SCAN_CRON_MINUTES:     str = "*/5"
SIGNALS_PER_SCAN:      int = 3
OUTCOME_CHECK_MINUTES: int = 5
CANDLE_MINUTES:        int = 15
SCAN_WORKERS:          int = 8

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"