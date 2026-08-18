# Walk-Forward Backtest and Evaluation Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get a real, out-of-sample-shaped picture of Zero-Lag MTF Pullback v1's performance across 500+ signals and 6-9 months before any Phase 2 parameter change (directional gates, regime filter, entry filter, ATR exits, trade management) is allowed to happen — closing out `architecture.txt` Priority 9, the explicit prerequisite for Priorities 1, 2, 4, 5, 6.

**Architecture:** `scripts/backtest_simple_strategy.py` already does the hard part correctly: it drives `strategy.detect_pending_setup`/`check_setup_confirmation` directly (same source of truth as live, no logic duplication), fetches all four timeframes with REST pagination, and is lookahead-safe (commit `7296b5c` fixed the higher-timeframe-close-time bug this kind of harness is prone to). What it's missing is (a) the full metrics table Priority 9 asked for — profit factor, expectancy, avg winner/loser, score-range and time-of-day breakdowns — and (b) a reusable walk-forward window-building utility Phase 2's plans will need once they actually fit parameters against TRAIN and validate on TEST. Separately, the only other backtest code on disk (`backtest/tpsl_walkforward.py`) is confirmed dead: it imports `scalper_v3_strategy` (deleted with the retired Super Scalper v3 strategy) and `backtest.engine`/`backtest.optimize` (source files don't exist — only their bytecode cache under `backtest/__pycache__/` survived their deletion). It gets removed, not repaired.

**Tech Stack:** Python, pandas, `mexc_client` (MEXC Futures REST), pytest.

## Global Constraints

- This plan does not fit or tune any strategy parameter — it only measures the *existing* fixed-parameter strategy more rigorously. Picking new values for `MIN_SIGNAL_SCORE`, score weights, or anything else is explicitly Phase 2 work, gated on this plan's report existing.
- `scripts/backtest_simple_strategy.py`'s core simulation loop (`backtest_symbol`, the `_SimulatedDatetime` monkeypatch, `_as_of_higher_tf`'s lookahead-safe cutoff logic) is not rewritten — only extended (more fields captured, more report sections). Don't touch its lookahead-prevention mechanics.
- Real historical data can only be fetched from a host that can reach `contract.mexc.com` — this sandbox's egress policy blocks it (documented in `backtest/fetch_data.py`'s own module docstring, confirmed still true). Every step in this plan that needs live data explicitly says so and is meant to run on the production server (or another host with that access), not in this coding session.
- If this plan lands before [`2026-08-18-score-component-instrumentation.md`](2026-08-18-score-component-instrumentation.md), Task 2's score-bucket breakdown still works — `check_setup_confirmation`'s `extra["score"]` exists regardless of whether the component breakdown has landed. The component-level backtest breakdown (which score component predicts outcome) is Phase 2 work once both this plan and the instrumentation plan are in.
- Run `python -m pytest -v` and confirm passing before every commit.

---

## File Structure

**New files:**
- `backtest/windows.py` — pure walk-forward window-building utility (TRAIN/TEST date ranges), reusable by Phase 2 plans.
- `tests/test_backtest_windows.py` — tests for `build_walk_forward_windows`.
- `tests/test_backtest_report_metrics.py` — tests for the extended `BacktestStats` metrics.

**Modified files:**
- `scripts/backtest_simple_strategy.py` — `Trade` gains `score`/`entry_time`/`hold_minutes`; `BacktestStats.print_report` gains profit factor, expectancy, avg winner/loser, score-range buckets, hour-of-day buckets, LONG/SHORT expectancy; `main()` gains a `--from-coin-pool` convenience flag.

**Deleted files:**
- `backtest/tpsl_walkforward.py` (dead — imports modules that no longer exist).
- `backtest/__pycache__/engine.cpython-310.pyc`, `backtest/__pycache__/optimize.cpython-310.pyc`, `backtest/__pycache__/tpsl_scan.cpython-310.pyc`, `backtest/__pycache__/tpsl_walkforward.cpython-310.pyc` (stale bytecode for source files that no longer exist).
- `backtestfull.log` (empty — the artifact of a run that could never have succeeded, since it depends on the dead code above).

