# Nadaraya-Watson Kernel Strategy — Design Spec (backtest research)

## Purpose

A brand-new, standalone backtest-research strategy, fully isolated from the
live bot (`main.py`/`strategy.py`/`config.py`/`bot.py`/`webui.py`/`database.py`
must never be touched). It replaces the Fibonacci MTF strategy explored on
`feature/fib-mtf-strategy` (which, after thorough tuning, was found to have
no positive edge at scale — 1,791 trades, 30.0% win rate, -4754.6% net ROI on
the full symbol pool). That branch is left untouched as a historical record.

This strategy is built around the "Nadaraya-Watson: Rational Quadratic
Kernel (Non-Repainting)" Pine Script indicator (jdehorty, MPL-2.0), ported
faithfully to Python, wrapped with the same quality-filter shape used
throughout this project's research strategies (HTF trend filter, RSI/volume
confirmation, BTC market-safety filter, RR-gated dynamic TP/SL).

**Goal:** finetune configuration to find the best achievable case of at
least 15% ROI (at 20x leverage) on a 60-day backtest — while continuing this
project's standing rule of honest reporting: report the real result even if
15% ROI is not reachable, rather than force it via overfitting to a small
sample.

## Source Indicator (reference, verbatim)

```pinescript
// This source code is subject to the terms of the Mozilla Public License 2.0 at https://mozilla.org/MPL/2.0/
// © jdehorty
// @version=5
indicator('Nadaraya-Watson: Rational Quadratic Kernel (Non-Repainting)', overlay=true, timeframe="")

src = input.source(close, 'Source')
h = input.float(8., 'Lookback Window', minval=3.)
r = input.float(8., 'Relative Weighting', step=0.25)
x_0 = input.int(25, "Start Regression at Bar")
smoothColors = input.bool(false, "Smooth Colors")
lag = input.int(2, "Lag")
size = array.size(array.from(src))

kernel_regression(_src, _size, _h) =>
    float _currentWeight = 0.
    float _cumulativeWeight = 0.
    for i = 0 to _size + x_0
        y = _src[i]
        w = math.pow(1 + (math.pow(i, 2) / ((math.pow(_h, 2) * 2 * r))), -r)
        _currentWeight += y*w
        _cumulativeWeight += w
    _currentWeight / _cumulativeWeight

yhat1 = kernel_regression(src, size, h)
yhat2 = kernel_regression(src, size, h-lag)

bool wasBearish = yhat1[2] > yhat1[1]
bool wasBullish = yhat1[2] < yhat1[1]
bool isBearish = yhat1[1] > yhat1
bool isBullish = yhat1[1] < yhat1
bool isBearishChange = isBearish and wasBullish
bool isBullishChange = isBullish and wasBearish

bool isBullishCross = ta.crossover(yhat2, yhat1)
bool isBearishCross = ta.crossunder(yhat2, yhat1)
bool isBullishSmooth = yhat2 > yhat1
bool isBearishSmooth = yhat2 < yhat1
```

**Reading of the math:** `kernel_regression` is a causal (backward-looking
only), rational-quadratic-kernel-weighted moving average, recomputed fresh
at every bar using only that bar and earlier ones — this is what makes it
"non-repainting" (no centered/future-looking window, unlike a Gaussian
smoother applied after the fact). The weight of a bar `i` positions back
from the current bar is `w(i) = (1 + i² / (2·h²·r))^-r`. `x_0` only pads the
Pine loop past the end of the available series (a no-op once bounded by
real history — Pine's `_src[i]` for out-of-range `i` returns `na` and the
sum simply stops contributing).

Two color-change modes exist in the source: rate-of-change based
(`isBullishChange`/`isBearishChange`, the **default**, `smoothColors=false`)
and crossover based (`isBullishCross`/`isBearishCross`, active when
`smoothColors=true`). This strategy uses the **default rate-of-change mode**
— a bar where `yhat1`'s slope flips from falling to rising (bullish) or
rising to falling (bearish).

## Isolation Principle

Same as every prior standalone strategy in this repo:

- `nw_strategy.py` may import **only** `calculate_ema`, `calculate_rsi`,
  `calculate_atr` from the live bot's `strategy.py` — nothing else.
- `config_nw.py` is fully independent, all values `os.getenv("NW_...")`-driven
  with hardcoded defaults, never reads the live bot's `config.py`.
