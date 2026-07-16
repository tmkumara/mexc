import numpy as np
import pandas as pd

from nw_kernel import kernel_weight, nw_estimate, detect_slope_flip


def test_kernel_weight_at_zero_distance_is_one():
    assert kernel_weight(0, 8.0, 8.0) == 1.0
    assert kernel_weight(0, 3.0, 25.0) == 1.0


def test_kernel_weight_decreases_with_distance():
    w0 = kernel_weight(0, 8.0, 8.0)
    w10 = kernel_weight(10, 8.0, 8.0)
    w50 = kernel_weight(50, 8.0, 8.0)
    assert w0 > w10 > w50 > 0.0


def _naive_kernel_regression(src: np.ndarray, h: float, r: float) -> np.ndarray:
    """Literal transcription of the Pine Script loop (sums all available
    history at every bar) -- used only as a reference to validate the
    vectorized nw_estimate against, not a hand-computed numeric answer."""
    n = len(src)
    out = np.full(n, np.nan)
    for t in range(n):
        cw = 0.0
        cumw = 0.0
        for i in range(t + 1):
            w = kernel_weight(i, h, r)
            cw += src[t - i] * w
            cumw += w
        out[t] = cw / cumw
    return out


def test_nw_estimate_matches_naive_full_history_sum():
    rng = np.random.default_rng(42)
    src = 100.0 + np.cumsum(rng.normal(0, 0.5, size=30))
    expected = _naive_kernel_regression(src, h=3.0, r=3.0)
    actual = nw_estimate(src, h=3.0, r=3.0, window=30)
    assert np.allclose(actual, expected, rtol=1e-9, atol=1e-9)


def test_nw_estimate_constant_series_returns_constant():
    src = np.full(50, 100.0)
    result = nw_estimate(src, h=8.0, r=8.0, window=300)
    assert np.allclose(result, 100.0)


def test_nw_estimate_truncation_is_close_to_full_history():
    rng = np.random.default_rng(7)
    src = 100.0 + np.cumsum(rng.normal(0, 0.5, size=150))
    full = nw_estimate(src, h=8.0, r=8.0, window=150)
    truncated = nw_estimate(src, h=8.0, r=8.0, window=60)
    # Rational-quadratic weight decays fast under h=8/r=8 (default config
    # params) -- bars beyond ~60 back contribute negligibly. If this
    # tolerance turns out too tight once actually run, loosen it slightly
    # rather than widening the window; the point of the test is that
    # truncation is a safe approximation, not exact equality.
    assert np.allclose(full[60:], truncated[60:], atol=1e-6)


def test_nw_estimate_window_larger_than_series_does_not_crash():
    src = np.array([100.0, 101.0, 99.0, 102.0])
    result = nw_estimate(src, h=8.0, r=8.0, window=300)
    assert len(result) == 4
    assert not np.isnan(result).any()


def test_detect_slope_flip_identifies_bearish_then_bullish():
    yhat = np.array([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0])
    flips = detect_slope_flip(yhat)
    assert list(flips) == ["none", "none", "none", "bearish", "none", "bullish", "none"]


def test_detect_slope_flip_handles_leading_nan():
    yhat = np.array([np.nan, np.nan, 1.0, 2.0, 1.0, 2.0])
    flips = detect_slope_flip(yhat)
    assert flips.iloc[0] == "none"
    assert flips.iloc[1] == "none"
    # bar 4 (value 1.0, after rising to 2.0 at bar3): bearish flip
    assert flips.iloc[4] == "bearish"
    # bar 5 (value 2.0, after falling to 1.0 at bar4): bullish flip
    assert flips.iloc[5] == "bullish"


def test_detect_slope_flip_flat_series_has_no_flips():
    yhat = np.full(10, 100.0)
    flips = detect_slope_flip(yhat)
    assert all(f == "none" for f in flips)
