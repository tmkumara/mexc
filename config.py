import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Coin scanner ─────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
TOP_N_COINS: int = 50
COIN_REFRESH_HOURS: int = 6

# ── Strategy settings ─────────────────────────────────────────────
LEVERAGE:  int = 20
TIMEFRAME: str = "15m"  # change here → cron and outcome interval update automatically

# Cron minute string for each supported timeframe (fires just after candle close)
_TIMEFRAME_CRON: dict[str, str] = {
    "1m":  "*",
    "5m":  "1,6,11,16,21,26,31,36,41,46,51,56",
    "15m": "1,16,31,46",
    "30m": "1,31",
    "1h":  "1",
    "4h":  "1",
}
# Candle duration in minutes
_CANDLE_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
}
# Outcome-check interval in minutes (check every half-timeframe, min 5m)
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 5, "30m": 10, "1h": 15, "4h": 30,
}

if TIMEFRAME not in _TIMEFRAME_CRON:
    raise ValueError(f"Unsupported TIMEFRAME '{TIMEFRAME}'. Choose from: {list(_TIMEFRAME_CRON)}")

SCAN_CRON_MINUTES:    str = _TIMEFRAME_CRON[TIMEFRAME]
OUTCOME_CHECK_MINUTES: int = _TIMEFRAME_MINUTES[TIMEFRAME]
CANDLE_MINUTES:        int = _CANDLE_MINUTES[TIMEFRAME]

# ── EMA periods ───────────────────────────────────────────────────
EMA_FAST:  int = 9
EMA_SLOW:  int = 21
EMA_TREND: int = 200

# ── RSI ───────────────────────────────────────────────────────────
RSI_PERIOD: int = 14

# ── MACD ─────────────────────────────────────────────────────────
MACD_FAST:          int = 12
MACD_SLOW:          int = 26
MACD_SIGNAL_PERIOD: int = 9

# ── Volume filter ─────────────────────────────────────────────────
VOLUME_MA_BARS:  int   = 20
VOLUME_MIN_MULT: float = 1.5

# ── Fixed ROI targets (on leveraged position) ─────────────────────
TP_ROI_PCT: float = 5.0    # gain when TP is hit
SL_ROI_PCT: float = 10.0   # loss when SL is hit

# ── Scheduler ─────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 30
SIGNAL_EXPIRE_HOURS:     int = 4

# Max concurrent pending signals in the channel
MAX_CONCURRENT_SIGNALS: int = 3

# ── MEXC Futures REST API ──────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ───────────────────────────────────────────────────────
DB_PATH = "signals.db"
