# Simple Supertrend Pullback v1 — Design Spec

Date: 2026-07-15
Source: `D:\Downloads\Finexa-KT\architecture.txt` (full text incorporated below, with
resolved ambiguities called out inline). This design **completely replaces** the
current Liquidation-Aware 1m Scalp (v14) strategy. No liq-cluster behavior is
preserved.

## Objective

Replace the current strategy with **15m Trend + 5m Supertrend Pullback**:

- Leverage: 20× isolated
- Gross take-profit ROI: 15% (≈0.75% price move)
- Maximum stop-loss ROI: 10% (≈0.50% price move)
- Minimum RR: 1.5
- Maximum 3 signals/day, max 1 active LONG + 1 active SHORT concurrently

Simplicity, transparency, and testability are the primary goals — no
liquidation-cluster estimation, no Nadaraya-Watson, no OI polling, no
two-phase arm/monitor workflow.

## Preparation (done before implementation starts)

1. Backup branch `backup/main-pre-supertrend-pullback-v1-2026-07-15` cut from
   current `main` HEAD, pushed to `origin`.
2. Implementation happens on a new branch `feature/supertrend-pullback-v1` off
   `main`. `main` stays deployable/untouched until this is tested and merged
   (main auto-deploys to the live server on push, per `CLAUDE.md`).

## Components retained as-is

`coin_scanner.py`, `mexc_client.py`, `market_data.py`, `candle_cache.py`,
`mexc_ws_client.py`, `ws_manager.py`, `database.py` (signals table schema),
`bot.py` (module, reworked message template), `reports.py`, `main.py`
(module, reworked scheduler/scan loop), `webui.py` (module, reworked
display).

## Components removed from the active runtime

Not imported/executed by the new runtime (files remain on disk for
reference until the new implementation is stable and tests pass, then may
be deleted in a follow-up):

- `liq_estimator.py`, `nw_kernel.py`
- Open-interest polling (`poll_open_interest_loop`, `OI_POLL_SEC`,
  `strategy.get_estimator`, `strategy.update_ticker_cache`)
- Liquidation-cluster calculations, magnet rules, provisional setups
- Funding-based veto logic
- Current 1m EMA/VWAP/RSI/volume base signal + `nw_ribbon` base signal
- The `armed_setups` two-phase arm/monitor workflow (DB table and its
  helper functions stay in `database.py`, just unused by the new runtime)
- `outcome_replay.py`'s breakeven-trail replay (file stays, unused — see
  "Resolved ambiguity 2" below)

## New strategy: indicators

**Trend timeframe (15m):** EMA 200, Supertrend (ATR period 10, multiplier 3.0)

**Entry timeframe (5m):** EMA 20, RSI 14, Supertrend (ATR period 10,
multiplier 2.0), Volume SMA 20, ATR 14

Only completed candles are used — `closed_df = df.iloc[:-1].copy()`,
independently for 15m and 5m. All decisions read from `closed_df`.

Required helper functions in `strategy.py`:
```python
calculate_ema(series, period)
calculate_rsi(series, period)   # Wilder smoothing, not simple rolling mean
calculate_atr(df, period)       # Wilder smoothing; TR = max(h-l, |h-prev_close|, |l-prev_close|)
calculate_supertrend(df, atr_period, multiplier)  # returns line + direction (1=bullish,-1=bearish)
```
Implemented with NumPy/pandas only — no TA-Lib, no paid/closed-source
indicator dependency. Non-repainting, no future-candle access.

## Long entry rules

15m trend: close > EMA200, Supertrend bullish, EMA200 not strongly
declining (`ema_200_current >= ema_200_three_bars_ago`, small float
tolerance).

5m pullback/confirmation: price was above EMA20 before the pullback; one of
the previous 3 completed candles touched/dipped slightly below EMA20;
latest completed candle closes back above EMA20 and is bullish
(`close > open`); 5m Supertrend bullish; RSI14 in [50, 68]; volume ≥ 1.2×
the 20-bar average; confirmation candle range ≤ 1.8× ATR14.

Anti-chasing: reject when `(close - ema_20) / close > 0.003`.

## Short entry rules

Mirror image of long: close < EMA200, Supertrend bearish, EMA200 not
strongly rising (`ema_200_current <= ema_200_three_bars_ago`); pullback
touched/poked above EMA20 in the prior 3 candles; latest candle closes back
below EMA20 and is bearish; 5m Supertrend bearish; RSI14 in [32, 50];
volume ≥ 1.2×; candle range ≤ 1.8× ATR14; reject when
`(ema_20 - close) / close > 0.003`.

## Entry / TP / SL

```python
entry_price = latest_closed_5m_close
tp_price_pct = TARGET_ROI_PCT / 100 / LEVERAGE   # 0.15/20 = 0.75%
```
LONG: `tp = entry * (1 + tp_price_pct)`; SHORT: `tp = entry * (1 - tp_price_pct)`.

