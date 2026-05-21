import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ──────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass optional API ────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── MEXC Futures WebSocket ────────────────────────────────────────
ENABLE_WEBSOCKET: bool = os.getenv("ENABLE_WEBSOCKET", "true").lower() == "true"
REST_FALLBACK_ENABLED: bool = os.getenv("REST_FALLBACK_ENABLED", "true").lower() == "true"

MEXC_WS_URL: str = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")

MEXC_INTERVAL_MAP: dict[str, str] = {
    "1m": "Min1",
    "3m": "Min1",
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h": "Min60",
    "4h": "Hour4",
    "1d": "Day1",
}

WS_RECONNECT_DELAY_SECONDS: int = int(os.getenv("WS_RECONNECT_DELAY_SECONDS", "5"))
WS_PING_INTERVAL_SECONDS: int = int(os.getenv("WS_PING_INTERVAL_SECONDS", "20"))
WS_PING_TIMEOUT_SECONDS: int = int(os.getenv("WS_PING_TIMEOUT_SECONDS", "10"))
WS_TEST_SYMBOLS: list[str] = []

CANDLE_CACHE_LIMIT: int = int(os.getenv("CANDLE_CACHE_LIMIT", "220"))
CANDLE_BOOTSTRAP_WORKERS: int = int(os.getenv("CANDLE_BOOTSTRAP_WORKERS", "6"))

# ── WebSocket heartbeat / subscription tuning ─────────────────────
WS_APP_HEARTBEAT_ENABLED: bool = os.getenv("WS_APP_HEARTBEAT_ENABLED", "true").lower() == "true"
WS_APP_HEARTBEAT_SECONDS: int = int(os.getenv("WS_APP_HEARTBEAT_SECONDS", "15"))
WS_SUBSCRIBE_DELAY_SECONDS: float = float(os.getenv("WS_SUBSCRIBE_DELAY_SECONDS", "0.12"))
WS_SUBSCRIBE_BATCH_SIZE: int = int(os.getenv("WS_SUBSCRIBE_BATCH_SIZE", "10"))
WS_SUBSCRIBE_BATCH_PAUSE_SECONDS: float = float(os.getenv("WS_SUBSCRIBE_BATCH_PAUSE_SECONDS", "0.80"))

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "XAUT_USDT",
    "XPT_USDT",
    "XAU_USDT",
    "XAG_USDT",
    "XPD_USDT",
    "XBR_USDT",
    "WTI_USDT",
}

TOP_N_COINS: int = 80
COIN_POOL_MIN_VOLUME_USD: float = 750_000
COIN_REFRESH_HOURS: int = 6

FUTURES_ONLY: bool = True
CRYPTO_FUTURES_ONLY: bool = True
QUOTE_CURRENCY: str = "USDT"
REQUIRE_SYMBOL_IN_CONTRACT_DETAIL: bool = True
REQUIRE_SYMBOL_IN_TICKER: bool = True

# ── Smart Coin Ranking ────────────────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool = True

COIN_RANK_CANDIDATE_MULTIPLIER: int = 4
COIN_RANK_MAX_CANDIDATES: int = 180

COIN_RANK_TIMEFRAME: str = "15m"
COIN_RANK_KLINE_COUNT: int = 48
COIN_RANK_WORKERS: int = 3

COIN_RANK_MIN_LAST_PRICE: float = 0.000001
COIN_RANK_MIN_RANGE_PCT: float = 0.08
COIN_RANK_MAX_RANGE_PCT: float = 20.00
COIN_RANK_MAX_ABS_MOVE_PCT: float = 24.00

COIN_RANK_VOLUME_WEIGHT: float = 40.0
COIN_RANK_VOLATILITY_WEIGHT: float = 25.0
COIN_RANK_TREND_WEIGHT: float = 15.0
COIN_RANK_LIQUIDITY_WEIGHT: float = 20.0

COIN_RANK_OVEREXTENSION_PENALTY: float = 25.0
COIN_RANK_LOW_ACTIVITY_PENALTY: float = 15.0

# ── Strategy: Stable 15m Breakout + Retest + 1h Trend ─────────────
STRATEGY_NAME: str = "Stable 15m Breakout Retest EMA/VWAP Scalper"

# 1h = direction filter, 15m = entry/retest
TREND_TF: str = "1h"
ENTRY_TF: str = "15m"

