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
TOP_N_COINS:              int   = 40
COIN_POOL_MIN_VOLUME_USD: float = 2_000_000
COIN_REFRESH_HOURS:       int   = 6

# ── Phase 2: Smart Coin Ranking ───────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool = True
COIN_RANK_CANDIDATE_MULTIPLIER: int = 3
COIN_RANK_MAX_CANDIDATES: int = 120
COIN_RANK_TIMEFRAME: str = "5m"
COIN_RANK_KLINE_COUNT: int = 48
COIN_RANK_WORKERS: int = 3
COIN_RANK_MIN_LAST_PRICE: float = 0.000001
COIN_RANK_MIN_RANGE_PCT: float = 0.25
COIN_RANK_MAX_RANGE_PCT: float = 7.50
COIN_RANK_MAX_ABS_MOVE_PCT: float = 8.00
COIN_RANK_VOLUME_WEIGHT: float = 40.0
COIN_RANK_VOLATILITY_WEIGHT: float = 25.0
COIN_RANK_TREND_WEIGHT: float = 15.0
COIN_RANK_LIQUIDITY_WEIGHT: float = 20.0
COIN_RANK_OVEREXTENSION_PENALTY: float = 45.0
COIN_RANK_LOW_ACTIVITY_PENALTY: float = 25.0

# ── Strategy: AMD + FVG Distribution v2.2 ─────────────────────────
# 1h = soft directional filter. 5m = AMD detection and entry.
TREND_TF: str = "1h"
ENTRY_TF: str = "5m"
TREND_KLINE_COUNT:   int = 180
ENTRY_KLINE_COUNT:   int = 240
MONITOR_KLINE_COUNT: int = 90

# Trend filter compatibility / soft bias.
EMA_FAST_PERIOD: int = 20
EMA_SLOW_PERIOD: int = 50
TREND_SLOPE_LOOKBACK: int = 5

# AMD accumulation settings.
AMD_ACCUMULATION_MIN_CANDLES: int = 10
AMD_ACCUMULATION_MAX_CANDLES: int = 26
AMD_MAX_ACCUMULATION_RANGE_PCT: float = 1.20
AMD_MIN_ACCUMULATION_RANGE_PCT: float = 0.18
AMD_RANGE_END_LOOKBACK: int = 18

# Manipulation / liquidity sweep settings.
AMD_SWEEP_LOOKBACK: int = 10
AMD_MIN_SWEEP_PCT: float = 0.08
AMD_MAX_SWEEP_PCT: float = 1.20
AMD_SWEEP_CLOSE_BACK_INSIDE: bool = True

# FVG settings.
AMD_FVG_LOOKBACK_AFTER_SWEEP: int = 5
AMD_MIN_FVG_SIZE_PCT: float = 0.035
AMD_MAX_FVG_SIZE_PCT: float = 0.55
AMD_DISTRIBUTION_BODY_MULTIPLIER: float = 1.35
AMD_DISTRIBUTION_CLOSE_POSITION: float = 0.65

# Retest / confirmation settings.
AMD_REQUIRE_FVG_RETEST: bool = True
AMD_CONFIRM_CLOSE_BEYOND_FVG: bool = True
AMD_CONFIRM_BREAK_PREVIOUS_CANDLE: bool = True
AMD_MAX_CONFIRM_DISTANCE_FROM_FVG_PCT: float = 0.18
AMD_INVALIDATE_BEYOND_SWEEP_BUFFER_PCT: float = 0.04

# Candle stats.
ENTRY_LOOKBACK: int = 180
AVG_BODY_PERIOD: int = 20
AVG_VOLUME_PERIOD: int = 20
AMD_MIN_VOLUME_MULTIPLIER: float = 1.05

# Momentum compatibility constants used by status/UI. AMD uses its own rules.
MOMENTUM_LOOKBACK: int = 10
MOMENTUM_BREAKOUT_LOOKBACK: int = 18
MOMENTUM_BODY_MULTIPLIER: float = 1.65
MOMENTUM_VOLUME_MULTIPLIER: float = 1.45
MOMENTUM_CLOSE_POSITION: float = 0.68
MAX_IMPULSE_CANDLE_BODY_PCT: float = 1.80
MAX_ENTRY_EXTENSION_FROM_EMA_PCT: float = 0.95
MAX_RECENT_RUNUP_PCT: float = 4.50
MAX_RECENT_RUNDOWN_PCT: float = 4.50
PULLBACK_WAVE_LOOKBACK: int = 36
PULLBACK_MIN_RETRACE: float = 0.382
PULLBACK_MAX_RETRACE: float = 0.618
CONFIRM_BREAK_PREVIOUS_CANDLE: bool = True
CONFIRM_VOLUME_MULTIPLIER: float = 1.00
MAX_CONFIRM_CANDLE_BODY_PCT: float = 0.85
MAX_CONFIRM_DISTANCE_FROM_ZONE_PCT: float = 0.20

# Fixed scalping risk model.
# At 20x leverage:
#   0.60% price TP ≈ 12% ROI
#   0.35% price SL ≈ 7% ROI loss
TAKE_PROFIT_PRICE_PCT: float = 0.60
STOP_LOSS_PRICE_PCT:   float = 0.35
MIN_TP_ROI_PCT: float = 9.0
MAX_TP_ROI_PCT: float = 18.0
MIN_SL_ROI_PCT: float = 5.0
MAX_SL_ROI_PCT: float = 10.0

# Pending setup lifecycle.
PENDING_SETUP_EXPIRE_CANDLES: int = 9   # 45 minutes on 5m
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Signal quality.
MIN_SIGNAL_SCORE: float = 82.0
SETUPS_PER_SCAN: int = 2

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20
TP_ROI_PCT: float = TAKE_PROFIT_PRICE_PCT * LEVERAGE
SL_ROI_PCT: float = STOP_LOSS_PRICE_PCT * LEVERAGE
REWARD_RATIO: float = TAKE_PROFIT_PRICE_PCT / STOP_LOSS_PRICE_PCT

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60
SIGNAL_EXPIRE_HOURS:     int = 3
MAX_CONCURRENT_SIGNALS:  int = 2
SETUP_SCAN_CRON_MINUTES: str = "*/1"
SETUP_MONITOR_MINUTES: int = 1
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5
SIGNALS_PER_SCAN: int = 1
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")