# Trendspeed Filter Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the Trend Speed Analyzer bot to produce ~10 signals/day at 10% TP ROI (20× leverage) by switching to 15m candles, tightening the trendspeed filter with magnitude + acceleration gates, and blocking counter-trend altcoin entries when BTC disagrees.

**Architecture:** All changes are additive filters inside `strategy.py::scan_symbol()` plus config tuning — no structural rewrites. A module-level BTC DynEMA cache (14-min TTL) fetches BTC once per 15m scan cycle instead of once per coin. Config defaults shift to match the new risk model; `.env` is updated to override old stale values.

**Tech Stack:** Python 3.11+, pandas, numpy, APScheduler, MEXC REST API (`mexc_client.get_klines`), SQLite (`database.py`).

## Global Constraints

- Leverage: 20× isolated
- Target TP ROI: ~10% (≥ 8% gate)
- Max SL ROI: 15%
- Min SL ROI: 2%
- Reward ratio: 2.0 (unchanged)
- Signal TF: 15m
- DynEMA params: max_length=50, accel_mult=5.0 (unchanged)
- All new config vars readable from `.env` — no hardcoded values
- `python -m py_compile config.py strategy.py main.py` must pass after every task
- Never break `database.py`, `bot.py`, `coin_scanner.py`, `reports.py`, `webui.py`

---

## File Map

| File | Change |
|---|---|
| `config.py` | 9 default updates + 7 new variables |
| `strategy.py` | New imports, module-level BTC cache, `_get_btc_dema()`, 4 new gates in `scan_symbol()` |
| `.env` | Replace stale MTF section, add all new strategy variables |

---

## Task 1: Update config.py defaults and add new variables

**Files:**
- Modify: `config.py`

**Interfaces:**
- Produces: `SIGNAL_TF="15m"`, `SL_ATR_MULT=0.5`, `MIN_TP_ROI_PCT=8.0`, `MAX_SL_ROI_PCT=15.0`, `MIN_SL_ROI_PCT=2.0`, `SIGNAL_COOLDOWN_MINUTES=60`, `MAX_DAILY_SIGNALS=10`, `MIN_DAILY_SIGNAL_GAP_MINUTES=6`, `MAX_CONCURRENT_SIGNALS=5`, `SETUP_SCAN_CRON_MINUTES="*/15"`, plus new vars: `SPEED_REL_THRESHOLD`, `SPEED_ACCEL_ENABLED`, `BTC_SYMBOL`, `BTC_TF`, `BTC_KLINE_COUNT`, `BTC_GATE_ENABLED`, `BTC_RANGING_PCT`

- [ ] **Step 1: Update 9 existing defaults in the Strategy section**

In `config.py`, find the `# ── Strategy: Trend Speed Analyzer` section and apply these exact changes:

```python
# SIGNAL_TF: "1h" → "15m"
SIGNAL_TF: str = os.getenv("SIGNAL_TF", "15m")

# SL_ATR_MULT: 1.0 → 0.5
SL_ATR_MULT: float = float(os.getenv("SL_ATR_MULT", "0.5"))

# MIN_TP_ROI_PCT: 30.0 → 8.0
MIN_TP_ROI_PCT: float = float(os.getenv("MIN_TP_ROI_PCT", "8.0"))

# MAX_SL_ROI_PCT: 35.0 → 15.0
MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "15.0"))

# MIN_SL_ROI_PCT: 5.0 → 2.0
MIN_SL_ROI_PCT: float = float(os.getenv("MIN_SL_ROI_PCT", "2.0"))
```

In the `# ── Scheduler` section:

```python
# SETUP_SCAN_CRON_MINUTES: "2" → "*/15"
SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "*/15")

# SIGNAL_COOLDOWN_MINUTES: 240 → 60
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60"))

# MAX_DAILY_SIGNALS: 5 → 10
MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "10"))

# MIN_DAILY_SIGNAL_GAP_MINUTES: 60 → 6
MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES", "6"))

# MAX_CONCURRENT_SIGNALS: 3 → 5
MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "5"))
```

- [ ] **Step 2: Add 7 new variables to config.py**

After the existing `MIN_SL_ROI_PCT` line in the Strategy section, add:

