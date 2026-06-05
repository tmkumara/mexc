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
QUOTE_CURRENCY: str = os.getenv("QUOTE_CURRENCY", "USDT")
CRYPTO_FUTURES_ONLY: bool = os.getenv("CRYPTO_FUTURES_ONLY", "true").lower() == "true"

EXCLUDE_COINS: set[str] = {
    coin.strip().upper()
    for coin in os.getenv(
        "EXCLUDE_COINS",
        "BTC_USDT,ETH_USDT,SOL_USDT,XAUT_USDT",
    ).split(",")
    if coin.strip()
}

TOP_N_COINS: int = int(os.getenv("TOP_N_COINS", "80"))
COIN_POOL_MIN_VOLUME_USD: float = float(os.getenv("COIN_POOL_MIN_VOLUME_USD", "5000000"))
COIN_REFRESH_HOURS: int = int(os.getenv("COIN_REFRESH_HOURS", "6"))

# ── Smart coin ranking / coin_scanner.py compatibility ────────────
ENABLE_SMART_COIN_RANKING: bool = os.getenv("ENABLE_SMART_COIN_RANKING", "true").lower() == "true"
COIN_RANK_CANDIDATE_MULTIPLIER: int = int(os.getenv("COIN_RANK_CANDIDATE_MULTIPLIER", "4"))
COIN_RANK_MAX_CANDIDATES: int = int(os.getenv("COIN_RANK_MAX_CANDIDATES", str(TOP_N_COINS * COIN_RANK_CANDIDATE_MULTIPLIER)))
COIN_RANK_TIMEFRAME: str = os.getenv("COIN_RANK_TIMEFRAME", "15m")
COIN_RANK_KLINE_COUNT: int = int(os.getenv("COIN_RANK_KLINE_COUNT", "80"))
COIN_RANK_WORKERS: int = int(os.getenv("COIN_RANK_WORKERS", "4"))
COIN_RANK_MIN_LAST_PRICE: float = float(os.getenv("COIN_RANK_MIN_LAST_PRICE", "0.000001"))
COIN_RANK_MIN_RANGE_PCT: float = float(os.getenv("COIN_RANK_MIN_RANGE_PCT", "0.20"))
COIN_RANK_MAX_RANGE_PCT: float = float(os.getenv("COIN_RANK_MAX_RANGE_PCT", "18.0"))
COIN_RANK_MAX_ABS_MOVE_PCT: float = float(os.getenv("COIN_RANK_MAX_ABS_MOVE_PCT", "12.0"))
COIN_RANK_VOLUME_WEIGHT: float = float(os.getenv("COIN_RANK_VOLUME_WEIGHT", "0.35"))
COIN_RANK_VOLATILITY_WEIGHT: float = float(os.getenv("COIN_RANK_VOLATILITY_WEIGHT", "0.30"))
COIN_RANK_TREND_WEIGHT: float = float(os.getenv("COIN_RANK_TREND_WEIGHT", "0.20"))
COIN_RANK_LIQUIDITY_WEIGHT: float = float(os.getenv("COIN_RANK_LIQUIDITY_WEIGHT", "0.15"))
COIN_RANK_OVEREXTENSION_PENALTY: float = float(os.getenv("COIN_RANK_OVEREXTENSION_PENALTY", "0.25"))
COIN_RANK_LOW_ACTIVITY_PENALTY: float = float(os.getenv("COIN_RANK_LOW_ACTIVITY_PENALTY", "0.20"))

