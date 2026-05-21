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
TOP_N_COINS:              int   = 60
COIN_POOL_MIN_VOLUME_USD: float = 1_500_000
COIN_REFRESH_HOURS:       int   = 6

# ── Phase 2: Smart Coin Ranking ───────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool = True
COIN_RANK_CANDIDATE_MULTIPLIER: int = 3
COIN_RANK_MAX_CANDIDATES: int = 140
COIN_RANK_TIMEFRAME: str = "5m"
COIN_RANK_KLINE_COUNT: int = 48
COIN_RANK_WORKERS: int = 3
COIN_RANK_MIN_LAST_PRICE: float = 0.000001
COIN_RANK_MIN_RANGE_PCT: float = 0.15
COIN_RANK_MAX_RANGE_PCT: float = 12.00
COIN_RANK_MAX_ABS_MOVE_PCT: float = 14.00
COIN_RANK_VOLUME_WEIGHT: float = 40.0
COIN_RANK_VOLATILITY_WEIGHT: float = 25.0
COIN_RANK_TREND_WEIGHT: float = 15.0
COIN_RANK_LIQUIDITY_WEIGHT: float = 20.0
COIN_RANK_OVEREXTENSION_PENALTY: float = 35.0
COIN_RANK_LOW_ACTIVITY_PENALTY: float = 20.0

# ── Strategy: VWAP Liquidity Sweep Scalper v3 ─────────────────────
# 1h = directional filter.
# 5m = liquidity sweep + VWAP/EMA reclaim confirmation.
TREND_TF: str = "1h"
ENTRY_TF: str = "5m"
TREND_KLINE_COUNT:   int = 180
ENTRY_KLINE_COUNT:   int = 220
MONITOR_KLINE_COUNT: int = 90

# Trend filter
EMA_FAST_PERIOD: int = 20
EMA_SLOW_PERIOD: int = 50
TREND_SLOPE_LOOKBACK: int = 5
REQUIRE_TREND_ALIGNMENT: bool = True

# VWAP / EMA confirmation
VWAP_LOOKBACK_BARS: int = 96        # 96 x 5m = 8h rolling VWAP
ENTRY_EMA_FAST_PERIOD: int = 9
ENTRY_EMA_SLOW_PERIOD: int = 21
MAX_ENTRY_DISTANCE_FROM_VWAP_PCT: float = 0.85
MIN_DISTANCE_TO_VWAP_TP_PCT: float = 0.12

# Liquidity sweep setup
ENTRY_LOOKBACK: int = 180
LIQUIDITY_LOOKBACK: int = 24
SWEEP_SCAN_LOOKBACK: int = 8
MIN_SWEEP_PCT: float = 0.06
MAX_SWEEP_PCT: float = 1.60
SWEEP_CLOSE_BACK_INSIDE: bool = True
MIN_REJECTION_WICK_RATIO: float = 0.38

# Candle stats / confirmation
AVG_BODY_PERIOD: int = 20
AVG_VOLUME_PERIOD: int = 20
MIN_SWEEP_VOLUME_MULTIPLIER: float = 0.95
CONFIRM_VOLUME_MULTIPLIER: float = 0.85
CONFIRM_BREAK_PREVIOUS_CANDLE: bool = True
MAX_CONFIRM_CANDLE_BODY_PCT: float = 1.15
MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT: float = 0.45

# Anti-chase / safety
MAX_RECENT_MOVE_PCT: float = 5.50
RECENT_MOVE_LOOKBACK: int = 36
INVALIDATE_SWEEP_BUFFER_PCT: float = 0.05

# Fixed scalp risk model.
# At 20x leverage:
#   0.38% price TP ≈ 7.6% ROI
#   0.28% price SL ≈ 5.6% ROI loss
TAKE_PROFIT_PRICE_PCT: float = 0.38
STOP_LOSS_PRICE_PCT:   float = 0.28
MIN_TP_ROI_PCT: float = 5.0
MAX_TP_ROI_PCT: float = 12.0
MIN_SL_ROI_PCT: float = 3.5
MAX_SL_ROI_PCT: float = 8.0

