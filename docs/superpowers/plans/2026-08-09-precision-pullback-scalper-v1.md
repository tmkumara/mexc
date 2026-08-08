# Precision Pullback Scalper v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the live MEXC signal-bot strategy (Binocular Pending-Breakout v1) with a fresh implementation of "Precision Pullback Scalper v1" — a dual-timeframe EMA-trend + pullback + RSI-reset + volume/ATR-confirmation strategy with fixed TP/SL and a single breakeven step — directly on `main`, after cutting a backup branch of the current state.

**Architecture:** `strategy.py`'s pipeline is fully rewritten (new indicators, new `detect_pending_setup`/`check_setup_confirmation`, new scoring). `outcome_check.py` gains a single-target breakeven-aware outcome walker, replacing the two Binocular-era walkers. `main.py`/`bot.py`/`webui.py`/`reports.py` are trimmed to one strategy (no more `STRATEGY_V1_ENABLED`/`SCALPER_V3_ENABLED` branching) and gain a three-way `win`/`loss`/`breakeven` outcome status. The dormant Super Scalper v3 strategy and its exclusive backtest tooling are deleted outright.

**Tech Stack:** Python 3, pandas/numpy, APScheduler, python-telegram-bot, FastAPI (dashboard), SQLite, pytest.

## Global Constraints

