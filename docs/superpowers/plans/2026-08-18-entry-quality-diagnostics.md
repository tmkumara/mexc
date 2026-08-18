# Entry-Quality Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log and persist entry-candle quality metrics (body/ATR, wick rejection, relative volume, distance from ZLEMA) for every fired signal, so a future pass can determine what actually distinguishes the fast losses (7 of 24 losses in the live log hit SL in exactly ~3.98 minutes — one candle after confirmation) from the rest, per `architecture.txt` Priority 7's explicit "add diagnostics before changing behavior" instruction. This plan adds **measurement only** — no new reject gate, no change to which setups fire.

**Architecture:** `strategy.check_setup_confirmation`'s `pending_breakout` branch already has the exact ENTRY_TF candle whose high/low broke the trigger price (`closed.iloc[-1]`, the same `closed` DataFrame it already fetched). This plan adds a `_entry_quality_diagnostics(closed, direction, fill_price)` helper computed at that exact point — the entry candle in a live fire and in every future backtest run are the same object, so the diagnostics are backtest-comparable from day one. Diagnostics flow through the `"confirmed"` result's `extra` dict exactly like Priority 3's score components did, into new nullable `signals` columns, and into a `[ENTRY_DIAGNOSTICS]` log line.

**Tech Stack:** Python, pandas, SQLite (stdlib `sqlite3`), pytest (`monkeypatch`).

## Global Constraints

- Diagnostics-only: `check_setup_confirmation`'s return value (`status`, `fill_price`) and every existing gate (`MIN_SIGNAL_SCORE`, expiry, settle-age) are untouched. Nothing computed by this plan is allowed to change whether or when a setup fires — that's explicitly Phase 2 work (the `entry-quality-filter` plan in the roadmap), gated on this plan's own data existing first.
- This plan depends on [`2026-08-18-score-component-instrumentation.md`](2026-08-18-score-component-instrumentation.md) Task 2's change to `check_setup_confirmation`'s `pending_breakout` branch (the `breakout_score, breakout_components = ...` refactor) — apply that plan first, or adapt this plan's diffs to whatever the branch looks like if it hasn't landed yet.
- Spread/order-book liquidity are explicitly out of scope — `mexc_client.py` has no order-book endpoint wired up, so there's no data source for them without a separate integration. `architecture.txt` Priority 8 itself caveats liquidity measurement as "where data is available"; it isn't here.
- No behavior change to `strategy.py`'s pure-function contract: it still never imports/calls `database`.
- Run `python -m pytest -v` and confirm passing before every commit.

---

## File Structure

**New files:**
- `tests/test_entry_diagnostics.py` — `_entry_quality_diagnostics` unit tests plus its wiring into `check_setup_confirmation`.

**Modified files:**
- `strategy.py` — new `_entry_quality_diagnostics` helper; `check_setup_confirmation`'s `pending_breakout` branch computes and returns it.
- `database.py` — six new nullable `signals` columns; `save_signal` accepts and persists them.
- `main.py` — `monitor_pending_setups` passes diagnostics through to `db.save_signal` and logs `[ENTRY_DIAGNOSTICS]`.

---

## Task 1: `_entry_quality_diagnostics` helper

**Files:**
- Modify: `strategy.py` (add near `_breakout_stage_score`, since both operate on the same confirmation candle)
- Test: `tests/test_entry_diagnostics.py`

**Interfaces:**
- Produces: `_entry_quality_diagnostics(closed: pd.DataFrame, direction: str, fill_price: float) -> dict[str, float]` with keys `candle_body_atr_ratio`, `candle_range_atr_ratio`, `upper_wick_ratio`, `lower_wick_ratio`, `volume_ratio`, `distance_from_zlema_pct`. `closed` is the same fully-closed ENTRY_TF DataFrame `check_setup_confirmation` already has in scope (last row = the entry/confirmation candle).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_entry_diagnostics.py
import pandas as pd

from strategy import _entry_quality_diagnostics


def _make_closed_df(n=100, last_open=100.0, last_high=102.0, last_low=99.5,
                     last_close=101.5, last_volume=500.0, base_volume=100.0):
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    opens = [100.0] * (n - 1) + [last_open]
    highs = [100.5] * (n - 1) + [last_high]
    lows = [99.5] * (n - 1) + [last_low]
    closes = [100.0] * (n - 1) + [last_close]
    volumes = [base_volume] * (n - 1) + [last_volume]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def test_candle_body_and_range_are_atr_normalized():
    df = _make_closed_df()
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["candle_body_atr_ratio"] > 0
    assert diag["candle_range_atr_ratio"] >= diag["candle_body_atr_ratio"]


