# VP-OB Confluence Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current Trend Speed Analyzer (DynEMA crossover) strategy with a 4H Volume Profile bias + 1H Order Block entry strategy, per `docs/superpowers/specs/2026-07-02-volume-profile-orderblock-strategy-design.md`.

**Architecture:** Two new pure-function modules (`volume_profile.py`, `order_blocks.py`) provide testable VP/OB math. `strategy.py` is rewritten to orchestrate a two-phase arm/monitor workflow against the already-existing (currently unused) `armed_setups` DB table. `config.py` gets new `VP_*`/`OB_*` knobs and retuned risk gates. `main.py` gains armed-setup lifecycle wiring; its scheduler cadence changes purely via config defaults (no code change needed there).

**Tech Stack:** Python 3, pandas, numpy, pytest (new dev dependency), SQLite (existing `database.py`, no schema changes).

## Global Constraints

- Leverage: 20x. Target RR: >=1:2 (`MIN_STRUCTURE_RR=2.00`). Target TP ROI: ~50% (`MIN_TP_ROI_PCT=45.0`, `TARGET_TP_ROI_PCT=50.0`), SL ROI cap 28% (`MAX_SL_ROI_PCT=28.0`).
- Timeframes: Volume Profile on 4H (`VP_TF=4h`), Order Blocks on 1H (`OB_TF=1h`).
- BTC 1H macro gate stays active (`BTC_GATE_ENABLED=true`), using the existing DynEMA-based `_get_btc_dema()`.
- Signal frequency target: 1-3/day — enforced primarily by confluence strictness (`MIN_SETUP_SCORE=90.0`), backstopped by `MAX_DAILY_SIGNALS=3` / `MIN_DAILY_SIGNAL_GAP_MINUTES=180`.
- No new DB tables/columns — reuse `armed_setups` exactly as it exists in `database.py` today.
- Only evaluate closed/completed candles, never the in-progress last candle (existing project convention, e.g. `strategy.py:259` `df.iloc[:-1]`).
- Every fired `Signal` must pass `_valid_trade_geometry()` before being returned from `strategy.py`.
- `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py volume_profile.py order_blocks.py` must pass after every task that touches one of these files.

---

### Task 1: Volume Profile module

**Files:**
- Create: `volume_profile.py`
- Create: `tests/__init__.py`
- Create: `tests/test_volume_profile.py`
- Modify: `requirements.txt` (add `pytest`)

**Interfaces:**
- Produces: `VolumeProfile` dataclass (`poc: float`, `vah: float`, `val: float`, `hvns: list[float]`, `lvns: list[float]`); `compute_volume_profile(df: pd.DataFrame, bins: int = 40, value_area_pct: float = 0.70, hvn_mult: float = 1.5, lvn_mult: float = 0.4) -> VolumeProfile | None` (df needs `high`, `low`, `volume` columns); `vp_bias(close: float, vp: VolumeProfile) -> str | None` (returns `"LONG"`, `"SHORT"`, or `None`); `next_target(direction: str, entry_price: float, vp: VolumeProfile) -> float`.

- [ ] **Step 1: Add pytest to requirements.txt**

Add this line to `requirements.txt` (append at the end of the file):

```
pytest==8.3.4
```

Run: `pip install pytest==8.3.4`
Expected: installs successfully.

- [ ] **Step 2: Create tests/__init__.py (empty, makes tests a package)**

Create `tests/__init__.py` with empty content (0 bytes).

- [ ] **Step 3: Write the failing tests**

Create `tests/test_volume_profile.py`:

```python
import pandas as pd
import pytest

from volume_profile import VolumeProfile, compute_volume_profile, vp_bias, next_target


def _make_df(volumes: list[float]) -> pd.DataFrame:
    """10 candles, each a distinct 1-unit price bin from 100 to 110."""
    rows = []
    for i, vol in enumerate(volumes):
        rows.append({"low": 100.0 + i, "high": 101.0 + i, "volume": vol})
    return pd.DataFrame(rows)


def test_poc_is_the_highest_volume_bin():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10)
    assert vp is not None
    assert abs(vp.poc - 105.5) < 1e-9


def test_value_area_covers_at_least_target_pct():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10, value_area_pct=0.70)
    assert vp.val <= vp.poc <= vp.vah

    total = sum([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    # bin i occupies price [100+i, 101+i); check volume of bins whose
    # midpoint falls inside [val, vah]
    covered = 0.0
    volumes = [10, 10, 10, 10, 10, 100, 10, 10, 10, 10]
    for i, vol in enumerate(volumes):
        mid = 100.0 + i + 0.5
        if vp.val <= mid <= vp.vah:
            covered += vol
    assert covered >= 0.70 * total


def test_hvn_detected_near_poc():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10, hvn_mult=1.5)
    assert any(abs(h - 105.5) < 1.5 for h in vp.hvns)


def test_degenerate_flat_range_returns_none():
    df = pd.DataFrame([{"low": 100.0, "high": 100.0, "volume": 10.0}] * 5)
    assert compute_volume_profile(df, bins=10) is None


def test_vp_bias():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert vp_bias(111.0, vp) == "LONG"
    assert vp_bias(95.0, vp) == "SHORT"
    assert vp_bias(105.0, vp) is None


def test_next_target_long():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert next_target("LONG", 102.0, vp) == 105.0   # entry below POC -> POC
    assert next_target("LONG", 107.0, vp) == 110.0   # entry above POC -> VAH


def test_next_target_short():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert next_target("SHORT", 108.0, vp) == 105.0  # entry above POC -> POC
    assert next_target("SHORT", 103.0, vp) == 100.0  # entry below POC -> VAL
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_volume_profile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'volume_profile'`

- [ ] **Step 5: Implement volume_profile.py**

Create `volume_profile.py`:

