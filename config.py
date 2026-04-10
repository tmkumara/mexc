import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# Coins to exclude
EXCLUDE_COINS = {"BTC_USDT", "ETH_USDT", "SOL_USDT"}

# How many coins to track
TOP_N_COINS = 20

# Hull Suite trade settings (existing strategy)
LEVERAGE = 50
TP_PCT = 0.001   # +0.1% price move = +5% ROI at 50x
SL_PCT = 0.002   # -0.2% price move = -10% ROI at 50x
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

# ── Scalping strategy (EMA/RSI/VWAP, 5-minute candles) ──────────────────────

# Priority-ordered zero-fee pairs (MEXC underscore format)
SCALPING_PAIRS: list[str] = [
    "SOL_USDT",
    "BTC_USDT",
    "ETH_USDT",
    "BNB_USDT",
    "DOGE_USDT",
    "ADA_USDT",
]

# 25× isolated margin
SCALPING_LEVERAGE: int = 25

# +3% ROI on margin = +0.12% price move at 25×
SCALPING_TP_PCT: float = 0.0012

# −10% ROI on margin = −0.40% price move at 25×
SCALPING_SL_PCT: float = 0.0040

# Scan interval in seconds
SCALPING_SCAN_INTERVAL: int = 60

# Deduplication cooldown per symbol+direction (minutes)
SCALPING_SIGNAL_COOLDOWN_MINUTES: int = 15

# Indicator parameters
EMA_FAST: int = 9
EMA_SLOW: int = 21
RSI_PERIOD: int = 7
RSI_OVERBOUGHT: int = 68   # avoid entering above this
RSI_OVERSOLD: int = 32     # avoid entering below this

# Volume confirmation
VOLUME_MA_PERIOD: int = 20
VOLUME_MULTIPLIER: float = 1.5   # current bar must be >= 1.5× the 20-bar average

# After every Nth signal today, append the risk disclaimer
RISK_REMINDER_EVERY_N: int = 10
