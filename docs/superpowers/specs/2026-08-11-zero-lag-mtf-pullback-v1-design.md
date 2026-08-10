# Zero-Lag MTF Pullback v1 — Design Spec

Date: 2026-08-11
Source: `D:\Downloads\Finexa-KT\architecture.txt` (a ChatGPT-authored strategy
proposal). The doc assumes the *current* `strategy.py` is an older
"Stateful SMC Sweep + OB Retest" pipeline with a `pending_setups` table
holding `sweep_type`/`ob_type` columns — that strategy does not exist
anywhere in this repo's git history. The live strategy today is **Precision
Pullback Scalper v1** (commit `046fcf8` onward; `armed_setups` table). This
spec follows architecture.txt's rules **strictly** for the new strategy's
math and pipeline shape, but maps its "what to rename/replace" instructions
onto this codebase's actual current files instead of the SMC baseline it
assumed.

## Relationship to prior work

Per explicit user instruction, this is a **full replacement** of the live
strategy: `strategy.py`, `config.py`, `database.py`'s setup-tracking table,
`main.py`, `outcome_check.py`, and the `bot.py`/`webui.py` display layers all
get rewritten in place on `main`, after a backup branch/tag is cut (matching
this repo's established `backup/main-pre-<strategy>` convention — see
`backup/main-pre-precision-pullback-scalper-v1` etc.). Unlike the Precision
Pullback pass (which deferred its 6-month backtest to a follow-up), **this
pass also rewrites the backtest harness**, per explicit user instruction —
though the actual 6-month data run and walk-forward tuning are still a
follow-up (running for hours against real MEXC history is not something to
do inside an implementation pass).

Decisions made during brainstorming that resolve the source doc's own
internal contradictions/gaps, confirmed with the user directly:

1. **Signal frequency**: doc's config table says `MAX_DAILY_SIGNALS = 3`,
   its closing note says "up to 10+ signals per day." Resolved: **10+/day**.
   `MAX_DAILY_SIGNALS = 12`, `MAX_CONCURRENT_SIGNALS = 4` (new starting
   values, user-approved; existing `MAX_ACTIVE_LONG_SIGNALS`/
   `MAX_ACTIVE_SHORT_SIGNALS` correlation caps raised to `2` each so higher
   `MAX_CONCURRENT_SIGNALS` isn't bottlenecked by a `1`-per-direction cap).
2. **Setup-tracking table**: doc proposes a `pending_setups` table with
   zero-lag-specific columns; current code has `armed_setups` (same concept,
   Precision-Pullback-specific columns). Resolved: **drop `armed_setups`,
   add `pending_setups`** matching the doc's schema (plus one practical
   addition — see "Database schema" below) — literal doc schema per user's
   explicit choice, not a column-repurposing of the old table.
3. **Scheduler**: doc's step 22 proposes splitting today's single combined
   5-minute job into `scan_for_setups` (5m) + `monitor_setups` (1m).
   Resolved: **split, per user's explicit choice** — see "Scheduler" below
   for why this doesn't reintroduce the candle-settle bug documented in
   `CLAUDE.md`.
4. **Breakeven**: doc is explicit that v1 has no breakeven step (a deliberate
   experimental-control decision — v2 tests it later, in isolation).
   Resolved: **no breakeven in this strategy version** —
   `outcome_check.check_tp_sl_with_breakeven` is deleted (git history is the
   recovery path), replaced by a new plain `check_tp_sl`.

## Objective

Replace `strategy.py`'s pipeline with architecture.txt's "Zero-Lag MTF
Pullback v1": a four-timeframe (4h/1h/15m/5m) trend-alignment + pullback +
zero-lag-crossover-confirmation model, fixed TP/SL, no breakeven, no
structural stops, no RSI/EMA20-50-200/ATR-band/volume filters at all — the
experiment is deliberately narrow (per doc §5): does the zero-lag indicator
itself have an edge, isolated from every filter the previous strategies
stacked on top of it.

### Pipeline

```
MEXC Coin Pool
   -> 4H Zero-Lag trend state (stateful: flips only on a cross of ZLEMA +/- band)
   -> 1H Zero-Lag trend state must agree with 4H
   -> 15m pullback: price returns toward the 1H(*) ZLEMA  [see note below]
   -> ARM PENDING SETUP (macro/trend recorded, pullback recorded)
   -> 5m Zero-Lag crossover (close crosses ZLEMA) + directional confirmation candle
   -> record confirmation candle's high/low, compute trigger_price
   -> price breaks trigger_price -> FIRE
   -> TP = +7% ROI (fixed) / SL = -10% ROI (fixed), 20x leverage
   -> Outcome checker: plain TP/SL walk, no breakeven
```

(*) Doc §12 says "15m price returns toward ZLEMA" without saying whose
ZLEMA — the pullback timeframe's own, or the trend timeframe's. Resolved:
**the 15m candle's own ZLEMA** (computed on 15m closes, length 70) — a pullback
is inherently a pullback *on the timeframe being watched*; comparing a 15m
close against a 1H indicator value would be a cross-timeframe distance check
architecture.txt never describes elsewhere. `zlema_15m` in the DB schema
reflects this.

