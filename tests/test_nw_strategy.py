# tests/test_nw_strategy.py
from nw_strategy import (
    _detect_trend_htf, find_swing_low, find_swing_high, _detect_nw_signal,
    _valid_trade_geometry, _calc_rr, _calculate_tp_sl_nw,
    BtcContext4h, _btc_filter_ok_4h,
)
from tests.nw_fixtures import make_4h_trend_df, make_swing_fixture, make_1h_nw_df
import config_nw


def test_detect_trend_htf_long_when_trending_up():
    df = make_4h_trend_df("LONG")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "LONG"


def test_detect_trend_htf_short_when_trending_down():
    df = make_4h_trend_df("SHORT")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "SHORT"


def test_detect_trend_htf_none_when_insufficient_history():
    df = make_4h_trend_df("LONG", bars=50)
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) is None


def test_find_swing_low_detects_known_pivot():
    df = make_swing_fixture(kind="low", fractal_bars=5, pivot_value=95.0)
    end_idx = len(df) - 1
    result = find_swing_low(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 95.0) < 1e-9


def test_find_swing_high_detects_known_pivot():
    df = make_swing_fixture(kind="high", fractal_bars=5, pivot_value=105.0)
    end_idx = len(df) - 1
    result = find_swing_high(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 105.0) < 1e-9


def test_find_swing_low_returns_none_when_no_fractal_in_lookback():
    # Monotonic decline -- no interior fractal low exists (the minimum is
    # always at the trailing edge, never confirmed by 5 higher bars after
    # it), so nothing should be found within a short lookback.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(15) * 0.5
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": np.full(15, 1000.0),
    })
    result = find_swing_low(df, end_idx=14, lookback=8, fractal_bars=5)
    assert result is None


def test_detect_nw_signal_long_fires_on_bullish_flip_with_confirmation():
    df = make_1h_nw_df("LONG")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "LONG")
    assert ok is True, reason
    assert details["close"] == closed["close"].iloc[-1]
    assert details["swing_level"] is not None
    assert 0.0 <= details["rsi"] <= 100.0


def test_detect_nw_signal_short_fires_on_bearish_flip_with_confirmation():
    df = make_1h_nw_df("SHORT")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "SHORT")
    assert ok is True, reason


def test_detect_nw_signal_rejects_when_no_flip_on_latest_bar():
    # A pure, unbroken decline never flips -- the latest bar is still
    # falling, not turning up.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(80) * 0.05
    df = pd.DataFrame({
        "open": closes + 0.01, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": np.full(80, 1000.0),
    })
    ok, reason, _ = _detect_nw_signal(df, "LONG")
    assert ok is False
    assert "flip" in reason


def test_detect_nw_signal_rejects_low_volume():
    df = make_1h_nw_df("LONG", volume_mult=0.5)
    closed = df.iloc[:-1]
    ok, reason, _ = _detect_nw_signal(closed, "LONG")
    assert ok is False
    assert "volume" in reason


def test_valid_trade_geometry_long():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=101.0, sl=99.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=99.0, sl=101.0) is False


def test_valid_trade_geometry_short():
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=99.0, sl=101.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=101.0, sl=99.0) is False


def test_calc_rr():
    assert abs(_calc_rr(entry=100.0, tp=102.0, sl=99.0) - 2.0) < 1e-9


def test_calculate_tp_sl_nw_long_fixed_tp_structural_sl():
    entry = 100.0
    details = {"atr": 0.20, "swing_level": 98.5}  # swing 1.5 below entry
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is not None
    tp, sl = result
    expected_tp = entry * (1.0 + config_nw.TP_PRICE_PCT)
    assert abs(tp - expected_tp) < 1e-9
    # raw sl distance = entry - (swing_level - atr*buffer)
    #                 = 100 - (98.5 - 0.20*0.5) = 100 - 98.4 = 1.6
    raw_distance = entry - (98.5 - 0.20 * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER)
    expected_distance = min(raw_distance, entry * config_nw.MAX_SL_PRICE_PCT)
    assert abs((entry - sl) - expected_distance) < 1e-9


def test_calculate_tp_sl_nw_short_fixed_tp_structural_sl():
    entry = 100.0
    details = {"atr": 0.20, "swing_level": 101.5}
    result = _calculate_tp_sl_nw("SHORT", entry, details)
    assert result is not None
    tp, sl = result
    expected_tp = entry * (1.0 - config_nw.TP_PRICE_PCT)
    assert abs(tp - expected_tp) < 1e-9
    raw_distance = (101.5 + 0.20 * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER) - entry
    expected_distance = min(raw_distance, entry * config_nw.MAX_SL_PRICE_PCT)
    assert abs((sl - entry) - expected_distance) < 1e-9