---

## Task 1: Remove dead backtest artifacts

**Files:**
- Delete: `backtest/tpsl_walkforward.py`, `backtest/__pycache__/engine.cpython-310.pyc`, `backtest/__pycache__/optimize.cpython-310.pyc`, `backtest/__pycache__/tpsl_scan.cpython-310.pyc`, `backtest/__pycache__/tpsl_walkforward.cpython-310.pyc`, `backtestfull.log`

- [ ] **Step 1: Confirm nothing else imports the dead modules**

Run: `grep -rn "scalper_v3_strategy\|backtest.engine\|backtest.optimize\|backtest\.tpsl_scan\|backtest\.tpsl_walkforward" --include=*.py .`
Expected: only `backtest/tpsl_walkforward.py` itself matches. If anything else matches, stop and investigate before deleting — don't remove a dependency something else still needs.

- [ ] **Step 2: Delete the dead files**

```bash
git rm backtest/tpsl_walkforward.py
rm -f backtest/__pycache__/engine.cpython-310.pyc backtest/__pycache__/optimize.cpython-310.pyc backtest/__pycache__/tpsl_scan.cpython-310.pyc backtest/__pycache__/tpsl_walkforward.cpython-310.pyc
git rm --cached backtestfull.log 2>/dev/null || true
rm -f backtestfull.log
```

(`backtestfull.log` and the two untracked files noted at the start of this session — `backtest/tpsl_walkforward.py` was untracked too — won't all be under git tracking; use `git status` first and adjust `git rm` vs plain `rm` per file based on what it actually shows, rather than assuming.)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions (nothing imported the deleted modules per Step 1).

- [ ] **Step 4: Commit**

```bash
git add -A backtest/
git commit -m "chore: remove dead backtest/tpsl_walkforward.py and its stale bytecode

Imports scalper_v3_strategy (deleted with the retired Super Scalper v3
strategy) and backtest.engine/backtest.optimize (source files already
gone, only bytecode cache survived). Confirmed nothing else references
it. backtestfull.log was the empty artifact of a run this code could
never have completed."
```

---

## Task 2: Extend the evaluation report to the full Priority-9 metrics table

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (`Trade` dataclass, `backtest_symbol`, `BacktestStats.print_report`)
- Test: `tests/test_backtest_report_metrics.py`

**Interfaces:**
- Produces: `Trade` gains `score: float = 0.0`, `entry_time: str = ""`, `hold_minutes: float = 0.0`. `BacktestStats.print_report()` output gains: Profit Factor, Average Winner %, Average Loser %, Expectancy/Trade %, LONG/SHORT Expectancy, Average/Median Holding Duration, a score-range breakdown (`<80` won't occur since that's below `MIN_SIGNAL_SCORE`, but `80-84.9`/`85-89.9`/`90-100` buckets, matching the buckets already used in the live-log diagnosis), an hour-of-day breakdown (by `entry_time`'s UTC hour).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_report_metrics.py
from scripts.backtest_simple_strategy import Trade, BacktestStats


def _trade(outcome, net_roi, score=85.0, direction="LONG", entry_time="2026-03-01T10:00:00", hold_minutes=20.0):
    return Trade(
        symbol="XRP_USDT", direction=direction, entry_price=1.0, tp_price=1.01, sl_price=0.99,
        rr=0.7, outcome=outcome, gross_roi_pct=net_roi, net_roi_pct=net_roi,
        closed_at="2026-03-01T10:20:00", score=score, entry_time=entry_time, hold_minutes=hold_minutes,
    )


def test_profit_factor_is_gross_wins_over_gross_losses():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0))
    stats.add(_trade("win", 7.0))
    stats.add(_trade("loss", -10.0))
    metrics = stats.compute_metrics()
    assert metrics["profit_factor"] == 14.0 / 10.0


def test_expectancy_per_trade_matches_average_net_roi():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0))
    stats.add(_trade("loss", -10.0))
    metrics = stats.compute_metrics()
    assert metrics["expectancy_pct"] == (7.0 + -10.0) / 2


