import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# ── CoinGlass (optional) ─────────────────────────────────────────
COINGLASS_API_KEY: str = os.getenv("COINGLASS_API_KEY", "")

# ── Coin pool ────────────────────────────────────────────────────
EXCLUDE_COINS: set[str] = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "XAUT_USDT"}
TOP_N_COINS:   int      = 40
COIN_REFRESH_HOURS: int = 4

# ── Timeframes ───────────────────────────────────────────────────
MTF_1H:  str = "1h"    # tier 1: trend direction
MTF_15M: str = "15m"   # tier 2: liquidity sweep detection
ENTRY_TF: str = "5m"   # tier 3: confirmation + outcome tracking

# ── EMA periods (applied on 1H) ───────────────────────────────────
EMA_50:  int = 50    # short-term trend
EMA_200: int = 200   # main trend filter

# ── RSI ──────────────────────────────────────────────────────────
RSI_PERIOD: int = 14

# ── Volume filter (on 5M confirmation candle) ─────────────────────
VOLUME_MA_BARS:  int   = 20
VOLUME_MIN_MULT: float = 1.3   # confirmation candle must be 1.3× MA

# ── Trade params ─────────────────────────────────────────────────
LEVERAGE:      int   = 10
REWARD_RATIO:  float = 2.0    # TP2 = 2R; TP1 = 1R (shown in message)
SL_ATR_BUFFER: float = 0.5   # additional SL distance below/above swept level in ATR units
MAX_RISK_PCT:  float = 2.0   # skip signals where SL is > 2% away from entry

# ── Scheduler ────────────────────────────────────────────────────
SIGNAL_COOLDOWN_MINUTES: int = 60    # 1h per coin
SIGNAL_EXPIRE_HOURS:     int = 4
MAX_CONCURRENT_SIGNALS:  int = 10

SCAN_CRON_MINUTES:     str = "1,6,11,16,21,26,31,36,41,46,51,56"   # every 5 min (1m past candle close)
SIGNALS_PER_SCAN:      int = 3     # top N signals to send each scan (targets 10-20/day)
OUTCOME_CHECK_MINUTES: int = 5
CANDLE_MINUTES:        int = 5

# ── MEXC Futures REST API ─────────────────────────────────────────
MEXC_BASE_URL = "https://contract.mexc.com/api/v1"

# ── Database ──────────────────────────────────────────────────────
DB_PATH = "signals.db"
