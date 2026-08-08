# Precision Pullback Scalper v1 — Design Spec

Date: 2026-08-09
Source: `D:\Downloads\Finexa-KT\architecture.txt` (a ChatGPT-authored strategy
proposal, written against an older SMC-based `strategy.py` that no longer
exists in this repo — the live strategy today is Binocular Pending-Breakout
v1, commit `8cb9c46`). This spec follows architecture.txt's rules **strictly**
for the strategy logic itself (indicators, entry/exit thresholds, scoring,
TP/SL/breakeven), adapted only where the source doc is silent on
implementation detail (e.g. exact EMA200 slope lookback) or where it must be
mapped onto this codebase's existing modules instead of the from-scratch
`binocular_signal_bot/`-style project it originally assumed.

## Relationship to prior work

Per explicit user instruction, this is a **full replacement** of the live
strategy, done **directly on `main`** after a backup branch is cut (not on a
side branch — unlike the 2026-07-30 Binocular migration, which stayed
unmerged pending a backtest comparison). Safety here comes from `DRY_RUN`
staying `true` after the rewrite (already the default) and from the explicit
user decision to defer the actual 6-month backtest/walk-forward tuning to a
follow-up session — this implementation is build-and-verify-only (tests +
`py_compile` + a dry-run boot), not a live-signal-ready release.

Also removed in this same pass, per explicit user instruction: the dormant
"Super Scalper v3" alternate strategy (`super_scalper_v3.py`,
`scalper_v3_strategy.py`) and two other retired modules that only that track
or an even-older strategy used (`liq_estimator.py`, `nw_kernel.py`). After
this change there is exactly one strategy in the codebase.

## Objective

Replace `strategy.py`'s pipeline with architecture.txt's "Precision Pullback
Scalper v1": a dual-timeframe EMA-trend + pullback + RSI-reset + volume/ATR
confirmation model, fixed TP/SL, single breakeven step, 100-point scoring
gate. No structural/ATR-derived stops, no RR-based gate (fixed TP/SL means RR
is a constant 0.7:1 by construction — quality control is entirely the score
threshold instead).

### Pipeline

```
MEXC Coin Pool
   -> 15m EMA200 trend + slope filter
   -> 5m EMA20/EMA50 alignment + separation filter
   -> wait for pullback into EMA20/EMA50 zone (no-chase distance check)
   -> RSI(14) momentum-reset zone, then turning back
   -> bull/bear confirmation candle (body size + close position + volume)
   -> ATR% volatility band check
   -> score >= MIN_SIGNAL_SCORE (default 80)
   -> ARM PENDING SETUP (breakout-buffer entry above/below confirmation candle)
   -> confirming candle breaks the entry level -> FIRE
   -> TP = +7% ROI (fixed) / SL = -10% ROI (fixed)
   -> price reaches +4% ROI -> SL moves to breakeven
```

## Timeframes

- `TREND_TF` (new config, default `"15m"`): EMA200 trend/slope filter only.
- `ENTRY_TF` (existing config, default changes from `"15m"` to `"5m"`): EMA20/
  EMA50, RSI14, ATR14, volume-MA20, confirmation candle, pending-setup entry/
  SL/TP — everything else. Both fetched via the existing `market_data`/
  `mexc_client` stack (already timeframe-parametrized — no client changes
  needed). Only fully closed candles are used on either timeframe (existing
  `iloc[:-1]` convention, both here).

## New indicators (`strategy.py`)

```python
calculate_ema20(df) -> pd.Series   # calculate_ema(close, EMA_FAST_LEN=20)
calculate_ema50(df) -> pd.Series   # calculate_ema(close, EMA_SLOW_LEN=50)
calculate_ema200_trend(df) -> pd.Series  # calculate_ema(close, EMA_TREND_LEN=200), computed on TREND_TF candles
calculate_volume_ma(df, period) -> pd.Series  # simple rolling mean of volume
```
`calculate_ema` and `calculate_rsi` and `calculate_atr` already exist in
`strategy.py` (generic, reused as-is — `calculate_atr` already used
elsewhere, `tests/test_indicators.py` coverage is unaffected).
`calculate_ema_ribbon`, `calculate_pvt`, `calculate_pvt_signal`,
`calculate_chandelier_direction`, `calculate_ema200` (Binocular's, singular-
purpose, superseded by `calculate_ema200_trend` above), `calculate_daily_vwap`
are all deleted — nothing in the new pipeline uses ribbon, PVT, Chandelier,
or VWAP.

