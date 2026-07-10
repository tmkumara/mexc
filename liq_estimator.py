"""
Liquidation-cluster estimator (no external liquidation feed).

When open interest RISES between two OI samples, new positions were opened
near the current price. This projects where those positions would liquidate
across a distribution of leverage tiers, and accumulates that magnitude into
price buckets. When price later sweeps through a bucket, the resting
liquidity there is cleared (those positions are gone, closed out). Clusters
also decay over time on every poll tick so stale estimates fade out.

The long/short split of newly opened OI is unknown (MEXC has no per-side OI
feed), so new OI is always split 50/50 across a projected long-liquidation
bucket (below price) and a short-liquidation bucket (above price).

Keep one LiqEstimator instance PER SYMBOL -- open interest, price, and
clusters are all symbol-specific.
"""

from __future__ import annotations

import numpy as np


def _bucket_price(price: float, bucket_pct: float) -> float:
    step = price * bucket_pct
    if step <= 0:
        return round(price, 8)
    return round(round(price / step) * step, 8)


class LiqEstimator:
    def __init__(
        self,
        leverage_tiers: dict[int, float],
        mmr_buffer: float,
        bucket_pct: float,
        decay: float,
        lookaround_pct: float,
        min_percentile: float,
        account_leverage: int,
    ):
        self.leverage_tiers = leverage_tiers
        self.mmr_buffer = mmr_buffer
        self.bucket_pct = bucket_pct
        self.decay = decay
        self.lookaround_pct = lookaround_pct
        self.min_percentile = min_percentile
        self.account_leverage = account_leverage
        self._clusters: dict[float, dict[str, float]] = {}
        self._last_oi: float | None = None
        self._last_price: float | None = None

    def on_oi_sample(self, oi_usdt: float, price: float) -> None:
        """Call once per poll with current open interest (USDT notional) and price."""
        if self._last_oi is not None:
            d_oi = oi_usdt - self._last_oi
            if d_oi > 0:
                self._distribute_new_positions(d_oi, price)
        self._last_oi = oi_usdt
        self._sweep(price)
        self._last_price = price

    def _distribute_new_positions(self, d_oi: float, entry_price: float) -> None:
        for lev, weight in self.leverage_tiers.items():
            dist = (1.0 / lev) - self.mmr_buffer / max(lev / self.account_leverage, 1)
            dist = max(dist, 0.002)
            long_liq = entry_price * (1 - dist)
            short_liq = entry_price * (1 + dist)
            mag = d_oi * weight * 0.5
            self._add(long_liq, "long", mag)
            self._add(short_liq, "short", mag)

    def _add(self, price: float, side: str, magnitude: float) -> None:
        key = _bucket_price(price, self.bucket_pct)
        bucket = self._clusters.setdefault(key, {"long": 0.0, "short": 0.0})
        bucket[side] += magnitude

    def _sweep(self, price: float) -> None:
        if self._last_price is None:
            return
        lo, hi = sorted((self._last_price, price))
        for key in [k for k in self._clusters if lo <= k <= hi]:
            del self._clusters[key]

    def decay_clusters(self) -> None:
        """Call once per poll tick (the caller controls cadence)."""
        dead = []
        for key, bucket in self._clusters.items():
            bucket["long"] *= self.decay
            bucket["short"] *= self.decay
            if bucket["long"] + bucket["short"] < 1e-9:
                dead.append(key)
        for key in dead:
            del self._clusters[key]

    def significant_clusters(self, price: float) -> list[tuple[float, str, float]]:
        """Return [(bucket_price, side, magnitude)] within the lookaround window,
        keeping only the top-percentile magnitudes."""
        window = price * self.lookaround_pct
        rows: list[tuple[float, str, float]] = []
        for key, bucket in self._clusters.items():
            if abs(key - price) > window:
                continue
            for side in ("long", "short"):
                if bucket[side] > 0:
                    rows.append((key, side, bucket[side]))
        if not rows:
            return []
        mags = np.array([r[2] for r in rows])
        threshold = np.percentile(mags, self.min_percentile)
        return [r for r in rows if r[2] >= threshold]

    def magnitude_between(self, p1: float, p2: float, side: str | None = None) -> float:
        lo, hi = sorted((p1, p2))
        total = 0.0
        for key, bucket in self._clusters.items():
            if lo <= key <= hi:
                total += (bucket["long"] + bucket["short"]) if side is None else bucket[side]
        return total