Structural SL: LONG uses the lowest low of the previous 3 completed 5m
candles minus `ATR14 * SL_ATR_BUFFER_MULTIPLIER (0.10)`; SHORT uses the
highest high plus the same buffer. If the structural stop is farther than
`MAX_SL_ROI_PCT/100/LEVERAGE` (0.50%) from entry, reject the trade — never
tighten the stop artificially to force a signal.

RR validation: `rr = reward_distance / risk_distance`, reject if
`rr < MIN_RR (1.5)`. Geometry validated: LONG `tp > entry > sl`, SHORT
`tp < entry < sl`. No invalid geometry may reach the DB or Telegram.

## BTC market safety filter

Computed once per scan cycle (not once per altcoin) from `BTC_USDT` 15m
candles: EMA200, Supertrend (ATR 10, mult 3.0), 1-candle and 3-candle % move.

LONG requires `btc_close > btc_ema_200`, `btc_supertrend_direction == 1`,
`btc_three_candle_move_pct >= -BTC_MAX_OPPOSING_MOVE_PCT`. SHORT is the
mirror. All new signals rejected when
`abs(btc_one_candle_move_pct) > BTC_MAX_SINGLE_CANDLE_MOVE_PCT` or
`abs(btc_three_candle_move_pct) > BTC_MAX_THREE_CANDLE_MOVE_PCT`.

Defaults: `ENABLE_BTC_FILTER=True`, `BTC_FILTER_SYMBOL=BTC_USDT`,
`BTC_FILTER_TF=15m`, `BTC_MAX_OPPOSING_MOVE_PCT=0.20`,
`BTC_MAX_SINGLE_CANDLE_MOVE_PCT=0.60`, `BTC_MAX_THREE_CANDLE_MOVE_PCT=1.20`.

## Resolved ambiguity 1: `evaluate_symbol` signature vs. BTC context

The spec's section 18 gives `evaluate_symbol(symbol: str) -> Signal | None`
but the BTC-filter addendum requires computing BTC context once per cycle
and passing it into every symbol evaluation to avoid duplicate API calls.
**Resolution:** `evaluate_symbol(symbol: str, btc_context: BtcContext |
None = None)`. `main.py`'s scan loop builds `BtcContext` once and passes it
to every call; if omitted (e.g. in a unit test or the backtest script
calling per-symbol without a shared cycle), `evaluate_symbol` fetches/builds
its own. `BtcContext` is a small dataclass (`close`, `ema_200`,
`supertrend_direction`, `one_candle_move_pct`, `three_candle_move_pct`).

## Resolved ambiguity 2: same-candle TP/SL tie-break, breakeven disabled

Spec section 20 wants: same-candle TP+SL hit → treat SL as hit first
(conservative). Spec section 21 disables breakeven entirely for v1 (target
is only ~0.75% away; moving stops would distort results). The existing
`outcome_replay.py` implements a *different*, more elaborate breakeven-trail
replay with a close/open-direction tie-break, which no longer matches what
the spec wants and doesn't apply once breakeven is off.

**Resolution:** `outcome_replay.py` is left in the repo untouched but is
**not called** by the new runtime (same "keep for reference, don't import"
treatment as `liq_estimator.py`/`nw_kernel.py`). A new, small, independently
testable helper (in `main.py` or a new `outcome_check.py`) implements the
plain rule from section 20:
```python
# LONG:  TP hit when high >= tp_price; SL hit when low <= sl_price
# SHORT: TP hit when low <= tp_price;  SL hit when high >= sl_price
# same-candle tie -> SL wins
```
`breakeven_triggered_at` DB column remains for schema compatibility but is
unused by the new strategy.

## Resolved ambiguity 3: backtest historical range

`mexc_client.get_klines` only accepts `count`, not a `start`/`end` range —
there is no pagination for pulling arbitrarily long history. A 30-day 5m
backtest (~8,640 candles) may be truncated to whatever MEXC returns for a
single request. **Resolution:** v1 backtest fetches the maximum single-request
count and reports the actual achieved lookback window in its output rather
than building pagination — matches the spec's own "first version," "verify
the baseline honestly" framing. Pagination can be a later enhancement if the
achieved window proves too short to be useful.

## Signal workflow (single-pass, no arming)

```python
async def scan_and_fire_signals(app):
    load coin pool
    check daily limit
    check minimum signal gap
    check concurrent active signals (per-direction)
    build BTC context once
    evaluate symbols in parallel (evaluate_symbol(symbol, btc_context))
    sort valid candidates by score, descending
    apply correlation and direction limits
    save and broadcast accepted signals
```