def test_score_bucket_breakdown_groups_correctly():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, score=82.0))
    stats.add(_trade("loss", -10.0, score=96.0))
    metrics = stats.compute_metrics()
    buckets = metrics["score_buckets"]
    assert buckets["80-84.9"]["trades"] == 1
    assert buckets["90-100"]["trades"] == 1


def test_long_short_expectancy_split_independently():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, direction="LONG"))
    stats.add(_trade("loss", -10.0, direction="SHORT"))
    metrics = stats.compute_metrics()
    assert metrics["long_expectancy_pct"] == 7.0
    assert metrics["short_expectancy_pct"] == -10.0


def test_holding_duration_avg_and_median():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, hold_minutes=10.0))
    stats.add(_trade("loss", -10.0, hold_minutes=30.0))
    metrics = stats.compute_metrics()
    assert metrics["avg_hold_minutes"] == 20.0
    assert metrics["median_hold_minutes"] == 20.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backtest_report_metrics.py -v`
Expected: FAIL — `Trade` doesn't accept `score`/`entry_time`/`hold_minutes` yet, and `BacktestStats.compute_metrics` doesn't exist (the current class only has `print_report`, which prints rather than returning a dict).

- [ ] **Step 3: Extend `Trade`**

In `scripts/backtest_simple_strategy.py`, add three fields to the `Trade` dataclass (current lines 107-118), after `closed_at`:

```python
    closed_at: str = ""
    score: float = 0.0
    entry_time: str = ""
    hold_minutes: float = 0.0