# Pending setup lifecycle.
PENDING_SETUP_EXPIRE_CANDLES: int = 10    # 50 minutes on 5m
MAX_PENDING_SETUPS_PER_SYMBOL: int = 1

# Signal quality / frequency.
MIN_SIGNAL_SCORE: float = 72.0
SETUPS_PER_SCAN: int = 4

# ── Backward-compatible constants for bot/status/webui text ───────
# Kept so existing UI/status imports don't break while strategy is v3.
MOMENTUM_LOOKBACK: int = 10
MOMENTUM_BREAKOUT_LOOKBACK: int = 18
MOMENTUM_BODY_MULTIPLIER: float = 1.20
MOMENTUM_VOLUME_MULTIPLIER: float = MIN_SWEEP_VOLUME_MULTIPLIER
MOMENTUM_CLOSE_POSITION: float = 0.58
MAX_IMPULSE_CANDLE_BODY_PCT: float = 2.50
MAX_ENTRY_EXTENSION_FROM_EMA_PCT: float = MAX_ENTRY_DISTANCE_FROM_VWAP_PCT
MAX_RECENT_RUNUP_PCT: float = MAX_RECENT_MOVE_PCT
MAX_RECENT_RUNDOWN_PCT: float = MAX_RECENT_MOVE_PCT
PULLBACK_WAVE_LOOKBACK: int = RECENT_MOVE_LOOKBACK
PULLBACK_MIN_RETRACE: float = 0.236
PULLBACK_MAX_RETRACE: float = 0.618
MAX_CONFIRM_DISTANCE_FROM_ZONE_PCT: float = MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT

# AMD compatibility constants. Not used by v3 strategy.
AMD_ACCUMULATION_MIN_CANDLES: int = 10
AMD_ACCUMULATION_MAX_CANDLES: int = 26
AMD_MAX_ACCUMULATION_RANGE_PCT: float = 1.20
AMD_MIN_ACCUMULATION_RANGE_PCT: float = 0.18
AMD_RANGE_END_LOOKBACK: int = 18
AMD_SWEEP_LOOKBACK: int = 10
AMD_MIN_SWEEP_PCT: float = MIN_SWEEP_PCT
AMD_MAX_SWEEP_PCT: float = MAX_SWEEP_PCT
AMD_SWEEP_CLOSE_BACK_INSIDE: bool = SWEEP_CLOSE_BACK_INSIDE
AMD_FVG_LOOKBACK_AFTER_SWEEP: int = 5
AMD_MIN_FVG_SIZE_PCT: float = 0.035
AMD_MAX_FVG_SIZE_PCT: float = 0.55
AMD_DISTRIBUTION_BODY_MULTIPLIER: float = 1.35
AMD_DISTRIBUTION_CLOSE_POSITION: float = 0.65
AMD_REQUIRE_FVG_RETEST: bool = False
AMD_CONFIRM_CLOSE_BEYOND_FVG: bool = False
AMD_CONFIRM_BREAK_PREVIOUS_CANDLE: bool = CONFIRM_BREAK_PREVIOUS_CANDLE
AMD_MAX_CONFIRM_DISTANCE_FROM_FVG_PCT: float = MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT
AMD_INVALIDATE_BEYOND_SWEEP_BUFFER_PCT: float = INVALIDATE_SWEEP_BUFFER_PCT
AMD_MIN_VOLUME_MULTIPLIER: float = MIN_SWEEP_VOLUME_MULTIPLIER

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = 20
TP_ROI_PCT: float = TAKE_PROFIT_PRICE_PCT * LEVERAGE
SL_ROI_PCT: float = STOP_LOSS_PRICE_PCT * LEVERAGE
REWARD_RATIO: float = TAKE_PROFIT_PRICE_PCT / STOP_LOSS_PRICE_PCT

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 25
SIGNAL_EXPIRE_HOURS:     int = 3
MAX_CONCURRENT_SIGNALS:  int = 3
SETUP_SCAN_CRON_MINUTES: str = "*/1"
SETUP_MONITOR_MINUTES: int = 1
OUTCOME_CHECK_MINUTES: int = 1
CANDLE_MINUTES:        int = 5
SIGNALS_PER_SCAN: int = 2
SCAN_WORKERS: int = 4

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")