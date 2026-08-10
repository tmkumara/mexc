# Zero-Lag MTF Pullback v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the live Precision Pullback Scalper v1 strategy with Zero-Lag MTF Pullback v1 — a four-timeframe (4h/1h/15m/5m) zero-lag-EMA trend-alignment + pullback + crossover-confirmation model with fixed TP/SL and no breakeven — as the sole active strategy on `main`.

**Architecture:** Same shape as today's bot (APScheduler + SQLite + python-telegram-bot), but the single 5-minute scan job splits into two: a 5-minute `scan_for_new_setups` job that arms a `pending_pullback` row once 4H/1H zero-lag trend state agree and 15m price has pulled back to its own ZLEMA, and a 1-minute `monitor_pending_setups` job that advances armed setups through a `pending_breakout` stage (5m ZLEMA crossover + confirmation candle) to `fired` (price breaks the trigger level). `strategy.py` stays pure (no DB access, returns dicts/tuples); `main.py` owns all persistence and Telegram side effects, matching the existing convention.

**Tech Stack:** Python, pandas/numpy, APScheduler (AsyncIOScheduler), SQLite (stdlib `sqlite3`), python-telegram-bot, pytest.

## Global Constraints

- Every source file this plan touches uses `from __future__ import annotations` where the original already does — do not remove it.
- No breakeven logic anywhere in this pass — `check_tp_sl` (new) has no breakeven branch; `check_tp_sl_with_breakeven` is deleted.
- `strategy.py` never imports or calls `database`/`db` — it returns plain dicts/tuples; only `main.py` (and the backtest script) touch the database.
- Only fully closed candles are ever used — every kline fetch drops the forming candle via `.iloc[:-1]`, on all four timeframes.
- `MIN_CANDLE_SETTLE_SECONDS` gating and the scan-cron settle-offset trick in `main.py` (the mechanism documented in `CLAUDE.md` as hard-won) must not be removed or weakened.
- `LEVERAGE = 20`, `TP_ROI_PCT = 7.0`, `SL_ROI_PCT = 10.0` (renamed from `MAX_SL_ROI_PCT`) — fixed ROI-at-leverage TP/SL, not structural.
- `MAX_DAILY_SIGNALS = 12`, `MAX_CONCURRENT_SIGNALS = 4`, `MAX_ACTIVE_LONG_SIGNALS = 2`, `MAX_ACTIVE_SHORT_SIGNALS = 2` (all changed from Precision Pullback's lower defaults, per the approved spec).
- `ZERO_LAG_LENGTH = 70`, `ZERO_LAG_BAND_LOOKBACK = 210`, `ZERO_LAG_MULTIPLIER = 1.2`, `MIN_SIGNAL_SCORE = 80` — exact defaults from the spec, do not retune in this pass.
- Reference spec: `docs/superpowers/specs/2026-08-11-zero-lag-mtf-pullback-v1-design.md` — consult it for anything a task references but doesn't fully re-explain (e.g. the state-machine diagram, the "why" behind a resolved ambiguity).
- All work lands as commits directly on `main` (no feature branch), after Task 1's backup branch/tag exist.
- Run `python -m pytest -v` and confirm passing before every commit that touches code (not doc-only commits).

---

## File Structure

**New files:**
- `tests/test_zero_lag_indicators.py` — ZLEMA, band, stateful trend-state tests.
- `tests/test_strategy_zero_lag.py` — full pipeline tests (both stages of the state machine).
- `tests/test_outcome_check_plain.py` — plain TP/SL walker tests.

**Modified files:**
- `strategy.py` — new indicators, new pipeline (`detect_pending_setup`, `check_setup_confirmation`, `build_trade_prices`), old Precision-Pullback-only functions removed.
- `config.py` — Precision Pullback constants removed, Zero-Lag constants added.
- `database.py` — `armed_setups` table/accessors removed, `pending_setups` table/accessors added.
- `outcome_check.py` — `check_tp_sl_with_breakeven` removed, `check_tp_sl` added.
- `main.py` — `scan_and_fire_signals` split into `scan_for_new_setups` + `monitor_pending_setups`; `check_outcomes` updated for the plain walker.
- `bot.py` — `cmd_status` config display, `notify_outcome` (breakeven branch removed).
- `webui.py` — `get_pending_setups`, `get_strategy_config`, `renderConfig()` JS.
- `scripts/backtest_simple_strategy.py` — four-timeframe/three-state rewrite.
- `tests/test_indicators.py` — trimmed to ATR-only (the other indicators it covers are deleted).
- `tests/test_correlation_limits.py` — updated to not depend on the old `MAX_ACTIVE_*_SIGNALS=1` default.
- `tests/strategy_fixtures.py` — Precision-Pullback-only builders removed, new zero-lag builders added.
- `tests/test_bot_formatting.py`, `tests/test_webui_stats.py` — updated field/text expectations.

**Deleted files:**
- `tests/test_precision_pullback_indicators.py`
- `tests/test_strategy_precision_pullback.py`
- `tests/test_outcome_check_breakeven.py`
- `tests/test_database_breakeven_status.py`

---

## Task 1: Backup branch and tag

**Files:** none (git operations only)

- [ ] **Step 1: Confirm working tree is clean**

Run: `git status`
Expected: `nothing to commit, working tree clean` (aside from the pre-existing untracked `backtest/tpsl_walkforward.py` and `backtestfull.log`, which are out of scope — see spec's "Backtest harness" section).

- [ ] **Step 2: Cut the backup branch from current `main` HEAD**

```bash
git branch backup/main-pre-zero-lag-mtf-pullback-v1
git tag pre-zero-lag-mtf-pullback-v1
```

- [ ] **Step 3: Push both to origin**

```bash
git push origin backup/main-pre-zero-lag-mtf-pullback-v1
git push origin pre-zero-lag-mtf-pullback-v1
```

- [ ] **Step 4: Verify**

Run: `git branch -a | grep zero-lag` and `git tag | grep zero-lag`
Expected: both the local and `remotes/origin/` copies of the branch, and the tag, are listed.

No commit for this task — it's pure git branch/tag setup, nothing to add to the index.

---

## Task 2: Zero-lag indicators

**Files:**
- Modify: `strategy.py` (add three new functions near the existing `# ── indicators ──` section, after `calculate_atr`)
- Test: `tests/test_zero_lag_indicators.py` (new)

**Interfaces:**
- Produces: `calculate_zlema(series: pd.Series, length: int) -> pd.Series`, `calculate_zlema_band(df: pd.DataFrame, zlema: pd.Series, atr_period: int, atr_lookback: int, multiplier: float) -> tuple[pd.Series, pd.Series]` (returns `(upper, lower)`), `calculate_zlema_trend_state(df: pd.DataFrame, zlema: pd.Series, upper: pd.Series, lower: pd.Series) -> pd.Series` (returns an `int` series of `-1`/`0`/`1`). All three live in `strategy.py` and are consumed by Task 5/6's pipeline.

These are pure functions — this task does not yet wire them into the pipeline or touch `config.py`, so it can be written and tested in isolation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zero_lag_indicators.py`:

```python
import numpy as np
import pandas as pd
import pytest

from strategy import calculate_zlema, calculate_zlema_band, calculate_zlema_trend_state


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_zlema_matches_hand_computed_construction():
    # length=5 -> lag = floor((5-1)/2) = 2
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])
    zlema = calculate_zlema(closes, length=5)

    lag = 2
    adjusted = 2 * closes - closes.shift(lag)
    expected = adjusted.ewm(span=5, adjust=False).mean()

    pd.testing.assert_series_equal(zlema, expected, check_names=False)


def test_zlema_lag_floor_division():
    # length=70 -> lag = floor((70-1)/2) = 34, matches architecture.txt exactly
    closes = pd.Series(np.arange(100, dtype=float))
    zlema = calculate_zlema(closes, length=70)
    adjusted = 2 * closes - closes.shift(34)
    expected = adjusted.ewm(span=70, adjust=False).mean()
    pd.testing.assert_series_equal(zlema, expected, check_names=False)


def test_zlema_band_widens_with_multiplier():
    df = _trend_df(250, step=0.3)
    zlema = calculate_zlema(df["close"], length=70)
    upper_1x, lower_1x = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=1.0)
    upper_2x, lower_2x = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=2.0)

    i = -1
    assert (upper_2x.iloc[i] - zlema.iloc[i]) == pytest.approx(2 * (upper_1x.iloc[i] - zlema.iloc[i]), rel=1e-6)
    assert (zlema.iloc[i] - lower_2x.iloc[i]) == pytest.approx(2 * (zlema.iloc[i] - lower_1x.iloc[i]), rel=1e-6)


def test_trend_state_starts_neutral_then_flips_on_cross_above():
    df = _trend_df(250, step=0.0)  # flat until the breakout below
    df.loc[df.index[-30:], "close"] = df["close"].iloc[-31] + np.linspace(0, 50, 30)
    df.loc[df.index[-30:], "high"] = df.loc[df.index[-30:], "close"] + 0.2
    df.loc[df.index[-30:], "low"] = df.loc[df.index[-30:], "close"] - 0.2

    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=1.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)

    assert state.iloc[0] == 0
    assert state.iloc[-1] == 1


def test_trend_state_holds_through_a_dip_that_does_not_cross_opposite_band():
    df = _trend_df(300, step=0.4)  # strong steady uptrend
    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=1.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)

    flip_idx = state[state == 1].index[0]
    flip_pos = df.index.get_loc(flip_idx)
    # dip the very next bar back toward zlema without crossing the lower band
    df.loc[df.index[flip_pos + 1], "close"] = float(zlema.iloc[flip_pos + 1])
    zlema2 = calculate_zlema(df["close"], length=70)
    upper2, lower2 = calculate_zlema_band(df, zlema2, atr_period=70, atr_lookback=210, multiplier=1.2)
    state2 = calculate_zlema_trend_state(df, zlema2, upper2, lower2)

    assert state2.iloc[flip_pos + 1] == 1  # held, did not reset to 0 or flip to -1


def test_trend_state_flips_to_bearish_on_cross_below():
    df = _trend_df(250, step=-0.4)
    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=1.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)
    assert state.iloc[-1] == -1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_zero_lag_indicators.py -v`
Expected: `ImportError: cannot import name 'calculate_zlema'` (the function doesn't exist yet).

- [ ] **Step 3: Implement the three indicator functions**

In `strategy.py`, immediately after the existing `calculate_volume_ma` function (before `_ema_trend_slope_up`), add:

```python
def calculate_zlema(series: pd.Series, length: int) -> pd.Series:
    """Zero-lag EMA per architecture.txt: lag = floor((length-1)/2);
    adjusted_price = 2*close - close.shift(lag); ZLEMA = EMA(adjusted, length)."""
    lag = (length - 1) // 2
    adjusted = 2.0 * series - series.shift(lag)
    return adjusted.ewm(span=length, adjust=False).mean()


def calculate_zlema_band(
    df: pd.DataFrame, zlema: pd.Series, atr_period: int, atr_lookback: int, multiplier: float,
) -> tuple[pd.Series, pd.Series]:
    """upper/lower = zlema +/- volatility, where volatility is the highest
    ATR(atr_period) over the last atr_lookback candles, times multiplier
    (architecture.txt's AlgoAlpha-derived band calculation)."""
    atr = calculate_atr(df, atr_period)
    volatility = atr.rolling(window=atr_lookback, min_periods=1).max() * multiplier
    return zlema + volatility, zlema - volatility