```python
"""
Volume Profile computation from OHLCV candles.

Approximates POC/VAH/VAL/HVN/LVN by binning the window's price range and
distributing each candle's volume across the bins its [low, high] overlaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class VolumeProfile:
    poc: float
    vah: float
    val: float
    hvns: list[float] = field(default_factory=list)
    lvns: list[float] = field(default_factory=list)


def _smooth3(values: list[float]) -> list[float]:
    n = len(values)
    out = [0.0] * n
    for i in range(n):
        window = values[max(0, i - 1): min(n, i + 2)]
        out[i] = sum(window) / len(window)
    return out


def compute_volume_profile(
    df: pd.DataFrame,
    bins: int = 40,
    value_area_pct: float = 0.70,
    hvn_mult: float = 1.5,
    lvn_mult: float = 0.4,
) -> VolumeProfile | None:
    """
    df must have 'high', 'low', 'volume' columns, one row per candle,
    already restricted to the desired lookback window.
    Returns None if the window is degenerate (flat price range or no volume).
    """
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if hi <= lo:
        return None

    bin_size = (hi - lo) / bins
    bin_volume = [0.0] * bins

    for _, row in df.iterrows():
        row_low = float(row["low"])
        row_high = float(row["high"])
        row_vol = float(row["volume"])
        first_bin = max(0, min(bins - 1, int((row_low - lo) / bin_size)))
        last_bin = max(0, min(bins - 1, int((row_high - lo) / bin_size)))
        if last_bin < first_bin:
            first_bin, last_bin = last_bin, first_bin
        span = last_bin - first_bin + 1
        per_bin = row_vol / span
        for b in range(first_bin, last_bin + 1):
            bin_volume[b] += per_bin

    total = sum(bin_volume)
    if total <= 0:
        return None

    poc_bin = max(range(bins), key=lambda b: bin_volume[b])

    lo_b = hi_b = poc_bin
    covered = bin_volume[poc_bin]
    while covered < value_area_pct * total and (lo_b > 0 or hi_b < bins - 1):
        next_lo = bin_volume[lo_b - 1] if lo_b > 0 else -1.0
        next_hi = bin_volume[hi_b + 1] if hi_b < bins - 1 else -1.0
        if next_hi >= next_lo:
            hi_b += 1
            covered += bin_volume[hi_b]
        else:
            lo_b -= 1
            covered += bin_volume[lo_b]

    poc = lo + (poc_bin + 0.5) * bin_size
    vah = lo + (hi_b + 1) * bin_size
    val = lo + lo_b * bin_size

    smoothed = _smooth3(bin_volume)
    mean_vol = total / bins
    hvns = [lo + (b + 0.5) * bin_size for b in range(bins) if smoothed[b] > hvn_mult * mean_vol]
    lvns = [lo + (b + 0.5) * bin_size for b in range(bins) if smoothed[b] < lvn_mult * mean_vol]

    return VolumeProfile(poc=poc, vah=vah, val=val, hvns=hvns, lvns=lvns)


def vp_bias(close: float, vp: VolumeProfile) -> str | None:
    """Returns 'LONG', 'SHORT', or None (inside the value area, no bias)."""
    if close > vp.vah:
        return "LONG"
    if close < vp.val:
        return "SHORT"
    return None


def next_target(direction: str, entry_price: float, vp: VolumeProfile) -> float:
    """TP target: POC if not yet crossed, else the opposite Value Area edge."""
    if direction == "LONG":
        return vp.poc if entry_price < vp.poc else vp.vah
    return vp.poc if entry_price > vp.poc else vp.val
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_volume_profile.py -v`
Expected: all 7 tests PASS

- [ ] **Step 7: Commit**

```bash
git add volume_profile.py tests/__init__.py tests/test_volume_profile.py requirements.txt
git commit -m "feat: add Volume Profile computation module"
```

---

### Task 2: Order Block detection module

**Files:**
- Create: `order_blocks.py`
- Create: `tests/test_order_blocks.py`

**Interfaces:**
- Consumes: nothing from Task 1 (independent module).
- Produces: `Swing` dataclass (`bar_index: int`, `price: float`, `kind: str`); `StructureEvent` dataclass (`bar_index: int`, `direction: str`, `kind: str`); `OrderBlock` dataclass (`direction: str`, `low: float`, `high: float`, `formed_at_bar: int`, `event_bar_index: int`, `structure_event: str`); `find_swings(df: pd.DataFrame, length: int = 6) -> list[Swing]`; `detect_bos_choch(df: pd.DataFrame, swings: list[Swing]) -> list[StructureEvent]`; `find_order_blocks(df: pd.DataFrame, structure_events: list[StructureEvent], atr: pd.Series, displacement_atr_mult: float = 1.5) -> list[OrderBlock]` (df needs `open`, `high`, `low`, `close` columns, all functions index `df` positionally by row position, so `df` must have a default RangeIndex or be reset first).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_order_blocks.py`:

```python
import pandas as pd
import pytest

from order_blocks import find_swings, detect_bos_choch, find_order_blocks


def _make_df() -> pd.DataFrame:
    rows = [
        # open,  high,  low,  close, volume
        (100,   101,   99,   100,   100),
        (100,   102,   99,   101,   100),
        (101,   103,   100,  102,   100),
        (102,   104,   101,  103,   100),   # swing high (104)
        (103,   103.5, 101,  102,   100),
        (102,   102.5, 98,   99,    100),
        (99,    100.5, 97,   98,    100),   # OB candle (bearish), also swing low (97)
        (98,    110,   97.5, 109,   500),   # displacement candle, breaks swing high
        (109,   111,   108,  110,   200),
        (110,   112,   109,  111,   200),
    ]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_find_swings_identifies_high_and_low():
    df = _make_df()
    swings = find_swings(df, length=2)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    assert any(s.bar_index == 3 and abs(s.price - 104.0) < 1e-9 for s in highs)
    assert any(s.bar_index == 6 and abs(s.price - 97.0) < 1e-9 for s in lows)


def test_detect_bos_choch_fires_on_structure_break():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    assert len(events) == 1
    assert events[0].bar_index == 7
    assert events[0].direction == "LONG"
    assert events[0].kind == "CHoCH"


def test_find_order_blocks_detects_displaced_ob():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    atr = pd.Series([1.0] * len(df))  # small ATR -> displacement gate passes easily

    obs = find_order_blocks(df, events, atr, displacement_atr_mult=1.5)

    assert len(obs) == 1
    ob = obs[0]
    assert ob.direction == "LONG"
    assert ob.formed_at_bar == 6
    assert ob.event_bar_index == 7
    assert abs(ob.low - 97.0) < 1e-9
    assert abs(ob.high - 100.5) < 1e-9
    assert ob.structure_event == "CHoCH"


