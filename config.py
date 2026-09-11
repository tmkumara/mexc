import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ───────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))

# ── Telegram ───────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass optional API ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ──────────────────────────────────────────────────────
QUOTE_CURRENCY: str       = os.getenv("QUOTE_CURRENCY", "USDT")
CRYPTO_FUTURES_ONLY: bool = os.getenv("CRYPTO_FUTURES_ONLY", "true").lower() == "true"

EXCLUDE_COINS: set[str] = {
    coin.strip().upper()
    for coin in os.getenv("EXCLUDE_COINS", "BTC_USDT,ETH_USDT,SOL_USDT,XAUT_USDT").split(",")
    if coin.strip()
}

TOP_N_COINS: int               = int(os.getenv("TOP_N_COINS", "80"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_POOL_MIN_SELECTED: int    = int(os.getenv("COIN_POOL_MIN_SELECTED", "20"))
COIN_REFRESH_HOURS: int        = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking ─────────────────────────────────────────────
ENABLE_SMART_COIN_RANKING: bool        = os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
COIN_RANK_CANDIDATE_MULTIPLIER: int    = int(os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4"))
COIN_RANK_MAX_CANDIDATES: int          = int(os.getenv("COIN_RANK_MAX_CANDIDATES", str(TOP_N_COINS * 4)))
COIN_RANK_TIMEFRAME: str               = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int             = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int                 = int(os.getenv("COIN_RANK_WORKERS", "4"))
COIN_RANK_MIN_LAST_PRICE: float        = float(os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.001"))
COIN_RANK_MIN_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20"))
COIN_RANK_MAX_RANGE_PCT: float         = float(os.getenv("COIN_RANK_MAX_RANGE_PCT", "60.0"))
COIN_RANK_MAX_ABS_MOVE_PCT: float      = float(os.getenv("COIN_RANK_MAX_ABS_MOVE_PCT", "8.0"))
COIN_RANK_VOLUME_WEIGHT: float         = float(os.getenv("COIN_RANK_VOLUME_WEIGHT",     "0.35"))
COIN_RANK_VOLATILITY_WEIGHT: float     = float(os.getenv("COIN_RANK_VOLATILITY_WEIGHT", "0.30"))
COIN_RANK_TREND_WEIGHT: float          = float(os.getenv("COIN_RANK_TREND_WEIGHT",      "0.20"))
COIN_RANK_LIQUIDITY_WEIGHT: float      = float(os.getenv("COIN_RANK_LIQUIDITY_WEIGHT",  "0.15"))
COIN_RANK_OVEREXTENSION_PENALTY: float = float(os.getenv("COIN_RANK_OVEREXTENSION_PENALTY", "0.25"))
COIN_RANK_LOW_ACTIVITY_PENALTY: float  = float(os.getenv("COIN_RANK_LOW_ACTIVITY_PENALTY",  "0.20"))

SMART_RANKING_LOOKBACK_MINUTES: int = int(os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240"))
SMART_RANKING_MIN_VOLUME_USD: float  = float(os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
SMART_RANKING_TOP_N: int             = int(os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS)))
MIN_24H_VOLUME_USD: float            = float(os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
MAX_SPREAD_PCT: float                = float(os.getenv("MAX_SPREAD_PCT", "0.35"))
MIN_PRICE_CHANGE_24H_PCT: float      = float(os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0"))

# ── Strategy: SMC Structure + Squeeze Momentum v1 ───────────────────
# Retired (Zero-Lag MTF Pullback v1) config below is intentionally left
# defined but unused -- see backup/zero-lag-mtf-pullback-v1 for that
# strategy. New vars below use distinct names rather than repurposing
# MIN_SIGNAL_SCORE (0-100 there vs 0-10 here), ATR_PERIOD, MACRO_TF,
# PULLBACK_TF, ZERO_LAG_*, TP_ROI_PCT/SL_ROI_PCT (fixed-% there vs
# structure-derived + floored/capped here) -- CLAUDE.md documents this
# repo has been bitten by exactly that kind of silent collision before.
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "SMC Structure + Squeeze Momentum v1",
)

MACRO_TF: str    = os.getenv("MACRO_TF", "4h")     # unused by this strategy
TREND_TF: str    = os.getenv("TREND_TF", "1h")     # 1h EMA50 trend bias
PULLBACK_TF: str = os.getenv("PULLBACK_TF", "15m") # unused by this strategy
ENTRY_TF: str    = os.getenv("ENTRY_TF", "30m")    # structure + squeeze evaluated here

MACRO_KLINE_COUNT: int    = int(os.getenv("MACRO_KLINE_COUNT", "300"))   # unused by this strategy
TREND_KLINE_COUNT: int    = int(os.getenv("TREND_KLINE_COUNT", "100"))
PULLBACK_KLINE_COUNT: int = int(os.getenv("PULLBACK_KLINE_COUNT", "250"))   # unused by this strategy
ENTRY_KLINE_COUNT: int    = int(os.getenv("ENTRY_KLINE_COUNT", "150"))

ZERO_LAG_LENGTH: int         = int(os.getenv("ZERO_LAG_LENGTH", "70"))          # unused by this strategy
ZERO_LAG_BAND_LOOKBACK: int  = int(os.getenv("ZERO_LAG_BAND_LOOKBACK", "210"))  # unused by this strategy
ZERO_LAG_MULTIPLIER: float   = float(os.getenv("ZERO_LAG_MULTIPLIER", "1.2"))   # unused by this strategy
ZERO_LAG_SLOPE_LOOKBACK: int = int(os.getenv("ZERO_LAG_SLOPE_LOOKBACK", "5"))   # unused by this strategy

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # unused by this strategy
PULLBACK_DISTANCE_PCT: float = float(os.getenv("PULLBACK_DISTANCE_PCT", "0.10")) / 100.0   # unused by this strategy
PENDING_EXPIRY_CANDLES: int = int(os.getenv("PENDING_EXPIRY_CANDLES", "6"))   # unused by this strategy

ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "70"))   # unused by this strategy

MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))   # unused by this strategy -- see SIGNAL_SCORE_THRESHOLD (0-10 scale)

# Minimum age (seconds) the last CLOSED candle must have before a signal
# can fire on it. MEXC's kline REST data for a just-closed candle can still
# get revised for a short window after the close.
MIN_CANDLE_SETTLE_SECONDS: int = int(os.getenv("MIN_CANDLE_SETTLE_SECONDS", "90"))

ENABLE_LONG_SIGNALS: bool = os.getenv("ENABLE_LONG_SIGNALS", "true").lower() == "true"

LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))    # unused by this strategy
SL_ROI_PCT: float = float(os.getenv("SL_ROI_PCT", "10.0"))   # unused by this strategy
TP_PRICE_PCT: float = TP_ROI_PCT / 100.0 / LEVERAGE   # unused by this strategy
SL_PRICE_PCT: float = SL_ROI_PCT / 100.0 / LEVERAGE   # unused by this strategy

