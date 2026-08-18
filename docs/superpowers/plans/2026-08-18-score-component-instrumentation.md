# Score Component Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the individual components of every signal's 0-100 score (not just the total) on both `pending_setups` and `signals` rows, and log them in a grep-able structured line, so a future pass can determine which components actually correlate with win/loss instead of assuming the current weights are right.

**Architecture:** `strategy._pullback_stage_score` and `strategy._breakout_stage_score` already compute the components internally (macro agreement flat 30, trend-slope strength 0-20, pullback quality 0-20, breakout freshness 0-20, breakout confirmation-candle quality 0-10) but only ever return the summed total — the components are thrown away. This plan refactors both functions to return `(total, components_dict)`, threads the components through `detect_pending_setup` → the `pending_setups` row → `check_setup_confirmation` → the fired `signals` row, and adds a `[SIGNAL_SCORE]` log line. This is instrumentation only — the scoring math itself, `MIN_SIGNAL_SCORE`, and every gate's pass/fail behavior are byte-for-byte unchanged.

**Tech Stack:** Python, pandas, SQLite (stdlib `sqlite3`), pytest (`monkeypatch`, `tmp_path`).

## Global Constraints

- The five components this plan instruments are the ones that actually exist in today's scoring model: `macro` (flat 30), `trend_strength` (0-20), `pullback` (0-20), `breakout_freshness` (0-20), `breakout_quality` (0-10). `architecture.txt`'s example log line also mentions `volume`/`momentum`/`volatility` — those aren't computed anywhere in the current pipeline and are explicitly out of scope here (they belong to Priority 2/4 work in Phase 2 of the roadmap, once the backtest shows they're worth adding). Do not fabricate values for them.
- No behavior change: `MIN_SIGNAL_SCORE` gating, the `partial_score + 30.0 < MIN_SIGNAL_SCORE` early-reject in `detect_pending_setup`, and every returned total must compute to the exact same numbers as before this plan — only add data, never change a comparison or a returned total's value.
- `strategy.py` still never imports or calls `database`/`db` — components flow out as plain dict keys, same as `score` does today; only `main.py` and `database.py` touch persistence.
- Existing `pending_setups`/`signals` columns and all current call sites of `save_pending_setup`/`save_signal` keep working unchanged — new columns are additive and nullable, added via the same idempotent `ALTER TABLE ... ADD COLUMN` + `try/except` pattern `database.init_db` already uses for the `signals` table.
- `main.py` has no existing unit test suite (no `tests/test_main*.py` — its scheduler jobs are async and DB/network-integration-heavy, and the codebase has never tested them directly). Follow that established pattern: verify `main.py`'s changes with `py_compile` plus a manual dry-run read-through, not a new test file — don't introduce a testing pattern the codebase doesn't otherwise use for this file.
- Run `python -m pytest -v` and confirm passing before every commit.

---

## File Structure

**New files:**
- `tests/test_score_components.py` — component breakdown correctness for `_pullback_stage_score`/`_breakout_stage_score`, plus `detect_pending_setup`/`check_setup_confirmation` exposing them.

**Modified files:**
- `strategy.py` — `_pullback_stage_score`, `_breakout_stage_score` return `(total, components)`; `detect_pending_setup` and `check_setup_confirmation` store components in their returned dicts.
- `database.py` — five new nullable columns each on `pending_setups` and `signals`; `save_pending_setup` and `save_signal` accept and persist them.
- `main.py` — `scan_for_new_setups` (setup already carries components, no change needed there since `save_pending_setup` reads from the dict) and `monitor_pending_setups` (pass breakout components through to `db.save_signal`, add `[SIGNAL_SCORE]` log line).
- `tests/test_pending_setups_db.py` — extend `_setup_dict` fixture with the new keys so existing tests keep passing with the new NOT-thrown-away columns exercised.

---

## Task 1: Component breakdown in the scoring functions

**Files:**
- Modify: `strategy.py:128-151` (`_pullback_stage_score`), `strategy.py:265-283` (`_breakout_stage_score`)
- Test: `tests/test_score_components.py`