def test_find_order_blocks_skips_weak_displacement_without_fvg():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    atr = pd.Series([20.0] * len(df))  # large ATR -> displacement gate fails, no FVG present

    obs = find_order_blocks(df, events, atr, displacement_atr_mult=1.5)

    assert obs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_order_blocks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'order_blocks'`

- [ ] **Step 3: Implement order_blocks.py**

Create `order_blocks.py`:

```python
"""
Order Block detection (Smart Money Concepts) from OHLCV candles.

An Order Block is the last opposite-color candle before an impulsive move
that breaks market structure (BOS/CHoCH) with displacement (a large ATR-
relative move or a Fair Value Gap).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Swing:
    bar_index: int
    price: float
    kind: str          # "high" or "low"


@dataclass
class StructureEvent:
    bar_index: int
    direction: str      # "LONG" or "SHORT" -- direction of the break
    kind: str           # "BOS" or "CHoCH"


@dataclass
class OrderBlock:
    direction: str
    low: float
    high: float
    formed_at_bar: int
    event_bar_index: int
    structure_event: str  # "BOS" or "CHoCH"


def find_swings(df: pd.DataFrame, length: int = 6) -> list[Swing]:
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    n = len(df)
    swings: list[Swing] = []
    for i in range(length, n - length):
        window_high = highs[i - length: i + length + 1]
        if highs[i] == window_high.max():
            swings.append(Swing(bar_index=i, price=float(highs[i]), kind="high"))
        window_low = lows[i - length: i + length + 1]
        if lows[i] == window_low.min():
            swings.append(Swing(bar_index=i, price=float(lows[i]), kind="low"))
    return swings


def detect_bos_choch(df: pd.DataFrame, swings: list[Swing]) -> list[StructureEvent]:
    closes = df["close"].astype(float).to_numpy()
    n = len(df)

    last_swing_high: Swing | None = None
    last_swing_low: Swing | None = None
    trend: str | None = None
    events: list[StructureEvent] = []

    swings_by_bar: dict[int, list[Swing]] = {}
    for s in swings:
        swings_by_bar.setdefault(s.bar_index, []).append(s)

    for i in range(n):
        for s in swings_by_bar.get(i, []):
            if s.kind == "high":
                last_swing_high = s
            else:
                last_swing_low = s

        if (last_swing_high is not None
                and i > last_swing_high.bar_index
                and closes[i] > last_swing_high.price):
            kind = "BOS" if trend == "LONG" else "CHoCH"
            events.append(StructureEvent(bar_index=i, direction="LONG", kind=kind))
            trend = "LONG"
            last_swing_high = None
        elif (last_swing_low is not None
                and i > last_swing_low.bar_index
                and closes[i] < last_swing_low.price):
            kind = "BOS" if trend == "SHORT" else "CHoCH"
            events.append(StructureEvent(bar_index=i, direction="SHORT", kind=kind))
            trend = "SHORT"
            last_swing_low = None

    return events


def _has_fair_value_gap(df: pd.DataFrame, start: int, end: int, direction: str) -> bool:
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    for k in range(start + 1, end):
        if direction == "LONG" and lows[k + 1] > highs[k - 1]:
            return True
        if direction == "SHORT" and highs[k + 1] < lows[k - 1]:
            return True
    return False


def find_order_blocks(
    df: pd.DataFrame,
    structure_events: list[StructureEvent],
    atr: pd.Series,
    displacement_atr_mult: float = 1.5,
) -> list[OrderBlock]:
    opens = df["open"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    highs = df["high"].astype(float).to_numpy()
    atr_arr = atr.to_numpy(dtype=float)

    obs: list[OrderBlock] = []
    for event in structure_events:
        i = event.bar_index
        j = i
        if event.direction == "LONG":
            while j > 0 and closes[j] >= opens[j]:
                j -= 1
            if j == 0 and closes[j] >= opens[j]:
                continue
        else:
            while j > 0 and closes[j] <= opens[j]:
                j -= 1
            if j == 0 and closes[j] <= opens[j]:
                continue

        atr_at_break = atr_arr[i] if not pd.isna(atr_arr[i]) else 0.0
        if atr_at_break <= 0:
            continue

        move = abs(closes[i] - closes[j])
        has_fvg = _has_fair_value_gap(df, j, i, event.direction)

        if move < displacement_atr_mult * atr_at_break and not has_fvg:
            continue

        obs.append(OrderBlock(
            direction=event.direction,
            low=float(lows[j]),
            high=float(highs[j]),
            formed_at_bar=j,
            event_bar_index=i,
            structure_event=event.kind,
        ))

    return obs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_order_blocks.py -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add order_blocks.py tests/test_order_blocks.py
git commit -m "feat: add Order Block (SMC) detection module"
```

---

### Task 3: Config changes

**Files:**
- Modify: `config.py:57-113`

**Interfaces:**
- Produces (new/changed constants consumed by Task 4): `VP_TF`, `VP_KLINE_COUNT`, `VP_LOOKBACK_BARS`, `VP_BINS`, `VP_VALUE_AREA_PCT`, `VP_HVN_MULT`, `VP_LVN_MULT`, `OB_TF`, `OB_KLINE_COUNT`, `OB_SWING_LENGTH`, `OB_DISPLACEMENT_ATR_MULT`, `OB_MAX_AGE_BARS`, `OB_CONFLUENCE_ATR_MULT`, `OB_BODY_RATIO_MIN`, `OB_VOLUME_MIN_MULT`, `OB_VOLUME_MA_BARS`, `SL_ATR_BUFFER_MULT`, `MIN_SETUP_SCORE`, `TARGET_TP_ROI_PCT`. Retained: `DYN_EMA_MAX_LENGTH`, `DYN_EMA_ACCEL_MULT`, `ATR_PERIOD`, `MIN_STRUCTURE_RR`, `MIN_TP_ROI_PCT`, `MAX_SL_ROI_PCT`, `BTC_*`, `LEVERAGE`. Removed: `SIGNAL_TF`, `SIGNAL_KLINE_COUNT`, `SL_ATR_MULT`, `REWARD_RATIO`, `MIN_SL_ROI_PCT`, `SPEED_REL_THRESHOLD`, `SPEED_ACCEL_ENABLED`.

- [ ] **Step 1: Replace the strategy config block**

In `config.py`, replace lines 57 through 113 (from `# ── Strategy: Trend Speed Analyzer (Zeiierman) ─────────────────────` through `MIN_DAILY_SIGNAL_GAP_MINUTES: int   = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES",   "6"))`) with:

```python
# ── Strategy: VP-OB Confluence (4H Volume Profile + 1H Order Block) ─
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "VP-OB Confluence (4H VP + 1H Order Block)",
)

# ── Volume Profile (bias + TP targets) ──────────────────────────────
VP_TF: str               = os.getenv("VP_TF", "4h")
VP_KLINE_COUNT: int      = int(os.getenv("VP_KLINE_COUNT", "40"))
VP_LOOKBACK_BARS: int    = int(os.getenv("VP_LOOKBACK_BARS", "30"))
VP_BINS: int             = int(os.getenv("VP_BINS", "40"))
VP_VALUE_AREA_PCT: float = float(os.getenv("VP_VALUE_AREA_PCT", "0.70"))
VP_HVN_MULT: float       = float(os.getenv("VP_HVN_MULT", "1.5"))
VP_LVN_MULT: float       = float(os.getenv("VP_LVN_MULT", "0.4"))

# ── Order Blocks (entry zone) ────────────────────────────────────────
OB_TF: str                      = os.getenv("OB_TF", "1h")
OB_KLINE_COUNT: int              = int(os.getenv("OB_KLINE_COUNT", "200"))
OB_SWING_LENGTH: int             = int(os.getenv("OB_SWING_LENGTH", "6"))
OB_DISPLACEMENT_ATR_MULT: float  = float(os.getenv("OB_DISPLACEMENT_ATR_MULT", "1.5"))
OB_MAX_AGE_BARS: int              = int(os.getenv("OB_MAX_AGE_BARS", "40"))
OB_CONFLUENCE_ATR_MULT: float    = float(os.getenv("OB_CONFLUENCE_ATR_MULT", "0.5"))
OB_BODY_RATIO_MIN: float         = float(os.getenv("OB_BODY_RATIO_MIN", "0.50"))
OB_VOLUME_MIN_MULT: float        = float(os.getenv("OB_VOLUME_MIN_MULT", "1.5"))
OB_VOLUME_MA_BARS: int           = int(os.getenv("OB_VOLUME_MA_BARS", "20"))

# Dynamic EMA parameters (used only by the BTC 1h macro gate below)
DYN_EMA_MAX_LENGTH: int   = int(os.getenv("DYN_EMA_MAX_LENGTH", "50"))
DYN_EMA_ACCEL_MULT: float = float(os.getenv("DYN_EMA_ACCEL_MULT", "5.0"))

# ATR period (shared: OB displacement, SL buffer, BTC gate) + SL buffer
ATR_PERIOD: int           = int(os.getenv("ATR_PERIOD", "14"))
SL_ATR_BUFFER_MULT: float = float(os.getenv("SL_ATR_BUFFER_MULT", "0.35"))

# Risk / reward / quality gates (applied at 20x leverage)
MIN_STRUCTURE_RR: float  = float(os.getenv("MIN_STRUCTURE_RR", "2.00"))
MIN_TP_ROI_PCT: float    = float(os.getenv("MIN_TP_ROI_PCT",  "45.0"))
TARGET_TP_ROI_PCT: float = float(os.getenv("TARGET_TP_ROI_PCT", "50.0"))
MAX_SL_ROI_PCT: float    = float(os.getenv("MAX_SL_ROI_PCT",  "28.0"))
MIN_SETUP_SCORE: float   = float(os.getenv("MIN_SETUP_SCORE", "90.0"))

# ── BTC macro gate ─────────────────────────────────────────────────
BTC_SYMBOL: str        = os.getenv("BTC_SYMBOL", "BTC_USDT")
BTC_TF: str            = os.getenv("BTC_TF", "1h")
BTC_KLINE_COUNT: int   = int(os.getenv("BTC_KLINE_COUNT", "300"))
BTC_GATE_ENABLED: bool = os.getenv("BTC_GATE_ENABLED", "true").lower() == "true"
BTC_RANGING_PCT: float = float(os.getenv("BTC_RANGING_PCT", "0.10"))

# ── Trade params ───────────────────────────────────────────────────
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))

# ── Scheduler ──────────────────────────────────────────────────────
# Default: scan hourly at :01 (aligns to 1H candle close)
SETUP_SCAN_CRON_MINUTES: str = os.getenv("SETUP_SCAN_CRON_MINUTES", "1")
SETUP_SCAN_CRON_HOURS: str   = os.getenv("SETUP_SCAN_CRON_HOURS",   "*")
OUTCOME_CHECK_MINUTES: int   = int(os.getenv("OUTCOME_CHECK_MINUTES", "1"))
COIN_REFRESH_CRON_HOURS: str = os.getenv("COIN_REFRESH_CRON_HOURS", f"*/{COIN_REFRESH_HOURS}")

SIGNALS_PER_SCAN: int        = int(os.getenv("SIGNALS_PER_SCAN",        "1"))
MAX_CONCURRENT_SIGNALS: int  = int(os.getenv("MAX_CONCURRENT_SIGNALS",  "5"))
SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "240"))
SIGNAL_EXPIRE_HOURS: int     = int(os.getenv("SIGNAL_EXPIRE_HOURS",     "48"))
SCAN_WORKERS: int            = int(os.getenv("SCAN_WORKERS",            "4"))

# Daily signal cap
MAX_DAILY_SIGNALS: int              = int(os.getenv("MAX_DAILY_SIGNALS",              "3"))
MIN_DAILY_SIGNAL_GAP_MINUTES: int   = int(os.getenv("MIN_DAILY_SIGNAL_GAP_MINUTES",   "180"))
```

- [ ] **Step 2: Update the derived candle-minutes block to key off OB_TF**

In `config.py`, find (near the bottom of the file):

```python
# ── Candle minutes (derived from SIGNAL_TF) ────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(SIGNAL_TF, 60))))
```

Replace with:

```python
# ── Candle minutes (derived from OB_TF) ────────────────────────────
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}
CANDLE_MINUTES: int = int(os.getenv("CANDLE_MINUTES", str(_TF_MINUTES.get(OB_TF, 60))))
```

- [ ] **Step 3: Verify config.py compiles**

Run: `python -m py_compile config.py`
Expected: no output, exit code 0

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "config: switch to VP-OB Confluence knobs (4H VP + 1H OB, 20x/1:2RR/50%ROI)"
```

---

### Task 4: Strategy rewrite

**Files:**
- Modify: `strategy.py` (full rewrite of the body; module docstring, imports, `Signal` dataclass, and everything below `_get_btc_dema` change — `_rma`, `_wma`, `_hma`, `_compute_trend_speed` are deleted; `_atr_series`, `_compute_dyn_ema`, `_valid_trade_geometry`, `_get_btc_dema` are kept verbatim)

**Interfaces:**
- Consumes: `volume_profile.compute_volume_profile`, `volume_profile.vp_bias`, `volume_profile.next_target`, `volume_profile.VolumeProfile` (Task 1); `order_blocks.find_swings`, `order_blocks.detect_bos_choch`, `order_blocks.find_order_blocks`, `order_blocks.OrderBlock` (Task 2); all `VP_*`/`OB_*`/gate config constants (Task 3); `database.armed_setup_exists`, `database.get_armed_setup_by_symbol`, `database.save_armed_setup`, `database.mark_armed_setup_invalidated`, `database.mark_armed_setup_expired`, `database.mark_armed_setup_missed` (existing, unchanged).
- Produces: `Signal` dataclass with new field `armed_setup_id: int | None = None` (all prior fields unchanged); `scan_symbol(symbol: str) -> Signal | None` (same signature `main.py` already calls).

- [ ] **Step 1: Replace strategy.py entirely**

Replace the full contents of `strategy.py` with:

```python
"""
VP-OB Confluence strategy.