**EMA200 slope**: architecture.txt says "EMA200 slope > 0" without defining a
lookback. Resolved as: `ema200.iloc[-1] > ema200.iloc[-1 - EMA_TREND_SLOPE_LOOKBACK]`
(new config, default `5` bars on `TREND_TF` — 75 minutes of 15m candles,
enough to filter out single-bar noise without lagging a real trend change by
more than ~1.25 hours).

## Entry pipeline (`strategy.py`)

All checks below run against the latest **closed** `ENTRY_TF` candle, with
the `TREND_TF` checks computed once per symbol per scan (cached alongside the
`ENTRY_TF` fetch, not refetched per candle).

1. **Trend filter** (reject reason `no_trend_alignment`):
   - LONG: `TREND_TF close > ema200_trend` and slope check above passes
   - SHORT: mirrored (`<`, slope falling)
2. **5m alignment + strength** (reject `no_ema_alignment` / `weak_trend`):
   - LONG: `ema20 > ema50` and `(ema20 - ema50) / close >= EMA_SEPARATION_MIN_PCT` (default `0.05%`)
   - SHORT: mirrored
3. **EMA200 agreement across timeframes** (reject `no_ema200_agreement`, folds
   architecture.txt's item 5 in): `ENTRY_TF close` must be on the same side of
   its own `calculate_ema(close, EMA_TREND_LEN)` as the `TREND_TF` check in
   step 1 — i.e. both timeframes agree on trend direction, not just one.
4. **Pullback + no-chase** (reject `no_pullback` / `chasing_price`): resolves
   architecture.txt's two overlapping thresholds (a "preferred ~0.20%, reject
   beyond 0.35-0.40%" range in the Pullback section, and a separately
   emphasized "biggest win-rate improvement" 0.30% cap in the filters
   section) as: **hard reject** if `abs(close - ema20) / close > NO_CHASE_MAX_DISTANCE_PCT`
   (new config, default `0.30%` — the filters-section value, since
   architecture.txt calls it out as the higher-priority rule). Distance
   `<= PULLBACK_PREFERRED_DISTANCE_PCT` (new config, default `0.20%`) instead
   feeds the scoring function (item 4 of "Pullback" section) rather than
   gating pass/fail on its own.
5. **RSI reset** (reject `no_rsi_reset`):
   - LONG: RSI14 was in `[RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX]` (default
     `42-55`) during the pullback and `rsi.iloc[-1] > rsi.iloc[-2]` (turning up)
   - SHORT: RSI14 in `[RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX]` (default
     `45-58`) and turning down
   "During the pullback" = anywhere in the last `PULLBACK_LOOKBACK_BARS` (new
   config, default `5` `ENTRY_TF` candles) — mirrors `RIBBON_LOOKBACK_BARS`'s
   old "bounded backward search" shape from the retired ribbon-flip strategy,
   applied here to the RSI zone touch instead.
6. **Confirmation candle** (reject `no_confirmation_candle`), on the latest
   closed `ENTRY_TF` candle:
   - LONG: `close > open`, `close > ema20`, `close > prev_candle.high`,
     `volume > volume_ma20 * VOLUME_CONFIRM_MULT` (default `1.15`)
   - SHORT: mirrored (`close < open`, `< ema20`, `< prev_candle.low`)
   - **Abnormal-candle reject** (`abnormal_candle`, folds architecture.txt's
     "don't trade abnormal candles" filter in here since it's a property of
     the same confirmation candle): `abs(close - open) / open > MAX_CANDLE_BODY_PCT`
     (default `0.8%`) rejects regardless of direction.
7. **ATR% band** (reject `atr_out_of_band`): `ATR14 / close` must be in
   `[ATR_MIN_PCT, ATR_MAX_PCT]` (defaults `0.25%` / `1.20%`).
8. **Score** (see below) `< MIN_SIGNAL_SCORE` (default `80`) -> reject
   `score_below_min`.

## Scoring (0-100, `_score_candidate`)

Exact rubric from architecture.txt:

| Component | Points | Basis |
|---|---|---|
| 15m EMA200 trend | 20 | flat 20 if step 1 passes (binary — the filter already gates on this) |
| 5m EMA20/50 alignment | 15 | flat 15 if step 2 passes |
| EMA200 slope strength | 10 | scaled by `abs(ema200.iloc[-1] - ema200.iloc[-1-LOOKBACK]) / ema200` vs a reference range, capped at 10 |
| Good EMA pullback | 15 | scaled: full 15 at distance `<= PULLBACK_PREFERRED_DISTANCE_PCT`, linearly down to 0 at `NO_CHASE_MAX_DISTANCE_PCT` |
| RSI reset | 10 | flat 10 if step 5 passes (binary — zone + turn is already pass/fail) |
| Confirmation candle | 15 | scaled by how cleanly close beats the prior high/low and `open`, relative to the candle's own range |
| Volume confirmation | 10 | scaled by `volume / volume_ma20` ratio above the `1.15` floor, capped at 10 |
| Good ATR environment | 5 | flat 5 if step 7 passes |
| **Total** | **100** | |

Only candidates scoring `>= MIN_SIGNAL_SCORE` are armed; when multiple
symbols qualify in one scan, `scan_and_fire_signals` ranks by score
descending (existing pattern, unchanged).

## Pending-setup entry, fixed TP/SL, breakeven

Reuses the existing `armed_setups` table as-is — **no schema migration**.
Its shape (`entry_low/high`, `sl_price`, `tp_price`, `score`, `rr`,
`setup_reason`, `status`, `expires_at`, `fired_signal_id`) already covers a
single-target pending setup; `tp2_price`/`tp3_price`/`position_size`
(added for Binocular's 3-target ladder) are simply left `NULL` here — this
strategy has one target and no position-sizing display.

```python
# LONG
entry = confirmation_candle.high * (1 + ENTRY_BUFFER_PCT)   # existing var, default 0.0002 (0.02%) — same value architecture.txt specifies
sl    = entry * (1 - MAX_SL_PRICE_PCT)                         # MAX_SL_PRICE_PCT = MAX_SL_ROI_PCT/100/LEVERAGE = 0.0050 (0.50%)
tp    = entry * (1 + TP_PRICE_PCT)                             # TP_PRICE_PCT = TP_ROI_PCT/100/LEVERAGE = 0.0035 (0.35%)
# SHORT mirrored: entry = confirmation_candle.low * (1 - ENTRY_BUFFER_PCT); sl = entry*(1+MAX_SL_PRICE_PCT); tp = entry*(1-TP_PRICE_PCT)
```
`rr` stored on the setup is the fixed constant `TP_ROI_PCT / MAX_SL_ROI_PCT` (0.70)
for every setup — not a gate, just carried through for display parity with
the existing `armed_setups.rr` column and the Telegram message.

Setup confirmation each scan (same cadence as today, against the latest
closed `ENTRY_TF` candle only): LONG confirms when `high > entry`, SHORT when
`low < entry`; same-candle SL guard (if the confirming candle's `low <= sl`/
`high >= sl` is also true, treat as an instant stop, not a fill — existing
tie-break convention). Not confirmed after `PENDING_SIGNAL_EXPIRY_CANDLES`
(new default `3`, i.e. `3 x 5m = 15 minutes` per architecture.txt) ->
expired. No "cancel on opposite signal" rule in architecture.txt — omitted
(setups simply expire on their own timer if unconfirmed).

**Breakeven**: new `BREAKEVEN_TRIGGER_ROI_PCT` (default `4.0`) ->
`BREAKEVEN_TRIGGER_PRICE_PCT = BREAKEVEN_TRIGGER_ROI_PCT/100/LEVERAGE` (0.20%).
Handled in outcome tracking (below), not at fire time — entry/SL/TP stored on
the `signals` row are the fixed values above; breakeven only moves the
*effective* stop while the trade is open.

## Outcome tracking: `outcome_check.check_tp_sl_with_breakeven`

New function alongside (not replacing) `check_tp_sl` and `check_target_ladder`
— both stay, since deleting them would touch nothing that still needs them,
but they're also no longer called by any live path once this ships (dead
code note added to their docstrings rather than deleting, since e.g.
`check_target_ladder` has real, tested, ladder-replay logic that's a
reasonable reference implementation to keep around — flagged in "Open
questions" below for the user to confirm).

```python
def check_tp_sl_with_breakeven(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    breakeven_trigger_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff. current_sl starts at
    sl_price. Each candle, in order (mirrors check_target_ladder's
    same-candle tie-break shape):
      1. current_sl hit (low <= current_sl for LONG / high >= current_sl for
         SHORT) -> close. status "loss" if current_sl == sl_price (breakeven
         never triggered), else "breakeven" (current_sl == entry_price).
      2. tp_price hit -> status "win".
      3. else, if breakeven not yet triggered and breakeven_trigger_price
         reached favorably -> current_sl = entry_price, record
         breakeven_triggered_at = ts (checked AFTER 1-2 so a candle that
         reaches the trigger and reverses to the original SL in the same
         candle is still a full loss, not a breakeven -- conservative,
         matches the existing SL-first philosophy elsewhere).

    Returns None while open, else:
    {"status": "win"|"loss"|"breakeven", "pnl_roi_pct": float,
     "breakeven_triggered_at": Timestamp|None, "closed_at": Timestamp}
    pnl_roi_pct is the raw price-move percent (not leverage-scaled -- caller
    applies LEVERAGE, matching check_tp_sl's existing convention). A
    "breakeven" close realizes ~0% (entry_price vs entry_price, i.e. exactly
    0.0 -- fees/slippage are not modelled here, matching how the rest of the
    bot ignores them at signal-generation time per config.py's
    ESTIMATED_*_FEE_PCT being informational-only elsewhere too).
    """
```

**New `signals.status` value: `"breakeven"`**, alongside existing
`pending/win/loss/expired`. This is a deliberate three-way outcome per
architecture.txt's own framing ("WIN +7% / BREAKEVEN ~0% / LOSS -10%" as
three distinct buckets, not two) — see "Fixes required outside strategy.py"
for every place that must learn about it.