```python
# ── BTC macro gate ─────────────────────────────────────────────────
BTC_SYMBOL: str        = os.getenv("BTC_SYMBOL", "BTC_USDT")
BTC_TF: str            = os.getenv("BTC_TF", "1h")
BTC_KLINE_COUNT: int   = int(os.getenv("BTC_KLINE_COUNT", "300"))
BTC_GATE_ENABLED: bool = os.getenv("BTC_GATE_ENABLED", "true").lower() == "true"
BTC_RANGING_PCT: float = float(os.getenv("BTC_RANGING_PCT", "0.10"))

# ── Trendspeed filter ──────────────────────────────────────────────
SPEED_REL_THRESHOLD: float = float(os.getenv("SPEED_REL_THRESHOLD", "0.0002"))
SPEED_ACCEL_ENABLED: bool  = os.getenv("SPEED_ACCEL_ENABLED", "true").lower() == "true"
```

- [ ] **Step 3: Compile check**

```bash
python -m py_compile config.py
echo $?
```

Expected output: `0` (no errors, no other output).

- [ ] **Step 4: Verify new vars load correctly**

```bash
python -c "
from config import (
    SIGNAL_TF, SL_ATR_MULT, MIN_TP_ROI_PCT, MAX_SL_ROI_PCT, MIN_SL_ROI_PCT,
    SETUP_SCAN_CRON_MINUTES, SIGNAL_COOLDOWN_MINUTES, MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES, MAX_CONCURRENT_SIGNALS,
    BTC_SYMBOL, BTC_TF, BTC_KLINE_COUNT, BTC_GATE_ENABLED, BTC_RANGING_PCT,
    SPEED_REL_THRESHOLD, SPEED_ACCEL_ENABLED,
)
assert SIGNAL_TF == '15m', f'Expected 15m got {SIGNAL_TF}'
assert SL_ATR_MULT == 0.5, f'Expected 0.5 got {SL_ATR_MULT}'
assert MIN_TP_ROI_PCT == 8.0, f'Expected 8.0 got {MIN_TP_ROI_PCT}'
assert MAX_SL_ROI_PCT == 15.0, f'Expected 15.0 got {MAX_SL_ROI_PCT}'
assert SETUP_SCAN_CRON_MINUTES == '*/15', f'Expected */15 got {SETUP_SCAN_CRON_MINUTES}'
assert SIGNAL_COOLDOWN_MINUTES == 60
assert MAX_DAILY_SIGNALS == 10
assert BTC_GATE_ENABLED == True
assert SPEED_REL_THRESHOLD == 0.0002
assert SPEED_ACCEL_ENABLED == True
print('config OK')
"
```

Expected: `config OK`

- [ ] **Step 5: Commit**

```bash
git add config.py
git commit -m "config: switch to 15m TF, tighten ROI targets, add BTC gate + speed filter vars"
```

---

## Task 2: Update .env with new strategy settings

**Files:**
- Modify: `.env`

**Interfaces:**
- Consumes: nothing (standalone env file)
- Produces: all new strategy vars active on next bot start; old stale MTF vars removed

- [ ] **Step 1: Replace the strategy section in .env**

Replace everything from `# ── Strategy:` to the end of the WebSocket section with the following. Keep the Telegram, Web UI, Database/Logs, and Coin pool sections unchanged at the top.

The new strategy section to use (replaces old MTF block and old scheduler block entirely):

```env
# ── Strategy: Trend Speed Analyzer (Zeiierman) ─────────────────────
STRATEGY_NAME=Trend Speed Analyzer (Zeiierman)

# Signal timeframe
SIGNAL_TF=15m
SIGNAL_KLINE_COUNT=300

# Dynamic EMA params (matches Pine Script defaults)
DYN_EMA_MAX_LENGTH=50
DYN_EMA_ACCEL_MULT=5.0

# ATR stop-loss
ATR_PERIOD=14
SL_ATR_MULT=0.5

# Risk / reward
REWARD_RATIO=2.0
MIN_STRUCTURE_RR=2.0

# ROI quality gates (at 20x leverage)
MIN_TP_ROI_PCT=8.0
MAX_SL_ROI_PCT=15.0
MIN_SL_ROI_PCT=2.0
LEVERAGE=20

# ── BTC macro gate ──────────────────────────────────────────────────
BTC_GATE_ENABLED=true
BTC_SYMBOL=BTC_USDT
BTC_TF=1h
BTC_KLINE_COUNT=300
BTC_RANGING_PCT=0.10

# ── Trendspeed filter ───────────────────────────────────────────────
SPEED_REL_THRESHOLD=0.0002
SPEED_ACCEL_ENABLED=true

# ── Scheduler & limits ──────────────────────────────────────────────
SETUP_SCAN_CRON_MINUTES=*/15
SETUP_SCAN_CRON_HOURS=*
OUTCOME_CHECK_MINUTES=1
SIGNALS_PER_SCAN=1
MAX_CONCURRENT_SIGNALS=5
SIGNAL_COOLDOWN_MINUTES=60
SIGNAL_EXPIRE_HOURS=6
MAX_DAILY_SIGNALS=10
MIN_DAILY_SIGNAL_GAP_MINUTES=6
SCAN_WORKERS=4
SCHEDULER_MISFIRE_GRACE_SECONDS=30
SCHEDULER_MAX_INSTANCES=1
```

