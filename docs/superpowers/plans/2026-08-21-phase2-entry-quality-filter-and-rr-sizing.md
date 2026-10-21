# Phase 2 Priority 4 + 5: Entry-Quality Reject Filter and RR/Exit Sizing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-gated entry-quality reject filter (Priority 4) to `check_setup_confirmation`'s confirmed branch, and raise the fixed RR from 0.70:1 to 1.5:1 (Priority 5), then re-run the project's existing 120-day/40-symbol backtest to confirm the combined change turns net ROI positive in-sample, matching the calibration analysis below.

**Architecture:** Both changes land entirely inside the existing three-state pipeline (`strategy.py` / `config.py` / `main.py`) — no new tables, no new pipeline stages. Priority 4 adds one pure-function gate (`_entry_quality_reject`) evaluated where `_entry_quality_diagnostics` is already computed (diagnostics were wired in as observational-only by the 2026-08-18 Plan C; this plan turns them into an active reject gate for the first time). Priority 5 is a one-line default change to `TP_ROI_PCT`, since `TP_ROI_PCT`/`SL_ROI_PCT` already drive RR everywhere in the codebase (`rr = round(TP_ROI_PCT / SL_ROI_PCT, 2)`).

**Tech Stack:** Python, pandas, pytest, the existing `scripts/backtest_simple_strategy.py` walk-forward-safe backtest harness (no changes needed there — it calls `strategy.py` directly, so it picks up both changes automatically).

**Spec:** `docs/superpowers/plans/2026-08-18-zero-lag-mtf-v2-roadmap.md` (Phase 2, Priorities 4 and 5) — this plan is the roadmap's own designated next step, unblocked now that `backtest/reports/2026-08-19-zero-lag-v1-baseline-120d.log` (1136 trades) exists. This plan document also **is** the calibration analysis the roadmap said Priority 4/5 needed before any concrete threshold could be written (see "Calibration analysis" below) — a fresh 1189-trade/120-day/40-symbol re-run of the same backtest, instrumented to capture per-trade entry-quality diagnostics and RR-grid what-if outcomes, which the existing baseline report did not capture.

## Calibration analysis (why these exact numbers)

Re-ran the same lookahead-safe simulation as `scripts/backtest_simple_strategy.py` (same 40-symbol coin-pool snapshot, 120 days), instrumented to additionally: (a) persist the `_entry_quality_diagnostics` fields already computed per confirmed trade, and (b) re-walk each trade's actual post-entry candle path with `outcome_check.check_tp_sl` at RR candidates 0.70 (baseline)/1.0/1.2/1.3/1.5 (TP_ROI = RR × the current SL_ROI=10.0, no extra network fetches needed since the price path was already fetched). Produced 1189 trades (slightly more than the original 1136-trade report — same symbols/window, later run, live coin universe drifts).

**Priority 5 — RR grid (entire 1189-trade sample, unfiltered):**

| RR | Win rate | Net ROI (pts) | Expectancy/trade |
|---|---|---|---|
| 0.70 (current) | 59.8% | -992.0 | -0.83% |
| 1.0 | 52.1% | -699.0 | -0.59% |
| 1.2 | 47.9% | -561.0 | -0.47% |
| 1.3 | 46.4% | -383.0 | -0.32% |
| 1.5 | 43.9% | **-29.0** | **-0.02%** |

Win rate mechanically falls as RR rises (a more distant TP is reached less often before price reverses to SL), but expectancy improves monotonically and 1.5 is nearly break-even alone — confirming the roadmap's own conclusion that RR, not entry filtering, is the dominant lever. By direction at RR=1.5: LONG is already net positive (+314 pts, 45.9% WR, n=676) while SHORT is not (-343 pts, 41.3% WR, n=513) — a direction-specific RR is a plausible future refinement but out of scope here (this plan applies one global RR, per the roadmap's Priority-1 note that LONG/SHORT asymmetry conclusions need their own fresh look and shouldn't be bundled opportunistically).

