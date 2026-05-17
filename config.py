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

# Quality mode:
# Keep the pool smaller to reduce weak/noisy low-quality setups.
TOP_N_COINS:              int   = 30
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── Stateful SMC Strategy ─────────────────────────────────────────
# Full scan detects setups.
# Monitor waits for order block retest and fires signal.
#
# Higher timeframe bias:
# 30m gives cleaner structure than 15m while still producing enough setups.
TREND_TF: str = "30m"
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
# 12 x 5m = 60 minutes.
# Shorter expiry helps avoid old/weak mitigations firing late.
PENDING_SETUP_EXPIRE_CANDLES: int = 12
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Retest confirmation
MAX_SIGNAL_CANDLE_BODY_PCT: float = 1.20

# RR / SL limits
MIN_STRUCTURE_RR: float = 2.00
MAX_STRUCTURE_RR: float = 5.00

SL_BUFFER_PCT: float = 0.05
TP_BUFFER_PCT: float = 0.02

MIN_SL_PCT: float = 0.20
MAX_SL_PCT: float = 2.00

# ── Quality filters ───────────────────────────────────────────────
# Only strong setups should be saved into pending_setups.
# Lower values create many waiting setups and more noise.
MIN_SIGNAL_SCORE: float = 90.0

# Maximum number of pending setups to save from each full setup scan.
# This is different from SIGNALS_PER_SCAN, which limits actual Telegram entries.
SETUPS_PER_SCAN: int = 3

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# Kept for compatibility with old report/status references.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
# Quality mode:
# - Fewer concurrent signals
# - Longer cooldown per coin
# - Only best signal per scan
SIGNAL_COOLDOWN_MINUTES: int = 180
SIGNAL_EXPIRE_HOURS:     int = 6
MAX_CONCURRENT_SIGNALS:  int = 3

# Full setup detection scan.
SETUP_SCAN_CRON_MINUTES: str = "*/5"

# Pending OB monitor.
SETUP_MONITOR_MINUTES: int = 1

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

SIGNALS_PER_SCAN: int = 1

# Keep modest to avoid MEXC rate limits.
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── MEXC Futures WebSocket API ────────────────────────────────────
ENABLE_WEBSOCKET: bool = os.getenv("ENABLE_WEBSOCKET", "true").lower() == "true"

# Current MEXC Futures WebSocket base URL.
MEXC_WS_URL: str = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")

# Number of candles to keep in memory per symbol + interval.
CANDLE_CACHE_LIMIT: int = int(os.getenv("CANDLE_CACHE_LIMIT", "60"))

# First local/WebSocket test should use only one symbol.
# Later, main.py can use the real coin pool.
WS_TEST_SYMBOLS: list[str] = [
    symbol.strip()
    for symbol in os.getenv("WS_TEST_SYMBOLS", "BTC_USDT").split(",")
    if symbol.strip()
]

# WebSocket subscription batching.
# 30 coins x 2 intervals = 60 subscriptions, but for safety we can batch later.
WS_SUBSCRIPTION_BATCH_SIZE: int = int(os.getenv("WS_SUBSCRIPTION_BATCH_SIZE", "20"))
WS_RECONNECT_DELAY_SECONDS: int = int(os.getenv("WS_RECONNECT_DELAY_SECONDS", "5"))
WS_PING_INTERVAL_SECONDS: int = int(os.getenv("WS_PING_INTERVAL_SECONDS", "20"))
WS_PING_TIMEOUT_SECONDS: int = int(os.getenv("WS_PING_TIMEOUT_SECONDS", "10"))

# App timeframe -> MEXC Futures WebSocket interval.
MEXC_INTERVAL_MAP: dict[str, str] = {
    "1m":  "Min1",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "1d":  "Day1",
}

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"