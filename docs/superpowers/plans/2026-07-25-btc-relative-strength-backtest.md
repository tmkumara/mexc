# BTC Relative-Strength Continuation Backtest (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a backtest-only prototype testing whether BTC relative-strength continuation (alts that are outperforming/underperforming BTC over a lookback window tend to keep doing so) has any real edge, across a 27-combination parameter grid, on the 8 symbols/6 months of 5m data already fetched in `backtest/data/`.

**Architecture:** A pure signal-computation module (`backtest/relative_strength.py`, unit-tested) separated from a runner script (`backtest/rs_continuation_backtest.py`) that loads data, sweeps the parameter grid, simulates trades with the same no-lookahead/SL-first-tie-break conventions as `backtest/engine.py`, and prints an aggregate results table.

**Tech Stack:** Python, pandas, pytest (existing repo stack — no new dependencies).

## Global Constraints

- Universe: the 8 symbols already fetched — `backtest/data/{1000BONK,BTC,ENA,ETH,SOL,WLD,XPL,XRP}_USDT_5m.parquet`. No new data fetch.
- Entry timeframe: 5m (native resolution of the fetched data — no resampling needed for this strategy, unlike the 15m Hull backtests).
- Sizing: 20x leverage, flat TP/SL at 10% ROI each way (`TP_PCT = 0.10 / 20 = 0.005` price move, `SL_PCT = 0.10 / 20 = 0.005` price move — matches current live v3 `TARGET_ROI_PCT`/`MAX_SL_ROI_PCT` defaults, for direct comparability to the PF≈1.02 baseline).
- SL-first same-bar tie-break (matches `outcome_check.check_tp_sl` and `backtest/engine.py` conventions).
- One position at a time per symbol (no pyramiding).
- No lookahead: signal computed as of a bar's CLOSE; entry fills at the next bar's OPEN.
- Parameter grid: `lookback_bars ∈ {12, 24, 48}` (1h/2h/4h at 5m resolution) × `move_threshold_pct ∈ {0.5, 1.0, 2.0}` × `rs_threshold_pct ∈ {1.0, 2.0, 3.0}` = 27 combinations.
- Success bar (from the spec): profit factor clearly and consistently > 1.0 across multiple combinations, with 200+ aggregate trades for the best combination — not one cherry-picked result.

---

### Task 1: Pure relative-strength signal function + unit tests

**Files:**
- Create: `backtest/relative_strength.py`
- Test: `tests/test_relative_strength.py`

**Interfaces:**
- Produces: `compute_returns(close: pd.Series, lookback_bars: int) -> pd.Series` — % return over the lookback window ending at each bar.
- Produces: `rs_signal(btc_return: float, alt_return: float, move_threshold_pct: float, rs_threshold_pct: float) -> str | None` — returns `"LONG"`, `"SHORT"`, or `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_relative_strength.py`:

```python
"""Unit tests for backtest/relative_strength.py -- pure functions, no network, no real data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import numpy as np
import pandas as pd

from backtest.relative_strength import compute_returns, rs_signal


def test_compute_returns_basic():
    close = pd.Series([100.0, 101.0, 102.0, 105.0])
    result = compute_returns(close, lookback_bars=2)
    # bar 2: (102-100)/100*100 = 2.0%   bar 3: (105-101)/101*100 ~= 3.9604%
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest_approx(2.0)
    assert result.iloc[3] == pytest_approx((105.0 - 101.0) / 101.0 * 100.0)


def pytest_approx(x, tol=1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - x) < tol
    return _Approx()


def test_rs_signal_long_when_btc_up_and_alt_outperforms():
    # BTC +2% (above 1% move threshold), alt +5% (rs_score=3%, above 1% rs threshold)
    assert rs_signal(btc_return=2.0, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) == "LONG"


def test_rs_signal_short_when_btc_down_and_alt_underperforms():
    # BTC -2%, alt -5% (rs_score=-3%, below -1% rs threshold)
    assert rs_signal(btc_return=-2.0, alt_return=-5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) == "SHORT"


def test_rs_signal_none_when_btc_move_too_small():
    # BTC only +0.2%, below the 1% move threshold -- no signal regardless of alt
    assert rs_signal(btc_return=0.2, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_when_alt_not_outperforming_enough():
    # BTC +2% (passes move threshold), alt +2.5% -- rs_score=0.5%, below 1% rs threshold
    assert rs_signal(btc_return=2.0, alt_return=2.5, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_when_alt_underperforms_during_btc_uptrend():
    # BTC +2%, alt only +1% -- rs_score=-1%, negative, but BTC is UP so LONG needs positive rs_score
    assert rs_signal(btc_return=2.0, alt_return=1.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_on_nan_inputs():
    assert rs_signal(btc_return=float("nan"), alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
    assert rs_signal(btc_return=2.0, alt_return=float("nan"), move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_boundary_is_exclusive():
    # Exactly AT the move threshold should not qualify (require strictly greater)
    assert rs_signal(btc_return=1.0, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
    # Exactly AT the rs threshold should not qualify
    assert rs_signal(btc_return=2.0, alt_return=3.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:\Test\personal\mexc && python -m pytest tests/test_relative_strength.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backtest.relative_strength'`

