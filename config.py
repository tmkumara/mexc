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

# Phase 1 goal:
# Cleaner momentum-pullback scalper with more signals than strict SMC.
TOP_N_COINS:              int   = 60
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── Strategy: Momentum Pullback Scalper ───────────────────────────
# 1H = direction filter.
# 5m = momentum impulse + pullback + confirmation entry.
TREND_TF: str = "1h"
ENTRY_TF: str = "5m"

TREND_KLINE_COUNT:   int = 180
ENTRY_KLINE_COUNT:   int = 220
MONITOR_KLINE_COUNT: int = 80

# Trend filter
EMA_FAST_PERIOD: int = 20
EMA_SLOW_PERIOD: int = 50
TREND_SLOPE_LOOKBACK: int = 5

# Entry/momentum filters
ENTRY_LOOKBACK: int = 160
AVG_BODY_PERIOD: int = 20
AVG_VOLUME_PERIOD: int = 20

MOMENTUM_LOOKBACK: int = 10
MOMENTUM_BREAKOUT_LOOKBACK: int = 18
MOMENTUM_BODY_MULTIPLIER: float = 1.25
MOMENTUM_VOLUME_MULTIPLIER: float = 1.15
MOMENTUM_CLOSE_POSITION: float = 0.62

# Pullback zone from impulse range.
# LONG: wait for price to retrace into 38.2% - 70.5% of impulse candle.
# SHORT: same idea inverted.
PULLBACK_MIN_RETRACE: float = 0.382
PULLBACK_MAX_RETRACE: float = 0.705

# Entry confirmation
CONFIRM_BREAK_PREVIOUS_CANDLE: bool = True
CONFIRM_VOLUME_MULTIPLIER: float = 0.80
MAX_CONFIRM_CANDLE_BODY_PCT: float = 1.20

# Fixed scalping risk model.
# At 20x leverage:
#   0.55% price TP ≈ 11% ROI
#   0.30% price SL ≈ 6% ROI loss
TAKE_PROFIT_PRICE_PCT: float = 0.55
STOP_LOSS_PRICE_PCT:   float = 0.30

# Emergency limits. These prevent crazy tiny/large signals.
MIN_TP_ROI_PCT: float = 8.0
MAX_TP_ROI_PCT: float = 18.0
MIN_SL_ROI_PCT: float = 4.0
MAX_SL_ROI_PCT: float = 9.0

# Pending setup lifecycle
PENDING_SETUP_EXPIRE_CANDLES: int = 12   # 12 x 5m = 60 minutes
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Signal quality
MIN_SIGNAL_SCORE: float = 68.0
SETUPS_PER_SCAN: int = 8

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20

# Kept for compatibility with old report/status references.
TP_ROI_PCT: float = TAKE_PROFIT_PRICE_PCT * LEVERAGE
SL_ROI_PCT: float = STOP_LOSS_PRICE_PCT * LEVERAGE
REWARD_RATIO: float = TAKE_PROFIT_PRICE_PCT / STOP_LOSS_PRICE_PCT

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 25
SIGNAL_EXPIRE_HOURS:     int = 4
MAX_CONCURRENT_SIGNALS:  int = 5

# Full setup detection scan.
SETUP_SCAN_CRON_MINUTES: str = "*/1"

# Pending pullback monitor.
SETUP_MONITOR_MINUTES: int = 1

# Outcome checker.
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5

# Telegram messages per monitor cycle.
SIGNALS_PER_SCAN: int = 3

# Keep modest to avoid MEXC rate limits.
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")
