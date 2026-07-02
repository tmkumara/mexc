# Design: Volume Profile + Order Block Confluence Strategy

**Date:** 2026-07-02
**Status:** Approved (design phase)
**Strategy name:** `VP-OB Confluence` (4H Volume Profile bias + 1H Order Block entry)

## Background

The bot's strategy has been replaced several times (see memory: EMA/RSI → Hull Suite →
ZLSMA+CE → RSI Heatmap → SMC Sweep+OB Retest → MTF Trend Pullback → Trend Speed Analyzer).
This design **fully replaces the current Trend Speed Analyzer (DynEMA + TrendSpeed
crossover) strategy** with a new strategy built on two concepts:

1. **Volume Profile** (POC, Value Area High/Low, High/Low Volume Nodes) — for directional
   bias and take-profit targets.
2. **Order Blocks** (Smart Money Concepts) — for entry zones, confirmed by market structure
   shifts (BOS/CHoCH) and displacement.

This project previously tried an "MTF SMC Sweep + OB Retest" strategy (evolution step 9)
before replacing it; the reason it was replaced is not recorded. This design treats that as
a fresh attempt, with configurable strictness knobs so frequency/quality can be tuned without
a rewrite if it turns out too strict or too loose.

## Target Profile (decided with user)

- **Signal frequency:** 1-3 high-quality signals/day (across the ~80-coin pool)
- **ROI target:** ~50% ROI per TP hit at 20x leverage (tight OB stops + far VP targets)
- **Risk:reward:** minimum 1:2, target 1:2-1:3
- **Timeframes:** 4H Volume Profile for bias/levels, 1H Order Blocks for entry
- **BTC macro gate:** kept — signal direction must still agree with BTC 1H DynEMA trend
- **VP window:** rolling last ~5 days (30 × 4H bars), recomputed every cycle

## Architecture: Two-Phase Armed-Setup Workflow

Chosen over (a) a stateless single-pass recompute-every-scan design and (b) an in-memory-only
tracking design, because Order Blocks are not immediately tradeable — price must return and
retest the zone, which can take many candles. This requires persistent state across scan
cycles. The existing `armed_setups` DB table already has the right columns for this pattern
(it appears to be unused schema left over from an earlier strategy generation) — this design
activates it rather than adding new tables.

```
Every hour, at :01 past (aligned to 1H candle close), for each pooled symbol:

PHASE 1 — Detect & Arm
  1. Fetch 4H candles → compute rolling Volume Profile (last 30×4h bars ≈ 5 days)
     → POC, VAH, VAL, HVN/LVN zones
  2. Determine bias: close > VAH → bullish bias; close < VAL → bearish bias;
     inside VA → no bias, skip symbol this cycle
  3. Fetch 1H candles → detect swing structure (BOS/CHoCH) + fresh Order Blocks
     with displacement (FVG or >=1.5x ATR move)
  4. Keep only OBs whose direction matches VP bias
  5. Confluence filter: OB must sit at/near (within 0.5x ATR of) VAL/VAH/POC/HVN
  6. If a qualifying OB isn't already armed for this symbol -> INSERT into
     armed_setups (zone, direction, computed SL, provisional TP, expiry)

PHASE 2 — Monitor & Fire (same cycle, runs over all currently-armed setups)
  1. For each armed setup not yet expired/invalidated:
     - If latest closed 1H candle closed beyond the OB's far edge without a
       valid retest -> mark invalidated, drop it
     - If latest closed 1H candle wicked into the zone AND closed back outside
       in the trade direction (sweep/rejection) with volume >= 1.5x 20-bar MA
       -> this is the trigger. Recompute final SL/TP against current VP levels,
       run geometry + ROI quality gates, fire Signal.
     - If armed setup exceeds max age (N bars) with no retest -> expire it
  2. BTC 1H macro gate still applies as a final check before firing.

Outcome tracking (main.py check_outcomes) is unchanged.
```

## Volume Profile Computation