def calculate_zlema_trend_state(
    df: pd.DataFrame, zlema: pd.Series, upper: pd.Series, lower: pd.Series,
) -> pd.Series:
    """Stateful trend per architecture.txt: NOT close-vs-zlema. Flips to +1
    only when close is beyond the upper band, to -1 only when close is
    beyond the lower band, and otherwise HOLDS the previous state (starts
    neutral/0 until the first cross). Setting state=+1 every bar close
    stays above upper is equivalent to 'cross above' detection (it
    re-asserts the same value), and holding via the else branch is exactly
    the persistence architecture.txt describes -- same walk shape as this
    file's calculate_supertrend, for the same reason (each bar's state
    depends on the previous bar's, not vectorizable as a comparison)."""
    close = df["close"].to_numpy()
    upper_v = upper.to_numpy()
    lower_v = lower.to_numpy()
    n = len(df)
    state = np.zeros(n, dtype=int)

    for i in range(n):
        if close[i] > upper_v[i]:
            state[i] = 1
        elif close[i] < lower_v[i]:
            state[i] = -1
        elif i > 0:
            state[i] = state[i - 1]
        # else: i == 0 and price is inside the band -> stays 0 (neutral)

    return pd.Series(state, index=df.index)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_zero_lag_indicators.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_zero_lag_indicators.py
git commit -m "feat: add zero-lag EMA, band, and stateful trend-state indicators"
```

---

## Task 3: Rewrite `config.py`

**Files:**
- Modify: `config.py:57-136` (the "Strategy: Precision Pullback Scalper v1" section through the `MAX_ACTIVE_SHORT_SIGNALS` line)

**Interfaces:**
- Produces: `STRATEGY_NAME`, `MACRO_TF`, `TREND_TF`, `PULLBACK_TF`, `ENTRY_TF`, `MACRO_KLINE_COUNT`, `TREND_KLINE_COUNT`, `PULLBACK_KLINE_COUNT`, `ENTRY_KLINE_COUNT`, `ZERO_LAG_LENGTH`, `ZERO_LAG_BAND_LOOKBACK`, `ZERO_LAG_MULTIPLIER`, `ZERO_LAG_SLOPE_LOOKBACK`, `ATR_PERIOD`, `ENTRY_BUFFER_PCT`, `PULLBACK_DISTANCE_PCT`, `PENDING_EXPIRY_CANDLES`, `LEVERAGE`, `TP_ROI_PCT`, `SL_ROI_PCT`, `TP_PRICE_PCT`, `SL_PRICE_PCT`, `MIN_SIGNAL_SCORE`, `MAX_DAILY_SIGNALS`, `MAX_CONCURRENT_SIGNALS`, `MAX_ACTIVE_LONG_SIGNALS`, `MAX_ACTIVE_SHORT_SIGNALS`, `MONITOR_INTERVAL_MINUTES` — every later task's `from config import (...)` line pulls from this set.

This task has no test of its own (a config module with only constants has nothing to unit test) — its correctness is verified transitively once Task 5/6's strategy tests import these names.

- [ ] **Step 1: Replace the strategy config block**

In `config.py`, replace everything from the `# ── Strategy: Precision Pullback Scalper v1 ──` comment (line 57) through the `NO_CHASE_MAX_DISTANCE_PCT` line (line 90) — i.e. `STRATEGY_NAME` through the pullback-distance constants — with:

```python
# ── Strategy: Zero-Lag MTF Pullback v1 ──────────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Zero-Lag MTF Pullback v1",
)

MACRO_TF: str    = os.getenv("MACRO_TF", "4h")
TREND_TF: str    = os.getenv("TREND_TF", "1h")
PULLBACK_TF: str = os.getenv("PULLBACK_TF", "15m")
ENTRY_TF: str    = os.getenv("ENTRY_TF", "5m")

MACRO_KLINE_COUNT: int    = int(os.getenv("MACRO_KLINE_COUNT", "300"))
TREND_KLINE_COUNT: int    = int(os.getenv("TREND_KLINE_COUNT", "300"))
PULLBACK_KLINE_COUNT: int = int(os.getenv("PULLBACK_KLINE_COUNT", "250"))
ENTRY_KLINE_COUNT: int    = int(os.getenv("ENTRY_KLINE_COUNT", "250"))

ZERO_LAG_LENGTH: int         = int(os.getenv("ZERO_LAG_LENGTH", "70"))
ZERO_LAG_BAND_LOOKBACK: int  = int(os.getenv("ZERO_LAG_BAND_LOOKBACK", "210"))
ZERO_LAG_MULTIPLIER: float   = float(os.getenv("ZERO_LAG_MULTIPLIER", "1.2"))
ZERO_LAG_SLOPE_LOOKBACK: int = int(os.getenv("ZERO_LAG_SLOPE_LOOKBACK", "5"))

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # 0.02%, unchanged value/name
PULLBACK_DISTANCE_PCT: float = float(os.getenv("PULLBACK_DISTANCE_PCT", "0.10")) / 100.0
PENDING_EXPIRY_CANDLES: int = int(os.getenv("PENDING_EXPIRY_CANDLES", "6"))   # 6 x 5m = 30 min
```

Then replace the `VOLUME_MA_PERIOD`/`VOLUME_CONFIRM_MULT`/`MAX_CANDLE_BODY_PCT`/`ATR_MIN_PCT`/`ATR_MAX_PCT`/`MIN_SIGNAL_SCORE` block (the old lines 92-99) with:

```python
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "70"))   # feeds the zero-lag band, not a separate filter -- no ATR_MIN/MAX_PCT gate in this strategy

MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))
```

Then replace the `MAX_SL_ROI_PCT`/`LEVERAGE`/`MAX_SL_PRICE_PCT`/`TP_ROI_PCT`/`TP_PRICE_PCT`/`BREAKEVEN_TRIGGER_ROI_PCT`/`BREAKEVEN_TRIGGER_PRICE_PCT`/`ENTRY_BUFFER_PCT`/`PENDING_SIGNAL_EXPIRY_CANDLES` block (old lines 111-126) with:

```python
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))   # unchanged
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))   # unchanged default
SL_ROI_PCT: float = float(os.getenv("SL_ROI_PCT", "10.0"))   # renamed from MAX_SL_ROI_PCT -- fixed, not a ceiling (no breakeven step in this strategy)
TP_PRICE_PCT: float = TP_ROI_PCT / 100.0 / LEVERAGE
SL_PRICE_PCT: float = SL_ROI_PCT / 100.0 / LEVERAGE
```

- [ ] **Step 2: Raise the signal-frequency defaults**

Replace the old lines 130-136 (`MAX_DAILY_SIGNALS` through `MAX_ACTIVE_SHORT_SIGNALS`) with:

```python
MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "12"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES", "60"))

MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "4"))

MAX_ACTIVE_LONG_SIGNALS: int = int(os.getenv("MAX_ACTIVE_LONG_SIGNALS", "2"))
MAX_ACTIVE_SHORT_SIGNALS: int = int(os.getenv("MAX_ACTIVE_SHORT_SIGNALS", "2"))
```

(`MIN_DAILY_SIGNAL_GAP_MINUTES` is unchanged — repeated here only because it sits inside the block being replaced.)

- [ ] **Step 3: Add the monitor-job cadence constant**

Immediately after the `OUTCOME_CHECK_MINUTES` line in the `# ── Scheduler ──` section, add:

```python
MONITOR_INTERVAL_MINUTES: int = int(os.getenv("MONITOR_INTERVAL_MINUTES", "1"))
```

- [ ] **Step 4: Verify the module still imports cleanly**