# Hardcoded coin universe -- only these 5 (base coin, resolved to live
# _USDT contracts by coin_scanner.get_whitelisted_pairs()). BTC/ETH stay
# excluded via EXCLUDE_COINS above regardless.
WHITELISTED_COINS: list[str] = [
    c.strip().upper()
    for c in os.getenv("WHITELISTED_COINS", "SOL,BNB,XRP,DOGE,ADA").split(",")
    if c.strip()
]

RISK_REWARD_RATIO: float = float(os.getenv("RISK_REWARD_RATIO", "2.0"))   # fixed 1:2 on every signal

# Every winning trade must return at least this much ROI on capital at
# LEVERAGE, given RISK_REWARD_RATIO -- derived risk floor:
#   risk_pct >= MIN_WIN_ROI_PCT / (LEVERAGE * RISK_REWARD_RATIO)
MIN_WIN_ROI_PCT: float = float(os.getenv("MIN_WIN_ROI_PCT", "30.0"))
MIN_RISK_PCT: float = MIN_WIN_ROI_PCT / 100.0 / (LEVERAGE * RISK_REWARD_RATIO)

# Cap how much a losing trade can cost (ROI on capital at LEVERAGE)
MAX_LOSS_ROI_PCT: float = float(os.getenv("MAX_LOSS_ROI_PCT", "40.0"))
MAX_RISK_PCT: float = MAX_LOSS_ROI_PCT / 100.0 / LEVERAGE