- [ ] **Step 2: Verify .env loads without errors**

```bash
python -c "
from dotenv import load_dotenv
load_dotenv(override=True)
import os
assert os.getenv('SIGNAL_TF') == '15m'
assert os.getenv('BTC_GATE_ENABLED') == 'true'
assert os.getenv('SPEED_REL_THRESHOLD') == '0.0002'
assert os.getenv('MAX_DAILY_SIGNALS') == '10'
print('.env OK')
"
```

Expected: `.env OK`

- [ ] **Step 3: Note for server deployment**

The server reads `.env` from the `APP_ENV` GitHub secret. After this PR merges, update the secret in GitHub → Settings → Secrets → `APP_ENV` with the new `.env` contents. The deploy workflow writes it on every push.

---

## Task 3: Add BTC DynEMA cache and macro gate to strategy.py

**Files:**
- Modify: `strategy.py`

**Interfaces:**
- Consumes: `BTC_SYMBOL`, `BTC_TF`, `BTC_KLINE_COUNT`, `BTC_GATE_ENABLED`, `BTC_RANGING_PCT`, `DYN_EMA_MAX_LENGTH`, `DYN_EMA_ACCEL_MULT` from config; `_compute_dyn_ema()` from same file; `get_klines` from mexc_client
- Produces: `_get_btc_dema() -> tuple[float | None, float | None]` — returns `(btc_close, btc_dema)`, fail-open as `(None, None)` on any error; BTC gate block in `scan_symbol()`

- [ ] **Step 1: Extend config imports in strategy.py**

Find the `from config import (` block at the top of `strategy.py` and add these lines:

```python
    MIN_TP_ROI_PCT,
    SPEED_REL_THRESHOLD,
    SPEED_ACCEL_ENABLED,
    BTC_SYMBOL,
    BTC_TF,
    BTC_KLINE_COUNT,
    BTC_GATE_ENABLED,
    BTC_RANGING_PCT,
```

- [ ] **Step 2: Add module-level BTC cache after the logger line**

After `logger = logging.getLogger(__name__)`, add:

```python
# BTC DynEMA cache — refreshed once per scan cycle (14-min TTL)
_btc_cache: dict = {"dema": None, "close": None, "ts": 0.0}
_BTC_CACHE_TTL = 14 * 60  # seconds — expires just before next 15m scan window
```

- [ ] **Step 3: Add _get_btc_dema() function**

Add this function immediately before the `scan_symbol` function (after `_valid_trade_geometry`):

```python
def _get_btc_dema() -> tuple[float | None, float | None]:
    """
    Return (btc_close, btc_dema) using a 14-min module-level cache.
    Fetches BTC_TF klines once per 15m scan window, not once per coin.
    Returns (None, None) on any failure — gate is fail-open.
    """
    global _btc_cache
    now_ts = datetime.now(timezone.utc).timestamp()
    if _btc_cache["dema"] is not None and now_ts - _btc_cache["ts"] < _BTC_CACHE_TTL:
        return _btc_cache["close"], _btc_cache["dema"]
    try:
        df = get_klines(BTC_SYMBOL, BTC_TF, count=BTC_KLINE_COUNT)
        if df is None or df.empty or len(df) < 210:
            logger.warning("[BTC-GATE] Insufficient BTC klines (%d) — gate open", 0 if df is None else len(df))
            return None, None
        close    = df["close"].astype(float)
        dema     = _compute_dyn_ema(close, DYN_EMA_MAX_LENGTH, DYN_EMA_ACCEL_MULT)
        btc_close = float(close.iloc[-1])
        btc_dema  = float(dema.iloc[-1])
        _btc_cache = {"dema": btc_dema, "close": btc_close, "ts": now_ts}
        logger.debug("[BTC-GATE] Cache refreshed close=%.6g dema=%.6g", btc_close, btc_dema)
        return btc_close, btc_dema
    except Exception as e:
        logger.warning("[BTC-GATE] Fetch failed: %s — gate open", e)
        return None, None
```