def test_calculate_tp_sl_nw_caps_at_max_sl_when_swing_far_away():
    entry = 100.0
    # Swing level absurdly far away -- raw distance must be capped, not
    # left uncapped or rejected.
    details = {"atr": 0.20, "swing_level": 50.0}
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is not None
    tp, sl = result
    assert abs((entry - sl) - entry * config_nw.MAX_SL_PRICE_PCT) < 1e-9


def test_calculate_tp_sl_nw_rejects_when_swing_on_wrong_side_of_entry():
    entry = 100.0
    # LONG's swing_level (even after the ATR buffer) sits above entry --
    # not a valid structural stop.
    details = {"atr": 0.20, "swing_level": 100.5}
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is None


def test_btc_filter_ok_long_when_btc_bullish():
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.1, three_candle_move_pct=0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is True, reason


def test_btc_filter_blocks_long_when_btc_bearish():
    btc = BtcContext4h(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "bearish" in reason


def test_btc_filter_blocks_on_extreme_single_candle_volatility():
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.9, three_candle_move_pct=0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "volatility" in reason


def test_btc_filter_ok_short_when_btc_bearish():
    btc = BtcContext4h(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    ok, reason = _btc_filter_ok_4h("SHORT", btc)
    assert ok is True, reason


def test_btc_filter_blocks_on_extreme_three_candle_volatility_isolated():
    # one_candle_move_pct stays well inside its own threshold (0.60) so
    # only the three_candle branch can be responsible for the block.
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.1, three_candle_move_pct=1.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "volatility" in reason


def test_btc_filter_allows_move_exactly_at_single_candle_threshold():
    # Exactly at the threshold must still pass -- the check is strict '>'.
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.60, three_candle_move_pct=0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is True, reason


def test_btc_filter_blocks_long_on_opposing_move_even_when_ema_trend_is_bullish():
    # close > ema_200 (the EMA-trend conjunct is satisfied on its own), but
    # three_candle_move_pct is past the -0.20 opposing-move tolerance --
    # isolates the opposing-move clause from the EMA-trend clause. Neither
    # volatility threshold is tripped (0.60 / 1.20).
    btc = BtcContext4h(close=101.0, ema_200=100.0, one_candle_move_pct=-0.05, three_candle_move_pct=-0.25)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "bearish" in reason


import nw_strategy
from nw_strategy import evaluate_symbol_nw, BtcContext4h as _BtcCtx
from tests.nw_fixtures import patch_nw_klines


def test_evaluate_symbol_nw_returns_signal_for_valid_long_setup(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", False)

    sig = evaluate_symbol_nw("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= nw_strategy.config_nw.MIN_RR


def test_evaluate_symbol_nw_returns_signal_for_valid_short_setup(monkeypatch):
    df_4h = make_4h_trend_df("SHORT")
    df_1h = make_1h_nw_df("SHORT")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", False)

    sig = evaluate_symbol_nw("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price


def test_evaluate_symbol_nw_returns_none_when_no_htf_trend(monkeypatch):
    # Flat 4H series -- close hovers at EMA200, no clear trend.
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=220, freq="4h")
    closes = np.full(220, 100.0)
    df_4h = pd.DataFrame({
        "open": closes, "high": closes + 0.01, "low": closes - 0.01,
        "close": closes, "volume": np.full(220, 1000.0),
    })
    df_4h = pd.concat([df_4h, df_4h.iloc[[-1]]])
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)

    reject_sink = {}
    sig = evaluate_symbol_nw("TEST_USDT", reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("no_trend", 0) >= 1


def test_evaluate_symbol_nw_uses_reject_sink_for_btc_filter(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", True)

    bearish_btc = _BtcCtx(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    reject_sink = {}
    sig = evaluate_symbol_nw("TEST_USDT", btc_context=bearish_btc, reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("btc_filter", 0) >= 1


def test_score_candidate_nw_within_bounds():
    details = {"rsi": 60.0, "volume_ratio": 1.5}
    score = nw_strategy._score_candidate_nw("LONG", details, rr=2.0)
    assert 0.0 <= score <= 100.0