No provisional setups, no liquidation-data wait, no 1-minute monitoring
cycle. Scheduler runs every 5 minutes, a few seconds after candle close
(cron `minute=*/5 second=5`) to ensure MEXC has finalized the candle.

## Candidate scoring (0–100)

15m trend alignment 25 / 5m Supertrend alignment 20 / EMA20 reclaim quality
20 / volume strength 15 / RSI quality 10 / RR quality 10. Volume ratio 1.2×
= minimum score, 2.0×+ = full score. RSI ideal ~55–62 (LONG) / ~38–45
(SHORT). RR 1.5 = minimum score, 2.0+ = full score. Sort descending; only
the highest-quality candidates permitted by runtime limits are sent.

## Correlation protection

```python
MAX_SIGNALS_PER_SCAN = 1
MAX_ACTIVE_LONG_SIGNALS = 1
MAX_ACTIVE_SHORT_SIGNALS = 1
```
New `database.count_active_signals_by_direction(direction: str) -> int`. A
pending LONG blocks another LONG until it closes/expires; same for SHORT.
(Addresses the previous bot's tendency to fire many correlated SHORTs at once.)

## Coin pool defaults

`TOP_N_COINS=30`, `COIN_POOL_MIN_SELECTED=20`, `COIN_POOL_MIN_VOLUME_USD=10000000`,
`COIN_REFRESH_HOURS=6`. Exclude `BTC_USDT, ETH_USDT, SOL_USDT, XAUT_USDT`
unless overridden. `CRYPTO_FUTURES_ONLY=true` stays.

## Configuration (`config.py`)

Remove all Nadaraya-Watson / liquidation-tier / cluster / funding-veto / OI
/ 1m-armed-setup / EMA-9-21-50 / VWAP settings. Add the full block from
architecture.txt section 16 (`STRATEGY_NAME`, `TREND_TF`/`ENTRY_TF`,
kline counts, EMA/RSI/ATR/Supertrend periods, volume multiplier, pullback
lookback, anti-chase distance, confirmation-candle ATR cap, SL ATR buffer,
`TARGET_ROI_PCT`/`MAX_SL_ROI_PCT`/`LEVERAGE` → derived `TP_PRICE_PCT`/
`MAX_SL_PRICE_PCT`, `MIN_RR`, scan interval, daily/gap/concurrent/direction
limits, cooldown, expiry) plus the BTC filter constants and:
```python
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DRY_RUN_SAVE_SIGNALS = os.getenv("DRY_RUN_SAVE_SIGNALS", "false").lower() == "true"
ESTIMATED_ENTRY_FEE_PCT = float(os.getenv("ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT = float(os.getenv("ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.01"))
```

## `Signal` dataclass

```python
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
    rr: float
    score: float
    entry_low: float
    entry_high: float
```
No `armed_setup_id`. Public API: `evaluate_symbol(symbol, btc_context=None) -> Signal | None`
performs: fetch 15m/5m candles (via `market_data.get_market_klines`) → drop
forming candle → validate candle count → compute indicators → determine
15m trend → detect 5m pullback/confirmation → BTC filter → compute TP/
structural SL → validate max SL distance → validate RR → compute score →
return `Signal` or `None`.

## Data access

`strategy.py` calls `market_data.get_market_klines(symbol, interval,
count)` (WS-cache-first, REST fallback) for both timeframes and for the
BTC filter context — not `mexc_client.get_klines` directly, since
`market_data.py` already implements exactly this abstraction.

## Outcome checking

Retain the concept in `main.py → check_outcomes`: load pending signals,
fetch 5m entry-timeframe candles, determine TP/SL via the new SL-first-tie
helper (resolved ambiguity 2), mark win/loss/expired, compute leveraged
ROI, notify Telegram. 48h→ now `SIGNAL_EXPIRE_HOURS=6` per spec default.

## Telegram formatting (`bot.py`)

New message template per architecture.txt section 22 (strategy name,
15m+5m setup description, gross ROI wording, RR, leverage). Financial-risk
warning retained.

## Dashboard (`webui.py`)

Remove Hybrid SMC Pro / order blocks / liquidity sweeps / pending SMC
setups / MSS / liquidation-setup / old trend-entry-config terminology and
the `armed_setups` section. Display: strategy name, trend/entry TF, EMA
periods, Supertrend params, RSI ranges, volume multiplier, target ROI, max
SL ROI, min RR, leverage, daily cap, active LONG/SHORT counts, recent
signals, performance.

## Database updates

Add `count_active_signals_by_direction(direction)`. Optionally add columns
`strategy_name TEXT, score REAL, rr REAL, entry_timeframe TEXT,
trend_timeframe TEXT, setup_reason TEXT` via guarded `ALTER TABLE` (same
try/except pattern already used in `init_db()` for `placed`/`placed_at`/
`breakeven_triggered_at`). Update `save_signal` to persist them.

## Logging

`[REJECT] <symbol> <reason>` at DEBUG for normal rejections (including
BTC-filter rejections). `[CANDIDATE] <symbol> <direction> score=... entry=...
tp=... sl=... rr=...` and `[SIGNAL] Fired #<id> <symbol> <direction>
score=...` at INFO. No per-symbol rejection flooding at INFO.

## Testing

New `tests/test_indicators.py`: `test_ema_values`, `test_rsi_uptrend`,
`test_rsi_downtrend`, `test_atr_values`, `test_supertrend_bullish_direction`,
`test_supertrend_bearish_direction`, `test_supertrend_does_not_use_future_data`.

New `tests/test_strategy_supertrend_pullback.py`: long + short variants of
`test_*_signal_valid`, `test_*_rejected_without_15m_trend`,
`test_*_rejected_without_pullback`, `test_*_rejected_when_rsi_*`,
`test_*_rejected_when_volume_too_low`, `test_*_rejected_when_candle_too_large`,
`test_*_rejected_when_stop_too_wide`, `test_*_rejected_when_rr_too_low`;
`test_active_last_candle_is_ignored`; `test_long_trade_geometry`,
`test_short_trade_geometry`, `test_invalid_geometry_rejected`; risk-formula
approximate-float assertions (0.75%↔15%, 0.50%↔10% at 20×);
`test_second_active_long_is_blocked`, `test_second_active_short_is_blocked`,
`test_long_and_short_can_coexist`.

New `tests/test_btc_filter.py`: `test_long_allowed_when_btc_bullish`,
`test_long_blocked_when_btc_bearish`, `test_short_allowed_when_btc_bearish`,
`test_short_blocked_when_btc_bullish`, `test_signal_blocked_during_extreme_btc_move`,
`test_btc_active_candle_is_ignored`.

**Legacy test cleanup:** `tests/test_strategy_liq_scalp.py`,
`tests/test_liq_estimator.py`, `tests/test_nw_kernel.py` are **deleted** in
this work (per explicit decision — they'd still pass against untouched
legacy modules, but the suite should reflect only what's actually running).
`tests/test_mexc_client.py` and `tests/test_outcome_replay.py` are
unaffected (mexc_client is retained as-is; outcome_replay stays in the repo
even though unused by the new runtime, so its existing tests still apply).

## Backtest utility

New `scripts/backtest_simple_strategy.py`: accepts one or more symbols,
uses `evaluate_symbol` directly against 15m/30-day-ish historical candles
(see resolved ambiguity 3 for the range caveat) processed chronologically
with no lookahead, simulates entry at confirmation close, TP/SL with
same-candle-SL-first, configurable fee/slippage. Prints: total trades,
wins, losses, expired, win rate, gross ROI, estimated fees, net ROI,
average ROI/trade, max consecutive losses, max drawdown, average RR,
LONG/SHORT performance breakdown, per-symbol performance. No automatic
parameter optimization in this pass.

## Dry-run mode

```python
DRY_RUN=true            # default
DRY_RUN_SAVE_SIGNALS=false  # default
```
`DRY_RUN=true, DRY_RUN_SAVE_SIGNALS=false`: evaluate normally, log
candidates, no DB save, no Telegram broadcast — required for safe
first-boot validation before going live.

## Migration order (drives the implementation plan's phases)

1. **Indicators + tests** — helpers, unit tests, verify green.
2. **Strategy** — rewrite `strategy.py`, `evaluate_symbol`, long/short
   tests, completed-candle handling, BTC filter + its tests.
3. **Runtime** — simplify `main.py`, remove OI polling, 5m schedule,
   directional active-signal limits, scoring integration.
4. **User interfaces** — Telegram messages, `/status`, dashboard.
5. **Cleanup** — remove unused imports/config, update `.env.example` and
   `README.md`, delete legacy tests, full suite green, dry-run smoke test.

## Acceptance criteria

Mirrors architecture.txt section 32 verbatim (20 criteria: no
`liq_estimator`/`nw_kernel` imports in `strategy.py`; no OI polling loop in
`main.py`; strategy uses only EMA/RSI/ATR/Supertrend/Volume; 15m trend / 5m
entry; completed candles only; ~0.75% TP / ≤0.50% SL at default config;
structural stops >0.50% rejected; RR<1.5 rejected; max 1 active LONG + 1
active SHORT; max 3 signals/day; scanner runs once per completed 5m candle;
Telegram reflects new strategy; dashboard has no SMC/liquidation
terminology; all tests pass; backtest runs with no future leakage; dry-run
works; `.env.example` has only new relevant settings; README explains the
new strategy and its limitations).

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import database"
python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT --days 30
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py
```
Confirm startup logs show strategy name, trend/entry TF, target ROI, max SL
ROI, leverage, dry-run enabled.