**Interfaces:**
- Produces: `_pullback_stage_score(direction, zlema_trend, distance_pct) -> tuple[float, dict[str, float]]` where the dict has keys `macro`, `trend_strength`, `pullback`. `_breakout_stage_score(direction, confirmation_high, confirmation_low, confirmation_close, candles_to_break) -> tuple[float, dict[str, float]]` where the dict has keys `breakout_freshness`, `breakout_quality`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_score_components.py
import pandas as pd

from strategy import _pullback_stage_score, _breakout_stage_score


def test_pullback_stage_score_components_sum_to_total():
    zlema_trend = pd.Series([1.0] * 10)
    total, components = _pullback_stage_score("LONG", zlema_trend, distance_pct=0.0)
    assert set(components.keys()) == {"macro", "trend_strength", "pullback"}
    assert components["macro"] == 30.0
    assert round(sum(components.values()), 1) == total


def test_pullback_stage_score_full_marks_at_zero_distance():
    zlema_trend = pd.Series([1.0] * 10)
    total, components = _pullback_stage_score("LONG", zlema_trend, distance_pct=0.0)
    assert components["pullback"] == 20.0
    assert total == 30.0 + components["trend_strength"] + 20.0


def test_breakout_stage_score_components_sum_to_total():
    total, components = _breakout_stage_score(
        "LONG", confirmation_high=110.0, confirmation_low=100.0,
        confirmation_close=108.0, candles_to_break=1,
    )
    assert set(components.keys()) == {"breakout_freshness", "breakout_quality"}
    assert components["breakout_freshness"] == 20.0
    assert round(sum(components.values()), 1) == total


def test_breakout_stage_score_freshness_decays_with_candles():
    total_1, c1 = _breakout_stage_score("LONG", 110.0, 100.0, 108.0, candles_to_break=1)
    total_3, c3 = _breakout_stage_score("LONG", 110.0, 100.0, 108.0, candles_to_break=3)
    assert c1["breakout_freshness"] > c3["breakout_freshness"]
    assert c3["breakout_freshness"] == 10.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_score_components.py -v`
Expected: FAIL — both functions currently return a single `float`, not a tuple, so unpacking (`total, components = ...`) raises `TypeError`.

- [ ] **Step 3: Refactor `_pullback_stage_score`**

Replace (current `strategy.py:128-151`):

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

with:

```python
def _pullback_stage_score(
    direction: str, zlema_trend: pd.Series, distance_pct: float,
) -> tuple[float, dict[str, float]]:
    """0-70: 30 flat (4H/1H agreement, already gated -- no pending setup
    exists to score without it) + up to 20 (1H ZLEMA slope strength) + up
    to 20 (15m pullback quality: full marks at half the max pullback
    distance, linear decay to 0 at the full distance). Returns (total,
    components) so callers can persist the breakdown, not just the sum."""
    macro = 30.0

    last = float(zlema_trend.iloc[-1])
    prev = (
        float(zlema_trend.iloc[-1 - ZERO_LAG_SLOPE_LOOKBACK])
        if len(zlema_trend) > ZERO_LAG_SLOPE_LOOKBACK else last
    )
    slope_move_pct = abs(last - prev) / last if last else 0.0
    trend_strength = 20.0 * min(1.0, slope_move_pct / 0.01)

    half = PULLBACK_DISTANCE_PCT / 2.0
    if distance_pct <= half:
        pullback_quality = 1.0
    else:
        span = max(PULLBACK_DISTANCE_PCT - half, 1e-9)
        pullback_quality = max(0.0, 1.0 - (distance_pct - half) / span)
    pullback = 20.0 * pullback_quality

    total = round(macro + trend_strength + pullback, 1)
    components = {
        "macro": round(macro, 1),
        "trend_strength": round(trend_strength, 1),
        "pullback": round(pullback, 1),
    }
    return total, components
```

- [ ] **Step 4: Refactor `_breakout_stage_score`**

Replace (current `strategy.py:265-283`):

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
```

with:

```python
def _breakout_stage_score(
    direction: str, confirmation_high: float, confirmation_low: float,
    confirmation_close: float, candles_to_break: int,
) -> tuple[float, dict[str, float]]:
    """0-30: up to 20 ('fresh' crossover -- loses 5 points per extra
    candle it took to break the trigger price beyond the first one,
    floored at 0) + up to 10 (confirmation candle's close position within
    its own high-low range -- how cleanly it closed near its high for
    LONG / low for SHORT). Returns (total, components)."""
    freshness = max(0.0, 20.0 - 5.0 * max(0, candles_to_break - 1))

    candle_range = max(confirmation_high - confirmation_low, 1e-9)
    if direction == "LONG":
        clearance = (confirmation_close - confirmation_low) / candle_range
    else:
        clearance = (confirmation_high - confirmation_close) / candle_range
    quality = 10.0 * min(1.0, max(0.0, clearance))

    total = round(freshness + quality, 1)
    components = {
        "breakout_freshness": round(freshness, 1),
        "breakout_quality": round(quality, 1),
    }
    return total, components
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_score_components.py -v`
Expected: all 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/test_score_components.py
git commit -m "refactor: score functions return component breakdown alongside total"
```

---

## Task 2: Thread components through the pipeline

**Files:**
- Modify: `strategy.py:154-262` (`detect_pending_setup`), `strategy.py:286-368` (`check_setup_confirmation`)
- Test: `tests/test_score_components.py`

**Interfaces:**
- Consumes: `_pullback_stage_score`/`_breakout_stage_score` from Task 1.
- Produces: `detect_pending_setup(...)`'s returned dict gains keys `score_macro`, `score_trend_strength`, `score_pullback` (all `float`). `check_setup_confirmation(...)`'s returned `extra` dict (for the `"confirmed"` and `"missed"` statuses) gains keys `score_breakout_freshness`, `score_breakout_quality` (all `float`) alongside the existing `score` key.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_score_components.py (append)
from datetime import datetime, timedelta, timezone

from strategy import check_setup_confirmation


def _pending_pullback_setup(direction="LONG"):
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "symbol": "XRP_USDT", "direction": direction, "status": "pending_pullback",
        "setup_time": now.isoformat(), "score": 65.0,
        "score_macro": 30.0, "score_trend_strength": 15.0, "score_pullback": 20.0,
    }


def test_detect_pending_setup_exposes_score_components(monkeypatch):
    import strategy

    # reuse the same kline-faking approach as tests/test_strategy_zero_lag.py
    from tests.strategy_fixtures import make_trending_klines  # existing fixture helper

    def fake_klines(symbol, tf, count):
        return make_trending_klines(tf, direction=1, length=count)

    monkeypatch.setattr(strategy, "get_market_klines", fake_klines)
    setup = strategy.detect_pending_setup("XRP_USDT")
    assert setup is not None
    assert setup["score_macro"] == 30.0
    assert "score_trend_strength" in setup
    assert "score_pullback" in setup
    assert round(
        setup["score_macro"] + setup["score_trend_strength"] + setup["score_pullback"], 1
    ) == setup["score"]


def test_check_setup_confirmation_confirmed_exposes_breakout_components(monkeypatch):
    import strategy
    from tests.strategy_fixtures import make_breakout_confirmed_klines  # existing fixture helper

    monkeypatch.setattr(strategy, "get_market_klines",
                         lambda symbol, tf, count: make_breakout_confirmed_klines())
    status, fill_price, extra = check_setup_confirmation({
        **_pending_pullback_setup(), "status": "pending_breakout",
        "confirmation_high": 110.0, "confirmation_low": 100.0, "confirmation_close": 108.0,
        "confirmation_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "trigger_price": 100.02,
    })
    if status == "confirmed":
        assert "score_breakout_freshness" in extra
        assert "score_breakout_quality" in extra
```

Note: the exact fixture helper names/signatures (`make_trending_klines`, `make_breakout_confirmed_klines`) must match whatever `tests/strategy_fixtures.py` and `tests/test_strategy_zero_lag.py` already use for equivalent setups — **read `tests/strategy_fixtures.py` and the existing passing tests in `tests/test_strategy_zero_lag.py` first** and reuse their exact fixture-building calls rather than inventing new ones, so this test follows the same monkeypatching convention already proven to work against `detect_pending_setup`/`check_setup_confirmation` in that file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_score_components.py -v`
Expected: FAIL — `setup["score_macro"]` raises `KeyError` (key doesn't exist yet).

- [ ] **Step 3: Update `detect_pending_setup`**

In `strategy.py`, change the call site (current line ~232):

```python
        partial_score = _pullback_stage_score(direction, zlema_trend, distance_pct)
        if partial_score + 30.0 < MIN_SIGNAL_SCORE:
```

to:

```python
        partial_score, score_components = _pullback_stage_score(direction, zlema_trend, distance_pct)
        if partial_score + 30.0 < MIN_SIGNAL_SCORE:
```

and in the returned dict (current lines ~241-258), add the three component keys alongside the existing `"score": partial_score`:

```python
            "score": partial_score,
            "score_macro": score_components["macro"],
            "score_trend_strength": score_components["trend_strength"],
            "score_pullback": score_components["pullback"],
```

- [ ] **Step 4: Update `check_setup_confirmation`**

In `strategy.py`, change the call site (current lines ~360-364):

```python
    breakout_score = _breakout_stage_score(
        direction, float(setup["confirmation_high"]), float(setup["confirmation_low"]),
        float(setup["confirmation_close"]), candles_to_break,
    )
    final_score = round(min(100.0, float(setup["score"]) + breakout_score), 1)
    if final_score < MIN_SIGNAL_SCORE:
        return "missed", None, {"score": final_score}

    return "confirmed", trigger_price, {"score": final_score}
```

to:

```python
    breakout_score, breakout_components = _breakout_stage_score(
        direction, float(setup["confirmation_high"]), float(setup["confirmation_low"]),
        float(setup["confirmation_close"]), candles_to_break,
    )
    final_score = round(min(100.0, float(setup["score"]) + breakout_score), 1)
    extra = {
        "score": final_score,
        "score_breakout_freshness": breakout_components["breakout_freshness"],
        "score_breakout_quality": breakout_components["breakout_quality"],
    }
    if final_score < MIN_SIGNAL_SCORE:
        return "missed", None, extra

    return "confirmed", trigger_price, extra
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_score_components.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions — `setup["score"]` and `extra["score"]` values and types are unchanged, only new keys were added.

- [ ] **Step 7: Commit**

```bash
git add strategy.py tests/test_score_components.py
git commit -m "feat: expose score components on pending setups and confirmations"
```

---

## Task 3: Persist components in the database

**Files:**
- Modify: `database.py:27-146` (`init_db`, `save_signal`, `save_pending_setup`)
- Test: `tests/test_pending_setups_db.py`, `tests/test_score_components.py`

**Interfaces:**
- Consumes: the five component keys from Task 2's setup/extra dicts.
- Produces: `signals` and `pending_setups` tables each gain columns `score_macro REAL`, `score_trend_strength REAL`, `score_pullback REAL`, `score_breakout_freshness REAL`, `score_breakout_quality REAL` (all nullable). `save_signal(...)` gains five new optional keyword parameters (default `None`) of the same names. `save_pending_setup(setup: dict)` reads the first three from the passed-in dict if present (matching its existing pattern of reading required keys directly off `setup`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_score_components.py (append)
from datetime import datetime, timedelta, timezone

import database as db


def test_save_pending_setup_persists_score_components(temp_db):
    now = datetime.now(timezone.utc)
    setup = {
        "symbol": "XRP_USDT", "direction": "LONG",
        "macro_tf": "4h", "trend_tf": "1h", "pullback_tf": "15m", "entry_tf": "5m",
        "macro_trend": 1, "trend_state": 1,
        "zlema_1h": 100.0, "zlema_15m": 100.5,
        "pullback_price": 100.4, "pullback_time": now.isoformat(),
        "score": 65.0, "score_macro": 30.0, "score_trend_strength": 15.0, "score_pullback": 20.0,
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "created_at": now.isoformat(),
    }
    setup_id = db.save_pending_setup(setup)
    rows = db.get_pending_setups("pending_pullback")
    assert rows[0]["score_macro"] == 30.0
    assert rows[0]["score_trend_strength"] == 15.0
    assert rows[0]["score_pullback"] == 20.0


def test_save_signal_persists_score_components(temp_db):
    signal_id = db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0, tp_price=1.01, sl_price=0.99,
        leverage=20, generated_at=datetime.now(timezone.utc),
        score_macro=30.0, score_trend_strength=18.0, score_pullback=20.0,
        score_breakout_freshness=20.0, score_breakout_quality=8.0,
    )
    rows = db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["score_macro"] == 30.0
    assert row["score_breakout_freshness"] == 20.0
    assert row["score_breakout_quality"] == 8.0