**Decision:** `TP_ROI_PCT` default changes from `7.0` to `15.0` (SL_ROI_PCT stays `10.0` → RR 1.5:1), the best of the tested candidates. Testing above 1.5 is future work — not covered by this analysis.

**Priority 4 — entry-quality diagnostics vs. outcome (1189 trades, quartile win rate):**

| Feature | Q1 (worst→best or vice versa) | Q4 | Spread |
|---|---|---|---|
| `candle_body_atr_ratio` (confirmation candle body / ATR) | 54.7% (≤0.196) | 68.0% (>0.707) | 13.3 pts, monotonic |
| `distance_from_zlema_pct` (how extended past its own ZLEMA at entry) | 64.1% (≤0.083%) | 53.1% (>0.266%) | 11.0 pts, monotonic |
| direction-specific "chase wick" (upper for LONG, lower for SHORT) | 68.2%/59.7% (bottom quartile) | 54.9%/51.6% (top quartile) | ~13.5 pts LONG (clean monotonic), ~8 pts SHORT (noisier) |
| `volume_ratio`, `candle_range_atr_ratio` | — | — | flat, not useful gates |

A thin-bodied confirmation candle (small body relative to ATR — an indecisive breakout bar), an entry already far past its own ZLEMA (chasing an extended move), or a confirmation candle with a large wick against the breakout direction (rejected near its extreme, i.e. exactly the "breakout-chase" pattern behind the 7/24 fast-SL losses the 2026-08-18 live diagnosis flagged) all independently predict materially worse outcomes.

**Combined filter (reject if ANY of: `candle_body_atr_ratio < 0.20` OR `distance_from_zlema_pct > 0.0027` OR chase-wick `> 0.45`), evaluated against the RR grid:**

| RR | Kept trades | Win rate | Net ROI (pts) |
|---|---|---|---|
| 0.70 | 418 (65% rejected) | 65.6% | +60.0 |
| 1.3 | 418 | 47.8% | +2.0 |
| 1.5 | 418 | 44.5% | +52.0 |

Every RR level turns net-positive once this filter is applied — even at the current 0.70:1 RR. The cost is volume: ~53% of otherwise-confirmed setups are rejected (from ~1189 down to ~555 across the full window), which at the live ~11 signals/day cadence would mean roughly halved signal frequency. Given the strategy's current live performance is a **confirmed** loser (-69.0 ROI points over 97 real trades since deploy, see project memory), trading frequency for solvency is the right side of that trade — but it's a real, deliberate cost worth stating plainly, not a free win.

**Partial out-of-sample check:** the three thresholds above were picked from quartile boundaries of the *pooled* 1189-trade sample (not a true train/test split), so this is not a rigorous walk-forward validation. As a lighter sanity check, splitting the same sample chronologically (last 25% of entries, 2026-08-04 onward, as a holdout slice not weighted any differently in threshold selection) shows the filter's improvement holds up rather than evaporating out-of-sample:

| Slice | RR | Filtered win rate | Filtered net ROI |
|---|---|---|---|
| train (first 75%, n≈892) | 0.70 | 65.6% | +60.0 |
| train | 1.5 | 44.5% | +52.0 |
| **holdout** (last 25%, n=297) | 0.70 | 67.9% | +74.0 |
| **holdout** | 1.5 | 54.7% | +368.0 |

The holdout slice performs at least as well as train at every RR tested — no sign of the improvement being pure in-sample noise, though this is a single chronological split on one 120-day window, not a multi-window walk-forward. Task 6 below re-runs the full, unmodified project backtest harness (which now has both changes baked into `strategy.py`/`config.py` directly) as the final, fresh confirmation for the record.

## Global Constraints