## `evaluate_symbol` replaced by two functions (same shape as Binocular's split)

```python
def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None
    # fetch TREND_TF + ENTRY_TF -> drop forming candles -> settle-age check
    # -> steps 1-8 above -> returns the setup dict (matches save_armed_setup's
    # shape) or None.

def check_setup_confirmation(setup: dict) -> tuple[str, float | None]
    # ("confirmed", fill_price) | ("expired", None) | ("waiting", None)
    # no "invalidated" outcome for this strategy (no cancel-on-opposite rule)
```

## Removed from the codebase

**`strategy.py`**: every Binocular-era function — `calculate_pvt`,
`calculate_pvt_signal`, `calculate_chandelier_direction`, `calculate_ema200`,
`calculate_ema_ribbon`, `calculate_daily_vwap`, `mtf_signal`, `position_size`,
the old `_build_pending_setup`/`detect_pending_setup`/
`check_setup_confirmation` bodies (rewritten above).

**Files deleted entirely**: `super_scalper_v3.py`, `scalper_v3_strategy.py`,
`liq_estimator.py`, `nw_kernel.py`.

**`config.py`** removed: `RIBBON_MA1_LEN..RIBBON_MA5_LEN`, `RIBBON_BASELINE_LEN`,
`SIGNAL_MODE`, `CONFIRMATION_TIMEFRAMES`, `MTF_MIN_CONFIRMATIONS`,
`ACCOUNT_BALANCE`, `RISK_PERCENT_PER_TRADE`, `PVT_SIGNAL_TYPE`,
`PVT_SIGNAL_LENGTH`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`,
`CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `BINOCULAR_EMA200_LEN`,
`TARGET1_CLOSE_FRACTION`, `TARGET2_CLOSE_FRACTION`, `TARGET3_CLOSE_FRACTION`,
`MOVE_SL_TO_BREAKEVEN_AFTER_T1`, `MIN_RR` (no longer a gate — RR is a fixed
constant now, see above), and every `SCALPER_V3_*` constant plus
`STRATEGY_NAME_V3`, `SCALPER_V3_ENABLED`. `STRATEGY_V1_ENABLED` also removed
— with only one strategy left, the scanner job is unconditionally scheduled
(no flag needed; matches the Binocular spec's own reasoning for why it
rejected a second flag, applied one step further here since there's no
"other" strategy left for any flag to distinguish from).

