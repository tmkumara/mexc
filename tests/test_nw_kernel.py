import numpy as np
import pytest

import nw_kernel as NW


def test_rq_weights_shape_and_positivity():
    w = NW.rq_weights(10, h=8.0, r=8.0)
    assert w.shape == (10,)
    assert np.all(w > 0)
    assert np.all(np.diff(w) < 0)  # weight strictly decreases as bars get older


def test_nw_estimate_is_non_repainting():
    rng = np.random.default_rng(42)
    base = 100 + np.cumsum(rng.normal(0, 1, 80))
    extra = 100 + np.cumsum(rng.normal(0, 1, 20)) + base[-1]
    extended = np.concatenate([base, extra])
    k = 50
    estimate_before_extension = NW.nw_estimate(base[:k + 1])
    estimate_after_extension = NW.nw_estimate(extended[:k + 1])
    assert estimate_after_extension == pytest.approx(estimate_before_extension)


def test_nw_signal_detects_bullish_change_on_pullback_resume():
    closes = np.concatenate([np.linspace(100, 70, 60), [100.0]])
    assert NW.nw_signal(closes) == "bullish_change"


def test_nw_signal_detects_bearish_change_on_spike_reversal():
    closes = np.concatenate([np.linspace(100, 130, 60), [100.0]])
    assert NW.nw_signal(closes) == "bearish_change"


def test_nw_signal_none_on_flat_market():
    closes = np.full(80, 100.0)
    assert NW.nw_signal(closes) is None


def test_ema_ribbon_bias_long_on_uptrend():
    closes = 100 + np.cumsum(np.full(250, 0.3))
    assert NW.ema_ribbon_bias(closes) == "long"


def test_ema_ribbon_bias_short_on_downtrend():
    closes = 100 - np.cumsum(np.full(250, 0.3))
    assert NW.ema_ribbon_bias(closes) == "short"


def test_ema_ribbon_bias_neutral_on_flat_market():
    closes = np.full(250, 100.0)
    assert NW.ema_ribbon_bias(closes) == "neutral"


def _build_bullish_pullback_resume():
    pre = 100 + np.cumsum(np.full(400, 0.3))
    pullback = np.linspace(pre[-1], pre[-1] - 2, 10)
    resume = pullback[-1] + np.linspace(0, 1.0, 2)[1:]
    return np.concatenate([pre, pullback, resume])


def _build_bearish_bounce_resume():
    pre = 100 + np.cumsum(np.full(400, -0.3))
    bounce = np.linspace(pre[-1], pre[-1] + 2, 10)
    resume = bounce[-1] - np.linspace(0, 1.0, 2)[1:]
    return np.concatenate([pre, bounce, resume])


def test_base_signal_nw_fires_long_when_turn_agrees_with_ribbon():
    closes = _build_bullish_pullback_resume()
    assert NW.ema_ribbon_bias(closes) == "long"
    assert NW.nw_signal(closes) == "bullish_change"
    assert NW.base_signal_nw(closes) == "long"


def test_base_signal_nw_fires_short_when_turn_agrees_with_ribbon():
    closes = _build_bearish_bounce_resume()
    assert NW.ema_ribbon_bias(closes) == "short"
    assert NW.nw_signal(closes) == "bearish_change"
    assert NW.base_signal_nw(closes) == "short"


def test_base_signal_nw_none_when_turn_disagrees_with_ribbon():
    pre = 100 + np.cumsum(np.full(400, 0.3))
    spike = np.linspace(pre[-1], pre[-1] + 30, 60)
    jump_down = np.array([pre[-1]])
    closes = np.concatenate([pre, spike, jump_down])
    assert NW.ema_ribbon_bias(closes) == "long"
    assert NW.nw_signal(closes) == "bearish_change"
    assert NW.base_signal_nw(closes) is None
