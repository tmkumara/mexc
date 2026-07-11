# SL Widen + Breakeven Trail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen `MAX_SL_PRICE_PCT` and add an actionable breakeven-trail mechanism (Telegram alert + internal tracking) to the Liquidation-Aware 1m Scalp strategy's outcome checker, per `docs/superpowers/specs/2026-07-11-sl-widen-breakeven-trail-design.md`.

**Architecture:** A new pure module `outcome_replay.py` (mirrors the existing `liq_estimator.py` convention: explicit params, no `import config`) replays a signal's candle history and returns whether/how it resolved, including breakeven-trigger detection. `main.py`'s `check_outcomes` becomes the wiring layer: it fetches candles (unchanged), calls `outcome_replay.replay_outcome`, persists a new `breakeven_triggered_at` DB column via a new `database.py` function, and sends a new Telegram notification via a new `bot.py` function. `strategy.py` is untouched — signal generation, entry, initial TP/SL are unaffected.

**Tech Stack:** Python 3.10, pandas (already a dependency) — no new dependencies.

## Global Constraints

- This plan implements `docs/superpowers/specs/2026-07-11-sl-widen-breakeven-trail-design.md` with **one deliberate deviation**: the spec described the replay logic as a private function (`_replay_outcome`) living directly in `main.py`. This plan instead puts it in a **new pure module `outcome_replay.py`**, with a public function `replay_outcome(...)` (no underscore — it's called cross-module). Reason: `main.py` runs `_backup_log_on_startup()` at import time (module-level code, not inside a function guarded by `if __name__ == "__main__"`), which truncates `mexc_bot.log` if it has content. The user's bot is running live right now, actively writing to that exact log file — any test file that does `from main import _replay_outcome` would trigger that truncation as a side effect of merely importing the module for testing, corrupting the live bot's log. Putting the pure logic in its own side-effect-free module avoids this entirely and matches the repo's established pattern of pure math in dedicated modules (`liq_estimator.py`, and historically `volume_profile.py`/`order_blocks.py`). Behavior and every design decision from the spec (same-bar tiebreak, existing-trigger handling, breakeven ROI calc, Telegram wording) are otherwise implemented exactly as specified.
- `outcome_replay.py` takes all parameters explicitly (`breakeven_trigger_pct` included) — no `import config` inside it, consistent with `liq_estimator.py`'s convention. `main.py` is the wiring layer that reads `config.BREAKEVEN_TRIGGER_PCT` and passes it in.
- **Do not touch the live `signals.db`** during implementation/testing. The user's bot is running against it right now. Task 2's DB-migration verification must run against a throwaway temp SQLite file (via `database.DB_PATH` reassignment), never the real `signals.db`. Do not run `python main.py` or anything that calls `database.init_db()` against the real DB as part of this plan's tasks — the actual migration only needs to run once, naturally, when the user restarts their own already-running bot process after this plan lands (that restart is the user's action, not this plan's).
- `strategy.py`, `liq_estimator.py`, `webui.py`, `reports.py`, `coin_scanner.py`, `mexc_client.py`, `coin_scanner.py`, and all WS-related modules are untouched. `reports.py` needs zero changes — a breakeven-scratch is stored as `status='loss'` with `pnl_roi≈0`, which `reports.py`'s existing `status in ("win","loss","pending","expired")` bucketing already handles with no code change (confirmed by reading `reports.py` during design).
- `bot.py`'s existing functions (`format_signal`, `broadcast_signal`, `notify_outcome`, all `cmd_*` handlers) are untouched — only one new function (`notify_breakeven_trigger`) and one import-line addition.
- No new dependencies (pandas is already in `requirements.txt`).

---

### Task 1: `config.py` + `.env.example` — widen SL, add breakeven trigger constant

**Files:**
- Modify: `config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `MAX_SL_PRICE_PCT` (existing name, new default `0.0045`), `BREAKEVEN_TRIGGER_PCT: float` (new, default `0.5`) — consumed by Task 5 (`main.py`).

- [ ] **Step 1: Widen `MAX_SL_PRICE_PCT` and add `BREAKEVEN_TRIGGER_PCT`**

In `config.py`, find:

```python
# ── Profit target / risk (price move = margin target / leverage) ───
TARGET_MARGIN_PROFIT: float  = float(os.getenv("TARGET_MARGIN_PROFIT", "0.12"))
MIN_RR: float                 = float(os.getenv("MIN_RR", "1.5"))
MAX_SL_PRICE_PCT: float       = float(os.getenv("MAX_SL_PRICE_PCT", "0.0032"))
```

Replace with:

```python
# ── Profit target / risk (price move = margin target / leverage) ───
TARGET_MARGIN_PROFIT: float  = float(os.getenv("TARGET_MARGIN_PROFIT", "0.12"))
MIN_RR: float                 = float(os.getenv("MIN_RR", "1.5"))
MAX_SL_PRICE_PCT: float       = float(os.getenv("MAX_SL_PRICE_PCT", "0.0045"))
BREAKEVEN_TRIGGER_PCT: float  = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.5"))
```

- [ ] **Step 2: Verify `config.py` imports cleanly**

Run: `python -c "import config; print(config.MAX_SL_PRICE_PCT, config.BREAKEVEN_TRIGGER_PCT)"`
Expected: prints `0.0045 0.5` with no traceback.

- [ ] **Step 3: Update `.env.example`**

Find:

```
# Liquidation-Aware 1m Scalp (v14) tuning -- see config.py for full defaults
TARGET_MARGIN_PROFIT=0.12
MIN_RR=1.5
MAX_SL_PRICE_PCT=0.0032
LEVERAGE_TIERS=10:0.20,20:0.25,25:0.20,50:0.20,75:0.10,100:0.05
```

Replace with:

```
# Liquidation-Aware 1m Scalp (v14) tuning -- see config.py for full defaults
TARGET_MARGIN_PROFIT=0.12
MIN_RR=1.5
MAX_SL_PRICE_PCT=0.0045
BREAKEVEN_TRIGGER_PCT=0.5
LEVERAGE_TIERS=10:0.20,20:0.25,25:0.20,50:0.20,75:0.10,100:0.05
```

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "feat: widen MAX_SL_PRICE_PCT to 0.45%, add BREAKEVEN_TRIGGER_PCT"
```

---

### Task 2: `database.py` — breakeven-trigger column + tracking function

**Files:**
- Modify: `database.py`

**Interfaces:**
- Produces: `mark_signal_breakeven_triggered(signal_id: int, triggered_at: datetime) -> None`. `signals` rows gain a nullable `breakeven_triggered_at TEXT` column, returned automatically by the existing `get_pending_signals()`/`get_signals_in_range()`/`get_all_signals()` (all do `SELECT *`).
- Consumed by: Task 5 (`main.py`).

- [ ] **Step 1: Add the migration column**

In `database.py`'s `init_db()`, find:

```python
        for col, definition in [
            ("placed",    "INTEGER NOT NULL DEFAULT 1"),
            ("placed_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass
```

Replace with:

```python
        for col, definition in [
            ("placed",    "INTEGER NOT NULL DEFAULT 1"),
            ("placed_at", "TEXT"),
            ("breakeven_triggered_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass
```

- [ ] **Step 2: Add `mark_signal_breakeven_triggered`**

Append to `database.py` (after `update_signal_outcome`):

```python
def mark_signal_breakeven_triggered(signal_id: int, triggered_at: datetime) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET breakeven_triggered_at = ?
            WHERE id = ? AND breakeven_triggered_at IS NULL
        """, (triggered_at.isoformat(), signal_id))
```

- [ ] **Step 3: Verify against a throwaway temp DB (NEVER the real `signals.db`)**

Run (this uses a temp file and monkeypatches `database.DB_PATH` in-process; it never touches the live `signals.db` the user's running bot uses):

```bash
python - <<'EOF'
import os, tempfile
from datetime import datetime

import database

tmp_path = os.path.join(tempfile.gettempdir(), "test_migration_signals.db")
if os.path.exists(tmp_path):
    os.remove(tmp_path)
database.DB_PATH = tmp_path

database.init_db()

sig_id = database.save_signal(
    symbol="TEST_USDT", direction="LONG", entry_price=100.0,
    tp_price=110.0, sl_price=95.0, leverage=20,
    generated_at=datetime.now(),
)

row = database.get_pending_signals()[0]
assert row["breakeven_triggered_at"] is None, f"expected NULL, got {row['breakeven_triggered_at']!r}"

t1 = datetime(2026, 1, 1, 12, 0, 0)
database.mark_signal_breakeven_triggered(sig_id, t1)
row = database.get_pending_signals()[0]
assert row["breakeven_triggered_at"] == t1.isoformat(), f"expected {t1.isoformat()!r}, got {row['breakeven_triggered_at']!r}"

# idempotency guard: a second call must NOT overwrite the first timestamp
t2 = datetime(2026, 1, 1, 13, 0, 0)
database.mark_signal_breakeven_triggered(sig_id, t2)
row = database.get_pending_signals()[0]
assert row["breakeven_triggered_at"] == t1.isoformat(), "second call must not overwrite the first trigger timestamp"

print("ALL DB MIGRATION CHECKS PASSED")
os.remove(tmp_path)
EOF
```

Expected: prints `ALL DB MIGRATION CHECKS PASSED`, no assertion errors, no traceback. Confirm afterward that `signals.db` (the real one, in the repo root) was not modified: `git status` / file mtime should show no change to it, since this script only ever touched the temp path.

- [ ] **Step 4: Commit**

```bash
git add database.py
git commit -m "feat: add breakeven_triggered_at column + mark_signal_breakeven_triggered"
```

---

### Task 3: `bot.py` — breakeven-trigger Telegram notification

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Produces: `async def notify_breakeven_trigger(app: Application, signal_db: dict, closing_now: bool = False) -> None`.
- Consumed by: Task 5 (`main.py`).

- [ ] **Step 1: Add the `BREAKEVEN_TRIGGER_PCT` import**

Find:

```python
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT, STRATEGY_NAME
```

Replace with:

```python
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT, STRATEGY_NAME, BREAKEVEN_TRIGGER_PCT
```

- [ ] **Step 2: Add `notify_breakeven_trigger`**

Append to `bot.py` immediately after `notify_outcome` (i.e., right after the function whose last line is `await _send_html(app, msg)` at the end of the "signal formatting" section, before the `# ── commands ──` section header):

```python
async def notify_breakeven_trigger(app: Application, signal_db: dict, closing_now: bool = False) -> None:
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    entry     = signal_db["entry_price"]
    arrow     = "🟢" if direction == "LONG" else "🔴"

    lines = [
        f"🔒 {_bold('Breakeven Trigger')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Price reached {int(BREAKEVEN_TRIGGER_PCT * 100)}% to target — move your stop to breakeven now.",
        f"Breakeven: {_code(f'{entry:,.6g}')}",
    ]
    if closing_now:
        lines.append(_italic("Note: this trade has already closed at breakeven by the time this alert was checked."))
    lines.append(f"🆔 ID: {_code(signal_db['id'])}")

    await _send_html(app, "\n".join(lines))
```

- [ ] **Step 3: Compile-check**

Run: `python -m py_compile bot.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: add notify_breakeven_trigger Telegram notification"
```

---

### Task 4: `outcome_replay.py` — pure breakeven-aware outcome replay logic

**Files:**
- Create: `outcome_replay.py`
- Create: `tests/test_outcome_replay.py`

**Interfaces:**
- Produces: `replay_outcome(direction: str, entry_price: float, tp_price: float, sl_price: float, df: pd.DataFrame, entry_candle_cutoff, existing_trigger_ts, breakeven_trigger_pct: float) -> tuple[str | None, "pd.Timestamp | None", bool]` — `(outcome, newly_triggered_at, closed_at_breakeven)`.
- Consumed by: Task 5 (`main.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outcome_replay.py`:

```python
from datetime import datetime, timedelta

import pandas as pd

from outcome_replay import replay_outcome

START = datetime(2026, 1, 1, 0, 0, 0)
CUTOFF = START - timedelta(minutes=1)
TRIGGER_PCT = 0.5


def _build_df(rows: list[tuple[float, float, float, float]]) -> tuple[pd.DataFrame, list[datetime]]:
    """rows: (open, high, low, close) for the real candles under test.
    Appends one extra trailing row that replay_outcome never evaluates
    (mirrors the in-progress bar the real caller also excludes via len(df)-1)."""
    timestamps = [START + timedelta(minutes=i) for i in range(len(rows) + 1)]
    all_rows = rows + [rows[-1]]
    df = pd.DataFrame(all_rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(timestamps))
    return df, timestamps[:-1]


def test_normal_tp_hit_no_breakeven():
    rows = [
        (100.0, 102.0, 99.0, 101.0),
        (104.0, 111.0, 104.0, 110.0),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "win"
    assert triggered is None
    assert closed_at_be is False


def test_normal_sl_hit_no_breakeven():
    rows = [
        (100.0, 101.0, 98.0, 99.0),
        (99.0, 100.0, 94.0, 95.0),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is False


def test_breakeven_triggers_then_closes_at_breakeven():
    rows = [
        (104.0, 106.0, 103.0, 105.0),   # reaches trigger (105) -- no TP(110) or original SL(95) hit
        (100.0, 101.0, 99.0, 99.5),     # active SL now breakeven (100) -- low 99 <= 100 -> stopped at breakeven
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered == ts[0]
    assert closed_at_be is True


def test_breakeven_triggers_then_hits_real_tp():
    rows = [
        (104.0, 106.0, 103.0, 105.0),   # reaches trigger
        (102.0, 112.0, 101.0, 111.0),   # goes on to hit real TP (110)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "win"
    assert triggered == ts[0]
    assert closed_at_be is False


def test_same_candle_tiebreak_original_sl_wins():
    rows = [
        (98.0, 106.0, 94.0, 96.0),   # single candle crosses BOTH original SL (95) and trigger (105)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is False


def test_existing_trigger_from_prior_tick_applies_only_after_its_candle():
    rows = [
        (101.0, 106.0, 99.0, 104.0),   # the historical trigger candle -- must still resolve
                                        # against the ORIGINAL sl (95), not breakeven, even
                                        # though its own timestamp equals existing_trigger_ts
        (100.5, 101.0, 99.5, 99.8),    # after the trigger candle -- active SL is breakeven (100)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, ts[0], TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is True


def test_still_pending_returns_all_none():
    rows = [
        (100.0, 101.0, 99.0, 100.5),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome is None
    assert triggered is None
    assert closed_at_be is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_outcome_replay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'outcome_replay'`

- [ ] **Step 3: Implement `outcome_replay.py`**

```python
"""
Breakeven-aware outcome replay for pending signals.

Replays every closed candle since a signal's entry against its TP/SL,
applying a single-step breakeven trail: once price reaches
breakeven_trigger_pct of the way from entry to TP, the ACTIVE stop for
all SUBSEQUENT candles becomes entry_price (breakeven) instead of the
original sl_price. The candle where the trigger is reached is itself
still evaluated against the ORIGINAL stop -- if a single candle's range
would hit both the original SL and the trigger level, the original SL
takes precedence for that candle, since intra-bar ordering can't be
determined from OHLC data alone.

Caller (main.py's check_outcomes) is responsible for: fetching the
candle DataFrame, persisting a newly-detected trigger timestamp, and
sending the breakeven Telegram notification.
"""

from __future__ import annotations

import pandas as pd


def replay_outcome(
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
    existing_trigger_ts,
    breakeven_trigger_pct: float,
) -> tuple[str | None, "pd.Timestamp | None", bool]:
    """
    Returns (outcome, newly_triggered_at, closed_at_breakeven):
      outcome            -- "win" | "loss" | None (still pending)
      newly_triggered_at -- candle timestamp if the breakeven trigger
                            fired during THIS call and existing_trigger_ts
                            was None, else None
      closed_at_breakeven -- True if outcome == "loss" and the stop that
                             was hit was the breakeven price (entry_price),
                             not the original sl_price
    """
    trigger_price = entry_price + breakeven_trigger_pct * (tp_price - entry_price)
    trigger_ts = existing_trigger_ts
    newly_triggered_at = None

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high  = float(df["high"].iloc[i])
        low   = float(df["low"].iloc[i])
        open_ = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])

        active_sl = entry_price if (trigger_ts is not None and ts > trigger_ts) else sl_price

        hit_tp = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        hit_sl = (low <= active_sl) if direction == "LONG" else (high >= active_sl)

        if hit_tp and hit_sl:
            outcome = "win" if (
                (direction == "LONG"  and close >= open_) or
                (direction == "SHORT" and close <= open_)
            ) else "loss"
            return outcome, newly_triggered_at, (outcome == "loss" and active_sl == entry_price)

        if hit_tp:
            return "win", newly_triggered_at, False

        if hit_sl:
            return "loss", newly_triggered_at, (active_sl == entry_price)

        if trigger_ts is None:
            reached = (high >= trigger_price) if direction == "LONG" else (low <= trigger_price)
            if reached:
                trigger_ts = ts
                newly_triggered_at = ts

    return None, newly_triggered_at, False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_outcome_replay.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add outcome_replay.py tests/test_outcome_replay.py
git commit -m "feat: add outcome_replay.py with breakeven-trail replay logic"
```

---

### Task 5: `main.py` — wire `check_outcomes` to the breakeven trail

**Files:**
- Modify: `main.py`

**Interfaces:**
- Consumes: `outcome_replay.replay_outcome` (Task 4), `database.mark_signal_breakeven_triggered` (Task 2), `bot.notify_breakeven_trigger` (Task 3), `config.BREAKEVEN_TRIGGER_PCT` (Task 1).
- `scan_and_fire_signals`, `_replay_outcome`'s call site aside, no other part of `main.py` changes.

- [ ] **Step 1: Add the new imports**

Find (near the top of `main.py`):

```python
import asyncio
import logging
import shutil
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date
```

Replace with:

```python
import asyncio
import logging
import shutil
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date

import pandas as pd
```

Find:

```python
import database as db
import strategy
import bot as tg
import coin_scanner
from mexc_client import get_klines
```

Replace with:

```python
import database as db
import strategy
import bot as tg
import coin_scanner
from mexc_client import get_klines
from outcome_replay import replay_outcome
```

Find the `from config import (` block's opening (do not change any existing names, just add one):

```python
from config import (
    LKT,
    LEVERAGE,
    SCALP_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
```

Replace with:

```python
from config import (
    LKT,
    LEVERAGE,
    SCALP_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    BREAKEVEN_TRIGGER_PCT,
```

- [ ] **Step 2: Rewrite the candle-scanning body of `check_outcomes`**

Find:

```python
        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)
        outcome = None

        for i in range(len(df) - 1):
            if df.index[i] <= entry_candle_cutoff:
                continue
            high  = float(df["high"].iloc[i])
            low   = float(df["low"].iloc[i])
            open_ = float(df["open"].iloc[i])
            close = float(df["close"].iloc[i])

            hit_tp = (high >= tp_price) if direction == "LONG" else (low  <= tp_price)
            hit_sl = (low  <= sl_price) if direction == "LONG" else (high >= sl_price)

            if hit_tp and hit_sl:
                outcome = "win" if (
                    (direction == "LONG"  and close >= open_) or
                    (direction == "SHORT" and close <= open_)
                ) else "loss"
                break
            if hit_tp:
                outcome = "win"
                break
            if hit_sl:
                outcome = "loss"
                break

        if outcome is None:
            continue

        pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp_price, sl_price)
        db.update_signal_outcome(sig["id"], outcome, pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), symbol, pnl)

        try:
            await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
        except Exception as e:
            logger.error("Failed to notify %s for %s: %s", outcome, symbol, e)
```

Replace with:

```python
        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        existing_trigger_ts = None
        if sig.get("breakeven_triggered_at"):
            # Stored via triggered_at.isoformat() where triggered_at is a
            # naive pandas Timestamp (df.index[i] -- kline timestamps are
            # naive throughout this codebase, same convention as
            # entry_candle_cutoff above), so this parses back naive with
            # no tz_localize needed.
            existing_trigger_ts = pd.Timestamp(sig["breakeven_triggered_at"])

        outcome, newly_triggered_at, closed_at_breakeven = replay_outcome(
            direction, entry_price, tp_price, sl_price,
            df, entry_candle_cutoff, existing_trigger_ts, BREAKEVEN_TRIGGER_PCT,
        )

        if newly_triggered_at is not None and existing_trigger_ts is None:
            db.mark_signal_breakeven_triggered(sig["id"], newly_triggered_at)
            try:
                await tg.notify_breakeven_trigger(app, sig, closing_now=(outcome is not None))
            except Exception as e:
                logger.error("Failed to notify breakeven trigger for %s: %s", symbol, e)

        if outcome is None:
            continue

        effective_sl_for_pnl = entry_price if closed_at_breakeven else sl_price
        pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp_price, effective_sl_for_pnl)
        db.update_signal_outcome(sig["id"], outcome, pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), symbol, pnl)

        try:
            await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
        except Exception as e:
            logger.error("Failed to notify %s for %s: %s", outcome, symbol, e)
```

- [ ] **Step 3: Compile-check**

Run: `python -m py_compile main.py`
Expected: no output, exit code 0.

Note: do NOT run `python -c "import main"` or `python main.py` as part of this task's verification — `main.py` has module-level side effects (`_backup_log_on_startup()`, which truncates `mexc_bot.log`) that must not run while the user's own bot instance is live and writing to that file. `py_compile` (syntax-only, does not execute module-level code) is the correct and sufficient check here.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire check_outcomes to the breakeven-trail replay logic"
```

---

### Task 6: Full verification + reminders

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass — the pre-existing 20 (`test_liq_estimator.py`, `test_mexc_client.py`, `test_strategy_liq_scalp.py`) plus the 7 new ones in `test_outcome_replay.py` (27 total).

- [ ] **Step 2: Compile-check every module touched or adjacent**

Run: `python -m py_compile config.py database.py bot.py outcome_replay.py main.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Confirm the live `signals.db` was not touched by this plan's work**

Run: `git status signals.db` (or check its mtime) — should show no modification from this plan's tasks. All DB testing happened against a temp file per Task 2.

- [ ] **Step 4: Final reminders to surface to the user**

Report these explicitly when the plan is done:
1. **Restart your locally-running bot** to pick up these changes — the new `MAX_SL_PRICE_PCT` default, the `BREAKEVEN_TRIGGER_PCT` constant, and the `breakeven_triggered_at` DB migration only take effect on the next `python main.py` startup (`init_db()` runs the `ALTER TABLE` migration automatically and safely against the existing `signals.db` — it's additive, matches the same pattern already used for the pre-existing `placed`/`placed_at` columns).
2. The two already-closed trades (`ONDO_USDT`, `LTC_USDT`) are historical — this change only affects signals that fire *after* the restart.
3. Watch for the new "🔒 Breakeven Trigger" Telegram message the next time a trade runs 50% of the way to its target — that's your cue to manually move your real stop-loss to breakeven on the exchange.
4. If this later gets pushed to the server, no new secrets are needed (no new `.env` variables beyond `BREAKEVEN_TRIGGER_PCT`, which has a safe default and doesn't strictly need to be in the `APP_ENV` GitHub secret unless you want to override it there too).
