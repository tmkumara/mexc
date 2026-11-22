"""Causal Wilder ATR and strictly confirmed local pivots."""

from collections import deque

from .models import SwingPoint


class WilderATR:
    def __init__(self, period):
        self.period = period
        self.previous_close = None
        self.warmup = []
        self.value = None

    def update(self, candle):
        true_range = candle.high - candle.low
        if self.previous_close is not None:
            true_range = max(true_range, abs(candle.high - self.previous_close),
                             abs(candle.low - self.previous_close))
        self.previous_close = candle.close
        if self.value is None:
            self.warmup.append(true_range)
            if len(self.warmup) == self.period:
                self.value = sum(self.warmup) / self.period
        else:
            self.value += (true_range - self.value) / self.period
        return self.value


class ConfirmedPivots:
    def __init__(self, left, right):
        self.left = left
        self.window = deque(maxlen=left + right + 1)

    def update(self, candle):
        self.window.append(candle)
        if len(self.window) < self.window.maxlen:
            return None
        window = list(self.window)
        pivot = window[self.left]
        neighbors = window[:self.left] + window[self.left + 1:]
        high = all(pivot.high > bar.high for bar in neighbors)
        low = all(pivot.low < bar.low for bar in neighbors)
        if high == low:
            return None
        return SwingPoint("high" if high else "low", pivot.high if high else pivot.low,
                          pivot.timestamp, candle.closed_at)