```

- [ ] **Step 4: Refactor `BacktestStats` to separate metric computation from printing**

Replace the current `print_report` (lines 128-201) with a `compute_metrics()` method that returns a dict, and a thin `print_report()` that calls it and formats the output — this is what lets Task 2's tests assert on numbers directly instead of parsing printed text, and is also what a future automated regression check (comparing Phase 2 candidate strategies against this baseline) will call programmatically rather than screen-scraping.

```python
    def compute_metrics(self) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"total_trades": 0}

        wins = [t for t in self.trades if t.outcome == "win"]
        losses = [t for t in self.trades if t.outcome == "loss"]
        expired = [t for t in self.trades if t.outcome == "expired"]
        closed_for_rate = len(wins) + len(losses)

        gross_wins = sum(t.net_roi_pct for t in wins)
        gross_losses = abs(sum(t.net_roi_pct for t in losses))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 1e-9 else None

        avg_winner = (gross_wins / len(wins)) if wins else 0.0
        avg_loser = (-gross_losses / len(losses)) if losses else 0.0

        net_roi = sum(t.net_roi_pct for t in self.trades)
        expectancy_pct = net_roi / n

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

        def _bucket_metrics(bucket: list[Trade]) -> dict:
            bwins = [t for t in bucket if t.outcome == "win"]
            blosses = [t for t in bucket if t.outcome == "loss"]
            bclosed = len(bwins) + len(blosses)
            return {
                "trades": len(bucket),
                "win_rate": (len(bwins) / bclosed * 100.0) if bclosed else 0.0,
                "expectancy_pct": (sum(t.net_roi_pct for t in bucket) / len(bucket)) if bucket else 0.0,
                "net_roi_pct": sum(t.net_roi_pct for t in bucket),
            }

        longs = [t for t in self.trades if t.direction == "LONG"]
        shorts = [t for t in self.trades if t.direction == "SHORT"]

        score_buckets: dict[str, dict] = {}
        for label, lo, hi in [("80-84.9", 80, 85), ("85-89.9", 85, 90), ("90-100", 90, 100.001)]:
            bucket = [t for t in self.trades if lo <= t.score < hi]
            score_buckets[label] = _bucket_metrics(bucket)

        hour_buckets: dict[int, dict] = {}
        for hour in range(24):
            bucket = [
                t for t in self.trades
                if t.entry_time and int(t.entry_time[11:13]) == hour
            ]
            if bucket:
                hour_buckets[hour] = _bucket_metrics(bucket)

        hold_minutes_sorted = sorted(t.hold_minutes for t in self.trades if t.outcome != "expired")

        return {
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "expired": len(expired),
            "win_rate": (len(wins) / closed_for_rate * 100.0) if closed_for_rate else 0.0,
            "profit_factor": profit_factor,
            "avg_winner_pct": avg_winner,
            "avg_loser_pct": avg_loser,
            "expectancy_pct": expectancy_pct,
            "net_roi_pct": net_roi,
            "max_drawdown_pct": max_drawdown,
            "max_consecutive_losses": max_consecutive,
            "long_expectancy_pct": _bucket_metrics(longs)["expectancy_pct"],
            "short_expectancy_pct": _bucket_metrics(shorts)["expectancy_pct"],
            "long": _bucket_metrics(longs),
            "short": _bucket_metrics(shorts),
            "score_buckets": score_buckets,
            "hour_buckets": hour_buckets,
            "avg_hold_minutes": (sum(hold_minutes_sorted) / len(hold_minutes_sorted)) if hold_minutes_sorted else 0.0,
            "median_hold_minutes": (
                hold_minutes_sorted[len(hold_minutes_sorted) // 2] if hold_minutes_sorted and len(hold_minutes_sorted) % 2
                else (sum(hold_minutes_sorted[len(hold_minutes_sorted)//2 - 1 : len(hold_minutes_sorted)//2 + 1]) / 2)
                if hold_minutes_sorted else 0.0
            ),
        }

    def print_report(self) -> None:
        m = self.compute_metrics()
        n = m["total_trades"]
        print(f"Total trades:        {n}")
        if n == 0:
            print("No trades generated -- nothing further to report.")
            return

        pf_str = f"{m['profit_factor']:.3f}" if m["profit_factor"] is not None else "inf (no losses)"
        print(f"Wins:                {m['wins']}")
        print(f"Losses:              {m['losses']}")
        print(f"Expired trades:      {m['expired']}")
        print(f"Win rate (win/loss): {m['win_rate']:.1f}%")
        print(f"Profit factor:       {pf_str}")
        print(f"Average winner:      {m['avg_winner_pct']:+.2f}%")
        print(f"Average loser:       {m['avg_loser_pct']:+.2f}%")
        print(f"Expectancy/trade:    {m['expectancy_pct']:+.2f}%")
        print(f"Net ROI:             {m['net_roi_pct']:+.1f}%")
        print(f"Max drawdown:        {m['max_drawdown_pct']:.1f}%")
        print(f"Max consecutive losses: {m['max_consecutive_losses']}")
        print(f"Average hold:        {m['avg_hold_minutes']:.1f} min (median {m['median_hold_minutes']:.1f} min)")

        print(f"\nLONG performance:  {m['long']['trades']} trades, WR {m['long']['win_rate']:.1f}%, "
              f"expectancy {m['long']['expectancy_pct']:+.2f}%, net ROI {m['long']['net_roi_pct']:+.1f}%")
        print(f"SHORT performance: {m['short']['trades']} trades, WR {m['short']['win_rate']:.1f}%, "
              f"expectancy {m['short']['expectancy_pct']:+.2f}%, net ROI {m['short']['net_roi_pct']:+.1f}%")

        print("\nBy score range:")
        for label, bucket in m["score_buckets"].items():
            print(f"  {label}: {bucket['trades']} trades, WR {bucket['win_rate']:.1f}%, "
                  f"expectancy {bucket['expectancy_pct']:+.2f}%")

        print("\nBy entry hour (UTC):")
        for hour in sorted(m["hour_buckets"]):
            bucket = m["hour_buckets"][hour]
            print(f"  {hour:02d}:00: {bucket['trades']} trades, WR {bucket['win_rate']:.1f}%, "
                  f"expectancy {bucket['expectancy_pct']:+.2f}%")

        print("\nPerformance by symbol:")
        for symbol in sorted({t.symbol for t in self.trades}):
            b = self._bucket_for_symbol(symbol)
            print(f"  {symbol}: {b['trades']} trades, WR {b['win_rate']:.1f}%, net ROI {b['net_roi_pct']:+.1f}%")

        print("\nMonthly performance:")
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                by_month[t.closed_at[:7]].append(t)
        for month_key in sorted(by_month):
            bucket = by_month[month_key]
            bwins = sum(1 for t in bucket if t.outcome == "win")
            print(f"  {month_key}: {len(bucket)} trades, {bwins}/{len(bucket)} wins, "
                  f"net ROI {sum(t.net_roi_pct for t in bucket):+.1f}%")

    def _bucket_for_symbol(self, symbol: str) -> dict:
        bucket = [t for t in self.trades if t.symbol == symbol]
        bwins = [t for t in bucket if t.outcome == "win"]
        blosses = [t for t in bucket if t.outcome == "loss"]
        bclosed = len(bwins) + len(blosses)
        return {
            "trades": len(bucket),
            "win_rate": (len(bwins) / bclosed * 100.0) if bclosed else 0.0,
            "net_roi_pct": sum(t.net_roi_pct for t in bucket),
        }
```

Remove the old `_bucket_report` closure-based helper it replaces (was defined inline inside the previous `print_report`).

- [ ] **Step 5: Populate the new `Trade` fields in `backtest_symbol`**

In `backtest_symbol`'s `"confirmed"` branch (current lines ~311-339), capture the entry timestamp and hold duration in minutes, and the final score from `extra`:

```python
                # confirmed
                direction = pending_setup["direction"]
                entry_score = float(pending_setup.get("score", 0.0))  # pullback-stage score already on the dict; see note below
                tp_price, sl_price = strategy.build_trade_prices(direction, fill_price)
                entry_candle_cutoff = df_entry_full.index[i]
                entry_time_str = df_entry_full.index[i].isoformat()
```

and in the `trades.append(Trade(...))` call, add:

```python
                    score=entry_score,
                    entry_time=entry_time_str,
                    hold_minutes=bars_held * entry_tf_minutes,
```

**Note:** `pending_setup["score"]` at this point in the loop is the *pullback-stage* score (0-70), not the final 0-100 score with the breakout-stage bonus added — the breakout-stage final score lives in `extra["score"]` from the `check_setup_confirmation` call two branches earlier in the same loop iteration (the `armed_breakout`/`pending_breakout` transitions happen on *prior* loop iterations, so `extra` from the confirming call is the right one to use here). Capture it there instead: in the block handling `status == "confirmed"` inside the `if pending_setup is not None:` section, `extra["score"]` (or `extra["score"]` if the score-instrumentation plan already landed and renamed nothing) is what should flow into `entry_score` — read the current state of that branch before writing this diff, since its exact local variable name depends on whether the score-instrumentation plan already landed in this working tree.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_backtest_report_metrics.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -v`
Expected: no regressions.

- [ ] **Step 8: Commit**

```bash
git add scripts/backtest_simple_strategy.py tests/test_backtest_report_metrics.py
git commit -m "feat: extend backtest report to the full Priority-9 metrics table

Adds profit factor, expectancy, avg winner/loser, LONG/SHORT
expectancy, score-range and entry-hour breakdowns, and holding
duration stats -- compute_metrics() returns a dict so these are also
assertable in tests, not just printed."
```

---

## Task 3: Reusable walk-forward window utility (infra for Phase 2)

**Files:**
- Create: `backtest/windows.py`
- Test: `tests/test_backtest_windows.py`

**Interfaces:**
- Produces: `build_walk_forward_windows(start: datetime, end: datetime, train_weeks: int = 6, test_weeks: int = 2) -> list[dict]`, each dict `{"train_start": datetime, "train_end": datetime, "test_start": datetime, "test_end": datetime}`, non-overlapping consecutive TEST windows advancing by `test_weeks` each step, discarding any trailing window whose TEST slice would run past `end`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backtest_windows.py
from datetime import datetime, timedelta

from backtest.windows import build_walk_forward_windows


def test_produces_expected_window_count_for_six_month_range():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 7, 1)   # 6 months = ~26 weeks
    windows = build_walk_forward_windows(start, end, train_weeks=6, test_weeks=2)
    assert len(windows) > 0
    for w in windows:
        assert w["train_end"] == w["test_start"]
        assert (w["train_end"] - w["train_start"]).days == 6 * 7
        assert (w["test_end"] - w["test_start"]).days == 2 * 7
        assert w["test_end"] <= end


def test_windows_advance_by_test_weeks():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 7, 1)
    windows = build_walk_forward_windows(start, end, train_weeks=6, test_weeks=2)
    if len(windows) >= 2:
        assert windows[1]["train_start"] - windows[0]["train_start"] == timedelta(weeks=2)


def test_empty_when_range_shorter_than_one_window():
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 10)   # much shorter than 6+2 weeks
    assert build_walk_forward_windows(start, end, train_weeks=6, test_weeks=2) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backtest_windows.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.windows'`.

- [ ] **Step 3: Implement `backtest/windows.py`**

```python
"""
backtest/windows.py -- pure walk-forward TRAIN/TEST window builder.

No strategy-specific logic here (deliberately -- the previous walk-forward
harness, deleted per docs/superpowers/plans/2026-08-18-walkforward-backtest-and-report.md
Task 1, coupled window-building to a specific retired strategy's parameter
grid). Phase 2 plans (directional gates, regime filter, entry filter, exit
sizing, trade management) each import this to get their TRAIN/TEST date
ranges, then run scripts/backtest_simple_strategy.py's simulation logic
scoped to each range.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def build_walk_forward_windows(
    start: datetime, end: datetime, train_weeks: int = 6, test_weeks: int = 2,
) -> list[dict]:
    """Consecutive non-overlapping TEST windows, each preceded by a
    train_weeks-long TRAIN window ending exactly where TEST begins.
    Advances by test_weeks each step. Drops any window whose TEST slice
    would extend past `end`."""
    windows: list[dict] = []
    train_start = start
    while True:
        train_end = train_start + timedelta(weeks=train_weeks)
        test_start = train_end
        test_end = test_start + timedelta(weeks=test_weeks)
        if test_end > end:
            break
        windows.append({
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
        })
        train_start += timedelta(weeks=test_weeks)
    return windows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backtest_windows.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/windows.py tests/test_backtest_windows.py
git commit -m "feat: add reusable walk-forward window builder for Phase 2 plans"
```

---

## Task 4: Coin-pool-driven symbol selection convenience

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (`main`)

**Interfaces:**
- Produces: `--from-coin-pool N` CLI flag, mutually exclusive-in-practice with `--symbols` (if both given, `--symbols` wins and a warning prints) — pulls the current top-N ranked symbols via `coin_scanner`'s existing ranking function rather than requiring a hand-typed list, so the backtest universe matches what the live bot would actually be scanning.

- [ ] **Step 1: Check what `coin_scanner` exposes for a one-shot ranked list**

Run: `grep -n "^def " coin_scanner.py`
Find the function that returns a ranked symbol list without requiring the scheduler/cache machinery (likely the function `refresh_coin_list`/`get_cached_coins` wrap, or a lower-level ranking function) — read it before writing Step 2's diff so the call matches its actual signature (this plan was written without re-reading `coin_scanner.py`'s full ranking pipeline in detail).

