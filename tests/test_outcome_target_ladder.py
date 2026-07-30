import pandas as pd
import pytest

from outcome_check import check_target_ladder


def test_t1_then_breakeven_stop_is_a_small_win():
    idx = pd.date_range("2026-01-01", periods=5, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.2, 101.5, 100.5, 100.5],
        "low":  [100.0, 100.5, 100.2, 99.8, 99.8],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])

    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "win"
    assert result["final_stage"] == 1
    assert result["t1_hit_at"] == idx[1]
    assert result["t2_hit_at"] is None
    assert result["pnl_roi_pct"] == pytest.approx(0.5, abs=1e-6)


def test_sl_before_t1_is_a_full_loss():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 100.5, 100.5],
        "low":  [100.0, 98.5, 98.5],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "loss"
    assert result["final_stage"] == 0
    assert result["pnl_roi_pct"] == pytest.approx(-1.0, abs=1e-6)


def test_full_ladder_t1_t2_t3_all_hit():
    idx = pd.date_range("2026-01-01", periods=4, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.5, 102.5, 103.5],
        "low":  [100.0, 100.5, 101.0, 102.0],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "win"
    assert result["final_stage"] == 3
    assert result["t1_hit_at"] == idx[1]
    assert result["t2_hit_at"] == idx[2]
    assert result["pnl_roi_pct"] == pytest.approx(0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0, abs=1e-6)


def test_same_candle_sl_priority_over_target():
    idx = pd.date_range("2026-01-01", periods=2, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.5],
        "low":  [100.0, 98.0],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "loss"
    assert result["final_stage"] == 0


def test_still_open_returns_none():
    idx = pd.date_range("2026-01-01", periods=2, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 100.3],
        "low":  [100.0, 99.8],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is None
