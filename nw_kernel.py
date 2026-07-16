"""
Nadaraya-Watson rational-quadratic-kernel regression (non-repainting).

Ported from the Pine Script indicator "Nadaraya-Watson: Rational Quadratic
Kernel (Non-Repainting)" by jdehorty (MPL-2.0). `kernel_regression` there is
a causal (backward-looking only) kernel-weighted moving average, recomputed
fresh at every bar using only that bar and earlier ones -- non-repainting
because there is no centered/future-looking window. The weight of a bar `i`
positions back from the current bar is `(1 + i^2/(2*h^2*r))^-r`, decaying
with distance. `nw_estimate` here truncates the sum to the most recent
`window` bars per point as a numerical approximation of the Pine loop's
full-history sum -- see tests/test_nw_kernel.py for the accuracy check
against a literal, untruncated transcription of the Pine loop.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def kernel_weight(i: int, h: float, r: float) -> float:
    """Rational quadratic kernel weight for a bar `i` positions back."""
    return (1.0 + (i ** 2) / (h ** 2 * 2.0 * r)) ** (-r)


def nw_estimate(src: np.ndarray, h: float, r: float, window: int) -> np.ndarray:
    """Causal rational-quadratic kernel regression, one estimate per bar.

    For bar t, sums src[t], src[t-1], ..., src[max(0, t-window+1)] weighted
    by kernel_weight(i, h, r), i = distance back from t. Implemented as a
    causal convolution: full-mode np.convolve(src, w)[t] equals
    sum_i w[i]*src[t-i] over the valid (in-range) i, which is exactly this
    sum once sliced to the first len(src) outputs.
    """
    n = len(src)
    window = min(window, n)
    i = np.arange(window)
    w = (1.0 + (i ** 2) / (h ** 2 * 2.0 * r)) ** (-r)

    numerator = np.convolve(src, w, mode="full")[:n]
    denominator = np.convolve(np.ones(n), w, mode="full")[:n]
    return numerator / denominator


def detect_slope_flip(yhat: np.ndarray) -> pd.Series:
    """Per-bar bullish/bearish/none slope-flip classification, matching
    Pine's default (non-smoothColors) isBullishChange/isBearishChange: a
    bullish flip at bar t means yhat was falling into t-1 (yhat[t-2] >
    yhat[t-1]) and is now rising into t (yhat[t-1] < yhat[t]). Bearish is
    the mirror. The first two bars of any series are always "none", as is
    any bar whose flip comparison touches a NaN.
    """
    yhat = np.asarray(yhat, dtype=float)
    n = len(yhat)
    result = np.full(n, "none", dtype=object)

    for t in range(2, n):
        y0, y1, y2 = yhat[t - 2], yhat[t - 1], yhat[t]
        if np.isnan(y0) or np.isnan(y1) or np.isnan(y2):
            continue
        was_bearish = y0 > y1
        was_bullish = y0 < y1
        is_bearish = y1 > y2
        is_bullish = y1 < y2
        if is_bullish and was_bearish:
            result[t] = "bullish"
        elif is_bearish and was_bullish:
            result[t] = "bearish"

    return pd.Series(result)
