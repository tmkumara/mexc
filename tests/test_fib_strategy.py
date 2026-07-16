import numpy as np
import pandas as pd

import fib_strategy
from fib_strategy import (
    FibLeg, find_swings, find_last_impulse_leg,
    fib_retracement_price, fib_extension_price,
)
from fib_strategy import _detect_trend_4h, _detect_fib_entry
from tests.fib_fixtures import make_1h_fib_df, make_4h_trend_df
from fib_strategy import BtcContext4h, _btc_filter_ok_4h, build_btc_context_4h


def test_fractal_swing_detection_finds_known_pivots():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    # The rise segment peaks at story index 19 (leg_end); the decline
    # segment bottoms at story index 9 (leg_start). Story starts at
    # len(closed) - 40.
    story_start = len(closed) - 40
    leg_start_pos = story_start + 9
    leg_end_pos = story_start + 19

    assert bool(swings["swing_low"].iloc[leg_start_pos]) is True
    assert bool(swings["swing_high"].iloc[leg_end_pos]) is True


def test_fib_levels_computed_correctly_for_known_leg():
    # LONG leg: 90 -> 110, span 20.
    price_0 = fib_retracement_price(90.0, 110.0, "LONG", 0.0)
    price_50 = fib_retracement_price(90.0, 110.0, "LONG", 0.5)
    price_618 = fib_retracement_price(90.0, 110.0, "LONG", 0.618)
    price_100 = fib_retracement_price(90.0, 110.0, "LONG", 1.0)

    assert price_0 == 110.0
    assert price_50 == 100.0
    assert abs(price_618 - 97.64) < 1e-9
    assert price_100 == 90.0

    ext_1272 = fib_extension_price(90.0, 110.0, "LONG", 1.272)
    assert abs(ext_1272 - 115.44) < 1e-9


def test_fib_levels_computed_correctly_for_short_leg():
    # SHORT leg: 110 -> 90, span 20 (leg_start=110 high, leg_end=90 low).
    price_0 = fib_retracement_price(110.0, 90.0, "SHORT", 0.0)
    price_50 = fib_retracement_price(110.0, 90.0, "SHORT", 0.5)
    price_100 = fib_retracement_price(110.0, 90.0, "SHORT", 1.0)

    assert price_0 == 90.0
    assert price_50 == 100.0
    assert price_100 == 110.0

    ext_1272 = fib_extension_price(110.0, 90.0, "SHORT", 1.272)
    assert abs(ext_1272 - 84.56) < 1e-9


def test_find_last_impulse_leg_long():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 90.0) < 0.01
    assert abs(leg.leg_end - 110.0) < 0.01
    assert leg.leg_start_idx < leg.leg_end_idx


def test_find_last_impulse_leg_short():
    df = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "SHORT", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 110.0) < 0.01
    assert abs(leg.leg_end - 90.0) < 0.01


def test_find_last_impulse_leg_returns_none_without_swings():
    # A monotonic (non-reversing) series has zero interior fractal swing
    # points -- each bar's fractal window max/min always falls at the
    # window's leading edge, never at the center. A perfectly flat/
    # duplicate-value series would instead tie for max/min at every
    # interior bar under find_swings' `==` comparison and spuriously
    # register as swings at every position, which isn't the "no swing
    # structure" case this test means to exercise.
    idx = pd.date_range("2026-01-01", periods=30, freq="1h")
    closes = 100.0 + np.arange(30) * 0.001
    flat = pd.DataFrame(
        {"open": closes, "high": closes + 0.1, "low": closes - 0.1, "close": closes, "volume": 1000.0},
        index=idx,
    )
    swings = find_swings(flat, fractal_bars=5)
    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)
    assert leg is None


def test_detect_trend_4h_long():
    df = make_4h_trend_df("LONG")
    closed = df.iloc[:-1]
    assert _detect_trend_4h(closed) == "LONG"


def test_detect_trend_4h_short():
    df = make_4h_trend_df("SHORT")
    closed = df.iloc[:-1]
    assert _detect_trend_4h(closed) == "SHORT"


def test_detect_trend_4h_none_when_flat():
    idx = pd.date_range("2026-01-01", periods=220, freq="4h")
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0},
        index=idx,
    )
    assert _detect_trend_4h(flat) is None