**`tests/`** deleted: `test_scalper_v3_strategy.py`, `test_super_scalper_v3.py`,
`test_binocular_indicators.py`, `test_strategy_binocular_pending.py`,
`test_outcome_target_ladder.py` (all logic/functions they cover are gone or
superseded — see "Testing" below for replacements).

## Retained as-is

`coin_scanner.py`, `mexc_client.py`, `market_data.py`, `candle_cache.py`,
`mexc_ws_client.py`, `ws_manager.py`, `reports.py` (aggregation logic changes,
see below, but the module/report-formatting shape stays), `clear_db.py`,
`backtest/engine.py` (independent, per the Binocular spec's own
cross-reference — still untouched), `database.py`'s `signals`/`armed_setups`
table *schemas* (no migration — new `status="breakeven"` value needs no DDL
change, it's just a string), `calculate_ema`/`calculate_rsi`/`calculate_atr`
in `strategy.py`, `valid_trade_geometry`/`direction_slot_available`.

## Fixes required outside `strategy.py`

- **`main.py`**: `scan_and_fire_signals` rewritten for the new pending-setup
  pipeline (process existing armed setups: confirm/expire; then scan the
  pool for new setups — same two-phase shape as Binocular, minus the
  invalidate-on-opposite-signal branch). `scan_and_fire_signals_v3` /
  `check_outcomes_v3` and the `SCALPER_V3_ENABLED` / `STRATEGY_V1_ENABLED`
  branches deleted outright — `main()` schedules exactly one scanner job and
  one outcome-check job unconditionally. `check_outcomes` calls
  `check_tp_sl_with_breakeven` instead of `check_tp_sl`; on a `"breakeven"`
  result, saves `status="breakeven"` and a `breakeven_triggered_at`
  timestamp (existing column, reused) instead of `win`/`loss`. Startup log
  line lists the new config constants (see below) instead of the removed
  Binocular/v3 ones.
- **`database.py`**: no schema change. `update_signal_outcome` already takes
  an arbitrary `status` string — confirm it has no `CHECK` constraint or
  hardcoded `win`/`loss` assumption that would reject `"breakeven"` (if it
  does, widen it there).
- **`reports.py`**: `wins`/`losses` filters gain a third `breakeven` filter.
  `win_rate = len(wins) / (len(wins) + len(losses)) * 100` (breakeven
  **excluded** from this ratio — it's neither a directional win nor loss,
  and diluting the win-rate stat with it would misrepresent the "70-90%
  target win rate" acceptance bar from architecture.txt, which is a pure
  win-vs-loss ratio). `closed = wins + losses + breakevens` for total-trade
  counts. `net_roi` sums over all three (a breakeven trade's ~0% pnl_roi
  still belongs in the expectancy sum). Report text gains a `⚖️ Breakeven:`
  line alongside the existing win/loss counts.
- **`webui.py`**: same three-way split as `reports.py` in its stats
  aggregation (`webui.py:169-178`). `get_strategy_config()` (`webui.py:232-263`)
  rewritten: drops every Binocular/v3 key, adds `trend_tf`, `entry_tf`,
  `min_signal_score`, `tp_roi_pct`, `sl_roi_pct`, `breakeven_trigger_roi_pct`,
  `no_chase_max_distance_pct`, `atr_min_pct`/`atr_max_pct`. Dashboard inline
  JS (the fields it renders from this payload) updated in the same change —
  same "Python and JS together" rule the Binocular spec called out.
- **`bot.py`**: `format_signal` (`bot.py:62`) simplified for single-TP (no
  more `if signal.tp2_price is not None` ladder branch needed, though
  leaving the guard harmless is fine since those columns are just always
  `None` now — no behavior change either way, simplest to leave it). SL/TP
  lines show the fixed `TP_ROI_PCT`/`SL_ROI_PCT` instead of
  `SCALPER_V3_MAX_SL_ROI_PCT` (`bot.py:129`). `notify_outcome`
  (`bot.py:171`) gains a `"breakeven"` status branch — e.g. "⚖️ BREAKEVEN
  STOP ~0%" — alongside its existing win/loss labels; the `final_stage`-based
  ladder labels (`bot.py:178-186`) are deleted (no ladder anymore — one
  status, one label per outcome). `cmd_status` (`bot.py:241`) config
  import/display block (`bot.py:247-282`) swaps every removed constant for
  the new ones (trend/entry TF, min score, fixed TP/SL%, breakeven trigger,
  no-chase distance, ATR band).

## Configuration (`config.py`)

```python
STRATEGY_NAME default -> "Precision Pullback Scalper v1"
# STRATEGY_V1_ENABLED removed entirely (see "Removed from the codebase")

TREND_TF: str = os.getenv("TREND_TF", "15m")
ENTRY_TF: str = os.getenv("ENTRY_TF", "5m")   # default changes from "15m"

EMA_FAST_LEN: int = int(os.getenv("EMA_FAST_LEN", "20"))
EMA_SLOW_LEN: int = int(os.getenv("EMA_SLOW_LEN", "50"))
EMA_TREND_LEN: int = int(os.getenv("EMA_TREND_LEN", "200"))
EMA_TREND_SLOPE_LOOKBACK: int = int(os.getenv("EMA_TREND_SLOPE_LOOKBACK", "5"))
EMA_SEPARATION_MIN_PCT: float = float(os.getenv("EMA_SEPARATION_MIN_PCT", "0.05")) / 100.0

RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_RESET_MIN: float = float(os.getenv("RSI_LONG_RESET_MIN", "42"))
RSI_LONG_RESET_MAX: float = float(os.getenv("RSI_LONG_RESET_MAX", "55"))
RSI_SHORT_RESET_MIN: float = float(os.getenv("RSI_SHORT_RESET_MIN", "45"))
RSI_SHORT_RESET_MAX: float = float(os.getenv("RSI_SHORT_RESET_MAX", "58"))
PULLBACK_LOOKBACK_BARS: int = int(os.getenv("PULLBACK_LOOKBACK_BARS", "5"))

PULLBACK_PREFERRED_DISTANCE_PCT: float = float(os.getenv("PULLBACK_PREFERRED_DISTANCE_PCT", "0.20")) / 100.0
NO_CHASE_MAX_DISTANCE_PCT: float = float(os.getenv("NO_CHASE_MAX_DISTANCE_PCT", "0.30")) / 100.0

VOLUME_MA_PERIOD: int = int(os.getenv("VOLUME_MA_PERIOD", "20"))
VOLUME_CONFIRM_MULT: float = float(os.getenv("VOLUME_CONFIRM_MULT", "1.15"))
MAX_CANDLE_BODY_PCT: float = float(os.getenv("MAX_CANDLE_BODY_PCT", "0.8")) / 100.0

ATR_MIN_PCT: float = float(os.getenv("ATR_MIN_PCT", "0.25")) / 100.0
ATR_MAX_PCT: float = float(os.getenv("ATR_MAX_PCT", "1.20")) / 100.0

MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # unchanged, reused
PENDING_SIGNAL_EXPIRY_CANDLES: int = int(os.getenv("PENDING_SIGNAL_EXPIRY_CANDLES", "3"))  # default changes from 5

LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))   # unchanged
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))
MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "10.0"))   # existing var/default, unchanged -- SL is fixed exactly at this value for this strategy, so "max" and "fixed" coincide
TP_PRICE_PCT: float = TP_ROI_PCT / 100.0 / LEVERAGE
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE   # existing var/formula, unchanged

BREAKEVEN_TRIGGER_ROI_PCT: float = float(os.getenv("BREAKEVEN_TRIGGER_ROI_PCT", "4.0"))
BREAKEVEN_TRIGGER_PRICE_PCT: float = BREAKEVEN_TRIGGER_ROI_PCT / 100.0 / LEVERAGE

# Unchanged (reused as-is): MAX_DAILY_SIGNALS=3, MIN_DAILY_SIGNAL_GAP_MINUTES,
# MAX_CONCURRENT_SIGNALS=2, MAX_ACTIVE_LONG_SIGNALS=1, MAX_ACTIVE_SHORT_SIGNALS=1,
# SIGNAL_COOLDOWN_MINUTES=240, SIGNAL_EXPIRE_HOURS, ENABLE_LONG_SIGNALS,
# MIN_CANDLE_SETTLE_SECONDS, ATR_PERIOD (superseded in meaning by RSI_PERIOD's
# sibling but kept since calculate_atr's signature still takes a period arg —
# ATR_PERIOD and the new ATR_MIN_PCT/MAX_PCT band are independent: one sizes
# the indicator window (14, unchanged), the other gates on its output).
```

## Testing

New `tests/test_precision_pullback_indicators.py`: EMA20/50/200 + slope
lookback, volume MA, RSI reset-zone detection (both directions, including
the "was in zone within lookback, now turning" bounded-search shape),
confirmation-candle body/close/volume checks, ATR% band, no-chase/pullback
distance scoring.

New `tests/test_strategy_precision_pullback.py`: long + short
`test_pending_setup_created_on_full_pipeline_pass`,
`test_rejected_when_trend_disagrees_across_timeframes`,
`test_rejected_when_chasing_price`,
`test_rejected_when_rsi_not_in_reset_zone`,
`test_rejected_when_candle_body_too_large`,
`test_rejected_when_atr_out_of_band`,
`test_rejected_when_score_below_minimum`,
`test_setup_confirms_on_entry_breakout`,
`test_setup_expires_after_n_candles`,
`test_same_candle_sl_blocks_confirmation`.

New `tests/test_outcome_check_breakeven.py`:
`test_tp_hit_is_a_win`, `test_sl_hit_before_breakeven_is_a_full_loss`,
`test_breakeven_trigger_then_stop_is_breakeven_not_loss`,
`test_breakeven_trigger_then_tp_is_still_a_win`,
`test_same_candle_original_sl_beats_breakeven_trigger` (the conservative
same-candle ordering rule above).

**Legacy cleanup**: delete the five test files listed under "Removed from
the codebase". `tests/strategy_fixtures.py`: remove Binocular/ladder-only
fixture builders no longer used, add new builders for this strategy's
setup/outcome shapes. `tests/test_indicators.py`, `tests/test_outcome_check.py`
(the `check_tp_sl`/`check_target_ladder` tests already there stay green
since neither function is deleted — see "Open questions"), `tests/test_mexc_client.py`,
`tests/test_backtest_engine.py`, `tests/test_correlation_limits.py`,
`tests/test_database_binocular_columns.py` (**delete** — Binocular-named,
covers columns that no longer have a live producer; the columns themselves
stay in the DB schema per "no migration" above, just untested since nothing
writes them now), `tests/test_database_direction_counts.py`,
`tests/test_bot_formatting.py` (updated for the new `format_signal`/
`notify_outcome` shapes, not deleted) unaffected otherwise.

## Backtest harness (build only — no 6-month run in this pass)

`scripts/backtest_simple_strategy.py` rewritten for this strategy's
create-now/confirm-later shape (same structural need the Binocular spec
identified — a single `evaluate_symbol` call per bar doesn't fit a pending
setup that confirms on a later bar):

```
for i in range(min_start, len(df) - 1):
    if in an open confirmed trade: replay check_tp_sl_with_breakeven forward, skip ahead past its close
    elif an armed pending setup exists: check confirmation/expiry against bar i
    else: try detect_pending_setup(symbol) as-of bar i; if found, hold it and keep scanning
```
`min_start = max(EMA_TREND_LEN on TREND_TF resampled to ENTRY_TF bar count, RSI_PERIOD, VOLUME_MA_PERIOD, ATR_PERIOD) + 10`.
Needs both `TREND_TF` and `ENTRY_TF` historical data — `backtest/fetch_data.py`
already supports arbitrary `--interval`, run once for each. `BacktestStats`/
`Trade` gain a `breakeven` outcome bucket alongside win/loss (matching the
live three-way status). This backtest is **not run** as part of this
implementation pass — building the harness and getting it importable/
unit-testable is in scope; fetching 6 months of real data (server-side,
per `fetch_data.py`'s own egress note) and the walk-forward tuning
architecture.txt describes is explicitly deferred to a follow-up.

## Migration order

1. **Backup** — cut `backup/main-pre-precision-pullback-scalper-v1` from
   current `main` HEAD, push to `origin`.
2. **Indicators + tests** — new EMA/volume helpers, `tests/test_precision_pullback_indicators.py`, green.
3. **Config** — remove every Binocular/v3/ribbon constant, add the new ones listed above.
4. **Strategy pipeline** — `detect_pending_setup`, `check_setup_confirmation`, `tests/test_strategy_precision_pullback.py`, green.
5. **Outcome tracking** — `outcome_check.check_tp_sl_with_breakeven`, `tests/test_outcome_check_breakeven.py`, green.
6. **Dependents** — `main.py` (single scanner + outcome job, breakeven status wiring), `database.py` (verify `update_signal_outcome` accepts the new status string), `reports.py`, `bot.py`, `webui.py` (Python **and** JS).
7. **Deletions** — `super_scalper_v3.py`, `scalper_v3_strategy.py`, `liq_estimator.py`, `nw_kernel.py`, the five superseded test files, and every config constant listed under "Removed from the codebase".
8. **Backtest harness** — rewrite `scripts/backtest_simple_strategy.py` for the new pipeline; confirm it imports and runs against whatever small local sample data is available (not a real 6-month run).
9. **Verify** — full test suite green, `py_compile` on every changed/new module, local `DRY_RUN=true` boot check (see "Final verification commands").

All of the above lands as commits directly on `main` (per explicit user
instruction — no feature branch this time), after step 1's backup branch is
pushed.

## Open questions (flagged, not blocking — pick defaults below unless told otherwise)

1. **Should `check_tp_sl` and `check_target_ladder` (Binocular's, now
   uncalled by any live path) be deleted along with the rest of the
   Binocular code, or kept as dead-but-tested reference code?** Default:
   **delete both**, plus their existing tests in `tests/test_outcome_check.py`
   that only exercise `check_target_ladder` (the plain `check_tp_sl` tests,
   if any test only that function generically, can stay since it's simple
   enough to keep as a general-purpose utility — but if nothing calls it
   either, remove it too for consistency with "exactly one strategy" from
   the Objective). Resolved this way for the plan unless you'd rather keep
   them.
2. **`ATR_PERIOD` vs `RSI_PERIOD`/new `*_LEN` naming split** — kept
   `ATR_PERIOD` (existing name) rather than renaming to `ATR_LEN` for
   consistency with the new `EMA_FAST_LEN` style, since renaming it would
   also require touching `SL_FLOOR_ATR_MULT`-style call sites that don't
   exist anymore anyway (removed with Binocular) — low-risk either way, kept
   as-is to minimize unrelated churn.

## Acceptance criteria

- No references to `calculate_pvt`/`calculate_chandelier_direction`/
  `calculate_ema_ribbon`/`calculate_daily_vwap`/`mtf_signal`/`position_size`/
  `RIBBON_*`/`SIGNAL_MODE`/`CHANDELIER_*`/`PVT_*`/`SCALPER_V3_*` remain
  anywhere in the non-test codebase
- `super_scalper_v3.py`, `scalper_v3_strategy.py`, `liq_estimator.py`,
  `nw_kernel.py` deleted
- Every confirmed setup satisfies `valid_trade_geometry` and fixed
  `SL_ROI_PCT`/`TP_ROI_PCT` at `LEVERAGE`x
- `check_tp_sl_with_breakeven` never returns `"loss"` after a breakeven
  trigger has moved the stop to entry; a same-candle original-SL hit always
  wins the tie-break over a same-candle breakeven trigger
- `signals.status = "breakeven"` is handled everywhere `win`/`loss` are
  handled today: `reports.py` (excluded from win_rate ratio, included in
  net_roi and closed-trade counts), `webui.py` dashboard stats, `bot.py`
  `notify_outcome`
- `main.py` schedules exactly one scanner job and one outcome job — no
  `SCALPER_V3_ENABLED`/`STRATEGY_V1_ENABLED` branching remains
- All tests pass; `py_compile` clean on every changed file
- `backup/main-pre-precision-pullback-scalper-v1` exists on `origin`
- Changes land as commits directly on `main` (no unmerged feature branch at
  the end of this pass)
- The actual 6-month backtest run and walk-forward parameter tuning are
  explicitly **not** part of this pass's deliverables

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import webui; import database; import outcome_check"
python -m py_compile config.py database.py strategy.py main.py bot.py webui.py outcome_check.py scripts/backtest_simple_strategy.py
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py
```
Confirm startup logs show strategy name `Precision Pullback Scalper v1`,
trend/entry timeframes (15m/5m), min signal score, fixed TP/SL ROI%,
breakeven trigger, leverage, dry-run enabled.
