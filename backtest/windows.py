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
