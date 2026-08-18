from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config
import database as db
from strategy import (
    _pullback_stage_score, _breakout_stage_score,
    detect_pending_setup, check_setup_confirmation,
)
from config import MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF
from tests.strategy_fixtures import (
    make_zero_lag_trend_df, make_zero_lag_pullback_df, make_zero_lag_crossover_df,
    patch_klines, patch_klines_multi,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    return db_path


def test_pullback_stage_score_components_sum_to_total():
    zlema_trend = pd.Series([1.0] * 10)
    total, components = _pullback_stage_score("LONG", zlema_trend, distance_pct=0.0)
    assert set(components.keys()) == {"macro", "trend_strength", "pullback"}
    assert components["macro"] == 30.0
    assert round(sum(components.values()), 1) == total


def test_pullback_stage_score_full_marks_at_zero_distance():
    zlema_trend = pd.Series([1.0] * 10)
    total, components = _pullback_stage_score("LONG", zlema_trend, distance_pct=0.0)
    assert components["pullback"] == 20.0
    assert total == 30.0 + components["trend_strength"] + 20.0


def test_breakout_stage_score_components_sum_to_total():
    total, components = _breakout_stage_score(
        "LONG", confirmation_high=110.0, confirmation_low=100.0,
        confirmation_close=108.0, candles_to_break=1,
    )
    assert set(components.keys()) == {"breakout_freshness", "breakout_quality"}
    assert components["breakout_freshness"] == 20.0
    assert round(sum(components.values()), 1) == total


def test_breakout_stage_score_freshness_decays_with_candles():
    total_1, c1 = _breakout_stage_score("LONG", 110.0, 100.0, 108.0, candles_to_break=1)
    total_3, c3 = _breakout_stage_score("LONG", 110.0, 100.0, 108.0, candles_to_break=3)
    assert c1["breakout_freshness"] > c3["breakout_freshness"]
    assert c3["breakout_freshness"] == 10.0


def _pipeline_dfs(direction: str) -> dict:
    return {
        MACRO_TF: make_zero_lag_trend_df(direction, bars=320, freq="4h"),
        TREND_TF: make_zero_lag_trend_df(direction, bars=320, freq="1h"),
        PULLBACK_TF: make_zero_lag_pullback_df(direction, bars=100),
        ENTRY_TF: make_zero_lag_trend_df(direction, bars=100, freq="5min"),
    }


def test_detect_pending_setup_exposes_score_components(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("LONG"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["score_macro"] == 30.0
    assert "score_trend_strength" in setup
    assert "score_pullback" in setup
    assert round(
        setup["score_macro"] + setup["score_trend_strength"] + setup["score_pullback"], 1
    ) == setup["score"]


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


def test_check_setup_confirmation_confirmed_exposes_breakout_components(monkeypatch):
    import strategy
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
    assert "score_breakout_freshness" in extra
    assert "score_breakout_quality" in extra
    assert extra["score"] == pytest.approx(
        setup["score"] + extra["score_breakout_freshness"] + extra["score_breakout_quality"], abs=0.1
    ) or extra["score"] == 100.0  # capped at 100


def test_save_pending_setup_persists_score_components(temp_db):
    now = datetime.now(timezone.utc)
    setup = {
        "symbol": "XRP_USDT", "direction": "LONG",
        "macro_tf": "4h", "trend_tf": "1h", "pullback_tf": "15m", "entry_tf": "5m",
        "macro_trend": 1, "trend_state": 1,
        "zlema_1h": 100.0, "zlema_15m": 100.5,
        "pullback_price": 100.4, "pullback_time": now.isoformat(),
        "score": 65.0, "score_macro": 30.0, "score_trend_strength": 15.0, "score_pullback": 20.0,
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "created_at": now.isoformat(),
    }
    db.save_pending_setup(setup)
    rows = db.get_pending_setups("pending_pullback")
    assert rows[0]["score_macro"] == 30.0
    assert rows[0]["score_trend_strength"] == 15.0
    assert rows[0]["score_pullback"] == 20.0


def test_save_signal_persists_score_components(temp_db):
    signal_id = db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0, tp_price=1.01, sl_price=0.99,
        leverage=20, generated_at=datetime.now(timezone.utc),
        score_macro=30.0, score_trend_strength=18.0, score_pullback=20.0,
        score_breakout_freshness=20.0, score_breakout_quality=8.0,
    )
    rows = db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["score_macro"] == 30.0
    assert row["score_breakout_freshness"] == 20.0
    assert row["score_breakout_quality"] == 8.0
