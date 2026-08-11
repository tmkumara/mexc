import pandas as pd
import pytest

from outcome_check import check_tp_sl


def _candles(rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return df


def test_tp_hit_is_a_win():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),   # entry candle (cutoff)
        (100.0, 100.5, 99.8, 100.3),   # TP=100.35 not yet hit (high=100.5 > tp but check exact)
        (100.3, 101.0, 100.2, 100.9),  # high=101.0 clears TP
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result is not None
    assert result["status"] == "win"
    assert result["pnl_roi_pct"] == pytest.approx(0.35, abs=0.01)


def test_sl_hit_is_a_loss():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.2, 99.4, 99.6),   # low=99.4 breaches SL=99.5
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result is not None
    assert result["status"] == "loss"
    assert result["pnl_roi_pct"] == pytest.approx(-0.50, abs=0.01)


def test_same_candle_sl_beats_tp_tie_break():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 101.0, 99.0, 100.5),   # single wild candle spans both TP and SL
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)

    assert result["status"] == "loss"


def test_returns_none_while_still_open():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.1, 99.9, 100.0),
    ])
    cutoff = df.index[0]
    result = check_tp_sl("LONG", entry_price=100.0, sl_price=99.5, tp_price=100.35, df=df, entry_candle_cutoff=cutoff)
    assert result is None


def test_short_direction_mirrors():
    df = _candles([
        (100.0, 100.1, 99.9, 100.0),
        (100.0, 100.2, 99.6, 99.65),  # low=99.6 clears SHORT TP=99.65? -- use exact clearance
    ])
    df.iloc[1, df.columns.get_loc("low")] = 99.6
    cutoff = df.index[0]
    result = check_tp_sl("SHORT", entry_price=100.0, sl_price=100.5, tp_price=99.65, df=df, entry_candle_cutoff=cutoff)
    assert result["status"] == "win"