- Full rewrite happens as commits **directly on `main`** — no feature branch (explicit user instruction). Step 1 cuts a backup branch of current `main` HEAD first.
- `DRY_RUN=true` stays the default throughout — this plan does not flip the bot live.
- Fixed risk model at `LEVERAGE=20`: TP `+7.0%` ROI, SL `-10.0%` ROI (raw RR **0.70:1** by construction — no `MIN_RR` gate for this strategy). Breakeven triggers at `+4.0%` ROI.
- Only fully closed candles are ever evaluated anywhere in the pipeline (`iloc[:-1]` convention, both `TREND_TF` and `ENTRY_TF`).
- Every confirmed setup must satisfy `valid_trade_geometry` (LONG: `tp > entry > sl`; SHORT: `tp < entry < sl`).
- Reference spec: `docs/superpowers/specs/2026-08-09-precision-pullback-scalper-v1-design.md`. Where this plan and the spec disagree on a factual detail (see Task 15's note), the plan — written after re-verifying the actual codebase — is authoritative.

---

### Task 1: Cut and push the backup branch

**Files:** none (git operation only).

- [ ] **Step 1: Verify a clean working tree**

Run: `git status --short`
Expected: only the two pre-existing untracked files from before this session (`backtest/tpsl_walkforward.py`, `backtestfull.log`) — nothing else uncommitted. If anything else is dirty, stop and ask before proceeding (do not discard unknown work).

- [ ] **Step 2: Cut the backup branch from current `main` HEAD**

```bash
git branch backup/main-pre-precision-pullback-scalper-v1
git push origin backup/main-pre-precision-pullback-scalper-v1
```

- [ ] **Step 3: Confirm it exists on origin**

Run: `git ls-remote --heads origin backup/main-pre-precision-pullback-scalper-v1`
Expected: one line showing the branch and its commit hash (should match `git rev-parse main`).

No commit for this task — it's a branch operation, not a file change.

---

### Task 2: Add test fixtures for the new strategy

**Files:**
- Modify: `tests/strategy_fixtures.py`

**Interfaces:**
- Produces: `make_trend_df(direction, bars=260, start_price=100.0, freq="15min") -> pd.DataFrame`, `make_pullback_confirmation_df(direction, bars=260, start_price=100.0) -> pd.DataFrame`, `patch_klines_multi(monkeypatch, strategy_module, dfs_by_interval: dict[str, pd.DataFrame]) -> None`. `make_15m_trend_df` and `patch_klines` (existing) are kept unchanged for tasks that still need a single timeframe.

- [ ] **Step 1: Add `make_trend_df`, keep `make_15m_trend_df` as a thin wrapper, add `make_pullback_confirmation_df` and `patch_klines_multi`**

Replace the full content of `tests/strategy_fixtures.py` with:

```python
"""
Deterministic OHLCV fixture builders for strategy tests.

Numeric constants here are reasoned, not hand-executed against pandas --
if a test using these fails for the wrong reason (RSI/EMA-distance/ATR
ratio landing outside the expected band), adjust the constants below and
re-run. That is expected TDD iteration, not a defect in the test itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_trend_df(
    direction: str = "LONG", bars: int = 260, start_price: float = 100.0, freq: str = "15min"
) -> pd.DataFrame:
    """A steadily trending, noiseless series -- long enough for EMA200 +
    its slope lookback to settle cleanly on either TREND_TF or ENTRY_TF.
    Ends with one extra duplicated row so callers can safely `iloc[:-1]`
    to drop the "forming" candle."""
    idx = pd.date_range("2026-01-01", periods=bars, freq=freq)
    step = 0.15 if direction == "LONG" else -0.15
    closes = start_price + np.arange(bars) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_15m_trend_df(direction: str = "LONG", bars: int = 220, start_price: float = 100.0) -> pd.DataFrame:
    """Kept for existing callers -- delegates to make_trend_df with freq="15min"."""
    return make_trend_df(direction, bars=bars, start_price=start_price, freq="15min")


def make_pullback_confirmation_df(
    direction: str = "LONG", bars: int = 260, start_price: float = 100.0
) -> pd.DataFrame:
    """ENTRY_TF (5m) fixture: a steady trend (EMA20/EMA50 aligned and
    separated) for most of the series, a 3-candle pullback toward EMA20
    that pulls RSI14 down/up into the reset zone, then a strong
    confirming candle (closes beyond open/EMA20/prior high-low, elevated
    volume) that should satisfy detect_pending_setup's full pipeline.
    SHORT mirrors every inequality. Ends with one duplicated last row so
    callers can safely iloc[:-1] to drop the "forming" candle, matching
    every other fixture in this module."""
    sign = 1.0 if direction == "LONG" else -1.0
    trend_bars = bars - 4
    step = 0.25 * sign
    closes = list(start_price + np.arange(trend_bars) * step)
    trend_last = closes[-1]

    pullback = [
        trend_last - 0.9 * sign,
        trend_last - 1.6 * sign,
        trend_last - 1.9 * sign,
    ]
    closes.extend(pullback)

    confirm_close = pullback[-1] + 3.0 * sign
    closes.append(confirm_close)

    closes = np.array(closes)
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]

    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    volumes[-1] = 1400.0   # 1.4x -- above VOLUME_CONFIRM_MULT's default 1.15x

    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_klines(monkeypatch, strategy_module, df: pd.DataFrame) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a single fixture."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        return df

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)


def patch_klines_multi(monkeypatch, strategy_module, dfs_by_interval: dict) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a
    per-interval fixture -- needed once a strategy fetches more than one
    timeframe (TREND_TF and ENTRY_TF) in the same detection pass."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        import pandas as pd
        return dfs_by_interval.get(interval, pd.DataFrame())

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)
```

- [ ] **Step 2: Verify existing callers of the old fixtures still work**

Run: `python -m pytest tests/test_indicators.py tests/test_outcome_check.py -v`
Expected: all PASS (these files don't use `strategy_fixtures` at all, so this just confirms nothing else broke on import).

- [ ] **Step 3: Commit**

```bash
git add tests/strategy_fixtures.py
git commit -m "test: add Precision Pullback Scalper v1 fixture builders"
```

---

### Task 3: Rewrite `config.py`

**Files:**
- Modify: `config.py` (full-file rewrite of the strategy-specific sections; coin-pool/scheduler/DB/log/API sections at the top and bottom are untouched)

**Interfaces:**
- Produces every new config constant referenced by later tasks: `TREND_TF`, `EMA_FAST_LEN`, `EMA_SLOW_LEN`, `EMA_TREND_LEN`, `EMA_TREND_SLOPE_LOOKBACK`, `EMA_SEPARATION_MIN_PCT`, `RSI_PERIOD`, `RSI_LONG_RESET_MIN/MAX`, `RSI_SHORT_RESET_MIN/MAX`, `PULLBACK_LOOKBACK_BARS`, `PULLBACK_PREFERRED_DISTANCE_PCT`, `NO_CHASE_MAX_DISTANCE_PCT`, `VOLUME_MA_PERIOD`, `VOLUME_CONFIRM_MULT`, `MAX_CANDLE_BODY_PCT`, `ATR_MIN_PCT`, `ATR_MAX_PCT`, `MIN_SIGNAL_SCORE`, `TP_ROI_PCT`, `TP_PRICE_PCT`, `BREAKEVEN_TRIGGER_ROI_PCT`, `BREAKEVEN_TRIGGER_PRICE_PCT`.
- Removes: `RIBBON_MA1_LEN..RIBBON_MA5_LEN`, `RIBBON_BASELINE_LEN`, `SIGNAL_MODE`, `CONFIRMATION_TIMEFRAMES`, `MTF_MIN_CONFIRMATIONS`, `ACCOUNT_BALANCE`, `RISK_PERCENT_PER_TRADE`, `PVT_SIGNAL_TYPE`, `PVT_SIGNAL_LENGTH`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `BINOCULAR_EMA200_LEN`, `TARGET1/2/3_CLOSE_FRACTION`, `MOVE_SL_TO_BREAKEVEN_AFTER_T1`, `MIN_RR`, `STRATEGY_V1_ENABLED`, every `SCALPER_V3_*` constant, `STRATEGY_NAME_V3`.

- [ ] **Step 1: Replace lines 57-210 of `config.py`**

Read the current file first (`config.py:57-210` spans from the `# ── Strategy: Binocular Pending-Breakout v1` header through the end of the `STRATEGY_NAME_V3` line). Replace that entire span with:

```python
# ── Strategy: Precision Pullback Scalper v1 ─────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Precision Pullback Scalper v1",
)

TREND_TF: str = os.getenv("TREND_TF", "15m")
ENTRY_TF: str = os.getenv("ENTRY_TF", "5m")
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "260"))
# EMA200 (trend filter, on TREND_TF) needs ~200 bars of warmup plus the
# slope lookback and margin; ENTRY_TF's own EMA200-agreement check needs
# the same on ENTRY_TF, plus RSI14/ATR14/VolumeMA20's much shorter
# warmup -- 260 covers all of it with margin.

EMA_FAST_LEN: int = int(os.getenv("EMA_FAST_LEN", "20"))
EMA_SLOW_LEN: int = int(os.getenv("EMA_SLOW_LEN", "50"))
EMA_TREND_LEN: int = int(os.getenv("EMA_TREND_LEN", "200"))
EMA_TREND_SLOPE_LOOKBACK: int = int(os.getenv("EMA_TREND_SLOPE_LOOKBACK", "5"))
EMA_SEPARATION_MIN_PCT: float = float(os.getenv("EMA_SEPARATION_MIN_PCT", "0.05")) / 100.0

RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_RESET_MIN: float = float(os.getenv("RSI_LONG_RESET_MIN", "42"))
RSI_LONG_RESET_MAX: float = float(os.getenv("RSI_LONG_RESET_MAX", "55"))
RSI_SHORT_RESET_MIN: float = float(os.getenv("RSI_SHORT_RESET_MIN", "45"))
RSI_SHORT_RESET_MAX: float = float(os.getenv("RSI_SHORT_RESET_MAX", "58"))
PULLBACK_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_LOOKBACK_BARS", "5"))

# architecture.txt gives two overlapping pullback-distance thresholds --
# a "preferred ~0.20%, reject beyond 0.35-0.40%" range and a separately
# emphasized "biggest win-rate improvement" 0.30% cap. Resolved (see
# design spec) as: NO_CHASE_MAX_DISTANCE_PCT is the hard reject, and
# PULLBACK_PREFERRED_DISTANCE_PCT only feeds the score.
PULLBACK_PREFERRED_DISTANCE_PCT: float = float(os.getenv("PULLBACK_PREFERRED_DISTANCE_PCT", "0.20")) / 100.0
NO_CHASE_MAX_DISTANCE_PCT: float = float(os.getenv("NO_CHASE_MAX_DISTANCE_PCT", "0.30")) / 100.0

VOLUME_MA_PERIOD: int = int(os.getenv("VOLUME_MA_PERIOD", "20"))
VOLUME_CONFIRM_MULT: float = float(os.getenv("VOLUME_CONFIRM_MULT", "1.15"))
MAX_CANDLE_BODY_PCT: float = float(os.getenv("MAX_CANDLE_BODY_PCT", "0.8")) / 100.0

ATR_MIN_PCT: float = float(os.getenv("ATR_MIN_PCT", "0.25")) / 100.0
ATR_MAX_PCT: float = float(os.getenv("ATR_MAX_PCT", "1.20")) / 100.0

MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))

# Minimum age (seconds) the last CLOSED candle must have before a signal
# can fire on it. MEXC's kline REST data for a just-closed candle can still
# get revised for a short window after the close.
MIN_CANDLE_SETTLE_SECONDS: int = int(os.getenv("MIN_CANDLE_SETTLE_SECONDS", "90"))

# ATR period backing the ATR% volatility-band filter.
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

ENABLE_LONG_SIGNALS: bool = os.getenv("ENABLE_LONG_SIGNALS", "true").lower() == "true"

# Fixed TP/SL sizing -- architecture.txt's core simplification: TP/SL are
# NOT derived from structure or ATR, they're flat ROI-%-at-LEVERAGE
# distances. Raw RR is therefore a fixed 0.70:1 by construction; there is
# no MIN_RR gate for this strategy (quality control is MIN_SIGNAL_SCORE).
MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "10.0"))
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE

TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))
TP_PRICE_PCT: float = TP_ROI_PCT / 100.0 / LEVERAGE

BREAKEVEN_TRIGGER_ROI_PCT: float = float(os.getenv("BREAKEVEN_TRIGGER_ROI_PCT", "4.0"))
BREAKEVEN_TRIGGER_PRICE_PCT: float = BREAKEVEN_TRIGGER_ROI_PCT / 100.0 / LEVERAGE

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # 0.02%
PENDING_SIGNAL_EXPIRY_CANDLES: int = int(os.getenv("PENDING_SIGNAL_EXPIRY_CANDLES", "3"))   # 3 x 5m = 15 min

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
```

This replacement ends exactly where the original `STRATEGY_NAME_V3` line did (`config.py:210`) — the file's next lines (the `LIVE_ENABLED` block, fee/slippage estimates, dry-run flags, scheduler, log, MEXC API, DB, and timeframe-map sections) are untouched and follow immediately after, so do **not** duplicate a `LIVE_ENABLED` definition here.

- [ ] **Step 2: Verify the file imports cleanly**

Run: `python -c "import config"`
Expected: no output, exit code 0.

- [ ] **Step 3: Verify every removed name is actually gone**

Run:
```bash
python -c "
import config
removed = ['RIBBON_MA1_LEN','RIBBON_MA2_LEN','RIBBON_MA3_LEN','RIBBON_MA4_LEN','RIBBON_MA5_LEN',
'RIBBON_BASELINE_LEN','SIGNAL_MODE','CONFIRMATION_TIMEFRAMES','MTF_MIN_CONFIRMATIONS',
'ACCOUNT_BALANCE','RISK_PERCENT_PER_TRADE','PVT_SIGNAL_TYPE','PVT_SIGNAL_LENGTH',
'RSI_FAST_PERIOD','RSI_SLOW_PERIOD','CHANDELIER_ATR_PERIOD','CHANDELIER_MULTIPLIER',
'BINOCULAR_EMA200_LEN','TARGET1_CLOSE_FRACTION','TARGET2_CLOSE_FRACTION',
'TARGET3_CLOSE_FRACTION','MOVE_SL_TO_BREAKEVEN_AFTER_T1','MIN_RR','STRATEGY_V1_ENABLED',
'STRATEGY_NAME_V3','SCALPER_V3_ENABLED','SCALPER_V3_TIMEFRAME']
present = [name for name in removed if hasattr(config, name)]
assert not present, f'still present: {present}'
print('OK -- all removed names are gone')
"
```
Expected: `OK -- all removed names are gone`.

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat: replace Binocular/Scalper-v3 config with Precision Pullback Scalper v1"
```

---

### Task 4: New indicator/helper functions in `strategy.py`

**Files:**
- Modify: `strategy.py` (additions only in this task — the old Binocular functions are removed in Task 6/8)
- Test: `tests/test_precision_pullback_indicators.py` (new)

**Interfaces:**
- Consumes: `config.EMA_TREND_SLOPE_LOOKBACK`, `config.RSI_LONG_RESET_MIN/MAX`, `config.RSI_SHORT_RESET_MIN/MAX`, `config.VOLUME_CONFIRM_MULT`, `config.MAX_CANDLE_BODY_PCT`, `config.ATR_MIN_PCT/MAX_PCT`.
- Produces: `calculate_volume_ma(df, period) -> pd.Series`, `_ema_trend_slope_up(ema_trend: pd.Series, lookback: int) -> bool`, `_rsi_reset_ok(direction: str, rsi: pd.Series, lookback: int) -> bool`, `_confirmation_candle_ok(direction: str, df: pd.DataFrame, ema20: pd.Series, vol_ma: pd.Series) -> bool`, `_abnormal_candle(df: pd.DataFrame) -> bool`, `_atr_pct_ok(atr_last: float, close: float) -> bool`. All added near the top of `strategy.py`, right after the existing `calculate_atr` function (around line 88), reusing existing `calculate_ema`/`calculate_rsi`/`calculate_atr` directly rather than adding redundant EMA20/50/200 wrapper functions.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_precision_pullback_indicators.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import config
import strategy
from tests.strategy_fixtures import make_trend_df


def test_calculate_volume_ma_simple_rolling_mean():
    df = pd.DataFrame({"volume": [10.0, 20.0, 30.0, 40.0, 50.0]})
    vol_ma = strategy.calculate_volume_ma(df, period=3)
    assert round(float(vol_ma.iloc[-1]), 4) == round((30.0 + 40.0 + 50.0) / 3, 4)


def test_ema_trend_slope_up_true_for_uptrend():
    df = make_trend_df("LONG", bars=260, freq="15min").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    assert strategy._ema_trend_slope_up(ema_trend, config.EMA_TREND_SLOPE_LOOKBACK) is True


def test_ema_trend_slope_up_false_for_downtrend():
    df = make_trend_df("SHORT", bars=260, freq="15min").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    assert strategy._ema_trend_slope_up(ema_trend, config.EMA_TREND_SLOPE_LOOKBACK) is False


def test_rsi_reset_long_true_when_in_zone_and_turning_up():
    rsi = pd.Series([70, 60, 50, 48, 45, 47])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is True


def test_rsi_reset_long_false_when_never_in_zone():
    rsi = pd.Series([70, 68, 65, 63, 62, 64])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is False


def test_rsi_reset_long_false_when_still_falling():
    rsi = pd.Series([70, 60, 50, 48, 45, 43])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is False


def test_rsi_reset_short_true_when_in_zone_and_turning_down():
    rsi = pd.Series([30, 40, 50, 52, 55, 53])
    assert strategy._rsi_reset_ok("SHORT", rsi, lookback=5) is True


def _two_candle_df(prev: dict, last: dict) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=2, freq="5min")
    return pd.DataFrame({
        "open":   [prev["open"],   last["open"]],
        "high":   [prev["high"],   last["high"]],
        "low":    [prev["low"],    last["low"]],
        "close":  [prev["close"],  last["close"]],
        "volume": [prev["volume"], last["volume"]],
    }, index=idx)


def test_confirmation_candle_long_passes():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is True


def test_confirmation_candle_long_fails_low_volume():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1050.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is False


def test_confirmation_candle_long_fails_does_not_close_above_prior_high():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 101.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is False


def test_confirmation_candle_short_passes():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 99.8, "volume": 1000.0},
        last={"open": 99.8, "high": 99.9, "low": 98.8, "close": 99.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 99.7])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("SHORT", df, ema20, vol_ma) is True


def test_abnormal_candle_true_when_body_too_large():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.0, "high": 101.5, "low": 99.0, "close": 101.0, "volume": 1300.0},
    )
    assert strategy._abnormal_candle(df) is True


def test_abnormal_candle_false_when_body_normal():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 100.6, "low": 100.0, "close": 100.4, "volume": 1300.0},
    )
    assert strategy._abnormal_candle(df) is False


def test_atr_pct_ok_within_band():
    assert strategy._atr_pct_ok(atr_last=0.5, close=100.0) is True


def test_atr_pct_ok_too_low():
    assert strategy._atr_pct_ok(atr_last=0.1, close=100.0) is False


def test_atr_pct_ok_too_high():
    assert strategy._atr_pct_ok(atr_last=1.5, close=100.0) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_precision_pullback_indicators.py -v`
Expected: FAIL / collection errors — none of `calculate_volume_ma`, `_ema_trend_slope_up`, `_rsi_reset_ok`, `_confirmation_candle_ok`, `_abnormal_candle`, `_atr_pct_ok` exist yet.

- [ ] **Step 3: Add the functions to `strategy.py`**

Insert immediately after the existing `calculate_atr` function (after line 88, before `calculate_supertrend`):

```python
def calculate_volume_ma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["volume"].rolling(window=period, min_periods=1).mean()


def _ema_trend_slope_up(ema_trend: pd.Series, lookback: int) -> bool:
    if len(ema_trend) <= lookback:
        return False
    return float(ema_trend.iloc[-1]) > float(ema_trend.iloc[-1 - lookback])


def _rsi_reset_ok(direction: str, rsi: pd.Series, lookback: int) -> bool:
    if len(rsi) < 2:
        return False
    if direction == "LONG":
        zone_lo, zone_hi = RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX
    else:
        zone_lo, zone_hi = RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX

    window = rsi.iloc[-(lookback + 1):]
    was_in_zone = bool(((window >= zone_lo) & (window <= zone_hi)).any())
    if not was_in_zone:
        return False

    turning = rsi.iloc[-1] > rsi.iloc[-2] if direction == "LONG" else rsi.iloc[-1] < rsi.iloc[-2]
    return bool(turning)


def _confirmation_candle_ok(direction: str, df: pd.DataFrame, ema20: pd.Series, vol_ma: pd.Series) -> bool:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close, open_ = float(last["close"]), float(last["open"])
    high, low, volume = float(last["high"]), float(last["low"]), float(last["volume"])
    ema20_last = float(ema20.iloc[-1])
    vol_ma_last = float(vol_ma.iloc[-1])

    if volume <= vol_ma_last * VOLUME_CONFIRM_MULT:
        return False

    if direction == "LONG":
        return close > open_ and close > ema20_last and close > float(prev["high"])
    return close < open_ and close < ema20_last and close < float(prev["low"])


def _abnormal_candle(df: pd.DataFrame) -> bool:
    last = df.iloc[-1]
    open_, close = float(last["open"]), float(last["close"])
    body_pct = abs(close - open_) / open_
    return body_pct > MAX_CANDLE_BODY_PCT


def _atr_pct_ok(atr_last: float, close: float) -> bool:
    atr_pct = atr_last / close
    return ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT
```

These reference `RSI_LONG_RESET_MIN/MAX`, `RSI_SHORT_RESET_MIN/MAX`, `VOLUME_CONFIRM_MULT`, `MAX_CANDLE_BODY_PCT`, `ATR_MIN_PCT`, `ATR_MAX_PCT` as module-level names — Task 6 updates `strategy.py`'s bottom `from config import (...)` block to bring these in. For this task's tests to pass in isolation, temporarily add a minimal import block right after `logger = logging.getLogger(__name__)` at the top of the file:

```python
from config import (
    RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX, RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX,
    VOLUME_CONFIRM_MULT, MAX_CANDLE_BODY_PCT, ATR_MIN_PCT, ATR_MAX_PCT, EMA_TREND_LEN,
    NO_CHASE_MAX_DISTANCE_PCT, PULLBACK_PREFERRED_DISTANCE_PCT,
)
```

(This block also covers Task 5's `_score_pending_setup`, added right after these functions later in this same task list, before Task 6 exists. Task 6 removes this temporary top-of-file import block once the real bottom-of-file import block is rewritten wholesale — see Task 6 Step 3.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_precision_pullback_indicators.py -v`
Expected: all PASS. If `_rsi_reset_ok`/`_confirmation_candle_ok` numeric assertions fail on a boundary, adjust the test fixture's numeric constants (not the function logic) and re-run, per this module's documented convention.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_precision_pullback_indicators.py
git commit -m "feat: add Precision Pullback Scalper v1 indicator helpers"
```

---

### Task 5: Add the scoring function

**Files:**
- Modify: `strategy.py`
- Test: `tests/test_precision_pullback_indicators.py` (append)

**Interfaces:**
- Consumes: `make_pullback_confirmation_df` (Task 2), `calculate_ema`, `calculate_volume_ma` (Task 4), `config.MIN_SIGNAL_SCORE`, `config.NO_CHASE_MAX_DISTANCE_PCT`, `config.EMA_TREND_LEN`, `config.EMA_TREND_SLOPE_LOOKBACK`, `config.EMA_FAST_LEN`, `config.VOLUME_CONFIRM_MULT`.
- Produces: `_score_pending_setup(direction: str, df: pd.DataFrame, ema_trend: pd.Series, slope_lookback: int, pullback_distance_pct: float, vol_ma: pd.Series) -> float` (0-100).

- [ ] **Step 1: Append the failing tests**

Add to `tests/test_precision_pullback_indicators.py`:

```python
from tests.strategy_fixtures import make_pullback_confirmation_df


def test_score_pending_setup_within_bounds_and_passes_gate_long():
    df = make_pullback_confirmation_df("LONG").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    vol_ma = strategy.calculate_volume_ma(df, config.VOLUME_MA_PERIOD)
    ema20 = strategy.calculate_ema(df["close"], config.EMA_FAST_LEN)
    close = float(df["close"].iloc[-1])
    distance_pct = abs(close - float(ema20.iloc[-1])) / close

    score = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, distance_pct, vol_ma
    )

    assert 0.0 <= score <= 100.0
    assert score >= config.MIN_SIGNAL_SCORE


def test_score_lower_when_pullback_distance_larger():
    df = make_pullback_confirmation_df("LONG").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    vol_ma = strategy.calculate_volume_ma(df, config.VOLUME_MA_PERIOD)

    score_tight = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, 0.0005, vol_ma
    )
    score_wide = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, 0.0025, vol_ma
    )

    assert score_tight > score_wide
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_precision_pullback_indicators.py -v -k score`
Expected: FAIL — `_score_pending_setup` does not exist yet.

- [ ] **Step 3: Add `_score_pending_setup` to `strategy.py`**

Insert right after `_atr_pct_ok` (end of Task 4's block):

```python
def _score_pending_setup(
    direction: str,
    df: pd.DataFrame,
    ema_trend: pd.Series,
    slope_lookback: int,
    pullback_distance_pct: float,
    vol_ma: pd.Series,
) -> float:
    """0-100 rubric: 15m EMA200 trend(20) + 5m EMA20/50 alignment(15) --
    both flat since they're already gated pass/fail upstream -- plus
    EMA200 slope strength(10), pullback quality(15), RSI reset(10, flat,
    already gated), confirmation-candle clearance(15), volume(10), and
    ATR environment(5, flat, already gated)."""
    score = 20.0 + 15.0

    ema_last = float(ema_trend.iloc[-1])
    ema_prev = float(ema_trend.iloc[-1 - slope_lookback]) if len(ema_trend) > slope_lookback else ema_last
    slope_move_pct = abs(ema_last - ema_prev) / ema_last if ema_last else 0.0
    score += 10.0 * min(1.0, slope_move_pct / 0.01)

    if pullback_distance_pct <= PULLBACK_PREFERRED_DISTANCE_PCT:
        pullback_score = 1.0
    else:
        span = max(NO_CHASE_MAX_DISTANCE_PCT - PULLBACK_PREFERRED_DISTANCE_PCT, 1e-9)
        pullback_score = max(0.0, 1.0 - (pullback_distance_pct - PULLBACK_PREFERRED_DISTANCE_PCT) / span)
    score += 15.0 * min(1.0, pullback_score)

    score += 10.0

    last = df.iloc[-1]
    prev = df.iloc[-2]
    candle_range = max(float(last["high"]) - float(last["low"]), 1e-9)
    if direction == "LONG":
        clearance = (float(last["close"]) - max(float(last["open"]), float(prev["high"]))) / candle_range
    else:
        clearance = (min(float(last["open"]), float(prev["low"])) - float(last["close"])) / candle_range
    score += 15.0 * min(1.0, max(0.0, clearance))

    vol_ratio = float(last["volume"]) / max(float(vol_ma.iloc[-1]), 1e-9)
    vol_score = min(1.0, max(0.0, (vol_ratio - VOLUME_CONFIRM_MULT) / VOLUME_CONFIRM_MULT))
    score += 10.0 * vol_score

    score += 5.0

    return round(min(100.0, max(0.0, score)), 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_precision_pullback_indicators.py -v`
Expected: all PASS (full file, including Task 4's tests). If the "passes gate" assertion fails because the fixture scores just under `MIN_SIGNAL_SCORE`, adjust `make_pullback_confirmation_df`'s constants in `tests/strategy_fixtures.py` (e.g. a slightly larger `confirm_close` overshoot or smaller pullback depth) and re-run — expected TDD iteration per this module's convention, not a defect in `_score_pending_setup` itself.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_precision_pullback_indicators.py
git commit -m "feat: add Precision Pullback Scalper v1 scoring function"
```

---

### Task 6: Rewrite the pipeline — `_build_pending_setup`, `detect_pending_setup`, `check_setup_confirmation`

**Files:**
- Modify: `strategy.py` (removes every Binocular-era function; rewrites `_build_pending_setup`/`detect_pending_setup`/`check_setup_confirmation`; rewrites the bottom import block)
- Test: `tests/test_strategy_precision_pullback.py` (new)

**Interfaces:**
- Consumes: everything from Tasks 4-5, `market_data.get_market_klines`, `config.TREND_TF/ENTRY_TF/ENTRY_KLINE_COUNT/CANDLE_MINUTES/MIN_CANDLE_SETTLE_SECONDS/ENABLE_LONG_SIGNALS/ENTRY_BUFFER_PCT/MAX_SL_PRICE_PCT/TP_PRICE_PCT/PENDING_SIGNAL_EXPIRY_CANDLES/MIN_SIGNAL_SCORE/TP_ROI_PCT/MAX_SL_ROI_PCT`.
- Produces: `detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None`, `check_setup_confirmation(setup: dict) -> tuple[str, float | None]` (status is one of `"confirmed"`, `"expired"`, `"invalidated"`, `"waiting"`) — same signatures `main.py` already calls, so Task 10 doesn't need to change call sites, only remove now-dead kwargs.
- Removes: `calculate_pvt`, `calculate_pvt_signal`, `calculate_chandelier_direction`, `calculate_ema200`, `calculate_daily_vwap`, `calculate_binocular_trigger`, `detect_transition`, `confirmed_mode_ok`, `mtf_signal`, `strict_mode_ok`, `position_size`, `_score_pending_setup`'s old body (replaced in Task 5), `_classify_no_trigger_reason`. `calculate_ema_ribbon` is also removed (Precision Pullback Scalper v1 has no ribbon concept at all, unlike Binocular which repurposed it as a confirmation filter). `calculate_supertrend` is **kept** (imported by `backtest/relative_strength.py`... actually verify — see Step 1 note below).

- [ ] **Step 1: Verify what still needs `calculate_supertrend`/`calculate_ema_ribbon` before removing anything**

Run:
```bash
grep -rn "calculate_supertrend\|calculate_ema_ribbon" --include="*.py" . | grep -v "^\./venv/\|__pycache__\|\.worktrees\|^\./strategy\.py"
```
If this returns any non-test-file hits outside `strategy.py` itself, keep that function; otherwise remove it in Step 2. (Expected: `calculate_ema_ribbon` has no other callers and is removed; `calculate_supertrend` is only referenced by `tests/test_indicators.py`, which Task 15 confirms is unaffected — keep it, since deleting a generically-useful, already-tested indicator function is out of scope for this strategy swap.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_strategy_precision_pullback.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config
import strategy
from tests.strategy_fixtures import (
    make_trend_df,
    make_pullback_confirmation_df,
    patch_klines,
    patch_klines_multi,
)


def _patch_pipeline(monkeypatch, entry_df, trend_df):
    patch_klines_multi(monkeypatch, strategy, {
        config.ENTRY_TF: entry_df,
        config.TREND_TF: trend_df,
    })
    monkeypatch.setattr(strategy, "MIN_CANDLE_SETTLE_SECONDS", 0)


def test_pending_setup_created_on_full_pipeline_pass_long(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("LONG", bars=260, freq="15min"),
    )

    setup = strategy.detect_pending_setup("TEST_USDT")

    assert setup is not None
    assert setup["direction"] == "LONG"
    assert setup["tp_price"] > setup["trigger_price"] > setup["sl_price"]
    assert setup["score"] >= config.MIN_SIGNAL_SCORE


def test_pending_setup_created_on_full_pipeline_pass_short(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("SHORT"),
        make_trend_df("SHORT", bars=260, freq="15min"),
    )

    setup = strategy.detect_pending_setup("TEST_USDT")

    assert setup is not None
    assert setup["direction"] == "SHORT"
    assert setup["tp_price"] < setup["trigger_price"] < setup["sl_price"]


def test_rejected_when_trend_disagrees_across_timeframes(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("SHORT", bars=260, freq="15min"),
    )

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_trend_alignment") == 1


def test_rejected_when_chasing_price(monkeypatch):
    entry_df = make_pullback_confirmation_df("LONG")
    entry_df.loc[entry_df.index[-2], "close"] += 5.0
    entry_df.loc[entry_df.index[-2], "high"] += 5.0
    _patch_pipeline(monkeypatch, entry_df, make_trend_df("LONG", bars=260, freq="15min"))

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("chasing_price") == 1


def test_rejected_when_score_below_minimum(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("LONG", bars=260, freq="15min"),
    )
    monkeypatch.setattr(strategy, "MIN_SIGNAL_SCORE", 101.0)

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("score_below_min") == 1


def test_setup_confirms_on_entry_breakout(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 101.0, "sl_price": 99.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [100.2], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "confirmed"
    assert fill_price == 101.0


def test_setup_expires_after_n_candles(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 200.0, "sl_price": 99.0,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [100.2], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "expired"
    assert fill_price is None


def test_same_candle_sl_blocks_confirmation(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 101.0, "sl_price": 99.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [98.5], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "invalidated"
    assert fill_price is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_precision_pullback.py -v`
Expected: FAIL — `detect_pending_setup`/`check_setup_confirmation` still contain Binocular logic and don't fetch two timeframes.

- [ ] **Step 4: Rewrite `strategy.py`'s bottom half**

Remove the temporary top-of-file import block added in Task 4 Step 3. Then replace everything from `def calculate_ema_ribbon` (the old line 139) through the end of the file (the old line 583, `_bump`) with:

```python
def _build_pending_setup(symbol: str, direction: str, df: pd.DataFrame) -> dict | None:
    last = df.iloc[-1]
    high, low = float(last["high"]), float(last["low"])

    if direction == "LONG":
        entry = high * (1 + ENTRY_BUFFER_PCT)
        sl = entry * (1 - MAX_SL_PRICE_PCT)
        tp = entry * (1 + TP_PRICE_PCT)
    else:
        entry = low * (1 - ENTRY_BUFFER_PCT)
        sl = entry * (1 + MAX_SL_PRICE_PCT)
        tp = entry * (1 - TP_PRICE_PCT)

    if not valid_trade_geometry(direction, entry, tp, sl):
        return None

    rr = round(TP_ROI_PCT / MAX_SL_ROI_PCT, 2)

    return {
        "symbol": symbol,
        "direction": direction,
        "trigger_price": entry,
        "entry_low": entry,
        "entry_high": entry,
        "sl_price": round(sl, 8),
        "tp_price": round(tp, 8),
        "rr": rr,
    }


def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw_entry = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
        if raw_entry is None or raw_entry.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_entry = raw_entry.iloc[:-1].copy()

        raw_trend = get_market_klines(symbol, TREND_TF, count=ENTRY_KLINE_COUNT)
        if raw_trend is None or raw_trend.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_trend = raw_trend.iloc[:-1].copy()

        min_history = max(EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD, VOLUME_MA_PERIOD, ATR_PERIOD) + 10
        if len(closed_entry) < min_history or len(closed_trend) < EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK + 10:
            _bump(reject_sink, "insufficient_history")
            return None

        candle_close_time = closed_entry.index[-1].to_pydatetime() + timedelta(minutes=CANDLE_MINUTES)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        ema_trend_15m = calculate_ema(closed_trend["close"], EMA_TREND_LEN)
        trend_close = float(closed_trend["close"].iloc[-1])
        trend_last = float(ema_trend_15m.iloc[-1])
        slope_up = _ema_trend_slope_up(ema_trend_15m, EMA_TREND_SLOPE_LOOKBACK)

        if trend_close > trend_last and slope_up:
            direction = "LONG"
        elif trend_close < trend_last and not slope_up:
            direction = "SHORT"
        else:
            _bump(reject_sink, "no_trend_alignment")
            return None

        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        ema20 = calculate_ema(closed_entry["close"], EMA_FAST_LEN)
        ema50 = calculate_ema(closed_entry["close"], EMA_SLOW_LEN)
        close = float(closed_entry["close"].iloc[-1])
        ema20_last = float(ema20.iloc[-1])
        ema50_last = float(ema50.iloc[-1])

        if direction == "LONG":
            aligned = ema20_last > ema50_last and (ema20_last - ema50_last) / close >= EMA_SEPARATION_MIN_PCT
        else:
            aligned = ema20_last < ema50_last and (ema50_last - ema20_last) / close >= EMA_SEPARATION_MIN_PCT
        if not aligned:
            _bump(reject_sink, "no_ema_alignment")
            return None

        ema_trend_entry_tf = calculate_ema(closed_entry["close"], EMA_TREND_LEN)
        ema_trend_entry_last = float(ema_trend_entry_tf.iloc[-1])
        agree = (close > ema_trend_entry_last) if direction == "LONG" else (close < ema_trend_entry_last)
        if not agree:
            _bump(reject_sink, "no_ema200_agreement")
            return None

        distance_pct = abs(close - ema20_last) / close
        if distance_pct > NO_CHASE_MAX_DISTANCE_PCT:
            _bump(reject_sink, "chasing_price")
            return None

        rsi = calculate_rsi(closed_entry["close"], RSI_PERIOD)
        if not _rsi_reset_ok(direction, rsi, PULLBACK_LOOKBACK_BARS):
            _bump(reject_sink, "no_rsi_reset")
            return None

        if _abnormal_candle(closed_entry):
            _bump(reject_sink, "abnormal_candle")
            return None

        vol_ma = calculate_volume_ma(closed_entry, VOLUME_MA_PERIOD)
        if not _confirmation_candle_ok(direction, closed_entry, ema20, vol_ma):
            _bump(reject_sink, "no_confirmation_candle")
            return None

        atr = calculate_atr(closed_entry, ATR_PERIOD)
        atr_last = float(atr.iloc[-1])
        if not _atr_pct_ok(atr_last, close):
            _bump(reject_sink, "atr_out_of_band")
            return None

        setup = _build_pending_setup(symbol, direction, closed_entry)
        if setup is None:
            _bump(reject_sink, "invalid_geometry")
            return None

        score = _score_pending_setup(direction, closed_entry, ema_trend_15m, EMA_TREND_SLOPE_LOOKBACK, distance_pct, vol_ma)
        if score < MIN_SIGNAL_SCORE:
            _bump(reject_sink, "score_below_min")
            return None

        setup["score"] = score
        setup["setup_reason"] = "Precision Pullback confirmation"
        setup["trend_summary"] = f"{TREND_TF} EMA200 + {ENTRY_TF} EMA20/50 pullback"
        setup["created_at"] = datetime.now(timezone.utc).isoformat()
        setup["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES)
        ).isoformat()
        return setup
    except Exception as e:
        logger.error("[PRECISION-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None


def check_setup_confirmation(setup: dict) -> tuple[str, float | None]:
    symbol = setup["symbol"]
    direction = setup["direction"]
    entry = setup["trigger_price"]
    sl = setup["sl_price"]

    raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
    if raw is None or raw.empty:
        return "waiting", None

    closed = raw.iloc[:-1].copy()
    if closed.empty:
        return "waiting", None

    latest = closed.iloc[-1]
    high, low = float(latest["high"]), float(latest["low"])

    if direction == "LONG":
        sl_hit = low <= sl
        entry_hit = high > entry
    else:
        sl_hit = high >= sl
        entry_hit = low < entry

    if sl_hit:
        return "invalidated", None
    if entry_hit:
        return "confirmed", entry

    created_at = datetime.fromisoformat(setup["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
    if age_minutes > PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES:
        return "expired", None

    return "waiting", None


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    ENTRY_TF, TREND_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    EMA_FAST_LEN, EMA_SLOW_LEN, EMA_TREND_LEN, EMA_TREND_SLOPE_LOOKBACK, EMA_SEPARATION_MIN_PCT,
    RSI_PERIOD, RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX, RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX,
    PULLBACK_LOOKBACK_BARS, PULLBACK_PREFERRED_DISTANCE_PCT, NO_CHASE_MAX_DISTANCE_PCT,
    VOLUME_MA_PERIOD, VOLUME_CONFIRM_MULT, MAX_CANDLE_BODY_PCT,
    ATR_PERIOD, ATR_MIN_PCT, ATR_MAX_PCT, MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS, LEVERAGE, MAX_SL_PRICE_PCT, MAX_SL_ROI_PCT, TP_PRICE_PCT, TP_ROI_PCT,
    ENABLE_LONG_SIGNALS, ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
)


def valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


def direction_slot_available(direction: str, active_long: int, active_short: int) -> bool:
    """Pure correlation-limit check -- at most one pending signal per direction."""
    from config import MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS
    if direction == "LONG":
        return active_long < MAX_ACTIVE_LONG_SIGNALS
    return active_short < MAX_ACTIVE_SHORT_SIGNALS


def _calc_rr(direction: str, entry: float, tp: float, sl: float) -> float:
    reward = abs(tp - entry)
    risk = abs(entry - sl)
    return reward / risk if risk > 0 else 0.0


def _roi_pct(direction: str, entry: float, tp: float, sl: float) -> tuple[float, float]:
    if direction == "LONG":
        tp_roi = (tp - entry) / entry * 100.0 * LEVERAGE
        sl_roi = (entry - sl) / entry * 100.0 * LEVERAGE
    else:
        tp_roi = (entry - tp) / entry * 100.0 * LEVERAGE
        sl_roi = (sl - entry) / entry * 100.0 * LEVERAGE
    return round(tp_roi, 2), round(sl_roi, 2)


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1
```

Also update the functions added in Tasks 4-5 (`_rsi_reset_ok`, `_confirmation_candle_ok`, `_abnormal_candle`, `_atr_pct_ok`, `_score_pending_setup`) to rely on this bottom import block instead of any temporary top-of-file import — since Python module-level names resolve at call time, not definition time, this works as long as the bottom import block executes before these functions are ever called (true for every test and for live use, since nothing calls them at import time).

Also update the module docstring at the top of `strategy.py` (lines 1-28) to describe Precision Pullback Scalper v1 instead of Binocular:

```python
"""
Precision Pullback Scalper v1.

Dual-timeframe pipeline: TREND_TF (15m) EMA200 trend + slope gates
direction; ENTRY_TF (5m) EMA20/EMA50 alignment, a pullback into the
EMA20/EMA50 zone (bounded by NO_CHASE_MAX_DISTANCE_PCT), an RSI14
reset-then-turn, a confirming candle (body/close/volume checks), and an
ATR% volatility band all gate a candidate; a 100-point score (rewarding
trend/pullback/candle/volume quality) must clear MIN_SIGNAL_SCORE.

A passing candidate creates a PENDING setup (persisted via
database.armed_setups): entry is a breakout-buffer beyond the
confirmation candle's high/low (ENTRY_BUFFER_PCT), SL/TP are FIXED
ROI-%-at-LEVERAGE distances (TP_ROI_PCT / MAX_SL_ROI_PCT) -- not
structural or ATR-derived -- so raw RR is a constant 0.70:1 by
construction; quality control is entirely the score gate. The setup
expires after PENDING_SIGNAL_EXPIRY_CANDLES candles if price never
breaks the entry level. Once confirmed, outcome_check.check_tp_sl_with_breakeven
walks the trade to a single TP/SL, moving the stop to breakeven once
price reaches BREAKEVEN_TRIGGER_ROI_PCT.

LONG signals can be disabled via ENABLE_LONG_SIGNALS (true by default).
The last closed candle on both timeframes must be at least
MIN_CANDLE_SETTLE_SECONDS old before it's used -- MEXC's kline REST data
for a just-closed candle can still get revised shortly after close. Only
completed candles are ever used anywhere in this pipeline.
"""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_precision_pullback.py tests/test_precision_pullback_indicators.py -v`
Expected: all PASS. If a pipeline test fails on a specific gate (e.g. the fixture doesn't quite clear `no_ema200_agreement`), adjust `make_pullback_confirmation_df`'s constants and re-run, per this module's documented fixture convention.

- [ ] **Step 6: Confirm no leftover references to removed functions**

Run:
```bash
grep -n "calculate_pvt\|calculate_chandelier_direction\|calculate_ema200\b\|calculate_daily_vwap\|calculate_binocular_trigger\|detect_transition\|confirmed_mode_ok\|mtf_signal\|strict_mode_ok\|position_size\|calculate_ema_ribbon\|_classify_no_trigger_reason" strategy.py
```
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add strategy.py tests/test_strategy_precision_pullback.py
git commit -m "feat: rewrite strategy.py pipeline for Precision Pullback Scalper v1"
```

---

### Task 7: `outcome_check.py` — add breakeven-aware single-TP walker, remove the two Binocular/legacy walkers

**Files:**
- Modify: `outcome_check.py` (full-file rewrite)
- Test: `tests/test_outcome_check_breakeven.py` (new)
- Delete: `tests/test_outcome_check.py`, `tests/test_outcome_target_ladder.py` (if it exists — see Step 5)

**Interfaces:**
- Produces: `check_tp_sl_with_breakeven(direction: str, entry_price: float, sl_price: float, tp_price: float, breakeven_trigger_price: float, df: pd.DataFrame, entry_candle_cutoff) -> dict | None`. Returns `None` while open, else `{"status": "win"|"loss"|"breakeven", "pnl_roi_pct": float, "breakeven_triggered_at": Timestamp|None, "closed_at": Timestamp}`.
- Removes: `check_tp_sl`, `check_target_ladder` (superseded — nothing calls either once Task 10 rewrites `main.py`).

- [ ] **Step 1: Check whether `tests/test_outcome_target_ladder.py` exists**

Run: `ls tests/test_outcome_target_ladder.py 2>&1 || echo "not present"`
(The design spec assumed this file exists per the Binocular migration's plan, but that file may never have been created in this codebase's actual history — confirm before trying to delete it in Step 5.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_outcome_check_breakeven.py`:

```python
import pandas as pd

from outcome_check import check_tp_sl_with_breakeven


def _df(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"high": [r[1] for r in rows], "low": [r[2] for r in rows]}, index=idx)


def test_tp_hit_is_a_win():
    df = _df([
        ("2026-01-01 00:00", 100.2, 99.9),
        ("2026-01-01 00:05", 100.36, 100.1),   # TP=100.35 hit
        ("2026-01-01 00:10", 100.36, 100.1),   # forming candle, ignored
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "win"
    assert result["breakeven_triggered_at"] is not None  # 100.2 was reached en route


def test_sl_hit_before_breakeven_is_a_full_loss():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        ("2026-01-01 00:05", 100.15, 99.4),   # SL=99.5 hit, never reached 100.2 trigger
        ("2026-01-01 00:10", 100.15, 99.4),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "loss"
    assert result["breakeven_triggered_at"] is None


def test_breakeven_trigger_then_stop_is_breakeven_not_loss():
    df = _df([
        ("2026-01-01 00:00", 100.25, 99.9),   # reaches 100.2 trigger -> SL moves to 100.0
        ("2026-01-01 00:05", 100.1, 99.95),   # pulls back to entry (100.0) -> breakeven stop
        ("2026-01-01 00:10", 100.1, 99.95),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "breakeven"
    assert result["pnl_roi_pct"] == 0.0
    assert result["breakeven_triggered_at"] is not None


def test_breakeven_trigger_then_tp_is_still_a_win():
    df = _df([
        ("2026-01-01 00:00", 100.25, 99.9),   # reaches trigger
        ("2026-01-01 00:05", 100.4, 100.1),   # then hits TP
        ("2026-01-01 00:10", 100.4, 100.1),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "win"
    assert result["breakeven_triggered_at"] is not None


def test_same_candle_original_sl_beats_breakeven_trigger():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        # one wild candle spans both the breakeven trigger (100.2) and the
        # original SL (99.5) -- conservative same-candle rule: original SL wins.
        ("2026-01-01 00:05", 100.3, 99.4),
        ("2026-01-01 00:10", 100.3, 99.4),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "loss"


def test_short_breakeven_then_stop_is_breakeven():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.75),   # reaches 99.8 trigger -> SL moves to 100.0
        ("2026-01-01 00:05", 100.05, 99.9),   # pulls back to entry -> breakeven stop
        ("2026-01-01 00:10", 100.05, 99.9),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "SHORT", entry_price=100.0, sl_price=100.5, tp_price=99.65,
        breakeven_trigger_price=99.8, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "breakeven"


def test_still_pending_returns_none():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        ("2026-01-01 00:05", 100.15, 99.95),
        ("2026-01-01 00:10", 100.15, 99.95),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_outcome_check_breakeven.py -v`
Expected: FAIL — `check_tp_sl_with_breakeven` does not exist.

- [ ] **Step 4: Replace `outcome_check.py`'s full content**

```python
"""
Breakeven-aware single-TP/SL outcome determination for Precision Pullback
Scalper v1's pending signals.

Same-candle tie-break, checked in this order every candle: (1) the
CURRENT stop (original SL, or entry_price once breakeven has triggered)
-- if hit, closes the trade; (2) TP -- if hit, closes as a win; (3) only
if neither hit, check whether the breakeven trigger price is reached for
the first time this candle and move the stop to entry_price. This order
means a single wild candle that spans both the breakeven trigger and the
original SL is conservatively treated as a full loss, matching the
SL-first tie-break convention used everywhere else in this bot.
"""

from __future__ import annotations

import pandas as pd


def check_tp_sl_with_breakeven(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    breakeven_trigger_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff. Returns None while
    still open, else:
    {"status": "win"|"loss"|"breakeven", "pnl_roi_pct": float,
     "breakeven_triggered_at": Timestamp|None, "closed_at": Timestamp}

    pnl_roi_pct is the raw price-move percent (not leverage-scaled -- the
    caller applies LEVERAGE). A "breakeven" close realizes exactly 0.0%
    (fees/slippage are not modelled here, matching how the rest of the
    bot treats ESTIMATED_*_FEE_PCT as backtest-only/informational).
    """
    current_sl = sl_price
    breakeven_triggered_at = None

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= current_sl) if direction == "LONG" else (high >= current_sl)
        if sl_hit:
            status = "loss" if current_sl == sl_price else "breakeven"
            pnl = 0.0 if status == "breakeven" else (
                (current_sl - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - current_sl) / entry_price * 100.0
            )
            return {
                "status": status, "pnl_roi_pct": round(pnl, 4),
                "breakeven_triggered_at": breakeven_triggered_at, "closed_at": ts,
            }

        tp_hit = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        if tp_hit:
            pnl = (
                (tp_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - tp_price) / entry_price * 100.0
            )
            return {
                "status": "win", "pnl_roi_pct": round(pnl, 4),
                "breakeven_triggered_at": breakeven_triggered_at, "closed_at": ts,
            }

        if breakeven_triggered_at is None:
            reached = (high >= breakeven_trigger_price) if direction == "LONG" else (low <= breakeven_trigger_price)
            if reached:
                current_sl = entry_price
                breakeven_triggered_at = ts

    return None
```

- [ ] **Step 5: Delete the superseded walkers and their tests**

```bash
rm tests/test_outcome_check.py
```
Only run `rm tests/test_outcome_target_ladder.py` if Step 1 confirmed it exists.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_outcome_check_breakeven.py -v`
Expected: all PASS.

- [ ] **Step 7: Confirm nothing else references the deleted functions**

Run: `grep -rln "check_tp_sl\b\|check_target_ladder" --include="*.py" . | grep -v "^\./venv/\|__pycache__\|\.worktrees\|outcome_check.py"`
Expected: only `main.py` and `scripts/backtest_simple_strategy.py` (both rewritten in later tasks) — no output from any file this task doesn't already know about. If something unexpected shows up, stop and investigate before continuing.

- [ ] **Step 8: Commit**

```bash
git add outcome_check.py tests/test_outcome_check_breakeven.py
git rm tests/test_outcome_check.py
git commit -m "feat: replace Binocular outcome walkers with breakeven-aware single-TP walker"
```

---

### Task 8: Verify `database.py` round-trips the new `"breakeven"` status

**Files:**
- Test: `tests/test_database_breakeven_status.py` (new)

No production code change — `database.py`'s `status` column is a plain `TEXT` with no `CHECK` constraint and `update_signal_outcome`/`get_pending_signals` both treat `status` as an opaque string, so `"breakeven"` already round-trips. This task exists purely to prove that with a real (temp-file) SQLite DB, per the design spec's acceptance criteria.

**Interfaces:**
- Consumes: `database.init_db`, `database.save_signal`, `database.update_signal_outcome`, `database.get_signals_in_range`, `config.DB_PATH`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_database_breakeven_status.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

import config
import database as db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    return db_path


def test_breakeven_status_round_trips(temp_db):
    now = datetime.now(timezone.utc)
    signal_id = db.save_signal(
        symbol="TEST_USDT", direction="LONG", entry_price=100.0,
        tp_price=100.35, sl_price=99.5, leverage=20, generated_at=now,
        strategy_name="Precision Pullback Scalper v1",
    )

    db.update_signal_outcome(signal_id, "breakeven", 0.0)

    rows = db.get_signals_in_range(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert len(rows) == 1
    assert rows[0]["status"] == "breakeven"
    assert rows[0]["pnl_roi"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_database_breakeven_status.py -v`
Expected: PASS immediately (no production code change needed) — this step exists to prove the claim, not to drive new code. If it fails, `database.py` has an unexpected constraint on `status` that must be fixed before continuing (stop and investigate rather than assuming).

- [ ] **Step 3: Commit**

```bash
git add tests/test_database_breakeven_status.py
git commit -m "test: verify signals.status round-trips a breakeven outcome"
```

---

### Task 9: Rewrite `main.py`

**Files:**
- Modify: `main.py` (full-file rewrite)

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation`, `strategy.valid_trade_geometry`, `strategy.direction_slot_available`, `strategy._roi_pct`, `strategy.Signal`, `outcome_check.check_tp_sl_with_breakeven`, every new `config` constant from Task 3.
- Produces: `scan_and_fire_signals(app)`, `check_outcomes(app)`, `main()` — same names `bot.py`/tests might reference, but no test file currently imports from `main.py` (verified: no `test_main.py` exists), so this task's correctness gate is `py_compile` + a local dry-run boot, matching how this codebase has always verified `main.py` end-to-end.

- [ ] **Step 1: Replace the full content of `main.py`**

```python
"""
Main entry point — Precision Pullback Scalper v1.

Scheduler jobs / background tasks:
  Every SCAN_INTERVAL_MINUTES (default 5m), a few seconds after candle
  close — scanner: two-phase pending-breakout loop. Phase 1 checks every
  currently-armed pending setup for entry-breakout confirmation or expiry;
  confirmed setups fire within the daily/gap/concurrent/direction limits.
  Phase 2 scans the remaining coin pool for new EMA-trend/pullback/RSI-
  reset/confirmation-candle setups and arms new pending setups.
  Every OUTCOME_CHECK_MINUTES — outcome checker (fixed single TP/SL,
  breakeven step at BREAKEVEN_TRIGGER_ROI_PCT).
  Every COIN_REFRESH_HOURS — coin pool refresh.
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner
from outcome_check import check_tp_sl_with_breakeven
from market_data import get_market_klines
from config import (
    LKT,
    LEVERAGE,
    ENTRY_TF,
    TREND_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCAN_INTERVAL_MINUTES,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    STRATEGY_NAME,
    MIN_SIGNAL_SCORE,
    TP_ROI_PCT,
    MAX_SL_ROI_PCT,
    BREAKEVEN_TRIGGER_ROI_PCT,
    BREAKEVEN_TRIGGER_PRICE_PCT,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
)


def _backup_log_on_startup() -> None:
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return
    log_path = Path(LOG_FILE)
    archive  = Path(LOG_BACKUP_DIR)
    archive.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 0:
        ts = datetime.now(LKT).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(log_path, archive / f"{log_path.stem}_{ts}{log_path.suffix or '.log'}")
        log_path.write_text("", encoding="utf-8")
    else:
        log_path.touch(exist_ok=True)


_backup_log_on_startup()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)

logging.Formatter.converter = lambda *args: datetime.now(LKT).timetuple()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Signal scanner ────────────────────────────────────────────────

async def scan_and_fire_signals(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_armed_setups(now)

    # ── Phase 1: process existing pending (armed) setups ────────────
    armed = db.get_armed_setups(limit=len(coins) + 20)
    armed_symbols = {s["symbol"] for s in armed}

    active_signals = db.count_active_signals()
    active_long = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig = db.latest_signal_time()

    for setup in armed:
        status, fill_price = strategy.check_setup_confirmation(setup)

        if status == "expired":
            db.mark_armed_setup_expired(setup["id"])
            continue
        if status == "invalidated":
            db.mark_armed_setup_invalidated(setup["id"], reason="sl_hit_before_entry")
            continue
        if status == "waiting":
            continue

        # status == "confirmed"
        slots = MAX_CONCURRENT_SIGNALS - active_signals
        daily_ok = signals_today < MAX_DAILY_SIGNALS
        gap_ok = (
            last_sig is None
            or (now - last_sig).total_seconds() >= MIN_DAILY_SIGNAL_GAP_MINUTES * 60
        )
        direction_ok = strategy.direction_slot_available(setup["direction"], active_long, active_short)

        if slots <= 0 or not daily_ok or not gap_ok or not direction_ok:
            db.mark_armed_setup_missed(setup["id"], reason="budget_or_slot_unavailable")
            continue

        tp_roi, sl_roi = strategy._roi_pct(setup["direction"], fill_price, setup["tp_price"], setup["sl_price"])
        sig = strategy.Signal(
            symbol=setup["symbol"],
            direction=setup["direction"],
            entry_price=fill_price,
            tp_price=setup["tp_price"],
            sl_price=setup["sl_price"],
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary=setup.get("setup_reason", ""),
            generated_at=now,
            rr=setup["rr"],
            score=setup["score"],
            entry_low=fill_price,
            entry_high=fill_price,
        )

        if not strategy.valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            db.mark_armed_setup_invalidated(setup["id"], reason="invalid_geometry_at_confirm")
            continue

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would confirm | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.2f",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )
            db.mark_armed_setup_fired(setup["id"], signal_id=-1)
            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1
            continue

        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
                strategy_name=STRATEGY_NAME,
                score=sig.score,
                rr=sig.rr,
                entry_timeframe=ENTRY_TF,
                trend_timeframe=TREND_TF,
                setup_reason=sig.timeframe_summary,
            )
            db.mark_armed_setup_fired(setup["id"], signal_id)

            if not DRY_RUN:
                await tg.broadcast_signal(app, sig, signal_id)

            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1

            logger.info(
                "[SIGNAL] Confirmed #%d %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )
        except Exception as e:
            logger.error("[SCAN] Failed to confirm setup for %s: %s", setup["symbol"], e, exc_info=True)

    # ── Phase 2: scan for new pending setups ────────────────────────
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    to_scan = [
        s for s in coins
        if s not in armed_symbols and not db.signal_exists_for_coin(s, cooldown_since)
    ]

    reject_maps = [dict() for _ in to_scan]
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(
                lambda i: strategy.detect_pending_setup(to_scan[i], reject_sink=reject_maps[i]),
                range(len(to_scan)),
            )),
        )

    new_setups = [s for s in results if s is not None]

    reject_counts: dict[str, int] = {}
    for m in reject_maps:
        for k, v in m.items():
            reject_counts[k] = reject_counts.get(k, 0) + v
    reject_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(reject_counts.items(), key=lambda kv: -kv[1])
    ) or "none"

    for setup in new_setups:
        try:
            db.save_armed_setup(setup)
            logger.info(
                "[PENDING] Armed %s %s entry=%.6g sl=%.6g tp=%.6g score=%.1f rr=%.2f",
                setup["symbol"], setup["direction"], setup["trigger_price"],
                setup["sl_price"], setup["tp_price"], setup["score"], setup["rr"],
            )
        except Exception as e:
            logger.error("[SCAN] Failed to arm setup for %s: %s", setup["symbol"], e, exc_info=True)

    logger.info(
        "[SCAN] Done — %d armed processed, %d/%d coins scanned for new setups, %d new pending | rejects: %s",
        len(armed), len(to_scan), len(coins), len(new_setups), reject_summary,
    )


# ── Outcome checker ───────────────────────────────────────────────

async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        sl_price = sig["sl_price"]
        tp_price = sig["tp_price"]

        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("Signal %s expired (%s)", sig["id"], symbol)
            if not DRY_RUN:
                try:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                except Exception as e:
                    logger.error("Failed to notify expiry for %s: %s", symbol, e)
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_market_klines(symbol, ENTRY_TF, count=fetch_count)
            if df is None or df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)
        breakeven_trigger_price = (
            entry_price * (1 + BREAKEVEN_TRIGGER_PRICE_PCT) if direction == "LONG"
            else entry_price * (1 - BREAKEVEN_TRIGGER_PRICE_PCT)
        )

        result = check_tp_sl_with_breakeven(
            direction, entry_price, sl_price, tp_price, breakeven_trigger_price,
            df, entry_candle_cutoff,
        )
        if result is None:
            continue

        pnl = result["pnl_roi_pct"] * LEVERAGE

        if result["breakeven_triggered_at"] is not None and sig.get("breakeven_triggered_at") is None:
            db.mark_signal_breakeven_triggered(sig["id"], result["breakeven_triggered_at"])

        db.update_signal_outcome(sig["id"], result["status"], pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], result["status"].upper(), symbol, pnl)

        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": result["status"], "pnl_roi": pnl})
            except Exception as e:
                logger.error("Failed to notify %s for %s: %s", result["status"], symbol, e)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Trend TF: %s  Entry TF: %s", TREND_TF, ENTRY_TF)
    logger.info("Min signal score: %.0f", MIN_SIGNAL_SCORE)
    logger.info(
        "TP: +%.1f%% ROI  SL: -%.1f%% ROI  Breakeven at +%.1f%% ROI",
        TP_ROI_PCT, MAX_SL_ROI_PCT, BREAKEVEN_TRIGGER_ROI_PCT,
    )
    logger.info("Leverage: %dx", LEVERAGE)
    logger.info("Dry run: %s", "enabled" if DRY_RUN else "disabled")
    logger.info(
        "[CONFIG] coin pool: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f COINGLASS=%s",
        TOP_N_COINS, COIN_POOL_MIN_SELECTED, COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
    )

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info("Coin pool: %d coins", len(coins))

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

    scheduler.add_job(
        scan_and_fire_signals,
        CronTrigger(minute=f"*/{SCAN_INTERVAL_MINUTES}", second=5),
        args=[app],
        id="signal_scanner",
    )

    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app],
        id="outcome_checker",
    )

    scheduler.add_job(
        coin_scanner.refresh_coin_list,
        CronTrigger(hour=f"*/{COIN_REFRESH_HOURS}"),
        id="coin_refresh",
    )

    async def _daily(app=app):
        await tg.auto_daily_report(type("ctx", (), {"application": app})())

    async def _weekly(app=app):
        await tg.auto_weekly_report(type("ctx", (), {"application": app})())

    async def _monthly(app=app):
        await tg.auto_monthly_report(type("ctx", (), {"application": app})())

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55),        id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7),             id="monthly_report")

    scheduler.start()

    logger.info(
        "Scheduler started — scan every %dm, outcome every %dm",
        SCAN_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES,
    )

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Bot is running. Press Ctrl+C to stop.")

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python -c "import main"`
Expected: no output, exit code 0 (this will also transitively import `bot`, `webui` is separate and not imported by `main`).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: rewrite main.py for Precision Pullback Scalper v1 (single strategy)"
```

---

### Task 10: Rewrite `reports.py` for three-way win/loss/breakeven stats

**Files:**
- Modify: `reports.py`
- Test: `tests/test_reports.py` (new)

**Interfaces:**
- Consumes: `database.get_signals_in_range`, `database.get_all_signals` (unchanged).
- Produces: `_stats(signals) -> dict` gains a `"breakevens"` key; `win_rate` stays `wins / (wins + losses)` (breakeven excluded from the ratio); `net_roi` sums over `win`/`loss`/`breakeven`. `daily_report`/`weekly_report`/`monthly_report`/`alltime_report` unchanged signatures.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reports.py`:

```python
from reports import _stats, _format_report


def _sig(status: str, pnl_roi: float, direction: str = "LONG") -> dict:
    return {"status": status, "pnl_roi": pnl_roi, "direction": direction}


def test_stats_counts_breakeven_separately_from_win_and_loss():
    signals = [
        _sig("win", 7.0),
        _sig("win", 7.0),
        _sig("loss", -10.0),
        _sig("breakeven", 0.0),
        _sig("pending", None),
        _sig("expired", 0.0),
    ]
    s = _stats(signals)
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["breakevens"] == 1
    assert s["pending"] == 1
    assert s["expired"] == 1
    assert s["total"] == 6


def test_win_rate_excludes_breakeven_from_the_ratio():
    signals = [_sig("win", 7.0), _sig("loss", -10.0), _sig("breakeven", 0.0)]
    s = _stats(signals)
    assert s["win_rate"] == 50.0   # 1 win / (1 win + 1 loss), not /3


def test_net_roi_includes_breakeven():
    signals = [_sig("win", 7.0), _sig("loss", -10.0), _sig("breakeven", 0.0)]
    s = _stats(signals)
    assert s["net_roi"] == -3.0


def test_format_report_shows_breakeven_line():
    signals = [_sig("win", 7.0), _sig("breakeven", 0.0)]
    text = _format_report("Test Report", signals)
    assert "Breakeven" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_reports.py -v`
Expected: FAIL — `_stats` doesn't have a `"breakevens"` key yet, `win_rate`/`net_roi` don't account for it.

- [ ] **Step 3: Rewrite `reports.py`**

Replace `_stats` (lines 10-37) and the `_format_report` lines list (lines 54-73):

```python
def _stats(signals: list[dict]) -> dict:
    total      = len(signals)
    wins       = [s for s in signals if s["status"] == "win"]
    losses     = [s for s in signals if s["status"] == "loss"]
    breakevens = [s for s in signals if s["status"] == "breakeven"]
    pending    = [s for s in signals if s["status"] == "pending"]
    expired    = [s for s in signals if s["status"] == "expired"]

    win_count  = len(wins)
    loss_count = len(losses)
    closed     = win_count + loss_count
    win_rate   = (win_count / closed * 100) if closed else 0

    net_roi = sum(s["pnl_roi"] or 0 for s in signals if s["status"] in ("win", "loss", "breakeven"))

    best  = max((s["pnl_roi"] or 0 for s in wins),   default=0)
    worst = min((s["pnl_roi"] or 0 for s in losses),  default=0)

    longs  = [s for s in signals if s["direction"] == "LONG"]
    shorts = [s for s in signals if s["direction"] == "SHORT"]

    return {
        "total": total,
        "wins": win_count, "losses": loss_count, "breakevens": len(breakevens),
        "pending": len(pending), "expired": len(expired),
        "win_rate": win_rate, "net_roi": net_roi,
        "best": best, "worst": worst,
        "longs": len(longs), "shorts": len(shorts),
    }
```

```python
    lines = [
        f"📊 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📡 Total signals:  `{s['total']}`",
        f"✅ Wins:           `{s['wins']}`",
        f"❌ Losses:         `{s['losses']}`",
        f"⚖️ Breakeven:      `{s['breakevens']}`",
        f"⏳ Pending:        `{s['pending']}`",
        f"💤 Expired:        `{s['expired']}`",
        "",
        f"🎯 Win rate:  `{s['win_rate']:.1f}%`  {_bar(s['win_rate'])}",
        f"{emoji} Net ROI:   `{sign}{s['net_roi']:.1f}%`",
        "",
        f"📈 Longs:   `{s['longs']}`",
        f"📉 Shorts:  `{s['shorts']}`",
        "",
        f"🔥 Best signal:   `+{s['best']:.1f}%`",
        f"💀 Worst signal:  `{s['worst']:.1f}%`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
    ]
```

(The rest of `_format_report` — the `if s["total"] == 0` early return, `sign`/`emoji` computation, and everything below the `lines` list — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_reports.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add reports.py tests/test_reports.py
git commit -m "feat: track breakeven outcomes separately in reports"
```

---

### Task 11: Rewrite `webui.py`

**Files:**
- Modify: `webui.py` (Python stats/config functions + the embedded HTML/JS dashboard string)
- Test: `tests/test_webui_stats.py` (new — covers the testable Python logic only; the HTML/JS is verified manually)

**Interfaces:**
- Consumes: same as Task 10's `_stats` logic, applied to `webui.get_stats`.
- Produces: `get_stats(since=None) -> dict` gains `"breakevens"`; `get_strategy_config() -> dict` returns the new Precision Pullback Scalper v1 keys instead of Binocular/v3 ones.

- [ ] **Step 1: Write the failing test**

Create `tests/test_webui_stats.py`:

```python
import sqlite3
from datetime import datetime, timezone

import pytest

import webui


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, entry_price REAL, tp_price REAL, sl_price REAL,
            leverage INTEGER, status TEXT, placed INTEGER, generated_at TEXT,
            placed_at TEXT, closed_at TEXT, pnl_roi REAL
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT INTO signals (symbol, direction, entry_price, tp_price, sl_price, leverage, "
        "status, placed, generated_at, pnl_roi) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        [
            ("A_USDT", "LONG", 100.0, 100.35, 99.5, 20, "win", now, 7.0),
            ("B_USDT", "LONG", 100.0, 100.35, 99.5, 20, "loss", now, -10.0),
            ("C_USDT", "LONG", 100.0, 100.35, 99.5, 20, "breakeven", now, 0.0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(webui, "DB_PATH", str(db_path))
    return db_path


def test_get_stats_reports_breakevens_and_excludes_from_win_rate(temp_db):
    stats = webui.get_stats()
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["breakevens"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["net_roi"] == -3.0


def test_get_strategy_config_reports_precision_pullback_keys():
    cfg = webui.get_strategy_config()
    assert "min_signal_score" in cfg
    assert "tp_roi_pct" in cfg
    assert "breakeven_trigger_roi_pct" in cfg
    assert "no_chase_max_distance_pct" in cfg
    assert "signal_mode" not in cfg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webui_stats.py -v`
Expected: FAIL — `get_stats` has no `"breakevens"` key, `get_strategy_config` still returns Binocular keys.

- [ ] **Step 3: Rewrite `get_stats` (lines 144-194)**

```python
def get_stats(since: datetime | None = None) -> dict:
    if not _table_exists("signals"):
        return {
            "total": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "pending": 0, "expired": 0,
            "win_rate": 0.0, "net_roi": 0.0, "best": 0.0, "worst": 0.0,
            "longs": 0, "shorts": 0,
        }

    if since:
        rows = _query(
            "SELECT * FROM signals WHERE generated_at >= ?",
            (since.isoformat(),),
        )
    else:
        rows = _query("SELECT * FROM signals")

    total = len(rows)
    wins = [r for r in rows if r.get("status") == "win"]
    losses = [r for r in rows if r.get("status") == "loss"]
    breakevens = [r for r in rows if r.get("status") == "breakeven"]
    pending = [r for r in rows if r.get("status") == "pending"]
    expired = [r for r in rows if r.get("status") == "expired"]
    longs = [r for r in rows if r.get("direction") == "LONG"]
    shorts = [r for r in rows if r.get("direction") == "SHORT"]

    closed = len(wins) + len(losses)
    win_rate = (len(wins) / closed * 100) if closed else 0.0
    net_roi = sum(r.get("pnl_roi") or 0 for r in rows if r.get("status") in ("win", "loss", "breakeven"))
    best = max((r.get("pnl_roi") or 0 for r in wins), default=0.0)
    worst = min((r.get("pnl_roi") or 0 for r in losses), default=0.0)

    return {
        "total": total,
        "wins": len(wins), "losses": len(losses), "breakevens": len(breakevens),
        "pending": len(pending), "expired": len(expired),
        "win_rate": round(win_rate, 1),
        "net_roi": round(net_roi, 1),
        "best": round(best, 1), "worst": round(worst, 1),
        "longs": len(longs), "shorts": len(shorts),
    }
```

- [ ] **Step 4: Rewrite `get_strategy_config` (lines 232-263)**

```python
def get_strategy_config() -> dict:
    """Return dashboard-safe strategy/runtime configuration for Precision Pullback Scalper v1."""
    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Precision Pullback Scalper v1"),
        "trend_tf": _safe_config_value("TREND_TF", "—"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "min_signal_score": _safe_config_value("MIN_SIGNAL_SCORE", "—"),
        "tp_roi_pct": _safe_config_value("TP_ROI_PCT", "—"),
        "max_sl_roi_pct": _safe_config_value("MAX_SL_ROI_PCT", "—"),
        "breakeven_trigger_roi_pct": _safe_config_value("BREAKEVEN_TRIGGER_ROI_PCT", "—"),
        "no_chase_max_distance_pct": _safe_config_value("NO_CHASE_MAX_DISTANCE_PCT", "—"),
        "atr_min_pct": _safe_config_value("ATR_MIN_PCT", "—"),
        "atr_max_pct": _safe_config_value("ATR_MAX_PCT", "—"),
        "entry_buffer_pct": _safe_config_value("ENTRY_BUFFER_PCT", "—"),
        "pending_signal_expiry_candles": _safe_config_value("PENDING_SIGNAL_EXPIRY_CANDLES", "—"),

        "top_n_coins": _safe_config_value("TOP_N_COINS", "—"),
        "min_volume_usd": _safe_config_value("COIN_POOL_MIN_VOLUME_USD", "—"),

        "leverage": _safe_config_value("LEVERAGE", "—"),

        "max_daily_signals": _safe_config_value("MAX_DAILY_SIGNALS", "—"),
        "min_daily_signal_gap_minutes": _safe_config_value("MIN_DAILY_SIGNAL_GAP_MINUTES", "—"),
        "max_concurrent_signals": _safe_config_value("MAX_CONCURRENT_SIGNALS", "—"),
        "max_active_long_signals": _safe_config_value("MAX_ACTIVE_LONG_SIGNALS", "—"),
        "max_active_short_signals": _safe_config_value("MAX_ACTIVE_SHORT_SIGNALS", "—"),
        "cooldown_minutes": _safe_config_value("SIGNAL_COOLDOWN_MINUTES", "—"),
        "scan_workers": _safe_config_value("SCAN_WORKERS", "—"),

        "crypto_futures_only": _safe_config_value("CRYPTO_FUTURES_ONLY", True),
        "dry_run": _safe_config_value("DRY_RUN", True),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_webui_stats.py -v`
Expected: all PASS.

- [ ] **Step 6: Update the embedded HTML/JS dashboard string**

In the `HTML = r"""..."""` block, make these text/logic replacements (no automated test — verified manually in Step 7):

1. `<title>Supertrend Pullback Dashboard</title>` → `<title>Precision Pullback Scalper Dashboard</title>`
2. `<div class="logo"><span>📡</span> Supertrend Pullback Bot</div>` → `<div class="logo"><span>📡</span> Precision Pullback Scalper Bot</div>`
3. `<div class="logo-sub">15m Trend (EMA200 + Supertrend) + 5m EMA20 Pullback Reclaim</div>` → `<div class="logo-sub">15m EMA200 Trend + 5m EMA20/50 Pullback + RSI Reset</div>`
4. Stats grid: add a Breakeven card right after the Losses card:
```html
    <div class="card"><div class="card-label">Breakeven</div><div class="card-value yellow" id="c-breakeven">—</div><div class="card-small">Stopped at entry</div></div>
```
5. Strategy-setup grid card labels/subtitles:
```html
    <div class="card"><div class="card-label">Timeframe</div><div class="card-value cyan" id="cfg-tf">—</div><div class="card-small">EMA200 trend + EMA20/50 pullback</div></div>
    <div class="card"><div class="card-label">Min Score</div><div class="card-value purple" id="cfg-quality">—</div><div class="card-small">0-100 quality gate</div></div>
    <div class="card"><div class="card-label">Entry Buffer</div><div class="card-value green" id="cfg-confirm">—</div><div class="card-small" id="cfg-confirm-sub">—</div></div>
    <div class="card"><div class="card-label">Risk Model</div><div class="card-value orange" id="cfg-rr">—</div><div class="card-small" id="cfg-rr-sub">—</div></div>
```
6. Pending Setups table header: `<th>T1</th>` → `<th>TP</th>`
7. Add a breakeven badge CSS rule right after `.badge-pending, .badge-waiting { ... }`:
```css
.badge-breakeven {
  background: rgba(245, 200, 75, .12);
  color: var(--yellow);
  border: 1px solid rgba(245, 200, 75, .2);
}
```
8. In the `<script>` block, `renderStats()`: add a breakeven line right after the losses line:
```javascript
  set("c-breakeven", s.breakevens);
```
9. `renderConfig()`: replace its entire body with:
```javascript
function renderConfig() {
  const c = data.config;

  set("cfg-tf", `${c.trend_tf} / ${c.entry_tf}`);
  set("cfg-quality", `score ≥ ${c.min_signal_score}`);
  set("cfg-confirm", `${(c.entry_buffer_pct * 100).toFixed(3)}%`);
  set("cfg-confirm-sub", `no-chase ${(c.no_chase_max_distance_pct * 100).toFixed(2)}%, expires after ${c.pending_signal_expiry_candles} candles`);
  set("cfg-rr", `+${c.tp_roi_pct}% / -${c.max_sl_roi_pct}%`);
  set("cfg-rr-sub", `BE at +${c.breakeven_trigger_roi_pct}% | ${c.leverage}x`);
}
```
10. Pending-setups table header column stays `<th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Score</th><th>Armed</th>` (only the `T1`→`TP` rename from item 6 above — the JS `renderPendingSetups()` function already reads `r.tp_price` into that column, no JS change needed there).

- [ ] **Step 7: Manually verify the dashboard boots and serves valid JSON**

```bash
python -m py_compile webui.py
```
Expected: no output, exit code 0.

Then (optional, requires no live DB — an empty/missing `signals.db` is handled by `_table_exists` returning `False`):
```bash
WEBUI_TOKEN=test123 python webui.py &
sleep 2
curl -s "http://localhost:6060/api/data?token=test123" | python -m json.tool | head -30
kill %1
```
Expected: valid JSON with `"config": {"strategy": "Precision Pullback Scalper v1", ...}` and no `signal_mode` key. Stop and fix before continuing if the server fails to start or the JSON is malformed.

- [ ] **Step 8: Commit**

```bash
git add webui.py tests/test_webui_stats.py
git commit -m "feat: update dashboard for Precision Pullback Scalper v1"
```

---

### Task 12: Rewrite `bot.py`

**Files:**
- Modify: `bot.py` (`format_signal`, `notify_outcome`, `cmd_status`; delete `notify_target_progress`, `format_v3_signal`, `broadcast_v3_signal`, `notify_v3_progress`)
- Modify: `tests/test_bot_formatting.py` (rewritten, not deleted — the file still tests `format_signal`, just against the new single-TP shape)

**Interfaces:**
- Consumes: Task 3's new `config` constants.
- Produces: `format_signal(signal, signal_id) -> str` (unchanged signature), `notify_outcome(app, signal_db) -> None` (unchanged signature, gains a `"breakeven"` branch), `cmd_status(update, context)` (unchanged signature). `build_app()` is unaffected — no v3 command handlers were ever registered there.

- [ ] **Step 1: Rewrite `tests/test_bot_formatting.py`'s failing assertions**

Replace the full file:

```python
from datetime import datetime, timezone

import bot
from bot import format_signal
from strategy import Signal


def _sample_signal() -> Signal:
    return Signal(
        symbol="XRP_USDT",
        direction="LONG",
        entry_price=1.100000,
        tp_price=1.108250,
        sl_price=1.088500,
        leverage=20,
        tp_roi_pct=7.0,
        sl_roi_pct=10.0,
        timeframe_summary="Precision Pullback confirmation",
        generated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        rr=0.70,
        score=82.5,
        entry_low=1.100000,
        entry_high=1.100000,
    )


def test_format_signal_contains_key_fields(monkeypatch):
    monkeypatch.setattr(bot, "STRATEGY_NAME", "Precision Pullback Scalper v1")
    msg = format_signal(_sample_signal(), signal_id=12)

    assert "XRP/USDT" in msg
    assert "LONG" in msg
    assert "1.1" in msg
    assert "1:0.7" in msg
    assert "20x" in msg
    assert "Precision Pullback Scalper v1" in msg
    assert "12" in msg


def test_format_signal_does_not_show_ladder_targets():
    msg = format_signal(_sample_signal(), signal_id=13)
    assert "T2" not in msg
    assert "T3" not in msg
    assert "(to T1)" not in msg


def test_format_signal_short_uses_red_arrow():
    sig = _sample_signal()
    sig.direction = "SHORT"
    msg = format_signal(sig, signal_id=15)
    assert "SHORT" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot_formatting.py -v`
Expected: FAIL — `format_signal` still shows `"🎯 T1 (50%):"` and `"(to T1)"`.

- [ ] **Step 3: Rewrite `format_signal` (lines 62-87)**

```python
def format_signal(signal, signal_id: int) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    lines = [
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"🎯 TP:       {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% gross ROI')}",
    ]
    lines.append(f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% gross ROI')}")
    lines.append(f"📊 RR:       {_code(f'1:{signal.rr:.3g}')}")
    lines.append(f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}")
    lines.append(f"🧭 Setup:    {_italic(escape(signal.timeframe_summary))}")
    lines.append(f"📈 Strategy: {STRATEGY_NAME}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {_code(signal.generated_at.astimezone(LKT).strftime('%Y-%m-%d %H:%M LKT'))}")
    lines.append(f"🆔 Signal ID: {_code(signal_id)}")
    lines.append(_italic("⚠️ Not financial advice. Use risk management."))
    return "\n".join(lines)
```

- [ ] **Step 4: Rewrite `notify_outcome` (lines 171-199)**

```python
async def notify_outcome(app: Application, signal_db: dict) -> None:
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0.0

    if status == "win":
        emoji, label = "✅", f"TARGET HIT {roi:+.1f}%"
    elif status == "breakeven":
        emoji, label = "⚖️", f"BREAKEVEN STOP {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOPPED OUT {roi:+.1f}%"
    else:
        emoji, label = "💤", "EXPIRED"

    arrow = "🟢" if direction == "LONG" else "🔴"
    msg = "\n".join([
        f"{emoji} {_bold('Signal Closed')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Result: {_code(label)}",
        f"🆔 ID: {_code(signal_db['id'])}",
    ])
    await _send_html(app, msg)
```

- [ ] **Step 5: Delete `notify_target_progress`, `format_v3_signal`, `broadcast_v3_signal`, `notify_v3_progress`**

Remove these four functions entirely (the old lines 95-114 and 117-168) — nothing calls any of them once Task 9's `main.py` is in place.

- [ ] **Step 6: Rewrite `cmd_status` (lines 241-303)**

```python
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import coin_scanner

    from config import (
        STRATEGY_NAME,
        TREND_TF, ENTRY_TF,
        MIN_SIGNAL_SCORE,
        TP_ROI_PCT, MAX_SL_ROI_PCT, BREAKEVEN_TRIGGER_ROI_PCT,
        NO_CHASE_MAX_DISTANCE_PCT,
        ATR_MIN_PCT, ATR_MAX_PCT,
        PENDING_SIGNAL_EXPIRY_CANDLES,
        SCAN_INTERVAL_MINUTES,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS,
        SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )

    state  = "⏸ PAUSED" if paused else "▶️ RUNNING"
    coins  = coin_scanner.get_cached_coins()
    active_long  = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")

    today_start   = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=tz.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig      = db.latest_signal_time()
    last_sig_str  = last_sig.astimezone(LKT).strftime("%H:%M LKT") if last_sig else "none"

    pairs_str = "  ".join(s.replace("_USDT", "") for s in coins[:20])
    cg_status = "SET" if COINGLASS_API_KEY else "not set"

    msg = "\n".join([
        "📡 <b>Scanner Status</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:       {_code(state)}",
        f"Strategy:    {_code(STRATEGY_NAME)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"TF:          {_code(f'{TREND_TF} trend / {ENTRY_TF} entry')}",
        f"Min score:   {_code(f'{MIN_SIGNAL_SCORE:.0f}/100')}",
        f"No-chase:    {_code(f'{NO_CHASE_MAX_DISTANCE_PCT*100:.2f}%')}  {_italic(f'ATR band {ATR_MIN_PCT*100:.2f}-{ATR_MAX_PCT*100:.2f}%')}",
        f"TP / SL:     {_code(f'+{TP_ROI_PCT:.1f}% / -{MAX_SL_ROI_PCT:.1f}% ROI')}  {_italic(f'BE at +{BREAKEVEN_TRIGGER_ROI_PCT:.1f}%')}",
        f"Pending exp: {_code(f'{PENDING_SIGNAL_EXPIRY_CANDLES} candles')}",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan every:  {_code(f'{SCAN_INTERVAL_MINUTES}min')}",
        f"Outcome chk: {_code(f'every {OUTCOME_CHECK_MINUTES} min')}",
        f"Cooldown:    {_code(f'{SIGNAL_COOLDOWN_MINUTES} min per coin')}",
        f"Expire:      {_code(f'{SIGNAL_EXPIRE_HOURS}h')}",
        f"Daily cap:   {_code(f'{signals_today}/{MAX_DAILY_SIGNALS}  (min gap {MIN_DAILY_SIGNAL_GAP_MINUTES} min)')}",
        f"Active:      {_code(f'{active_long}/{MAX_ACTIVE_LONG_SIGNALS} LONG, {active_short}/{MAX_ACTIVE_SHORT_SIGNALS} SHORT')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pool size:   {_code(f'{len(coins)} / {TOP_N_COINS} (min {COIN_POOL_MIN_SELECTED})')}",
        f"Min volume:  {_code(f'${COIN_POOL_MIN_VOLUME_USD:,.0f}')}",
        f"CoinGlass:   {_code(cg_status)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Today:       {_code(f'{signals_today} signals')}",
        f"Last signal: {_code(last_sig_str)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pool ({len(coins)}):  {_code(pairs_str)}",
        f"Time (LKT):  {_code(datetime.now(LKT).strftime('%H:%M'))}",
    ])

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
```

Also update the module docstring at the top of `bot.py` (line 2): `"""Telegram bot: commands and signal broadcast for VP-OB Confluence strategy.` → `"""Telegram bot: commands and signal broadcast for Precision Pullback Scalper v1.`

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot_formatting.py -v`
Expected: all PASS.

- [ ] **Step 8: Confirm no leftover references to deleted v3 functions/config**

Run: `grep -n "format_v3_signal\|broadcast_v3_signal\|notify_v3_progress\|notify_target_progress\|SCALPER_V3\|SIGNAL_MODE\|MIN_RR" bot.py`
Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add bot.py tests/test_bot_formatting.py
git commit -m "feat: update Telegram bot for Precision Pullback Scalper v1"
```

---

### Task 13: Delete orphaned files

**Files:**
- Delete: `super_scalper_v3.py`, `scalper_v3_strategy.py`, `liq_estimator.py`, `nw_kernel.py`
- Delete: `tests/test_super_scalper_v3.py`, `tests/test_scalper_v3_strategy.py`, `tests/test_binocular_indicators.py`, `tests/test_strategy_binocular_pending.py`, `tests/test_database_binocular_columns.py`
- Delete: `backtest/engine.py`, `backtest/optimize.py`, `backtest/rs_continuation_backtest.py`, `backtest/tpsl_scan.py`, `backtest/tpsl_walkforward.py`, `tests/test_backtest_engine.py`

**Note — correction to the design spec:** the spec claimed `backtest/engine.py` is "fully independent — untouched," inherited from the prior Binocular migration's cross-reference check without re-verifying it for this change. Re-checking during planning found `backtest/engine.py`, `backtest/optimize.py`, `backtest/tpsl_scan.py`, and `backtest/tpsl_walkforward.py` all `import scalper_v3_strategy` / `from super_scalper_v3 import SuperScalper` directly, and `backtest/rs_continuation_backtest.py` imports `SCALPER_V3_MAX_SL_PRICE_PCT`/`SCALPER_V3_TP_PRICE_PCT` from `config` — all five would raise `ImportError` the moment `scalper_v3_strategy.py`/`super_scalper_v3.py`/those config constants are gone. Since they're exclusively Super Scalper v3 backtest tooling (confirmed via their own module docstrings), they're deleted here alongside the strategy files they depend on, consistent with the user's "remove old alternate strategies" scope decision. `backtest/relative_strength.py` and `backtest/fetch_data.py` have no such dependency (verified: only import `pandas`/`mexc_client`/generic `config` names) and are kept.

- [ ] **Step 1: Confirm the dependency claim before deleting**

Run:
```bash
grep -l "scalper_v3_strategy\|super_scalper_v3\|SCALPER_V3_MAX_SL_PRICE_PCT\|SCALPER_V3_TP_PRICE_PCT" backtest/*.py
```
Expected: `backtest/engine.py`, `backtest/optimize.py`, `backtest/rs_continuation_backtest.py`, `backtest/tpsl_scan.py`, `backtest/tpsl_walkforward.py` — exactly the five files this task deletes.

- [ ] **Step 2: Delete the files**

```bash
git rm super_scalper_v3.py scalper_v3_strategy.py liq_estimator.py nw_kernel.py
git rm tests/test_super_scalper_v3.py tests/test_scalper_v3_strategy.py tests/test_binocular_indicators.py tests/test_strategy_binocular_pending.py tests/test_database_binocular_columns.py
git rm backtest/engine.py backtest/optimize.py backtest/rs_continuation_backtest.py backtest/tpsl_scan.py backtest/tpsl_walkforward.py tests/test_backtest_engine.py
```

- [ ] **Step 3: Confirm nothing still imports the deleted modules**

Run:
```bash
grep -rln "import super_scalper_v3\|import scalper_v3_strategy\|from super_scalper_v3\|from scalper_v3_strategy\|import liq_estimator\|from liq_estimator\|import nw_kernel\|from nw_kernel" --include="*.py" . | grep -v "^\./venv/\|__pycache__\|\.worktrees"
```
Expected: no output.

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest -v`
Expected: all remaining tests PASS, no collection errors from the deleted files. (`tests/test_correlation_limits.py`, `tests/test_database_direction_counts.py`, `tests/test_indicators.py`, `tests/test_mexc_client.py` should all be untouched and green — they don't reference anything deleted in this task.)

- [ ] **Step 5: Commit**

```bash
git commit -m "chore: remove Super Scalper v3, liq_estimator, nw_kernel, and their exclusive backtest tooling"
```

---

### Task 14: Rewrite `scripts/backtest_simple_strategy.py`

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (full-file rewrite)

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation`, `outcome_check.check_tp_sl_with_breakeven`, `config.ENTRY_TF/TREND_TF/ENTRY_KLINE_COUNT/EMA_TREND_LEN/EMA_TREND_SLOPE_LOOKBACK/EMA_SLOW_LEN/RSI_PERIOD/VOLUME_MA_PERIOD/ATR_PERIOD/BREAKEVEN_TRIGGER_PRICE_PCT/LEVERAGE/ESTIMATED_*_FEE_PCT/ESTIMATED_SLIPPAGE_PCT`.

This script has no unit test in the existing suite (`tests/test_backtest_engine.py`, the only backtest-adjacent test, is deleted in Task 13 as v3-exclusive) — its correctness gate is that it imports cleanly and runs against a tiny local sample without crashing, since a real backtest run against live MEXC data is explicitly out of scope for this pass (per the design spec).

- [ ] **Step 1: Replace the full content of `scripts/backtest_simple_strategy.py`**

```python
"""
Backtest utility for Precision Pullback Scalper v1.

Two-phase simulation: an armed pending setup (from
strategy.detect_pending_setup, as-of each bar) waits for a breakout
confirmation (as strategy.check_setup_confirmation would live), then
outcome_check.check_tp_sl_with_breakeven walks the fixed single TP/SL
(with its one breakeven step) forward from the confirming bar -- the
exact same functions the live bot uses, so backtest and live share one
source of truth and no signal logic is duplicated here.

Needs both TREND_TF and ENTRY_TF historical data per symbol -- fetch both
with backtest/fetch_data.py first (arbitrary --interval supported).

History beyond a single REST request's cap (MAX_REST_COUNT) is assembled
by paging backward via `end` cursors (see get_klines_extended). The
exchange may still run out of older data before --days is satisfied; the
script reports what it actually achieved.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy
from outcome_check import check_tp_sl_with_breakeven
from mexc_client import get_klines
from config import (
    ENTRY_TF, TREND_TF, ENTRY_KLINE_COUNT, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    EMA_TREND_LEN, EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD,
    VOLUME_MA_PERIOD, ATR_PERIOD, BREAKEVEN_TRIGGER_PRICE_PCT, LEVERAGE,
)

MAX_REST_COUNT = 2000   # single-request ceiling this script asks MEXC for


def get_klines_extended(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Page backward past MEXC's single-request ceiling via `end` cursors to
    assemble up to `days` of history. Stops early if the exchange runs out
    of older data (returns fewer, or a short final page)."""
    tf_minutes = _TF_MINUTES.get(interval, 5)
    target_start = datetime.utcnow() - timedelta(days=days)

    chunks: list[pd.DataFrame] = []
    cursor_end: datetime | None = None
    seen_earliest: datetime | None = None

    while True:
        end_param = int(cursor_end.timestamp()) if cursor_end is not None else None
        df = get_klines(symbol, interval, count=MAX_REST_COUNT, end=end_param)
        if df.empty:
            break

        chunks.append(df)
        earliest = df.index[0].to_pydatetime()
        if seen_earliest is not None and earliest >= seen_earliest:
            break
        seen_earliest = earliest

        if earliest <= target_start:
            break

        cursor_end = earliest - timedelta(minutes=tf_minutes)
        time.sleep(0.25)

    if not chunks:
        return pd.DataFrame()

    combined = pd.concat(chunks)
    combined = combined[~combined.index.duplicated(keep="first")]
    combined.sort_index(inplace=True)
    return combined[combined.index >= target_start]


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    rr: float
    outcome: str            # "win" | "loss" | "breakeven" | "expired"
    gross_roi_pct: float
    net_roi_pct: float
    breakeven_triggered: bool = False
    closed_at: str = ""


@dataclass
class BacktestStats:
    trades: list[Trade] = field(default_factory=list)

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)

    def print_report(self) -> None:
        n = len(self.trades)
        print(f"Total trades:        {n}")
        if n == 0:
            print("No trades generated -- nothing further to report.")
            return

        wins = [t for t in self.trades if t.outcome == "win"]
        losses = [t for t in self.trades if t.outcome == "loss"]
        breakevens = [t for t in self.trades if t.outcome == "breakeven"]
        expired = [t for t in self.trades if t.outcome == "expired"]

        closed_for_rate = len(wins) + len(losses)
        win_rate = (len(wins) / closed_for_rate * 100.0) if closed_for_rate else 0.0
        gross_roi = sum(t.gross_roi_pct for t in self.trades)
        total_fees = sum(t.gross_roi_pct - t.net_roi_pct for t in self.trades)
        net_roi = sum(t.net_roi_pct for t in self.trades)
        avg_roi = net_roi / n

        consecutive = max_consecutive = 0
        running = peak = 0.0
        max_drawdown = 0.0
        for t in self.trades:
            if t.outcome == "loss":
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            else:
                consecutive = 0
            running += t.net_roi_pct
            peak = max(peak, running)
            max_drawdown = min(max_drawdown, running - peak)

        avg_rr = sum(t.rr for t in self.trades) / n

        longs = [t for t in self.trades if t.direction == "LONG"]
        shorts = [t for t in self.trades if t.direction == "SHORT"]

        print(f"Wins:                {len(wins)}")
        print(f"Losses:              {len(losses)}")
        print(f"Breakeven:           {len(breakevens)}")
        print(f"Expired trades:      {len(expired)}")
        print(f"Win rate (win/loss): {win_rate:.1f}%")
        print(f"Gross ROI:           {gross_roi:+.1f}%")
        print(f"Estimated fees:      {total_fees:.1f}%")
        print(f"Net ROI:             {net_roi:+.1f}%")
        print(f"Average ROI/trade:   {avg_roi:+.2f}%")
        print(f"Max consecutive losses: {max_consecutive}")
        print(f"Max drawdown:        {max_drawdown:.1f}%")
        print(f"Average RR:          {avg_rr:.2f}")

        def _bucket_report(label: str, bucket: list[Trade]) -> None:
            if not bucket:
                print(f"{label} performance:  no trades")
                return
            bwins = sum(1 for t in bucket if t.outcome == "win")
            print(
                f"{label} performance:  {len(bucket)} trades, "
                f"{bwins}/{len(bucket)} wins ({bwins / len(bucket) * 100:.1f}%), "
                f"net ROI {sum(t.net_roi_pct for t in bucket):+.1f}%"
            )

        _bucket_report("LONG", longs)
        _bucket_report("SHORT", shorts)

        print("\nPerformance by symbol:")
        for symbol in sorted({t.symbol for t in self.trades}):
            _bucket_report(f"  {symbol}", [t for t in self.trades if t.symbol == symbol])

        breakeven_rate = sum(1 for t in self.trades if t.breakeven_triggered) / n * 100
        print(f"\nBreakeven-trigger rate: {breakeven_rate:.1f}%")

        print("\nMonthly performance:")
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                month_key = t.closed_at[:7]
                by_month[month_key].append(t)
        for month_key in sorted(by_month):
            _bucket_report(f"  {month_key}", by_month[month_key])


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    """Last `window_count` rows ending at upto_idx, plus a duplicated last
    row standing in for the still-forming candle, so
    detect_pending_setup's/check_setup_confirmation's iloc[:-1] leaves
    exactly that trailing window as 'completed'."""
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start : upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state. One setup/trade at a time."""
    trades: list[Trade] = []

    df_entry_full = get_klines_extended(symbol, ENTRY_TF, days)
    df_trend_full = get_klines_extended(symbol, TREND_TF, days)

    if df_entry_full.empty or df_trend_full.empty:
        print(f"[{symbol}] no candle history returned for one or both timeframes -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_entry_full)} x {ENTRY_TF}, "
        f"{len(df_trend_full)} x {TREND_TF} bars", flush=True,
    )

    min_start = max(EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD, VOLUME_MA_PERIOD, ATR_PERIOD) + 10

    original_get_market_klines = strategy.get_market_klines
    pending_setup: dict | None = None
    in_trade_until_idx = -1

    try:
        for i in range(min_start, len(df_entry_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of_entry = _with_forming_row(df_entry_full, i, ENTRY_KLINE_COUNT)
            ts = df_entry_full.index[i]
            as_of_trend = df_trend_full[df_trend_full.index <= ts]
            if as_of_trend.empty:
                continue
            as_of_trend = _with_forming_row(as_of_trend, len(as_of_trend) - 1, ENTRY_KLINE_COUNT)

            def _fake(sym: str, interval: str, count: int = 100, _entry=as_of_entry, _trend=as_of_trend):
                if interval == ENTRY_TF:
                    return _entry
                if interval == TREND_TF:
                    return _trend
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            if pending_setup is not None:
                status, fill_price = strategy.check_setup_confirmation(pending_setup)
                if status in ("expired", "invalidated"):
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue

                # confirmed
                entry_candle_cutoff = df_entry_full.index[i]
                direction = pending_setup["direction"]
                breakeven_trigger_price = (
                    fill_price * (1 + BREAKEVEN_TRIGGER_PRICE_PCT) if direction == "LONG"
                    else fill_price * (1 - BREAKEVEN_TRIGGER_PRICE_PCT)
                )
                result = check_tp_sl_with_breakeven(
                    direction, fill_price, pending_setup["sl_price"], pending_setup["tp_price"],
                    breakeven_trigger_price, df_entry_full, entry_candle_cutoff,
                )
                bars_held = 1
                if result is None:
                    outcome = "expired"
                    gross_roi_pct = 0.0
                    breakeven_triggered = False
                    closed_at_str = str(df_entry_full.index[i])
                else:
                    outcome = result["status"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    breakeven_triggered = result["breakeven_triggered_at"] is not None
                    closed_idx = df_entry_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=direction, entry_price=fill_price,
                    tp_price=pending_setup["tp_price"], sl_price=pending_setup["sl_price"],
                    rr=pending_setup["rr"], outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    breakeven_triggered=breakeven_triggered,
                    closed_at=closed_at_str,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["created_at"] = df_entry_full.index[i].isoformat()
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Precision Pullback Scalper v1")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=30, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    stats = BacktestStats()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(backtest_symbol, symbol, args.days): symbol
            for symbol in args.symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                for trade in future.result():
                    stats.add(trade)
            except Exception as e:
                print(f"[{symbol}] FAILED: {e}", flush=True)

    print("\n" + "=" * 60)
    stats.print_report()


if __name__ == "__main__":
    sys.exit(main() or 0)
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import backtest_simple_strategy"`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "feat: rewrite backtest script for Precision Pullback Scalper v1's two-phase pipeline"
```

---

### Task 15: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS, zero collection errors.

- [ ] **Step 2: `py_compile` every changed/new production file**

Run:
```bash
python -m py_compile config.py database.py strategy.py main.py bot.py webui.py outcome_check.py reports.py scripts/backtest_simple_strategy.py
```
Expected: no output, exit code 0.

- [ ] **Step 3: Confirm no stray references to anything removed in this plan**

Run:
```bash
grep -rn "calculate_pvt\|calculate_chandelier_direction\|calculate_daily_vwap\|calculate_binocular_trigger\|RIBBON_MA\|SIGNAL_MODE\|CHANDELIER_\|BINOCULAR_EMA200\|MIN_RR\b\|STRATEGY_V1_ENABLED\|SCALPER_V3\|STRATEGY_NAME_V3\|check_target_ladder\|check_tp_sl\b" --include="*.py" . | grep -v "^\./venv/\|__pycache__\|\.worktrees\|docs/superpowers"
```
Expected: no output (the spec/plan docs themselves are excluded from this check since they legitimately discuss the removed names historically).

- [ ] **Step 4: Local dry-run boot check**

Run (from repo root, with `.env` containing at least placeholder `TELEGRAM_TOKEN`/`TELEGRAM_CHANNEL_ID` — reuse whatever `.env` already exists locally):
```bash
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false timeout 20 python main.py
```
Expected: startup logs show:
```
Strategy: Precision Pullback Scalper v1
Trend TF: 15m  Entry TF: 5m
Min signal score: 80
TP: +7.0% ROI  SL: -10.0% ROI  Breakeven at +4.0% ROI
Leverage: 20x
Dry run: enabled
```
followed by coin pool loading and `Scheduler started`, with no tracebacks, before the 20s timeout kills it. (If there's no usable `.env`/network in this environment, this step may need to run on the server per `CLAUDE.md`'s deployment section instead — note that explicitly rather than skipping verification silently.)

- [ ] **Step 5: Confirm the backup branch is intact and main has moved past it**

Run: `git log --oneline backup/main-pre-precision-pullback-scalper-v1..main | wc -l`
Expected: a positive number (every commit from Tasks 1-14 that landed on `main`).

No commit for this task — pure verification. If any step fails, fix the underlying issue and re-run this task's steps from the top before considering the work done.

---

## Summary of what this plan does NOT include (explicitly deferred)

- Running the actual 6-month backtest / walk-forward parameter tuning architecture.txt describes (needs server-side data fetch — separate follow-up session, per explicit user instruction during brainstorming).
- Flipping `DRY_RUN` or `LIVE_ENABLED` to `true` — this strategy has not been validated on historical data yet.
- Deploying to the production server — `main`'s auto-deploy workflow (`.github/workflows/deploy.yml`) will pick these commits up on push per existing CI, but pushing is a separate, deliberate action outside this plan's scope (confirm with the user before pushing, since `main` auto-deploys).