- Every fired signal must still satisfy `strategy.valid_trade_geometry()` — this plan does not touch geometry validation.
- No new DB tables or schema migrations — `pending_setups`/`signals`' existing `miss_reason` free-text column (see `database.mark_pending_setup_missed`) already accepts any reason string.
- New config values follow the existing `os.getenv(NAME, "default")` pattern exactly (see `config.py`'s existing `ENABLE_LONG_SIGNALS`/`ENABLE_SMART_COIN_RANKING` for the boolean convention: `os.getenv(X, "true").lower() == "true"`).
- `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py` must pass after every task (per CLAUDE.md's validation convention).
- `DRY_RUN` stays as currently configured — flipping it is a deployment decision outside this plan's scope (already raised separately with the user given the server's current live losing performance).

---

## File Structure

- **Modify `config.py`** (~line 84-97): add the Priority 4 entry-quality config block; change `TP_ROI_PCT` default for Priority 5.
- **Modify `strategy.py`** (~line 303-433): add `_entry_quality_reject()` helper; wire it into `check_setup_confirmation`'s score-gate/confirmed branch; set `extra["miss_reason"]` on both reject paths.
- **Modify `main.py`** (~line 214-216): use `extra.get("miss_reason", "score_below_min")` instead of the hardcoded string, so the DB records the real reject reason.
- **Create `tests/test_entry_quality_filter.py`**: unit tests for `_entry_quality_reject` plus an integration test of `check_setup_confirmation`'s gating/miss_reason wiring.
- **Modify `tests/test_strategy_zero_lag.py`** (or wherever `build_trade_prices`/RR is already tested): add an RR-value assertion for the new 1.5:1 default.
- **Modify `CLAUDE.md`**: Key Config table rows for `TP_ROI_PCT`/RR and the new `ENTRY_QUALITY_*` values; Signal Logic section's `check_setup_confirmation` pseudocode gains the entry-quality gate.
- **Create `backtest/reports/2026-08-21-zero-lag-v1-priority4-5-120d.log`**: fresh full backtest report with both changes applied, committed as the task's evidence.

---

### Task 1: `_entry_quality_reject` helper + new config values

**Files:**
- Modify: `config.py:84` (insert after `MIN_SIGNAL_SCORE`, before the `MIN_CANDLE_SETTLE_SECONDS` comment block)
- Modify: `strategy.py:303-341` (add helper after `_entry_quality_diagnostics`)
- Test: `tests/test_entry_quality_filter.py` (new)

**Interfaces:**
- Produces: `strategy._entry_quality_reject(direction: str, diagnostics: dict[str, float]) -> str | None` — returns a `miss_reason` string if any gate fails, else `None`. Consumes the same `diagnostics` dict shape `_entry_quality_diagnostics` already returns (keys: `candle_body_atr_ratio`, `candle_range_atr_ratio`, `upper_wick_ratio`, `lower_wick_ratio`, `volume_ratio`, `distance_from_zlema_pct`).
- Produces (config): `ENABLE_ENTRY_QUALITY_FILTER: bool`, `ENTRY_QUALITY_MIN_BODY_ATR_RATIO: float`, `ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT: float`, `ENTRY_QUALITY_MAX_CHASE_WICK_RATIO: float` — consumed by Task 2.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_entry_quality_filter.py
from strategy import _entry_quality_reject


def _diag(**overrides) -> dict:
    base = {
        "candle_body_atr_ratio": 0.5,
        "candle_range_atr_ratio": 1.0,
        "upper_wick_ratio": 0.1,
        "lower_wick_ratio": 0.1,
        "volume_ratio": 1.5,
        "distance_from_zlema_pct": 0.001,
    }
    base.update(overrides)
    return base


def test_accepts_comfortable_diagnostics():
    assert _entry_quality_reject("LONG", _diag()) is None
    assert _entry_quality_reject("SHORT", _diag()) is None


def test_rejects_weak_body():
    diag = _diag(candle_body_atr_ratio=0.10)
    assert _entry_quality_reject("LONG", diag) == "entry_quality_weak_body"


def test_rejects_extended_from_zlema():
    diag = _diag(distance_from_zlema_pct=0.005)
    assert _entry_quality_reject("LONG", diag) == "entry_quality_extended_from_zlema"


def test_rejects_long_chase_wick_on_upper_wick():
    diag = _diag(upper_wick_ratio=0.6)
    assert _entry_quality_reject("LONG", diag) == "entry_quality_chase_wick"


def test_rejects_short_chase_wick_on_lower_wick():
    diag = _diag(lower_wick_ratio=0.6)
    assert _entry_quality_reject("SHORT", diag) == "entry_quality_chase_wick"


def test_short_ignores_upper_wick_and_long_ignores_lower_wick():
    # A SHORT setup with a large upper wick (not its chase-side wick) must not reject.
    assert _entry_quality_reject("SHORT", _diag(upper_wick_ratio=0.9)) is None
    assert _entry_quality_reject("LONG", _diag(lower_wick_ratio=0.9)) is None


def test_body_check_takes_priority_over_other_gates():
    # Weak body alone is enough to reject even if every other diagnostic is fine.
    diag = _diag(candle_body_atr_ratio=0.05)
    assert _entry_quality_reject("LONG", diag) == "entry_quality_weak_body"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_quality_filter.py -v`
Expected: FAIL — `ImportError: cannot import name '_entry_quality_reject'` (function doesn't exist yet).

- [ ] **Step 3: Add the config values**

In `config.py`, immediately after line 84 (`MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))`), insert:

```python

