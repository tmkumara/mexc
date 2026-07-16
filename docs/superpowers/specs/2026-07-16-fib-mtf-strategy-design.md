# Fibonacci Multi-Timeframe Strategy — Design Spec (backtest research only)

Date: 2026-07-16

## Objective

Build and backtest a new, standalone signal strategy — **4H Trend + 1H
Fibonacci Retracement Pullback** — as a research artifact only. This is
**not** a replacement for the live Supertrend Pullback v1 strategy and is
**not** wired into `main.py`/`strategy.py`/`config.py` in any way. The
deliverable is a 60-day backtest report the user reviews before any decision
about further work or deployment is made.

Rationale: the live bot's realistic frequency ceiling (~7-14 candidates/day
unconstrained) and win-rate profile are now well understood from prior
backtesting (see `2026-07-15-supertrend-pullback-v1-design.md` and its
subsequent live results). This is a genuinely different edge hypothesis
(structure/retracement-based instead of EMA-pullback-based) explored on its
own branch so it can be judged on its own merits without risking the
deployed strategy.

## Branch and files

- New branch `feature/fib-mtf-strategy`, cut from `main`. `main` stays
  untouched and deployable (it auto-deploys to the live server on push, per
  `CLAUDE.md`) — nothing on this branch merges until the user reviews
  backtest results and explicitly decides to proceed.
- `fib_strategy.py` (new, repo root) — all strategy logic. Fully standalone;
  does not import from or modify `strategy.py`.
- `config_fib.py` (new, repo root) — this strategy's own tunables, loaded
  from `.env` the same way `config.py` does, but a separate module so
  nothing about the live bot's config is touched.
- `scripts/backtest_fib_strategy.py` (new) — backtest runner, reusing the
  proven architecture from `scripts/backtest_simple_strategy.py`: bounded
  klines windows (avoids the O(n²) lookback bug found in that earlier
  build), `ProcessPoolExecutor`-based per-symbol parallelism, `--symbols`,
  `--days`, `--workers` CLI args, `get_klines_extended()` pagination reused
  as-is from `mexc_client.get_klines`'s `start`/`end` support.
- `tests/test_fib_strategy.py` (new) — unit tests for swing/fib/entry/TP-SL
  logic in isolation, no network calls.

## Indicators and data

**Trend timeframe (4H):** EMA 200.

**Fib/entry timeframe (1H):** RSI 14, Volume SMA (20-bar), ATR 14, N-bar
fractal swing pivots (5-bar default — a candle is a swing high/low if its
high/low is the max/min among itself and 2 candles on each side).

Only completed candles are used on both timeframes (`iloc[:-1]`), matching
the live bot's convention — never evaluate an in-progress candle.

Reused indicator helpers: `calculate_ema`, `calculate_rsi` (Wilder
smoothing) from `strategy.py` are imported directly (pure functions, no
strategy-state coupling, safe to share). ATR is likewise imported. No new
indicator math duplicated where the existing implementation already fits.

## 4H trend filter

```
close > EMA(200) on 4H AND ema_200 not falling (ema_200_now >= ema_200_3_bars_ago) -> LONG bias
close < EMA(200) on 4H AND ema_200 not rising  (ema_200_now <= ema_200_3_bars_ago) -> SHORT bias
otherwise -> no trade
```

Same shape as the live bot's `_detect_trend()`, just on 4H instead of 15m,
and without the Supertrend leg (this strategy's structure signal comes from
the swing/fib mechanism instead).

## 1H swing detection and Fibonacci anchor

1. Compute 5-bar fractal swing highs/lows over the 1H lookback window.
2. Take the most recent completed impulse leg in the trend direction:
   - LONG: last swing low → most recent swing high after it.
   - SHORT: last swing high → most recent swing low after it.
3. Anchor points: `leg_start` (0% level), `leg_end` (100% level).
4. Fib retracement levels computed from the leg:
   `0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0`.
5. Fib extension levels (measured from `leg_end` continuing past `leg_start`
   in the trend direction): `1.272, 1.618`.

If no valid impulse leg exists in the lookback window (`FIB_SWING_LOOKBACK_BARS`,
default 60 bars ≈ 2.5 days on 1H), reject with `no_swing_leg`.

## Entry rules

**Zone touch:** within the last `FIB_ZONE_LOOKBACK_BARS` (default 10) 1H
candles, price must have traded into the golden zone — retracement between
the 0.5 and 0.618 levels of the leg (inclusive), i.e.
`low <= zone_upper and high >= zone_lower` on at least one candle in that
window, for a LONG (mirrored for SHORT).

**Confirmation** on the latest completed 1H candle:
- Rejection candle: closes back out of the zone in the trend direction —
  LONG requires `close > zone_upper` and `close > open` (bullish candle);
  SHORT requires `close < zone_lower` and `close < open` (bearish candle).