- [ ] **Step 2: Add the flag**

In `scripts/backtest_simple_strategy.py`'s `main()`, add:

```python
    parser.add_argument("--from-coin-pool", type=int, default=0,
                         help="ignore --symbols; use the top-N current coin-pool ranking instead")
```

and before building `futures`, if `args.from_coin_pool > 0`, replace `args.symbols` with the result of whatever ranking call Step 1 identified (truncated to `args.from_coin_pool` symbols), printing which symbols were selected so the run is reproducible from its own console output.

- [ ] **Step 3: Compile check**

Run: `python -m py_compile scripts/backtest_simple_strategy.py`
Expected: no output (success).

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "feat: add --from-coin-pool convenience flag to the backtest script"
```

---

## Task 5: Run the backtest and produce the report (server-side, not this sandbox)

**Files:** none (execution only)

- [ ] **Step 1: Confirm network access**

Run (on the production server, per `CLAUDE.md`'s deploy section and `backtest/fetch_data.py`'s docstring — this sandbox cannot reach `contract.mexc.com`): `curl -s -o /dev/null -w "%{http_code}" https://contract.mexc.com/api/v1/contract/ping` (or equivalent) to confirm reachability before a multi-hour run.

- [ ] **Step 2: Run the backtest across the current coin pool for 6-9 months**