# Priority-4 entry-quality reject gate (2026-08-21 Phase 2 plan). Calibrated
# against a 1189-trade/120-day/40-symbol backtest: each threshold marks the
# worst-performing quartile of a diagnostic that _entry_quality_diagnostics
# already computes (previously observational-only, per the 2026-08-18 Plan
# C). See docs/superpowers/plans/2026-08-21-phase2-entry-quality-filter-and-rr-sizing.md
# for the full analysis, including the volume/win-rate tradeoff (rejects
# ~53% of otherwise-confirmed setups in that sample).
ENABLE_ENTRY_QUALITY_FILTER: bool = os.getenv("ENABLE_ENTRY_QUALITY_FILTER", "true").lower() == "true"
ENTRY_QUALITY_MIN_BODY_ATR_RATIO: float = float(os.getenv("ENTRY_QUALITY_MIN_BODY_ATR_RATIO", "0.20"))
ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT: float = float(os.getenv("ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT", "0.0027"))
ENTRY_QUALITY_MAX_CHASE_WICK_RATIO: float = float(os.getenv("ENTRY_QUALITY_MAX_CHASE_WICK_RATIO", "0.45"))
```

- [ ] **Step 4: Add the helper function**

In `strategy.py`, immediately after `_entry_quality_diagnostics` (after line 341, before `def check_setup_confirmation`), insert:

```python
def _entry_quality_reject(direction: str, diagnostics: dict[str, float]) -> str | None:
    """Priority-4 reject gate (2026-08-21 Phase 2 plan) -- returns a
    miss_reason string if the confirmation candle's diagnostics fail any
    calibrated threshold, else None. Backed by a 1189-trade backtest: each
    condition alone predicted materially worse win rate (see the plan doc
    for the full quartile breakdown), and the combination held up on a
    later chronological holdout slice of the same window."""
    if diagnostics["candle_body_atr_ratio"] < ENTRY_QUALITY_MIN_BODY_ATR_RATIO:
        return "entry_quality_weak_body"

    if diagnostics["distance_from_zlema_pct"] > ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT:
        return "entry_quality_extended_from_zlema"

    chase_wick = diagnostics["upper_wick_ratio"] if direction == "LONG" else diagnostics["lower_wick_ratio"]
    if chase_wick > ENTRY_QUALITY_MAX_CHASE_WICK_RATIO:
        return "entry_quality_chase_wick"

    return None