- RSI(14) in a directional band: `RSI_LONG_MIN <= rsi <= RSI_LONG_MAX`
  (default 50-68) for LONG, `RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX` (default
  32-50) for SHORT — same band convention as the live bot.
- Volume ≥ `MIN_VOLUME_MULTIPLIER` (default 1.3×) trailing 20-bar average.

No anti-chasing distance check is needed here (unlike the EMA-pullback
strategy) — the fib zone itself defines how far price may be from the
"ideal" entry, so a separate distance cap would be redundant.

## TP / SL

```
entry_price = latest_closed_1h_close
tp_price    = fib extension level (default 1.272) of the leg, in the trend direction
sl_price    = leg_start (the swing point anchoring the retracement) minus/plus
              ATR(14) * SL_ATR_BUFFER_MULTIPLIER (default 0.15), in the direction
              that widens the stop (below leg_start for LONG, above for SHORT)
```

If the resulting SL distance exceeds `MAX_SL_ROI_PCT / 100 / LEVERAGE`
(same leverage/ROI framing as the live bot, defaults `LEVERAGE=20`,
`MAX_SL_ROI_PCT=10.0` → 0.50% max price distance), reject with
`stop_too_wide` — never tighten the stop artificially to force a signal.

RR validation: `rr = reward_distance / risk_distance`, reject if
`rr < MIN_RR` (reused default 1.5). Geometry validated before any candidate
is returned: LONG `sl < entry < tp`, SHORT `tp < entry < sl`.

`FIB_TP_EXTENSION_LEVEL` is a config constant (default `1.272`) so `1.618`
can be tried later without code changes.

## BTC market-safety filter

Reused conceptually from the live bot's `build_btc_context()` /
`_btc_filter_ok()`, but rebuilt against **4H** BTC candles (`BTC_FILTER_TF`
for this strategy defaults to `4h`, distinct from the live bot's `15m`) to
match this strategy's own trend timeframe rather than mixing timeframes.
Same gating logic: LONG requires BTC's own 4H trend (EMA200 + not
opposing-move) to agree; blocked on extreme BTC single/multi-candle moves.
Implemented as `fib_strategy.build_btc_context_4h()` / `_btc_filter_ok()` —
separate functions in `fib_strategy.py`, not imports from `strategy.py`
(the dataclass shape is copied, not shared, to keep this module fully
standalone per the branch's isolation goal).

## Candidate scoring (0-100, backtest ranking only, not a firing floor)

4H trend/EMA-slope strength 25 / fib zone precision (closer to 0.618 =
higher) 20 / confirmation candle quality (RSI + volume) 25 / RR quality 15 /
extension-target plausibility (tighter historical extension hit-rate for
that symbol, if available — otherwise flat) 15. Exact weighting may be
simplified to trend/zone/confirmation/RR only (no symbol-history term) if
that last term proves impractical during implementation — this is a ranking
aid for the backtest report, not a strategy correctness requirement, so the
implementation plan can adjust it without a spec update.

## Config (`config_fib.py`)

```python
FIB_TREND_TF = "4h"
FIB_ENTRY_TF = "1h"
FIB_TREND_EMA_PERIOD = 200
FIB_SWING_FRACTAL_BARS = 5          # N-bar pivot width
FIB_SWING_LOOKBACK_BARS = 60        # 1H bars searched for the impulse leg
FIB_ZONE_LOOKBACK_BARS = 10         # 1H bars searched for a zone touch
FIB_ZONE_LOWER = 0.5
FIB_ZONE_UPPER = 0.618
FIB_TP_EXTENSION_LEVEL = 1.272
FIB_SL_ATR_BUFFER_MULTIPLIER = 0.15
FIB_RSI_PERIOD = 14
FIB_RSI_LONG_MIN, FIB_RSI_LONG_MAX = 50, 68
FIB_RSI_SHORT_MIN, FIB_RSI_SHORT_MAX = 32, 50
FIB_VOLUME_MA_PERIOD = 20
FIB_MIN_VOLUME_MULTIPLIER = 1.3
FIB_ATR_PERIOD = 14

LEVERAGE = 20
MAX_SL_ROI_PCT = 10.0
MIN_RR = 1.5

ENABLE_BTC_FILTER = True
BTC_FILTER_SYMBOL = "BTC_USDT"
BTC_FILTER_TF = "4h"
BTC_MAX_OPPOSING_MOVE_PCT = 0.20
BTC_MAX_SINGLE_CANDLE_MOVE_PCT = 0.60
BTC_MAX_THREE_CANDLE_MOVE_PCT = 1.20
```

## `FibSignal` dataclass

```python
@dataclass
class FibSignal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    rr: float
    score: float
    leg_start: float
    leg_end: float
    zone_lower: float
    zone_upper: float
    generated_at: datetime
```

Public API: `evaluate_symbol_fib(symbol: str, btc_context: BtcContext4h |
None = None, reject_sink: dict | None = None) -> FibSignal | None`. Signature
mirrors the live bot's `evaluate_symbol` for consistency, but is a fully
separate function in `fib_strategy.py`.

## Backtest utility

`scripts/backtest_fib_strategy.py`: accepts one or more symbols and a
`--days` window (default 60), fetches 4H and 1H history via
`mexc_client.get_klines`'s `start`/`end` pagination (reusing
`get_klines_extended` logic from `scripts/backtest_simple_strategy.py`),
walks forward chronologically with bounded windows per evaluation (no
lookahead, no O(n²) cost), simulates entry at confirmation close, TP/SL with
same-candle-SL-first tie-break (same convention as the live bot's outcome
checker). Reports: total trades, wins, losses, expired, win rate, gross ROI,
estimated fees, net ROI, average ROI/trade, max consecutive losses, max
drawdown, average RR, LONG/SHORT performance breakdown, per-symbol
performance, and average trades/day (to compare against the user's 10/day
target). `--workers` flag for `ProcessPoolExecutor` parallelism, same as the
existing harness.

