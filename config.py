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

# ── Strategy: Binocular Pending-Breakout v1 ─────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Binocular Pending-Breakout v1",
)

ENTRY_TF: str = os.getenv("ENTRY_TF", "15m")
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "220"))
# Bumped from 120 -- EMA200 (confirmed/strict SIGNAL_MODE) needs ~200 bars
# of warmup, plus margin for the Chandelier(10)/RSI(55)/PVT-signal(21)
# periods and the ribbon baseline(60).

# 6-EMA ribbon (Pine script defaults) -- confirmed/strict-mode filter,
# not the primary trigger (see SIGNAL_MODE below).
RIBBON_MA1_LEN: int = int(os.getenv("RIBBON_MA1_LEN", "30"))
RIBBON_MA2_LEN: int = int(os.getenv("RIBBON_MA2_LEN", "35"))
RIBBON_MA3_LEN: int = int(os.getenv("RIBBON_MA3_LEN", "40"))
RIBBON_MA4_LEN: int = int(os.getenv("RIBBON_MA4_LEN", "45"))
RIBBON_MA5_LEN: int = int(os.getenv("RIBBON_MA5_LEN", "50"))
RIBBON_BASELINE_LEN: int = int(os.getenv("RIBBON_BASELINE_LEN", "60"))

# Minimum age (seconds) the last CLOSED candle must have before a signal
# can fire on it. MEXC's kline REST data for a just-closed candle can still
# get revised for a short window after the close.
MIN_CANDLE_SETTLE_SECONDS: int = int(os.getenv("MIN_CANDLE_SETTLE_SECONDS", "90"))

# ATR period backing the Chandelier trailing-stop direction flip.
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

# LONG signals underperformed SHORT in every backtest of the prior
# ribbon-flip strategy -- left enabled by default so the asymmetry can
# keep being observed on live data; set to "false" to run SHORT-only.
ENABLE_LONG_SIGNALS: bool = os.getenv("ENABLE_LONG_SIGNALS", "true").lower() == "true"

MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "10.0"))
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE

MIN_RR: float = float(os.getenv("MIN_RR", "1.5"))

# ── Strategy: Binocular Pending-Breakout v1 ─────────────────────────
SIGNAL_MODE: str = os.getenv("SIGNAL_MODE", "confirmed")   # "original" | "confirmed" | "strict"
CONFIRMATION_TIMEFRAMES: str = os.getenv("CONFIRMATION_TIMEFRAMES", "30m,1h,4h")   # strict mode only
MTF_MIN_CONFIRMATIONS: int = int(os.getenv("MTF_MIN_CONFIRMATIONS", "2"))          # strict mode only

ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))              # informational only
RISK_PERCENT_PER_TRADE: float = float(os.getenv("RISK_PERCENT_PER_TRADE", "1.0"))  # informational only

PVT_SIGNAL_TYPE: str = os.getenv("PVT_SIGNAL_TYPE", "SMA")           # "SMA" | "EMA"
PVT_SIGNAL_LENGTH: int = int(os.getenv("PVT_SIGNAL_LENGTH", "21"))
RSI_FAST_PERIOD: int = int(os.getenv("RSI_FAST_PERIOD", "25"))
RSI_SLOW_PERIOD: int = int(os.getenv("RSI_SLOW_PERIOD", "55"))
CHANDELIER_ATR_PERIOD: int = int(os.getenv("CHANDELIER_ATR_PERIOD", "10"))
CHANDELIER_MULTIPLIER: float = float(os.getenv("CHANDELIER_MULTIPLIER", "2.2"))
BINOCULAR_EMA200_LEN: int = int(os.getenv("BINOCULAR_EMA200_LEN", "200"))

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # 0.02%
PENDING_SIGNAL_EXPIRY_CANDLES: int = int(os.getenv("PENDING_SIGNAL_EXPIRY_CANDLES", "5"))