```

Add the four new names to the existing `from config import (...)` block at `strategy.py:449-457`:

```python
from config import (
    MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF,
    MACRO_KLINE_COUNT, TREND_KLINE_COUNT, PULLBACK_KLINE_COUNT, ENTRY_KLINE_COUNT,
    CANDLE_MINUTES, _TF_MINUTES,
    ZERO_LAG_LENGTH, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER, ZERO_LAG_SLOPE_LOOKBACK,
    ATR_PERIOD, PULLBACK_DISTANCE_PCT, MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS, LEVERAGE, SL_PRICE_PCT, SL_ROI_PCT, TP_PRICE_PCT, TP_ROI_PCT,
    ENABLE_LONG_SIGNALS, ENTRY_BUFFER_PCT, PENDING_EXPIRY_CANDLES,
    ENABLE_ENTRY_QUALITY_FILTER, ENTRY_QUALITY_MIN_BODY_ATR_RATIO,
    ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT, ENTRY_QUALITY_MAX_CHASE_WICK_RATIO,
)
```

Note: `_entry_quality_reject` is defined above this import line in the file (line ~342 vs. import at ~449), same as every other function in this module — fine, since Python resolves the names inside the function body at call time, not at definition time, and the import completes before any of these functions are actually invoked (identical pattern to how `MIN_SIGNAL_SCORE` is already used at line 430 despite being imported at line 454).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_quality_filter.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Compile check and commit**

Run: `python -m py_compile config.py strategy.py`

```bash
git add config.py strategy.py tests/test_entry_quality_filter.py
git commit -m "feat: add Priority-4 entry-quality reject gate (not yet wired into confirmation)"
```

---

### Task 2: Wire the gate into `check_setup_confirmation`

**Files:**
- Modify: `strategy.py:418-433` (the `confirmed`/`missed` branch)
- Test: `tests/test_entry_quality_filter.py` (extend)

**Interfaces:**
- Consumes: `_entry_quality_reject(direction, diagnostics)` from Task 1; `ENABLE_ENTRY_QUALITY_FILTER` from Task 1.
- Produces: `check_setup_confirmation` now returns `("missed", None, extra)` with `extra["miss_reason"]` set to `"score_below_min"`, `"entry_quality_weak_body"`, `"entry_quality_extended_from_zlema"`, or `"entry_quality_chase_wick"` — consumed by Task 3 (`main.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_quality_filter.py (append)
from datetime import datetime, timedelta, timezone

import strategy
from strategy import check_setup_confirmation


def _pending_breakout_setup(direction: str = "LONG", score: float = 85.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "symbol": "XRP_USDT", "direction": direction, "status": "pending_breakout",
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "score": score,
        "trigger_price": 100.0,
        "confirmation_high": 101.0, "confirmation_low": 99.0,
        "confirmation_close": 100.5, "confirmation_time": (now - timedelta(minutes=5)).isoformat(),
    }


def _breakout_candle_df():
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=95, freq="5min")
    df = pd.DataFrame({
        "open": [100.0] * 94 + [100.2],
        "high": [100.5] * 94 + [101.5],
        "low": [99.5] * 94 + [100.1],
        "close": [100.0] * 94 + [101.2],
        "volume": [1000.0] * 95,
    }, index=idx)
    return pd.concat([df, df.iloc[[-1]]])


def test_confirmed_rejected_when_entry_quality_gate_fails(monkeypatch):
    from tests.strategy_fixtures import patch_klines

    df = _breakout_candle_df()
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "_entry_quality_reject", lambda direction, diag: "entry_quality_weak_body")

    setup = _pending_breakout_setup()
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "missed"
    assert extra["miss_reason"] == "entry_quality_weak_body"


def test_confirmed_passes_when_entry_quality_gate_clears(monkeypatch):
    from tests.strategy_fixtures import patch_klines

    df = _breakout_candle_df()
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "_entry_quality_reject", lambda direction, diag: None)

    setup = _pending_breakout_setup()
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "confirmed"
    assert "miss_reason" not in extra


def test_score_below_min_still_sets_miss_reason(monkeypatch):
    from tests.strategy_fixtures import patch_klines

    df = _breakout_candle_df()
    patch_klines(monkeypatch, strategy, df)
    setup = _pending_breakout_setup(score=10.0)  # too low to clear MIN_SIGNAL_SCORE even with full breakout score

    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "missed"
    assert extra["miss_reason"] == "score_below_min"