Symbol universe: reuse the same liquid-USDT-perpetual selection used in the
prior 46-symbol/60-day Supertrend backtest run (excluding BTC/ETH/SOL/XAUT
and the other non-crypto-quote symbols already excluded live), so results
are comparable apples-to-apples against the existing strategy's backtested
numbers.

## Testing

`tests/test_fib_strategy.py`:
- `test_fractal_swing_detection_finds_known_pivots`
- `test_fib_levels_computed_correctly_for_known_leg`
- `test_zone_touch_detected_within_lookback`
- `test_zone_touch_not_detected_outside_lookback`
- `test_long_signal_valid`, `test_short_signal_valid`
- `test_rejected_without_4h_trend`
- `test_rejected_without_swing_leg`
- `test_rejected_without_zone_touch`
- `test_rejected_when_rsi_out_of_band` (long + short)
- `test_rejected_when_volume_too_low`
- `test_rejected_when_stop_too_wide`
- `test_rejected_when_rr_too_low`
- `test_long_trade_geometry`, `test_short_trade_geometry`,
  `test_invalid_geometry_rejected`
- `test_btc_filter_blocks_opposing_direction` (4H variant)
- `test_active_last_candle_is_ignored` (both timeframes)

All tests run standalone, no network calls (synthetic OHLCV DataFrames),
matching the existing test suite's style (`tests/test_btc_filter.py`,
`tests/test_strategy_supertrend_pullback.py`).

## Explicitly out of scope

- No changes to `strategy.py`, `main.py`, `config.py`, `bot.py`, `webui.py`,
  `database.py`.
- No `ENTRY_SIGNAL_MODE`-style toggle to switch the live bot onto this
  strategy — that is a separate, later decision if the backtest results
  justify it.
- No live deployment, no server changes, no `.env` changes on the
  production server.
- No automatic parameter optimization pass — first version reports honest
  baseline results only, same philosophy as the original Supertrend
  Pullback v1 backtest.

## Migration order (drives the implementation plan's phases)

1. **Fib/swing math + tests** — fractal swing detection, fib level/extension
   calculation, zone-touch detection; unit tests green with synthetic data.
2. **Strategy** — `fib_strategy.py`: 4H trend filter, entry confirmation,
   TP/SL, RR/geometry validation, BTC filter (4H variant); long + short unit
   tests green.
3. **Backtest harness** — `scripts/backtest_fib_strategy.py`, reusing the
   pagination + `ProcessPoolExecutor` pattern; smoke test on 2-3 symbols
   over a short window before the full run.
4. **Full backtest run** — same symbol universe as the prior Supertrend
   backtest, 60 days, full report generated and reviewed with the user.

## Acceptance criteria

- `fib_strategy.py` has zero imports from/of `strategy.py`'s stateful
  pieces (pure indicator helpers may be shared; strategy logic may not).
- No LONG candidate can have `tp <= entry` or `sl >= entry`; no SHORT
  candidate can have `tp >= entry` or `sl <= entry`.
- Every candidate has `rr >= MIN_RR`.
- Every candidate's SL distance is `<= MAX_SL_ROI_PCT/100/LEVERAGE`.
- All unit tests pass (`python -m pytest tests/test_fib_strategy.py -v`).
- `python -m py_compile fib_strategy.py config_fib.py
  scripts/backtest_fib_strategy.py` passes.
- Backtest runs end-to-end on the full 60-day/multi-symbol set and prints a
  complete report (no crashes, no silent truncation of history without it
  being reported).
- `main.py`, `strategy.py`, `config.py` are byte-identical to `main`'s HEAD
  at the end of this work (verified via `git diff main -- main.py strategy.py
  config.py` being empty).

## Final verification commands

```bash
python -m pytest tests/test_fib_strategy.py -v
python -c "import config_fib; import fib_strategy"
python scripts/backtest_fib_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT --days 14
git diff main -- main.py strategy.py config.py   # must be empty
```