```bash
cd /opt/signals
source venv/bin/activate
python scripts/backtest_simple_strategy.py --from-coin-pool 60 --days 270 --workers 6 2>&1 | tee backtest_v1_6mo_report.log
```

Adjust `--days` upward toward 270 (~9 months) if the run completes comfortably within available time and MEXC's retained history supports it — `get_klines_extended`'s pagination already reports what it actually achieved per symbol, so check the "achieved history" lines in the output rather than assuming the full `--days` was granted.

- [ ] **Step 3: Verify sample size**

Check the printed `Total trades:` line. Target is 500+ per Priority 9. If it comes in well short (the live 6-day sample produced only 53, so 6-9 months of the same ~1 confirmed signal per ~10 hours cadence could land anywhere from a few hundred to over a thousand depending on regime) — note the actual count in the report rather than re-running with loosened gates, which would defeat the point of measuring the *current* strategy as-is.

- [ ] **Step 4: Save the report**

Commit `backtest_v1_6mo_report.log` (or move it under `backtest/reports/` if that convention fits better next to `backtest/README.md`) so Phase 2 plans can cite concrete numbers from it instead of re-running the backtest every time they need a baseline.

```bash
mkdir -p backtest/reports
mv backtest_v1_6mo_report.log backtest/reports/2026-08-zero-lag-v1-baseline.log
git add backtest/reports/2026-08-zero-lag-v1-baseline.log
git commit -m "docs: add Zero-Lag MTF Pullback v1's first real 6-9mo backtest report

Establishes the out-of-sample-shaped baseline Priority 9 required
before any Phase 2 parameter change (directional gates, regime filter,
entry filter, exit sizing, trade management)."
```

