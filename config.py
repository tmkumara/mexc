import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ──────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))   # Sri Lanka Time (UTC+5:30)

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass (optional) ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
TOP_N_COINS:              int   = 40
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000   # minimum $5M daily volume
COIN_REFRESH_HOURS:       int   = 6

# ── NWE-RQK strategy ─────────────────────────────────────────────
NWE_H:           float = 8.0   # Lookback Window (bandwidth)
NWE_ALPHA:       float = 8.0   # Relative Weighting
NWE_SIZE:        int   = 25    # Start Regression at Bar (window size)
NWE_TF:          str   = "1h"  # Timeframe (1H closes, matching TradingView "60" setting)
NWE_KLINE_COUNT: int   = 80    # bars to fetch (NWE_SIZE + warm-up buffer)

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE:     int   = 20
TP_ROI_PCT:   float = 5.0    # target ROI %  →  entry move = TP_ROI / leverage
SL_ROI_PCT:   float = 4.0    # stop ROI %    →  entry move = SL_ROI / leverage
REWARD_RATIO: float = TP_ROI_PCT / SL_ROI_PCT   # 1.25 : 1 R:R

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 120   # same coin blocked 2h after signal
SIGNAL_EXPIRE_HOURS:     int = 8     # pending signals auto-expire after 8h
MAX_CONCURRENT_SIGNALS:  int = 10

SCAN_CRON_MINUTES:     str = "*/5" # every 5 minutes
SIGNALS_PER_SCAN:      int = 3
OUTCOME_CHECK_MINUTES: int = 5    # how often to poll for TP/SL hits
CANDLE_MINUTES:        int = 60   # 1H candle size in minutes
SCAN_WORKERS:          int = 8    # concurrent threads for NWE analysis

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"