def test_entry_quality_gate_disabled_bypasses_reject(monkeypatch):
    from tests.strategy_fixtures import patch_klines

    df = _breakout_candle_df()
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "ENABLE_ENTRY_QUALITY_FILTER", False)
    monkeypatch.setattr(strategy, "_entry_quality_reject", lambda direction, diag: "entry_quality_weak_body")

    setup = _pending_breakout_setup()
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "confirmed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entry_quality_filter.py -v`
Expected: the four new tests FAIL (gate not wired in yet — `check_setup_confirmation` always returns `"confirmed"` for a passing breakout regardless of `_entry_quality_reject`, and `extra` never has `"miss_reason"`).

- [ ] **Step 3: Wire the gate**

Replace `strategy.py:422-433`:

```python
    diagnostics = _entry_quality_diagnostics(closed, direction, trigger_price)
    final_score = round(min(100.0, float(setup["score"]) + breakout_score), 1)
    extra = {
        "score": final_score,
        "score_breakout_freshness": breakout_components["breakout_freshness"],
        "score_breakout_quality": breakout_components["breakout_quality"],
        **diagnostics,
    }
    if final_score < MIN_SIGNAL_SCORE:
        return "missed", None, extra

    return "confirmed", trigger_price, extra
```

with:

```python
    diagnostics = _entry_quality_diagnostics(closed, direction, trigger_price)
    final_score = round(min(100.0, float(setup["score"]) + breakout_score), 1)
    extra = {
        "score": final_score,
        "score_breakout_freshness": breakout_components["breakout_freshness"],
        "score_breakout_quality": breakout_components["breakout_quality"],
        **diagnostics,
    }
    if final_score < MIN_SIGNAL_SCORE:
        extra["miss_reason"] = "score_below_min"
        return "missed", None, extra

    if ENABLE_ENTRY_QUALITY_FILTER:
        reject_reason = _entry_quality_reject(direction, diagnostics)
        if reject_reason is not None:
            extra["miss_reason"] = reject_reason
            return "missed", None, extra

    return "confirmed", trigger_price, extra
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entry_quality_filter.py tests/test_score_components.py tests/test_entry_diagnostics.py tests/test_strategy_zero_lag.py -v`
Expected: PASS — including the pre-existing suites, to confirm the new gate doesn't regress the score/diagnostics/crossover behavior they cover.

- [ ] **Step 5: Compile check and commit**

Run: `python -m py_compile strategy.py`

```bash
git add strategy.py tests/test_entry_quality_filter.py
git commit -m "feat: wire the entry-quality reject gate into check_setup_confirmation"
```

---

### Task 3: Propagate the real miss reason in `main.py`

**Files:**
- Modify: `main.py:214-216`

**Interfaces:**
- Consumes: `extra["miss_reason"]` from Task 2.
- Produces: no new interface — `database.mark_pending_setup_missed`'s existing `reason` parameter now receives the accurate cause instead of always `"score_below_min"`.

- [ ] **Step 1: Make the change**

Replace `main.py:214-216`:

```python
        if status == "missed":
            db.mark_pending_setup_missed(setup["id"], reason="score_below_min", final_score=extra["score"])
            continue
```

with:

```python
        if status == "missed":
            db.mark_pending_setup_missed(
                setup["id"], reason=extra.get("miss_reason", "score_below_min"), final_score=extra["score"],
            )
            continue
```

- [ ] **Step 2: Compile check**

Run: `python -m py_compile main.py`
Expected: no output (success).

- [ ] **Step 3: Manual verification**