# Older compatibility names used by some coin_scanner versions.
SMART_RANKING_LOOKBACK_MINUTES: int = int(os.getenv("SMART_RANKING_LOOKBACK_MINUTES", "240"))
SMART_RANKING_MIN_VOLUME_USD: float = float(os.getenv("SMART_RANKING_MIN_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
SMART_RANKING_TOP_N: int = int(os.getenv("SMART_RANKING_TOP_N", str(TOP_N_COINS)))
MIN_24H_VOLUME_USD: float = float(os.getenv("MIN_24H_VOLUME_USD", str(COIN_POOL_MIN_VOLUME_USD)))
MAX_SPREAD_PCT: float = float(os.getenv("MAX_SPREAD_PCT", "0.35"))
MIN_PRICE_CHANGE_24H_PCT: float = float(os.getenv("MIN_PRICE_CHANGE_24H_PCT", "0.0"))

# ── Hybrid SMC Pro Strategy ───────────────────────────────────────
# Strategy flow:
#   15m market-structure bias → 5m liquidity sweep/displacement/OB
#   → save waiting setup → 5m OB retest confirmation → signal.
STRATEGY_NAME: str = os.getenv("STRATEGY_NAME", "Hybrid SMC Pro")
STRATEGY_TF: str = os.getenv("STRATEGY_TF", "5m")          # compatibility only; active entry TF is ENTRY_TF
TREND_TF: str = os.getenv("TREND_TF", "15m")               # market-structure bias
ENTRY_TF: str = os.getenv("ENTRY_TF", "5m")                # sweep + displacement + OB + retest
HTF_TREND_TF: str = os.getenv("HTF_TREND_TF", "1h")        # higher-timeframe trend confirmation

TREND_KLINE_COUNT: int = int(os.getenv("TREND_KLINE_COUNT", "220"))
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "220"))
MONITOR_KLINE_COUNT: int = int(os.getenv("MONITOR_KLINE_COUNT", "60"))
HTF_KLINE_COUNT: int = int(os.getenv("HTF_KLINE_COUNT", "260"))

# Swing / structure detection
SWING_LEFT: int = int(os.getenv("SWING_LEFT", "3"))
SWING_RIGHT: int = int(os.getenv("SWING_RIGHT", "2"))
STRUCTURE_LOOKBACK: int = int(os.getenv("STRUCTURE_LOOKBACK", "160"))
ENTRY_LOOKBACK: int = int(os.getenv("ENTRY_LOOKBACK", "180"))
SWEEP_LOOKBACK: int = int(os.getenv("SWEEP_LOOKBACK", "18"))

# Displacement candle
AVG_BODY_PERIOD: int = int(os.getenv("AVG_BODY_PERIOD", "20"))
DISPLACEMENT_BODY_MULTIPLIER: float = float(os.getenv("DISPLACEMENT_BODY_MULTIPLIER", "1.35"))
DISPLACEMENT_CLOSE_POSITION: float = float(os.getenv("DISPLACEMENT_CLOSE_POSITION", "0.65"))

# Order block
ORDER_BLOCK_LOOKBACK: int = int(os.getenv("ORDER_BLOCK_LOOKBACK", "12"))
MAX_SIGNAL_CANDLE_BODY_PCT: float = float(os.getenv("MAX_SIGNAL_CANDLE_BODY_PCT", "1.20"))
PENDING_SETUP_EXPIRE_CANDLES: int = int(os.getenv("PENDING_SETUP_EXPIRE_CANDLES", "24"))
MAX_PENDING_SETUPS_PER_SYMBOL: int = int(os.getenv("MAX_PENDING_SETUPS_PER_SYMBOL", "1"))

# HTF trend filter
ENABLE_HTF_FILTER: bool = os.getenv("ENABLE_HTF_FILTER", "true").lower() == "true"
HTF_EMA_FAST: int = int(os.getenv("HTF_EMA_FAST", "50"))
HTF_EMA_SLOW: int = int(os.getenv("HTF_EMA_SLOW", "200"))
HTF_EMA_SLOPE_LOOKBACK: int = int(os.getenv("HTF_EMA_SLOPE_LOOKBACK", "3"))

# Entry EMA alignment filter
ENABLE_ENTRY_EMA_FILTER: bool = os.getenv("ENABLE_ENTRY_EMA_FILTER", "true").lower() == "true"
EMA_FAST_FILTER: int = int(os.getenv("EMA_FAST_FILTER", "20"))
EMA_SLOW_FILTER: int = int(os.getenv("EMA_SLOW_FILTER", "50"))