- [ ] **Step 5: Update the roadmap doc**

In `docs/superpowers/plans/2026-08-18-zero-lag-mtf-v2-roadmap.md`, mark Phase 1's status column complete and add a short "Phase 1 results" section summarizing the report's headline numbers (total trades, win rate, profit factor, expectancy, LONG vs SHORT split) so Phase 2 plans can be written against real data.

---

## Self-Review

- **Spec coverage:** Priority 9's every explicit ask is covered: dead-code cleanup so nothing masquerades as validation that never ran (Task 1); the exact metrics table requested — total/wins/losses/WR/avg winner/avg loser/profit factor/expectancy/net ROI/max drawdown/max consecutive losses/LONG-SHORT WR+expectancy/avg+median holding duration, broken down by score range/symbol/direction/hour (Task 2); a reusable walk-forward mechanism for Phase 2's actual parameter-fitting work (Task 3); running against the real coin-pool universe (Task 4); realistic fees/slippage (already in `scripts/backtest_simple_strategy.py`, unchanged); no lookahead (unchanged, explicitly protected in Global Constraints); 500+ signals across a multi-month window (Task 5). Volatility/trend-regime breakdown is intentionally deferred — it needs Priority 2's regime measures to exist first (Phase 2), so bucketing by a regime that doesn't get computed yet would be fabricated, not measured.
- **Placeholder scan:** No TBDs. Two explicit "read the current code before writing this diff" notes (Task 2 Step 5's `extra["score"]` sourcing, Task 4 Step 1's `coin_scanner` call) are flagged as such rather than guessed — both depend on exact current-file state this plan wasn't re-verified against line-by-line at planning time, which is more honest than a diff that might silently target the wrong variable.
- **Type consistency:** `compute_metrics()`'s return dict keys are used identically across Task 2's tests, `print_report`, and Task 5's manual verification — no renaming across that boundary.
- **Sandbox limitation carried forward honestly:** Task 5 cannot be executed by an agent working in this sandbox (no egress to MEXC) — stated plainly rather than silently skipped or faked.
