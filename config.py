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
TOP_N_COINS:              int   = 80
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── SMC / Market Structure Strategy ───────────────────────────────
# Trend timeframe = market structure bias
# Entry timeframe = liquidity sweep + displacement + order block retest
TREND_TF: str = "15m"
ENTRY_TF: str = "5m"

TREND_KLINE_COUNT: int = 220
ENTRY_KLINE_COUNT: int = 260

# Swing detection
# For 5m scalping, 3/2 is faster than 5/5.
SWING_LEFT:  int = 3
SWING_RIGHT: int = 2

# How far back to search for valid SMC setup
STRUCTURE_LOOKBACK: int = 160
ENTRY_LOOKBACK:     int = 180

# Liquidity sweep settings
SWEEP_LOOKBACK: int = 18

# Displacement settings
AVG_BODY_PERIOD:               int   = 20
DISPLACEMENT_BODY_MULTIPLIER:  float = 1.4
DISPLACEMENT_CLOSE_POSITION:   float = 0.65
# LONG displacement: close should be in top 65% of candle range
# SHORT displacement: close should be in bottom 35% of candle range

# Order block settings
ORDER_BLOCK_LOOKBACK: int = 12

# Retest / mitigation settings
# Retest candle must touch OB zone and close away from it.
RETEST_LOOKBACK_AFTER_DISPLACEMENT: int = 30

# Entry filters
MAX_SIGNAL_CANDLE_BODY_PCT: float = 1.20
MIN_STRUCTURE_RR:           float = 1.50
MAX_STRUCTURE_RR:           float = 5.00

# Stop/target buffers
SL_BUFFER_PCT: float = 0.05
TP_BUFFER_PCT: float = 0.02

# SL safety limits as % of entry
MIN_SL_PCT: float = 0.20
MAX_SL_PCT: float = 2.00

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# These are kept for compatibility with reports/bot display.
# Actual TP/SL is dynamically calculated from market structure.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60
SIGNAL_EXPIRE_HOURS:     int = 6
MAX_CONCURRENT_SIGNALS:  int = 10

# 5m entry timeframe:
# - scan every 1 minute to catch newly closed 5m candles
# - outcome checker runs every 1 minute
# - candle size is 5 minutes
SCAN_CRON_MINUTES:     str = "*/1"
SIGNALS_PER_SCAN:      int = 3
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

# Keep modest to avoid MEXC rate limits when scanning many coins
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"