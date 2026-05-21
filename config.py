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
COIN_POOL_MIN_VOLUME_USD: float = 500_000
COIN_REFRESH_HOURS:       int   = 6

# ── Smart Coin Ranking ────────────────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool = True

COIN_RANK_CANDIDATE_MULTIPLIER: int = 4
COIN_RANK_MAX_CANDIDATES: int = 180

COIN_RANK_TIMEFRAME: str = "5m"
COIN_RANK_KLINE_COUNT: int = 36
COIN_RANK_WORKERS: int = 3

COIN_RANK_MIN_LAST_PRICE: float = 0.000001
COIN_RANK_MIN_RANGE_PCT: float = 0.05
COIN_RANK_MAX_RANGE_PCT: float = 18.00
COIN_RANK_MAX_ABS_MOVE_PCT: float = 22.00

COIN_RANK_VOLUME_WEIGHT: float = 40.0
COIN_RANK_VOLATILITY_WEIGHT: float = 25.0
COIN_RANK_TREND_WEIGHT: float = 15.0
COIN_RANK_LIQUIDITY_WEIGHT: float = 20.0

COIN_RANK_OVEREXTENSION_PENALTY: float = 25.0
COIN_RANK_LOW_ACTIVITY_PENALTY: float = 15.0

# ── New Strategy: Squeeze Momentum + WaveTrend + Supertrend ────────
STRATEGY_NAME: str = "Squeeze Momentum WT Supertrend"

ENTRY_TF: str = "5m"
TREND_TF: str = ENTRY_TF

ENTRY_KLINE_COUNT: int = 220
TREND_KLINE_COUNT: int = ENTRY_KLINE_COUNT
MONITOR_KLINE_COUNT: int = 80

# WaveTrend settings from Pine Script
WT_CHANNEL_LENGTH: int = 10
WT_AVERAGE_LENGTH: int = 21
WT_SIGNAL_LENGTH: int = 4

WT_OVERBOUGHT_LEVEL_1: float = 60.0
WT_OVERBOUGHT_LEVEL_2: float = 53.0
WT_OVERSOLD_LEVEL_1: float = -60.0
WT_OVERSOLD_LEVEL_2: float = -53.0

# Supertrend settings from Pine Script
SUPERTREND_ATR_PERIOD: int = 10
SUPERTREND_FACTOR: float = 2.5

# Squeeze Momentum settings from Pine Script
SQUEEZE_BB_LENGTH: int = 20
SQUEEZE_BB_MULT: float = 2.0
SQUEEZE_KC_LENGTH: int = 20
SQUEEZE_KC_MULT: float = 1.5
SQUEEZE_USE_TRUE_RANGE: bool = True
SQUEEZE_SIGNAL_LENGTH: int = 5

SQUEEZE_LOWER_THRESHOLD: float = -1.0
SQUEEZE_UPPER_THRESHOLD: float = 1.0

# Signal behavior
USE_RECENT_SQUEEZE_RELEASE: bool = True
RECENT_SQUEEZE_RELEASE_BARS: int = 3

USE_WAVETREND_CROSS_CONFIRMATION: bool = True
RECENT_WT_CROSS_BARS: int = 3

REQUIRE_SUPERTREND_ALIGNMENT: bool = True
REQUIRE_SQUEEZE_RELEASE: bool = True
REQUIRE_WAVETREND_ALIGNMENT: bool = True

MIN_SIGNAL_SCORE: float = 72.0
SIGNALS_PER_SCAN: int = 2

# Risk model based on Pine Script ATR risk lines
# Pine script shows TP1/TP2/TP3 = ATR x 1/2/3 and SL = ATR x 3.
# Since bot supports one TP, we use TP2 by default.
TARGET_ATR_MULTIPLIER: float = 2.0
STOP_LOSS_ATR_MULTIPLIER: float = 3.0

MIN_TP_ROI_PCT: float = 2.0
MAX_TP_ROI_PCT: float = 25.0
MIN_SL_ROI_PCT: float = 2.0
MAX_SL_ROI_PCT: float = 25.0

MAX_SIGNAL_CANDLE_BODY_PCT: float = 2.8
MAX_RECENT_MOVE_PCT: float = 8.0
RECENT_MOVE_LOOKBACK: int = 36

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# These are dynamic in the new ATR strategy but kept for reports/status compatibility.
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0
REWARD_RATIO: float = TARGET_ATR_MULTIPLIER / STOP_LOSS_ATR_MULTIPLIER

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 25
SIGNAL_EXPIRE_HOURS:     int = 3
MAX_CONCURRENT_SIGNALS:  int = 3

SETUP_SCAN_CRON_MINUTES: str = "*/1"
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

# Kept for old status/webui compatibility.
SETUP_MONITOR_MINUTES: int = 0
SETUPS_PER_SCAN: int = 0

SCAN_WORKERS: int = 4

# ── Backward-compatible constants ─────────────────────────────────
EMA_FAST_PERIOD: int = 20
EMA_SLOW_PERIOD: int = 50
MOMENTUM_BODY_MULTIPLIER: float = 1.0
MOMENTUM_VOLUME_MULTIPLIER: float = 1.0
TAKE_PROFIT_PRICE_PCT: float = 0.0
STOP_LOSS_PRICE_PCT: float = 0.0

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")