def test_volume_ratio_reflects_spike():
    df = _make_closed_df(last_volume=500.0, base_volume=100.0)
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["volume_ratio"] > 4.0   # ~5x the trailing average


def test_long_upper_wick_near_zero_for_close_at_high():
    df = _make_closed_df(last_open=100.0, last_high=102.0, last_low=99.5, last_close=101.9)
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["upper_wick_ratio"] < 0.1


def test_short_lower_wick_near_zero_for_close_at_low():
    df = _make_closed_df(last_open=100.0, last_high=100.5, last_low=97.0, last_close=97.1)
    diag = _entry_quality_diagnostics(df, "SHORT", fill_price=97.0)
    assert diag["lower_wick_ratio"] < 0.1


def test_distance_from_zlema_pct_is_signed_by_direction():
    df = _make_closed_df()
    diag_long = _entry_quality_diagnostics(df, "LONG", fill_price=105.0)
    diag_short = _entry_quality_diagnostics(df, "SHORT", fill_price=95.0)
    assert "distance_from_zlema_pct" in diag_long
    assert "distance_from_zlema_pct" in diag_short
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_diagnostics.py -v`
Expected: FAIL — `ImportError: cannot import name '_entry_quality_diagnostics'`.

- [ ] **Step 3: Implement the helper**

Add to `strategy.py`, near `_breakout_stage_score`:

```python
def _entry_quality_diagnostics(closed: pd.DataFrame, direction: str, fill_price: float) -> dict[str, float]:
    """Observational only -- computed at the exact entry candle
    (check_setup_confirmation's pending_breakout branch) so live and
    backtest runs measure the identical candle. Not used for any gate;
    see docs/superpowers/plans/2026-08-18-entry-quality-diagnostics.md."""
    atr = calculate_atr(closed, ATR_PERIOD)
    last_atr = float(atr.iloc[-1]) if len(atr) else 0.0
    last_atr = last_atr if last_atr > 1e-12 else 1e-12

    last = closed.iloc[-1]
    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    candle_range = max(h - l, 1e-12)

    body_atr_ratio = abs(c - o) / last_atr
    range_atr_ratio = candle_range / last_atr

    upper_wick_ratio = (h - max(o, c)) / candle_range
    lower_wick_ratio = (min(o, c) - l) / candle_range

    volume_lookback = closed["volume"].iloc[-21:-1]  # trailing 20 bars, excludes the entry candle itself
    avg_volume = float(volume_lookback.mean()) if len(volume_lookback) else 0.0
    last_volume = float(closed["volume"].iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume > 1e-12 else 0.0

    zlema = calculate_zlema(closed["close"], ZERO_LAG_LENGTH)
    zlema_last = float(zlema.iloc[-1])
    if direction == "LONG":
        distance_from_zlema_pct = (fill_price - zlema_last) / zlema_last if zlema_last else 0.0
    else:
        distance_from_zlema_pct = (zlema_last - fill_price) / zlema_last if zlema_last else 0.0

    return {
        "candle_body_atr_ratio": round(body_atr_ratio, 4),
        "candle_range_atr_ratio": round(range_atr_ratio, 4),
        "upper_wick_ratio": round(upper_wick_ratio, 4),
        "lower_wick_ratio": round(lower_wick_ratio, 4),
        "volume_ratio": round(volume_ratio, 4),
        "distance_from_zlema_pct": round(distance_from_zlema_pct, 6),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_diagnostics.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_entry_diagnostics.py
git commit -m "feat: add _entry_quality_diagnostics helper (observational only)"
```

---

## Task 2: Wire into `check_setup_confirmation`

**Files:**
- Modify: `strategy.py` (`check_setup_confirmation`'s `pending_breakout` branch)
- Test: `tests/test_entry_diagnostics.py`

**Interfaces:**
- Consumes: `_entry_quality_diagnostics` from Task 1.
- Produces: the `"confirmed"` (and `"missed"`) result's `extra` dict gains the six diagnostic keys from Task 1, alongside `score`/`score_breakout_*` from the score-instrumentation plan.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_diagnostics.py (append)
from datetime import datetime, timezone

from strategy import check_setup_confirmation


def test_confirmed_result_carries_entry_diagnostics(monkeypatch):
    import strategy

    # Reuse whatever kline-faking fixture tests/test_strategy_zero_lag.py
    # already uses to drive check_setup_confirmation into a "confirmed"
    # result -- read that file first and match its exact setup.
    from tests.strategy_fixtures import make_breakout_confirmed_klines

    monkeypatch.setattr(strategy, "get_market_klines",
                         lambda symbol, tf, count: make_breakout_confirmed_klines())

    setup = {
        "id": 1, "symbol": "XRP_USDT", "direction": "LONG", "status": "pending_breakout",
        "setup_time": datetime.now(timezone.utc).isoformat(), "score": 65.0,
        "confirmation_high": 110.0, "confirmation_low": 100.0, "confirmation_close": 108.0,
        "confirmation_time": datetime.now(timezone.utc).isoformat(),
        "trigger_price": 100.02,
    }
    status, fill_price, extra = check_setup_confirmation(setup)
    if status == "confirmed":
        for key in ("candle_body_atr_ratio", "candle_range_atr_ratio", "upper_wick_ratio",
                    "lower_wick_ratio", "volume_ratio", "distance_from_zlema_pct"):
            assert key in extra
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_entry_diagnostics.py::test_confirmed_result_carries_entry_diagnostics -v`
Expected: FAIL (keys missing from `extra`) once the fixture is wired to actually reach `"confirmed"` — if the fixture instead reaches `"waiting"`, adjust the fixture call to match whatever `tests/test_strategy_zero_lag.py` uses to reach the breakout-fill branch (its `TestConfirmed`-equivalent case), since this test's whole point is exercising that exact branch.

- [ ] **Step 3: Wire the helper into the `pending_breakout` branch**

In `strategy.py`'s `check_setup_confirmation`, in the `# status == "pending_breakout"` section, after `entry_hit` is confirmed truthy (right before or alongside the `breakout_score, breakout_components = _breakout_stage_score(...)` call from the score-instrumentation plan), add:

```python
    diagnostics = _entry_quality_diagnostics(closed, direction, trigger_price)
```

and merge `diagnostics` into the `extra` dict built for both the `"missed"` and `"confirmed"` returns:

```python
    extra = {
        "score": final_score,
        "score_breakout_freshness": breakout_components["breakout_freshness"],
        "score_breakout_quality": breakout_components["breakout_quality"],
        **diagnostics,
    }
```

(If Task 2 of the score-instrumentation plan hasn't landed yet in this working tree, build `extra` as `{"score": final_score, **diagnostics}` instead and merge the score-component keys in whichever plan lands second.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_diagnostics.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/test_entry_diagnostics.py
git commit -m "feat: attach entry-quality diagnostics to confirmed/missed results"
```

---

## Task 3: Persist and log diagnostics

**Files:**
- Modify: `database.py` (`init_db`'s `signals` ALTER loop, `save_signal`), `main.py` (`monitor_pending_setups`)
- Test: `tests/test_entry_diagnostics.py`

**Interfaces:**
- Consumes: the six diagnostic keys from Task 2's `extra` dict.
- Produces: `signals` table gains columns `diag_candle_body_atr_ratio REAL`, `diag_candle_range_atr_ratio REAL`, `diag_upper_wick_ratio REAL`, `diag_lower_wick_ratio REAL`, `diag_volume_ratio REAL`, `diag_distance_from_zlema_pct REAL`. `save_signal(...)` gains six new `float | None = None` keyword parameters with those same names (minus the `diag_` prefix, to keep the Python-side name matching `extra`'s key — only the DB column is prefixed, to make ad-hoc `SELECT *` output self-explanatory next to the un-prefixed `score`/`entry_price`/etc. columns).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_diagnostics.py (append)
from datetime import datetime, timezone

import database as db


def test_save_signal_persists_entry_diagnostics(temp_db):
    signal_id = db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0, tp_price=1.01, sl_price=0.99,
        leverage=20, generated_at=datetime.now(timezone.utc),
        candle_body_atr_ratio=0.8, candle_range_atr_ratio=1.2,
        upper_wick_ratio=0.05, lower_wick_ratio=0.1,
        volume_ratio=2.3, distance_from_zlema_pct=0.0012,
    )
    rows = db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["diag_candle_body_atr_ratio"] == 0.8
    assert row["diag_volume_ratio"] == 2.3
```

Needs the same `temp_db` fixture as `tests/test_pending_setups_db.py` (see that file's `@pytest.fixture def temp_db`) — add it locally if `tests/test_entry_diagnostics.py` doesn't already have it from Task 1/2 (Task 1/2's tests don't touch the DB, so it likely needs adding here for the first time in this file).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_entry_diagnostics.py::test_save_signal_persists_entry_diagnostics -v`
Expected: FAIL — `TypeError: save_signal() got an unexpected keyword argument 'candle_body_atr_ratio'`.

- [ ] **Step 3: Add columns to `signals`**

In `database.py`'s `init_db`, extend the `ALTER TABLE` loop with:

```python
            ("diag_candle_body_atr_ratio", "REAL"),
            ("diag_candle_range_atr_ratio", "REAL"),
            ("diag_upper_wick_ratio", "REAL"),
            ("diag_lower_wick_ratio", "REAL"),
            ("diag_volume_ratio", "REAL"),
            ("diag_distance_from_zlema_pct", "REAL"),
```

- [ ] **Step 4: Update `save_signal`**

Add six parameters (`float | None = None`) to the signature, and extend the `INSERT` statement's column list (mapping each Python parameter to its `diag_`-prefixed column), placeholders, and value tuple — same mechanical pattern as the score-component columns.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_diagnostics.py -v`
Expected: all PASS.

- [ ] **Step 6: Wire `main.py`**

In `monitor_pending_setups`'s `db.save_signal(...)` call, add the six new keyword arguments sourced from `extra` (already merged with the diagnostic keys per Task 2 Step 3):

```python
                candle_body_atr_ratio=extra["candle_body_atr_ratio"],
                candle_range_atr_ratio=extra["candle_range_atr_ratio"],
                upper_wick_ratio=extra["upper_wick_ratio"],
                lower_wick_ratio=extra["lower_wick_ratio"],
                volume_ratio=extra["volume_ratio"],
                distance_from_zlema_pct=extra["distance_from_zlema_pct"],
```

and add a `[ENTRY_DIAGNOSTICS]` log line next to the `[SIGNAL_SCORE]` line (or combined with it if the score-instrumentation plan already landed):

```python
            logger.info(
                "[ENTRY_DIAGNOSTICS] #%d body_atr=%.2f range_atr=%.2f upper_wick=%.2f "
                "lower_wick=%.2f vol_ratio=%.2f dist_zlema_pct=%.4f",
                signal_id, extra["candle_body_atr_ratio"], extra["candle_range_atr_ratio"],
                extra["upper_wick_ratio"], extra["lower_wick_ratio"],
                extra["volume_ratio"], extra["distance_from_zlema_pct"] * 100,
            )
```

- [ ] **Step 7: Compile check**

Run: `python -m py_compile main.py database.py strategy.py`
Expected: no output (success).

- [ ] **Step 8: Manual dry-run verification**

Same as the score-instrumentation plan's Task 4 Step 4: run with `DRY_RUN=true`, confirm a fired dry-run signal logs `[ENTRY_DIAGNOSTICS]` with six sane-looking numbers (e.g. `dist_zlema_pct` should be small and positive for a clean LONG breakout, `vol_ratio` well above 0).

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -v`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add database.py main.py tests/test_entry_diagnostics.py
git commit -m "feat: persist and log entry-quality diagnostics per fired signal"
```

---

## Self-Review

- **Spec coverage:** Priority 7 of `architecture.txt` asked to investigate whether the fast losses correlate with "oversized entry candles, volatility spikes, entering too far from ZLEMA, ... reversal candles, failed breakout/retest, abnormal volume" and to "add diagnostics before changing behavior." This plan measures exactly those (body/ATR → oversized candles; range/ATR → volatility spikes; distance_from_zlema_pct → chase distance; wick ratios → reversal/rejection candles; volume_ratio → abnormal volume), attached to every fired signal so it's directly joinable against `status`/`pnl_roi`/`closed_at` for the correlation analysis — without touching which signals fire. Liquidity/spread are explicitly and honestly marked out of scope (no data source exists).
- **Placeholder scan:** No TBDs; every metric has a concrete formula and test.
- **Type consistency:** `_entry_quality_diagnostics` returns the same six keys end to end (helper → `extra` dict → `save_signal` params → DB columns), with the DB-column vs. Python-param naming difference (`diag_` prefix) called out explicitly rather than left implicit.
- **Dependency note:** This plan assumes the score-instrumentation plan's `check_setup_confirmation` refactor for context but includes a fallback diff (Task 2 Step 3) if it hasn't landed yet, so the two plans can execute in either order despite touching the same function.
