from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config
import database as db
from strategy import _entry_quality_diagnostics, check_setup_confirmation


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    return db_path


def _make_closed_df(n=100, last_open=100.0, last_high=102.0, last_low=99.5,
                     last_close=101.5, last_volume=500.0, base_volume=100.0):
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    opens = [100.0] * (n - 1) + [last_open]
    highs = [100.5] * (n - 1) + [last_high]
    lows = [99.5] * (n - 1) + [last_low]
    closes = [100.0] * (n - 1) + [last_close]
    volumes = [base_volume] * (n - 1) + [last_volume]
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def test_candle_body_and_range_are_atr_normalized():
    df = _make_closed_df()
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["candle_body_atr_ratio"] > 0
    assert diag["candle_range_atr_ratio"] >= diag["candle_body_atr_ratio"]


def test_volume_ratio_reflects_spike():
    df = _make_closed_df(last_volume=500.0, base_volume=100.0)
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["volume_ratio"] > 4.0   # ~5x the trailing average


def test_long_upper_wick_near_zero_for_close_at_high():
    df = _make_closed_df(last_open=100.0, last_high=102.0, last_low=99.5, last_close=101.9)
    diag = _entry_quality_diagnostics(df, "LONG", fill_price=102.0)
    assert diag["upper_wick_ratio"] < 0.1


def test_short_lower_wick_near_zero_for_close_at_low():
    df = _make_closed_df(last_open=100.0, last_high=100.5, last_low=97.0, last_close=97.1)
    diag = _entry_quality_diagnostics(df, "SHORT", fill_price=97.0)
    assert diag["lower_wick_ratio"] < 0.1


def test_distance_from_zlema_pct_is_signed_by_direction():
    df = _make_closed_df()
    diag_long = _entry_quality_diagnostics(df, "LONG", fill_price=105.0)
    diag_short = _entry_quality_diagnostics(df, "SHORT", fill_price=95.0)
    assert "distance_from_zlema_pct" in diag_long
    assert "distance_from_zlema_pct" in diag_short


def _pending_breakout_setup(direction: str, trigger_price: float, confirmation_high: float,
                             confirmation_low: float, confirmation_close: float,
                             confirmation_time: str, score: float = 65.0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 2, "symbol": "XRP_USDT", "direction": direction, "status": "pending_breakout",
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "score": score,
        "trigger_price": trigger_price,
        "confirmation_high": confirmation_high, "confirmation_low": confirmation_low,
        "confirmation_close": confirmation_close, "confirmation_time": confirmation_time,
    }


def test_confirmed_result_carries_entry_diagnostics(monkeypatch):
    import strategy
    from tests.strategy_fixtures import make_zero_lag_crossover_df, patch_klines

    df = make_zero_lag_crossover_df("LONG", bars=90)
    closed = df.iloc[:-1]
    confirmation_high = float(closed["high"].iloc[-1])
    confirmation_low = float(closed["low"].iloc[-1])
    confirmation_close = float(closed["close"].iloc[-1])
    confirmation_time = closed.index[-1].isoformat()
    trigger_price = confirmation_high * 1.0002

    breakout_df = df.copy()
    breakout_row = breakout_df.iloc[[-1]].copy()
    breakout_row.index = [breakout_df.index[-1] + (breakout_df.index[-1] - breakout_df.index[-2])]
    breakout_row["open"] = confirmation_close
    breakout_row["close"] = trigger_price * 1.001
    breakout_row["high"] = trigger_price * 1.002
    breakout_row["low"] = confirmation_close
    breakout_df = pd.concat([breakout_df.iloc[:-1], breakout_row, breakout_row])

    patch_klines(monkeypatch, strategy, breakout_df)

    setup = _pending_breakout_setup(
        "LONG", trigger_price, confirmation_high, confirmation_low, confirmation_close, confirmation_time,
    )
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "confirmed"
    for key in ("candle_body_atr_ratio", "candle_range_atr_ratio", "upper_wick_ratio",
                "lower_wick_ratio", "volume_ratio", "distance_from_zlema_pct"):
        assert key in extra


def test_save_signal_persists_entry_diagnostics(temp_db):
    signal_id = db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0, tp_price=1.01, sl_price=0.99,
        leverage=20, generated_at=datetime.now(timezone.utc),
        candle_body_atr_ratio=0.8, candle_range_atr_ratio=1.2,
        upper_wick_ratio=0.05, lower_wick_ratio=0.1,
        volume_ratio=2.3, distance_from_zlema_pct=0.0012,
    )
    rows = db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["diag_candle_body_atr_ratio"] == 0.8
    assert row["diag_volume_ratio"] == 2.3