## Timeframes

New config (none of these existed before):
- `MACRO_TF = "4h"` — trend state gate only (direction must be `+1` for LONG, `-1` for SHORT).
- `TREND_TF = "1h"` — must agree with `MACRO_TF`'s state (existing config name, value changes from `"15m"`).
- `PULLBACK_TF = "15m"` — pullback-toward-ZLEMA detection, arms the pending setup.
- `ENTRY_TF = "5m"` — crossover + confirmation candle + breakout trigger (existing config name, unchanged value).

All four fetched via the existing `market_data`/`mexc_client` stack — already
timeframe-parametrized (`MEXC_INTERVAL_MAP` already has `Min60`/`Hour4`), no
client changes needed. Only fully closed candles are used on any timeframe
(existing `iloc[:-1]` convention, all four).

## New indicators (`strategy.py`)

```python
def calculate_zlema(series: pd.Series, length: int) -> pd.Series:
    """Zero-lag EMA: lag = floor((length-1)/2); adjusted = 2*close - close.shift(lag);
    ZLEMA = EMA(adjusted, length). Exact port of the Pine calculation in
    architecture.txt §7 -- the shift creates NaN for the first `lag` bars,
    which is fine since callers already require far more warmup than that."""

def calculate_zlema_band(df: pd.DataFrame, zlema: pd.Series, atr_period: int,
                          atr_lookback: int, multiplier: float) -> tuple[pd.Series, pd.Series]:
    """upper = zlema + volatility, lower = zlema - volatility, where
    volatility = rolling(atr_period-ATR, window=atr_lookback).max() * multiplier
    -- architecture.txt §8's 'highest ATR from last 210 candles'. Reuses the
    existing calculate_atr(df, ATR_PERIOD)."""

def calculate_zlema_trend_state(df: pd.DataFrame, zlema: pd.Series,
                                 upper: pd.Series, lower: pd.Series) -> pd.Series:
    """THE critical function per architecture.txt §9: trend is NOT
    close-vs-zlema. It's a stateful walk -- trend flips to +1 only on a
    cross ABOVE upper, to -1 only on a cross BELOW lower, and otherwise
    holds its previous value (starts at 0/neutral until the first cross).
    Must be computed by walking bar-by-bar in order (same shape as this
    file's existing calculate_supertrend, which already does a stateful
    walk for the same reason) -- not vectorizable as a simple comparison."""
```

