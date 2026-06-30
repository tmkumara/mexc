# Trend Speed Filter Improvement — Design Spec
**Date:** 2026-06-30  
**Status:** Approved  
**Target:** 10% TP ROI @ 20× leverage, 10 signals/day  

---

## 1. Goal

Improve the Trend Speed Analyzer bot to produce ~10 signals/day with a 10% TP ROI target at 20× isolated leverage. Current setup (1h TF, loose trendspeed gate) targets 30–50% ROI and caps at 5 signals/day — too infrequent and oversized for the user's preferred style.

---

## 2. Approach Selected

**Approach A — Lightweight Upgrade:**
- Switch signal timeframe 1h → 15m
- Add trendspeed magnitude gate (price-relative threshold)
- Add trendspeed acceleration gate (momentum must be building)
- Add BTC 1h DynEMA macro filter (altcoin direction must align with BTC trend)
- Tighten SL/TP and ROI config to match 10% target

No structural rewrites. All changes are additive filters inside `scan_symbol()` plus config tuning.

---

## 3. Signal Flow

```
scan_symbol(symbol)
│
├─ 1. Fetch 300 × 15m klines
├─ 2. Drop in-progress candle (use completed only)
├─ 3. Compute DynEMA (max_length=50, accel_mult=5.0) — unchanged
├─ 4. Compute TrendSpeed (RMA×HMA pipeline) — unchanged
│
├─ 5. DynEMA crossover check (last 2 completed candles)
│     LONG:  prev_close ≤ prev_dema AND curr_close > curr_dema
│     SHORT: prev_close ≥ prev_dema AND curr_close < curr_dema
│     Neither → return None
│
├─ 6. Trendspeed direction gate [existing]
│     LONG: curr_speed > 0 / SHORT: curr_speed < 0
│
├─ 7. Trendspeed magnitude gate [NEW]
│     abs(curr_speed) / curr_close > SPEED_REL_THRESHOLD (default 0.0002)
│     Rejects weak/stalling crossovers; price-relative so works across all coin prices
│
├─ 8. Trendspeed acceleration gate [NEW, toggleable]
│     LONG:  curr_speed > prev_speed  (momentum building upward)
│     SHORT: curr_speed < prev_speed  (momentum building downward)
│     Uses trendspeed.iloc[-1] vs trendspeed.iloc[-2] (both completed candles)
│
├─ 9. BTC 1h macro gate [NEW, toggleable]
│     _get_btc_dema() — module-level cache, 14-min TTL, fetched once per scan cycle
│     LONG:  btc_close > btc_dema × (1 + BTC_RANGING_PCT/100)
│     SHORT: btc_close < btc_dema × (1 - BTC_RANGING_PCT/100)
│     Ranging band (within ±BTC_RANGING_PCT): gate passes both directions
│
├─ 10. ATR(14)-based SL/TP
│      SL = entry ± ATR × SL_ATR_MULT (0.5)
│      TP = entry ± risk × REWARD_RATIO (2.0)
│      → At typical 15m ATR ~0.4%: SL ~0.2%, TP ~0.4%, SL ROI ~4%, TP ROI ~8-10%
│
├─ 11. ROI bounds gate
│      sl_roi ∈ [MIN_SL_ROI_PCT=2%, MAX_SL_ROI_PCT=15%]
│      tp_roi ≥ MIN_TP_ROI_PCT=8%
│
└─ 12. Return Signal or None
```

---

## 4. BTC Cache Design

Module-level cache in `strategy.py`:

```python
_btc_cache: dict = {"dema": None, "close": None, "ts": 0.0}
_BTC_CACHE_TTL = 14 * 60  # 14 minutes — expires before next 15m scan
```

- `_get_btc_dema()` checks TTL; if stale, fetches `BTC_KLINE_COUNT` × `BTC_TF` klines, computes DynEMA, stores last values
- Called at the top of `scan_symbol()` — not once per coin; cache ensures single fetch per 15m window across all 80 coins
- If BTC fetch fails: gate passes (fail-open), warning logged

---

## 5. Config Changes

### Changed defaults