- [ ] **Step 3: Write the minimal implementation**

Create `backtest/relative_strength.py`:

```python
"""
Pure functions for the BTC relative-strength continuation backtest (Phase
1 -- see docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md).

No lookahead: compute_returns is a plain causal pct_change (each bar's
value only depends on bars at or before it). rs_signal is a pure function
of two already-computed return values -- callers are responsible for
passing btc_return/alt_return as of the same bar's CLOSE, then filling
any resulting entry at the NEXT bar's OPEN (see
backtest/rs_continuation_backtest.py for the walk-forward loop that does
this).
"""

from __future__ import annotations

import pandas as pd


def compute_returns(close: pd.Series, lookback_bars: int) -> pd.Series:
    """% return over the lookback window, ending at each bar (causal -- pandas
    pct_change only looks backward, never forward)."""
    return close.pct_change(lookback_bars) * 100.0


def rs_signal(
    btc_return: float,
    alt_return: float,
    move_threshold_pct: float,
    rs_threshold_pct: float,
) -> str | None:
    """Given BTC's and one alt's cumulative % return over the same lookback
    window, return "LONG", "SHORT", or None.

    LONG:  BTC moved up more than move_threshold_pct AND the alt's excess
           return over BTC (rs_score) exceeds rs_threshold_pct.
    SHORT: BTC moved down more than move_threshold_pct AND the alt's excess
           return over BTC is below -rs_threshold_pct.
    Thresholds are exclusive (strictly greater/less than) so a bar sitting
    exactly on a threshold does not qualify.
    """
    if pd.isna(btc_return) or pd.isna(alt_return):
        return None

    rs_score = alt_return - btc_return

    if btc_return > move_threshold_pct and rs_score > rs_threshold_pct:
        return "LONG"
    if btc_return < -move_threshold_pct and rs_score < -rs_threshold_pct:
        return "SHORT"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:\Test\personal\mexc && python -m pytest tests/test_relative_strength.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Compile-check and commit**

Run: `python -m py_compile backtest/relative_strength.py`
Expected: no output (success)

```bash
git add backtest/relative_strength.py tests/test_relative_strength.py
git commit -m "feat: add BTC relative-strength signal function + unit tests

