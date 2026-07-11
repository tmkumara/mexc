# Liquidation-Aware 1m Scalp (v14) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the VP-OB Confluence strategy (iteration #13) with a Liquidation-Aware 1m Scalp strategy (iteration #14) — EMA/RSI/VWAP base signal on 1m candles, gated by a free open-interest-derived liquidation-cluster estimator — while reusing all existing infrastructure (Telegram, DB schema, outcome tracking, coin pool, deploy).

**Architecture:** Two new/changed pure-math + wiring pieces plug into the existing two-phase `armed_setups` arm/monitor model unchanged: (1) `liq_estimator.py`, a new pure module estimating resting liquidation liquidity from open-interest deltas (analogous to `volume_profile.py`/`order_blocks.py` in the strategy it replaces); (2) `strategy.py` rewritten to compute a 1m EMA(9/21/50)+VWAP+RSI+volume base signal and gate it through the liquidation filter, arming a setup when the base signal fires and firing a `Signal` once the filter clears. `main.py` gains a 1-minute scan cadence (was hourly) and a staggered open-interest poll loop; everything else (`bot.py`'s message layout, `database.py`, `webui.py`, `reports.py`, `coin_scanner.py`, the WS modules) is untouched.

**Tech Stack:** Python 3.10, pandas, numpy, requests (existing `mexc_client.py` session) — no new dependencies.

## Global Constraints

- Branch `feature/liq-scalp-v14` is already checked out. Do **not** push to `main`; do not merge.
- 1m candle data comes from **REST polling**, not the dormant WebSocket path: confirmed with the user that `ws_manager.py`/`mexc_ws_client.py`/`candle_cache.py`/`market_data.py` are not currently wired into `main.py` at all (strategy.py has always called `mexc_client.get_klines()` directly), so this plan adds a new 1-minute `IntervalTrigger` scan job using the same REST + `ThreadPoolExecutor` pattern the hourly VP-OB scan already used. **None of those four WS-related files are touched.**
- `database.py`, `webui.py`, `reports.py`, `coin_scanner.py`, `clear_db.py`, `.github/workflows/deploy.yml`, deploy scripts (`deploy.sh`, `restart_bot.sh`, `clean_runtime_state.sh`) are **not touched** anywhere in this plan. `webui.py` reads strategy config via `_safe_config_value()` (`getattr(config, name, default)`), so it degrades to `"—"` for any removed constant without breaking — verified in `webui.py:51-56`.
- **One necessary exception to "keep `bot.py` unchanged":** `bot.py`'s `cmd_status` does a plain `from config import (OB_TF, DYN_EMA_MAX_LENGTH, ..., SETUP_SCAN_CRON_MINUTES, ...)` — a real import statement, not a safe lookup. Since this plan removes those exact config names, `/status` would raise `ImportError` on first use if left untouched. Task 8 makes the minimal mechanical fix: swap those names for their v14 equivalents in `cmd_status` only. `format_signal`, `broadcast_signal`, `notify_outcome`, and all command handlers otherwise are untouched — the new funding-rate/magnet info rides in the existing generic `Signal.timeframe_summary` string field, so the signal message layout itself needs zero edits.
- The `Signal` dataclass and `armed_setups`/`signals` table schemas are reused exactly as-is (no migration): `entry_low`/`entry_high` are set equal to `entry_price` for scalp setups (no retest zone, unlike the OB strategy), `score` becomes an RR-derived sort key, `rr`/`tp_roi_pct`/`sl_roi_pct` keep their existing meaning.
- Scope cuts from the reference `~liqbot-poc` implementation, made to fit the existing infra and avoid unrequested scope: no Binance `@forceOrder` cross-exchange liquidation feed (adds an external dependency never asked for); no BTC macro trend gate (VP-OB-specific, not part of the liq-scalp spec — the funding-rate veto is the macro filter for this strategy); no ATR-based stop (replaced by `MAX_SL_PRICE_PCT`, a flat price-distance cap, per the reference spec).
- `MAX_DAILY_SIGNALS`, `MIN_DAILY_SIGNAL_GAP_MINUTES`, and `SIGNAL_EXPIRE_HOURS` keep their current default values — **not silently changed** even though 1m scalping fires far more often than hourly VP-OB. Task 10's final summary flags these as things to retune by hand (suggested: 5-8 signals/day, 30 min gap, 2-4h expiry) once live behavior is observed.
- `order_blocks.py`, `volume_profile.py`, their tests, and `scripts/vp_ob_sanity_check.py` are deleted outright (Task 6) — the prior strategy is already preserved on `archive/v13-vp-ob-confluence`, matching how every previous strategy iteration in this repo has been retired.
- Direction strings are `"LONG"`/`"SHORT"` (uppercase), matching the existing `Signal`/`armed_setups` convention — not the reference's lowercase `"long"`/`"short"`.
- All new pure-math functions/classes take parameters explicitly (no `import config` inside `liq_estimator.py`), matching the existing style of `volume_profile.py`/`order_blocks.py`. `strategy.py` is the wiring layer that reads `config` and passes values in, exactly as it already does today.

---

### Task 1: `liq_estimator.py` — liquidation-cluster estimator

**Files:**
- Create: `liq_estimator.py`
- Test: `tests/test_liq_estimator.py`

**Interfaces:**
- Produces: `class LiqEstimator(leverage_tiers: dict[int, float], mmr_buffer: float, bucket_pct: float, decay: float, lookaround_pct: float, min_percentile: float, account_leverage: int)` with methods `on_oi_sample(oi_usdt: float, price: float) -> None`, `decay_clusters() -> None`, `significant_clusters(price: float) -> list[tuple[float, str, float]]`, `magnitude_between(p1: float, p2: float, side: str | None = None) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_liq_estimator.py`:

```python
import pytest

from liq_estimator import LiqEstimator


def _single_tier_estimator(**overrides):
    params = dict(
        leverage_tiers={10: 1.0},
        mmr_buffer=0.0,
        bucket_pct=0.01,
        decay=0.9,
        lookaround_pct=0.5,
        min_percentile=0,
        account_leverage=10,
    )
    params.update(overrides)
    return LiqEstimator(**params)


def test_rising_oi_accumulates_clusters_at_projected_liquidation_prices():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)   # first sample: baseline only
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)   # d_oi=200 -> distribute

    long_mag = est.magnitude_between(85.0, 95.0, side="long")
    short_mag = est.magnitude_between(105.0, 115.0, side="short")
    assert long_mag == pytest.approx(100.0)
    assert short_mag == pytest.approx(100.0)
    # no cross-contamination
    assert est.magnitude_between(85.0, 95.0, side="short") == pytest.approx(0.0)


def test_falling_oi_does_not_distribute_new_positions():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=800.0, price=100.0)   # d_oi negative -> no new clusters
    assert est.magnitude_between(0.0, 1000.0) == pytest.approx(0.0)


def test_price_sweep_clears_the_bucket_it_crosses():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)   # clusters at ~90 (long) and ~110 (short)
    assert est.magnitude_between(85.0, 95.0, side="long") == pytest.approx(100.0)

    est.on_oi_sample(oi_usdt=1200.0, price=85.0)    # price sweeps down through 90
    assert est.magnitude_between(85.0, 95.0, side="long") == pytest.approx(0.0)
    # the far cluster at ~110 was never crossed
    assert est.magnitude_between(105.0, 115.0, side="short") == pytest.approx(100.0)


def test_decay_shrinks_and_eventually_removes_clusters():
    est = _single_tier_estimator(decay=0.5)
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)
    assert est.magnitude_between(105.0, 115.0, side="short") == pytest.approx(100.0)

    for _ in range(100):
        est.decay_clusters()

    assert est.magnitude_between(0.0, 10_000_000.0) < 1e-6


def test_significant_clusters_respects_lookaround_window():
    est = _single_tier_estimator(lookaround_pct=0.05)   # window = +/-5 at price 100
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)        # clusters at ~90 and ~110, both 10 away
    assert est.significant_clusters(100.0) == []

    est_wide = _single_tier_estimator(lookaround_pct=0.2)   # window = +/-20
    est_wide.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est_wide.on_oi_sample(oi_usdt=1200.0, price=100.0)
    clusters = est_wide.significant_clusters(100.0)
    assert len(clusters) == 2
    sides = {c[1] for c in clusters}
    assert sides == {"long", "short"}


def test_significant_clusters_filters_by_percentile():
    est = LiqEstimator(
        leverage_tiers={10: 0.1, 50: 0.9},
        mmr_buffer=0.0,
        bucket_pct=0.01,
        decay=1.0,
        lookaround_pct=0.5,
        min_percentile=90,
        account_leverage=10,
    )
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=2000.0, price=100.0)   # d_oi=1000 -> two magnitude tiers per side

    clusters = est.significant_clusters(100.0)
    # only the lev=50 tier (magnitude 450) clears the 90th percentile of [50,50,450,450]
    assert len(clusters) == 2
    for _, _, magnitude in clusters:
        assert magnitude == pytest.approx(450.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_liq_estimator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'liq_estimator'`

- [ ] **Step 3: Implement `liq_estimator.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_liq_estimator.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add liq_estimator.py tests/test_liq_estimator.py
git commit -m "feat: add LiqEstimator pure module for liquidation-cluster estimation"
```

---

### Task 2: `config.py` — remove VP-OB constants, add liq-scalp constants

**Files:**
- Modify: `config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing new.
- Produces: `SCALP_TF`, `SCALP_KLINE_COUNT`, `EMA_FAST`, `EMA_MID`, `EMA_SLOW`, `RSI_PERIOD`, `RSI_LONG_MIN`, `RSI_LONG_MAX`, `RSI_SHORT_MIN`, `RSI_SHORT_MAX`, `SCALP_VOLUME_MIN_MULT`, `SCALP_VOLUME_MA_BARS`, `TARGET_MARGIN_PROFIT`, `MIN_RR`, `MAX_SL_PRICE_PCT`, `LEVERAGE_TIERS: dict[int, float]`, `MMR_BUFFER`, `BUCKET_PCT`, `CLUSTER_DECAY`, `CLUSTER_LOOKAROUND`, `CLUSTER_MIN_PERCENTILE`, `OI_POLL_SEC`, `FUNDING_EXTREME`, `SCALP_ARM_MAX_AGE_BARS`, `SCALP_SCAN_INTERVAL_MINUTES` (all read by Task 4/5's `strategy.py`, `main.py`). `LEVERAGE` (existing, unchanged, default `20`) is reused as-is.

- [ ] **Step 1: Remove the VP-OB-specific config block**

In `config.py`, delete lines 57-103 (the `STRATEGY_NAME` value plus everything from `# ── Volume Profile ...` through `# ── BTC macro gate ─` inclusive — i.e. all `VP_*`, `OB_*`, `DYN_EMA_*`, `ATR_PERIOD`, `SL_ATR_BUFFER_MULT`, `MIN_STRUCTURE_RR`, `MIN_TP_ROI_PCT`, `TARGET_TP_ROI_PCT`, `MAX_SL_ROI_PCT`, `MIN_SETUP_SCORE`, `BTC_SYMBOL`, `BTC_TF`, `BTC_KLINE_COUNT`, `BTC_GATE_ENABLED`, `BTC_RANGING_PCT`), and also delete `SETUP_SCAN_CRON_MINUTES`/`SETUP_SCAN_CRON_HOURS` (lines 110-111).

- [ ] **Step 2: Add the liq-scalp config block**

Insert in their place:

```python
# ── Strategy: Liquidation-Aware 1m Scalp (v14) ──────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Liquidation-Aware 1m Scalp (v14)",
)

# ── Base signal (1m EMA/RSI/VWAP/volume) ────────────────────────────
SCALP_TF: str               = os.getenv("SCALP_TF", "1m")
SCALP_KLINE_COUNT: int      = int(os.getenv("SCALP_KLINE_COUNT", "100"))
EMA_FAST: int                = int(os.getenv("EMA_FAST", "9"))
EMA_MID: int                 = int(os.getenv("EMA_MID", "21"))
EMA_SLOW: int                = int(os.getenv("EMA_SLOW", "50"))
RSI_PERIOD: int               = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN: float           = float(os.getenv("RSI_LONG_MIN", "50"))
RSI_LONG_MAX: float           = float(os.getenv("RSI_LONG_MAX", "68"))
RSI_SHORT_MIN: float          = float(os.getenv("RSI_SHORT_MIN", "32"))
RSI_SHORT_MAX: float          = float(os.getenv("RSI_SHORT_MAX", "50"))
SCALP_VOLUME_MIN_MULT: float  = float(os.getenv("SCALP_VOLUME_MIN_MULT", "1.3"))
SCALP_VOLUME_MA_BARS: int     = int(os.getenv("SCALP_VOLUME_MA_BARS", "20"))

# ── Profit target / risk (price move = margin target / leverage) ───
TARGET_MARGIN_PROFIT: float  = float(os.getenv("TARGET_MARGIN_PROFIT", "0.12"))
MIN_RR: float                 = float(os.getenv("MIN_RR", "1.5"))
MAX_SL_PRICE_PCT: float       = float(os.getenv("MAX_SL_PRICE_PCT", "0.0032"))

# ── Liquidation cluster estimator (see liq_estimator.py) ────────────
_LEVERAGE_TIERS_DEFAULT = "10:0.20,20:0.25,25:0.20,50:0.20,75:0.10,100:0.05"
LEVERAGE_TIERS: dict[int, float] = {}
for _pair in os.getenv("LEVERAGE_TIERS", _LEVERAGE_TIERS_DEFAULT).split(","):
    _lev_str, _weight_str = _pair.split(":")
    LEVERAGE_TIERS[int(_lev_str)] = float(_weight_str)

MMR_BUFFER: float            = float(os.getenv("MMR_BUFFER", "0.006"))
BUCKET_PCT: float             = float(os.getenv("BUCKET_PCT", "0.0005"))
CLUSTER_DECAY: float          = float(os.getenv("CLUSTER_DECAY", "0.97"))
CLUSTER_LOOKAROUND: float     = float(os.getenv("CLUSTER_LOOKAROUND", "0.02"))
CLUSTER_MIN_PERCENTILE: float = float(os.getenv("CLUSTER_MIN_PERCENTILE", "90"))
OI_POLL_SEC: int              = int(os.getenv("OI_POLL_SEC", "60"))

# ── Funding filter ───────────────────────────────────────────────────
FUNDING_EXTREME: float        = float(os.getenv("FUNDING_EXTREME", "0.0004"))

# ── Armed-setup lifetime (in 1m bars) ────────────────────────────────
SCALP_ARM_MAX_AGE_BARS: int   = int(os.getenv("SCALP_ARM_MAX_AGE_BARS", "10"))

# ── Scan cadence ─────────────────────────────────────────────────────
SCALP_SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCALP_SCAN_INTERVAL_MINUTES", "1"))
```

- [ ] **Step 3: Fix the `CANDLE_MINUTES` derivation**

Find (near the bottom of `config.py`):

```python
# ── Candle minutes (derived from OB_TF) ────────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(OB_TF, 60))))
```

Replace with:

```python
# ── Candle minutes (derived from SCALP_TF) ─────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(SCALP_TF, 1))))
```

- [ ] **Step 4: Verify `config.py` still imports cleanly**

Run: `python -c "import config; print(config.STRATEGY_NAME, config.LEVERAGE_TIERS, config.CANDLE_MINUTES)"`
Expected: prints `Liquidation-Aware 1m Scalp (v14) {10: 0.2, 20: 0.25, 25: 0.2, 50: 0.2, 75: 0.1, 100: 0.05} 1` with no traceback.

- [ ] **Step 5: Update `.env.example`**

Append to `.env.example`:

```bash
# Liquidation-Aware 1m Scalp (v14) tuning -- see config.py for full defaults
TARGET_MARGIN_PROFIT=0.12
MIN_RR=1.5
MAX_SL_PRICE_PCT=0.0032
LEVERAGE_TIERS=10:0.20,20:0.25,25:0.20,50:0.20,75:0.10,100:0.05
MMR_BUFFER=0.006
BUCKET_PCT=0.0005
CLUSTER_DECAY=0.97
CLUSTER_LOOKAROUND=0.02
CLUSTER_MIN_PERCENTILE=90
OI_POLL_SEC=60
FUNDING_EXTREME=0.0004

# 1m scalping fires much more often than the old hourly VP-OB strategy.
# MAX_DAILY_SIGNALS / MIN_DAILY_SIGNAL_GAP_MINUTES / SIGNAL_EXPIRE_HOURS
# keep their existing defaults until you've watched live behavior -- once
# you have, consider retuning towards ~5-8 signals/day, ~30min gap, and a
# 2-4h expiry (a 1m scalp sitting "pending" for 48h no longer makes sense).
```

- [ ] **Step 6: Commit**

```bash
git add config.py .env.example
git commit -m "feat: replace VP-OB config with Liquidation-Aware 1m Scalp (v14) config"
```

---

### Task 3: `mexc_client.py` — `get_ticker()`

**Files:**
- Modify: `mexc_client.py`
- Test: `tests/test_mexc_client.py`

**Interfaces:**
- Consumes: `mexc_client._get(path, params=None, retries=5) -> dict` (existing).
- Produces: `get_ticker(symbol: str) -> dict | None` returning `{"fair_price": float, "hold_vol": float, "funding_rate": float}` or `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mexc_client.py`:

```python
import mexc_client


def test_get_ticker_parses_list_response(monkeypatch):
    def fake_get(path, params=None, retries=5):
        assert path == "/contract/ticker"
        assert params == {"symbol": "BTC_USDT"}
        return {"data": [
            {"symbol": "ETH_USDT", "fairPrice": "1.0", "holdVol": "1.0", "fundingRate": "0.0"},
            {"symbol": "BTC_USDT", "fairPrice": "65000.5", "holdVol": "12345.0", "fundingRate": "0.0001"},
        ]}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0001}


def test_get_ticker_parses_dict_response(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5",
                          "holdVol": "12345.0", "fundingRate": "0.0001"}}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0001}


def test_get_ticker_returns_none_when_symbol_missing(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": [{"symbol": "ETH_USDT", "fairPrice": "1.0", "holdVol": "1.0", "fundingRate": "0.0"}]}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    assert mexc_client.get_ticker("BTC_USDT") is None


def test_get_ticker_returns_none_on_missing_required_field(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5"}}   # holdVol missing
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    assert mexc_client.get_ticker("BTC_USDT") is None


def test_get_ticker_defaults_funding_rate_to_zero_when_absent(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5", "holdVol": "12345.0"}}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mexc_client.py -v`
Expected: FAIL with `AttributeError: module 'mexc_client' has no attribute 'get_ticker'`

- [ ] **Step 3: Implement `get_ticker()`**

Append to `mexc_client.py` (after `get_current_price`):

```python
def get_ticker(symbol: str) -> dict | None:
    """
    Return {"fair_price", "hold_vol", "funding_rate"} for a symbol from
    GET /contract/ticker, or None if the symbol isn't present or a required
    field is missing.
    """
    try:
        data = _get("/contract/ticker", params={"symbol": symbol})
        tickers = data.get("data")

        if isinstance(tickers, list):
            row = next((t for t in tickers if t.get("symbol") == symbol), None)
        elif isinstance(tickers, dict):
            row = tickers
        else:
            row = None

        if not row:
            return None

        return {
            "fair_price": float(row["fairPrice"]),
            "hold_vol": float(row["holdVol"]),
            "funding_rate": float(row.get("fundingRate", 0.0)),
        }
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mexc_client.py -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mexc_client.py tests/test_mexc_client.py
git commit -m "feat: add get_ticker() for open-interest/funding polling"
```

---

### Task 4: `strategy.py` — rewrite as the Liquidation-Aware 1m Scalp strategy

**Files:**
- Modify: `strategy.py` (full rewrite)
- Test: `tests/test_strategy_liq_scalp.py`

**Interfaces:**
- Consumes: `LiqEstimator` (Task 1), `config.SCALP_*`/`EMA_*`/`RSI_*`/`TARGET_MARGIN_PROFIT`/`MIN_RR`/`MAX_SL_PRICE_PCT`/`LEVERAGE_TIERS`/`MMR_BUFFER`/`BUCKET_PCT`/`CLUSTER_*`/`FUNDING_EXTREME`/`SCALP_ARM_MAX_AGE_BARS`/`LEVERAGE` (Task 2), `mexc_client.get_klines` (existing).
- Produces (must stay byte-for-byte compatible with `bot.py`'s `format_signal` and `main.py`'s `scan_and_fire_signals`, both unmodified): `Signal` dataclass with fields `symbol, direction, entry_price, tp_price, sl_price, leverage, tp_roi_pct, sl_roi_pct, timeframe_summary, generated_at, rr, score, entry_low, entry_high, armed_setup_id`; `monitor_symbol(symbol: str) -> Signal | None`; `arm_symbol(symbol: str) -> None`. New public surface for Task 5 (`main.py`): `get_estimator(symbol: str) -> LiqEstimator`, `update_ticker_cache(symbol: str, fair_price: float, funding_rate: float) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_strategy_liq_scalp.py`:

```python
import numpy as np
import pandas as pd
import pytest

from liq_estimator import LiqEstimator
from strategy import _base_signal, _evaluate_liquidity, _valid_trade_geometry


def _build_df(pattern: list[float], n_bars: int = 60, base_vol: float = 100.0, spike_vol: float = 500.0) -> pd.DataFrame:
    deltas = (pattern * (n_bars // 3 + 1))[: n_bars - 1]
    closes = [100.0]
    for d in deltas:
        closes.append(closes[-1] + d)
    closes = np.array(closes)
    opens = np.concatenate(([closes[0]], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    volumes = np.full(n_bars, base_vol)
    volumes[-1] = spike_vol
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


def test_base_signal_detects_long_on_uptrend_with_volume_confirmation():
    df = _build_df([4, 4, -5])
    assert _base_signal(df) == "LONG"


def test_base_signal_detects_short_on_downtrend_with_volume_confirmation():
    df = _build_df([-4, -4, 5])
    assert _base_signal(df) == "SHORT"


def test_base_signal_none_on_flat_market():
    df = pd.DataFrame({
        "open": [100.0] * 60, "high": [100.5] * 60, "low": [99.5] * 60,
        "close": [100.0] * 60, "volume": [100.0] * 60,
    })
    assert _base_signal(df) is None


def test_base_signal_none_on_insufficient_history():
    df = _build_df([4, 4, -5], n_bars=30)
    assert _base_signal(df) is None


def _estimator_with_magnet_both_sides() -> LiqEstimator:
    est = LiqEstimator(
        leverage_tiers={20: 1.0},
        mmr_buffer=0.0,
        bucket_pct=0.0005,
        decay=1.0,
        lookaround_pct=0.06,
        min_percentile=0,
        account_leverage=20,
    )
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=3000.0, price=100.0)   # d_oi=2000 -> clusters at ~95 (long) and ~105 (short)
    return est


def test_evaluate_liquidity_long_passes_with_magnet_above():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0, estimator=est)
    assert ok is True
    assert tp == pytest.approx(100.6)
    assert sl == pytest.approx(99.68)
    assert "RR" in reason


def test_evaluate_liquidity_short_passes_with_magnet_below():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("SHORT", 100.0, funding=0.0, estimator=est)
    assert ok is True
    assert tp == pytest.approx(99.4)
    assert sl == pytest.approx(100.32)


def test_evaluate_liquidity_vetoes_when_no_magnet():
    est = LiqEstimator(
        leverage_tiers={20: 1.0}, mmr_buffer=0.0, bucket_pct=0.0005,
        decay=1.0, lookaround_pct=0.06, min_percentile=0, account_leverage=20,
    )
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0, estimator=est)
    assert ok is False
    assert tp is None and sl is None
    assert "no magnet" in reason


def test_evaluate_liquidity_vetoes_on_extreme_funding():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0005, estimator=est)
    assert ok is False
    assert "funding" in reason


def test_valid_trade_geometry():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=101.0, sl=99.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=99.0, sl=101.0) is False
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=99.0, sl=101.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=101.0, sl=99.0) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_strategy_liq_scalp.py -v`
Expected: FAIL — `strategy.py` still exposes the old VP-OB internals (`_atr_series`, `_try_arm_setup` referencing `order_blocks`/`volume_profile`), not `_base_signal`/`_evaluate_liquidity`.

- [ ] **Step 3: Rewrite `strategy.py`**

Replace the entire file with:

```python
"""
Liquidation-Aware 1m Scalp strategy (v14).

Two-phase workflow, persisted via the `armed_setups` DB table (same table
the prior VP-OB strategy used -- schema unchanged):

Phase 1 (arm): on each pooled coin, compute the 1m EMA(9/21/50) stack +
rolling VWAP side + RSI zone + volume confirmation ("base signal"). If a
base signal fires, evaluate the liquidation-cluster filter immediately; if
it already clears, arm the setup with real levels. If it doesn't yet clear
(no magnet cluster ahead, magnet too close, a larger opposing pool behind
entry, funding extreme against direction, or no clean stop placement), arm
the setup anyway with provisional levels so the next few 1m closes can be
re-checked without re-deriving the base signal from scratch.

Phase 2 (monitor): each 1m close, re-run the base signal and (if still
active) the liquidity filter against the armed setup's direction using the
latest price/cluster state. Fires a Signal the moment the filter passes,
invalidates the setup once the base signal itself drops, and expires it
after SCALP_ARM_MAX_AGE_BARS minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import database as db
from liq_estimator import LiqEstimator
from mexc_client import get_klines
from config import (
    SCALP_TF,
    SCALP_KLINE_COUNT,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_PERIOD,
    RSI_LONG_MIN,
    RSI_LONG_MAX,
    RSI_SHORT_MIN,
    RSI_SHORT_MAX,
    SCALP_VOLUME_MIN_MULT,
    SCALP_VOLUME_MA_BARS,
    TARGET_MARGIN_PROFIT,
    MIN_RR,
    MAX_SL_PRICE_PCT,
    LEVERAGE_TIERS,
    MMR_BUFFER,
    BUCKET_PCT,
    CLUSTER_DECAY,
    CLUSTER_LOOKAROUND,
    CLUSTER_MIN_PERCENTILE,
    FUNDING_EXTREME,
    SCALP_ARM_MAX_AGE_BARS,
    LEVERAGE,
)

logger = logging.getLogger(__name__)

TP_PRICE_PCT = TARGET_MARGIN_PROFIT / LEVERAGE

# One LiqEstimator + latest ticker snapshot per symbol, fed by main.py's OI poll loop.
_estimators: dict[str, LiqEstimator] = {}
_ticker_cache: dict[str, dict] = {}


@dataclass
class Signal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    tp_roi_pct: float
    sl_roi_pct: float
    timeframe_summary: str
    generated_at: datetime
    rr: float = 0.0
    score: float = 0.0
    entry_low: float = 0.0
    entry_high: float = 0.0
    armed_setup_id: int | None = None


def get_estimator(symbol: str) -> LiqEstimator:
    est = _estimators.get(symbol)
    if est is None:
        est = LiqEstimator(
            leverage_tiers=LEVERAGE_TIERS,
            mmr_buffer=MMR_BUFFER,
            bucket_pct=BUCKET_PCT,
            decay=CLUSTER_DECAY,
            lookaround_pct=CLUSTER_LOOKAROUND,
            min_percentile=CLUSTER_MIN_PERCENTILE,
            account_leverage=LEVERAGE,
        )
        _estimators[symbol] = est
    return est


def update_ticker_cache(symbol: str, fair_price: float, funding_rate: float) -> None:
    _ticker_cache[symbol] = {"fair_price": fair_price, "funding_rate": funding_rate}


def _latest_funding(symbol: str) -> float:
    return _ticker_cache.get(symbol, {}).get("funding_rate", 0.0)


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


# ── indicators ──────────────────────────────────────────────────────

def _ema(values: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(closes: np.ndarray, n: int) -> np.ndarray:
    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = np.empty(len(diffs))
    avg_loss = np.empty(len(diffs))
    avg_gain[0] = gains[:n].mean()
    avg_loss[0] = losses[:n].mean()
    for i in range(1, len(diffs)):
        avg_gain[i] = (avg_gain[i - 1] * (n - 1) + gains[i]) / n
        avg_loss[i] = (avg_loss[i - 1] * (n - 1) + losses[i]) / n
    rs = avg_gain / np.where(avg_loss == 0, 1e-12, avg_loss)
    return np.concatenate(([50.0], 100 - 100 / (1 + rs)))


def _rolling_vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    typical = (high + low + close) / 3.0
    return np.cumsum(typical * volume) / np.maximum(np.cumsum(volume), 1e-12)


def _base_signal(df: pd.DataFrame) -> str | None:
    """EMA 9/21/50 stack + rolling VWAP side + RSI zone + volume confirmation."""
    close = df["close"].astype(float).to_numpy()
    if len(close) < EMA_SLOW + 5:
        return None
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()

    e_fast = _ema(close, EMA_FAST)[-1]
    e_mid = _ema(close, EMA_MID)[-1]
    e_slow = _ema(close, EMA_SLOW)[-1]
    r = _rsi(close, RSI_PERIOD)[-1]
    vwap = _rolling_vwap(high, low, close, volume)[-1]
    vol_ok = volume[-1] > SCALP_VOLUME_MIN_MULT * volume[-(SCALP_VOLUME_MA_BARS + 1): -1].mean()
    price = close[-1]

    if e_fast > e_mid > e_slow and price > vwap and RSI_LONG_MIN < r < RSI_LONG_MAX and vol_ok:
        return "LONG"
    if e_fast < e_mid < e_slow and price < vwap and RSI_SHORT_MIN < r < RSI_SHORT_MAX and vol_ok:
        return "SHORT"
    return None


# ── liquidity filter ─────────────────────────────────────────────────

def _stop_below(price: float, clusters: list[tuple[float, str, float]]) -> float | None:
    max_sl = price * (1 - MAX_SL_PRICE_PCT)
    blockers = [c[0] for c in clusters if max_sl <= c[0] < price]
    sl = (min(blockers) * 0.9985) if blockers else max_sl
    return sl if sl >= price * (1 - MAX_SL_PRICE_PCT * 1.5) else None


def _stop_above(price: float, clusters: list[tuple[float, str, float]]) -> float | None:
    max_sl = price * (1 + MAX_SL_PRICE_PCT)
    blockers = [c[0] for c in clusters if price < c[0] <= max_sl]
    sl = (max(blockers) * 1.0015) if blockers else max_sl
    return sl if sl <= price * (1 + MAX_SL_PRICE_PCT * 1.5) else None


def _evaluate_liquidity(
    direction: str, price: float, funding: float, estimator: LiqEstimator,
) -> tuple[bool, float | None, float | None, str]:
    """Returns (ok, tp, sl, reason)."""
    clusters = estimator.significant_clusters(price)
    tp_dist = price * TP_PRICE_PCT
    magnet = None

    if direction == "LONG":
        above = [c for c in clusters if c[0] > price and c[1] == "short"]
        if not above:
            return False, None, None, "no short-liq cluster above (no magnet)"
        magnet = min(above, key=lambda c: c[0])
        if magnet[0] < price * (1 + TP_PRICE_PCT * 0.6):
            return False, None, None, "magnet too close - move likely exhausted"
        tp = min(magnet[0] * 0.999, price + tp_dist)
        danger = estimator.magnitude_between(price * 0.997, price, side="long")
        pull = estimator.magnitude_between(price, magnet[0], side="short")
        if danger > pull:
            return False, None, None, "larger liq pool just below entry"
        sl = _stop_below(price, clusters)
        if funding > FUNDING_EXTREME:
            return False, None, None, "crowded longs (funding extreme) - long veto"
    else:
        below = [c for c in clusters if c[0] < price and c[1] == "long"]
        if not below:
            return False, None, None, "no long-liq cluster below (no magnet)"
        magnet = max(below, key=lambda c: c[0])
        if magnet[0] > price * (1 - TP_PRICE_PCT * 0.6):
            return False, None, None, "magnet too close - move likely exhausted"
        tp = max(magnet[0] * 1.001, price - tp_dist)
        danger = estimator.magnitude_between(price, price * 1.003, side="short")
        pull = estimator.magnitude_between(magnet[0], price, side="long")
        if danger > pull:
            return False, None, None, "larger liq pool just above entry"
        sl = _stop_above(price, clusters)
        if funding < -FUNDING_EXTREME:
            return False, None, None, "crowded shorts (funding extreme) - short veto"

    if sl is None:
        return False, None, None, "no clean stop placement (dense cluster in the way)"
    if not _valid_trade_geometry(direction, price, tp, sl):
        return False, None, None, "invalid trade geometry"

    rr = abs(tp - price) / abs(price - sl)
    if rr < MIN_RR:
        return False, None, None, f"RR {rr:.2f} below minimum {MIN_RR}"

    return True, tp, sl, f"RR {rr:.2f}, magnet at {magnet[0]:.6g}, funding {funding * 100:.4f}%"


def _roi(direction: str, entry: float, tp: float, sl: float) -> tuple[float, float, float]:
    if direction == "LONG":
        risk_pct = (entry - sl) / entry * 100.0
        reward_pct = (tp - entry) / entry * 100.0
    else:
        risk_pct = (sl - entry) / entry * 100.0
        reward_pct = (entry - tp) / entry * 100.0
    rr = reward_pct / risk_pct if risk_pct > 0 else 0.0
    return round(reward_pct * LEVERAGE, 1), round(risk_pct * LEVERAGE, 1), round(rr, 2)


def _score_from_rr(rr: float) -> float:
    return round(min(100.0, max(0.0, rr) * 20.0), 1)


# ── Phase 1: arm ──────────────────────────────────────────────────────

def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
    if df is None or df.empty or len(df) < EMA_SLOW + 6:
        return
    window = df.iloc[:-1]   # last CLOSED 1m bar only

    direction = _base_signal(window)
    if direction is None:
        return

    price = float(window["close"].iloc[-1])
    funding = _latest_funding(symbol)
    estimator = get_estimator(symbol)
    ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=SCALP_ARM_MAX_AGE_BARS)

    if ok:
        tp_roi, sl_roi, rr = _roi(direction, price, tp, sl)
        db.save_armed_setup({
            "symbol": symbol,
            "direction": direction,
            "trigger_price": price,
            "entry_low": price,
            "entry_high": price,
            "sl_price": sl,
            "tp_price": tp,
            "rr": rr,
            "score": _score_from_rr(rr),
            "setup_reason": reason,
            "trend_summary": f"1m base signal + liq filter passed | funding {funding * 100:.4f}%",
            "expires_at": expires_at.isoformat(),
        })
        logger.info("[SCALP-ARM] %s %s price=%.6g tp=%.6g sl=%.6g rr=%.2f (%s)",
                    symbol, direction, price, tp, sl, rr, reason)
        return

    # Base signal fired but the liquidity filter hasn't cleared yet -- arm
    # with provisional levels so main.py's monitor pass keeps re-checking
    # this symbol every cycle instead of re-deriving the base signal.
    provisional_sl = price * (1 - MAX_SL_PRICE_PCT) if direction == "LONG" else price * (1 + MAX_SL_PRICE_PCT)
    provisional_tp = price * (1 + TP_PRICE_PCT) if direction == "LONG" else price * (1 - TP_PRICE_PCT)
    provisional_rr = TP_PRICE_PCT / MAX_SL_PRICE_PCT
    db.save_armed_setup({
        "symbol": symbol,
        "direction": direction,
        "trigger_price": price,
        "entry_low": price,
        "entry_high": price,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(provisional_rr, 2),
        "score": 0.0,
        "setup_reason": reason,
        "trend_summary": f"1m base signal (awaiting liq filter) | funding {funding * 100:.4f}%",
        "expires_at": expires_at.isoformat(),
    })
    logger.info("[SCALP-WAIT] %s %s price=%.6g veto=%s", symbol, direction, price, reason)


# ── Phase 2: monitor ──────────────────────────────────────────────────

def _monitor_setup(symbol: str, setup: dict) -> Signal | None:
    direction = setup["direction"]

    expires_at = datetime.fromisoformat(setup["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        db.mark_armed_setup_expired(setup["id"])
        logger.info("[SCALP-EXPIRE] %s setup #%s aged out", symbol, setup["id"])
        return None

    df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
    if df is None or df.empty or len(df) < EMA_SLOW + 6:
        return None
    window = df.iloc[:-1]

    if _base_signal(window) != direction:
        db.mark_armed_setup_invalidated(setup["id"], "base signal no longer active")
        logger.info("[SCALP-INVALIDATE] %s %s base signal dropped", symbol, direction)
        return None

    price = float(window["close"].iloc[-1])
    funding = _latest_funding(symbol)
    estimator = get_estimator(symbol)
    ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)
    if not ok:
        logger.debug("[SCALP-WAIT] %s %s still vetoed: %s", symbol, direction, reason)
        return None

    tp_roi, sl_roi, rr = _roi(direction, price, tp, sl)

    logger.info("[SCALP-SIGNAL] %s %s entry=%.6g tp=%.6g sl=%.6g rr=%.2f (%s)",
                symbol, direction, price, tp, sl, rr, reason)

    return Signal(
        symbol=symbol,
        direction=direction,
        entry_price=round(price, 8),
        tp_price=round(tp, 8),
        sl_price=round(sl, 8),
        leverage=LEVERAGE,
        tp_roi_pct=tp_roi,
        sl_roi_pct=sl_roi,
        timeframe_summary=f"1m liq-scalp | {reason}",
        generated_at=datetime.now(timezone.utc),
        rr=rr,
        score=_score_from_rr(rr),
        entry_low=price,
        entry_high=price,
        armed_setup_id=setup["id"],
    )


# ── Public: scan one symbol ───────────────────────────────────────

def monitor_symbol(symbol: str) -> Signal | None:
    """
    Check only an already-armed setup for this symbol. Does not arm a new
    setup. Safe to call every cycle regardless of any firing-budget
    throttle in the caller.
    """
    try:
        existing = db.get_armed_setup_by_symbol(symbol)
        if existing is not None:
            return _monitor_setup(symbol, existing)
        return None
    except Exception as e:
        logger.error("Error monitoring %s: %s", symbol, e, exc_info=True)
        return None


def arm_symbol(symbol: str) -> None:
    """
    Try to arm a new setup for this symbol if none is currently armed. Does
    not check for or fire retests -- call monitor_symbol for that.
    """
    try:
        if db.get_armed_setup_by_symbol(symbol) is None:
            _try_arm_setup(symbol)
    except Exception as e:
        logger.error("Error arming %s: %s", symbol, e, exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_strategy_liq_scalp.py -v`
Expected: all 9 tests PASS

- [ ] **Step 5: Compile-check the whole app**

Run: `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py`
Expected: no output, exit code 0. (This will still fail at this point because `main.py` imports `OB_TF`/`SETUP_SCAN_CRON_*` which Task 2 removed, and `bot.py` imports the same — both are fixed in Tasks 5 and 8. Confirm the failures are *only* those two files' import errors, not `strategy.py`.)

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/test_strategy_liq_scalp.py
git commit -m "feat: rewrite strategy.py as Liquidation-Aware 1m Scalp (v14)"
```

---

### Task 5: `scripts/liq_sanity_check.py`

**Files:**
- Create: `scripts/liq_sanity_check.py`

**Interfaces:**
- Consumes: `mexc_client.get_klines`, `mexc_client.get_ticker` (Task 3), `strategy._base_signal`, `strategy._evaluate_liquidity`, `strategy.get_estimator` (Task 4), `config.SCALP_TF`, `config.SCALP_KLINE_COUNT`.

- [ ] **Step 1: Create the script**

```python
"""
Offline diagnostic: pull real MEXC data for a few known-liquid pairs, feed
each symbol's LiqEstimator from a handful of ticker snapshots, and print
the estimated clusters plus whether a signal/veto would fire right now.

Honest limitation: this samples open interest only a few seconds apart
(OI_SAMPLES times), whereas live usage samples every OI_POLL_SEC over
hours. Treat the printed clusters as a plumbing/sanity check, not a
realistic heatmap -- run the bot live for a while before trusting the
cluster shape.

Run: python scripts/liq_sanity_check.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mexc_client import get_klines, get_ticker
from strategy import _base_signal, _evaluate_liquidity, get_estimator
from config import SCALP_TF, SCALP_KLINE_COUNT

SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
OI_SAMPLES = 3
OI_SAMPLE_GAP_SECONDS = 2


def check_symbol(symbol: str) -> None:
    print(f"\n=== {symbol} ===")

    estimator = get_estimator(symbol)
    funding = 0.0
    for i in range(OI_SAMPLES):
        ticker = get_ticker(symbol)
        if ticker is None:
            print("  ticker: fetch failed")
            return
        estimator.on_oi_sample(ticker["hold_vol"] * ticker["fair_price"], ticker["fair_price"])
        estimator.decay_clusters()
        funding = ticker["funding_rate"]
        if i < OI_SAMPLES - 1:
            time.sleep(OI_SAMPLE_GAP_SECONDS)

    df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
    if df is None or df.empty or len(df) < 60:
        print("  klines: insufficient candles")
        return
    window = df.iloc[:-1]

    direction = _base_signal(window)
    price = float(window["close"].iloc[-1])
    print(f"  price={price:.6g} funding={funding * 100:.4f}% base_signal={direction}")

    clusters = estimator.significant_clusters(price)
    print(f"  significant clusters near price: {len(clusters)}")
    for bucket_price, side, magnitude in sorted(clusters, key=lambda c: c[0])[:10]:
        print(f"    {bucket_price:.6g} {side} magnitude={magnitude:,.0f}")

    if direction is None:
        print("  no base signal -- nothing to evaluate")
        return

    ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)
    if ok:
        print(f"  SIGNAL {direction} entry={price:.6g} tp={tp:.6g} sl={sl:.6g} -- {reason}")
    else:
        print(f"  VETO {direction} -- {reason}")


if __name__ == "__main__":
    for sym in SYMBOLS:
        check_symbol(sym)
```

- [ ] **Step 2: Delete the old sanity script (superseded)**

```bash
git rm scripts/vp_ob_sanity_check.py
```

- [ ] **Step 3: Commit**

```bash
git add scripts/liq_sanity_check.py
git commit -m "feat: add liq_sanity_check.py, remove superseded vp_ob_sanity_check.py"
```

---

### Task 6: Delete the old VP-OB modules and tests

**Files:**
- Delete: `order_blocks.py`, `volume_profile.py`, `tests/test_order_blocks.py`, `tests/test_volume_profile.py`

**Interfaces:** none — this is a pure removal now that `strategy.py` (Task 4) no longer imports either module. The prior strategy remains fully recoverable on the `archive/v13-vp-ob-confluence` branch.

- [ ] **Step 1: Confirm nothing still references them**

Run: `grep -rln "order_blocks\|volume_profile" --include="*.py" .`
Expected: no output (Task 4 already removed `strategy.py`'s imports; Task 5 already removed `scripts/vp_ob_sanity_check.py`).

- [ ] **Step 2: Delete the files**

```bash
git rm order_blocks.py volume_profile.py tests/test_order_blocks.py tests/test_volume_profile.py
```

- [ ] **Step 3: Run the full test suite to confirm nothing else depended on them**

Run: `pytest -v`
Expected: all remaining tests pass (Task 1/3/4's new tests), zero collection errors.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: remove VP-OB modules superseded by Liquidation-Aware 1m Scalp (v14)"
```

---

### Task 7: `main.py` — 1-minute scan cadence + open-interest poll loop

**Files:**
- Modify: `main.py`

**Interfaces:**
- Consumes: `strategy.get_estimator`, `strategy.update_ticker_cache` (Task 4), `mexc_client.get_ticker` (Task 3), `config.SCALP_TF`, `config.SCALP_SCAN_INTERVAL_MINUTES`, `config.OI_POLL_SEC` (Task 2).
- `scan_and_fire_signals(app)` itself needs **no changes** — it already calls `strategy.monitor_symbol`/`strategy.arm_symbol` generically and reads only generic `Signal` fields.

- [ ] **Step 1: Fix the config imports**

In `main.py`, find:

```python
from config import (
    LKT,
    LEVERAGE,
    OB_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SETUP_SCAN_CRON_MINUTES,
    SETUP_SCAN_CRON_HOURS,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNALS_PER_SCAN,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    STRATEGY_NAME,
)
```

Replace with:

```python
from config import (
    LKT,
    LEVERAGE,
    SCALP_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCALP_SCAN_INTERVAL_MINUTES,
    OI_POLL_SEC,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNALS_PER_SCAN,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    STRATEGY_NAME,
)
```

- [ ] **Step 2: Swap `OB_TF` for `SCALP_TF` in the outcome checker**

In `check_outcomes`, find:

```python
        try:
            df = get_klines(symbol, OB_TF, count=fetch_count)
```

Replace with:

```python
        try:
            df = get_klines(symbol, SCALP_TF, count=fetch_count)
```

- [ ] **Step 3: Add the open-interest poll loop**

Add this new function above `async def main():`:

```python
# ── Open-interest poll loop ───────────────────────────────────────

async def poll_open_interest_loop() -> None:
    """
    Continuously refreshes each pooled coin's LiqEstimator with fresh
    open-interest/price/funding data, staggering requests across
    OI_POLL_SEC so pooled coins don't all hit the ticker endpoint at once.
    """
    from mexc_client import get_ticker

    while True:
        coins = coin_scanner.get_cached_coins()
        if not coins:
            await asyncio.sleep(OI_POLL_SEC)
            continue

        per_symbol_delay = max(OI_POLL_SEC / len(coins), 0.25)

        for symbol in coins:
            try:
                ticker = get_ticker(symbol)
                if ticker is not None:
                    estimator = strategy.get_estimator(symbol)
                    estimator.on_oi_sample(ticker["hold_vol"] * ticker["fair_price"], ticker["fair_price"])
                    estimator.decay_clusters()
                    strategy.update_ticker_cache(symbol, ticker["fair_price"], ticker["funding_rate"])
            except Exception as e:
                logger.error("[OI-POLL] %s error: %s", symbol, e, exc_info=True)
            await asyncio.sleep(per_symbol_delay)
```

- [ ] **Step 4: Change the scan job cadence and start the OI poll task**

In `main()`, find:

```python
    # Signal scanner (hourly at :01 by default, aligns to 1h candle close)
    # timezone must be explicit here -- a standalone CronTrigger object passed
    # to add_job() does NOT inherit the scheduler's timezone (only the 'cron'
    # string-alias form does), so without this it silently falls back to the
    # server's local system timezone instead of UTC.
    scheduler.add_job(
        scan_and_fire_signals,
        CronTrigger(hour=SETUP_SCAN_CRON_HOURS, minute=SETUP_SCAN_CRON_MINUTES, timezone="UTC"),
        args=[app],
        id="signal_scanner",
    )
```

Replace with:

```python
    # Signal scanner -- every SCALP_SCAN_INTERVAL_MINUTES, aligns to 1m candle close.
    scheduler.add_job(
        scan_and_fire_signals,
        IntervalTrigger(minutes=SCALP_SCAN_INTERVAL_MINUTES),
        args=[app],
        id="signal_scanner",
    )
```

Then find, near the end of `main()`:

```python
    scheduler.start()

    logger.info(
        "Scheduler started — scan=%s/%s outcome=%dm",
        SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS, OUTCOME_CHECK_MINUTES,
    )
```

Replace with:

```python
    scheduler.start()
    oi_poll_task = asyncio.create_task(poll_open_interest_loop(), name="oi_poll")

    logger.info(
        "Scheduler started — scan every %dm, outcome every %dm, OI poll every %ds",
        SCALP_SCAN_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES, OI_POLL_SEC,
    )
```

Then find the shutdown block:

```python
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")
```

Replace with:

```python
        finally:
            scheduler.shutdown(wait=False)
            oi_poll_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")
```

- [ ] **Step 5: Fix the remaining `OB_TF`/`SETUP_SCAN_CRON_*` log lines**

Find:

```python
    logger.info(
        "[CONFIG] OB TF=%s scan=%s/%s daily_cap=%d gap=%dmin cooldown=%dmin slots=%d",
        OB_TF, SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        SIGNAL_COOLDOWN_MINUTES, MAX_CONCURRENT_SIGNALS,
    )
```

Replace with:

```python
    logger.info(
        "[CONFIG] scalp TF=%s scan_interval=%dmin daily_cap=%d gap=%dmin cooldown=%dmin slots=%d",
        SCALP_TF, SCALP_SCAN_INTERVAL_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        SIGNAL_COOLDOWN_MINUTES, MAX_CONCURRENT_SIGNALS,
    )
```

And update the module docstring at the top of `main.py` from:

```python
"""
Main entry point — VP-OB Confluence (4H Volume Profile + 1H Order Block).

Scheduler jobs:
  Hourly at :01   — scanner: arm/monitor Order Block setups, fire signal on retest
  Every 1 min     — outcome checker
  Every 6h        — coin pool refresh
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""
```

to:

```python
"""
Main entry point — Liquidation-Aware 1m Scalp (v14).

Scheduler jobs / background tasks:
  Every SCALP_SCAN_INTERVAL_MINUTES (default 1m) — scanner: arm/monitor scalp
                                                    setups, fire signal when
                                                    the liquidity filter clears
  Every 1 min     — outcome checker
  Every 6h        — coin pool refresh
  Continuous      — open-interest poll loop (staggered across pooled coins)
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""
```

- [ ] **Step 6: Compile-check**

Run: `python -m py_compile main.py`
Expected: no output, exit code 0.

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat: switch scan cadence to 1m and add open-interest poll loop"
```

---

### Task 8: `bot.py` — fix `cmd_status` config references (only necessary edit)

**Files:**
- Modify: `bot.py`

**Interfaces:** none new — `format_signal`, `broadcast_signal`, `notify_outcome`, and all `cmd_*` handlers besides `cmd_status` are untouched.

- [ ] **Step 1: Fix the import inside `cmd_status`**

Find:

```python
    from config import (
        STRATEGY_NAME,
        OB_TF,
        DYN_EMA_MAX_LENGTH, DYN_EMA_ACCEL_MULT,
        ATR_PERIOD, SL_ATR_BUFFER_MULT,
        MIN_STRUCTURE_RR,
        MIN_TP_ROI_PCT, MAX_SL_ROI_PCT,
        SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )
```

Replace with:

```python
    from config import (
        STRATEGY_NAME,
        SCALP_TF,
        EMA_FAST, EMA_MID, EMA_SLOW,
        MAX_SL_PRICE_PCT, TARGET_MARGIN_PROFIT,
        MIN_RR,
        SCALP_SCAN_INTERVAL_MINUTES,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )
```

- [ ] **Step 2: Fix the status message field list**

Find:

```python
        f"OB TF:       {_code(OB_TF.upper())}",
        f"DynEMA len:  {_code(f'max={DYN_EMA_MAX_LENGTH}  accel×{DYN_EMA_ACCEL_MULT}')}",
        f"SL buffer:   {_code(f'ATR({ATR_PERIOD}) × {SL_ATR_BUFFER_MULT}')}",
        f"RR min:      {_code(f'1:{MIN_STRUCTURE_RR:.2g}')}",
        f"TP ROI min:  {_code(f'>= {MIN_TP_ROI_PCT}% at {LEVERAGE}x')}",
        f"SL ROI max:  {_code(f'<= {MAX_SL_ROI_PCT}% at {LEVERAGE}x')}",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan cron:   {_code(f'{SETUP_SCAN_CRON_MINUTES}/{SETUP_SCAN_CRON_HOURS} (min/h)')}",
```

Replace with:

```python
        f"Scalp TF:    {_code(SCALP_TF)}",
        f"EMA stack:   {_code(f'{EMA_FAST}/{EMA_MID}/{EMA_SLOW}')}",
        f"SL cap:      {_code(f'{MAX_SL_PRICE_PCT * 100:.2f}% price')}",
        f"RR min:      {_code(f'1:{MIN_RR:.2g}')}",
        f"TP target:   {_code(f'{TARGET_MARGIN_PROFIT * 100:.0f}% margin @ {LEVERAGE}x')}",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan every:  {_code(f'{SCALP_SCAN_INTERVAL_MINUTES}min')}",
```

- [ ] **Step 3: Compile-check**

Run: `python -m py_compile bot.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "fix: update cmd_status config references for v14 (message layout unchanged)"
```

---

### Task 9: Docs — README.md, CLAUDE.md, skill files

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/mexc-bot/SKILL.md`
- Modify: `.agents/skills/mexc-bot/SKILL.md` (kept identical to the `.claude` copy — verified byte-identical today)

- [ ] **Step 1: Prepend a current-strategy pointer to `README.md`**

`README.md` today is a historical design write-up for an older liquidity-sweep idea (iteration predates v13). Insert at the very top, above line 1:

```markdown
> **Current strategy (v14, `feature/liq-scalp-v14`): Liquidation-Aware 1m Scalp.**
> EMA(9/21/50) + rolling VWAP + RSI + volume base signal on 1m candles,
> gated by a free open-interest-derived liquidation-cluster estimator
> (`liq_estimator.py`). See `CLAUDE.md` for the full architecture and
> `docs/superpowers/plans/2026-07-11-liquidation-aware-scalp-v14.md` for
> the implementation plan. The write-up below is retained for historical
> reference only and does not describe the currently running strategy.

---

```

- [ ] **Step 2: Rewrite the strategy sections of `CLAUDE.md`**

Replace the `## Signal Logic (strategy.py)` section (from `## Signal Logic (strategy.py)` through the end of `### Tuning constants (top of strategy.py)`) with:

```markdown
## Signal Logic (strategy.py) — Liquidation-Aware 1m Scalp (v14)

Two-phase arm/monitor workflow on 1m candles, persisted via the `armed_setups` table:

```
Phase 1 (arm) -- strategy.arm_symbol(symbol):
  1. Base signal on the last CLOSED 1m bar:
     EMA(9) > EMA(21) > EMA(50)  (LONG)  /  reversed (SHORT)
     price on the correct side of rolling VWAP
     RSI(14) in 50-68 (LONG) / 32-50 (SHORT)
     volume > 1.3x the trailing 20-bar average
  2. If a base signal fires, evaluate the liquidity filter immediately.
     If it already clears, arm with real levels; if not, arm anyway with
     provisional levels so Phase 2 keeps re-checking without re-deriving
     the base signal.

Phase 2 (monitor) -- strategy.monitor_symbol(symbol), every cycle,
regardless of the firing-budget throttle:
  1. Re-run the base signal; invalidate the setup if it's no longer active.
  2. Re-run the liquidity filter; fire a Signal the moment it clears.
  3. Expire the setup after SCALP_ARM_MAX_AGE_BARS minutes.
```

### Liquidity filter (`_evaluate_liquidity` in strategy.py, backed by `liq_estimator.py`)

`liq_estimator.py` estimates resting liquidation liquidity for free: when
open interest rises between two polls (`OI_POLL_SEC`, default 60s), new
positions were opened near the current price. Their liquidation prices are
projected across `LEVERAGE_TIERS` into `BUCKET_PCT`-wide price buckets.
Price sweeping a bucket clears it (those positions closed); clusters decay
by `CLUSTER_DECAY` every poll tick.

A signal only fires when, for the base signal's direction:
- a significant opposite-side liquidation cluster sits ahead as a magnet
  (top `CLUSTER_MIN_PERCENTILE` by magnitude, within `CLUSTER_LOOKAROUND`)
- the magnet isn't so close the move looks already exhausted
- no larger same-side cluster sits behind entry (fighting the trade)
- funding isn't extreme against the direction (`FUNDING_EXTREME`)
- the stop can be placed cleanly, capped at `MAX_SL_PRICE_PCT`, never inside a dense cluster
- RR >= `MIN_RR`

TP is placed just before the magnet cluster, capped at
`TARGET_MARGIN_PROFIT / LEVERAGE` price distance from entry.

### Tuning constants (config.py)

```python
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
RSI_PERIOD = 14
RSI_LONG_MIN, RSI_LONG_MAX   = 50, 68
RSI_SHORT_MIN, RSI_SHORT_MAX = 32, 50
SCALP_VOLUME_MIN_MULT = 1.3
TARGET_MARGIN_PROFIT  = 0.12   # 12% margin target -> price move = this / LEVERAGE
MIN_RR                = 1.5
MAX_SL_PRICE_PCT      = 0.0032
LEVERAGE_TIERS = {10: 0.20, 20: 0.25, 25: 0.20, 50: 0.20, 75: 0.10, 100: 0.05}
MMR_BUFFER            = 0.006
BUCKET_PCT            = 0.0005
CLUSTER_DECAY         = 0.97
CLUSTER_LOOKAROUND    = 0.02
CLUSTER_MIN_PERCENTILE = 90
OI_POLL_SEC           = 60
FUNDING_EXTREME       = 0.0004
SCALP_ARM_MAX_AGE_BARS = 10   # minutes
```

Not part of this strategy (removed with VP-OB): ATR-based stop, BTC macro
trend gate, Volume Profile / Order Block detection.
```

Also update the top-level `## Architecture` section's "**1. Signal generation**" paragraph — replace:

```markdown
**1. Signal generation** (`strategy.py`)
Runs on APScheduler at `minute=1` of every hour (aligns to 1h candle close). Calls `get_klines()` for each active pair, computes Supertrend(10, 2.5) + EMA200 + RSI(14), fires a `Signal` dataclass when Supertrend flips direction with EMA200 and RSI confirming. Uses `iloc[-2]` (last *completed* candle), never `iloc[-1]` (in-progress).
```

with:

```markdown
**1. Signal generation** (`strategy.py`)
Runs on APScheduler every `SCALP_SCAN_INTERVAL_MINUTES` (default 1, aligns to 1m candle close). Two-phase arm/monitor over `armed_setups`: arms on an EMA/VWAP/RSI/volume base signal, fires once a liquidation-cluster filter (`liq_estimator.py`) clears. Uses `iloc[:-1]` (last *completed* candle), never the in-progress bar.
```

- [ ] **Step 3: Update `.claude/skills/mexc-bot/SKILL.md`**

Replace the `## Current Strategy Architecture` section through `## Preferred Risk Model`'s config block with the v14 description (mirroring the CLAUDE.md wording from Step 2), and replace the "Preferred config values" block's VP-OB names (`MIN_STRUCTURE_RR`, `MIN_TP_ROI_PCT`, etc.) with the v14 names (`MIN_RR`, `MAX_SL_PRICE_PCT`, `TARGET_MARGIN_PROFIT`, etc.) at the same defaults as `config.py` Task 2. Keep the "Critical Rules" (trade geometry), "Required Helper Functions" (`_valid_trade_geometry`), and "Response Style"/"Acceptance Criteria" sections as-is — those are strategy-agnostic and still apply verbatim to `strategy.py`'s `_valid_trade_geometry`.

- [ ] **Step 4: Sync `.agents/skills/mexc-bot/SKILL.md`**

```bash
cp "D:/Test/personal/mexc/.claude/skills/mexc-bot/SKILL.md" "D:/Test/personal/mexc/.agents/skills/mexc-bot/SKILL.md"
```

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md .claude/skills/mexc-bot/SKILL.md .agents/skills/mexc-bot/SKILL.md
git commit -m "docs: describe Liquidation-Aware 1m Scalp (v14) strategy"
```

---

### Task 10: Full verification + final reminders

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: every test in `tests/` passes (Tasks 1, 3, 4's new tests; no leftover VP-OB tests since Task 6 deleted them).

- [ ] **Step 2: Compile-check every module**

Run: `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py liq_estimator.py mexc_client.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Run the sanity script against real MEXC data**

Run: `python scripts/liq_sanity_check.py`
Expected: for each of `BTC_USDT`/`ETH_USDT`/`SOL_USDT`, prints price/funding/base_signal, a cluster count, and either a `SIGNAL`/`VETO` line (or "no base signal" if none fired in that moment — expected most of the time given how narrow the RSI/volume gates are).

- [ ] **Step 4: Confirm the branch is clean and un-pushed**

Run: `git status && git log --oneline main..feature/liq-scalp-v14`
Expected: working tree clean; shows the commits from Tasks 1-9 ahead of `main`. Do **not** push — the user will decide when.

- [ ] **Step 5: Final reminders to surface to the user**

Report these explicitly when the plan is done:
1. Update the GitHub Actions `APP_ENV` secret with the new `.env` variables added in Task 2 (`TARGET_MARGIN_PROFIT`, `MIN_RR`, `MAX_SL_PRICE_PCT`, `LEVERAGE_TIERS`, `MMR_BUFFER`, `BUCKET_PCT`, `CLUSTER_DECAY`, `CLUSTER_LOOKAROUND`, `CLUSTER_MIN_PERCENTILE`, `OI_POLL_SEC`, `FUNDING_EXTREME`) before merging — the deploy workflow overwrites `.env` from this secret.
2. Run `python clear_db.py --yes` on the server after deploying — old `armed_setups`/`signals` rows belong to the VP-OB generation and shouldn't be scored/monitored under v14's logic.
3. Merging `feature/liq-scalp-v14` to `main` triggers auto-deploy (`.github/workflows/deploy.yml`) and restarts both `mexc-bot` and `mexc-dashboard` immediately — merge only when ready to go live.
4. `MAX_DAILY_SIGNALS`/`MIN_DAILY_SIGNAL_GAP_MINUTES`/`SIGNAL_EXPIRE_HOURS` were deliberately left at their VP-OB-era defaults (3/day, 180min gap, 48h expiry) — watch live signal frequency for a day or two, then retune towards the suggested 5-8/day, 30min gap, 2-4h expiry.
