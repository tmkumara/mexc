# Binocular Trend Confluence v1 — Design Spec

Date: 2026-07-27
Source: `D:\Downloads\2- Binocular Trend ( PAID ).txt` (TradingView Pine Script v5
indicator "Binocular" by Jib1979). This design **replaces** the current Simple
Supertrend Pullback v1 strategy (`strategy.py`). No pullback/EMA20-reclaim
behavior is preserved.

## Objective

Replace the active strategy with a strategy derived from the Binocular
indicator's actual trade-trigger logic:

- **15m** — Supply/Demand zone detection (structural confluence)
- **5m** — Chandelier-Exit direction + Price-Volume-Trend-vs-signal momentum +
  dual-RSI(25/55) regime, confirmed by a breakout-buffer close

Everything else in the Pine script (Settlement/StdDev bands, VWAP D/W/M, MA
ribbon 30/35/40/45/50-vs-60, MTF RSI table, MTF signal table, ZigZag, P/L
table, "wave" trend-channel bar coloring) is chart-only visual tooling with
no bearing on the BUY/SELL trigger and is **not ported**.

Risk/reward stays governed by the bot's existing leverage-adjusted ROI
framework (`TARGET_ROI_PCT` / `MAX_SL_ROI_PCT` / `MIN_RR`), not the Pine
script's raw TP1/TP2/TP3 range multiples — this keeps risk sizing consistent
and comparable to prior strategies in the dashboard/stats.

## Preparation (done before implementation starts)

1. Backup branch `backup/supertrend-pullback-v1` cut from current `main`
   HEAD, pushed to `origin`. Restores the retired strategy in full if ever
   needed; no old-strategy code is kept in the working tree.
2. Implementation happens on a new branch off `main`; `main` stays
   deployable/untouched until this is tested (main auto-deploys to the live
   server on push, per `CLAUDE.md`).

## Components retained as-is

`coin_scanner.py`, `mexc_client.py`, `market_data.py`, `candle_cache.py`,
`mexc_ws_client.py`, `ws_manager.py`, `database.py` (schema unchanged — same
columns used), `bot.py` (message template unchanged — reads generic
`Signal`/`STRATEGY_NAME` fields, no hardcoded old-strategy wording), `main.py`
(scheduler/scan loop unchanged — same `evaluate_symbol` signature),
`webui.py` (unchanged — reads `STRATEGY_NAME` and config values generically),
`scalper_v3_strategy.py` / `STRATEGY_NAME_V3` track (fully independent,
out of scope).

The BTC market-safety filter (`build_btc_context`, `_btc_filter_ok`,
`BtcContext`) is retained **verbatim** — it is independent of the
trend/entry trigger logic being replaced.

## Components removed from the active runtime

- `_detect_trend` (15m EMA200 + Supertrend trend gate)
- `_detect_pullback_and_confirmation` (5m EMA20 pullback/reclaim logic)
- `TREND_EMA_PERIOD`, `ENTRY_EMA_PERIOD`, `PULLBACK_LOOKBACK_BARS`,
  `MAX_EMA_DISTANCE_PCT`, `MAX_CONFIRMATION_CANDLE_ATR`,
  `MIN_VOLUME_MULTIPLIER`, `VOLUME_MA_PERIOD`,
  `TREND_SUPERTREND_ATR_PERIOD`/`MULTIPLIER`,
  `ENTRY_SUPERTREND_ATR_PERIOD`/`MULTIPLIER` (the 15m/5m Supertrend used by
  the old trend/entry gates — the new strategy uses Chandelier Exit
  instead, a related but distinct stop-line indicator, so these are
  replaced rather than reused)

`calculate_ema`, `calculate_rsi`, `calculate_atr` are kept (still used, RSI
now called with two different periods). `calculate_supertrend` is deleted
from `strategy.py` (no longer referenced by anything in the new pipeline).

## New strategy: indicators

**Zone timeframe (15m):**
- ATR(`ZONE_ATR_PERIOD`, default 50) — matches Pine's zone ATR
- Pivot high/low via centered lookback `ZONE_SWING_LENGTH` (default 10)

**Trigger timeframe (5m):**
- Chandelier Exit: `ta.highest(close, CHANDELIER_ATR_PERIOD) - CHANDELIER_MULTIPLIER * ATR`
  (long stop) / mirrored short stop, direction flips on close crossing the
  opposite prior stop — ported directly from the Pine `calculation()`
  function's `longStop`/`shortStop`/`dir` logic