Two-phase workflow, persisted via the `armed_setups` DB table:

Phase 1 (arm): compute a 4H Volume Profile bias (close above VAH -> LONG,
below VAL -> SHORT), detect 1H Order Blocks (BOS/CHoCH + displacement) in
that direction, keep only ones near a VP level (POC/VAH/VAL/HVN), and arm
the best one if it clears a minimum confluence score.

Phase 2 (monitor): each cycle, check any already-armed setup for structural
invalidation, expiry, or a valid retest (wick into the zone, close back out
in the trade direction, body ratio + volume confirmed). A valid retest
recomputes SL/TP against fresh VP levels and applies the RR/ROI gates
before firing a Signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import database as db
from mexc_client import get_klines
from order_blocks import OrderBlock, detect_bos_choch, find_order_blocks, find_swings
from volume_profile import VolumeProfile, compute_volume_profile, next_target, vp_bias
from config import (
    VP_TF,
    VP_KLINE_COUNT,
    VP_LOOKBACK_BARS,
    VP_BINS,
    VP_VALUE_AREA_PCT,
    VP_HVN_MULT,
    VP_LVN_MULT,
    OB_TF,
    OB_KLINE_COUNT,
    OB_SWING_LENGTH,
    OB_DISPLACEMENT_ATR_MULT,
    OB_MAX_AGE_BARS,
    OB_CONFLUENCE_ATR_MULT,
    OB_BODY_RATIO_MIN,
    OB_VOLUME_MIN_MULT,
    OB_VOLUME_MA_BARS,
    ATR_PERIOD,
    SL_ATR_BUFFER_MULT,
    DYN_EMA_MAX_LENGTH,
    DYN_EMA_ACCEL_MULT,
    LEVERAGE,
    MIN_STRUCTURE_RR,
    MIN_TP_ROI_PCT,
    MAX_SL_ROI_PCT,
    MIN_SETUP_SCORE,
    BTC_SYMBOL,
    BTC_TF,
    BTC_KLINE_COUNT,
    BTC_GATE_ENABLED,
    BTC_RANGING_PCT,
)