TARGET1_CLOSE_FRACTION: float = float(os.getenv("TARGET1_CLOSE_FRACTION", "0.5"))
TARGET2_CLOSE_FRACTION: float = float(os.getenv("TARGET2_CLOSE_FRACTION", "0.3"))
TARGET3_CLOSE_FRACTION: float = float(os.getenv("TARGET3_CLOSE_FRACTION", "0.2"))
MOVE_SL_TO_BREAKEVEN_AFTER_T1: bool = os.getenv("MOVE_SL_TO_BREAKEVEN_AFTER_T1", "true").lower() == "true"

SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "3"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES", "60"))

MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "2"))

MAX_ACTIVE_LONG_SIGNALS: int = int(os.getenv("MAX_ACTIVE_LONG_SIGNALS", "1"))
MAX_ACTIVE_SHORT_SIGNALS: int = int(os.getenv("MAX_ACTIVE_SHORT_SIGNALS", "1"))

SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "1"))
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "240"))

SIGNAL_EXPIRE_HOURS: int = int(os.getenv("SIGNAL_EXPIRE_HOURS", "6"))

SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# ── Strategy: Ribbon-Flip Trend-Bar Confirmation v1 ──
# On by default for backward compatibility; set false in .env to stop
# firing new v1 signals (e.g. once v3 is validated and preferred).
# Outcome checking for any already-pending v1 signals keeps running
# regardless, so open trades still resolve to win/loss/expired.
STRATEGY_V1_ENABLED: bool = os.getenv("STRATEGY_V1_ENABLED", "true").lower() == "true"

# ── Strategy: Super Scalper v3 (SuperTrend + Keltner + regime filter) ──
# Off by default -- runs alongside v1 in main.py only when explicitly
# enabled, and never live-trades until LIVE_ENABLED is flipped true.
SCALPER_V3_ENABLED: bool = os.getenv("SCALPER_V3_ENABLED", "false").lower() == "true"
SCALPER_V3_TIMEFRAME: str = os.getenv("SCALPER_V3_TIMEFRAME", "5m")
SCALPER_V3_HISTORY_BARS: int = int(os.getenv("SCALPER_V3_HISTORY_BARS", "300"))

SCALPER_V3_ATR_PERIOD: int = int(os.getenv("SCALPER_V3_ATR_PERIOD", "10"))
SCALPER_V3_ATR_MULT: float = float(os.getenv("SCALPER_V3_ATR_MULT", "2.5"))
SCALPER_V3_KC_EMA: int = int(os.getenv("SCALPER_V3_KC_EMA", "20"))
SCALPER_V3_KC_ATR_PERIOD: int = int(os.getenv("SCALPER_V3_KC_ATR_PERIOD", "14"))
SCALPER_V3_KC_MULT: float = float(os.getenv("SCALPER_V3_KC_MULT", "2.0"))
SCALPER_V3_ENTRY_ZONE: float = float(os.getenv("SCALPER_V3_ENTRY_ZONE", "0.55"))
SCALPER_V3_SLOPE_LOOKBACK: int = int(os.getenv("SCALPER_V3_SLOPE_LOOKBACK", "3"))
SCALPER_V3_AO_FAST: int = int(os.getenv("SCALPER_V3_AO_FAST", "5"))
SCALPER_V3_AO_SLOW: int = int(os.getenv("SCALPER_V3_AO_SLOW", "34"))
SCALPER_V3_STRENGTH_LOOKBACK: int = int(os.getenv("SCALPER_V3_STRENGTH_LOOKBACK", "5"))
SCALPER_V3_ADX_PERIOD: int = int(os.getenv("SCALPER_V3_ADX_PERIOD", "14"))
SCALPER_V3_ADX_MIN: float = float(os.getenv("SCALPER_V3_ADX_MIN", "22.0"))
SCALPER_V3_CHOP_PERIOD: int = int(os.getenv("SCALPER_V3_CHOP_PERIOD", "14"))
SCALPER_V3_CHOP_MAX: float = float(os.getenv("SCALPER_V3_CHOP_MAX", "50.0"))
SCALPER_V3_EXPAND_PERIOD: int = int(os.getenv("SCALPER_V3_EXPAND_PERIOD", "20"))
SCALPER_V3_EXPAND_MIN: float = float(os.getenv("SCALPER_V3_EXPAND_MIN", "1.10"))
SCALPER_V3_MIN_STRENGTH: int = int(os.getenv("SCALPER_V3_MIN_STRENGTH", "2"))
# regime() votes on 3 independent signals (ADX/Choppiness/band-expansion) and
# labels anything with >=2 "TRENDING". 6978d54 relaxed this to 2 based on a
# backtest fidelity fix, but 2026-07-21..25 live data (106 signals) showed
# 2/3-vote trades net -241% ROI (69 trades) vs 3/3-vote trades roughly
# breakeven at -10.5% ROI (35 trades) -- reverted back to requiring
# unanimous votes.
SCALPER_V3_MIN_REGIME_VOTES: int = int(os.getenv("SCALPER_V3_MIN_REGIME_VOTES", "3"))