```python
BINS = 40                      # price bins across the window's hi-lo range
VALUE_AREA_PCT = 0.70

def compute_volume_profile(candles: list[Candle]) -> VolumeProfile:
    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    bin_size = (hi - lo) / BINS
    bin_volume = [0.0] * BINS

    for c in candles:
        first_bin = int((c.low - lo) / bin_size)
        last_bin  = min(int((c.high - lo) / bin_size), BINS - 1)
        span = max(last_bin - first_bin + 1, 1)
        per_bin = c.volume / span            # uniform hi-lo distribution
        for b in range(first_bin, last_bin + 1):
            bin_volume[b] += per_bin

    poc_bin = max(range(BINS), key=lambda b: bin_volume[b])
    total = sum(bin_volume)

    # Value Area: greedy expansion from POC until 70% of volume covered
    lo_b = hi_b = poc_bin
    covered = bin_volume[poc_bin]
    while covered < VALUE_AREA_PCT * total:
        next_lo = bin_volume[lo_b - 1] if lo_b > 0 else -1
        next_hi = bin_volume[hi_b + 1] if hi_b < BINS - 1 else -1
        if next_hi >= next_lo:
            hi_b += 1; covered += bin_volume[hi_b]
        else:
            lo_b -= 1; covered += bin_volume[lo_b]

    poc = lo + (poc_bin + 0.5) * bin_size
    vah = lo + (hi_b + 1) * bin_size
    val = lo + lo_b * bin_size

    # HVN/LVN: smooth histogram (3-bin moving avg), then threshold vs mean
    smoothed = _smooth3(bin_volume)
    mean_vol = total / BINS
    hvns = [lo + (b+0.5)*bin_size for b in range(BINS) if smoothed[b] > 1.5*mean_vol]
    lvns = [lo + (b+0.5)*bin_size for b in range(BINS) if smoothed[b] < 0.4*mean_vol]

    return VolumeProfile(poc=poc, vah=vah, val=val, hvns=hvns, lvns=lvns)
```

**Bias rule:** `close > vah` -> bullish bias (only arm bullish OBs); `close < val` -> bearish
bias; otherwise no bias, skip.

**TP rule:** TP = POC if entry sits below POC (LONG) / above POC (SHORT) beyond entry,
else the opposite Value Area edge (VAH for longs, VAL for shorts). Single-target only
(matches current DB schema — no multi-target TP ladder).

New config: `VP_TF=4h`, `VP_LOOKBACK_BARS=30`, `VP_BINS=40`, `VP_VALUE_AREA_PCT=0.70`,
`VP_HVN_MULT=1.5`, `VP_LVN_MULT=0.4`.

## Order Block Detection (1H)

```python
SWING_LENGTH = 6           # bars each side for a pivot high/low
DISPLACEMENT_ATR_MULT = 1.5
OB_MAX_AGE_BARS = 40       # ~1.7 days on 1H before an unmitigated OB expires
OB_INVALIDATE_AT_MIDPOINT = True

def find_swings(candles, length=SWING_LENGTH) -> list[Swing]: ...
    # bar i is a swing high if its high is the max of the +/-length window; swing low mirrors

def detect_bos_choch(candles, swings) -> list[StructureEvent]: ...
    # a CLOSE beyond the last confirmed swing high/low = BOS (continuation) or
    # CHoCH (reversal); use close, not wick, to reduce fakeouts

def find_order_blocks(candles, structure_events, atr) -> list[OrderBlock]:
    obs = []
    for event in structure_events:
        i = event.bar_index
        j = i
        while j > 0 and _same_direction(candles[j], event.direction):
            j -= 1
        ob_candle = candles[j]

        move = abs(candles[i].close - ob_candle.close)
        has_fvg = _has_fair_value_gap(candles, j, i, event.direction)
        if move < DISPLACEMENT_ATR_MULT * atr[i] and not has_fvg:
            continue

        obs.append(OrderBlock(
            direction=event.direction,
            low=ob_candle.low, high=ob_candle.high,
            formed_at_bar=j, structure_event=event.kind,
        ))
    return obs
```

**Confluence filter** (Phase 1, before arming): OB direction must match 4H VP bias, AND
OB midpoint within `0.5x ATR(1h)` of VAL, VAH, POC, or an HVN.

**Mitigation / invalidation** (Phase 2, every cycle):
- First touch back into `[low, high]` after formation = the retest — only the first touch
  is tradeable; the armed setup is consumed (fired or dropped) on first touch either way.
- Invalidated if a candle **closes** past the OB's 50% midpoint against the trade direction
  before any valid retest fires it.
- Expired if `current_bar - formed_at_bar > OB_MAX_AGE_BARS` with no retest.

**Entry trigger on retest:** the retest candle must close back outside the zone in the
trade direction, with body ratio >= 50% and volume >= 1.5x the 20-bar volume MA.

New config: `OB_TF=1h`, `OB_SWING_LENGTH=6`, `OB_DISPLACEMENT_ATR_MULT=1.5`,
`OB_MAX_AGE_BARS=40`, `OB_CONFLUENCE_ATR_MULT=0.5`, `OB_BODY_RATIO_MIN=0.50`,
`OB_VOLUME_MIN_MULT=1.5`.