logger = logging.getLogger(__name__)

# BTC DynEMA cache — refreshed once per scan cycle (14-min TTL)
_btc_cache: dict = {"dema": None, "close": None, "ts": 0.0}
_BTC_CACHE_TTL = 14 * 60


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


# ── Low-level indicator helpers (kept from the previous strategy) ──

def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    high       = df["high"].astype(float)
    low        = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _compute_dyn_ema(
    close: pd.Series,
    max_length: int = 50,
    accel_mult: float = 5.0,
) -> pd.Series:
    """Dynamic EMA with accelerating alpha — used only by the BTC macro gate."""
    c = close.to_numpy(dtype=float)
    n = len(c)

    abs_c = np.abs(c)
    max_abs = np.array([
        np.nanmax(abs_c[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_abs[max_abs == 0] = 1e-10

    counts_diff_norm = (c + max_abs) / (2.0 * max_abs)
    dyn_length = 5.0 + counts_diff_norm * (max_length - 5)

    delta = np.abs(np.diff(c, prepend=c[0]))
    max_delta = np.array([
        np.nanmax(delta[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_delta[max_delta == 0] = 1.0
    accel_factor = delta / max_delta

    alpha_base = 2.0 / (dyn_length + 1.0)
    alpha = np.minimum(1.0, alpha_base * (1.0 + accel_factor * accel_mult))

    dyn_ema = np.empty(n, dtype=float)
    dyn_ema[0] = c[0]
    for i in range(1, n):
        dyn_ema[i] = alpha[i] * c[i] + (1.0 - alpha[i]) * dyn_ema[i - 1]

    return pd.Series(dyn_ema, index=close.index)


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


def _get_btc_dema() -> tuple[float | None, float | None]:
    """
    Return (btc_close, btc_dema) using a 14-min module-level cache.
    Returns (None, None) on any failure — gate is fail-open.
    """
    global _btc_cache
    now_ts = datetime.now(timezone.utc).timestamp()
    if _btc_cache["dema"] is not None and now_ts - _btc_cache["ts"] < _BTC_CACHE_TTL:
        return _btc_cache["close"], _btc_cache["dema"]
    try:
        df = get_klines(BTC_SYMBOL, BTC_TF, count=BTC_KLINE_COUNT)
        if df is None or df.empty or len(df) < 210:
            logger.warning("[BTC-GATE] Insufficient BTC klines (%d) — gate open", 0 if df is None else len(df))
            return None, None
        close    = df["close"].astype(float)
        dema     = _compute_dyn_ema(close, DYN_EMA_MAX_LENGTH, DYN_EMA_ACCEL_MULT)
        btc_close = float(close.iloc[-1])
        btc_dema  = float(dema.iloc[-1])
        _btc_cache = {"dema": btc_dema, "close": btc_close, "ts": now_ts}
        logger.debug("[BTC-GATE] Cache refreshed close=%.6g dema=%.6g", btc_close, btc_dema)
        return btc_close, btc_dema
    except Exception as e:
        logger.warning("[BTC-GATE] Fetch failed: %s — gate open", e)
        return None, None


# ── Scoring ──────────────────────────────────────────────────────────

def _compute_setup_score(displacement_atr_ratio: float, confluence_distance_atr: float) -> float:
    """
    0-100 composite score. Displacement component rewards moves well beyond
    the minimum threshold (maxes out at 2x OB_DISPLACEMENT_ATR_MULT);
    confluence component rewards proximity to a VP level (maxes out at zero
    distance, zero at the max allowed confluence distance).
    """
    disp_component = min(50.0, 50.0 * displacement_atr_ratio / (2.0 * OB_DISPLACEMENT_ATR_MULT))
    conf_component = 50.0 * max(0.0, 1.0 - confluence_distance_atr / OB_CONFLUENCE_ATR_MULT)
    return round(disp_component + conf_component, 1)


# ── Phase 1: arm ──────────────────────────────────────────────────────

def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        return
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)

    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        return

    bias = vp_bias(float(vp_window["close"].iloc[-1]), vp)
    if bias is None:
        return

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    min_ob_bars = ATR_PERIOD + OB_SWING_LENGTH * 2 + 10
    if ob_df is None or ob_df.empty or len(ob_df) < min_ob_bars:
        return
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)

    atr = _atr_series(ob_window, ATR_PERIOD)
    swings = find_swings(ob_window, length=OB_SWING_LENGTH)
    events = detect_bos_choch(ob_window, swings)
    obs = find_order_blocks(ob_window, events, atr, displacement_atr_mult=OB_DISPLACEMENT_ATR_MULT)

    matching = [ob for ob in obs if ob.direction == bias]
    if not matching:
        return

    latest_atr = float(atr.iloc[-1])
    if latest_atr <= 0:
        return

    vp_levels = [vp.poc, vp.vah, vp.val] + vp.hvns
    best_ob: OrderBlock | None = None
    best_distance_atr: float | None = None
    for ob in matching:
        mid = (ob.low + ob.high) / 2.0
        distance_atr = min(abs(mid - level) for level in vp_levels) / latest_atr
        if distance_atr > OB_CONFLUENCE_ATR_MULT:
            continue
        if best_distance_atr is None or distance_atr < best_distance_atr:
            best_ob = ob
            best_distance_atr = distance_atr

    if best_ob is None:
        return

    displacement_move = abs(
        float(ob_window["close"].iloc[best_ob.event_bar_index])
        - float(ob_window["close"].iloc[best_ob.formed_at_bar])
    )
    displacement_atr_ratio = displacement_move / latest_atr

    score = _compute_setup_score(displacement_atr_ratio, best_distance_atr)
    if score < MIN_SETUP_SCORE:
        logger.info(
            "[OB-REJECT] %s %s score %.1f below min %.1f (disp=%.2fx conf=%.2fx)",
            symbol, best_ob.direction, score, MIN_SETUP_SCORE, displacement_atr_ratio, best_distance_atr,
        )
        return

    buffer = latest_atr * SL_ATR_BUFFER_MULT
    provisional_entry = (best_ob.low + best_ob.high) / 2.0
    if best_ob.direction == "LONG":
        provisional_sl = best_ob.low - buffer
    else:
        provisional_sl = best_ob.high + buffer
    provisional_tp = next_target(best_ob.direction, provisional_entry, vp)

    risk = abs(provisional_entry - provisional_sl)
    rr = abs(provisional_tp - provisional_entry) / risk if risk > 0 else 0.0

    age_bars = (len(ob_window) - 1) - best_ob.formed_at_bar
    expires_in_bars = max(OB_MAX_AGE_BARS - age_bars, 1)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_bars)

    reason = f"{best_ob.structure_event} disp={displacement_atr_ratio:.2f}xATR conf={best_distance_atr:.2f}xATR"
    db.save_armed_setup({
        "symbol": symbol,
        "direction": best_ob.direction,
        "trigger_price": provisional_entry,
        "entry_low": best_ob.low,
        "entry_high": best_ob.high,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(rr, 2),
        "score": score,
        "setup_reason": reason,
        "trend_summary": f"VP({VP_TF}) POC={vp.poc:.6g} VAH={vp.vah:.6g} VAL={vp.val:.6g}",
        "expires_at": expires_at.isoformat(),
    })
    logger.info(
        "[OB-ARM] %s %s zone=[%.6g,%.6g] score=%.1f reason=%s",
        symbol, best_ob.direction, best_ob.low, best_ob.high, score, reason,
    )


# ── Phase 2: monitor ──────────────────────────────────────────────────

def _monitor_setup(symbol: str, setup: dict) -> Signal | None:
    direction = setup["direction"]
    ob_low = setup["entry_low"]
    ob_high = setup["entry_high"]

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    if ob_df is None or ob_df.empty or len(ob_df) < 2:
        return None
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)
    if ob_window.empty:
        return None

    last = ob_window.iloc[-1]
    last_open  = float(last["open"])
    last_high  = float(last["high"])
    last_low   = float(last["low"])
    last_close = float(last["close"])
    last_vol   = float(last["volume"])

    mid = (ob_low + ob_high) / 2.0

    if direction == "LONG" and last_close < mid:
        db.mark_armed_setup_invalidated(setup["id"], f"close {last_close:.6g} < midpoint {mid:.6g}")
        logger.info("[OB-INVALIDATE] %s LONG closed below midpoint", symbol)
        return None
    if direction == "SHORT" and last_close > mid:
        db.mark_armed_setup_invalidated(setup["id"], f"close {last_close:.6g} > midpoint {mid:.6g}")
        logger.info("[OB-INVALIDATE] %s SHORT closed above midpoint", symbol)
        return None

    expires_at = datetime.fromisoformat(setup["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        db.mark_armed_setup_expired(setup["id"])
        logger.info("[OB-EXPIRE] %s setup #%s aged out", symbol, setup["id"])
        return None

    touched = (last_low <= ob_high) and (last_high >= ob_low)
    if not touched:
        return None

    confirmed = last_close > ob_high if direction == "LONG" else last_close < ob_low
    if not confirmed:
        return None

    candle_range = last_high - last_low
    if candle_range <= 0:
        return None
    body_ratio = abs(last_close - last_open) / candle_range
    if body_ratio < OB_BODY_RATIO_MIN:
        db.mark_armed_setup_missed(setup["id"], f"body_ratio {body_ratio:.2f} below {OB_BODY_RATIO_MIN}")
        logger.info("[OB-MISS] %s retest body ratio %.2f below min", symbol, body_ratio)
        return None
    if direction == "LONG" and last_close < last_open:
        db.mark_armed_setup_missed(setup["id"], "retest candle not bullish")
        return None
    if direction == "SHORT" and last_close > last_open:
        db.mark_armed_setup_missed(setup["id"], "retest candle not bearish")
        return None

    volume_ma = float(ob_window["volume"].astype(float).tail(OB_VOLUME_MA_BARS).mean())
    if volume_ma <= 0 or last_vol < OB_VOLUME_MIN_MULT * volume_ma:
        db.mark_armed_setup_missed(setup["id"], f"volume {last_vol:.0f} below {OB_VOLUME_MIN_MULT}x MA {volume_ma:.0f}")
        logger.info("[OB-MISS] %s retest volume below threshold", symbol)
        return None

    if BTC_GATE_ENABLED:
        btc_close, btc_dema = _get_btc_dema()
        if btc_close is not None and btc_dema is not None:
            ranging_margin = btc_dema * (BTC_RANGING_PCT / 100.0)
            btc_bullish = btc_close > btc_dema + ranging_margin
            btc_bearish = btc_close < btc_dema - ranging_margin
            if direction == "LONG" and btc_bearish:
                db.mark_armed_setup_missed(setup["id"], "BTC macro gate: bearish")
                logger.info("[OB-MISS] %s LONG blocked by BTC macro gate", symbol)
                return None
            if direction == "SHORT" and btc_bullish:
                db.mark_armed_setup_missed(setup["id"], "BTC macro gate: bullish")
                logger.info("[OB-MISS] %s SHORT blocked by BTC macro gate", symbol)
                return None

    entry = last_close
    atr = float(_atr_series(ob_window, ATR_PERIOD).iloc[-1])
    if atr <= 0:
        db.mark_armed_setup_missed(setup["id"], "non-positive ATR at retest")
        return None
    buffer = atr * SL_ATR_BUFFER_MULT

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        db.mark_armed_setup_missed(setup["id"], "VP refetch failed")
        return None
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)
    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        db.mark_armed_setup_missed(setup["id"], "VP recompute degenerate")
        return None

    if direction == "LONG":
        sl_price = ob_low - buffer
        tp_price = next_target("LONG", entry, vp)
    else:
        sl_price = ob_high + buffer
        tp_price = next_target("SHORT", entry, vp)

    if not _valid_trade_geometry(direction, entry, tp_price, sl_price):
        db.mark_armed_setup_missed(setup["id"], "invalid trade geometry at retest")
        logger.info("[OB-MISS] %s invalid geometry at retest", symbol)
        return None

    if direction == "LONG":
        risk_pct = (entry - sl_price) / entry * 100.0
        reward_pct = (tp_price - entry) / entry * 100.0
    else:
        risk_pct = (sl_price - entry) / entry * 100.0
        reward_pct = (entry - tp_price) / entry * 100.0

    if risk_pct <= 0 or reward_pct <= 0:
        db.mark_armed_setup_missed(setup["id"], "non-positive risk/reward at retest")
        return None

    rr = reward_pct / risk_pct
    tp_roi_pct = reward_pct * LEVERAGE
    sl_roi_pct = risk_pct * LEVERAGE

    if rr < MIN_STRUCTURE_RR:
        db.mark_armed_setup_missed(setup["id"], f"RR {rr:.2f} below min {MIN_STRUCTURE_RR}")
        logger.info("[OB-MISS] %s RR %.2f below min", symbol, rr)
        return None
    if tp_roi_pct < MIN_TP_ROI_PCT:
        db.mark_armed_setup_missed(setup["id"], f"TP ROI {tp_roi_pct:.1f} below min {MIN_TP_ROI_PCT}")
        logger.info("[OB-MISS] %s TP ROI %.1f below min", symbol, tp_roi_pct)
        return None
    if sl_roi_pct > MAX_SL_ROI_PCT:
        db.mark_armed_setup_missed(setup["id"], f"SL ROI {sl_roi_pct:.1f} above max {MAX_SL_ROI_PCT}")
        logger.info("[OB-MISS] %s SL ROI %.1f above max", symbol, sl_roi_pct)
        return None

    logger.info(
        "[SIGNAL] %s %s entry=%.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f",
        symbol, direction, entry, tp_price, sl_price, rr, setup["score"],
    )

    return Signal(
        symbol=symbol,
        direction=direction,
        entry_price=round(entry, 8),
        tp_price=round(tp_price, 8),
        sl_price=round(sl_price, 8),
        leverage=LEVERAGE,
        tp_roi_pct=round(tp_roi_pct, 1),
        sl_roi_pct=round(sl_roi_pct, 1),
        timeframe_summary=f"{OB_TF.upper()} OB retest | VP({VP_TF}) bias | score {setup['score']:.0f}",
        generated_at=datetime.now(timezone.utc),
        rr=round(rr, 2),
        score=setup["score"],
        entry_low=ob_low,
        entry_high=ob_high,
        armed_setup_id=setup["id"],
    )


# ── Public: scan one symbol ───────────────────────────────────────

def scan_symbol(symbol: str) -> Signal | None:
    """
    If a setup is already armed for this symbol, monitor it for retest/
    invalidation/expiry. Otherwise, try to arm a new one. Returns a Signal
    only when an armed setup fires this cycle.
    """
    try:
        existing = db.get_armed_setup_by_symbol(symbol)
        if existing is not None:
            return _monitor_setup(symbol, existing)
        _try_arm_setup(symbol)
        return None
    except Exception as e:
        logger.error("Error scanning %s: %s", symbol, e, exc_info=True)
        return None
```

- [ ] **Step 2: Verify strategy.py compiles**

Run: `python -m py_compile strategy.py`
Expected: no output, exit code 0

- [ ] **Step 3: Commit**

```bash
git add strategy.py
git commit -m "strategy: replace Trend Speed Analyzer with VP-OB Confluence arm/monitor workflow"
```

---

### Task 5: main.py and bot.py wiring

**Files:**
- Modify: `main.py:1-11` (docstring), `main.py:109-218` (`scan_and_fire_signals`)
- Modify: `bot.py:73` (strategy name line in `format_signal`)

**Interfaces:**
- Consumes: `strategy.scan_symbol` (Task 4, same signature as before — no caller-side signature change needed except reading the new `armed_setup_id` field); `database.expire_old_armed_setups`, `database.mark_armed_setup_fired`, `database.mark_armed_setup_missed` (existing, unchanged); `config.STRATEGY_NAME` (Task 3).

- [ ] **Step 1: Update main.py's module docstring**

In `main.py`, replace lines 1-11:

```python
"""
Main entry point — Trend Speed Analyzer (Zeiierman).

Scheduler jobs:
  Hourly at :02   — scanner: detect DynEMA crossover, fire signal on close
  Every 1 min     — outcome checker
  Every 6h        — coin pool refresh
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""
```

with:

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

- [ ] **Step 2: Add config imports for OB_TF and expire_old_armed_setups wiring**

In `main.py`, find the `from config import (` block (currently starting at line 31) and add `OB_TF` to the imported names, right after `SIGNAL_TF,`:

```python
from config import (
    LKT,
    LEVERAGE,
    SIGNAL_TF,
    OB_TF,
    CANDLE_MINUTES,
```

- [ ] **Step 3: Expire stale armed setups at the top of scan_and_fire_signals**

In `main.py`, inside `scan_and_fire_signals`, find:

```python
    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now            = datetime.now(timezone.utc)
```

Replace with:

```python
    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_armed_setups(now)
```

(Note: the original `now = datetime.now(timezone.utc)` line that followed is now merged into this replacement — do not duplicate it. The line `cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)` and everything after stays as-is, unchanged, right after this block.)

- [ ] **Step 4: Wire armed-setup fired/missed transitions into the fire loop**

In `main.py`, find this block inside `scan_and_fire_signals`:

```python
        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
            )

            await tg.broadcast_signal(app, sig, signal_id)
            fired += 1

            logger.info(
                "[SCAN] Fired #%d | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.1f",
                signal_id, sig.symbol, sig.direction,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )

        except Exception as e:
            logger.error("[SCAN] Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)
```

Replace with:

```python
        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
            )

            if sig.armed_setup_id is not None:
                db.mark_armed_setup_fired(sig.armed_setup_id, signal_id)

            await tg.broadcast_signal(app, sig, signal_id)
            fired += 1

            logger.info(
                "[SCAN] Fired #%d | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.1f",
                signal_id, sig.symbol, sig.direction,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )

        except Exception as e:
            logger.error("[SCAN] Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, f"post-scan save failed: {e}")
```

Also find the two earlier `continue` sites in the same loop (cooldown race-guard and geometry-block) and add the same cleanup so a returned Signal never leaves its armed setup stuck as `'armed'`:

Find:

```python
        # Re-check cooldown (race guard for parallel results)
        if db.signal_exists_for_coin(sig.symbol, cooldown_since):
            logger.debug("[SCAN] %s cooldown hit after parallel scan", sig.symbol)
            continue

        # Geometry validation (skill requirement)
        if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            continue
```

Replace with:

```python
        # Re-check cooldown (race guard for parallel results)
        if db.signal_exists_for_coin(sig.symbol, cooldown_since):
            logger.debug("[SCAN] %s cooldown hit after parallel scan", sig.symbol)
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, "cooldown hit after parallel scan")
            continue

        # Geometry validation (skill requirement)
        if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, "geometry invalid post-scan")
            continue
```

- [ ] **Step 5: Point the outcome checker at OB_TF instead of SIGNAL_TF**

In `main.py`, inside `check_outcomes`, find:

```python
        try:
            df = get_klines(symbol, SIGNAL_TF, count=fetch_count)
```

Replace with:

```python
        try:
            df = get_klines(symbol, OB_TF, count=fetch_count)
```

- [ ] **Step 6: Update the startup log line**

In `main.py`, inside `main()`, find:

```python
    logger.info(
        "[CONFIG] signal TF=%s scan=%s/%s daily_cap=%d gap=%dmin cooldown=%dmin slots=%d",
        SIGNAL_TF, SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        SIGNAL_COOLDOWN_MINUTES, MAX_CONCURRENT_SIGNALS,
    )
```

Replace with:

```python
    logger.info(
        "[CONFIG] OB TF=%s scan=%s/%s daily_cap=%d gap=%dmin cooldown=%dmin slots=%d",
        OB_TF, SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        SIGNAL_COOLDOWN_MINUTES, MAX_CONCURRENT_SIGNALS,
    )
```

- [ ] **Step 7: Update bot.py's hardcoded strategy name**

In `bot.py`, update the module docstring (line 2) and the config import (line 17).

Find:

```python
"""
Telegram bot: commands and signal broadcast for Trend Speed Analyzer strategy.

Signal messages use HTML parse mode.
"""
```

Replace with:

```python
"""
Telegram bot: commands and signal broadcast for VP-OB Confluence strategy.

Signal messages use HTML parse mode.
"""
```

Find:

```python
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT
```

Replace with:

```python
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT, STRATEGY_NAME
```

Then in `bot.py`, find (in `format_signal`):

```python
        f"📈 Strategy: Trend Speed Analyzer (Zeiierman)",
```

Replace with:

```python
        f"📈 Strategy: {STRATEGY_NAME}",
```

- [ ] **Step 8: Verify everything compiles**

Run: `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py volume_profile.py order_blocks.py`
Expected: no output, exit code 0

- [ ] **Step 9: Commit**

```bash
git add main.py bot.py
git commit -m "main/bot: wire armed-setup lifecycle, switch outcome checker to OB_TF"
```

---

### Task 6: Offline sanity script and final validation

**Files:**
- Create: `scripts/vp_ob_sanity_check.py`

**Interfaces:**
- Consumes: `mexc_client.get_klines`, `volume_profile.compute_volume_profile`, `volume_profile.vp_bias`, `order_blocks.find_swings`, `order_blocks.detect_bos_choch`, `order_blocks.find_order_blocks`, `strategy._atr_series`, `config.VP_TF`, `config.VP_KLINE_COUNT`, `config.VP_LOOKBACK_BARS`, `config.VP_BINS`, `config.VP_VALUE_AREA_PCT`, `config.VP_HVN_MULT`, `config.VP_LVN_MULT`, `config.OB_TF`, `config.OB_KLINE_COUNT`, `config.OB_SWING_LENGTH`, `config.OB_DISPLACEMENT_ATR_MULT`, `config.ATR_PERIOD` (all existing by this point in the plan).

- [ ] **Step 1: Write the sanity-check script**

Create `scripts/vp_ob_sanity_check.py`:

```python
"""
Offline sanity check: fetch recent candles for a few known-liquid pairs and
print the computed Volume Profile levels and detected Order Blocks, so the
math can be eyeballed against a chart before trusting live signals.

Run: python scripts/vp_ob_sanity_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mexc_client import get_klines
from order_blocks import detect_bos_choch, find_order_blocks, find_swings
from strategy import _atr_series
from volume_profile import compute_volume_profile, vp_bias
from config import (
    ATR_PERIOD,
    OB_DISPLACEMENT_ATR_MULT,
    OB_KLINE_COUNT,
    OB_SWING_LENGTH,
    OB_TF,
    VP_BINS,
    VP_HVN_MULT,
    VP_KLINE_COUNT,
    VP_LOOKBACK_BARS,
    VP_LVN_MULT,
    VP_TF,
    VP_VALUE_AREA_PCT,
)

SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]


def check_symbol(symbol: str) -> None:
    print(f"\n=== {symbol} ===")

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        print("  VP: insufficient candles")
        return
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)
    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        print("  VP: degenerate window")
        return
    close = float(vp_window["close"].iloc[-1])
    bias = vp_bias(close, vp)
    print(f"  VP({VP_TF}): POC={vp.poc:.6g} VAH={vp.vah:.6g} VAL={vp.val:.6g} "
          f"close={close:.6g} bias={bias}")
    print(f"  HVNs: {[round(h, 4) for h in vp.hvns]}")
    print(f"  LVNs: {[round(l, 4) for l in vp.lvns]}")

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    min_ob_bars = ATR_PERIOD + OB_SWING_LENGTH * 2 + 10
    if ob_df is None or ob_df.empty or len(ob_df) < min_ob_bars:
        print("  OB: insufficient candles")
        return
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)
    atr = _atr_series(ob_window, ATR_PERIOD)
    swings = find_swings(ob_window, length=OB_SWING_LENGTH)
    events = detect_bos_choch(ob_window, swings)
    obs = find_order_blocks(ob_window, events, atr, displacement_atr_mult=OB_DISPLACEMENT_ATR_MULT)

    print(f"  OB({OB_TF}): {len(swings)} swings, {len(events)} structure events, {len(obs)} order blocks")
    for ob in obs[-5:]:
        print(f"    {ob.direction} [{ob.low:.6g}, {ob.high:.6g}] "
              f"formed_at_bar={ob.formed_at_bar} event={ob.structure_event}")


if __name__ == "__main__":
    for sym in SYMBOLS:
        check_symbol(sym)
```

- [ ] **Step 2: Run the sanity script and eyeball the output**

Run: `python scripts/vp_ob_sanity_check.py`
Expected: prints VP levels (POC/VAH/VAL) and detected Order Blocks for BTC_USDT, ETH_USDT, SOL_USDT with no exceptions. Compare the printed POC/VAH/VAL against a chart for the same pair/timeframe to confirm the levels look reasonable (POC should sit where the pair spent the most time/volume recently; VAH/VAL should bracket the bulk of recent price action).

- [ ] **Step 3: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all tests from Task 1 and Task 2 PASS (11 tests total)

- [ ] **Step 4: Final compile check across the whole app**

Run: `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py volume_profile.py order_blocks.py scripts/vp_ob_sanity_check.py`
Expected: no output, exit code 0

- [ ] **Step 5: Commit**

```bash
git add scripts/vp_ob_sanity_check.py
git commit -m "test: add offline VP/OB sanity-check script"
```

---

## Deployment Note (not part of this plan's tasks — do after review)

Once this plan's tasks are merged and pushed to `main`, the auto-deploy workflow restarts `mexc-bot` and `mexc-dashboard` with no schema migration needed (armed_setups already exists). Recommend clearing stale state first since old signals/armed_setups rows belong to the previous strategy:

```bash
# on server, per CLAUDE.md's clean restart procedure
sudo systemctl stop mexc-bot
python clear_db.py --yes
sudo systemctl start mexc-bot
sudo journalctl -u mexc-bot -f
```

Watch logs for `[OB-ARM]`, `[SIGNAL]`, `[OB-INVALIDATE]`, `[OB-EXPIRE]`, `[OB-MISS]`, `[OB-REJECT]` for at least a day before trusting signal quality, per the design spec's testing plan.
