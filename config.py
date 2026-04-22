import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Coin scanner ─────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT"}
TOP_N_COINS: int = 20
COIN_REFRESH_HOURS: int = 6

# ── Strategy settings ─────────────────────────────────────────────
LEVERAGE  = 20
TIMEFRAME = "5m"

# ZLSMA (Zero Lag Least Squares Moving Average)
ZLSMA_LENGTH: int = 200

# Chandelier Exit
CE_ATR_PERIOD: int   = 1
CE_ATR_MULT:   float = 2.0

# Fixed ROI targets (at LEVERAGE)
TP_ROI_PCT: float = 3.0   # +3% ROI on position
SL_ROI_PCT: float = 10.0  # -10% ROI on position

# Max concurrent pending signals in the channel
MAX_CONCURRENT_SIGNALS: int = 1

# ── Scheduler ─────────────────────────────────────────────────────
# Same symbol blocked for N minutes after a signal fires
SIGNAL_COOLDOWN_MINUTES: int = 60

# Pending signals auto-expire after N hours
SIGNAL_EXPIRE_HOURS: int = 4

# Refresh zero-fee coin list every N hours
COIN_REFRESH_HOURS: int = 6

# ── MEXC Futures REST API ──────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ───────────────────────────────────────────────────────
DB_PATH = "signals.db"