Run: `python -c "import config"`
Expected: no output, exit code 0. (This will still succeed even though nothing references the new names yet — `config.py` has no internal cross-references to the removed names at this point since we haven't touched `strategy.py`'s import list yet. `strategy.py` importing the now-removed `EMA_FAST_LEN` etc. at its bottom will break — that's expected and fixed in Task 5.)

- [ ] **Step 5: Commit**

```bash
git add config.py
git commit -m "feat: replace Precision Pullback config with Zero-Lag MTF Pullback v1"
```

Note: this commit intentionally leaves `strategy.py`, `database.py`, `main.py`, `bot.py`, `webui.py` broken (they still import names this task removed) — Tasks 4-10 fix each in turn. This mirrors the Precision Pullback migration's own commit granularity (small, reviewable commits over a working end state at every step is not required until the final verification task).

---

## Task 4: `pending_setups` table and accessors in `database.py`

**Files:**
- Modify: `database.py:94-136` (the `armed_setups` table DDL inside `init_db()`)
- Modify: `database.py:293-412` (the entire "armed_setups table" accessor-function section)

**Interfaces:**
- Consumes: nothing new (uses the existing `_conn()` context manager, unchanged).
- Produces: `save_pending_setup(setup: dict) -> int | None`, `get_pending_setups(status: str, limit: int = 200) -> list[dict]`, `get_pending_setup_by_symbol(symbol: str) -> dict | None`, `pending_setup_exists(symbol: str) -> bool`, `update_pending_setup_breakout(setup_id: int, confirmation_high: float, confirmation_low: float, confirmation_close: float, confirmation_time: str, trigger_price: float) -> None`, `mark_pending_setup_fired(setup_id: int, signal_id: int, final_score: float) -> None`, `mark_pending_setup_missed(setup_id: int, reason: str = "", final_score: float | None = None) -> None`, `mark_pending_setup_expired(setup_id: int) -> None`, `expire_old_pending_setups(now: datetime) -> None`, `count_pending_setups() -> int`. Task 6/8 call these.

**Plan-level addition beyond the spec's literal schema:** the spec's `pending_setups` column list (from architecture.txt §13) already got `trigger_price` added as a necessary practical addition (flagged in the spec). This task adds two more, for the same reason — computing the final two-stage score (spec's "Scoring" section) needs the confirmation candle's actual close price and the timestamp the crossover happened, neither of which architecture.txt's schema lists but both of which are unrecoverable once the setup advances past that candle:
- `confirmation_close REAL` — the confirming candle's close, needed for the confirmation-candle-quality score component.
- `confirmation_time TEXT` — ISO timestamp of the crossover candle, needed to compute how many candles elapsed before the breakout triggered (the "freshness" score component).

- [ ] **Step 1: Replace the `armed_setups` table DDL**

In `database.py`, replace the entire block from `# ── armed_setups table ─────────────────────────────────────` (line 94) through the `ALTER TABLE armed_setups ADD COLUMN` loop (line 136) with:

```python
        # ── pending_setups table ───────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_setups (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol             TEXT    NOT NULL,
                direction          TEXT    NOT NULL,
                status             TEXT    NOT NULL DEFAULT 'pending_pullback',

                macro_tf           TEXT    NOT NULL,
                trend_tf           TEXT    NOT NULL,
                pullback_tf        TEXT    NOT NULL,
                entry_tf           TEXT    NOT NULL,

                macro_trend        INTEGER NOT NULL,
                trend_state        INTEGER NOT NULL,

                zlema_1h           REAL    NOT NULL,
                zlema_15m          REAL    NOT NULL,

                pullback_price     REAL    NOT NULL,
                pullback_time      TEXT    NOT NULL,

                confirmation_high  REAL,
                confirmation_low   REAL,
                confirmation_close REAL,
                confirmation_time  TEXT,
                trigger_price      REAL,

                score              REAL    NOT NULL,

                setup_time         TEXT    NOT NULL,
                expires_at         TEXT    NOT NULL,
                created_at         TEXT    NOT NULL,

                fired_signal_id    INTEGER,
                fired_at           TEXT,
                updated_at         TEXT,
                miss_reason        TEXT
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_setups_status
            ON pending_setups (status)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_setups_symbol_status
            ON pending_setups (symbol, status)
        """)
```

- [ ] **Step 2: Replace the `armed_setups` accessor functions**

Replace the entire `# ── armed_setups table ────────────────────────────────────────────` section (from `save_armed_setup` through `count_armed_setups`, lines 293-412) with:

```python
# ── pending_setups table ─────────────────────────────────────────

def save_pending_setup(setup: dict) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO pending_setups (
                symbol, direction, status,
                macro_tf, trend_tf, pullback_tf, entry_tf,
                macro_trend, trend_state,
                zlema_1h, zlema_15m,
                pullback_price, pullback_time,
                score, setup_time, expires_at, created_at, updated_at
            ) VALUES (?, ?, 'pending_pullback', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            setup["symbol"], setup["direction"],
            setup["macro_tf"], setup["trend_tf"], setup["pullback_tf"], setup["entry_tf"],
            setup["macro_trend"], setup["trend_state"],
            setup["zlema_1h"], setup["zlema_15m"],
            setup["pullback_price"], setup["pullback_time"],
            setup["score"], setup["setup_time"], setup["expires_at"], setup["created_at"], now,
        ))
        return cur.lastrowid


def get_pending_setups(status: str, limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM pending_setups
            WHERE status = ?
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, (status, limit)).fetchall()
        return [dict(r) for r in rows]


def get_pending_setup_by_symbol(symbol: str) -> dict | None:
    with _conn() as con:
        row = con.execute("""
            SELECT * FROM pending_setups
            WHERE symbol = ? AND status IN ('pending_pullback', 'pending_breakout')
            ORDER BY created_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        return dict(row) if row else None


def pending_setup_exists(symbol: str) -> bool:
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM pending_setups
            WHERE symbol = ? AND status IN ('pending_pullback', 'pending_breakout')
            LIMIT 1
        """, (symbol,)).fetchone()
        return row is not None


def update_pending_setup_breakout(
    setup_id: int, confirmation_high: float, confirmation_low: float,
    confirmation_close: float, confirmation_time: str, trigger_price: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'pending_breakout',
                confirmation_high = ?, confirmation_low = ?, confirmation_close = ?,
                confirmation_time = ?, trigger_price = ?, updated_at = ?
            WHERE id = ? AND status = 'pending_pullback'
        """, (confirmation_high, confirmation_low, confirmation_close, confirmation_time, now, setup_id))


def mark_pending_setup_fired(setup_id: int, signal_id: int, final_score: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'fired', fired_signal_id = ?, fired_at = ?, score = ?, updated_at = ?
            WHERE id = ?
        """, (signal_id, now, final_score, now, setup_id))


def mark_pending_setup_missed(setup_id: int, reason: str = "", final_score: float | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'missed', miss_reason = ?, score = COALESCE(?, score), updated_at = ?
            WHERE id = ?
        """, (reason, final_score, now, setup_id))


def mark_pending_setup_expired(setup_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired', updated_at = ?
            WHERE id = ?
        """, (now, setup_id))


def expire_old_pending_setups(now: datetime) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired', updated_at = ?
            WHERE status IN ('pending_pullback', 'pending_breakout') AND expires_at <= ?
        """, (now.isoformat(), now.isoformat()))


def count_pending_setups() -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM pending_setups WHERE status IN ('pending_pullback', 'pending_breakout')"
        ).fetchone()
        return row[0]
```

- [ ] **Step 3: Write a smoke test for the new table**

Create `tests/test_pending_setups_db.py`:

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


def _setup_dict(symbol="XRP_USDT", direction="LONG"):
    now = datetime.now(timezone.utc)
    return {
        "symbol": symbol, "direction": direction,
        "macro_tf": "4h", "trend_tf": "1h", "pullback_tf": "15m", "entry_tf": "5m",
        "macro_trend": 1, "trend_state": 1,
        "zlema_1h": 100.0, "zlema_15m": 100.5,
        "pullback_price": 100.4, "pullback_time": now.isoformat(),
        "score": 65.0, "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "created_at": now.isoformat(),
    }


def test_save_and_fetch_pending_pullback(temp_db):
    setup_id = db.save_pending_setup(_setup_dict())
    assert setup_id is not None
    assert db.pending_setup_exists("XRP_USDT") is True

    rows = db.get_pending_setups("pending_pullback")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending_pullback"


def test_breakout_transition_then_fire(temp_db):
    setup_id = db.save_pending_setup(_setup_dict())
    db.update_pending_setup_breakout(
        setup_id, confirmation_high=101.0, confirmation_low=100.0,
        confirmation_close=100.9, confirmation_time=datetime.now(timezone.utc).isoformat(),
        trigger_price=101.02,
    )
    rows = db.get_pending_setups("pending_breakout")
    assert len(rows) == 1
    assert rows[0]["trigger_price"] == pytest.approx(101.02)

    db.mark_pending_setup_fired(setup_id, signal_id=42, final_score=88.0)
    assert db.get_pending_setups("pending_breakout") == []
    assert db.pending_setup_exists("XRP_USDT") is False


def test_expire_old_pending_setups(temp_db):
    setup = _setup_dict()
    setup["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.save_pending_setup(setup)
    db.expire_old_pending_setups(datetime.now(timezone.utc))
    assert db.get_pending_setups("pending_pullback") == []
```

- [ ] **Step 4: Run the new test**

Run: `python -m pytest tests/test_pending_setups_db.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_pending_setups_db.py
git commit -m "feat: replace armed_setups with pending_setups table for zero-lag state machine"
```

---

## Task 5: Strategy pipeline — `detect_pending_setup` and scoring, delete legacy functions

**Files:**
- Modify: `strategy.py` (deletes lines 62-91's `calculate_ema`/`calculate_rsi`/`calculate_volume_ma`, keeps `calculate_atr`; deletes lines 94-142 entirely; deletes lines 145-189 `_score_pending_setup`; deletes lines 192-237 `calculate_supertrend`; deletes lines 240-379 `_build_pending_setup`/`detect_pending_setup`; rewrites the bottom import block, lines 421-433)
- Modify: `tests/strategy_fixtures.py` (remove Precision-only builders, add zero-lag builders)
- Test: `tests/test_strategy_zero_lag.py` (new — pullback-stage tests only in this task; breakout-stage tests come in Task 6)

**Interfaces:**
- Consumes: `calculate_zlema`, `calculate_zlema_band`, `calculate_zlema_trend_state` (Task 2); `calculate_atr` (existing, unchanged); config names from Task 3 (`MACRO_TF`, `TREND_TF`, `PULLBACK_TF`, `ENTRY_TF`, `*_KLINE_COUNT`, `ZERO_LAG_*`, `ATR_PERIOD`, `PULLBACK_DISTANCE_PCT`, `PENDING_EXPIRY_CANDLES`, `MIN_SIGNAL_SCORE`, `MIN_CANDLE_SETTLE_SECONDS`, `ENABLE_LONG_SIGNALS`, `CANDLE_MINUTES`, `_TF_MINUTES`).
- Produces: `_pullback_stage_score(direction: str, zlema_trend: pd.Series, distance_pct: float) -> float` (0-70), `detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None` (returns a dict shaped for `database.save_pending_setup`, or `None`). Task 6 adds `_breakout_stage_score` and `check_setup_confirmation` alongside these in the same file. Task 8 (`main.py`) calls `detect_pending_setup`.

- [ ] **Step 1: Delete the Precision-Pullback-only indicator/helper functions**

In `strategy.py`, delete these functions entirely (keep `calculate_atr`, which stays exactly as-is): `calculate_ema`, `calculate_rsi`, `calculate_volume_ma`, `_ema_trend_slope_up`, `_rsi_reset_ok`, `_confirmation_candle_ok`, `_abnormal_candle`, `_atr_pct_ok`, `_score_pending_setup`, `calculate_supertrend`, `_build_pending_setup`, `detect_pending_setup` (the old body — a new one is written in Step 3 below).

- [ ] **Step 2: Write the scoring helper**

In the gap left by the deleted `_score_pending_setup`, add:

```python
def _pullback_stage_score(direction: str, zlema_trend: pd.Series, distance_pct: float) -> float:
    """0-70: 30 flat (4H/1H agreement, already gated -- no pending setup
    exists to score without it) + up to 20 (1H ZLEMA slope strength) + up
    to 20 (15m pullback quality: full marks at half the max pullback
    distance, linear decay to 0 at the full distance)."""
    score = 30.0

    last = float(zlema_trend.iloc[-1])
    prev = (
        float(zlema_trend.iloc[-1 - ZERO_LAG_SLOPE_LOOKBACK])
        if len(zlema_trend) > ZERO_LAG_SLOPE_LOOKBACK else last
    )
    slope_move_pct = abs(last - prev) / last if last else 0.0
    score += 20.0 * min(1.0, slope_move_pct / 0.01)

    half = PULLBACK_DISTANCE_PCT / 2.0
    if distance_pct <= half:
        pullback_score = 1.0
    else:
        span = max(PULLBACK_DISTANCE_PCT - half, 1e-9)
        pullback_score = max(0.0, 1.0 - (distance_pct - half) / span)
    score += 20.0 * pullback_score

    return round(score, 1)
```

- [ ] **Step 3: Write `detect_pending_setup`**

In the gap left by the deleted old `detect_pending_setup`, add:

```python
def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw_macro = get_market_klines(symbol, MACRO_TF, count=MACRO_KLINE_COUNT)
        if raw_macro is None or raw_macro.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_macro = raw_macro.iloc[:-1].copy()

        raw_trend = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        if raw_trend is None or raw_trend.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_trend = raw_trend.iloc[:-1].copy()

        raw_pullback = get_market_klines(symbol, PULLBACK_TF, count=PULLBACK_KLINE_COUNT)
        if raw_pullback is None or raw_pullback.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_pullback = raw_pullback.iloc[:-1].copy()

        min_mtf_history = ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + 10
        min_pullback_history = ZERO_LAG_LENGTH + 10
        if (
            len(closed_macro) < min_mtf_history
            or len(closed_trend) < min_mtf_history
            or len(closed_pullback) < min_pullback_history
        ):
            _bump(reject_sink, "insufficient_history")
            return None

        pullback_tf_minutes = _TF_MINUTES.get(PULLBACK_TF, 15)
        candle_close_time = closed_pullback.index[-1].to_pydatetime() + timedelta(minutes=pullback_tf_minutes)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        zlema_macro = calculate_zlema(closed_macro["close"], ZERO_LAG_LENGTH)
        upper_macro, lower_macro = calculate_zlema_band(
            closed_macro, zlema_macro, ATR_PERIOD, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER,
        )
        macro_state = calculate_zlema_trend_state(closed_macro, zlema_macro, upper_macro, lower_macro)
        macro_trend = int(macro_state.iloc[-1])
        if macro_trend == 0:
            _bump(reject_sink, "no_macro_trend")
            return None

        zlema_trend = calculate_zlema(closed_trend["close"], ZERO_LAG_LENGTH)
        upper_trend, lower_trend = calculate_zlema_band(
            closed_trend, zlema_trend, ATR_PERIOD, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER,
        )
        trend_state_series = calculate_zlema_trend_state(closed_trend, zlema_trend, upper_trend, lower_trend)
        trend_state = int(trend_state_series.iloc[-1])
        if trend_state != macro_trend:
            _bump(reject_sink, "no_trend_agreement")
            return None

        direction = "LONG" if macro_trend == 1 else "SHORT"
        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        zlema_pullback = calculate_zlema(closed_pullback["close"], ZERO_LAG_LENGTH)
        pullback_close = float(closed_pullback["close"].iloc[-1])
        zlema_15m_last = float(zlema_pullback.iloc[-1])

        if direction == "LONG":
            in_pullback = pullback_close <= zlema_15m_last * (1 + PULLBACK_DISTANCE_PCT)
            raw_distance_pct = (pullback_close - zlema_15m_last) / zlema_15m_last
        else:
            in_pullback = pullback_close >= zlema_15m_last * (1 - PULLBACK_DISTANCE_PCT)
            raw_distance_pct = (zlema_15m_last - pullback_close) / zlema_15m_last

        if not in_pullback:
            _bump(reject_sink, "no_pullback")
            return None
        distance_pct = abs(raw_distance_pct)

        partial_score = _pullback_stage_score(direction, zlema_trend, distance_pct)
        if partial_score + 30.0 < MIN_SIGNAL_SCORE:
            # Even a perfect breakout stage (max 30 more points) couldn't
            # clear the bar -- cheap early exit, avoids arming a setup that
            # would only get discarded later at the pending_breakout gate.
            _bump(reject_sink, "score_below_min")
            return None

        now = datetime.now(timezone.utc)
        return {
            "symbol": symbol,
            "direction": direction,
            "macro_tf": MACRO_TF,
            "trend_tf": TREND_TF,
            "pullback_tf": PULLBACK_TF,
            "entry_tf": ENTRY_TF,
            "macro_trend": macro_trend,
            "trend_state": trend_state,
            "zlema_1h": float(zlema_trend.iloc[-1]),
            "zlema_15m": zlema_15m_last,
            "pullback_price": pullback_close,
            "pullback_time": closed_pullback.index[-1].isoformat(),
            "score": partial_score,
            "setup_time": now.isoformat(),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=PENDING_EXPIRY_CANDLES * CANDLE_MINUTES)).isoformat(),
        }
    except Exception as e:
        logger.error("[ZERO-LAG-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
```

- [ ] **Step 4: Rewrite the bottom import block**

Replace the `# ── evaluate_symbol pipeline ──` import block (old lines 423-433) with:

```python
from market_data import get_market_klines
from config import (
    MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF,
    MACRO_KLINE_COUNT, TREND_KLINE_COUNT, PULLBACK_KLINE_COUNT, ENTRY_KLINE_COUNT,
    CANDLE_MINUTES, _TF_MINUTES,
    ZERO_LAG_LENGTH, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER, ZERO_LAG_SLOPE_LOOKBACK,
    ATR_PERIOD, PULLBACK_DISTANCE_PCT, MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS, LEVERAGE, SL_PRICE_PCT, SL_ROI_PCT, TP_PRICE_PCT, TP_ROI_PCT,
    ENABLE_LONG_SIGNALS, ENTRY_BUFFER_PCT, PENDING_EXPIRY_CANDLES,
)
```

(`valid_trade_geometry`, `direction_slot_available`, `_calc_rr`, `_roi_pct`, `_bump` below this import block are untouched — they don't reference any removed config name.)

- [ ] **Step 5: Replace fixture builders in `tests/strategy_fixtures.py`**

Delete `make_trend_df`, `make_15m_trend_df`, `make_pullback_confirmation_df` (Precision-Pullback-only). Keep `patch_klines` and `patch_klines_multi` unchanged (both are generic over interval names, not strategy-specific). Add:

```python
def make_zero_lag_trend_df(
    direction: str = "LONG", bars: int = 320, start_price: float = 100.0, freq: str = "1h",
) -> pd.DataFrame:
    """A steady, noiseless trend on MACRO_TF/TREND_TF -- long enough
    (>= ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + margin) for
    calculate_zlema_trend_state to settle into a single directional state
    well before the end of the series. Ends with one duplicated last row
    so callers can safely iloc[:-1] to drop the 'forming' candle."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq=freq)
    closes = start_price * (1.0 + 0.0015 * sign) ** np.arange(bars)
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_zero_lag_pullback_df(
    direction: str = "LONG", bars: int = 100, start_price: float = 100.0,
) -> pd.DataFrame:
    """PULLBACK_TF (15m) fixture: a steady trend, then a final closed
    candle that pulls back to within a small distance of its own ZLEMA
    (comfortably inside the default PULLBACK_DISTANCE_PCT=0.10% band).
    SHORT mirrors every inequality. Ends with one duplicated last row."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    trend_bars = bars - 1
    closes = start_price * (1.0 + 0.002 * sign) ** np.arange(trend_bars)
    trend_last = closes[-1]
    pullback_close = trend_last * (1 - 0.0004 * sign)
    closes = np.concatenate([closes, [pullback_close]])
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_zero_lag_crossover_df(
    direction: str = "LONG", bars: int = 90, start_price: float = 100.0,
) -> pd.DataFrame:
    """ENTRY_TF (5m) fixture: price sits on the WRONG side of its own
    ZLEMA for most of the series, then the final closed candle crosses
    back with a directional confirmation close (close > open for LONG,
    close < open for SHORT) -- the shape check_setup_confirmation's
    pending_pullback stage looks for. Ends with one duplicated last row."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
    below_bars = bars - 1
    closes = start_price * (1.0 - 0.0008 * sign) ** np.arange(below_bars)
    last_below = closes[-1]
    crossover_close = last_below * (1 + 0.01 * sign)
    closes = np.concatenate([closes, [crossover_close]])
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    opens[-1] = last_below
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])
```

(Constants here are reasoned, not hand-executed against pandas — same disclaimer as the rest of this module: if a test fails because a fixture lands just outside an expected gate, adjust the constant and re-run; that's expected TDD iteration.)

- [ ] **Step 6: Write the pullback-stage tests**

Create `tests/test_strategy_zero_lag.py`:

```python
from tests.strategy_fixtures import (
    make_zero_lag_trend_df, make_zero_lag_pullback_df, patch_klines_multi,
)
from strategy import detect_pending_setup
from config import MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF


def _pipeline_dfs(direction: str) -> dict:
    return {
        MACRO_TF: make_zero_lag_trend_df(direction, bars=320, freq="4h"),
        TREND_TF: make_zero_lag_trend_df(direction, bars=320, freq="1h"),
        PULLBACK_TF: make_zero_lag_pullback_df(direction, bars=100),
        ENTRY_TF: make_zero_lag_trend_df(direction, bars=100, freq="5min"),
    }


def test_pending_pullback_armed_on_long_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("LONG"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "LONG"
    assert setup["macro_trend"] == 1
    assert setup["trend_state"] == 1
    assert setup["score"] >= 30.0


def test_pending_pullback_armed_on_short_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("SHORT"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "SHORT"
    assert setup["macro_trend"] == -1


def test_rejected_when_macro_and_trend_disagree(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[TREND_TF] = make_zero_lag_trend_df("SHORT", bars=320, freq="1h")
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_trend_agreement") == 1


def test_rejected_when_outside_pullback_distance(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    far_pullback = make_zero_lag_pullback_df("LONG", bars=100)
    far_pullback.iloc[-1, far_pullback.columns.get_loc("close")] *= 1.02  # 2% away, well outside 0.10% band
    far_pullback.iloc[-2, far_pullback.columns.get_loc("close")] *= 1.02
    dfs[PULLBACK_TF] = far_pullback
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_pullback") == 1


def test_rejected_when_insufficient_history(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[MACRO_TF] = make_zero_lag_trend_df("LONG", bars=50, freq="4h")  # too short
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("insufficient_history") == 1
```

- [ ] **Step 7: Run the new tests**

Run: `python -m pytest tests/test_strategy_zero_lag.py -v`
Expected: all 5 tests PASS. If `test_rejected_when_outside_pullback_distance` fails because the fixture's default distance was already outside the band even before the `*= 1.02` nudge (making `no_pullback` fire for the "should pass" tests too), or any pullback test fails because the fixture's default distance isn't tight enough — adjust `make_zero_lag_pullback_df`'s `0.0004` pullback-offset constant per Step 5's disclaimer and re-run.

Run also: `python -m pytest tests/test_zero_lag_indicators.py tests/test_pending_setups_db.py -v` to confirm Tasks 2/4 are still green (this task didn't touch either file, but is a good checkpoint before committing).

- [ ] **Step 8: Commit**

```bash
git add strategy.py tests/strategy_fixtures.py tests/test_strategy_zero_lag.py
git commit -m "feat: rewrite detect_pending_setup for Zero-Lag MTF Pullback v1, remove Precision Pullback pipeline"
```

Note: `strategy.py` still has no `check_setup_confirmation` or `build_trade_prices` after this task (deleted in Step 1, not yet rewritten) — `main.py`/`bot.py`/`webui.py`/the backtest script are still broken until Task 6 restores it and Tasks 7-10 catch up. This is expected — same "small commits over a temporarily broken end-to-end state" pattern as Task 3.

---

## Task 6: Strategy pipeline — `check_setup_confirmation` and `build_trade_prices`

**Files:**
- Modify: `strategy.py` (add `_breakout_stage_score`, `check_setup_confirmation`, `build_trade_prices`, near where the old `check_setup_confirmation` used to be)
- Modify: `tests/test_strategy_zero_lag.py` (add breakout-stage tests)

**Interfaces:**
- Consumes: everything from Task 5 (same file), `ENTRY_TF`, `ENTRY_KLINE_COUNT`, `ZERO_LAG_LENGTH`, `ENTRY_BUFFER_PCT`, `PENDING_EXPIRY_CANDLES`, `CANDLE_MINUTES`, `MIN_SIGNAL_SCORE`, `SL_PRICE_PCT`, `TP_PRICE_PCT` (all already imported in Task 5's Step 4 import block).
- Produces: `check_setup_confirmation(setup: dict) -> tuple[str, float | None, dict | None]` — status is one of `"waiting"`, `"expired"`, `"armed_breakout"`, `"missed"`, `"confirmed"`; the third element carries the payload `main.py` needs to persist for `"armed_breakout"` (confirmation/trigger fields) or the final score for `"missed"`/`"confirmed"`. `build_trade_prices(direction: str, entry: float) -> tuple[float, float]` — returns `(tp_price, sl_price)`. **This is a signature change from Precision Pullback's `check_setup_confirmation`, which returned a 2-tuple** — Task 8 (`main.py`) and Task 12 (backtest script) must use the 3-tuple form; no other task calls this function.

- [ ] **Step 1: Write the failing breakout-stage tests**

Append to `tests/test_strategy_zero_lag.py`:

```python
from datetime import datetime, timedelta, timezone

from tests.strategy_fixtures import make_zero_lag_crossover_df, patch_klines
from strategy import check_setup_confirmation, build_trade_prices


def _pending_pullback_setup(direction: str = "LONG") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "symbol": "XRP_USDT", "direction": direction, "status": "pending_pullback",
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "score": 65.0,
    }


def _pending_breakout_setup(direction: str, trigger_price: float, confirmation_high: float,
                             confirmation_low: float, confirmation_close: float,
                             confirmation_time: str, score: float = 65.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 2, "symbol": "XRP_USDT", "direction": direction, "status": "pending_breakout",
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "score": score,
        "trigger_price": trigger_price,
        "confirmation_high": confirmation_high, "confirmation_low": confirmation_low,
        "confirmation_close": confirmation_close, "confirmation_time": confirmation_time,
    }


def test_crossover_plus_confirmation_candle_arms_breakout(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    patch_klines(monkeypatch, strategy, df)

    status, fill_price, extra = check_setup_confirmation(_pending_pullback_setup("LONG"))

    assert status == "armed_breakout"
    assert fill_price is None
    assert extra is not None
    assert extra["trigger_price"] > extra["confirmation_high"]  # LONG buffer is above the confirming high


def test_no_crossover_stays_waiting(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    # Flatten the final candle so it never actually crosses back above zlema.
    last_idx = df.index[-2]
    df.loc[last_idx, "close"] = df.loc[last_idx, "open"] * 0.999
    patch_klines(monkeypatch, strategy, df)

    status, fill_price, extra = check_setup_confirmation(_pending_pullback_setup("LONG"))

    assert status == "waiting"
    assert fill_price is None
    assert extra is None


def test_confirmed_on_trigger_price_breakout(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    closed = df.iloc[:-1]
    confirmation_high = float(closed["high"].iloc[-1])
    confirmation_low = float(closed["low"].iloc[-1])
    confirmation_close = float(closed["close"].iloc[-1])
    confirmation_time = closed.index[-1].isoformat()
    trigger_price = confirmation_high * 1.0002

    breakout_df = df.copy()
    # Next closed candle breaks above the trigger price.
    breakout_row = breakout_df.iloc[[-1]].copy()
    breakout_row.index = [breakout_df.index[-1] + (breakout_df.index[-1] - breakout_df.index[-2])]
    breakout_row["open"] = confirmation_close
    breakout_row["close"] = trigger_price * 1.001
    breakout_row["high"] = trigger_price * 1.002
    breakout_row["low"] = confirmation_close
    breakout_df = pd.concat([breakout_df.iloc[:-1], breakout_row, breakout_row])  # last row duplicated = "forming"

    patch_klines(monkeypatch, strategy, breakout_df)

    setup = _pending_breakout_setup(
        "LONG", trigger_price, confirmation_high, confirmation_low, confirmation_close, confirmation_time,
    )
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "confirmed"
    assert fill_price == pytest.approx(trigger_price)
    assert extra["score"] >= 65.0


def test_setup_expires_from_original_setup_time(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    patch_klines(monkeypatch, strategy, df)

    setup = _pending_pullback_setup("LONG")
    setup["setup_time"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "expired"


def test_build_trade_prices_long():
    tp, sl = build_trade_prices("LONG", entry=100.0)
    assert tp == pytest.approx(100.35, abs=0.01)   # +7% ROI / 20x = +0.35%
    assert sl == pytest.approx(99.5, abs=0.01)      # -10% ROI / 20x = -0.50%


def test_build_trade_prices_short():
    tp, sl = build_trade_prices("SHORT", entry=100.0)
    assert tp == pytest.approx(99.65, abs=0.01)
    assert sl == pytest.approx(100.5, abs=0.01)
```

Add `import pandas as pd` and `import pytest` to the top of `tests/test_strategy_zero_lag.py` if not already present from Task 5.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_zero_lag.py -v`
Expected: the six new tests FAIL with `ImportError: cannot import name 'check_setup_confirmation'` (deleted in Task 5, not yet rewritten).

- [ ] **Step 3: Write `_breakout_stage_score`, `check_setup_confirmation`, `build_trade_prices`**

In `strategy.py`, in the gap where the old `check_setup_confirmation` used to live, add:

```python
def _breakout_stage_score(
    direction: str, confirmation_high: float, confirmation_low: float,
    confirmation_close: float, candles_to_break: int,
) -> float:
    """0-30: up to 20 ('fresh' crossover -- loses 5 points per extra
    candle it took to break the trigger price beyond the first one,
    floored at 0) + up to 10 (confirmation candle's close position within
    its own high-low range -- how cleanly it closed near its high for
    LONG / low for SHORT)."""
    freshness = max(0.0, 20.0 - 5.0 * max(0, candles_to_break - 1))

    candle_range = max(confirmation_high - confirmation_low, 1e-9)
    if direction == "LONG":
        clearance = (confirmation_close - confirmation_low) / candle_range
    else:
        clearance = (confirmation_high - confirmation_close) / candle_range
    quality = 10.0 * min(1.0, max(0.0, clearance))

    return round(freshness + quality, 1)


def check_setup_confirmation(setup: dict) -> tuple[str, float | None, dict | None]:
    symbol = setup["symbol"]
    direction = setup["direction"]
    status = setup["status"]

    setup_time = datetime.fromisoformat(setup["setup_time"])
    if setup_time.tzinfo is None:
        setup_time = setup_time.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - setup_time).total_seconds() / 60.0
    if age_minutes > PENDING_EXPIRY_CANDLES * CANDLE_MINUTES:
        return "expired", None, None

    raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
    if raw is None or raw.empty:
        return "waiting", None, None
    closed = raw.iloc[:-1].copy()
    if len(closed) < ZERO_LAG_LENGTH + 5:
        return "waiting", None, None

    if status == "pending_pullback":
        zlema = calculate_zlema(closed["close"], ZERO_LAG_LENGTH)
        prev_close, curr_close = float(closed["close"].iloc[-2]), float(closed["close"].iloc[-1])
        prev_zlema, curr_zlema = float(zlema.iloc[-2]), float(zlema.iloc[-1])
        curr_open = float(closed["open"].iloc[-1])

        if direction == "LONG":
            crossed = prev_close <= prev_zlema and curr_close > curr_zlema
            candle_ok = curr_close > curr_open
        else:
            crossed = prev_close >= prev_zlema and curr_close < curr_zlema
            candle_ok = curr_close < curr_open

        if not (crossed and candle_ok):
            return "waiting", None, None

        last = closed.iloc[-1]
        confirmation_high, confirmation_low = float(last["high"]), float(last["low"])
        confirmation_close = float(last["close"])
        if direction == "LONG":
            trigger_price = confirmation_high * (1 + ENTRY_BUFFER_PCT)
        else:
            trigger_price = confirmation_low * (1 - ENTRY_BUFFER_PCT)

        return "armed_breakout", None, {
            "confirmation_high": confirmation_high,
            "confirmation_low": confirmation_low,
            "confirmation_close": confirmation_close,
            "confirmation_time": closed.index[-1].isoformat(),
            "trigger_price": trigger_price,
        }

    # status == "pending_breakout"
    last = closed.iloc[-1]
    high, low = float(last["high"]), float(last["low"])
    trigger_price = float(setup["trigger_price"])

    entry_hit = (high > trigger_price) if direction == "LONG" else (low < trigger_price)
    if not entry_hit:
        return "waiting", None, None

    confirmation_time = datetime.fromisoformat(setup["confirmation_time"])
    if confirmation_time.tzinfo is None:
        confirmation_time = confirmation_time.replace(tzinfo=timezone.utc)
    candle_ts = closed.index[-1].to_pydatetime()
    if candle_ts.tzinfo is None:
        candle_ts = candle_ts.replace(tzinfo=timezone.utc)
    candles_to_break = max(1, round((candle_ts - confirmation_time).total_seconds() / 60.0 / CANDLE_MINUTES))

    breakout_score = _breakout_stage_score(
        direction, float(setup["confirmation_high"]), float(setup["confirmation_low"]),
        float(setup["confirmation_close"]), candles_to_break,
    )
    final_score = round(min(100.0, float(setup["score"]) + breakout_score), 1)
    if final_score < MIN_SIGNAL_SCORE:
        return "missed", None, {"score": final_score}

    return "confirmed", trigger_price, {"score": final_score}


def build_trade_prices(direction: str, entry: float) -> tuple[float, float]:
    if direction == "LONG":
        sl = entry * (1 - SL_PRICE_PCT)
        tp = entry * (1 + TP_PRICE_PCT)
    else:
        sl = entry * (1 + SL_PRICE_PCT)
        tp = entry * (1 - TP_PRICE_PCT)
    return round(tp, 8), round(sl, 8)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_zero_lag.py -v`
Expected: all 11 tests (5 from Task 5 + 6 new) PASS. If `test_confirmed_on_trigger_price_breakout` or `test_crossover_plus_confirmation_candle_arms_breakout` fail because the fixture's crossover magnitude (`0.01` in `make_zero_lag_crossover_df`) isn't decisive enough against the ZLEMA computed from a 90-bar-only series, widen that constant per the fixtures module's disclaimer and re-run.

- [ ] **Step 5: Run the full test suite so far**

Run: `python -m pytest tests/test_zero_lag_indicators.py tests/test_pending_setups_db.py tests/test_strategy_zero_lag.py tests/test_correlation_limits.py -v`
Expected: all PASS (Task 5's note about `strategy.py` being import-broken for other modules doesn't affect these files, which only import `strategy`/`database`/`config` directly, not `main`/`bot`/`webui`).

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/test_strategy_zero_lag.py
git commit -m "feat: add check_setup_confirmation breakout stage and build_trade_prices"
```

---

## Task 7: Plain `check_tp_sl` in `outcome_check.py`

**Files:**
- Modify: `outcome_check.py` (replace entirely — the whole file is currently just `check_tp_sl_with_breakeven` plus its docstring)
- Test: `tests/test_outcome_check_plain.py` (new)
- Delete: `tests/test_outcome_check_breakeven.py`

**Interfaces:**
- Produces: `check_tp_sl(direction: str, entry_price: float, sl_price: float, tp_price: float, df: pd.DataFrame, entry_candle_cutoff) -> dict | None`. Returns `None` while open, else `{"status": "win"|"loss", "pnl_roi_pct": float, "closed_at": Timestamp}`. Task 8 (`main.py`) and Task 12 (backtest script) call this.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outcome_check_plain.py`:

```python
import pandas as pd
import pytest

from outcome_check import check_tp_sl


def _candles(rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return df


def test_tp_hit_is_a_win():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),   # entry candle (cutoff)
        (100.0, 100.5, 99.8, 100.3),   # TP=100.35 not yet hit (high=100.5 > tp but check exact)
        (100.3, 101.0, 100.2, 100.9),  # high=101.0 clears TP
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result is not None
    assert result["status"] == "win"
    assert result["pnl_roi_pct"] == pytest.approx(0.35, abs=0.01)


def test_sl_hit_is_a_loss():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.2, 99.4, 99.6),   # low=99.4 breaches SL=99.5
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result is not None
    assert result["status"] == "loss"
    assert result["pnl_roi_pct"] == pytest.approx(-0.50, abs=0.01)


def test_same_candle_sl_beats_tp_tie_break():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 101.0, 99.0, 100.5),   # single wild candle spans both TP and SL
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result["status"] == "loss"


def test_returns_none_while_still_open():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.1, 99.9, 100.0),
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)
    assert result is None


def test_short_direction_mirrors():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.2, 99.6, 99.65),  # low=99.6 clears SHORT TP=99.65? -- use exact clearance
    ])
    df.iloc[1, df.columns.get_loc("low")] = 99.6
    cutoff = df.index[0]
    result = check_tp_sl("SHORT", entry_price=100.0, sl_price=100.5, tp_price=99.65, df=df, entry_candle_cutoff=cutoff)
    assert result["status"] == "win"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_outcome_check_plain.py -v`
Expected: `ImportError: cannot import name 'check_tp_sl'`.

- [ ] **Step 3: Replace `outcome_check.py`**

Replace the entire file with:

```python
"""
Plain single-TP/SL outcome determination for Zero-Lag MTF Pullback v1's
fired signals. No breakeven step in this strategy version (see the design
spec's "Relationship to prior work" section) -- same-candle tie-break, SL
checked before TP, matching the convention used everywhere else in this
bot.
"""