# Flat SL/TP sizing (replaces the structural SuperTrend-SL / Keltner-TP1-TP2
# exits -- see scalper_v3_strategy._calc_tp_sl). Both entry paths use the
# same fixed ROI-at-LEVERAGE distance, no trailing stop, no breakeven step.
SCALPER_V3_TARGET_ROI_PCT: float = float(os.getenv("SCALPER_V3_TARGET_ROI_PCT", "10.0"))
SCALPER_V3_MAX_SL_ROI_PCT: float = float(os.getenv("SCALPER_V3_MAX_SL_ROI_PCT", "10.0"))
SCALPER_V3_TP_PRICE_PCT: float = SCALPER_V3_TARGET_ROI_PCT / 100.0 / LEVERAGE
SCALPER_V3_MAX_SL_PRICE_PCT: float = SCALPER_V3_MAX_SL_ROI_PCT / 100.0 / LEVERAGE

# Informational progress checkpoint (Telegram ping only -- does not move
# the SL or close the trade). Stored in the tp1_price column/field.
SCALPER_V3_TP1_NOTIFY_ROI_PCT: float = float(os.getenv("SCALPER_V3_TP1_NOTIFY_ROI_PCT", "7.0"))
SCALPER_V3_TP1_NOTIFY_PRICE_PCT: float = SCALPER_V3_TP1_NOTIFY_ROI_PCT / 100.0 / LEVERAGE

# Funding-rate safety filter -- block entries when funding is stacked hard
# against the trade direction (crowded-trade / squeeze risk).
SCALPER_V3_MAX_ADVERSE_FUNDING_PCT: float = float(
    os.getenv("SCALPER_V3_MAX_ADVERSE_FUNDING_PCT", "0.05")
)

# Move SL to breakeven (entry price) once TP1 (kc_mid) fills.
SCALPER_V3_BREAKEVEN_AFTER_TP1: bool = os.getenv("SCALPER_V3_BREAKEVEN_AFTER_TP1", "true").lower() == "true"

SCALPER_V3_SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCALPER_V3_SCAN_INTERVAL_MINUTES", "5"))
SCALPER_V3_MAX_CONCURRENT_SIGNALS: int = int(os.getenv("SCALPER_V3_MAX_CONCURRENT_SIGNALS", "2"))
SCALPER_V3_SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SCALPER_V3_SIGNAL_COOLDOWN_MINUTES", "240"))
SCALPER_V3_EXPIRE_HOURS: int = int(os.getenv("SCALPER_V3_EXPIRE_HOURS", "6"))
# 2026-07-21..25 live data (106 signals, mostly votes=2) fired up to 40/day
# with only MAX_CONCURRENT_SIGNALS as a brake. With PF sitting at ~1.0-1.05
# even under the restored votes=3 gate (see 2026-07-25 backtest), more
# trades/day is pure variance, not more expectancy -- cap exposure while
# the edge is this thin.
SCALPER_V3_MAX_DAILY_SIGNALS: int = int(os.getenv("SCALPER_V3_MAX_DAILY_SIGNALS", "10"))
SCALPER_V3_MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("SCALPER_V3_MIN_DAILY_SIGNAL_GAP_MINUTES", "15"))
STRATEGY_NAME_V3: str = os.getenv("STRATEGY_NAME_V3", "Super Scalper v3")

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
