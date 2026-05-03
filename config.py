import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── Coin scanner ─────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
COIN_REFRESH_HOURS: int = 4   # refresh RSI-ranked coin list every 4h

# ── RSI heatmap coin selection (4h timeframe) ─────────────────────
RSI_HTF:            str   = "4h"
RSI_PERIOD_HTF:     int   = 14
RSI_OVERSOLD_MAX:   float = 40.0   # RSI < 40  → LONG candidate
RSI_OVERBOUGHT_MIN: float = 65.0   # RSI > 65  → SHORT candidate
RSI_TOP_N_EACH:     int   = 4      # top 4 oversold + top 4 overbought = max 8 coins

# ── Entry timeframe (15m) ─────────────────────────────────────────
LEVERAGE:  int = 20
TIMEFRAME: str = "15m"

_TIMEFRAME_CRON: dict[str, str] = {
    "1m":  "*",
    "5m":  "1,6,11,16,21,26,31,36,41,46,51,56",
    "15m": "1,16,31,46",
    "30m": "1,31",
    "1h":  "1",
    "4h":  "1",
}
_CANDLE_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
}
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 5, "30m": 10, "1h": 15, "4h": 30,
}

if TIMEFRAME not in _TIMEFRAME_CRON:
    raise ValueError(f"Unsupported TIMEFRAME '{TIMEFRAME}'. Choose from: {list(_TIMEFRAME_CRON)}")

SCAN_CRON_MINUTES:    str = _TIMEFRAME_CRON[TIMEFRAME]
OUTCOME_CHECK_MINUTES: int = _TIMEFRAME_MINUTES[TIMEFRAME]
CANDLE_MINUTES:        int = _CANDLE_MINUTES[TIMEFRAME]

# ── EMA periods (15m entry) ───────────────────────────────────────
EMA_FAST:  int = 9
EMA_SLOW:  int = 21
EMA_TREND: int = 200

# ── RSI + MACD (15m entry) ────────────────────────────────────────
RSI_PERIOD:         int = 14
MACD_FAST:          int = 12
MACD_SLOW:          int = 26
MACD_SIGNAL_PERIOD: int = 9

# ── Volume filter ─────────────────────────────────────────────────
VOLUME_MA_BARS:  int   = 20
VOLUME_MIN_MULT: float = 1.5

# ── Fibonacci qualification (1h timeframe) ────────────────────────
FIB_HTF:             str   = "1h"
FIB_LOOKBACK:        int   = 200     # 1h candles to scan for pivots (~8 days)
FIB_PIVOT_STRENGTH:  int   = 5       # bars each side required for local pivot
FIB_MIN_IMPULSE_PCT: float = 3.0     # ignore impulse swings smaller than 3%
FIB_GOLDEN_LOW:      float = 0.618   # golden pocket lower bound
FIB_GOLDEN_HIGH:     float = 0.786   # golden pocket upper bound
FIB_TP_EXTENSION:    float = 1.272   # TP at 1.272 Fibonacci extension
FIB_SL_BUFFER_PCT:   float = 0.5     # % buffer beyond swing anchor for SL

# ── Signal quality gates ──────────────────────────────────────────
MIN_TP_ROI_PCT: float = 50.0   # reject signals where TP ROI < 50%
MIN_RR_RATIO:   float = 2.0    # reject signals where reward/risk < 2

# ── Scheduler ─────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 30
SIGNAL_EXPIRE_HOURS:     int = 4
MAX_CONCURRENT_SIGNALS:  int = 3

# ── MEXC Futures REST API ──────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ───────────────────────────────────────────────────────
DB_PATH = "signals.db"
