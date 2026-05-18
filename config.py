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

# Stable swing mode:
# Keep the pool smaller to reduce weak/noisy low-quality setups.
TOP_N_COINS:              int   = 30
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── Stateful SMC Strategy ─────────────────────────────────────────
# Full scan detects setups.
# Monitor waits for order block retest and fires signal.
#
# Stable swing setup:
# 4H = higher-timeframe market structure bias.
# 1H = sweep + displacement + micro BOS + OB retest entry.
TREND_TF: str = "4h"
ENTRY_TF: str = "1h"

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

# Micro BOS confirmation
# After sweep + displacement, the displacement candle must break recent minor structure.
# LONG  = close breaks recent minor high.
# SHORT = close breaks recent minor low.
MICRO_BOS_LOOKBACK: int = 8
MICRO_BOS_BUFFER_PCT: float = 0.00

# Order block
ORDER_BLOCK_LOOKBACK: int = 12

# OB touch tolerance
# Fixes the main issue from logs:
# WAIT_NO_OB_TOUCH was too high because price often comes close to the OB
# but does not touch the exact zone.
#
# 0.20 means:
# If price comes within 0.20% of the OB zone, it is treated as an OB retest candidate.
OB_TOUCH_TOLERANCE_PCT: float = 0.20

# Pending setup lifecycle
# 24 x 1H = 24 hours.
# Stable 4H/1H setups need more time than 5m setups to retest.
PENDING_SETUP_EXPIRE_CANDLES: int = 24
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Retest confirmation
# 1H candles are naturally larger than 5m candles.
# Keep this moderately relaxed, but not too loose.
MAX_SIGNAL_CANDLE_BODY_PCT: float = 2.50

# RR / SL limits
# Stable swing mode can use wider SL and better RR.
MIN_STRUCTURE_RR: float = 2.00
MAX_STRUCTURE_RR: float = 5.00

SL_BUFFER_PCT: float = 0.05
TP_BUFFER_PCT: float = 0.02

# 1H/4H structures often have wider SL distance than 5m setups.
MIN_SL_PCT: float = 0.30
MAX_SL_PCT: float = 4.00

# ── Quality filters ───────────────────────────────────────────────
# Only strong setups should be saved into pending_setups.
# Lower values create many waiting setups and more noise.
MIN_SIGNAL_SCORE: float = 85.0

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
# Stable swing mode:
# - Fewer concurrent signals
# - Longer cooldown per coin
# - Longer signal expiry
SIGNAL_COOLDOWN_MINUTES: int = 360
SIGNAL_EXPIRE_HOURS:     int = 72
MAX_CONCURRENT_SIGNALS:  int = 3

# Full setup detection scan.
# 1H candle strategy does not need scanning every 5 minutes.
SETUP_SCAN_CRON_MINUTES: str = "*/15"

# Pending OB monitor.
SETUP_MONITOR_MINUTES: int = 5

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = 5

# Must match ENTRY_TF = 1h.
CANDLE_MINUTES: int = 60

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
# 4H/1H uses fewer candles than 5m/30m, but keep enough for strategy lookbacks.
CANDLE_CACHE_LIMIT: int = int(os.getenv("CANDLE_CACHE_LIMIT", "260"))

# First local/WebSocket test should use only one symbol.
# Later, main.py can use the real coin pool.
WS_TEST_SYMBOLS: list[str] = [
    symbol.strip()
    for symbol in os.getenv("WS_TEST_SYMBOLS", "BTC_USDT").split(",")
    if symbol.strip()
]

# WebSocket subscription batching.
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