TREND_KLINE_COUNT: int = 160
ENTRY_KLINE_COUNT: int = 180
MONITOR_KLINE_COUNT: int = 100

BREAKOUT_LOOKBACK: int = 20
RETEST_MAX_CANDLES: int = 3

EMA_PERIOD: int = 50
TREND_EMA_PERIOD: int = 50
VWAP_LOOKBACK_BARS: int = 64

ATR_PERIOD: int = 14
ATR_SL_BUFFER_MULTIPLIER: float = 0.35

MIN_RR: float = 1.20
TARGET_RR: float = 1.30
MAX_RR: float = 1.60

MAX_BREAKOUT_CANDLE_BODY_PCT: float = 1.80
MAX_RETEST_CANDLE_BODY_PCT: float = 1.30

MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT: float = 0.35
MAX_DISTANCE_FROM_VWAP_PCT: float = 1.20

MIN_VOLUME_MULTIPLIER: float = 1.00
AVG_VOLUME_PERIOD: int = 20

# Stronger score for stable mode
MIN_SIGNAL_SCORE: float = 78.0
SETUPS_PER_SCAN: int = 2
SIGNALS_PER_SCAN: int = 1

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 10

TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = TARGET_RR

MIN_TP_ROI_PCT: float = 2.5
MAX_TP_ROI_PCT: float = 18.0
MIN_SL_ROI_PCT: float = 2.0
MAX_SL_ROI_PCT: float = 12.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 90
SIGNAL_EXPIRE_HOURS: int = 6
MAX_CONCURRENT_SIGNALS: int = 1

SETUP_SCAN_CRON_MINUTES: str = "*/5"
SETUP_MONITOR_MINUTES: int = 1
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES: int = 15

PENDING_SETUP_EXPIRE_CANDLES: int = RETEST_MAX_CANDLES

SCAN_WORKERS: int = 4

# ── Backward-compatible constants for old imports/UI ──────────────
EMA_FAST_PERIOD: int = EMA_PERIOD
EMA_SLOW_PERIOD: int = EMA_PERIOD
MOMENTUM_BODY_MULTIPLIER: float = 1.0
MOMENTUM_VOLUME_MULTIPLIER: float = MIN_VOLUME_MULTIPLIER
TAKE_PROFIT_PRICE_PCT: float = 0.0
STOP_LOSS_PRICE_PCT: float = 0.0

# Old Squeeze/WaveTrend compatibility values.
WT_CHANNEL_LENGTH: int = 10
WT_AVERAGE_LENGTH: int = 21
WT_SIGNAL_LENGTH: int = 4
WT_OVERBOUGHT_LEVEL_1: float = 60.0
WT_OVERBOUGHT_LEVEL_2: float = 53.0
WT_OVERSOLD_LEVEL_1: float = -60.0
WT_OVERSOLD_LEVEL_2: float = -53.0
SUPERTREND_ATR_PERIOD: int = 10
SUPERTREND_FACTOR: float = 2.5
SQUEEZE_BB_LENGTH: int = 20
SQUEEZE_BB_MULT: float = 2.0
SQUEEZE_KC_LENGTH: int = 20
SQUEEZE_KC_MULT: float = 1.5
SQUEEZE_USE_TRUE_RANGE: bool = True
SQUEEZE_SIGNAL_LENGTH: int = 5
SQUEEZE_LOWER_THRESHOLD: float = -1.0
SQUEEZE_UPPER_THRESHOLD: float = 1.0
USE_RECENT_SQUEEZE_RELEASE: bool = True
RECENT_SQUEEZE_RELEASE_BARS: int = 3
USE_WAVETREND_CROSS_CONFIRMATION: bool = True
RECENT_WT_CROSS_BARS: int = 3
REQUIRE_SUPERTREND_ALIGNMENT: bool = True
REQUIRE_SQUEEZE_RELEASE: bool = True
REQUIRE_WAVETREND_ALIGNMENT: bool = True
TARGET_ATR_MULTIPLIER: float = 2.0
STOP_LOSS_ATR_MULTIPLIER: float = 3.0
MAX_SIGNAL_CANDLE_BODY_PCT: float = MAX_BREAKOUT_CANDLE_BODY_PCT
MAX_RECENT_MOVE_PCT: float = 8.0
RECENT_MOVE_LOOKBACK: int = 36

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")