SL_BUFFER_PCT: float = float(os.getenv("SL_BUFFER_PCT", "0.15")) / 100.0   # small buffer beyond the swing point used for SL

# Market structure (fractal swing pivots): bars required on each side to confirm a pivot
STRUCTURE_LEFT: int = int(os.getenv("STRUCTURE_LEFT", "2"))
STRUCTURE_RIGHT: int = int(os.getenv("STRUCTURE_RIGHT", "2"))
STRUCTURE_LOOKBACK_BARS: int = int(os.getenv("STRUCTURE_LOOKBACK_BARS", "6"))   # BOS/CHoCH must be within the last N closed bars (~3h at 30m; widened from 2 after live logs showed real breaks commonly 4-13 bars old, missing the original 1h window)

# Squeeze momentum (LazyBear SQZMOM, matches the live TradingView chart's
# SQZMOM_LB settings 20/2/20/1.5)
SQZ_BB_LENGTH: int = int(os.getenv("SQZ_BB_LENGTH", "20"))
SQZ_BB_MULT: float = float(os.getenv("SQZ_BB_MULT", "2.0"))
SQZ_KC_LENGTH: int = int(os.getenv("SQZ_KC_LENGTH", "20"))
SQZ_KC_MULT: float = float(os.getenv("SQZ_KC_MULT", "1.5"))
SQZ_LOOKBACK_BARS: int = int(os.getenv("SQZ_LOOKBACK_BARS", "8"))   # squeeze must have fired within the last N closed bars (~4h at 30m; widened alongside STRUCTURE_LOOKBACK_BARS)

SIGNAL_SCORE_THRESHOLD: float = float(os.getenv("SIGNAL_SCORE_THRESHOLD", "7.0"))   # 0-10 scale

SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))

MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "12"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES", "60"))

MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "4"))

MAX_ACTIVE_LONG_SIGNALS: int = int(os.getenv("MAX_ACTIVE_LONG_SIGNALS", "2"))
MAX_ACTIVE_SHORT_SIGNALS: int = int(os.getenv("MAX_ACTIVE_SHORT_SIGNALS", "2"))

SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "1"))
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "240"))

SIGNAL_EXPIRE_HOURS: int = int(os.getenv("SIGNAL_EXPIRE_HOURS", "6"))

SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# ── Live trading master switch -- paper-trade / backtest-only until
# explicitly flipped true after reviewing optimization results. ─────────
LIVE_ENABLED: bool = os.getenv("LIVE_ENABLED", "false").lower() == "true"

# ── Fee / slippage estimates (backtest only) ─────────────────────────
ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.01"))

# ── Dry run ────────────────────────────────────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
DRY_RUN_SAVE_SIGNALS: bool = os.getenv("DRY_RUN_SAVE_SIGNALS", "false").lower() == "true"

# ── Scheduler ──────────────────────────────────────────────────────
OUTCOME_CHECK_MINUTES: int = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
MONITOR_INTERVAL_MINUTES: int = int(os.getenv("MONITOR_INTERVAL_MINUTES", "1"))
COIN_REFRESH_CRON_HOURS: str = os.getenv("COIN_REFRESH_CRON_HOURS", f"*/{COIN_REFRESH_HOURS}")

SCHEDULER_MISFIRE_GRACE_SECONDS: int = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", "30"))
SCHEDULER_MAX_INSTANCES: int = int(os.getenv("SCHEDULER_MAX_INSTANCES", "1"))

# ── Log ────────────────────────────────────────────────────────────
LOG_FILE: str = os.getenv("LOG_FILE", "mexc_bot.log")
ENABLE_LOG_BACKUP_ON_START: bool = os.getenv("ENABLE_LOG_BACKUP_ON_START", "true").lower() == "true"
LOG_BACKUP_DIR: str = os.getenv("LOG_BACKUP_DIR", "logs/archive")

# ── MEXC REST API ──────────────────────────────────────────────────
MEXC_BASE_URL: str = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")

# ── Database ───────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "signals.db")

# ── Candle minutes (derived from ENTRY_TF) ──────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(ENTRY_TF, 5))))

# ── MEXC interval map ──────────────────────────────────────────────
MEXC_INTERVAL_MAP: dict[str, str] = {
    "1m":  "Min1",
    "3m":  "Min3",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "8h":  "Hour8",
    "1d":  "Day1",
}
