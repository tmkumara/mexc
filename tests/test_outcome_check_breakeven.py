import pandas as pd

from outcome_check import check_tp_sl_with_breakeven


def _df(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"high": [r[1] for r in rows], "low": [r[2] for r in rows]}, index=idx)


def test_tp_hit_is_a_win():
    df = _df([
        ("2026-01-01 00:00", 100.2, 99.9),
        ("2026-01-01 00:05", 100.36, 100.1),   # TP=100.35 hit
        ("2026-01-01 00:10", 100.36, 100.1),   # forming candle, ignored
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "win"
    assert result["breakeven_triggered_at"] is not None  # 100.2 was reached en route


def test_sl_hit_before_breakeven_is_a_full_loss():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        ("2026-01-01 00:05", 100.15, 99.4),   # SL=99.5 hit, never reached 100.2 trigger
        ("2026-01-01 00:10", 100.15, 99.4),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "loss"
    assert result["breakeven_triggered_at"] is None


def test_breakeven_trigger_then_stop_is_breakeven_not_loss():
    df = _df([
        ("2026-01-01 00:00", 100.25, 99.9),   # reaches 100.2 trigger -> SL moves to 100.0
        ("2026-01-01 00:05", 100.1, 99.95),   # pulls back to entry (100.0) -> breakeven stop
        ("2026-01-01 00:10", 100.1, 99.95),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "breakeven"
    assert result["pnl_roi_pct"] == 0.0
    assert result["breakeven_triggered_at"] is not None


def test_breakeven_trigger_then_tp_is_still_a_win():
    df = _df([
        ("2026-01-01 00:00", 100.25, 99.9),   # reaches trigger
        ("2026-01-01 00:05", 100.4, 100.1),   # then hits TP
        ("2026-01-01 00:10", 100.4, 100.1),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "win"
    assert result["breakeven_triggered_at"] is not None


def test_same_candle_original_sl_beats_breakeven_trigger():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        # one wild candle spans both the breakeven trigger (100.2) and the
        # original SL (99.5) -- conservative same-candle rule: original SL wins.
        ("2026-01-01 00:05", 100.3, 99.4),
        ("2026-01-01 00:10", 100.3, 99.4),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "loss"


def test_short_breakeven_then_stop_is_breakeven():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.75),   # reaches 99.8 trigger -> SL moves to 100.0
        ("2026-01-01 00:05", 100.05, 99.9),   # pulls back to entry -> breakeven stop
        ("2026-01-01 00:10", 100.05, 99.9),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "SHORT", entry_price=100.0, sl_price=100.5, tp_price=99.65,
        breakeven_trigger_price=99.8, df=df, entry_candle_cutoff=cutoff,
    )
    assert result["status"] == "breakeven"


def test_still_pending_returns_none():
    df = _df([
        ("2026-01-01 00:00", 100.1, 99.9),
        ("2026-01-01 00:05", 100.15, 99.95),
        ("2026-01-01 00:10", 100.15, 99.95),
    ])
    cutoff = pd.Timestamp("2025-12-31 23:55")
    result = check_tp_sl_with_breakeven(
        "LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35,
        breakeven_trigger_price=100.2, df=df, entry_candle_cutoff=cutoff,
    )
    assert result is None