`calculate_atr` is reused as-is (already generic, already used elsewhere).
Every Precision-Pullback-only indicator is deleted: `calculate_ema`,
`calculate_rsi`, `calculate_volume_ma`, `_ema_trend_slope_up`,
`_rsi_reset_ok`, `_confirmation_candle_ok` (Precision's volume/EMA20/prior-
high-low version — a *new*, differently-shaped confirmation-candle check is
added, see below), `_abnormal_candle`, `_atr_pct_ok`, `_score_pending_setup`
(Precision's rubric — new rubric below). `calculate_supertrend` is also
deleted — nothing in this pipeline uses Supertrend; it was already dead
code carried over from an earlier strategy (not referenced by
`detect_pending_setup`/`check_setup_confirmation` in the current file) and
this pass is the natural point to remove it.

## State machine (resolved ambiguity — see "Objective" pipeline above)

architecture.txt's prose describes three sequential conditions (§10-11 macro/
trend, §12 15m pullback "creates the pending setup", §15 5m crossover "once
a pending LONG exists, we wait for...", §16 breakout trigger) without fully
specifying whether the 5m crossover is checked *at* pending-setup-creation
time or as a *separate later* stage. Resolved as a **three-state machine**,
because collapsing it to two states (like Precision Pullback's "evaluate
everything in one shot, arm once) would mean re-fetching and re-evaluating
5m data speculatively every 5-minute scan even when the pullback hasn't
resolved into a crossover yet — the doc's own architecture explicitly wants
the 5m crossover treated as a distinct, faster-cadence event (§22's job
split):

```
pending_pullback   -- armed by scan_for_new_setups (5m) once 4H/1H trend
                       agree and 15m price has pulled back to its own ZLEMA.
                       Records macro_trend, trend_state, zlema_1h, zlema_15m,
                       pullback_price, pullback_time.
        |  monitor_pending_setups (1m) checks for the 5m ZLEMA crossover +
        |  confirmation candle (doc §15) each run.
        v
pending_breakout   -- crossover found. Records confirmation_high,
                       confirmation_low, trigger_price. Same job now also
                       checks whether price has broken trigger_price.
        |
        v
fired              -- breakout confirmed. entry = trigger_price (or the
                       confirming candle's actual breakout print -- see
                       "Entry/TP/SL" below), tp/sl computed from entry,
                       Signal row created (existing `signals` table,
                       unchanged shape).
```

Either state can also transition to `expired` if `PENDING_EXPIRY_CANDLES` (6
x 5m = 30 minutes, doc §14) elapses from `setup_time` without reaching
`fired`. There is no `invalidated` state (unlike Precision Pullback's
same-candle-SL-before-entry tie-break) — SL doesn't exist until entry
happens (it's a fixed % of entry, doc §17), so there's nothing to invalidate
against before the setup fires.

## Entry pipeline (`strategy.py`)

`detect_pending_setup(symbol, reject_sink=None)` — runs every
`scan_for_new_setups` cycle (5m), only for symbols with no existing
`pending_setups` row (mirrors today's `armed_symbols` exclusion in
`main.py`):

1. Fetch `MACRO_TF`, `TREND_TF`, `PULLBACK_TF` closed klines (`iloc[:-1]` on
   each). Reject `missing_data`/`insufficient_history` if any is empty or
   shorter than `ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + slope margin`.
2. Compute `calculate_zlema_trend_state` on `MACRO_TF` — must be `+1` (LONG)
   or `-1` (SHORT), never `0` (neutral/no-cross-yet) -> reject `no_macro_trend`.
3. Compute the same on `TREND_TF` — must equal the `MACRO_TF` state exactly
   -> reject `no_trend_agreement` (doc §11: "4H Bull / 1H Bear -> ignore the
   coin").
4. Compute ZLEMA on `PULLBACK_TF` (15m). LONG: latest 15m close
   `<= zlema_15m * (1 + PULLBACK_DISTANCE_PCT)` (doc §12's `close <= ZLEMA *
   1.001` example, generalized to config — default `PULLBACK_DISTANCE_PCT =
   0.10%`, doc's `config.py` block value, not the `0.1%`-from-`1.001` prose
   example; the prose example is illustrative, the config block is
   authoritative). SHORT mirrored (`>= zlema_15m * (1 - PULLBACK_DISTANCE_PCT)`)
   -> reject `no_pullback` if outside this band.
5. Settle-age check on the `PULLBACK_TF` candle (`MIN_CANDLE_SETTLE_SECONDS`,
   reused as-is) -> reject `candle_not_settled`.
6. `direction_slot_available` / `ENABLE_LONG_SIGNALS` gates, unchanged from
   today.
7. Score the pending pullback (see "Scoring — pullback stage" below) ->
   reject `score_below_min` if partial score (the two components knowable at
   this stage) can't mathematically reach `MIN_SIGNAL_SCORE` even with a
   perfect 5m crossover later — cheap early exit, not a hard gate (the real
   gate is the final score once the crossover stage's components are known).
8. Arm: `save_pending_setup` with `status="pending_pullback"`.

`check_setup_confirmation(setup)` — runs every `monitor_pending_setups`
cycle (1m), for every row with `status IN ("pending_pullback",
"pending_breakout")`:

**If `pending_pullback`:**
1. Fetch `ENTRY_TF` (5m) closed klines, settle-age check (existing pattern).
   If the candle hasn't advanced since last check, return `"waiting"` (cheap
   no-op — most 1-minute polls land here).
2. Compute ZLEMA on `ENTRY_TF`. Crossover: LONG needs
   `prev_close <= zlema_5m_prev` and `curr_close > zlema_5m_curr` (doc §15,
   `ta.crossover` equivalent); SHORT mirrored.
3. Confirmation candle (doc §15's added condition): LONG needs
   `close > open` on that same crossover candle; SHORT needs `close < open`.
   No volume/body/EMA checks at all (doc §5: explicitly none of Precision
   Pullback's filters carry over).
4. No crossover this candle -> `"waiting"`. Expiry check (`setup_time` vs
   `PENDING_EXPIRY_CANDLES * CANDLE_MINUTES`) -> `"expired"`.
5. Crossover found -> record `confirmation_high`/`confirmation_low` (that
   candle's high/low), compute `trigger_price` (doc §16: `confirmation_high
   * (1 + ENTRY_BUFFER_PCT)` for LONG, `confirmation_low * (1 -
   ENTRY_BUFFER_PCT)` for SHORT), transition to `pending_breakout`, return
   `"armed_breakout"` (new status main.py handles by persisting the DB
   update and continuing — not yet a fire).

**If `pending_breakout`:**
1. Fetch the latest closed `ENTRY_TF` candle (already-fetched-this-cycle
   data reused where possible).
2. LONG: `high > trigger_price` -> confirmed, fill price = `trigger_price`.
   SHORT: `low < trigger_price` -> confirmed. (Same "breakout buffer must
   actually be broken" philosophy as Precision Pullback, doc §16.)
3. No break yet -> `"waiting"`. Expiry (from the *original* `setup_time`,
   not reset when entering `pending_breakout`) -> `"expired"`.
4. Confirmed -> compute `tp`/`sl` from `trigger_price` (see "Entry/TP/SL"),
   return `"confirmed"` with the fill price — same return shape
   `main.py` already expects from Precision Pullback's confirmation path.

## Scoring (0-100)

Exact rubric from architecture.txt §19:

| Component | Points | Basis |
|---|---|---|
| 4H/1H agreement | 30 | flat 30 (binary — already gated: no agreement means no pending setup exists to score) |
| 1H ZLEMA slope | 20 | scaled by `abs(zlema_1h.iloc[-1] - zlema_1h.iloc[-1-N]) / zlema_1h` vs a reference range, capped at 20 (`N` = new `ZERO_LAG_SLOPE_LOOKBACK`, default `5`, same reasoning as Precision Pullback's `EMA_TREND_SLOPE_LOOKBACK`) |
| Clean 15m pullback | 20 | scaled: full 20 at pullback distance `<= PULLBACK_DISTANCE_PCT / 2`, linearly down to 0 at `PULLBACK_DISTANCE_PCT` |
| Fresh 5m crossover | 20 | flat 20 the candle it happens on, decaying by a fixed step per candle it takes to then break `trigger_price` (rewards a quick breakout — "fresh" per doc's own word choice) |
| Confirmation candle quality | 10 | scaled by the candle's close position within its own range (how cleanly it closed near its high for LONG / low for SHORT) |
| **Total** | **100** | |

Three components (4H/1H agreement, 1H ZLEMA slope, 15m pullback) are
knowable at the `pending_pullback` stage — all their inputs (`MACRO_TF`,
`TREND_TF`, `PULLBACK_TF` data) are already fetched by `detect_pending_setup`
(70 of 100 max); the other two (fresh 5m crossover, confirmation candle
quality) are only knowable once the 5m crossover happens. The final
`score_below_min` gate (default `80`) is therefore evaluated at
`pending_breakout` → `fired` transition, not at arm time —
`_score_pending_setup` is called twice (once for the early partial-score
sanity check in step 7 above, once with full data at fire time) and the
setup is dropped (`mark_pending_setup_missed`, not fired) if the final score
doesn't clear the bar even though everything else lined up.

## Entry, fixed TP/SL

```python
# LONG (fired from pending_breakout, trigger_price already computed)
entry = trigger_price
sl    = entry * (1 - SL_PRICE_PCT)   # SL_PRICE_PCT = SL_ROI_PCT/100/LEVERAGE = 0.0050 (0.50%)
tp    = entry * (1 + TP_PRICE_PCT)   # TP_PRICE_PCT = TP_ROI_PCT/100/LEVERAGE = 0.0035 (0.35%)
# SHORT mirrored: sl = entry*(1+SL_PRICE_PCT); tp = entry*(1-TP_PRICE_PCT)
```
`rr` is the fixed constant `TP_ROI_PCT / SL_ROI_PCT` (0.70) for every
setup, carried through purely for display parity with the existing
`signals.rr` column and Telegram message — doc §17 is explicit there is no
RR-based reject gate.

## Database schema (`database.py`)

`armed_setups` table and every accessor (`save_armed_setup`,
`get_armed_setups`, `get_armed_setup_by_symbol`, `armed_setup_exists`,
`mark_armed_setup_fired`, `mark_armed_setup_missed`,
`mark_armed_setup_expired`, `mark_armed_setup_invalidated`,
`expire_old_armed_setups`, `count_armed_setups`) **deleted**. New
`pending_setups` table, matching architecture.txt §13's column list plus
one practical addition (`trigger_price` — needed to evaluate the breakout in
`pending_breakout`; the doc's prose defines this value in §16 but its own
schema list in §13 omits it, an oversight resolved by including it):

```sql
CREATE TABLE pending_setups (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT    NOT NULL,
    direction          TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'pending_pullback',
                       -- 'pending_pullback' | 'pending_breakout' | 'fired'
                       -- | 'expired' | 'missed'

    macro_tf           TEXT    NOT NULL,
    trend_tf           TEXT    NOT NULL,
    pullback_tf        TEXT    NOT NULL,
    entry_tf           TEXT    NOT NULL,

    macro_trend        INTEGER NOT NULL,   -- +1 / -1 at arm time
    trend_state        INTEGER NOT NULL,   -- 1H state at arm time, == macro_trend

    zlema_1h           REAL    NOT NULL,
    zlema_15m          REAL    NOT NULL,

    pullback_price     REAL    NOT NULL,
    pullback_time      TEXT    NOT NULL,

    confirmation_high  REAL,               -- set on pending_breakout transition
    confirmation_low   REAL,
    trigger_price      REAL,               -- set on pending_breakout transition

    score              REAL    NOT NULL,   -- final score once known; partial
                                            -- score held in-process, not persisted

    setup_time         TEXT    NOT NULL,   -- = created_at, kept as a separate
                                            -- column since architecture.txt
                                            -- lists both -- expiry is always
                                            -- measured from this value, even
                                            -- across the pullback->breakout
                                            -- transition
    expires_at         TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,

    fired_signal_id    INTEGER,
    fired_at           TEXT,
    updated_at         TEXT,
    miss_reason        TEXT
)
```

New accessors mirroring the old ones 1:1: `save_pending_setup`,
`get_pending_setups(status=...)` (generalizes `get_armed_setups`, which
hardcoded `status='armed'` — needed here since two live statuses exist),
`get_pending_setup_by_symbol`, `pending_setup_exists`,
`update_pending_setup_breakout` (new — persists `confirmation_high/low`,
`trigger_price`, transitions to `pending_breakout`), `mark_pending_setup_fired`,
`mark_pending_setup_missed`, `mark_pending_setup_expired`,
`expire_old_pending_setups`, `count_pending_setups`. No
`mark_pending_setup_invalidated` (no invalidated state, see "State machine").

`signals` table: **unchanged**. `status` values stay `pending/win/loss/
expired` — no `breakeven` value is ever written by this strategy (the
column and every downstream consumer already treat it as just another
string, so leaving it in the schema costs nothing; see `reports.py`/
`webui.py` below, which need no changes since they're generic).

## Outcome tracking (`outcome_check.py`)

```python
def check_tp_sl(direction: str, entry_price: float, sl_price: float,
                 tp_price: float, df: pd.DataFrame, entry_candle_cutoff) -> dict | None:
    """Walks closed candles after entry_candle_cutoff. Each candle, in
    order: (1) SL hit -> "loss"; (2) TP hit -> "win" (SL-first tie-break on
    a single wild candle, matching the convention everywhere else in this
    bot). Returns None while open, else {"status": "win"|"loss",
    "pnl_roi_pct": float, "closed_at": Timestamp}. pnl_roi_pct is the raw
    price-move percent (not leverage-scaled -- caller applies LEVERAGE)."""
```

`check_tp_sl_with_breakeven` is **deleted** (git history is the recovery
path if a v2 breakeven experiment is built later, per doc §18).

## `main.py` — scheduler

Split into two jobs (user-approved), replacing today's single
`scan_and_fire_signals`:

- **`scan_for_new_setups`** — every `SCAN_INTERVAL_MINUTES` (5m), keeps
  today's settle-offset cron logic unchanged (`MIN_CANDLE_SETTLE_SECONDS +
  5s` past each candle boundary, the exact mechanism `CLAUDE.md` documents
  as hard-won) since this is the job whose settle-age check runs against a
  candle on the same cadence as the job itself (`PULLBACK_TF` = 15m here,
  actually — wider margin than the 5m-vs-5m case that caused the original
  bug, but the offset logic and its startup guard-rail
  (`RuntimeError` if no valid offset exists) are kept as-is for safety).
  Scans the coin pool minus symbols that already have a `pending_setups`
  row, calls `strategy.detect_pending_setup`, arms new `pending_pullback`
  rows.
- **`monitor_pending_setups`** — every 1 minute (new `MONITOR_INTERVAL_MINUTES`
  config, default `1`), calls `strategy.check_setup_confirmation` for every
  `pending_pullback`/`pending_breakout` row. **Does not need its own
  settle-offset cron trick**: `check_setup_confirmation` only ever reads the
  latest *closed* 5m candle (`iloc[:-1]`), same as `detect_pending_setup`
  always has — Precision Pullback's existing `check_setup_confirmation`
  never had a settle-age gate at all (only `detect_pending_setup` does), so
  polling it every 1 minute just means most polls see "no new closed candle
  since last check" and return `"waiting"` — never a lookahead risk, since a
  still-forming candle is dropped by `iloc[:-1]` regardless of how often the
  job runs. This is why the doc's job split doesn't reintroduce the bug
  `CLAUDE.md` documents (that bug was specifically about the *scan* job's
  cron offset leaving too little margin, not about polling frequency).

`check_outcomes` stays on `OUTCOME_CHECK_MINUTES`, now calling
`outcome_check.check_tp_sl` instead of `check_tp_sl_with_breakeven`; no
`breakeven_triggered_at` handling. `coin_scanner` refresh and the daily/
weekly/monthly report jobs are unchanged.

## `bot.py` / `webui.py` / `reports.py`

- **`bot.py`**: `format_signal` — no `rr`/breakeven-specific lines beyond
  what's already generic (RR line stays, shows the fixed 0.70 constant).
  `cmd_status` (`bot.py:153-219`) config import/display block swaps every
  Precision-Pullback constant (`NO_CHASE_MAX_DISTANCE_PCT`, `ATR_MIN_PCT`/
  `MAX_PCT`, `BREAKEVEN_TRIGGER_ROI_PCT`) for the new ones
  (`MACRO_TF`/`TREND_TF`/`PULLBACK_TF`/`ENTRY_TF` four-way display,
  `ZERO_LAG_LENGTH`, `ZERO_LAG_MULTIPLIER`, `PULLBACK_DISTANCE_PCT`,
  `PENDING_EXPIRY_CANDLES`). `notify_outcome` (`bot.py:88-111`) drops the
  `"breakeven"` branch (dead code once nothing produces that status again,
  but left harmless if kept — removed for consistency with "exactly one
  strategy, no unused paths" precedent from the Precision Pullback pass).
- **`webui.py`**: `get_pending_setups()` (`webui.py:256-258`) calls
  `db.get_pending_setups(status=...)` instead of `db.get_armed_setups`.
  `get_strategy_config()` (`webui.py:222-253`) drops every Precision-
  Pullback-only key, adds `macro_tf`, `trend_tf`, `pullback_tf`, `entry_tf`,
  `zero_lag_length`, `zero_lag_multiplier`, `pullback_distance_pct`,
  `pending_expiry_candles`. `renderConfig()` JS (`webui.py:994-1003`)
  updated in the same change to read the new payload shape (same "Python
  and JS together" rule the Precision Pullback spec followed). `get_stats`/
  `_stats`/`build_payload` and the dashboard's breakeven stat tile are
  **unchanged** — still generic over whatever `status` values exist
  historically in `signals`; a strategy that never writes `"breakeven"`
  again just always shows `0` there, which is correct, not broken.
- **`reports.py`**: **no changes** — already fully generic (win_rate already
  excludes breakeven from its ratio, net_roi already sums win/loss/
  breakeven, `_stats` already reports a `breakevens` count that will simply
  stay `0` for every new signal).

## Configuration (`config.py`)

```python
STRATEGY_NAME default -> "Zero-Lag MTF Pullback v1"

MACRO_TF: str    = os.getenv("MACRO_TF", "4h")
TREND_TF: str    = os.getenv("TREND_TF", "1h")        # was "15m"
PULLBACK_TF: str = os.getenv("PULLBACK_TF", "15m")
ENTRY_TF: str    = os.getenv("ENTRY_TF", "5m")         # unchanged value

MACRO_KLINE_COUNT: int    = int(os.getenv("MACRO_KLINE_COUNT", "300"))
TREND_KLINE_COUNT: int    = int(os.getenv("TREND_KLINE_COUNT", "300"))
PULLBACK_KLINE_COUNT: int = int(os.getenv("PULLBACK_KLINE_COUNT", "250"))
ENTRY_KLINE_COUNT: int    = int(os.getenv("ENTRY_KLINE_COUNT", "250"))   # existing name, default changes from 260

ZERO_LAG_LENGTH: int          = int(os.getenv("ZERO_LAG_LENGTH", "70"))
ZERO_LAG_BAND_LOOKBACK: int   = int(os.getenv("ZERO_LAG_BAND_LOOKBACK", "210"))
ZERO_LAG_MULTIPLIER: float    = float(os.getenv("ZERO_LAG_MULTIPLIER", "1.2"))
ZERO_LAG_SLOPE_LOOKBACK: int  = int(os.getenv("ZERO_LAG_SLOPE_LOOKBACK", "5"))
ATR_PERIOD: int                = int(os.getenv("ATR_PERIOD", "70"))   # feeds the ZL band, not a separate filter -- unlike Precision Pullback there's no ATR_MIN/MAX_PCT gate

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # unchanged value/name
PULLBACK_DISTANCE_PCT: float = float(os.getenv("PULLBACK_DISTANCE_PCT", "0.10")) / 100.0
PENDING_EXPIRY_CANDLES: int = int(os.getenv("PENDING_EXPIRY_CANDLES", "6"))   # new name (was PENDING_SIGNAL_EXPIRY_CANDLES=3)

LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))   # unchanged
TP_ROI_PCT: float = float(os.getenv("TP_ROI_PCT", "7.0"))   # unchanged default
SL_ROI_PCT: float = float(os.getenv("SL_ROI_PCT", "10.0"))  # renamed from MAX_SL_ROI_PCT -- doc calls it SL_ROI_PCT (a fixed value here, "MAX_" no longer accurate since there's no breakeven step that could make the realized SL smaller)
TP_PRICE_PCT: float = TP_ROI_PCT / 100.0 / LEVERAGE
SL_PRICE_PCT: float = SL_ROI_PCT / 100.0 / LEVERAGE

MIN_SIGNAL_SCORE: float = float(os.getenv("MIN_SIGNAL_SCORE", "80"))   # unchanged default, still untuned -- same caveat as Precision Pullback's CLAUDE.md note

MAX_DAILY_SIGNALS: int = int(os.getenv("MAX_DAILY_SIGNALS", "12"))              # was 3
MAX_CONCURRENT_SIGNALS: int = int(os.getenv("MAX_CONCURRENT_SIGNALS", "4"))     # was 2
MAX_ACTIVE_LONG_SIGNALS: int = int(os.getenv("MAX_ACTIVE_LONG_SIGNALS", "2"))   # was 1
MAX_ACTIVE_SHORT_SIGNALS: int = int(os.getenv("MAX_ACTIVE_SHORT_SIGNALS", "2")) # was 1

MONITOR_INTERVAL_MINUTES: int = int(os.getenv("MONITOR_INTERVAL_MINUTES", "1"))   # new -- monitor_pending_setups cadence

# Unchanged (reused as-is): MIN_DAILY_SIGNAL_GAP_MINUTES, SIGNAL_COOLDOWN_MINUTES,
# SIGNAL_EXPIRE_HOURS, ENABLE_LONG_SIGNALS, MIN_CANDLE_SETTLE_SECONDS,
# SCAN_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES, SCAN_WORKERS,
# COIN_REFRESH_HOURS, TOP_N_COINS, EXCLUDE_COINS, all coin-scanner/smart-
# ranking constants, SCHEDULER_*, DRY_RUN*, ESTIMATED_*_FEE_PCT/SLIPPAGE.
```

Removed entirely: `EMA_FAST_LEN`, `EMA_SLOW_LEN`, `EMA_TREND_LEN`,
`EMA_TREND_SLOPE_LOOKBACK`, `EMA_SEPARATION_MIN_PCT`, `RSI_PERIOD`,
`RSI_LONG_RESET_MIN/MAX`, `RSI_SHORT_RESET_MIN/MAX`,
`PULLBACK_LOOKBACK_BARS`, `PULLBACK_PREFERRED_DISTANCE_PCT`,
`NO_CHASE_MAX_DISTANCE_PCT`, `VOLUME_MA_PERIOD`, `VOLUME_CONFIRM_MULT`,
`MAX_CANDLE_BODY_PCT`, `ATR_MIN_PCT`, `ATR_MAX_PCT`,
`BREAKEVEN_TRIGGER_ROI_PCT`, `BREAKEVEN_TRIGGER_PRICE_PCT`,
`PENDING_SIGNAL_EXPIRY_CANDLES` (renamed, see above), `MAX_SL_ROI_PCT`/
`MAX_SL_PRICE_PCT` (renamed, see above).

## Testing

New `tests/test_zero_lag_indicators.py`: `calculate_zlema` against a hand-
computed small series (verify the `2*close - close.shift(lag)` construction
and `lag = floor((length-1)/2)`), `calculate_zlema_band`, and — most
important — `calculate_zlema_trend_state`'s statefulness: trend flips only
on a cross, holds through a close that dips back inside the band without
crossing the *opposite* band, starts neutral (`0`) before any cross exists
in the warmup window.

New `tests/test_strategy_zero_lag.py`: long + short
`test_pending_pullback_armed_on_full_pipeline_pass`,
`test_rejected_when_macro_and_trend_disagree`,
`test_rejected_when_outside_pullback_distance`,
`test_pending_breakout_recorded_on_crossover_plus_confirmation_candle`,
`test_no_crossover_stays_waiting`,
`test_confirmed_on_trigger_price_breakout`,
`test_setup_expires_from_original_setup_time_across_both_stages` (expiry
timer doesn't reset on the pullback->breakout transition),
`test_final_score_gate_can_reject_after_breakout_even_if_pullback_score_was_high`.

New `tests/test_outcome_check_plain.py`: `test_tp_hit_is_a_win`,
`test_sl_hit_is_a_loss`, `test_same_candle_sl_beats_tp_tie_break`.

**Legacy cleanup**: delete `tests/test_precision_pullback_indicators.py`,
`tests/test_strategy_precision_pullback.py`, `tests/test_outcome_check_breakeven.py`,
`tests/test_database_breakeven_status.py` (covers a status value nothing
produces anymore — the *column* stays per "no migration" above, this test
was specifically about the breakeven-trigger write path, which is gone).
`tests/strategy_fixtures.py`: remove Precision-Pullback-only fixture
builders, add new ones for the three-state pending-setup shape.
`tests/test_bot_formatting.py`, `tests/test_webui_stats.py` updated for the
new `cmd_status`/`get_strategy_config` field sets, not deleted.
`tests/test_correlation_limits.py`, `tests/test_database_direction_counts.py`,
`tests/test_reports.py`, `tests/test_mexc_client.py`, `tests/test_outcome_replay.py`,
`tests/test_relative_strength.py` unaffected (all generic over `signals`
table shape/direction counts, none reference the deleted functions).

## Backtest harness

`scripts/backtest_simple_strategy.py` rewritten for the three-state pipeline
and four timeframes, keeping its existing "fake `strategy.get_market_klines`
+ `_SimulatedDatetime`" as-of injection technique (already proven correct in
the two-timeframe case, `7296b5c`'s lookahead fix carries the same
discipline forward: filter each higher timeframe by *close time* relative to
the bar being evaluated, never by open time). Generalizes
`get_klines_extended`/`_with_forming_row` calls to four timeframes
(`MACRO_TF`, `TREND_TF`, `PULLBACK_TF`, `ENTRY_TF`) instead of two, and the
trade-simulation loop gains a middle state (`pending_breakout`) between "no
setup" and "in an open trade," mirroring `check_setup_confirmation`'s new
two-step return shape. Replays `outcome_check.check_tp_sl` (no breakeven
arguments) instead of `check_tp_sl_with_breakeven`. `Trade`/`BacktestStats`
drop the `breakeven`/`breakeven_triggered` fields entirely (doc §18 — v1 has
none to report).

`backtest/tpsl_walkforward.py` (untracked, not part of this repo's tracked
history) is **out of scope** — it already imports `scalper_v3_strategy`,
which was deleted in an earlier pass (`1aba90b`), so it's already broken
independent of this change. Left untouched; not this task's concern.

This pass builds and unit-tests the harness only — **no actual 6-month data
run** (that's a separate follow-up once `backtest/fetch_data.py` has pulled
real 4h/1h/15m/5m history, per the doc's own step 30-31 sequencing, which
puts backtesting *after* the code lands).

## Migration order

1. **Backup** — cut `backup/main-pre-zero-lag-mtf-pullback-v1` from current
   `main` HEAD, tag `pre-zero-lag-mtf-pullback-v1`.
2. **Indicators + tests** — `calculate_zlema`, `calculate_zlema_band`,
   `calculate_zlema_trend_state`, `tests/test_zero_lag_indicators.py`, green.
3. **Config** — remove every Precision-Pullback constant, add the new ones listed above.
4. **Database** — `armed_setups` -> `pending_setups` (new table + accessors), old accessors deleted.
5. **Strategy pipeline** — `detect_pending_setup`, `check_setup_confirmation` (three-state), `tests/test_strategy_zero_lag.py`, green.
6. **Outcome tracking** — `outcome_check.check_tp_sl`, `tests/test_outcome_check_plain.py`, green; `check_tp_sl_with_breakeven` deleted.
7. **Dependents** — `main.py` (two-job scheduler split), `bot.py`, `webui.py` (Python **and** JS).
8. **Deletions** — legacy test files listed above, every config constant listed under "Removed entirely," `calculate_supertrend` and Precision-Pullback-only functions in `strategy.py`.
9. **Backtest harness** — rewrite `scripts/backtest_simple_strategy.py` for the four-timeframe/three-state pipeline; confirm it imports and runs (not a real 6-month run).
10. **Verify** — full test suite green, `py_compile` on every changed/new module, local `DRY_RUN=true` boot check.

All of the above lands as commits directly on `main` (matching the Precision
Pullback pass's precedent — no feature branch), after step 1's backup
branch/tag exist.

## Open questions (flagged, not blocking — pick defaults below unless told otherwise)

1. **Partial-score early exit at the pullback stage (pipeline step 7)** —
   is a real reject worth computing (max-possible-remaining-score check) or
   should every 4H/1H/15m-passing candidate always arm regardless, and only
   get filtered at the final score gate? Default: **compute it** (cheap, and
   avoids arming — then 1-minute-polling — setups that mathematically can't
   pass regardless of what the 5m candle does).
2. **`ATR_PERIOD` reuse** — architecture.txt's zero-lag band uses a 70-period
   ATR (doc §8), same number as `ZERO_LAG_LENGTH` coincidentally. Kept as a
   separate config (`ATR_PERIOD`) rather than hardcoding `=
   ZERO_LAG_LENGTH`, in case a later tuning pass wants to decouple them.
3. **`pending_setups.score` before the final stage exists** — the column is
   `NOT NULL`, but the true 100-point score isn't known until
   `pending_breakout`. Default: store the **partial** score (max 70, the
   three pullback-stage components) while `status = 'pending_pullback'`,
   overwrite with the full score on the `pending_breakout` transition.
   Dashboard/Telegram always display the score as of `fired` time, so this
   is purely an internal-state detail.

## Acceptance criteria

- No references to `calculate_ema`, `calculate_rsi`, `calculate_volume_ma`,
  `calculate_supertrend`, `_score_pending_setup` (Precision's), `RSI_*`,
  `EMA_*`, `NO_CHASE_*`, `PULLBACK_LOOKBACK_BARS`,
  `PULLBACK_PREFERRED_DISTANCE_PCT`, `VOLUME_*`, `MAX_CANDLE_BODY_PCT`,
  `ATR_MIN_PCT`/`ATR_MAX_PCT`, `BREAKEVEN_*`, `armed_setups` remain anywhere
  in the non-test codebase.
- `pending_setups` table exists with the three-state `status` values;
  `signals` table schema unchanged.
- Every fired setup satisfies `valid_trade_geometry` and fixed
  `SL_ROI_PCT`/`TP_ROI_PCT` at `LEVERAGE`x.
- `check_tp_sl` never produces a `"breakeven"` status (function doesn't
  support one); `signals.status` for this strategy is only ever
  `pending/win/loss/expired`.
- `main.py` schedules exactly `scan_for_new_setups` (5m) and
  `monitor_pending_setups` (1m) for signal generation, plus the unchanged
  outcome/coin-refresh/report jobs.
- All tests pass; `py_compile` clean on every changed file.
- `backup/main-pre-zero-lag-mtf-pullback-v1` branch and
  `pre-zero-lag-mtf-pullback-v1` tag exist.
- Changes land as commits directly on `main` (no unmerged feature branch at
  the end of this pass).
- The actual 6-month backtest run and walk-forward parameter tuning are
  explicitly **not** part of this pass's deliverables.

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import webui; import database; import outcome_check"
python -m py_compile config.py database.py strategy.py main.py bot.py webui.py outcome_check.py scripts/backtest_simple_strategy.py
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py
```
Confirm startup logs show strategy name `Zero-Lag MTF Pullback v1`, all four
timeframes (4h/1h/15m/5m), min signal score, fixed TP/SL ROI%, leverage,
dry-run enabled, and both scheduler jobs (`scan_for_new_setups` every 5m,
`monitor_pending_setups` every 1m).