- No file under this strategy's ownership ever imports or modifies
  `main.py`, `strategy.py` (beyond the three whitelisted helpers),
  `config.py`, `bot.py`, `webui.py`, or `database.py`.
- `mexc_client.py`, `coin_scanner.py`, and `scripts/backtest_simple_strategy.py`
  (already ported/shared with the fib strategy's backtest tooling) are
  reused read-only, unmodified.

## Files

- `nw_kernel.py` (new) — indicator math:
  - `kernel_weight(i, h, r)` — `(1 + i²/(2·h²·r))^-r`.
  - `nw_estimate(src: np.ndarray, h: float, r: float, window: int) -> np.ndarray`
    — vectorized, causal kernel regression producing `yhat1` for every bar,
    truncating the weighted sum to the most recent `window` bars per point
    (rational-quadratic weights decay with distance, so a bounded window is
    a close numerical approximation of the full-history Pine sum — verified
    by a dedicated accuracy test, not assumed).
  - `detect_slope_flip(yhat: np.ndarray) -> pd.Series` — per-bar bullish/
    bearish/none classification matching Pine's `isBullishChange` /
    `isBearishChange` (rate-of-change mode).
- `config_nw.py` (new) — standalone config, `NW_`-prefixed env vars.
- `nw_strategy.py` (new) — `evaluate_symbol_nw(symbol, btc_context=None,
  reject_sink=None)`, the strategy's single public entry point, same shape
  as `fib_strategy.evaluate_symbol_fib`.
- `tests/nw_fixtures.py`, `tests/test_nw_kernel.py`, `tests/test_config_nw.py`,
  `tests/test_nw_strategy.py` (new).
- `scripts/backtest_nw_strategy.py` (new) — backtest harness, duplicating
  the proven `Trade`/`BacktestStats`/`_with_forming_row`/`_find_as_of_index`/
  `_simulate_outcome`/`_roi_with_costs`/`backtest_symbol`/`main` structure
  from `scripts/backtest_fib_strategy.py`, importing only
  `get_klines_extended` from `scripts/backtest_simple_strategy.py`.
- `scripts/fetch_backtest_symbol_pool.py` (reused, unmodified) — already
  supports both the curated ranked pool (default) and the `--max` full pool.

## Signal Logic

```
nw_strategy.evaluate_symbol_nw(symbol, btc_context=None, reject_sink=None):

  1. _detect_trend_htf(df_4h):
     close > EMA(200) on NW_TREND_TF (4h) -> LONG bias
     close < EMA(200) on NW_TREND_TF (4h) -> SHORT bias
     otherwise -> no trade

  2. _detect_nw_signal(df_1h, direction):
     compute yhat1 = nw_estimate(close, NW_LOOKBACK_WINDOW, NW_RELATIVE_WEIGHTING, ...)
       on NW_ENTRY_TF (1h)
     the latest CLOSED candle must be a slope-flip bar in the trend
       direction (bullish flip for LONG, bearish flip for SHORT) --
       matches Pine's default (non-smoothColors) isBullishChange/
       isBearishChange definition
     RSI(14) inside the direction's band (NW_RSI_LONG_MIN/MAX,
       NW_RSI_SHORT_MIN/MAX)
     volume >= NW_MIN_VOLUME_MULTIPLIER x trailing NW_VOLUME_MA_PERIOD avg

  3. If NW_ENABLE_BTC_FILTER, build_btc_context_nw()/_btc_filter_ok_nw()
     must pass -- same shape as the fib strategy's 4H BTC filter (own
     4H EMA200/trend + 1-candle/3-candle move caps).

  4. _calculate_tp_sl_nw():
     TP: fixed distance at TP_PRICE_PCT = TARGET_ROI_PCT / 100 / LEVERAGE
       (15.0 / 100 / 20 = 0.75% price move at the target 20x leverage)
     SL: structural -- beyond the swing low/high preceding the slope-flip
       bar (found via the same fractal-swing approach as the fib
       strategy, reused conceptually but reimplemented locally, no
       cross-import), plus an ATR buffer (NW_SL_ATR_BUFFER_MULTIPLIER),
       capped at MAX_SL_PRICE_PCT = MAX_SL_ROI_PCT / 100 / LEVERAGE

  5. RR = reward / risk must be >= MIN_RR.

  6. _score_candidate_nw(): 0-100 composite (trend alignment, slope-flip
     strength, RSI quality, volume quality, RR quality) for ranking
     multiple candidates within a scan.
```

## Config (`config_nw.py`, initial defaults)

| Variable | Default | Purpose |
|---|---|---|
| `NW_TREND_TF` / `NW_ENTRY_TF` | 4h / 1h | HTF trend and NW entry timeframes |
| `NW_TREND_EMA_PERIOD` | 200 | HTF trend EMA period |
| `NW_LOOKBACK_WINDOW` (Pine `h`) | 8.0 | Kernel lookback window |
| `NW_RELATIVE_WEIGHTING` (Pine `r`) | 8.0 | Kernel relative weighting |
| `NW_KERNEL_SUM_WINDOW` | 500 | Bars summed per kernel estimate (truncation bound, not a Pine param) |
| `NW_RSI_PERIOD` | 14 | RSI period |
| `NW_RSI_LONG_MIN` / `MAX` | 45 / 75 | 1h RSI band for LONG |
| `NW_RSI_SHORT_MIN` / `MAX` | 25 / 55 | 1h RSI band for SHORT |
| `NW_VOLUME_MA_PERIOD` | 20 | Volume MA period |
| `NW_MIN_VOLUME_MULTIPLIER` | 1.0 | Volume confirmation multiplier |
| `NW_ATR_PERIOD` | 14 | ATR period |
| `NW_SL_ATR_BUFFER_MULTIPLIER` | 0.5 | SL buffer beyond structural swing |
| `LEVERAGE` | 20 | Target leverage (per this session's explicit ask) |
| `TARGET_ROI_PCT` | 15.0 | Target ROI% at leverage -> TP_PRICE_PCT |
| `MAX_SL_ROI_PCT` | 20.0 | Starting SL cap (tuned during iteration, same as fib's post-tuning value) |
| `MIN_RR` | 1.5 | Minimum reward:risk |
| `NW_ENABLE_BTC_FILTER` | true | BTC 4H market-safety gate |
| `NW_SIGNAL_EXPIRE_HOURS` | 48 | Pending signal TTL (backtest only) |
| `ESTIMATED_ENTRY_FEE_PCT` / `EXIT_FEE_PCT` / `SLIPPAGE_PCT` | 0.02 / 0.02 / 0.01 | Backtest cost model |

All values `os.getenv("NW_...")`-driven (the four leverage/ROI/RR variables
keep the same bare names as `config_fib.py` for consistency with the rest
of the project's config pattern).

## Testing Strategy

- `nw_kernel.py`: unit tests with hand-computed weights for small `i`/`h`/`r`
  combinations; a synthetic series compared against a naive full-sum
  reference implementation to validate the windowed-truncation
  approximation stays within a tight numerical tolerance; slope-flip
  detection tested against constructed up/down/flat sequences.
- `nw_strategy.py`: fixture-driven tests mirroring `fib_strategy.py`'s
  coverage — trend detection, signal detection (including RSI/volume
  reject paths), TP/SL calculation and geometry validation, RR gating,
  BTC filter, and full `evaluate_symbol_nw` integration cases for both
  LONG and SHORT.
- `config_nw.py`: defaults-loaded assertions, `MAX_SL_PRICE_PCT` derived-value
  assertion.

## Backtest Workflow

1. Implement via the formal task plan (subagent-driven-development),
   TDD throughout.
2. Initial config-tuning iterations on the curated ~47-symbol pool
   (`scripts/fetch_backtest_symbol_pool.py` default mode) for fast
   feedback — this was the only pool size that ever produced a usable
   signal-to-noise ratio during the fib strategy's research.
3. Once a stable, reasoned configuration is reached (or after honest
   exhaustion of reasonable tuning avenues), run the definitive 60-day
   backtest across the full ~658-symbol pool (`--max` mode), per this
   session's explicit instruction, and report the real result --
   including fee-drag and drawdown analysis -- without forcing a
   favorable number.

## Out of Scope

- No changes to the live bot's files.
- No breakeven/trailing-stop management (fixed TP/SL checked candle-by-candle
  to win/loss/expiry, same as every other strategy in this repo).
- No live/paper trading wiring -- backtest research only, same as the fib
  strategy.