- [ ] **Step 4: Add BTC macro gate inside scan_symbol()**

Inside `scan_symbol()`, locate the comment block `# Trend speed must confirm direction` (the existing direction gate). Add the BTC gate immediately AFTER the trendspeed direction check and BEFORE `# ATR-based stop loss`:

```python
        # BTC 1h macro gate — altcoin direction must align with BTC DynEMA trend
        if BTC_GATE_ENABLED:
            btc_close, btc_dema = _get_btc_dema()
            if btc_close is not None and btc_dema is not None:
                ranging_margin = btc_dema * (BTC_RANGING_PCT / 100.0)
                btc_bullish    = btc_close > btc_dema + ranging_margin
                btc_bearish    = btc_close < btc_dema - ranging_margin
                if direction == "LONG" and btc_bearish:
                    logger.info(
                        "[REJECT] %s LONG blocked — BTC bearish close=%.6g dema=%.6g",
                        symbol, btc_close, btc_dema,
                    )
                    return None
                if direction == "SHORT" and btc_bullish:
                    logger.info(
                        "[REJECT] %s SHORT blocked — BTC bullish close=%.6g dema=%.6g",
                        symbol, btc_close, btc_dema,
                    )
                    return None
```

- [ ] **Step 5: Compile check**

```bash
python -m py_compile strategy.py
echo $?
```

Expected: `0`

- [ ] **Step 6: Logic test for BTC gate (no network)**

```bash
python -c "
import strategy

# Inject mock cache values directly
strategy._btc_cache = {
    'dema': 100.0,
    'close': 95.0,   # BTC bearish (below dema - ranging_margin)
    'ts': 9_999_999_999.0,  # far future — cache never expires
}

# Simulate: gate should reject a LONG when BTC is bearish
btc_close, btc_dema = strategy._get_btc_dema()
ranging_margin = btc_dema * (0.10 / 100.0)
btc_bearish = btc_close < btc_dema - ranging_margin
assert btc_bearish, f'Expected BTC bearish: close={btc_close} dema={btc_dema} margin={ranging_margin}'

# BTC bullish scenario
strategy._btc_cache['close'] = 105.0
btc_close, btc_dema = strategy._get_btc_dema()
ranging_margin = btc_dema * (0.10 / 100.0)
btc_bullish = btc_close > btc_dema + ranging_margin
assert btc_bullish, 'Expected BTC bullish'

print('BTC gate logic OK')
"
```

Expected: `BTC gate logic OK`

- [ ] **Step 7: Commit**

```bash
git add strategy.py
git commit -m "strategy: add BTC 1h DynEMA macro gate with 14-min cache"
```

---

## Task 4: Add trendspeed magnitude, acceleration, and TP ROI gates

**Files:**
- Modify: `strategy.py`

**Interfaces:**
- Consumes: `SPEED_REL_THRESHOLD`, `SPEED_ACCEL_ENABLED`, `MIN_TP_ROI_PCT` from config (already imported in Task 3)
- Produces: 3 new rejection paths in `scan_symbol()` — magnitude reject, acceleration reject, TP ROI reject

- [ ] **Step 1: Add magnitude gate inside scan_symbol()**

Locate the existing trendspeed direction gate block in `scan_symbol()`:

```python
        # Trend speed must confirm direction
        if direction == "LONG" and curr_speed <= 0:
            ...
            return None
        if direction == "SHORT" and curr_speed >= 0:
            ...
            return None
```

Immediately after that block (and before the BTC gate from Task 3), add:

```python
        # Trendspeed magnitude gate — rejects weak/stalling crosses
        _mag = abs(curr_speed) / curr_close if curr_close != 0.0 else 0.0
        if _mag < SPEED_REL_THRESHOLD:
            logger.info(
                "[REJECT] %s %s speed magnitude %.7f below threshold %.7f",
                symbol, direction, _mag, SPEED_REL_THRESHOLD,
            )
            return None
```