## SL/TP Placement & Risk Gates

```python
ATR_PERIOD = 14
SL_ATR_BUFFER_MULT = 0.35     # anti-stop-hunt buffer beyond the OB edge

def compute_sl_tp(ob: OrderBlock, direction: str, vp: VolumeProfile, atr: float, entry_price: float):
    buffer = SL_ATR_BUFFER_MULT * atr
    if direction == "LONG":
        sl = ob.low - buffer
        tp = vp.poc if entry_price < vp.poc else vp.vah
    else:
        sl = ob.high + buffer
        tp = vp.poc if entry_price > vp.poc else vp.val
    return sl, tp
```

Risk/ROI gates (20x leverage):

```env
LEVERAGE=20
MIN_STRUCTURE_RR=2.00
MIN_TP_ROI_PCT=45.0
TARGET_TP_ROI_PCT=50.0
MAX_SL_ROI_PCT=28.0
MIN_SETUP_SCORE=90          # confluence score (VP proximity + displacement + volume ratio, 0-100)
```

If a retest fires but computed RR/ROI fails these gates, the setup is dropped (not
re-armed) — enforces "first mitigation only."

Daily caps (kept from current config, now the frequency backstop rather than primary
lever): `MAX_DAILY_SIGNALS=3`, `MIN_DAILY_SIGNAL_GAP_MINUTES=180`,
`SIGNAL_COOLDOWN_MINUTES=240`.

## File-Level Changes

- **`strategy.py`** — full rewrite. Remove `_compute_dyn_ema`, `_compute_trend_speed`, and
  all TrendSpeed-specific gates. Add `compute_volume_profile()`, `find_swings()`,
  `detect_bos_choch()`, `find_order_blocks()`, `arm_setup()`, `monitor_armed_setups()`.
  Keep `Signal` dataclass, `_valid_trade_geometry()`, `_get_btc_dema()` (BTC macro gate),
  `_atr_series()`.
- **`config.py`** — remove `SIGNAL_TF`, `SPEED_REL_THRESHOLD`, `SPEED_ACCEL_ENABLED`,
  `DYN_EMA_MAX_LENGTH`, `DYN_EMA_ACCEL_MULT`. Add all `VP_*`/`OB_*` knobs above. Retune
  ROI/RR/cap values to the targets above. Change `SETUP_SCAN_CRON_MINUTES` to hourly
  (aligned to 1H close).
- **`database.py`** — **no schema changes.** `armed_setups` table (currently unused) already
  has the needed columns: `entry_low`/`entry_high` (OB zone), `trigger_price`, `sl_price`/
  `tp_price`, `rr`, `score` (confluence score), `setup_reason` (text description),
  `expires_at`, `miss_reason` (invalidation reason).
- **`main.py`** — scheduler calls the new two-phase `scan_symbol()` once per hour per pooled
  symbol; keep `db.claim_setup_for_fire()` / `mark_setup_fire_failed()` pattern, geometry
  and outcome-checker blocks unchanged.
- **`bot.py` / `webui.py`** — update displayed strategy name/config; optionally surface
  VP levels and OB zone info in the Telegram message body (nice-to-have).
- **Deleted:** `trend_scanner.py` (400-line dead code, unused Fibonacci/trend alert
  scanner, not imported anywhere).

## Error Handling

- Skip symbol if fewer than `VP_LOOKBACK_BARS` 4H candles are available (new listings).
- Guard divide-by-zero if a symbol's price range is flat (`bin_size == 0`).
- Existing REST/WS fallback chains in `market_data.py` are unchanged.

## Testing Plan

1. `python -m py_compile config.py database.py strategy.py main.py bot.py webui.py`
2. Standalone offline script: feed recent historical candles for 3-5 known-liquid pairs
   through `compute_volume_profile`/`find_order_blocks`, log POC/VAH/VAL and detected OBs,
   sanity-check against what a chart would show, before going live.
3. Deploy and watch `journalctl -u mexc-bot -f` for `[ARM]`/`[FIRE]`/`[EXPIRE]`/
   `[ENTRY-REJECT]` log lines for at least a day before trusting signal quality.

## Research Reference

Background research on Volume Profile and Order Block concepts (algorithmic definitions,
confluence rules, parameter ranges) was conducted via a research-only agent; findings are
summarized/incorporated throughout this design (bin-based VP approximation, BOS/CHoCH +
displacement-gated OB detection, VP-level confluence and TP targeting, ATR-buffered SL
placement).