```

Uses the existing `temp_db` fixture from `tests/test_pending_setups_db.py` — add `import pytest` and the same fixture (copy it, or move it to a shared `conftest.py` if one already exists; check first with `ls tests/conftest.py`) at the top of `tests/test_score_components.py` so this test file doesn't depend on another test file's fixture being collected first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_score_components.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such column: score_macro` (or `TypeError: save_signal() got an unexpected keyword argument`).

- [ ] **Step 3: Add columns to `signals`**

In `database.py`, extend the existing `ALTER TABLE` loop (current lines 61-92) by adding five entries to the list:

```python
            ("score_macro", "REAL"),
            ("score_trend_strength", "REAL"),
            ("score_pullback", "REAL"),
            ("score_breakout_freshness", "REAL"),
            ("score_breakout_quality", "REAL"),
```

- [ ] **Step 4: Add the same columns to `pending_setups` via an idempotent ALTER loop**

`pending_setups`'s columns are currently all baked into its `CREATE TABLE IF NOT EXISTS` (lines 96-133), which won't add columns to an already-existing table on the server. Add a second `ALTER TABLE` loop right after the `pending_setups` `CREATE TABLE`/index statements (after current line 142), mirroring the `signals` table's pattern:

```python
        for col, definition in [
            ("score_macro", "REAL"),
            ("score_trend_strength", "REAL"),
            ("score_pullback", "REAL"),
            ("score_breakout_freshness", "REAL"),
            ("score_breakout_quality", "REAL"),
        ]:
            try:
                con.execute(f"ALTER TABLE pending_setups ADD COLUMN {col} {definition}")
            except Exception:
                pass
```

- [ ] **Step 5: Update `save_signal`**

Add five parameters to the signature (current lines 149-166), each `float | None = None`, right after `position_size`:

```python
    position_size: float | None = None,
    score_macro: float | None = None,
    score_trend_strength: float | None = None,
    score_pullback: float | None = None,
    score_breakout_freshness: float | None = None,
    score_breakout_quality: float | None = None,
) -> int:
```

and extend the `INSERT` statement's column list, placeholder list, and parameter tuple to include the five new columns/values (same mechanical pattern as the existing `tp2_price, tp3_price, position_size` columns already there).

- [ ] **Step 6: Update `save_pending_setup`**

In `database.py`'s `save_pending_setup` (current lines 301-321), add the three pullback-stage component columns to the `INSERT` statement (column list, `?` placeholders, and the parameter tuple), reading them the same way the function already reads `setup["score"]` — i.e. `setup["score_macro"]`, `setup["score_trend_strength"]`, `setup["score_pullback"]`. Since `strategy.detect_pending_setup` (Task 2) always sets these three keys now, no `.get()` fallback is needed — keep the same direct-indexing style the function already uses for its other required fields.

- [ ] **Step 7: Extend the existing `_setup_dict` test fixture**

In `tests/test_pending_setups_db.py`, add the three keys to `_setup_dict()`'s returned dict so every existing test using that fixture continues to pass against the now-required columns:

```python
        "score": 65.0, "score_macro": 30.0, "score_trend_strength": 15.0, "score_pullback": 20.0,
```

(replacing the existing lone `"score": 65.0,` line in that fixture).

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_score_components.py tests/test_pending_setups_db.py -v`
Expected: all PASS.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions.

- [ ] **Step 10: Commit**

```bash
git add database.py tests/test_score_components.py tests/test_pending_setups_db.py
git commit -m "feat: persist score components on pending_setups and signals rows"
```

---

## Task 4: Wire `main.py` and add the structured log line

**Files:**
- Modify: `main.py:170-309` (`monitor_pending_setups`)

**Interfaces:**
- Consumes: `setup["score_macro"]`/`score_trend_strength`/`score_pullback` (already on every setup dict since Task 2/3), `extra["score_breakout_freshness"]`/`score_breakout_quality`/`score` (already on every `"confirmed"` result since Task 2).
- Produces: no new interface — `db.save_signal` call gains the five new keyword arguments; a new `[SIGNAL_SCORE]` log line is emitted right after the existing `[SIGNAL] Confirmed #%d ...` line.