There is no existing `tests/test_main.py` in this project (main.py's scheduler jobs aren't unit-tested elsewhere either — `monitor_pending_setups` is only exercised live/in backtest). Read the diff back and confirm `extra` always has a `"miss_reason"` key by the time this line runs — true after Task 2, since both reject branches in `check_setup_confirmation` now set it before returning `"missed"`.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "fix: log the real entry-quality/score miss reason instead of always score_below_min"
```

---

### Task 4: Priority 5 — raise RR to 1.5:1

**Files:**
- Modify: `config.py:94`
- Test: extend an existing RR/geometry test file — use `tests/test_strategy_zero_lag.py`

**Interfaces:**
- Produces: `config.TP_ROI_PCT == 15.0`, so `TP_ROI_PCT / SL_ROI_PCT == 1.5` everywhere it's read (`main.py`'s `Signal(rr=...)`, `scripts/backtest_simple_strategy.py`'s `Trade(rr=...)`, `strategy.build_trade_prices`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_strategy_zero_lag.py (append)
def test_rr_is_one_point_five_by_default():
    import config
    assert round(config.TP_ROI_PCT / config.SL_ROI_PCT, 2) == 1.5


def test_build_trade_prices_reflects_new_rr():
    from strategy import build_trade_prices
    tp, sl = build_trade_prices("LONG", 100.0)
    tp_pct = (tp - 100.0) / 100.0
    sl_pct = (100.0 - sl) / 100.0
    assert round(tp_pct / sl_pct, 2) == 1.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_zero_lag.py -k rr -v`
Expected: FAIL — `assert 0.7 == 1.5` (current default still 7.0/10.0).

- [ ] **Step 3: Change the default**

In `config.py:94`, replace:

```python
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))   # unchanged default
```

with:

```python
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "15.0"))   # Priority-5 Phase-2 change (2026-08-21): RR 0.70->1.5, see docs/superpowers/plans/2026-08-21-phase2-entry-quality-filter-and-rr-sizing.md
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_zero_lag.py -v`
Expected: PASS (full file, to confirm no other test hardcoded the old 0.70 RR)

Also run the full suite once to catch any other hardcoded-RR assumption:

Run: `python -m pytest tests/ -v`
Expected: PASS. If anything else fails on the RR value, fix that test's expectation to 1.5 (grep the failure's assertion — any hardcoded `0.7`/`0.70`/`7.0` tied to `rr`/`TP_ROI_PCT` needs updating; this is a legitimate consequence of the intentional Priority-5 default change, not a regression).

- [ ] **Step 5: Compile check and commit**

Run: `python -m py_compile config.py`

```bash
git add config.py tests/test_strategy_zero_lag.py
git commit -m "feat: raise fixed RR from 0.70:1 to 1.5:1 (Priority 5)"
```

---

### Task 5: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the Key Config table**

In the `TP_ROI_PCT / SL_ROI_PCT` row, change `7.0 / 10.0` to `15.0 / 10.0` and update the RR note from `0.70:1` to `1.5:1`, referencing this plan doc.

Add four new rows to the Key Config table (after the `MIN_SIGNAL_SCORE` row):

```
| `ENABLE_ENTRY_QUALITY_FILTER` | true | Priority-4 Phase-2 gate (2026-08-21) — master toggle for the entry-quality reject filter below |
| `ENTRY_QUALITY_MIN_BODY_ATR_RATIO` | 0.20 | Reject if the confirmation candle's body/ATR ratio falls below this (thin, indecisive breakout bar) |
| `ENTRY_QUALITY_MAX_ZLEMA_DISTANCE_PCT` | 0.0027 | Reject if entry price is already this far past its own ZLEMA (chasing an extended move) |
| `ENTRY_QUALITY_MAX_CHASE_WICK_RATIO` | 0.45 | Reject if the confirmation candle's directional wick (upper for LONG, lower for SHORT) exceeds this fraction of its range (rejected near its extreme) |
```

- [ ] **Step 2: Update the Signal Logic pseudocode**

In the `check_setup_confirmation` pseudocode block's `pending_breakout stage` description, after the score-gate sentence (`If the final 0-100 score is still < MIN_SIGNAL_SCORE -> "missed"...`), add:

```
  Otherwise (score clears MIN_SIGNAL_SCORE), if ENABLE_ENTRY_QUALITY_FILTER,
  the confirmation candle's already-computed entry-quality diagnostics are
  checked against three calibrated thresholds (weak body-to-ATR ratio,
  over-extension past its own ZLEMA, or an oversized wick against the
  breakout direction) -- any one failing also returns "missed" (with a
  specific miss_reason instead of "score_below_min"). Otherwise ->
  "confirmed", entry = trigger_price.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for the Priority 4/5 entry-quality filter and RR change"
```

---

### Task 6: Re-run the full backtest and commit the report

**Files:**
- Create: `backtest/reports/2026-08-21-zero-lag-v1-priority4-5-120d.log`

**Interfaces:** none — this is a validation/evidence task, not a code change. `scripts/backtest_simple_strategy.py` needs no modification: it imports `strategy.py` directly, so Tasks 1-4's changes apply automatically.

- [ ] **Step 1: Run the full 120-day/40-symbol backtest**

Run (from the repo root, with the venv active):

```bash
python scripts/backtest_simple_strategy.py --from-coin-pool 40 --days 120 --workers 6 > backtest/reports/2026-08-21-zero-lag-v1-priority4-5-120d.log 2>&1
```

Expected runtime: comparable to the original 2026-08-19 baseline run (same symbol count/window) — size this to finish within a single tool call/session per the operational note in project memory (a background run of this shape has previously been killed by an unrelated scheduled-wakeup re-invocation in some environments); run it in the foreground or with a generous single-call timeout rather than backgrounding it across a wakeup boundary.

- [ ] **Step 2: Compare against both the original baseline and this plan's calibration analysis**

Read the new report's `Net ROI:`, `Win rate (win/loss):`, and `Expectancy/trade:` lines. Expected direction (not exact match, since live coin-pool ranking may have drifted again and this is a fresh, not identical, sample): net ROI substantially less negative than the 2026-08-19 baseline's -1202.9%, ideally near break-even or positive, consistent with the calibration analysis above (+52.0 pts on the 418-trade filtered/RR-1.5 subset there). If net ROI is still sharply negative, do not silently accept it — stop and re-open the calibration analysis (this plan's own numbers) rather than treating the new report as a rubber stamp.

- [ ] **Step 3: Commit the report**

```bash
git add backtest/reports/2026-08-21-zero-lag-v1-priority4-5-120d.log
git commit -m "docs: add Priority 4/5 post-change backtest report"
```

---

## Self-Review

**Spec coverage:**
- Priority 4 (entry-quality filter thresholds) — Tasks 1-2 implement the calibrated gate; Task 3 fixes reason propagation; Task 5 documents it. Covered.
- Priority 5 (RR/exit sizing) — Task 4 implements the fixed-ROI RR change to 1.5:1, the best of the tested candidates (0.70/1.0/1.2/1.3/1.5). The roadmap also mentioned "ATR-based volatility-normalized exits" as part of Priority 5 — **not covered by this plan**; the calibration analysis only tested the fixed-ROI grid explicitly named in the roadmap's comparison list. Flagged as future work, not silently dropped.
- Validation/evidence — Task 6 re-runs the existing harness for a fresh, code-driven (not spreadsheet-driven) confirmation, committed to the repo the same way the 2026-08-19 baseline was.

**Placeholder scan:** no TBD/TODO, every code step has full replacement text or full new function bodies, every test has real assertions and concrete threshold values (0.20, 0.0027, 0.45, 1.5) rather than "an appropriate value."

**Type consistency:** `_entry_quality_reject(direction: str, diagnostics: dict[str, float]) -> str | None` is used identically in Task 1's unit tests, Task 2's integration tests, and the wiring in `check_setup_confirmation`. `extra["miss_reason"]` is produced in Task 2 and consumed in Task 3 with matching semantics (a free-text string, matching `database.mark_pending_setup_missed`'s existing `reason: str` parameter — no new type needed).
