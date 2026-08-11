from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tests.strategy_fixtures import (
    make_zero_lag_trend_df, make_zero_lag_pullback_df, make_zero_lag_crossover_df,
    patch_klines, patch_klines_multi,
)
from strategy import detect_pending_setup, check_setup_confirmation, build_trade_prices
from config import MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF


def _pipeline_dfs(direction: str) -> dict:
    return {
        MACRO_TF: make_zero_lag_trend_df(direction, bars=320, freq="4h"),
        TREND_TF: make_zero_lag_trend_df(direction, bars=320, freq="1h"),
        PULLBACK_TF: make_zero_lag_pullback_df(direction, bars=100),
        ENTRY_TF: make_zero_lag_trend_df(direction, bars=100, freq="5min"),
    }


def test_pending_pullback_armed_on_long_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("LONG"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "LONG"
    assert setup["macro_trend"] == 1
    assert setup["trend_state"] == 1
    assert setup["score"] >= 30.0


def test_pending_pullback_armed_on_short_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("SHORT"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "SHORT"
    assert setup["macro_trend"] == -1


def test_rejected_when_macro_and_trend_disagree(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[TREND_TF] = make_zero_lag_trend_df("SHORT", bars=320, freq="1h")
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_trend_agreement") == 1


def test_rejected_when_outside_pullback_distance(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    far_pullback = make_zero_lag_pullback_df("LONG", bars=100)
    far_pullback.iloc[-1, far_pullback.columns.get_loc("close")] *= 1.02  # 2% away, well outside 0.10% band
    far_pullback.iloc[-2, far_pullback.columns.get_loc("close")] *= 1.02
    dfs[PULLBACK_TF] = far_pullback
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_pullback") == 1


def test_rejected_when_insufficient_history(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[MACRO_TF] = make_zero_lag_trend_df("LONG", bars=50, freq="4h")  # too short
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("insufficient_history") == 1


def _pending_pullback_setup(direction: str = "LONG") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "symbol": "XRP_USDT", "direction": direction, "status": "pending_pullback",
        "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "score": 65.0,
    }


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


def test_crossover_plus_confirmation_candle_arms_breakout(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    patch_klines(monkeypatch, strategy, df)

    status, fill_price, extra = check_setup_confirmation(_pending_pullback_setup("LONG"))

    assert status == "armed_breakout"
    assert fill_price is None
    assert extra is not None
    assert extra["trigger_price"] > extra["confirmation_high"]  # LONG buffer is above the confirming high


def test_no_crossover_stays_waiting(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    # Flatten the final candle so it never actually crosses back above zlema.
    last_idx = df.index[-2]
    df.loc[last_idx, "close"] = df.loc[last_idx, "open"] * 0.999
    patch_klines(monkeypatch, strategy, df)

    status, fill_price, extra = check_setup_confirmation(_pending_pullback_setup("LONG"))

    assert status == "waiting"
    assert fill_price is None
    assert extra is None


def test_confirmed_on_trigger_price_breakout(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    closed = df.iloc[:-1]
    confirmation_high = float(closed["high"].iloc[-1])
    confirmation_low = float(closed["low"].iloc[-1])
    confirmation_close = float(closed["close"].iloc[-1])
    confirmation_time = closed.index[-1].isoformat()
    trigger_price = confirmation_high * 1.0002

    breakout_df = df.copy()
    # Next closed candle breaks above the trigger price.
    breakout_row = breakout_df.iloc[[-1]].copy()
    breakout_row.index = [breakout_df.index[-1] + (breakout_df.index[-1] - breakout_df.index[-2])]
    breakout_row["open"] = confirmation_close
    breakout_row["close"] = trigger_price * 1.001
    breakout_row["high"] = trigger_price * 1.002
    breakout_row["low"] = confirmation_close
    breakout_df = pd.concat([breakout_df.iloc[:-1], breakout_row, breakout_row])  # last row duplicated = "forming"

    patch_klines(monkeypatch, strategy, breakout_df)

    setup = _pending_breakout_setup(
        "LONG", trigger_price, confirmation_high, confirmation_low, confirmation_close, confirmation_time,
    )
    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "confirmed"
    assert fill_price == pytest.approx(trigger_price)
    assert extra["score"] >= 65.0


def test_setup_expires_from_original_setup_time(monkeypatch):
    import strategy
    df = make_zero_lag_crossover_df("LONG", bars=90)
    patch_klines(monkeypatch, strategy, df)

    setup = _pending_pullback_setup("LONG")
    setup["setup_time"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    status, fill_price, extra = check_setup_confirmation(setup)

    assert status == "expired"


def test_build_trade_prices_long():
    tp, sl = build_trade_prices("LONG", entry=100.0)
    assert tp == pytest.approx(100.35, abs=0.01)   # +7% ROI / 20x = +0.35%
    assert sl == pytest.approx(99.5, abs=0.01)      # -10% ROI / 20x = -0.50%


def test_build_trade_prices_short():
    tp, sl = build_trade_prices("SHORT", entry=100.0)
    assert tp == pytest.approx(99.65, abs=0.01)
    assert sl == pytest.approx(100.5, abs=0.01)