- Price-Volume-Trend: `pvt[i] = pvt[i-1] + (close[i]-close[i-1])/close[i-1] * volume[i]`,
  smoothed by `PVT_SIGNAL_LENGTH` (default 21) SMA or EMA per `PVT_SIGNAL_TYPE`
- Dual RSI: `RSI_FAST_PERIOD` (default 25) vs `RSI_SLOW_PERIOD` (default 55)

Only completed candles are used — `closed_df = df.iloc[:-1].copy()`,
independently for 15m and 5m, same as today. All decisions read from
`closed_df`. Implemented with NumPy/pandas only, non-repainting, no
future-candle access.

Required new helper functions in `strategy.py`:
```python
calculate_chandelier_exit(df, atr_period, multiplier)  # returns long_stop, short_stop, direction (1/-1)
calculate_pvt(df)                                       # returns cumulative PVT series
calculate_pvt_signal(pvt, length, ma_type)               # SMA or EMA of PVT
find_pivot_highs(df, swing_length)                       # centered pivot detection
find_pivot_lows(df, swing_length)
build_zones(df, swing_length, atr, box_width)            # returns ordered list of active (non-BOS) zone dicts: {type, top, bottom, formed_at_index}
```

## Zone detection (15m)

Recomputed fresh from the fetched 15m history on every call — no persisted
state between scans, same approach the old strategy used for
EMA/Supertrend (recompute-from-scratch each cycle, not an incremental
indicator).

1. Find pivot highs/lows over the full closed 15m series using a centered
   `ZONE_SWING_LENGTH`-bar window (a bar is a pivot high if it's the max of
   the `2*ZONE_SWING_LENGTH+1`-bar window centered on it; symmetric for
   lows).
2. Each pivot high forms a **supply zone**: `top = pivot_price`,
   `bottom = top - ATR(50) * (ZONE_BOX_WIDTH / 10)` (Pine's box-width
   formula, default `ZONE_BOX_WIDTH=2.5`). Each pivot low forms a **demand
   zone**, mirrored.
