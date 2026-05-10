import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Timezone ──────────────────────────────────────────────────────
LKT = timezone(timedelta(hours=5, minutes=30))   # Sri Lanka Time (UTC+5:30)

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass (optional) ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
TOP_N_COINS:              int   = 100
COIN_POOL_MIN_VOLUME_USD: float = 5_000_000   # minimum $5M daily volume
COIN_REFRESH_HOURS:       int   = 6

# ── RSI pre-filter (Phase 1 of scan pipeline) ────────────────────
RSI_PREFILTER_OVERSOLD:   float = 35    # RSI below → LONG candidate
RSI_PREFILTER_OVERBOUGHT: float = 65    # RSI above → SHORT candidate
RSI_PREFILTER_BARS:       int   = 30    # 15M bars needed for RSI calc
PREFILTER_WORKERS:        int   = 10    # concurrent threads for pre-filter
SCAN_WORKERS:             int   = 5     # concurrent threads for full analysis

# ── Timeframes ───────────────────────────────────────────────────
MTF_1H:   str = "1h"   # tier 1: higher-TF structure (2 HH / 2 LL)
SWEEP_TF: str = "15m"  # tier 2: liquidity sweep + acceptance detection
ENTRY_TF: str = "5m"   # tier 3: retest confirmation + outcome tracking

# ── EMA filter (5M confirmation candle) ──────────────────────────
EMA_50: int = 50

# ── RSI ──────────────────────────────────────────────────────────
RSI_PERIOD: int = 14

# ── Volume filter (on 5M confirmation candle) ─────────────────────
VOLUME_MA_BARS:  int   = 20
VOLUME_MIN_MULT: float = 1.3

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE:      int   = 10
REWARD_RATIO:  float = 2.0    # TP = 2R
SL_ATR_BUFFER: float = 0.5   # extra SL distance beyond zone edge in ATR units
MAX_RISK_PCT:  float = 2.0   # skip signals where SL > 2% from entry

# ── Signal quality gates ──────────────────────────────────────────
MIN_ZONE_WIDTH_PCT: float = 0.15  # skip zones narrower than 0.15% of price
ENTRY_ZONE_BUFFER:  float = 0.003 # 5M close must be within 0.3% above zone_high (LONG) / below zone_low (SHORT)
MIN_SIGNAL_SCORE:   int   = 40    # discard signals scored below this threshold

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60
SIGNAL_EXPIRE_HOURS:     int = 4
ZONE_EXPIRE_HOURS:          int = 48   # waiting_retest zones expire after 48h
ZONE_EXPIRE_ACCEPTED_HOURS: int = 24   # accepted zones (no retest yet) expire after 24h
MAX_CONCURRENT_SIGNALS:  int = 10

SCAN_CRON_MINUTES:     str = "1,6,11,16,21,26,31,36,41,46,51,56"   # every 5 min
SIGNALS_PER_SCAN:      int = 3
OUTCOME_CHECK_MINUTES: int = 5
CANDLE_MINUTES:        int = 5

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"

# ── Trend Scanner (4H/1D Fibonacci alert) ────────────────────────
TREND_N_COINS:            int   = 150   # top N by 24h volume; MEXC throttle → fewer succeed
TREND_SCAN_WORKERS:       int   = 8     # concurrent kline-fetch threads
TREND_PIVOT_LOOKBACK:     int   = 5     # bars each side required to confirm a pivot
TREND_MIN_IMPULSE_PCT:    float = 5.0   # min impulse size on 4H (%)
TREND_MIN_IMPULSE_1D:     float = 8.0   # min impulse size on 1D (%)
TREND_ADX_MIN:            int   = 25    # ADX threshold — directional, not ranging
TREND_ADX_PERIOD:         int   = 14
TREND_KLINE_COUNT_4H:     int   = 100   # 4H bars fetched (~16 days)
TREND_KLINE_COUNT_1D:     int   = 100   # 1D bars fetched (~3 months)
# Momentum quality filters — detect strong impulse moves (consecutive bullish/bearish candles)
TREND_IMPULSE_WINDOW:     int   = 15    # candles before pivot to inspect for momentum
TREND_MIN_MOMENTUM_RATIO: float = 0.60  # ≥60% of impulse candles must close in trend direction
TREND_MIN_BODY_RATIO:     float = 0.40  # avg body/range ≥40% — filters doji/wick-heavy candles
