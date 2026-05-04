import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass (optional) ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
TOP_N_COINS:   int      = 40      # coins to monitor
COIN_REFRESH_HOURS: int = 4

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE:    int   = 20
TP_ROI_PCT:  float = 5.0    # 5 % ROI → 0.25 % price move
SL_ROI_PCT:  float = 10.0   # 10 % ROI → 0.5 % price move

# Derived price-move percentages
TP_PRICE_PCT: float = TP_ROI_PCT / LEVERAGE / 100   # 0.0025
SL_PRICE_PCT: float = SL_ROI_PCT / LEVERAGE / 100   # 0.005

# ── Timeframes ───────────────────────────────────────────────────
ENTRY_TF: str = "15m"   # entry signal timeframe
MTF_4H:   str = "4h"    # momentum confirmation
MTF_1D:   str = "1d"    # macro trend

# ── EMA periods ──────────────────────────────────────────────────
EMA_FAST:   int = 9
EMA_SLOW:   int = 21
EMA_DAILY:  int = 50    # daily trend filter

# ── RSI ──────────────────────────────────────────────────────────
RSI_PERIOD:         int   = 14
RSI_4H_LONG_MIN:    float = 45.0   # 4H RSI floor for LONG signals
RSI_4H_SHORT_MAX:   float = 55.0   # 4H RSI ceiling for SHORT signals
RSI_ENTRY_OVERSOLD:   float = 35.0   # 15m extreme → triggers LONG
RSI_ENTRY_OVERBOUGHT: float = 65.0   # 15m extreme → triggers SHORT

# ── Volume filter ────────────────────────────────────────────────
VOLUME_MA_BARS:  int   = 20
VOLUME_MIN_MULT: float = 1.2   # loose — only filters very low-vol spikes

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60    # 1 h per coin
SIGNAL_EXPIRE_HOURS:     int = 4
MAX_CONCURRENT_SIGNALS:  int = 10

SCAN_CRON_MINUTES:     str = "1,16,31,46"   # every 15 min
OUTCOME_CHECK_MINUTES: int = 5
CANDLE_MINUTES:        int = 15

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"
