"""
Nadaraya-Watson regression, Rational Quadratic kernel -- non-repainting port
of jdehorty's Pine v5 indicator (MPL 2.0). Causal: bar i uses only bars <= i.

Signals (matching Pine):
  slope-change mode (smoothColors=false): bullish when yhat1 turns up
  crossover mode   (smoothColors=true) : yhat2 (h-lag) crossing yhat1

Self-contained pure-math module: no imports from strategy.py or config.py,
matching how liq_estimator.py stays independently testable.
"""

from __future__ import annotations

import numpy as np


def rq_weights(n: int, h: float, r: float) -> np.ndarray:
    """w_i = (1 + i^2 / (2*r*h^2))^(-r) for i = 0..n-1 (i=0 is newest bar)."""
    i = np.arange(n, dtype=float)
    return np.power(1.0 + (i ** 2) / (h ** 2 * 2.0 * r), -r)


def nw_estimate(closes: np.ndarray, h: float = 8.0, r: float = 8.0) -> float:
    """Kernel estimate at the LAST bar, using the trailing window like Pine."""
    win = min(len(closes), 500)   # cap for speed; weights ~0 beyond this
    seg = closes[-win:][::-1]     # newest first
    w = rq_weights(len(seg), h, r)
    return float(np.dot(seg, w) / w.sum())


def nw_series(closes: np.ndarray, h: float = 8.0, r: float = 8.0, tail: int = 6) -> np.ndarray:
    """Last `tail` estimates (enough for slope/cross detection)."""
    out = []
    for k in range(len(closes) - tail, len(closes)):
        out.append(nw_estimate(closes[:k + 1], h, r))
    return np.array(out)


def nw_signal(closes: np.ndarray, h: float = 8.0, r: float = 8.0,
              lag: int = 2, smooth: bool = False) -> str | None:
    """Returns 'bullish_change' | 'bearish_change' | None on the last closed bar."""
    if len(closes) < 60:
        return None
    y1 = nw_series(closes, h, r)
    if smooth:
        y2 = nw_series(closes, h - lag, r)
        if y2[-2] <= y1[-2] and y2[-1] > y1[-1]:
            return "bullish_change"
        if y2[-2] >= y1[-2] and y2[-1] < y1[-1]:
            return "bearish_change"
        return None
    was_bear = y1[-3] > y1[-2]
    was_bull = y1[-3] < y1[-2]
    is_bear = y1[-2] > y1[-1]
    is_bull = y1[-2] < y1[-1]
    if is_bull and was_bear:
        return "bullish_change"
    if is_bear and was_bull:
        return "bearish_change"
    return None


def ema(arr, n):
    a = np.asarray(arr, dtype=float)
    alpha = 2 / (n + 1)
    out = np.empty_like(a)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def ema_ribbon_bias(closes: np.ndarray, fast: int = 20, mid: int = 50,
                     slow: int = 100, trend: int = 200) -> str:
    """'long' | 'short' | 'neutral' from the fast/mid/slow/trend EMA stack."""
    e_fast, e_mid, e_slow, e_trend = (ema(closes, n)[-1] for n in (fast, mid, slow, trend))
    px = closes[-1]
    if e_fast > e_mid > e_slow and px > e_trend:
        return "long"
    if e_fast < e_mid < e_slow and px < e_trend:
        return "short"
    return "neutral"


def base_signal_nw(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2,
                    smooth: bool = False, fast: int = 20, mid: int = 50,
                    slow: int = 100, trend: int = 200) -> str | None:
    """Combined: NW slope-turn fires the trigger, EMA ribbon gates direction.
    Only take NW turns IN the direction of the ribbon (trend continuation)."""
    trig = nw_signal(closes, h, r, lag, smooth)
    if trig is None:
        return None
    bias = ema_ribbon_bias(closes, fast, mid, slow, trend)
    if trig == "bullish_change" and bias == "long":
        return "long"
    if trig == "bearish_change" and bias == "short":
        return "short"
    return None
