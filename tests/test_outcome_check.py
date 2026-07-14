import pandas as pd

from outcome_check import check_tp_sl


def _df(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"high": [r[1] for r in rows], "low": [r[2] for r in rows]}, index=idx)


def test_long_tp_hit():
    df = _df([
        ("2026-01-01 00:00", 101.0, 99.5),
        ("2026-01-01 00:05", 106.0, 104.0),
        ("2026-01-01 00:10", 106.0, 104.0),   # forming candle, ignored
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("LONG", 100.0, 105.0, 95.0, df, cutoff) == "win"


def test_long_sl_hit():
    df = _df([
        ("2026-01-01 00:00", 101.0, 99.5),
        ("2026-01-01 00:05", 102.0, 94.0),
        ("2026-01-01 00:10", 102.0, 94.0),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("LONG", 100.0, 105.0, 95.0, df, cutoff) == "loss"


def test_long_same_candle_tie_favors_sl():
    df = _df([
        ("2026-01-01 00:00", 101.0, 99.5),
        ("2026-01-01 00:05", 106.0, 94.0),   # both TP and SL touched in one candle
        ("2026-01-01 00:10", 106.0, 94.0),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("LONG", 100.0, 105.0, 95.0, df, cutoff) == "loss"


def test_short_tp_hit():
    df = _df([
        ("2026-01-01 00:00", 100.5, 99.0),
        ("2026-01-01 00:05", 96.0, 94.0),
        ("2026-01-01 00:10", 96.0, 94.0),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("SHORT", 100.0, 95.0, 105.0, df, cutoff) == "win"


def test_short_sl_hit():
    df = _df([
        ("2026-01-01 00:00", 100.5, 99.0),
        ("2026-01-01 00:05", 106.0, 99.5),
        ("2026-01-01 00:10", 106.0, 99.5),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("SHORT", 100.0, 95.0, 105.0, df, cutoff) == "loss"


def test_still_pending_returns_none():
    df = _df([
        ("2026-01-01 00:00", 101.0, 99.5),
        ("2026-01-01 00:05", 101.5, 99.0),
        ("2026-01-01 00:10", 101.5, 99.0),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("LONG", 100.0, 105.0, 95.0, df, cutoff) is None


def test_candles_before_entry_cutoff_are_ignored():
    df = _df([
        ("2025-12-31 23:50", 200.0, 1.0),     # would look like a win/loss but predates entry
        ("2026-01-01 00:05", 101.5, 99.0),
        ("2026-01-01 00:10", 101.5, 99.0),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    assert check_tp_sl("LONG", 100.0, 105.0, 95.0, df, cutoff) is None