# ATR filter
ENABLE_ATR_FILTER: bool = os.getenv("ENABLE_ATR_FILTER", "true").lower() == "true"
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))
MIN_ATR_PCT: float = float(os.getenv("MIN_ATR_PCT", "0.18"))
MAX_ATR_PCT: float = float(os.getenv("MAX_ATR_PCT", "2.50"))
ATR_SL_MULTIPLIER: float = float(os.getenv("ATR_SL_MULTIPLIER", "0.25"))

# Volume confirmation
ENABLE_VOLUME_FILTER: bool = os.getenv("ENABLE_VOLUME_FILTER", "true").lower() == "true"
VOLUME_LOOKBACK: int = int(os.getenv("VOLUME_LOOKBACK", "20"))
MIN_VOLUME_MULTIPLIER: float = float(os.getenv("MIN_VOLUME_MULTIPLIER", "1.10"))

# BTC market regime filter
ENABLE_BTC_FILTER: bool = os.getenv("ENABLE_BTC_FILTER", "true").lower() == "true"
BTC_SYMBOL: str = os.getenv("BTC_SYMBOL", "BTC_USDT")
BTC_TF: str = os.getenv("BTC_TF", "15m")
BTC_EMA_PERIOD: int = int(os.getenv("BTC_EMA_PERIOD", "50"))
BTC_KLINE_COUNT: int = int(os.getenv("BTC_KLINE_COUNT", "100"))

# RR / SL controls
MIN_STRUCTURE_RR: float = float(os.getenv("MIN_STRUCTURE_RR", "1.50"))
MAX_STRUCTURE_RR: float = float(os.getenv("MAX_STRUCTURE_RR", "3.00"))
REWARD_RATIO: float = float(os.getenv("REWARD_RATIO", "1.50"))
SL_BUFFER_PCT: float = float(os.getenv("SL_BUFFER_PCT", "0.05"))
TP_BUFFER_PCT: float = float(os.getenv("TP_BUFFER_PCT", "0.02"))
MIN_SL_PCT: float = float(os.getenv("MIN_SL_PCT", "0.20"))
MAX_SL_PCT: float = float(os.getenv("MAX_SL_PCT", "1.25"))
MIN_SETUP_SCORE: int = int(os.getenv("MIN_SETUP_SCORE", "75"))

# ── Legacy EMA/CCI names kept only so old imports do not fail ─────
EMA_FAST: int = int(os.getenv("EMA_FAST", "10"))
EMA_SLOW: int = int(os.getenv("EMA_SLOW", "20"))
CCI_LENGTH: int = int(os.getenv("CCI_LENGTH", "20"))
CCI_MIN_ABS: float = float(os.getenv("CCI_MIN_ABS", "50.0"))
SL_LOOKBACK: int = int(os.getenv("SL_LOOKBACK", "20"))
BOS_LOOKBACK: int = int(os.getenv("BOS_LOOKBACK", "20"))
DOUBLE_LOOKBACK: int = int(os.getenv("DOUBLE_LOOKBACK", "40"))
DOUBLE_TOLERANCE_PCT: float = float(os.getenv("DOUBLE_TOLERANCE_PCT", "1.5"))
PATTERN_MIN_SCORE: int = int(os.getenv("PATTERN_MIN_SCORE", "2"))
STRATEGY_KLINE_COUNT: int = int(os.getenv("STRATEGY_KLINE_COUNT", "260"))

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
TP_ROI_PCT: float = 0.0
SL_ROI_PCT: float = 0.0

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60"))
SIGNAL_EXPIRE_HOURS: int = int(os.getenv("SIGNAL_EXPIRE_HOURS", "6"))
MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "10"))
SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "*/5")
SETUP_MONITOR_MINUTES: int = int(os.getenv("SETUP_MONITOR_MINUTES", "1"))
OUTCOME_CHECK_MINUTES: int = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
SIGNALS_PER_SCAN: int = int(os.getenv("SIGNALS_PER_SCAN", "3"))
SCAN_WORKERS: int = int(os.getenv("SCAN_WORKERS", "4"))

# Important: outcome checking uses ENTRY_TF 5m candles, so this must stay 5 by default.
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", "5"))

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")

# ── Database ──────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "signals.db")
