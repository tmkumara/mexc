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

# ── Stateful SMC Strategy ─────────────────────────────────────────
# Full scan detects setups.
# Monitor waits for order block retest and fires signal.
TREND_TF: str = "15m"
ENTRY_TF: str = "5m"

TREND_KLINE_COUNT: int = 220
ENTRY_KLINE_COUNT: int = 220
MONITOR_KLINE_COUNT: int = 40

# Swing detection
SWING_LEFT:  int = 3
SWING_RIGHT: int = 2

STRUCTURE_LOOKBACK: int = 160
ENTRY_LOOKBACK:     int = 180

# Liquidity sweep
SWEEP_LOOKBACK: int = 18

# Displacement candle
AVG_BODY_PERIOD:              int   = 20
DISPLACEMENT_BODY_MULTIPLIER: float = 1.4
DISPLACEMENT_CLOSE_POSITION:  float = 0.65

# Order block
ORDER_BLOCK_LOOKBACK: int = 12

# Pending setup lifecycle
PENDING_SETUP_EXPIRE_CANDLES: int = 24   # 24 x 5m = 120 minutes
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Retest confirmation
MAX_SIGNAL_CANDLE_BODY_PCT: float = 1.20

# RR / SL limits
MIN_STRUCTURE_RR: float = 1.50
MAX_STRUCTURE_RR: float = 5.00

SL_BUFFER_PCT: float = 0.05
TP_BUFFER_PCT: float = 0.02

MIN_SL_PCT: float = 0.20
MAX_SL_PCT: float = 2.00

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# Kept for compatibility with old report/status references.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60
SIGNAL_EXPIRE_HOURS:     int = 6
MAX_CONCURRENT_SIGNALS:  int = 10

# Full setup detection scan.
SETUP_SCAN_CRON_MINUTES: str = "*/5"

# Pending OB monitor.
SETUP_MONITOR_MINUTES: int = 1

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

SIGNALS_PER_SCAN: int = 3

# Keep modest to avoid MEXC rate limits.
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"