3. Zones that overlap an existing zone within `2 * ATR(50)` of each other's
   midpoint are skipped (Pine's `f_check_overlapping`), keeping the zone
   list from getting cluttered.
4. A zone is invalidated ("BOS") once any later closed 15m candle closes
   through it (`close >= zone.top` for supply, `close <= zone.bottom` for
   demand) — matches the Pine script's `f_sd_to_bos`.
5. Only zones still active (not BOS'd) as of the latest closed 15m candle
   are candidates for the confluence filter. `ZONE_MAX_AGE_BARS` (default
   100) caps how far back a zone may have formed to still count — avoids
   anchoring to ancient structure.

## Trigger (5m, latest closed candle)

```python
dir = chandelier_direction(closed_5m)              # 1 or -1
pvt, pvt_signal = calculate_pvt(...), calculate_pvt_signal(...)
rsi_fast, rsi_slow = calculate_rsi(close, 25), calculate_rsi(close, 55)

long_candidate  = dir == 1  and pvt.iloc[-1] > pvt_signal.iloc[-1] and rsi_fast.iloc[-1] > rsi_slow.iloc[-1]
short_candidate = dir == -1 and pvt.iloc[-1] < pvt_signal.iloc[-1] and rsi_fast.iloc[-1] < rsi_slow.iloc[-1]
```

**Breakout confirmation (single-pass adaptation).** The Pine script is a
two-stage design: a BUY/SELL state arms an entry level
(`high * (1 + buffer)`), and a *separate later candle* must break that
level before the trade actually triggers — the retired `armed_setups`
two-phase workflow, which this bot's architecture deliberately does not
use (see `CLAUDE.md`). Since a candle can never close beyond its own high,
using the signal candle's own high as the buffer reference is impossible
to satisfy in a single pass. **Resolution:** the buffer reference is the
*previous* closed candle's high/low instead of the signal candle's own:

```python
# LONG
long_ok = long_candidate and close > prev_high * (1 + ENTRY_BUFFER_PCT)
# SHORT
short_ok = short_candidate and close < prev_low * (1 - ENTRY_BUFFER_PCT)
```

This preserves "the breakout is already confirmed by the time the candle
closes" without persisted arm state. Repeat-fire protection continues to
come from the existing `SIGNAL_COOLDOWN_MINUTES` (no re-implementation of
Pine's `buy`/`buy1`/`sell`/`sell1` transition-edge state tracking).

`ENTRY_BUFFER_PCT` defaults to `0.0002` (0.02%), matching the Pine script's
`bfr` default.

## Zone confluence filter

- LONG requires the 5m close to be at or inside (± `ZONE_PROXIMITY_ATR_MULT
  * ATR(50)` tolerance) the **most recent active 15m demand zone** that is
  at or below current price.
- SHORT requires the mirrored **active supply zone** at or above current
  price.
- No qualifying zone within `ZONE_MAX_AGE_BARS` → reject
  (`no_zone_confluence` reject bucket).

This is the "buy at demand / sell at supply" reading of the indicator: the
Chandelier/PVT/RSI trigger says a breakout is underway; the zone confirms
it's breaking out **from structure**, not chasing price into open air.

## Entry / TP / SL

```python
entry_price = latest_closed_5m_close
tp_price_pct = TARGET_ROI_PCT / 100 / LEVERAGE   # unchanged from today
```
LONG: `tp = entry * (1 + tp_price_pct)`; SHORT: `tp = entry * (1 - tp_price_pct)`.

Structural SL is sourced from the confluence zone's far boundary, buffered
by ATR — same shape as the old strategy's pullback-window stop, just fed
by the zone instead:

```python
# LONG: stop below the demand zone's bottom
structural_sl = demand_zone.bottom - ATR(50) * SL_ATR_BUFFER_MULTIPLIER
# SHORT: stop above the supply zone's top
structural_sl = supply_zone.top + ATR(50) * SL_ATR_BUFFER_MULTIPLIER
```

If the structural stop is farther than `MAX_SL_ROI_PCT/100/LEVERAGE` from
entry, reject the trade — never tighten the stop artificially to force a
signal (same rule as today).

RR validation: `rr = reward_distance / risk_distance`, reject if
`rr < MIN_RR`. Geometry validated via the existing `valid_trade_geometry`
(unchanged). No invalid geometry may reach the DB or Telegram.

## BTC market safety filter

Unchanged — retained verbatim from the current strategy, including all
defaults (`ENABLE_BTC_FILTER=True`, `BTC_FILTER_SYMBOL=BTC_USDT`,
`BTC_FILTER_TF=15m`, `BTC_MAX_OPPOSING_MOVE_PCT`,
`BTC_MAX_SINGLE_CANDLE_MOVE_PCT`, `BTC_MAX_THREE_CANDLE_MOVE_PCT`).

## `evaluate_symbol` signature (unchanged)

```python
def evaluate_symbol(symbol: str, btc_context: BtcContext | None = None, reject_sink: dict | None = None) -> Signal | None
```
Pipeline: fetch 15m/5m candles → drop forming candle → validate candle
count → build 15m zones → compute 5m Chandelier/PVT/RSI trigger + breakout
confirmation → zone confluence filter → BTC filter → compute TP/structural
SL from zone → validate max SL distance → validate RR → compute score →
return `Signal` or `None`. Same `Signal`/`BtcContext` dataclasses as today,
unchanged field-for-field.

## Candidate scoring (0–100)

Mirrors the shape of today's `_score_candidate` with new inputs:

- Zone proximity quality (25) — closer to zone midpoint scores higher,
  edge-of-zone scores lower
- Chandelier/PVT/RSI alignment strength (25) — magnitude of
  `pvt - pvt_signal` relative to its own recent range, and
  `rsi_fast - rsi_slow` spread
- Breakout quality (20) — how cleanly the close cleared
  `prev_high/low * (1 ± buffer)`, similar shape to today's EMA-reclaim
  quality term
- RSI regime quality (10) — reuse today's ideal-band shape (near but not
  extreme separation between fast/slow RSI)
- RR quality (10) — unchanged formula, `MIN_RR` = 0 score, `MIN_RR + 0.5` = full score
- Zone freshness (10) — newer zones (fewer bars since formation, within
  `ZONE_MAX_AGE_BARS`) score higher than older ones

## Reject-reason buckets

`_reason_bucket` gains new categories: `no_chandelier_alignment`,
`no_pvt_momentum`, `no_rsi_regime`, `no_breakout_confirmation`,
`no_zone_confluence`, `zone_stop_too_wide` (reusing existing
`stop_too_wide`/`rr_below_min`/`invalid_geometry`/`missing_data`/
`insufficient_history`/`btc_filter` buckets where the check is unchanged).

## Configuration (`config.py`)

Remove: `TREND_EMA_PERIOD`, `ENTRY_EMA_PERIOD`, `PULLBACK_LOOKBACK_BARS`,
`MAX_EMA_DISTANCE_PCT`, `MAX_CONFIRMATION_CANDLE_ATR`,
`MIN_VOLUME_MULTIPLIER`, `VOLUME_MA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`,
`TREND_SUPERTREND_MULTIPLIER`, `ENTRY_SUPERTREND_ATR_PERIOD`,
`ENTRY_SUPERTREND_MULTIPLIER`.

Add:
```python
CHANDELIER_ATR_PERIOD = int(os.getenv("CHANDELIER_ATR_PERIOD", "10"))
CHANDELIER_MULTIPLIER = float(os.getenv("CHANDELIER_MULTIPLIER", "2.2"))
PVT_SIGNAL_LENGTH = int(os.getenv("PVT_SIGNAL_LENGTH", "21"))
PVT_SIGNAL_TYPE = os.getenv("PVT_SIGNAL_TYPE", "SMA")           # "SMA" | "EMA"
RSI_FAST_PERIOD = int(os.getenv("RSI_FAST_PERIOD", "25"))
RSI_SLOW_PERIOD = int(os.getenv("RSI_SLOW_PERIOD", "55"))
ZONE_SWING_LENGTH = int(os.getenv("ZONE_SWING_LENGTH", "10"))
ZONE_ATR_PERIOD = int(os.getenv("ZONE_ATR_PERIOD", "50"))
ZONE_BOX_WIDTH = float(os.getenv("ZONE_BOX_WIDTH", "2.5"))
ZONE_PROXIMITY_ATR_MULT = float(os.getenv("ZONE_PROXIMITY_ATR_MULT", "0.5"))
ZONE_MAX_AGE_BARS = int(os.getenv("ZONE_MAX_AGE_BARS", "100"))
ENTRY_BUFFER_PCT = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))
```
`STRATEGY_NAME` default becomes `"Binocular Trend Confluence v1"`. All
other existing config (`LEVERAGE`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`,
`MIN_RR`, `SL_ATR_BUFFER_MULTIPLIER`, BTC filter constants,
`MAX_ACTIVE_LONG_SIGNALS`/`MAX_ACTIVE_SHORT_SIGNALS`,
`MAX_CONCURRENT_SIGNALS`, `MAX_DAILY_SIGNALS`, `SIGNAL_COOLDOWN_MINUTES`,
`SIGNAL_EXPIRE_HOURS`, coin pool settings, `DRY_RUN*`) is unchanged.

## Database / Telegram / Dashboard

No DB schema changes required — `Signal` dataclass is unchanged in shape.

`bot.py` and `webui.py` **do** need targeted edits, correcting an earlier
assumption in this spec: `bot.py:cmd_status` directly imports
`TREND_EMA_PERIOD, ENTRY_EMA_PERIOD, RSI_LONG_MIN, RSI_LONG_MAX,
RSI_SHORT_MIN, RSI_SHORT_MAX` from `config` and formats them into the
`/status` message (`bot.py:208-223,244-246`) — removing those config
constants without updating `bot.py` would raise `ImportError` the next
time `/status` runs. `webui.py:get_strategy_config()` (`webui.py:232-267`)
hardcodes the same old field names via `_safe_config_value` — that helper
degrades missing attributes to `"—"` rather than crashing, but the
dashboard would silently show stale/blank old-strategy fields forever.
Both need their old-strategy-specific lines replaced with Chandelier/PVT/
RSI/zone-relevant fields, the same way the prior Supertrend Pullback v1
migration rewrote both message templates when the underlying config
changed shape.

## Outcome checking

Unchanged — `main.py → check_outcomes` and `outcome_check.check_tp_sl`
operate purely on `entry/tp/sl` prices and 5m candles, independent of how
the strategy derived them.

## Testing

New `tests/test_binocular_indicators.py`: `test_chandelier_exit_bullish_direction`,
`test_chandelier_exit_bearish_direction`, `test_chandelier_exit_does_not_use_future_data`,
`test_pvt_accumulates_correctly`, `test_pvt_signal_sma`, `test_pvt_signal_ema`,
`test_pivot_high_detection`, `test_pivot_low_detection`,
`test_build_zones_creates_demand_and_supply`,
`test_zone_marked_bos_after_close_through`,
`test_overlapping_zones_are_skipped`.

New `tests/test_strategy_binocular.py`: long + short variants of
`test_*_signal_valid`, `test_*_rejected_without_chandelier_alignment`,
`test_*_rejected_without_pvt_momentum`, `test_*_rejected_without_rsi_regime`,
`test_*_rejected_without_breakout_confirmation`,
`test_*_rejected_without_zone_confluence`,
`test_*_rejected_when_stop_too_wide`, `test_*_rejected_when_rr_too_low`;
`test_active_last_candle_is_ignored`; `test_long_trade_geometry`,
`test_short_trade_geometry`, `test_invalid_geometry_rejected`.

`tests/test_btc_filter.py` is unaffected (BTC filter unchanged) — kept as
regression coverage.

**Legacy test cleanup:** `tests/test_strategy_supertrend_pullback.py` (if
it exists under that name) is **deleted** in this work, since it exercises
`_detect_trend`/`_detect_pullback_and_confirmation`, which no longer exist
— same "suite reflects only what's actually running" policy used in the
prior strategy migration. `tests/test_indicators.py` keeps
`calculate_ema`/`calculate_rsi`/`calculate_atr` coverage but drops
`calculate_supertrend` tests (function removed).

## Backtest utility

`scripts/backtest_simple_strategy.py` (existing, calls `evaluate_symbol`
directly) is mostly strategy-agnostic, but **does** need a small fix,
correcting another earlier assumption in this spec: it imports
`TREND_EMA_PERIOD, ENTRY_EMA_PERIOD, PULLBACK_LOOKBACK_BARS` at module
level (`scripts/backtest_simple_strategy.py:37`) and uses them to compute
`min_start`, the first index it's willing to evaluate
(`scripts/backtest_simple_strategy.py:256`) — removing those constants
without updating this file breaks it at import time. `min_start` must be
recomputed from the new strategy's minimum-history requirements
(`ZONE_ATR_PERIOD + ZONE_SWING_LENGTH * 2 + 10` for the 15m side,
`RSI_SLOW_PERIOD + 20` for the 5m side, combined with the same `max(...)`
the file already uses). Once fixed, re-run it against the new strategy to
sanity-check trade frequency/quality before enabling live/dry-run on the
server.

## Migration order (drives the implementation plan's phases)

1. **Backup** — cut `backup/supertrend-pullback-v1` branch, push.
2. **Indicators + tests** — Chandelier Exit, PVT/PVT-signal, pivot/zone
   builder helpers, unit tests, verify green.
3. **Strategy** — rewrite `strategy.py`'s trigger/confluence/TP-SL pipeline
   in `evaluate_symbol`, long/short tests, completed-candle handling. BTC
   filter code is moved as-is, not rewritten.
4. **Config** — remove old trend/pullback settings, add new Chandelier/
   PVT/RSI/zone settings, update `STRATEGY_NAME`, `.env.example`.
5. **Cleanup** — delete superseded tests, full suite green, run
   `scripts/backtest_simple_strategy.py` against a handful of symbols,
   dry-run smoke test (`DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py`).

## Acceptance criteria

- No `_detect_trend`/`_detect_pullback_and_confirmation`/
  `calculate_supertrend` references remain in `strategy.py`
- Zone detection and Chandelier/PVT/RSI trigger both operate only on
  completed candles (no forming-candle access)
- BTC filter behavior is unchanged (existing BTC filter tests still pass
  unmodified)
- Every accepted signal satisfies `valid_trade_geometry`, `rr >= MIN_RR`,
  and structural SL distance `<= MAX_SL_ROI_PCT/100/LEVERAGE`
- `evaluate_symbol` signature and `Signal`/`BtcContext` dataclasses are
  byte-for-byte unchanged from today
- `main.py`, `bot.py`, `webui.py`, `database.py` require no code changes
  (config/display values only)
- All tests pass; `scripts/backtest_simple_strategy.py` runs against the
  new strategy with no future-data leakage; dry-run boots cleanly and logs
  the new strategy name, zone/trigger config, target ROI, max SL ROI,
  leverage
- `backup/supertrend-pullback-v1` branch exists on `origin` and contains
  the full pre-migration `strategy.py`/`config.py`

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import database"
python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT --days 30
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py
```
Confirm startup logs show strategy name `Binocular Trend Confluence v1`,
zone/trigger config, target ROI, max SL ROI, leverage, dry-run enabled.
