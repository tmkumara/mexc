import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Trading pairs (MEXC futures format) ──────────────────────────
TRADING_PAIRS: list[str] = [
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "BNB_USDT",
    "XRP_USDT",
    "DOGE_USDT",
    "ADA_USDT",
]

# ── Strategy settings ─────────────────────────────────────────────
LEVERAGE   = 10
TIMEFRAME  = "1h"

# Supertrend params
ST_LENGTH     = 10
ST_MULTIPLIER = 2.5

# EMA trend filter
EMA_TREND_PERIOD = 200

# Risk:Reward — TP = REWARD_RATIO × risk distance
REWARD_RATIO: float = 2.0

# ── Scheduler ─────────────────────────────────────────────────────
# Scan every hour, aligned to candle close
SCAN_INTERVAL_SECONDS = 3600

# Same symbol+direction blocked for N minutes after a signal
SIGNAL_COOLDOWN_MINUTES = 240

# Pending signals auto-expire after N hours
SIGNAL_EXPIRE_HOURS = 48

# ── MEXC Futures REST API ──────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ───────────────────────────────────────────────────────
DB_PATH = "signals.db"