def test_detect_fib_entry_long_valid():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_fib_entry(closed, "LONG")

    assert ok is True, reason
    assert abs(details["leg_start"] - 90.0) < 0.01
    assert abs(details["leg_end"] - 110.0) < 0.01
    assert details["zone_low"] < details["zone_high"]


def test_detect_fib_entry_short_valid():
    df = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    closed = df.iloc[:-1]
    ok, reason, details = _detect_fib_entry(closed, "SHORT")

    assert ok is True, reason
    assert abs(details["leg_start"] - 110.0) < 0.01
    assert abs(details["leg_end"] - 90.0) < 0.01


def test_detect_fib_entry_rejected_without_swing_leg():
    # NOTE: deviates from the brief's literal fixture (a perfectly flat
    # series with constant open/high/low/close). A flat series ties for
    # max/min at every interior bar under find_swings' `==` comparison and
    # spuriously registers a "swing" at every position (verified: 136/136
    # interior bars flagged), so a leg WAS found and this test failed for
    # the wrong reason (rejected later at the confirmation-candle check
    # instead of at the leg-detection check). This is the exact same edge
    # case already called out in test_find_last_impulse_leg_returns_none_
    # without_swings above -- reusing its monotonic (non-tying) series
    # here for the same reason: it has no interior fractal extrema at all,
    # so find_swings correctly finds nothing.
    idx = pd.date_range("2026-01-01", periods=140, freq="1h")
    closes = 100.0 + np.arange(140) * 0.001
    flat = pd.DataFrame(
        {"open": closes, "high": closes + 0.1, "low": closes - 0.1, "close": closes, "volume": 1000.0},
        index=idx,
    )
    ok, reason, _ = _detect_fib_entry(flat, "LONG")
    assert ok is False
    assert reason == "no valid impulse leg found"


def test_detect_fib_entry_rejected_without_zone_touch():
    # NOTE: deviates from the brief's literal fixture args. The brief's
    # theory -- that a huge leg span (10 -> 1000) makes "the fixture's
    # fixed retracement offset land short of the zone" -- does not hold:
    # make_1h_fib_df's default retracement level (0.559 of the leg span)
    # is a *proportional* fraction, so it always lands inside the
    # 0.5-0.618 zone regardless of leg magnitude (verified: with
    # leg_start=10, leg_end=1000 the fixture still produced ok=True, zone
    # touch included). Isolating "no zone touch" requires moving the
    # fixture's retracement level itself outside the zone, so
    # tests/fib_fixtures.py gained a `retrace_fraction` parameter
    # (default 0.559, unchanged for every other test) -- passing 0.2 here
    # keeps the hold segment well above the 0.5-0.618 zone so it never
    # touches it before the confirmation candle.
    df = make_1h_fib_df("LONG", leg_start=10.0, leg_end=1000.0, retrace_fraction=0.2)
    closed = df.iloc[:-1]
    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason in ("no valid impulse leg found", "no zone touch")


def test_detect_fib_entry_rejected_when_rsi_out_of_band(monkeypatch):
    # The fixture's confirmation candle otherwise passes zone-touch and
    # direction checks -- force RSI into deep-oversold territory (well
    # outside the LONG band) to isolate the RSI gate specifically.
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]

    def _fake_rsi(series, period):
        return pd.Series(20.0, index=series.index)

    monkeypatch.setattr(fib_strategy, "calculate_rsi", _fake_rsi)

    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason.startswith("RSI")


def test_detect_fib_entry_rejected_when_volume_too_low(monkeypatch):
    df = make_1h_fib_df("LONG")
    # df has bars+1 rows (last one duplicated as the "forming" candle);
    # index -2 is the real confirmation candle that survives `iloc[:-1]`.
    df.iloc[-2, df.columns.get_loc("volume")] = 1.0
    closed = df.iloc[:-1]

    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason.startswith("volume ratio")


from fib_strategy import (
    _valid_trade_geometry, _calc_rr, _calculate_tp_sl_fib, _score_candidate_fib,
)


def test_long_trade_geometry():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=105.0, sl=98.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=95.0, sl=98.0) is False