| Variable | Old | New | Reason |
|---|---|---|---|
| `SIGNAL_TF` | `1h` | `15m` | 15m candles for tighter ROI |
| `CANDLE_MINUTES` | `60` | `15` | Derived from SIGNAL_TF |
| `SETUP_SCAN_CRON_MINUTES` | `2` | `*/15` | Scan every 15m |
| `SETUP_SCAN_CRON_HOURS` | `*` | `*` | Unchanged |
| `SL_ATR_MULT` | `1.0` | `0.5` | Tighter stop → ~10% TP ROI |
| `MIN_TP_ROI_PCT` | `30.0` | `8.0` | Allow 10% target with variance |
| `MAX_SL_ROI_PCT` | `35.0` | `15.0` | Cap for 15m ATR-based stops |
| `MIN_SL_ROI_PCT` | `5.0` | `2.0` | Allow tight but real stops |
| `SIGNAL_COOLDOWN_MINUTES` | `240` | `60` | Allow coin repeats ~hourly |
| `MAX_DAILY_SIGNALS` | `5` | `10` | Target 10/day |
| `MIN_DAILY_SIGNAL_GAP_MINUTES` | `60` | `6` | Allow signals 6 min apart |
| `MAX_CONCURRENT_SIGNALS` | `3` | `5` | More open trades simultaneously |

### New variables

| Variable | Default | Purpose |
|---|---|---|
| `BTC_SYMBOL` | `BTC_USDT` | Symbol to use for macro filter |
| `BTC_TF` | `1h` | Timeframe for BTC DynEMA |
| `BTC_KLINE_COUNT` | `300` | Kline history for BTC DynEMA warmup |
| `BTC_GATE_ENABLED` | `true` | Toggle BTC filter on/off |
| `BTC_RANGING_PCT` | `0.10` | ±% band where BTC is "ranging" → pass both directions |
| `SPEED_REL_THRESHOLD` | `0.0002` | Min `abs(speed)/close`; 0.02% of price |
| `SPEED_ACCEL_ENABLED` | `true` | Toggle acceleration gate on/off |

### .env additions

```env
# ── Trend Speed filter ─────────────────────────────────
SPEED_REL_THRESHOLD=0.0002
SPEED_ACCEL_ENABLED=true

# ── BTC macro gate ──────────────────────────────────────
BTC_GATE_ENABLED=true
BTC_SYMBOL=BTC_USDT
BTC_TF=1h
BTC_KLINE_COUNT=300
BTC_RANGING_PCT=0.10

# ── Signal timeframe ────────────────────────────────────
SIGNAL_TF=15m
SIGNAL_KLINE_COUNT=300

# ── Risk model ──────────────────────────────────────────
SL_ATR_MULT=0.5
REWARD_RATIO=2.0
MIN_TP_ROI_PCT=8.0
MAX_SL_ROI_PCT=15.0
MIN_SL_ROI_PCT=2.0
LEVERAGE=20

# ── Scheduler & limits ─────────────────────────────────
SETUP_SCAN_CRON_MINUTES=*/15
SETUP_SCAN_CRON_HOURS=*
SIGNAL_COOLDOWN_MINUTES=60
MAX_DAILY_SIGNALS=10
MIN_DAILY_SIGNAL_GAP_MINUTES=6
MAX_CONCURRENT_SIGNALS=5
```

---

## 6. Files Changed

| File | Change |
|---|---|
| `strategy.py` | Add magnitude gate, acceleration gate, BTC cache + gate inside `scan_symbol()` |
| `config.py` | Add 7 new variables, update defaults for 9 existing variables |
| `.env` | Add all new variables with correct defaults |

No changes to: `main.py`, `bot.py`, `database.py`, `coin_scanner.py`, `mexc_client.py`, `reports.py`, `webui.py`.

---

## 7. Expected Signal Output

- 80 coins × 15m TF = 96 candle closes/day per coin
- DynEMA crossovers on 15m: ~2–6/day per coin (market-dependent)
- After speed magnitude + acceleration + BTC gate: ~15–20% pass rate
- With 60 min cooldown per coin: **8–14 signals/day** across pool ✓
- TP ROI target: **~10%** at 20× leverage ✓

---

## 8. Tuning Notes

- `SPEED_REL_THRESHOLD`: raise to `0.0003` if too many weak signals pass; lower to `0.0001` if signal count drops below 8/day
- `BTC_RANGING_PCT`: raise to `0.20` in choppy BTC markets to allow more signals through
- `SL_ATR_MULT`: raise to `0.6` if stops are too tight on volatile coins (adjust `MAX_SL_ROI_PCT` to `18`)
- `SPEED_ACCEL_ENABLED=false`: disable acceleration gate if signal count persistently < 6/day
- `BTC_GATE_ENABLED=false`: disable BTC gate for testing or during BTC-decoupled altseason