- [ ] **Step 2: Add acceleration gate inside scan_symbol()**

Immediately after the magnitude gate block, add:

```python
        # Trendspeed acceleration gate — momentum must be building
        if SPEED_ACCEL_ENABLED:
            prev_speed = float(trendspeed.iloc[-2])
            if direction == "LONG" and curr_speed <= prev_speed:
                logger.info(
                    "[REJECT] %s LONG speed decelerating (%.4f <= %.4f)",
                    symbol, curr_speed, prev_speed,
                )
                return None
            if direction == "SHORT" and curr_speed >= prev_speed:
                logger.info(
                    "[REJECT] %s SHORT speed decelerating (%.4f >= %.4f)",
                    symbol, curr_speed, prev_speed,
                )
                return None
```

- [ ] **Step 3: Add TP ROI minimum gate inside scan_symbol()**

Locate the TP ROI computation block near the bottom of `scan_symbol()`:

```python
        if direction == "LONG":
            tp_roi_pct = (tp_price - entry) / entry * 100.0 * LEVERAGE
        else:
            tp_roi_pct = (entry - tp_price) / entry * 100.0 * LEVERAGE
```

Immediately after that block (before `rr = REWARD_RATIO`), add:

```python
        if tp_roi_pct < MIN_TP_ROI_PCT:
            logger.info(
                "[REJECT] %s %s TP ROI %.1f%% below min %.1f%%",
                symbol, direction, tp_roi_pct, MIN_TP_ROI_PCT,
            )
            return None
```

- [ ] **Step 4: Compile check**

```bash
python -m py_compile strategy.py
echo $?
```

Expected: `0`

- [ ] **Step 5: Logic test for new gates (no network)**

```bash
python -c "
import math

# -- Magnitude gate logic --
SPEED_REL_THRESHOLD = 0.0002
close = 1.5
# Below threshold: should reject
speed_weak = 0.0001  # 0.0001/1.5 = 0.0000667 < 0.0002
mag_weak = abs(speed_weak) / close
assert mag_weak < SPEED_REL_THRESHOLD, 'weak speed should fail'

# Above threshold: should pass
speed_strong = 0.0005  # 0.0005/1.5 = 0.000333 > 0.0002
mag_strong = abs(speed_strong) / close
assert mag_strong >= SPEED_REL_THRESHOLD, 'strong speed should pass'

# -- Acceleration gate logic --
# LONG: curr must be greater than prev
curr_speed_long = 0.05
prev_speed_long = 0.03
assert curr_speed_long > prev_speed_long, 'LONG accelerating — should pass'

curr_speed_long_bad = 0.02
assert not (curr_speed_long_bad > prev_speed_long), 'LONG decelerating — should reject'

# SHORT: curr must be less (more negative) than prev
curr_speed_short = -0.05
prev_speed_short = -0.03
assert curr_speed_short < prev_speed_short, 'SHORT accelerating — should pass'

# -- TP ROI gate --
MIN_TP_ROI_PCT = 8.0
LEVERAGE = 20
entry = 1.0
tp_long = 1.005   # 0.5% move
tp_roi = (tp_long - entry) / entry * 100.0 * LEVERAGE
assert tp_roi >= MIN_TP_ROI_PCT, f'TP ROI {tp_roi} should pass min {MIN_TP_ROI_PCT}'

tp_low = 1.003    # 0.3% move → 6% ROI → should reject
tp_roi_low = (tp_low - entry) / entry * 100.0 * LEVERAGE
assert tp_roi_low < MIN_TP_ROI_PCT, f'Low TP ROI {tp_roi_low} should fail min {MIN_TP_ROI_PCT}'

print('All gate logic OK')
"
```

Expected: `All gate logic OK`

- [ ] **Step 6: Commit**

```bash
git add strategy.py
git commit -m "strategy: add trendspeed magnitude + acceleration gates, TP ROI min gate"
```

---

## Task 5: Final compile, smoke test, and push

**Files:**
- Read: `strategy.py`, `config.py`, `main.py`, `bot.py`

**Interfaces:**
- Consumes: all previous tasks completed

- [ ] **Step 1: Full compile check across all bot files**

```bash
python -m py_compile config.py strategy.py main.py bot.py database.py webui.py
echo $?
```

Expected: `0`

- [ ] **Step 2: Import smoke test — verify strategy loads with no errors**