def test_short_trade_geometry():
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=95.0, sl=102.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=105.0, sl=102.0) is False


def test_invalid_geometry_rejected_on_zero_price():
    assert _valid_trade_geometry("LONG", entry=0.0, tp=105.0, sl=98.0) is False


def test_calc_rr():
    rr = _calc_rr(entry=100.0, tp=106.0, sl=98.0)
    assert abs(rr - 3.0) < 1e-9


def test_calculate_tp_sl_fib_long_valid():
    # A tight, realistic leg (0.5% of price) -- config_fib.MAX_SL_PRICE_PCT
    # caps stops at 0.5% of entry (MAX_SL_ROI_PCT=10.0 / LEVERAGE=20), so
    # the leg magnitude must actually respect that cap for this to return
    # a valid (non-None) result.
    details = {"leg_start": 100.0, "leg_end": 100.5, "atr": 0.01}
    result = _calculate_tp_sl_fib("LONG", entry=100.3, details=details)

    assert result is not None
    tp, sl = result
    assert abs(tp - 100.636) < 1e-6           # 100.0 + 0.5*1.272
    assert sl < 100.0                          # leg_start minus ATR buffer
    assert tp > 100.3 > sl


def test_calculate_tp_sl_fib_short_valid():
    details = {"leg_start": 100.5, "leg_end": 100.0, "atr": 0.01}
    result = _calculate_tp_sl_fib("SHORT", entry=100.2, details=details)

    assert result is not None
    tp, sl = result
    assert abs(tp - 99.864) < 1e-6             # 100.5 - 0.5*1.272
    assert sl > 100.5
    assert tp < 100.2 < sl


def test_calculate_tp_sl_fib_rejects_stop_too_wide():
    # ATR buffer alone would blow past MAX_SL_PRICE_PCT for a tiny entry
    # price -- confirms the width guard works, not just the geometry sign.
    details = {"leg_start": 50.0, "leg_end": 110.0, "atr": 500.0}
    result = _calculate_tp_sl_fib("LONG", entry=100.5, details=details)
    assert result is None


def test_score_candidate_fib_within_bounds():
    details = {
        "close": 100.5, "zone_low": 97.64, "zone_high": 100.0,
        "rsi": 58.0, "volume_ratio": 1.8,
    }
    score = _score_candidate_fib("LONG", details, rr=2.5)
    assert 0.0 <= score <= 100.0


def _bullish_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=50100.0, ema_200=49500.0, one_candle_move_pct=0.1, three_candle_move_pct=0.3)


def _bearish_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=49500.0, ema_200=50100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)


def _extreme_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=50100.0, ema_200=49500.0, one_candle_move_pct=0.9, three_candle_move_pct=0.3)


def test_btc_filter_long_allowed_when_btc_bullish():
    ok, _ = _btc_filter_ok_4h("LONG", _bullish_btc_4h())
    assert ok is True


def test_btc_filter_long_blocked_when_btc_bearish():
    ok, reason = _btc_filter_ok_4h("LONG", _bearish_btc_4h())
    assert ok is False
    assert "bearish" in reason


def test_btc_filter_short_allowed_when_btc_bearish():
    ok, _ = _btc_filter_ok_4h("SHORT", _bearish_btc_4h())
    assert ok is True


def test_btc_filter_short_blocked_when_btc_bullish():
    ok, reason = _btc_filter_ok_4h("SHORT", _bullish_btc_4h())
    assert ok is False
    assert "bullish" in reason


def test_btc_filter_blocked_during_extreme_move():
    ok, reason = _btc_filter_ok_4h("LONG", _extreme_btc_4h())
    assert ok is False
    assert "volatility" in reason


def test_build_btc_context_4h_from_klines(monkeypatch):
    df_btc = make_4h_trend_df("LONG", bars=220)

    def _fake(symbol, interval, count=100):
        assert symbol == fib_strategy.config_fib.BTC_FILTER_SYMBOL
        assert interval == fib_strategy.config_fib.BTC_FILTER_TF
        return df_btc

    monkeypatch.setattr(fib_strategy, "get_market_klines", _fake)

    ctx = build_btc_context_4h()
    assert ctx is not None
    assert ctx.close > ctx.ema_200
