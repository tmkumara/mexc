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