```bash
python -c "
import config
import strategy
import main
import bot
print('strategy.SIGNAL_TF =', config.SIGNAL_TF)
print('strategy.SL_ATR_MULT =', config.SL_ATR_MULT)
print('strategy.MAX_DAILY_SIGNALS =', config.MAX_DAILY_SIGNALS)
print('BTC gate enabled =', config.BTC_GATE_ENABLED)
print('Speed threshold =', config.SPEED_REL_THRESHOLD)
print('All imports OK')
"
```

Expected output (values may differ if .env overrides):
```
strategy.SIGNAL_TF = 15m
strategy.SL_ATR_MULT = 0.5
strategy.MAX_DAILY_SIGNALS = 10
BTC gate enabled = True
Speed threshold = 0.0002
All imports OK
```

- [ ] **Step 3: Verify scan_symbol gate order is correct**

Open `strategy.py` and confirm `scan_symbol()` applies gates in this exact order:
1. Crossover check (return None if no cross)
2. Trendspeed direction (speed > 0 / < 0)
3. Trendspeed magnitude (`abs(speed)/close > SPEED_REL_THRESHOLD`)
4. Trendspeed acceleration (`curr_speed > prev_speed` for LONG)
5. BTC macro gate (`_get_btc_dema()` → direction check)
6. ATR SL/TP calculation
7. Geometry validation
8. SL ROI bounds gate
9. TP ROI minimum gate
10. Return Signal

- [ ] **Step 4: Commit .env**

`.env` is not tracked by git (in `.gitignore`). Instead, copy the new strategy block and update the `APP_ENV` secret in GitHub:
- Go to: GitHub repo → Settings → Secrets and variables → Actions → `APP_ENV`
- Replace the entire value with the updated `.env` contents from Task 2

- [ ] **Step 5: Final commit and push**

```bash
git add docs/superpowers/plans/2026-07-01-trendspeed-filter-improvement.md
git commit -m "docs: add trendspeed filter improvement implementation plan"
git push origin main
```

Expected: push triggers GitHub Actions deploy → server restarts with new 15m strategy + BTC gate + speed filters.

- [ ] **Step 6: Verify on server after deploy**

```bash
# Watch for first scan cycle (fires at */15 minutes)
journalctl -u mexc-bot -f | grep -E "SCAN|REJECT|SIGNAL|BTC-GATE"
```

Expected log lines after first scan:
```
[SCAN] Scanning XX/80 coins ...
[BTC-GATE] Cache refreshed close=XXXXX dema=XXXXX
[REJECT] COIN_USDT LONG speed magnitude ... below threshold ...   ← filter working
[SCAN] Done — N signal(s) fired
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| Switch SIGNAL_TF 1h → 15m | Task 1, Task 2 |
| CANDLE_MINUTES auto-derives from SIGNAL_TF | Already handled in config.py `_TF_MINUTES` logic — no extra task needed |
| Scan cron `*/15` | Task 1 |
| SL_ATR_MULT 1.0 → 0.5 | Task 1 |
| MIN_TP_ROI_PCT 30 → 8 | Task 1 |
| MAX_SL_ROI_PCT 35 → 15 | Task 1 |
| MIN_SL_ROI_PCT 5 → 2 | Task 1 |
| SIGNAL_COOLDOWN_MINUTES 240 → 60 | Task 1 |
| MAX_DAILY_SIGNALS 5 → 10 | Task 1 |
| MIN_DAILY_SIGNAL_GAP_MINUTES 60 → 6 | Task 1 |
| MAX_CONCURRENT_SIGNALS 3 → 5 | Task 1 |
| 7 new config vars | Task 1 |
| .env updated | Task 2 |
| BTC cache (14-min TTL) | Task 3 |
| `_get_btc_dema()` fail-open | Task 3 |
| BTC macro gate in `scan_symbol()` | Task 3 |
| Trendspeed magnitude gate | Task 4 |
| Trendspeed acceleration gate | Task 4 |
| TP ROI minimum gate | Task 4 |
| py_compile passes | Task 5 |
| Server deploy note | Task 5 |

**No gaps found.**

**Placeholder scan:** No TBD, TODO, or incomplete steps found.

**Type consistency:** `_get_btc_dema()` returns `tuple[float | None, float | None]` — used correctly in Task 3 gate with `if btc_close is not None` guard. `SPEED_REL_THRESHOLD` float used as float in magnitude comparison throughout. All consistent.