from __future__ import annotations

import pandas as pd


def check_tp_sl(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff. Each candle, in order:
    (1) SL hit -> "loss"; (2) TP hit -> "win" (SL-first tie-break on a
    single wild candle that spans both). Returns None while open, else
    {"status": "win"|"loss", "pnl_roi_pct": float, "closed_at": Timestamp}.
    pnl_roi_pct is the raw price-move percent (not leverage-scaled -- the
    caller applies LEVERAGE).
    """
    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= sl_price) if direction == "LONG" else (high >= sl_price)
        if sl_hit:
            pnl = (
                (sl_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - sl_price) / entry_price * 100.0
            )
            return {"status": "loss", "pnl_roi_pct": round(pnl, 4), "closed_at": ts}

        tp_hit = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        if tp_hit:
            pnl = (
                (tp_price - entry_price) / entry_price * 100.0 if direction == "LONG"
                else (entry_price - tp_price) / entry_price * 100.0
            )
            return {"status": "win", "pnl_roi_pct": round(pnl, 4), "closed_at": ts}

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_outcome_check_plain.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Delete the old breakeven test file**

```bash
git rm tests/test_outcome_check_breakeven.py
```

- [ ] **Step 6: Commit**

```bash
git add outcome_check.py tests/test_outcome_check_plain.py
git commit -m "feat: replace breakeven outcome walker with plain check_tp_sl"
```

---

## Task 8: `main.py` — split scheduler into `scan_for_new_setups` + `monitor_pending_setups`

**Files:**
- Modify: `main.py` (replace `scan_and_fire_signals` (lines 111-280) with two functions; update `check_outcomes` (lines 285-346); update the import block (lines 38-71); update scheduler wiring in `main()` (lines 403-418, 441-444))

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation` (3-tuple), `strategy.build_trade_prices`, `strategy.valid_trade_geometry`, `strategy.direction_slot_available`, `strategy._roi_pct`, `strategy.Signal` (Task 5/6); `db.pending_setup_exists`, `db.get_pending_setups`, `db.save_pending_setup`, `db.update_pending_setup_breakout`, `db.mark_pending_setup_fired`, `db.mark_pending_setup_missed`, `db.mark_pending_setup_expired`, `db.expire_old_pending_setups` (Task 4); `outcome_check.check_tp_sl` (Task 7); `config.MONITOR_INTERVAL_MINUTES`, `config.SL_ROI_PCT`, `config.TP_ROI_PCT` and the rest of Task 3's new names.
- Produces: no new public interface (this is the top-level entry point) — but note for whoever runs the bot: two scheduler job ids now exist, `"setup_scanner"` (5m) and `"setup_monitor"` (1m), replacing the old single `"signal_scanner"`.

This task has no unit test of its own (there's no existing test for `main.py`'s scheduler wiring or its async job bodies — they're integration-tested by the `DRY_RUN=true python main.py` boot check in Task 13). Correctness here is verified by the full test suite (which exercises `strategy`/`database`/`outcome_check` directly) plus that manual boot check.

- [ ] **Step 1: Replace the import block**

Replace `main.py`'s `from config import (...)` block (lines 38-71) with:

```python
from config import (
    LKT,
    LEVERAGE,
    MACRO_TF,
    TREND_TF,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCAN_INTERVAL_MINUTES,
    MONITOR_INTERVAL_MINUTES,
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
    MIN_CANDLE_SETTLE_SECONDS,
    TP_ROI_PCT,
    SL_ROI_PCT,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
)
```

Also change the line `from outcome_check import check_tp_sl_with_breakeven` to `from outcome_check import check_tp_sl`.

- [ ] **Step 2: Replace `scan_and_fire_signals` with `scan_for_new_setups`**

Replace the entire `scan_and_fire_signals` function (lines 111-280, everything from `async def scan_and_fire_signals` up to the blank line before `# ── Outcome checker ──`) with:

```python
# ── Setup scanner (5m) ────────────────────────────────────────────

async def scan_for_new_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_pending_setups(now)

    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    to_scan = [
        s for s in coins
        if not db.pending_setup_exists(s) and not db.signal_exists_for_coin(s, cooldown_since)
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
            db.save_pending_setup(setup)
            logger.info(
                "[PENDING] Armed pullback %s %s zlema_15m=%.6g score=%.1f",
                setup["symbol"], setup["direction"], setup["zlema_15m"], setup["score"],
            )
        except Exception as e:
            logger.error("[SCAN] Failed to arm setup for %s: %s", setup["symbol"], e, exc_info=True)

    logger.info(
        "[SCAN] Done — %d/%d coins scanned, %d new pullback setups | rejects: %s",
        len(to_scan), len(coins), len(new_setups), reject_summary,
    )


# ── Setup monitor (1m) ────────────────────────────────────────────

async def monitor_pending_setups(app: Application) -> None:
    if tg.paused:
        return

    now = datetime.now(timezone.utc)
    db.expire_old_pending_setups(now)

    setups = db.get_pending_setups("pending_pullback") + db.get_pending_setups("pending_breakout")
    if not setups:
        return

    active_signals = db.count_active_signals()
    active_long = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig = db.latest_signal_time()

    for setup in setups:
        status, fill_price, extra = strategy.check_setup_confirmation(setup)

        if status == "expired":
            db.mark_pending_setup_expired(setup["id"])
            continue
        if status == "waiting":
            continue
        if status == "armed_breakout":
            db.update_pending_setup_breakout(setup["id"], **extra)
            continue
        if status == "missed":
            db.mark_pending_setup_missed(setup["id"], reason="score_below_min", final_score=extra["score"])
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
            db.mark_pending_setup_missed(setup["id"], reason="budget_or_slot_unavailable", final_score=extra["score"])
            continue

        tp_price, sl_price = strategy.build_trade_prices(setup["direction"], fill_price)
        if not strategy.valid_trade_geometry(setup["direction"], fill_price, tp_price, sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                setup["symbol"], setup["direction"], fill_price, tp_price, sl_price,
            )
            db.mark_pending_setup_missed(setup["id"], reason="invalid_geometry_at_confirm", final_score=extra["score"])
            continue

        tp_roi, sl_roi = strategy._roi_pct(setup["direction"], fill_price, tp_price, sl_price)
        macro_label = "Bullish" if setup["macro_trend"] == 1 else "Bearish"
        sig = strategy.Signal(
            symbol=setup["symbol"],
            direction=setup["direction"],
            entry_price=fill_price,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary=f"4H:{macro_label} 1H:Agree 15m:Pullback 5m:Recovery",
            generated_at=now,
            rr=round(TP_ROI_PCT / SL_ROI_PCT, 2),
            score=extra["score"],
            entry_low=fill_price,
            entry_high=fill_price,
        )

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would confirm | %s %s @ %.6g TP=%.6g SL=%.6g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            db.mark_pending_setup_fired(setup["id"], signal_id=-1, final_score=extra["score"])
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
            db.mark_pending_setup_fired(setup["id"], signal_id, final_score=extra["score"])

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
                "[SIGNAL] Confirmed #%d %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.sl_price,
            )
        except Exception as e:
            logger.error("[MONITOR] Failed to confirm setup for %s: %s", setup["symbol"], e, exc_info=True)
```

- [ ] **Step 3: Update `check_outcomes` for the plain walker**

In `check_outcomes` (lines 285-346), remove the breakeven-trigger-price calculation and the `check_tp_sl_with_breakeven` call. Replace this block:

```python
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
```

with:

```python
        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        result = check_tp_sl(direction, entry_price, sl_price, tp_price, df, entry_candle_cutoff)
        if result is None:
            continue

        pnl = result["pnl_roi_pct"] * LEVERAGE

        db.update_signal_outcome(sig["id"], result["status"], pnl)
```

- [ ] **Step 4: Update the scheduler wiring in `main()`**

Replace the single `scheduler.add_job(scan_and_fire_signals, ...)` block (lines 403-411) with two jobs:

```python
    scheduler.add_job(
        scan_for_new_setups,
        CronTrigger(
            minute=f"{_settle_offset_minute}-59/{SCAN_INTERVAL_MINUTES}",
            second=_settle_offset_second,
        ),
        args=[app],
        id="setup_scanner",
    )

    scheduler.add_job(
        monitor_pending_setups,
        IntervalTrigger(minutes=MONITOR_INTERVAL_MINUTES),
        args=[app],
        id="setup_monitor",
    )
```

(The `_settle_offset_minute`/`_settle_offset_second` computation immediately above this block, and its `RuntimeError` guard, are unchanged — still gates `scan_for_new_setups`'s cron offset against `MIN_CANDLE_SETTLE_SECONDS`/`SCAN_INTERVAL_MINUTES`, per this spec's "Scheduler" section explaining why `monitor_pending_setups` doesn't need the same treatment.)

Update the log line after `scheduler.start()` (lines 441-444):

```python
    logger.info(
        "Scheduler started — scan every %dm, monitor every %dm, outcome every %dm",
        SCAN_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES,
    )
```

- [ ] **Step 5: Update the startup log lines in `main()`**

Replace the `logger.info("Trend TF: %s  Entry TF: %s", ...)` and TP/SL/breakeven log lines near the top of `main()` with:

```python
    logger.info("Macro/Trend/Pullback/Entry TF: %s / %s / %s / %s", MACRO_TF, TREND_TF, "15m", ENTRY_TF)
    logger.info("Min signal score: %.0f", MIN_SIGNAL_SCORE)
    logger.info("TP: +%.1f%% ROI  SL: -%.1f%% ROI  (no breakeven)", TP_ROI_PCT, SL_ROI_PCT)
```

(`"15m"` is hardcoded for `PULLBACK_TF` in this log line rather than imported, since `PULLBACK_TF` isn't otherwise needed in `main.py` — import it alongside `MACRO_TF`/`TREND_TF`/`ENTRY_TF` in Step 1 instead if you'd rather avoid the literal; either is fine, this plan uses the literal to keep the Step 1 import list shorter.)

- [ ] **Step 6: Verify the module imports cleanly**

Run: `python -c "import main"`
Expected: no output, exit code 0. (This confirms every name Step 1's import list references actually exists in `config.py` after Task 3, and that `strategy`/`database`/`outcome_check` expose everything `main.py` now calls.)

- [ ] **Step 7: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS except the ones this task doesn't yet fix — `tests/test_indicators.py` (still imports deleted `calculate_ema`/`calculate_rsi`/`calculate_supertrend`, fixed in Task 11), `tests/test_precision_pullback_indicators.py`, `tests/test_strategy_precision_pullback.py`, `tests/test_database_breakeven_status.py` (all deleted in Task 11), `tests/test_bot_formatting.py`, `tests/test_webui_stats.py` (fixed in Tasks 9/10). Confirm the failure list matches exactly this set — anything else failing is a regression introduced by this task.

- [ ] **Step 8: Commit**

```bash
git add main.py
git commit -m "feat: split scheduler into scan_for_new_setups (5m) + monitor_pending_setups (1m)"
```

---

## Task 9: `bot.py` — `cmd_status` and `notify_outcome`

**Files:**
- Modify: `bot.py:88-111` (`notify_outcome`), `bot.py:153-219` (`cmd_status`)
- Modify: `tests/test_bot_formatting.py`

**Interfaces:**
- Consumes: `config.MACRO_TF`, `config.TREND_TF`, `config.PULLBACK_TF`, `config.ENTRY_TF`, `config.ZERO_LAG_LENGTH`, `config.ZERO_LAG_MULTIPLIER`, `config.PULLBACK_DISTANCE_PCT`, `config.PENDING_EXPIRY_CANDLES`, `config.TP_ROI_PCT`, `config.SL_ROI_PCT` (Task 3).
- Produces: no new public interface — `format_signal`/`broadcast_signal` are unchanged (already generic over `Signal`'s fields).

- [ ] **Step 1: Update `notify_outcome` — remove the breakeven branch**

Replace:

```python
    if status == "win":
        emoji, label = "✅", f"TARGET HIT {roi:+.1f}%"
    elif status == "breakeven":
        emoji, label = "⚖️", f"BREAKEVEN STOP {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOPPED OUT {roi:+.1f}%"
    else:
        emoji, label = "💤", "EXPIRED"
```

with:

```python
    if status == "win":
        emoji, label = "✅", f"TARGET HIT {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOPPED OUT {roi:+.1f}%"
    else:
        emoji, label = "💤", "EXPIRED"
```

- [ ] **Step 2: Update `cmd_status`'s config import and message body**

Replace the `from config import (...)` block inside `cmd_status` (lines 156-172) with:

```python
    from config import (
        STRATEGY_NAME,
        MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF,
        MIN_SIGNAL_SCORE,
        TP_ROI_PCT, SL_ROI_PCT,
        ZERO_LAG_LENGTH, ZERO_LAG_MULTIPLIER,
        PULLBACK_DISTANCE_PCT,
        PENDING_EXPIRY_CANDLES,
        SCAN_INTERVAL_MINUTES,
        MONITOR_INTERVAL_MINUTES,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS,
        SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )
```

Replace the message body lines that reference removed config (`NO_CHASE_MAX_DISTANCE_PCT`, `ATR_MIN_PCT`/`MAX_PCT`, `BREAKEVEN_TRIGGER_ROI_PCT`, `PENDING_SIGNAL_EXPIRY_CANDLES`):

```python
        f"TF:          {_code(f'{MACRO_TF}/{TREND_TF}/{PULLBACK_TF}/{ENTRY_TF}')}",
        f"Min score:   {_code(f'{MIN_SIGNAL_SCORE:.0f}/100')}",
        f"Zero-lag:    {_code(f'len={ZERO_LAG_LENGTH} mult={ZERO_LAG_MULTIPLIER}')}  {_italic(f'pullback {PULLBACK_DISTANCE_PCT*100:.2f}%')}",
        f"TP / SL:     {_code(f'+{TP_ROI_PCT:.1f}% / -{SL_ROI_PCT:.1f}% ROI')}",
        f"Pending exp: {_code(f'{PENDING_EXPIRY_CANDLES} candles')}",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan every:  {_code(f'{SCAN_INTERVAL_MINUTES}min')}",
        f"Monitor:     {_code(f'every {MONITOR_INTERVAL_MINUTES} min')}",
        f"Outcome chk: {_code(f'every {OUTCOME_CHECK_MINUTES} min')}",
```

(This replaces the old `f"TF: ..."`, `f"Min score: ..."`, `f"No-chase: ..."`, `f"TP / SL: ..."`, `f"Pending exp: ..."`, `f"Leverage: ..."` lines, and adds the new `f"Monitor: ..."` line right after `f"Scan every: ..."`. Every other line in the message body — daily cap, active signals, pool size, etc. — is unchanged.)

- [ ] **Step 3: Update `tests/test_bot_formatting.py`**

Replace `timeframe_summary="Precision Pullback confirmation"` with `timeframe_summary="4H:Bullish 1H:Agree 15m:Pullback 5m:Recovery"` in `_sample_signal()`. Replace both occurrences of `"Precision Pullback Scalper v1"` (the `monkeypatch.setattr` value and the assertion string) with `"Zero-Lag MTF Pullback v1"` in `test_format_signal_contains_key_fields`.

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_bot_formatting.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py tests/test_bot_formatting.py
git commit -m "feat: update Telegram bot status/outcome display for Zero-Lag MTF Pullback v1"
```

---

## Task 10: `webui.py` — pending setups, strategy config, dashboard JS

**Files:**
- Modify: `webui.py:222-258` (`get_strategy_config`, `get_pending_setups`), `webui.py:994-1003` (`renderConfig()` JS)
- Modify: `tests/test_webui_stats.py`

**Interfaces:**
- Consumes: `db.get_pending_setups(status: str, limit: int = 200) -> list[dict]` (Task 4), `config.MACRO_TF`/`TREND_TF`/`PULLBACK_TF`/`ENTRY_TF`/`ZERO_LAG_LENGTH`/`ZERO_LAG_MULTIPLIER`/`PULLBACK_DISTANCE_PCT`/`PENDING_EXPIRY_CANDLES` (Task 3).
- Produces: no new public interface — `build_payload()`'s shape gains new `config` keys and its `pending_setups` values now come from the new table; `get_stats`/`_stats` are untouched (already generic).

- [ ] **Step 1: Update `get_strategy_config`**

Replace the function body (lines 224-253) with:

```python
def get_strategy_config() -> dict:
    """Return dashboard-safe strategy/runtime configuration for Zero-Lag MTF Pullback v1."""
    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Zero-Lag MTF Pullback v1"),
        "macro_tf": _safe_config_value("MACRO_TF", "—"),
        "trend_tf": _safe_config_value("TREND_TF", "—"),
        "pullback_tf": _safe_config_value("PULLBACK_TF", "—"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "min_signal_score": _safe_config_value("MIN_SIGNAL_SCORE", "—"),
        "tp_roi_pct": _safe_config_value("TP_ROI_PCT", "—"),
        "sl_roi_pct": _safe_config_value("SL_ROI_PCT", "—"),
        "zero_lag_length": _safe_config_value("ZERO_LAG_LENGTH", "—"),
        "zero_lag_multiplier": _safe_config_value("ZERO_LAG_MULTIPLIER", "—"),
        "pullback_distance_pct": _safe_config_value("PULLBACK_DISTANCE_PCT", "—"),
        "pending_expiry_candles": _safe_config_value("PENDING_EXPIRY_CANDLES", "—"),

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


def get_pending_setups() -> list[dict]:
    import database as db
    return db.get_pending_setups("pending_pullback", limit=50) + db.get_pending_setups("pending_breakout", limit=50)
```

- [ ] **Step 2: Update `renderConfig()` JS**

Replace:

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

with:

```javascript
function renderConfig() {
  const c = data.config;

  set("cfg-tf", `${c.macro_tf}/${c.trend_tf}/${c.pullback_tf}/${c.entry_tf}`);
  set("cfg-quality", `score ≥ ${c.min_signal_score}`);
  set("cfg-confirm", `len=${c.zero_lag_length} mult=${c.zero_lag_multiplier}`);
  set("cfg-confirm-sub", `pullback ${(c.pullback_distance_pct * 100).toFixed(2)}%, expires after ${c.pending_expiry_candles} candles`);
  set("cfg-rr", `+${c.tp_roi_pct}% / -${c.sl_roi_pct}%`);
  set("cfg-rr-sub", `${c.leverage}x  no breakeven`);
}
```

- [ ] **Step 3: Update `tests/test_webui_stats.py`**

Replace `test_get_strategy_config_reports_precision_pullback_keys` with:

```python
def test_get_strategy_config_reports_zero_lag_keys():
    cfg = webui.get_strategy_config()
    assert "min_signal_score" in cfg
    assert "tp_roi_pct" in cfg
    assert "sl_roi_pct" in cfg
    assert "zero_lag_length" in cfg
    assert "pullback_distance_pct" in cfg
    assert "no_chase_max_distance_pct" not in cfg
    assert "breakeven_trigger_roi_pct" not in cfg
    assert "signal_mode" not in cfg
```

(`test_get_stats_reports_breakevens_and_excludes_from_win_rate` and its `temp_db` fixture are unchanged — `get_stats`/`_stats` still read the generic `signals` table and still correctly report `0` breakevens for a strategy that never writes that status; the existing fixture's synthetic `breakeven` row is still valid test data for that generic code path.)

- [ ] **Step 4: Run the test**

Run: `python -m pytest tests/test_webui_stats.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Verify the module imports cleanly**

Run: `python -c "import webui"`
Expected: no output, exit code 0.

- [ ] **Step 6: Commit**

```bash
git add webui.py tests/test_webui_stats.py
git commit -m "feat: update dashboard config/pending-setups for Zero-Lag MTF Pullback v1"
```

---

## Task 11: Legacy test cleanup

**Files:**
- Delete: `tests/test_precision_pullback_indicators.py`, `tests/test_strategy_precision_pullback.py`, `tests/test_database_breakeven_status.py`
- Modify: `tests/test_indicators.py` (trim to ATR-only)
- Modify: `tests/test_correlation_limits.py` (stop depending on the old `MAX_ACTIVE_*_SIGNALS=1` default)

**Interfaces:** none new — this task only removes dead references and updates two tests' fixtures/expectations to match Task 3's config changes.

`tests/test_outcome_check_breakeven.py` was already deleted in Task 7 — not repeated here.

- [ ] **Step 1: Delete the three obsolete test files**

```bash
git rm tests/test_precision_pullback_indicators.py tests/test_strategy_precision_pullback.py tests/test_database_breakeven_status.py
```

(All three test functions/behavior that no longer exist: the first two cover `calculate_ema`/`calculate_rsi`/`_rsi_reset_ok`/etc. and the old `detect_pending_setup`/`check_setup_confirmation` pipeline, both fully replaced in Tasks 5-6; the third covers a `signals.status = "breakeven"` write path nothing produces anymore, per Task 7.)

- [ ] **Step 2: Trim `tests/test_indicators.py` to ATR only**

Replace the entire file with:

```python
import numpy as np
import pandas as pd
import pytest

from strategy import calculate_atr


def test_atr_values():
    df = pd.DataFrame({
        "open":  [100.0, 101.0, 99.0, 102.0],
        "high":  [101.5, 102.0, 101.0, 103.0],
        "low":   [99.5, 100.0, 98.0, 101.0],
        "close": [101.0, 99.0, 102.0, 102.5],
    })
    atr = calculate_atr(df, 3)
    assert not np.isnan(atr.iloc[-1])
    assert atr.iloc[-1] > 0.0
```

(`calculate_ema`, `calculate_rsi`, and `calculate_supertrend` — the other three functions this file used to cover — are deleted from `strategy.py` in Task 5; `calculate_zlema`/`calculate_zlema_band`/`calculate_zlema_trend_state`, their replacements in spirit, already have their own dedicated `tests/test_zero_lag_indicators.py` from Task 2, so nothing here needs to move rather than delete.)

- [ ] **Step 3: Fix `tests/test_correlation_limits.py`'s hardcoded default assumption**

This file calls `direction_slot_available("LONG", active_long=1, active_short=0)` and asserts `False`, relying on the *default* `MAX_ACTIVE_LONG_SIGNALS` being `1` — Task 3 changed that default to `2`, which would silently flip this assertion to `True` and break the test without an import error to flag it. Replace the entire file with a version that pins the config values it depends on explicitly, rather than relying on whatever `config.py` currently defaults to:

```python
import config
from strategy import direction_slot_available


def test_second_active_long_is_blocked_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_LONG_SIGNALS", 1)
    assert direction_slot_available("LONG", active_long=0, active_short=0) is True
    assert direction_slot_available("LONG", active_long=1, active_short=0) is False


def test_second_active_short_is_blocked_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SHORT_SIGNALS", 1)
    assert direction_slot_available("SHORT", active_long=0, active_short=0) is True
    assert direction_slot_available("SHORT", active_long=0, active_short=1) is False


def test_long_and_short_can_coexist():
    assert direction_slot_available("LONG", active_long=0, active_short=1) is True
    assert direction_slot_available("SHORT", active_long=1, active_short=0) is True


def test_current_default_allows_two_active_per_direction():
    # Zero-Lag MTF Pullback v1's higher signal-frequency target raised this
    # cap from Precision Pullback's 1 -- pin the expectation to the actual
    # current default so a future config change fails loudly here instead
    # of silently changing correlation-limit behavior.
    assert config.MAX_ACTIVE_LONG_SIGNALS == 2
    assert config.MAX_ACTIVE_SHORT_SIGNALS == 2
    assert direction_slot_available("LONG", active_long=1, active_short=0) is True
    assert direction_slot_available("LONG", active_long=2, active_short=0) is False
```

Note: `direction_slot_available` reads `MAX_ACTIVE_LONG_SIGNALS`/`MAX_ACTIVE_SHORT_SIGNALS` via a local `from config import ...` **inside the function body** (see `strategy.py`'s existing implementation) — `monkeypatch.setattr(config, ...)` works here because that import re-reads the module attribute at call time, not at module-load time.

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS — this is the first point in the plan where the entire suite should be green. If anything outside this task's files still fails, stop and check whether an earlier task's step was skipped or misapplied before proceeding.

- [ ] **Step 5: Commit**

```bash
git add -u tests/
git commit -m "test: remove Precision Pullback legacy tests, fix correlation-limit default assumption"
```

---

## Task 12: Rewrite the backtest harness

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (full rewrite)

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation` (3-tuple), `strategy.build_trade_prices`, `outcome_check.check_tp_sl` (Tasks 5-7); `config.MACRO_TF`/`TREND_TF`/`PULLBACK_TF`/`ENTRY_TF`, `*_KLINE_COUNT`, `ZERO_LAG_LENGTH`, `ZERO_LAG_BAND_LOOKBACK`, `TP_ROI_PCT`, `SL_ROI_PCT`, `LEVERAGE`, `_TF_MINUTES` (Task 3).
- Produces: no interface other module consumes — this is a standalone CLI script. Not run against real 6-month data in this task (per the spec's explicit scope boundary) — this task's deliverable is "imports cleanly and produces sane output against a short local run," not a production backtest.

This task has no dedicated pytest file — `scripts/backtest_simple_strategy.py` has never had one (verified: not present in `tests/`), consistent with it being a manually-invoked CLI tool. Correctness is verified by Step 3's `py_compile` + a short live smoke run against one real symbol.

- [ ] **Step 1: Replace the entire file**

```python
"""
Backtest utility for Zero-Lag MTF Pullback v1.

Three-state simulation, mirroring the live pipeline exactly: a
pending_pullback setup (from strategy.detect_pending_setup, as-of each
bar) advances to pending_breakout (5m ZLEMA crossover + confirmation
candle, via strategy.check_setup_confirmation) then to a fired trade
(price breaks the trigger level) -- the exact same functions the live bot
uses, so backtest and live share one source of truth and no signal logic
is duplicated here.

Needs all four timeframes' historical data per symbol -- fetch with
backtest/fetch_data.py first (arbitrary --interval supported).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy
from outcome_check import check_tp_sl
from mexc_client import get_klines
from config import (
    MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF, _TF_MINUTES,
    MACRO_KLINE_COUNT, TREND_KLINE_COUNT, PULLBACK_KLINE_COUNT, ENTRY_KLINE_COUNT,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    ZERO_LAG_LENGTH, ZERO_LAG_BAND_LOOKBACK, LEVERAGE, TP_ROI_PCT, SL_ROI_PCT,
)

MAX_REST_COUNT = 2000   # single-request ceiling this script asks MEXC for


class _SimulatedDatetime(datetime):
    """Stand-in for strategy.datetime during backtesting. check_setup_confirmation
    computes age_minutes = (datetime.now(timezone.utc) - setup_time) --
    with a historical setup_time and the REAL wall clock, that's always
    enormous, so every pending setup would be marked expired after just
    one simulated bar instead of PENDING_EXPIRY_CANDLES. Overriding only
    now() to return the current as-of bar's timestamp fixes this;
    fromisoformat/utcnow/etc. are inherited unchanged (utcnow() staying on
    the real wall clock is fine -- detect_pending_setup's settle-age check
    just needs 'not suspiciously fresh', which real-now vs.
    historical-candle-time always satisfies)."""
    _sim_now: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        if cls._sim_now is None:
            return super().now(tz)
        return cls._sim_now if tz is None else cls._sim_now.astimezone(tz)


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
    outcome: str            # "win" | "loss" | "expired"
    gross_roi_pct: float
    net_roi_pct: float
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


def _as_of_higher_tf(
    df_full: pd.DataFrame, ts, entry_tf_minutes: int, tf_minutes: int, window_count: int,
) -> pd.DataFrame:
    """As-of view of a higher timeframe, filtered by CLOSE time relative to
    the entry bar's own close time (ts + entry_tf_minutes) -- a candle on
    df_full is only 'closed' by then if its own open + tf_minutes <= that
    close time. Filtering by open time (`index <= ts`) would leak a
    still-forming higher-timeframe candle's real historical close/high/low
    into the strategy on most bars -- this is the exact lookahead bug
    fixed in commit 7296b5c for the two-timeframe case, generalized here
    to three higher timeframes."""
    cutoff = ts + timedelta(minutes=entry_tf_minutes) - timedelta(minutes=tf_minutes)
    as_of = df_full[df_full.index <= cutoff]
    if as_of.empty:
        return as_of
    return _with_forming_row(as_of, len(as_of) - 1, window_count)


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state. One setup/trade at a time."""
    trades: list[Trade] = []

    df_entry_full = get_klines_extended(symbol, ENTRY_TF, days)
    df_pullback_full = get_klines_extended(symbol, PULLBACK_TF, days)
    df_trend_full = get_klines_extended(symbol, TREND_TF, days)
    df_macro_full = get_klines_extended(symbol, MACRO_TF, days)

    if any(d.empty for d in (df_entry_full, df_pullback_full, df_trend_full, df_macro_full)):
        print(f"[{symbol}] no candle history returned for one or more timeframes -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_entry_full)}x{ENTRY_TF}, {len(df_pullback_full)}x{PULLBACK_TF}, "
        f"{len(df_trend_full)}x{TREND_TF}, {len(df_macro_full)}x{MACRO_TF} bars", flush=True,
    )

    min_start = ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + 10

    original_get_market_klines = strategy.get_market_klines
    original_datetime = strategy.datetime
    entry_tf_minutes = _TF_MINUTES.get(ENTRY_TF, 5)
    pullback_tf_minutes = _TF_MINUTES.get(PULLBACK_TF, 15)
    trend_tf_minutes = _TF_MINUTES.get(TREND_TF, 60)
    macro_tf_minutes = _TF_MINUTES.get(MACRO_TF, 240)

    pending_setup: dict | None = None
    in_trade_until_idx = -1

    try:
        for i in range(min_start, len(df_entry_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of_entry = _with_forming_row(df_entry_full, i, ENTRY_KLINE_COUNT)
            ts = df_entry_full.index[i]

            as_of_pullback = _as_of_higher_tf(df_pullback_full, ts, entry_tf_minutes, pullback_tf_minutes, PULLBACK_KLINE_COUNT)
            as_of_trend = _as_of_higher_tf(df_trend_full, ts, entry_tf_minutes, trend_tf_minutes, TREND_KLINE_COUNT)
            as_of_macro = _as_of_higher_tf(df_macro_full, ts, entry_tf_minutes, macro_tf_minutes, MACRO_KLINE_COUNT)

            if as_of_pullback.empty or as_of_trend.empty or as_of_macro.empty:
                continue

            def _fake(sym, interval, count=100,
                      _entry=as_of_entry, _pullback=as_of_pullback, _trend=as_of_trend, _macro=as_of_macro):
                if interval == ENTRY_TF:
                    return _entry
                if interval == PULLBACK_TF:
                    return _pullback
                if interval == TREND_TF:
                    return _trend
                if interval == MACRO_TF:
                    return _macro
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            bar_now = ts.to_pydatetime()
            if bar_now.tzinfo is None:
                bar_now = bar_now.replace(tzinfo=timezone.utc)
            _SimulatedDatetime._sim_now = bar_now
            strategy.datetime = _SimulatedDatetime

            if pending_setup is not None:
                status, fill_price, extra = strategy.check_setup_confirmation(pending_setup)

                if status in ("expired", "missed"):
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue
                if status == "armed_breakout":
                    pending_setup.update(extra)
                    pending_setup["status"] = "pending_breakout"
                    continue

                # confirmed
                direction = pending_setup["direction"]
                tp_price, sl_price = strategy.build_trade_prices(direction, fill_price)
                entry_candle_cutoff = df_entry_full.index[i]

                result = check_tp_sl(direction, fill_price, sl_price, tp_price, df_entry_full, entry_candle_cutoff)
                bars_held = 1
                if result is None:
                    outcome = "expired"
                    gross_roi_pct = 0.0
                    closed_at_str = str(df_entry_full.index[i])
                else:
                    outcome = result["status"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    closed_idx = df_entry_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=direction, entry_price=fill_price,
                    tp_price=tp_price, sl_price=sl_price,
                    rr=round(TP_ROI_PCT / SL_ROI_PCT, 2), outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    closed_at=closed_at_str,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["setup_time"] = df_entry_full.index[i].isoformat()
                setup["status"] = "pending_pullback"
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines
        strategy.datetime = original_datetime

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Zero-Lag MTF Pullback v1")
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

- [ ] **Step 2: Verify it compiles and imports**

Run: `python -m py_compile scripts/backtest_simple_strategy.py`
Expected: no output, exit code 0.

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import backtest_simple_strategy"`
Expected: no output, exit code 0.

- [ ] **Step 3: Smoke-run against a short real window (optional but recommended if network access to MEXC is available)**

Run: `python scripts/backtest_simple_strategy.py --symbols BTC_USDT --days 3 --workers 1`
Expected: prints achieved history counts for all four timeframes, then a report (even "Total trades: 0" is a valid, sane result for a 3-day window — the point of this smoke run is confirming no exception, not finding trades). If this environment has no network access to MEXC, skip this step and note it as unverified in the final commit message.

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "feat: rewrite backtest harness for Zero-Lag MTF Pullback v1's four-timeframe pipeline"
```

---

## Task 13: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS, zero failures, zero errors.

- [ ] **Step 2: Import check**

Run: `python -c "import config; import strategy; import main; import bot; import webui; import database; import outcome_check"`
Expected: no output, exit code 0.

- [ ] **Step 3: Compile check**

Run: `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py outcome_check.py scripts/backtest_simple_strategy.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Dry-run boot check**

Run: `DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py` (Windows: set `$env:DRY_RUN='true'; $env:DRY_RUN_SAVE_SIGNALS='false'; python main.py` in PowerShell, or the bash tool's `export` form), let it run for ~15-20 seconds, then stop it (Ctrl+C).

Expected startup log lines confirm:
- `Strategy: Zero-Lag MTF Pullback v1`
- `Macro/Trend/Pullback/Entry TF: 4h / 1h / 15m / 5m`
- `Min signal score: 80`
- `TP: +7.0% ROI  SL: -10.0% ROI  (no breakeven)`
- `Leverage: 20x`
- `Dry run: enabled`
- `Scheduler started — scan every 5m, monitor every 1m, outcome every 1m`
- No traceback, no `RuntimeError` from the settle-offset guard, no `ImportError`.

- [ ] **Step 5: Grep for leftover references to removed names**

Run: `grep -rn "calculate_ema\b\|calculate_rsi\b\|calculate_supertrend\|armed_setups\|check_tp_sl_with_breakeven\|MAX_SL_ROI_PCT\|BREAKEVEN_TRIGGER\|NO_CHASE_MAX_DISTANCE_PCT\|PULLBACK_PREFERRED_DISTANCE_PCT\|PENDING_SIGNAL_EXPIRY_CANDLES" --include=*.py .`
Expected: no matches outside `docs/superpowers/specs/` and `docs/superpowers/plans/` (which legitimately reference the old names historically) and `backtest/tpsl_walkforward.py` (already-broken, out of scope per the spec).

- [ ] **Step 6: Confirm backup branch/tag survived**

Run: `git branch -a | grep zero-lag` and `git tag | grep zero-lag`
Expected: `backup/main-pre-zero-lag-mtf-pullback-v1` (local + `origin/`) and `pre-zero-lag-mtf-pullback-v1` both still present.

- [ ] **Step 7: Confirm no unmerged feature branch**

Run: `git branch --show-current`
Expected: `main` — every task in this plan committed directly to `main`, per the Global Constraints.

- [ ] **Step 8: Final status check**

Run: `git status`
Expected: clean working tree (all commits already made task-by-task) or, if this verification task itself needed a fix, one final commit:

```bash
git add -A
git commit -m "fix: address final verification findings"
```

No further commit needed if Steps 1-7 all passed cleanly on the first attempt.

---

## Self-Review Notes

**Spec coverage:** every numbered section of `docs/superpowers/specs/2026-08-11-zero-lag-mtf-pullback-v1-design.md` maps to a task — indicators (Task 2), config (Task 3), database schema (Task 4), entry pipeline + scoring (Tasks 5-6), outcome tracking (Task 7), scheduler split (Task 8), bot/webui (Tasks 9-10), removed-code list (Tasks 5, 11), testing (Tasks 2, 4-11), backtest harness (Task 12), migration order (Tasks 1-13 follow the spec's order 1:1), acceptance criteria (Task 13's grep/boot-check steps directly verify each bullet).

**Placeholder scan:** no "TBD"/"add error handling"/"similar to Task N" phrasing anywhere above — every step either shows the exact code to write or the exact command to run.

**Type consistency check performed:** `check_setup_confirmation`'s 3-tuple return shape (`status, fill_price, extra`) is used identically in Task 6's tests, Task 8's `main.py`, and Task 12's backtest script. `build_trade_prices(direction, entry) -> tuple[float, float]` (`tp, sl` order) matches across Task 6's definition, Task 8's call site, and Task 12's call site. `db.get_pending_setups(status, limit=200)` signature (positional `status`, not a filter dict) matches across Task 4's definition, Task 8's two call sites, and Task 10's `get_pending_setups()`. `db.mark_pending_setup_fired`/`mark_pending_setup_missed` both take `final_score` as a keyword-friendly parameter, used consistently in Task 8. One resolved gap during this review: Task 5's `detect_pending_setup` returns a dict **without** a `"status"` key (matching `database.save_pending_setup`, which hardcodes `status='pending_pullback'` in its `INSERT`) — but Task 12's backtest script manually sets `pending_setup["status"] = "pending_pullback"` after calling `detect_pending_setup` directly (bypassing the DB layer that would otherwise supply it), which is correct and already reflected in Task 12's Step 1 code above; flagging here only to confirm it was checked, not because it needed a fix.

