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
TIMEFRAME = "5m"   # change here → cron and outcome interval update automatically

# Cron minute string for each supported timeframe (fires just after candle close)
_TIMEFRAME_CRON: dict[str, str] = {
    "1m":  "*",
    "5m":  "1,6,11,16,21,26,31,36,41,46,51,56",
    "15m": "1,16,31,46",
    "30m": "1,31",
    "1h":  "1",
    "4h":  "1",
}
# Outcome-check interval in minutes (check every half-timeframe, min 5m)
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 5, "30m": 10, "1h": 15, "4h": 30,
}

if TIMEFRAME not in _TIMEFRAME_CRON:
    raise ValueError(f"Unsupported TIMEFRAME '{TIMEFRAME}'. Choose from: {list(_TIMEFRAME_CRON)}")

SCAN_CRON_MINUTES:    str = _TIMEFRAME_CRON[TIMEFRAME]
OUTCOME_CHECK_MINUTES: int = _TIMEFRAME_MINUTES[TIMEFRAME]

# ZLSMA (Zero Lag Least Squares Moving Average)
ZLSMA_LENGTH: int = 200

# Chandelier Exit
CE_ATR_PERIOD: int   = 1
CE_ATR_MULT:   float = 2.0

# ── Signal filter tuning ──────────────────────────────────────────
# Mode A: previous N candle wicks must not touch ZLSMA (raise to 10 for stricter)
ZLSMA_SEPARATION_CANDLES: int = 5

# Mode B: consecutive candles above/below ZLSMA required to confirm crossover
ZLSMA_CROSS_CONFIRM: int = 2

# Mode B: how many bars back to search for a CE flip that occurred before the ZLSMA cross
CE_CROSS_LOOKBACK: int = 15

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