- [ ] **Step 1: Pass components into `db.save_signal`**

In `main.py`'s `monitor_pending_setups`, the `db.save_signal(...)` call (current lines 275-289) already builds its kwargs from `sig` (a `strategy.Signal`) and a few setup-scoped locals. `strategy.Signal` doesn't currently carry the component fields, so pass them directly from `setup`/`extra` (both already in scope in that loop) rather than adding them to the `Signal` dataclass — keeps this change local to persistence, matching how `entry_timeframe`/`trend_timeframe`/`setup_reason` are already passed as separate arguments rather than `Signal` fields:

```python
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
                score_macro=setup["score_macro"],
                score_trend_strength=setup["score_trend_strength"],
                score_pullback=setup["score_pullback"],
                score_breakout_freshness=extra["score_breakout_freshness"],
                score_breakout_quality=extra["score_breakout_quality"],
            )
```

- [ ] **Step 2: Add the structured `[SIGNAL_SCORE]` log line**

Right after the existing `logger.info("[SIGNAL] Confirmed #%d ...")` call (current lines 303-307), add:

```python
            logger.info(
                "[SIGNAL_SCORE] #%d macro=%.1f trend_strength=%.1f pullback=%.1f "
                "breakout_freshness=%.1f breakout_quality=%.1f total=%.1f",
                signal_id, setup["score_macro"], setup["score_trend_strength"], setup["score_pullback"],
                extra["score_breakout_freshness"], extra["score_breakout_quality"], sig.score,
            )
```

- [ ] **Step 3: Compile check**

Run: `python -m py_compile main.py database.py strategy.py`
Expected: no output (success).

- [ ] **Step 4: Manual dry-run verification**

Run the bot locally with `DRY_RUN=true` (per `CLAUDE.md`'s existing dry-run convention — no live trading risk) for long enough to see at least one `[PENDING] Armed pullback` line, then confirm in `mexc_bot.log` (or stdout) that a fired dry-run signal produces both the existing `[SIGNAL]`/`[DRY-RUN]` line and the new `[SIGNAL_SCORE]` line with five non-null, sane-looking numbers (macro should always read `30.0`). This is the same style of manual verification `CLAUDE.md` already documents for scheduler-job changes (no automated test harness exists for `main.py`, per this plan's Global Constraints).

- [ ] **Step 5: Run the full suite one more time**

Run: `python -m pytest -v`
Expected: all PASS (no `main.py` tests exist to run, but this confirms Tasks 1-3 are still intact after the `main.py` edit).

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: wire score components into save_signal and log [SIGNAL_SCORE]"
```

---

## Self-Review

- **Spec coverage:** Priority 3 of `architecture.txt` asked to (a) instrument every confirmed signal with individual score components, (b) log them in a `SIGNAL_SCORE:` style line, (c) store them in the database with the outcome. Task 1/2 do (a), Task 4 Step 2 does (b) (adapted to this codebase's single-line structured-log convention rather than the multi-line example format — same information, consistent with every other `[TAG] k=v k=v` log line already in `main.py`), Task 3 does (c) (components land in `signals`, joinable to `status`/`pnl_roi` for the later correlation analysis Priority 3 also asks for — that analysis itself is Phase 2 work, gated on Plan 4's backtest data existing).
- **Placeholder scan:** No TBDs; every step has literal code. The one explicit scope note (no `volume`/`momentum`/`volatility` components) is a documented decision, not a placeholder.
- **Type consistency:** `_pullback_stage_score`/`_breakout_stage_score` both consistently return `tuple[float, dict[str, float]]` across Tasks 1-2. `save_signal`'s five new parameters are `float | None` end to end (function signature → DB column type `REAL` → test assertions). `save_pending_setup` reads three of the same key names directly off the `setup` dict, matching Task 2's dict-key names exactly (`score_macro`, `score_trend_strength`, `score_pullback` — no renaming across the boundary).
- **Fixture reuse:** Task 2's test explicitly instructs reading `tests/strategy_fixtures.py` and `tests/test_strategy_zero_lag.py` first rather than guessing fixture signatures — flagged rather than silently assumed, since this plan was written without re-reading that file's current contents in full.
