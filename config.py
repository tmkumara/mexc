import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Coins to exclude
EXCLUDE_COINS = {"BTC_USDT", "ETH_USDT", "SOL_USDT"}

# How many coins to track
TOP_N_COINS = 10

# Trade settings
LEVERAGE = 20
TP_PCT = 0.005   # +0.5% price move = +10% ROI at 20x
SL_PCT = 0.005   # -0.5% price move = -10% ROI at 20x
RISK_PCT = 0.80  # 80% of account per trade (informational, shown in signal)

# Scanner
SCAN_INTERVAL_SECONDS = 300   # scan every 5 minutes
COIN_REFRESH_HOURS = 6        # refresh zero-fee coin list every 6 hours

# Auto-expire pending signals after this many hours
SIGNAL_EXPIRE_HOURS = 24

# Signal cooldown per coin (minutes) — avoid spamming same coin
SIGNAL_COOLDOWN_MINUTES = 60

# MEXC Futures API
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# Database
DB_PATH = "signals.db"