Pure compute_returns/rs_signal functions for the Phase 1 relative-strength
continuation backtest (see docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md).
No production wiring -- backtest-only per the spec's scope."
```

---

### Task 2: Backtest runner — parameter grid sweep across all 8 symbols

**Files:**
- Create: `backtest/rs_continuation_backtest.py`

**Interfaces:**
- Consumes: `backtest.relative_strength.compute_returns(close: pd.Series, lookback_bars: int) -> pd.Series` and `backtest.relative_strength.rs_signal(btc_return, alt_return, move_threshold_pct, rs_threshold_pct) -> str | None` from Task 1.
- Consumes: parquet files at `backtest/data/{SYMBOL}_5m.parquet` (columns: `open, high, low, close, volume`, indexed by UTC timestamp) — same format `backtest/engine.py`/`backtest/optimize.py` already read.

This task has no unit tests of its own (it's a data-driven runner script, validated by actually running it against real data and checking the output is well-formed — same convention as `backtest/tpsl_scan.py` and every ad-hoc sweep script used earlier this session, none of which have dedicated test files; the *logic* it depends on is already unit-tested in Task 1).

- [ ] **Step 1: Write the runner script**

Create `backtest/rs_continuation_backtest.py`:

```python
"""
Phase 1 backtest: BTC relative-strength continuation. See
docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md
for the full design and success criteria.

Sweeps a 27-combination grid (lookback_bars x move_threshold_pct x
rs_threshold_pct) across the 8 already-fetched symbols, using the same
20x/10%-ROI-TP/10%-ROI-SL flat sizing as the current live v3 strategy, so
any edge found is attributable to the entry signal rather than a
favorable TP:SL ratio. SL-first same-bar tie-break, one position at a
time per symbol, no lookahead (signal as of a bar's close, entry at the
next bar's open) -- matches backtest/engine.py's conventions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.relative_strength import compute_returns, rs_signal

DATA_DIR = Path(__file__).resolve().parent / "data"

LEVERAGE = 20
TP_PCT = 0.10 / LEVERAGE  # 10% ROI target -> 0.5% price move
SL_PCT = 0.10 / LEVERAGE  # 10% ROI stop   -> 0.5% price move

LOOKBACK_BARS = [12, 24, 48]          # 1h, 2h, 4h at 5m resolution
MOVE_THRESHOLDS = [0.5, 1.0, 2.0]     # BTC's own move, percent
RS_THRESHOLDS = [1.0, 2.0, 3.0]       # excess return required, percent


def load_symbol(symbol: str) -> pd.DataFrame:
    return pd.read_parquet(DATA_DIR / f"{symbol}_5m.parquet")


def simulate(
    alt_close: pd.Series,
    alt_high: pd.Series,
    alt_low: pd.Series,
    alt_open: pd.Series,
    btc_returns: pd.Series,
    alt_returns: pd.Series,
    move_threshold_pct: float,
    rs_threshold_pct: float,
) -> list[dict]:
    """Walk forward bar by bar, open at most one position at a time,
    SL-first same-bar tie-break. Returns a list of closed-trade dicts."""
    n = len(alt_close)
    trades: list[dict] = []
    open_until = -1

    for i in range(n - 1):
        if i <= open_until:
            continue

        direction = rs_signal(
            btc_return=btc_returns.iloc[i],
            alt_return=alt_returns.iloc[i],
            move_threshold_pct=move_threshold_pct,
            rs_threshold_pct=rs_threshold_pct,
        )
        if direction is None:
            continue

        entry_idx = i + 1
        entry_price = float(alt_open.iloc[entry_idx])

        if direction == "LONG":
            tp_price = entry_price * (1 + TP_PCT)
            sl_price = entry_price * (1 - SL_PCT)
        else:
            tp_price = entry_price * (1 - TP_PCT)
            sl_price = entry_price * (1 + SL_PCT)

        exit_price = None
        exit_reason = None
        j_final = n - 1
        for j in range(entry_idx, n):
            high = float(alt_high.iloc[j])
            low = float(alt_low.iloc[j])
            if direction == "LONG":
                hit_sl = low <= sl_price
                hit_tp = high >= tp_price
            else:
                hit_sl = high >= sl_price
                hit_tp = low <= tp_price
            if hit_sl:
                exit_price, exit_reason, j_final = sl_price, "sl", j
                break
            if hit_tp:
                exit_price, exit_reason, j_final = tp_price, "tp", j
                break

        if exit_price is None:
            open_until = i  # never resolved within available data
            continue

        roi_pct = (
            (exit_price - entry_price) / entry_price * LEVERAGE * 100.0
            if direction == "LONG"
            else (entry_price - exit_price) / entry_price * LEVERAGE * 100.0
        )
        trades.append({"direction": direction, "roi_pct": roi_pct, "win": exit_reason == "tp"})
        open_until = j_final

    return trades


def main():
    symbols = sorted(p.stem.replace("_5m", "") for p in DATA_DIR.glob("*_5m.parquet"))
    if "BTC_USDT" not in symbols:
        print("BTC_USDT data not found in backtest/data/ -- cannot compute relative strength.")
        return
    alt_symbols = [s for s in symbols if s != "BTC_USDT"]

    btc_df = load_symbol("BTC_USDT")
    alt_dfs = {s: load_symbol(s) for s in alt_symbols}

    print(f"Symbols: BTC_USDT (reference) + {alt_symbols}\n")

    results = []
    for lookback_bars in LOOKBACK_BARS:
        btc_returns = compute_returns(btc_df["close"], lookback_bars)
        for move_threshold_pct in MOVE_THRESHOLDS:
            for rs_threshold_pct in RS_THRESHOLDS:
                all_trades = []
                for sym, df in alt_dfs.items():
                    aligned_btc = btc_returns.reindex(df.index, method="ffill")
                    alt_returns = compute_returns(df["close"], lookback_bars)
                    trades = simulate(
                        alt_close=df["close"], alt_high=df["high"], alt_low=df["low"], alt_open=df["open"],
                        btc_returns=aligned_btc, alt_returns=alt_returns,
                        move_threshold_pct=move_threshold_pct, rs_threshold_pct=rs_threshold_pct,
                    )
                    all_trades.extend(trades)

                n = len(all_trades)
                if n == 0:
                    results.append((lookback_bars, move_threshold_pct, rs_threshold_pct, 0, 0.0, None))
                    continue
                wins = sum(1 for t in all_trades if t["win"])
                wr = wins / n * 100.0
                gains = sum(t["roi_pct"] for t in all_trades if t["roi_pct"] > 0)
                losses = -sum(t["roi_pct"] for t in all_trades if t["roi_pct"] <= 0)
                pf = gains / losses if losses > 0 else float("inf")
                results.append((lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf))

    results.sort(key=lambda r: (r[5] is None, -(r[5] if r[5] is not None else 0)))

    print(f"{'lookback':>8}  {'move%':>6}  {'rs%':>5}  {'trades':>7}  {'WR%':>7}  {'PF':>8}")
    for lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf in results:
        pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
        print(f"{lookback_bars:>8}  {move_threshold_pct:>6.1f}  {rs_threshold_pct:>5.1f}  {n:>7}  {wr:>6.2f}%  {pf_str:>8}")

    qualifying = [r for r in results if r[5] is not None and r[5] > 1.0 and r[3] >= 200]
    print(f"\n{len(qualifying)}/{len(results)} combinations clear the Phase 1 bar (PF>1.0, n>=200 trades).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Compile-check**

Run: `python -m py_compile backtest/rs_continuation_backtest.py`
Expected: no output (success)

- [ ] **Step 3: Run it against real data on the server**

The 6-month parquet data lives on the production server (`68.168.222.74:/opt/signals/backtest/data/`), not locally — same as every other backtest this session. Copy the two new files up and run there:

```bash
scp backtest/relative_strength.py backtest/rs_continuation_backtest.py root@68.168.222.74:/opt/signals/backtest/
ssh root@68.168.222.74 "cd /opt/signals && source venv/bin/activate && python -m pytest tests/test_relative_strength.py -v"
```
Expected: 9 tests PASS (confirms the server's checkout also has Task 1's committed files once pulled, or the scp'd copy if testing ahead of a deploy).

```bash
ssh root@68.168.222.74 "cd /opt/signals && source venv/bin/activate && nohup python backtest/rs_continuation_backtest.py > backtest/rs_continuation.log 2>&1 < /dev/null & disown; echo STARTED_PID=\$!"
```

Poll until the process exits (27 combinations x 7 symbols — expect low-single-digit minutes given prior sweeps of similar size in this session took under 2 minutes), then:

```bash
ssh root@68.168.222.74 "cat /opt/signals/backtest/rs_continuation.log"
```

Expected: a 27-row results table sorted by profit factor descending, followed by a count of how many combinations clear the Phase 1 success bar (PF>1.0, n>=200 trades).

- [ ] **Step 4: Commit**

```bash
git add backtest/rs_continuation_backtest.py
git commit -m "feat: add BTC relative-strength continuation backtest runner

Phase 1 go/no-go check per docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md --
sweeps the 27-combination grid across all 8 fetched symbols using the
same flat 20x/10%-ROI TP-SL sizing as live v3, for direct comparability
to the PF~1.02 baseline. Backtest-only; no production wiring."
```

---

## Self-Review Notes

- **Spec coverage:** Universe (8 symbols) ✓ Task 2. Mechanism (per-symbol rs_score, independent of ranking) ✓ Task 1. Parameter grid (3×3×3) ✓ Task 2 constants. Sizing (20x, 10%/10% ROI, SL-first, one position at a time, no lookahead) ✓ Task 2 `simulate()`. Success criteria (PF>1.0 across multiple combos, 200+ trades) ✓ Task 2's `qualifying` summary line. Out-of-scope items (no cross-sectional ranking, no production wiring) ✓ respected — Task 2 is symbol-independent per the spec, no DB/Telegram/scheduler code anywhere in this plan.
- **Placeholder scan:** none found — every step has complete, runnable code.
- **Type consistency:** `rs_signal` returns `str | None` in Task 1 and is consumed as `direction: str | None` in Task 2's `simulate()`; `compute_returns` returns `pd.Series` in both.
