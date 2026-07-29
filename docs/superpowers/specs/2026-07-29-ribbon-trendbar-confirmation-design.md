# Ribbon-Flip + Trend-Bar Confirmation v1 — Design Spec

Date: 2026-07-29
Source: same Pine Script indicator as before (`D:\Downloads\2- Binocular Trend ( PAID ).txt`), this time porting the 6-EMA ribbon alignment logic (`longCond`/`shortCond`/`CondIni`, present in the code but never wired into a signal) and the "Trend Bar" Price-Action-Channel indicator, per the user's own manual trading rule described directly from a TradingView screenshot. This design **completely replaces** the current Binocular Trend Confluence v1 strategy (deployed 2026-07-27) — it fired ~0 signals live and, once loosened, showed a fragile LONG/SHORT win-rate asymmetry (30.8% vs 16.7% over a 44-trade, 6-month/10-symbol backtest). No zone-confluence, Chandelier/PVT/dual-RSI, or BTC-filter behavior is preserved.

## Objective

Replace the active strategy with the user's actual trading process:

- **"Arrow 1" — ribbon flip**: a 6-EMA ribbon (5 short EMAs at lengths 30/35/40/45/50, one baseline EMA at length 60, all of `close`) flips fully bullish (all 5 short EMAs cross above the baseline) or fully bearish (cross below)
- **"Arrow 2" — Trend Bar confirmation**: on a later bar (bounded lookback), a Price-Action-Channel "Trend Bar" also confirms the same direction — green when a candle's entire range sits above `EMA(high, 50)`, red when entirely below `EMA(low, 50)`
- If the ribbon reverts to the opposite alignment before the Trend Bar ever confirms, the setup is invalidated (no signal)
- Single 15m timeframe. No BTC filter, no zone confluence, no Chandelier/PVT/RSI — full replace.

## Preparation

1. Backup branch `backup/binocular-trend-confluence-v1` cut from current `main` HEAD (this is what's live on the server right now — commit `bd777e9` or later, whatever `main` is at when work starts), pushed to `origin`.
2. Implementation on a new branch/worktree off `main`.

## Components removed from the active runtime

From `strategy.py`: `build_zones`, `find_pivot_highs`, `find_pivot_lows`, `_find_confluence_zone`, `calculate_chandelier_exit`, `calculate_pvt`, `calculate_pvt_signal`, `_detect_trigger`, `build_btc_context`, `_btc_filter_ok`, `BtcContext` dataclass, and the current `_calculate_tp_sl`/`_score_candidate`/`_reason_bucket`/`evaluate_symbol` bodies (rewritten, see below).

From `config.py`: `TREND_TF`, `TREND_KLINE_COUNT`, `TREND_EMA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`, `TREND_SUPERTREND_MULTIPLIER`, `ENABLE_BTC_FILTER`, `BTC_FILTER_SYMBOL`, `BTC_FILTER_TF`, `BTC_MAX_OPPOSING_MOVE_PCT`, `BTC_MAX_SINGLE_CANDLE_MOVE_PCT`, `BTC_MAX_THREE_CANDLE_MOVE_PCT`, `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `PVT_SIGNAL_LENGTH`, `PVT_SIGNAL_TYPE`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `ZONE_SWING_LENGTH`, `ZONE_ATR_PERIOD`, `ZONE_BOX_WIDTH`, `ZONE_PROXIMITY_ATR_MULT`, `ZONE_MAX_AGE_BARS`, `ENTRY_BUFFER_PCT`.

**Keep** `calculate_ema`, `calculate_rsi`, `calculate_atr`, `calculate_supertrend` in `strategy.py` even though the main pipeline no longer calls all of them: `calculate_atr` is still used internally (structural SL buffer); `calculate_supertrend` is imported directly by `backtest/engine.py`, a self-contained backtester for the unrelated "Super Scalper v3" strategy — breaking that import for zero benefit is not worth it; `calculate_rsi` has generic test coverage (`tests/test_indicators.py`) and costs nothing to leave. None of these four functions have any coupling to the code being removed.

`SL_ATR_BUFFER_MULTIPLIER`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`, `LEVERAGE`, `TP_PRICE_PCT`, `MAX_SL_PRICE_PCT`, `MIN_RR`, and every scan/coin-pool/cooldown/expiry config constant are **unchanged**.

## New strategy: indicators

All computed on a single timeframe (`ENTRY_TF`, default changes to `15m`).

**6-EMA ribbon** (Pine defaults, exact):
```python
RIBBON_MA1_LEN = 30
RIBBON_MA2_LEN = 35
RIBBON_MA3_LEN = 40
RIBBON_MA4_LEN = 45
RIBBON_MA5_LEN = 50
RIBBON_BASELINE_LEN = 60   # "MA6" in the Pine script
```
`ma1..ma5 = calculate_ema(close, len)` for each length above; `baseline = calculate_ema(close, RIBBON_BASELINE_LEN)`.

**Price-Action-Channel Trend Bar**:
```python
TREND_BAR_PAC_LENGTH = 50
```
`pac_hi = calculate_ema(high, TREND_BAR_PAC_LENGTH)`, `pac_lo = calculate_ema(low, TREND_BAR_PAC_LENGTH)`. Per bar: `"green"` if `low > pac_hi and high > pac_hi`; `"red"` if `high < pac_lo and low < pac_lo`; else `"gray"` — ported directly from the Pine script's `wcolor` expression.

**Ribbon-flip lookback**:
```python
RIBBON_LOOKBACK_BARS = 12
```

Required new helper functions in `strategy.py`:
```python
calculate_ema_ribbon(df) -> pd.DataFrame   # columns ma1..ma5, baseline
calculate_trend_bar(df, pac_length) -> pd.Series   # "green" | "red" | "gray" per bar
```

## Ribbon-flip detection (stateless, single-pass)

Ported from the Pine script's `longCond`/`shortCond`/`CondIni` logic, but reframed to avoid persisted state (the bot's architecture evaluates fresh each scan, per `CLAUDE.md`):

```python
def _detect_ribbon_flip(df, lookback_bars) -> tuple[str | None, int | None]:
    """Returns (direction, flip_index) or (None, None).

    direction is the CURRENT bar's ribbon alignment (bullish/bearish), only
    returned if that alignment began (flipped from the opposite state) within
    the last `lookback_bars` bars and has held continuously since. flip_index
    is the positional index of the flip bar, used later to size the
    structural stop.
    """
```
Algorithm: compute `bullish[i] = ma1[i]>baseline[i] and ma2[i]>baseline[i] and ma3[i]>baseline[i] and ma4[i]>baseline[i] and ma5[i]>baseline[i]` (and the mirrored `bearish[i]`) for the full closed series. At the last index `n-1`: if `bullish[n-1]`, walk backward while `bullish[j]` holds, up to `lookback_bars` steps; if a `j` is found where `bullish[j] and not bullish[j-1]` (a genuine flip-in), return `("LONG", j)`. Mirror for `bearish[n-1]` → `("SHORT", j)`. If neither the current bar's ribbon is aligned, or no flip-in is found within the lookback window (ribbon has been aligned longer than the window, or isn't aligned at all), return `(None, None)`.

This single walk-backward naturally implements "reject if ribbon reverted before confirmation": if the ribbon flips bullish, sits for a few bars, then flips back to bearish, the *next* time it flips bullish again starts a **new** `flip_index` — there is no way for a stale, already-reverted bullish state to still register, because `bullish[n-1]` (the current bar) has to be true for the search to run at all.

## Entry rule (replaces `_detect_trigger` / zone confluence)

On the latest closed bar:
1. `_detect_ribbon_flip(df, RIBBON_LOOKBACK_BARS)` → `(direction, flip_index)`. If `direction is None`, reject (`no_ribbon_flip`).
2. `calculate_trend_bar(df, TREND_BAR_PAC_LENGTH)` at the current bar must equal `"green"` for LONG / `"red"` for SHORT. If not, reject (`no_trend_bar_confirmation`).
3. If both hold, direction confirmed.

No RSI, no PVT, no zone, no BTC filter.

## Entry / TP / SL

```python
entry_price = latest_closed_close
tp_price_pct = TARGET_ROI_PCT / 100 / LEVERAGE   # unchanged, 0.75%
```
LONG: `tp = entry * (1 + tp_price_pct)`; SHORT mirrored.

Structural SL: the swing extreme over the window from `flip_index` to the current (confirmation) bar, inclusive — `min(low[flip_index : n])` for LONG, `max(high[flip_index : n])` for SHORT — minus/plus an ATR buffer (`SL_ATR_BUFFER_MULTIPLIER`, unchanged), capped at `MAX_SL_PRICE_PCT`. Same shape as every prior version's structural-stop pattern, just fed by the ribbon-flip window instead of a pullback window or a zone. If the structural stop exceeds the cap, reject (`stop_too_wide`) — never tighten artificially.

RR validation: `rr = reward / risk`, reject if `< MIN_RR`. `valid_trade_geometry` unchanged.

## `evaluate_symbol` (signature and dataclasses unchanged)

```python
def evaluate_symbol(symbol: str, btc_context=None, reject_sink: dict | None = None) -> Signal | None
```
`btc_context` parameter **stays in the signature** (accepted but ignored/unused) — `main.py`, `bot.py`, tests, and the backtest script all call `evaluate_symbol` positionally/by-keyword in ways that assume this parameter exists; removing it is a wider, unnecessary blast radius for zero behavioral benefit. It simply has no effect now (no BTC filter exists to use it). `Signal`/`BtcContext` dataclasses: `Signal` unchanged; `BtcContext` dataclass itself is deleted (nothing constructs one anymore), but the parameter name/type-hint stays as `btc_context=None` (loosen the type hint to `object | None` or just remove the specific `BtcContext` type annotation since the class no longer exists).

Pipeline: fetch single-timeframe candles via `get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)` → drop forming candle → validate history length (`RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10` at minimum, covering both the baseline EMA's warm-up and the flip lookback) → `_detect_ribbon_flip` → `calculate_trend_bar` confirmation → `_calculate_tp_sl` from the flip window → `valid_trade_geometry` → RR gate → `_roi_pct` → `_score_candidate` → `Signal`.

## Candidate scoring (0–100)

Simpler than before — two real signals, not five:
- **Ribbon alignment strength (40)**: how far the ribbon has separated from the baseline, normalized by ATR — e.g. `min(1.0, abs(ma5[n-1] - baseline[n-1]) / (ATR * 2))`, scaled to 40
- **Flip freshness (20)**: `1.0 - (n-1 - flip_index) / RIBBON_LOOKBACK_BARS`, scaled to 20 — a confirmation on the bar right after the flip scores higher than one that took most of the lookback window
- **Trend Bar strength (20)**: how far the candle's range extends beyond the PAC channel edge, normalized by ATR, scaled to 20
- **RR quality (20)**: same shape as every prior version — `MIN_RR` floor, `2×MIN_RR` ceiling

## Reject-reason buckets

`_reason_bucket` simplifies to: `no_ribbon_flip`, `no_trend_bar_confirmation`, plus the existing `stop_too_wide`, `rr_below_min`, `invalid_geometry`, `missing_data`, `insufficient_history`, `error` (all unchanged shape, `_bump`/`reject_sink` untouched).

## Configuration (`config.py`)

```python
STRATEGY_NAME default -> "Ribbon-Flip Trend-Bar Confirmation v1"
ENTRY_TF default -> "15m"   # single timeframe now
ENTRY_KLINE_COUNT stays (still the fetch window size)

RIBBON_MA1_LEN = 30
RIBBON_MA2_LEN = 35
RIBBON_MA3_LEN = 40
RIBBON_MA4_LEN = 45
RIBBON_MA5_LEN = 50
RIBBON_BASELINE_LEN = 60
RIBBON_LOOKBACK_BARS = 12
TREND_BAR_PAC_LENGTH = 50
```
All other existing config (`LEVERAGE`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`, `MIN_RR`, `SL_ATR_BUFFER_MULTIPLIER`, `MAX_ACTIVE_LONG_SIGNALS`/`SHORT`, `MAX_CONCURRENT_SIGNALS`, `MAX_DAILY_SIGNALS`, `SIGNAL_COOLDOWN_MINUTES`, `SIGNAL_EXPIRE_HOURS`, coin pool settings, `DRY_RUN*`) unchanged.

## Fixes required outside `strategy.py` (all confirmed by direct cross-reference search — not guessed)

- **`main.py`**: drop `TREND_TF` from the top-level config import (`main.py:38-79`); drop the `btc_context = strategy.build_btc_context()` call (`main.py:149`) and any downstream pass-through of it into the per-symbol scan calls; `save_signal(..., trend_timeframe=TREND_TF)` (`main.py:238`) → pass `trend_timeframe=ENTRY_TF` instead (same value as `entry_timeframe`, no DB schema change — the column stays, just always mirrors the single timeframe now); startup log line `logger.info("Trend TF: %s", TREND_TF)` (`main.py:529`) → remove (only `Entry TF` line remains).
- **`bot.py`**: `cmd_status`'s import block (`bot.py:208-223`) drops `TREND_TF`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`; adds `RIBBON_BASELINE_LEN`, `RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`. Message lines (`bot.py:244-246`, currently "Zone TF" / "Trigger TF" / "RSI regime") → replace with something like `f"TF: {_code(ENTRY_TF)} (Ribbon 30/35/40/45/50 vs {RIBBON_BASELINE_LEN})"` and `f"Confirm: {_code(f'Trend Bar within {RIBBON_LOOKBACK_BARS} bars')}"`.
- **`webui.py`**: `get_strategy_config()` (`webui.py:232-267`) drops `trend_tf` (`webui.py:236`) and `enable_btc_filter` (`webui.py:264`) plus every Chandelier/PVT/RSI/zone field from the last migration, adds the new `RIBBON_*`/`TREND_BAR_PAC_LENGTH` fields. The inline dashboard JS (`webui.py:973,975,976` — `cfg-tf`, `cfg-confirm`, `cfg-confirm-sub`) reads `c.trend_tf`/`c.enable_btc_filter`/the old Chandelier/PVT fields directly — **this must be fixed in the same change**, not left for a later final-review catch (that exact gap slipped through review once already in this codebase's history). New JS should read whatever new fields Python now returns, and the "BTC filter" card either gets repurposed (e.g. show ribbon/trend-bar state) or removed from the dashboard layout — implementer's call, but it must not silently render `undefined`.
- **`mexc_ws_client.py`**: `run_ws_test()` dev helper (`mexc_ws_client.py:396,419`) imports `TREND_TF` and includes it in `app_intervals=[ENTRY_TF, TREND_TF]` — drop `TREND_TF`, keep just `[ENTRY_TF]`. Not exercised by the test suite or the live bot; low risk, but it would `ImportError` the moment someone runs `python mexc_ws_client.py` manually.
- **`scripts/backtest_simple_strategy.py`**: needs a genuine rewrite, not a find-replace — its entire design fakes `strategy.get_market_klines` per-interval across three routes (15m, 5m, BTC). New version: single-timeframe fetch (`get_klines_extended(symbol, ENTRY_TF, days)`), the faked `get_market_klines` only needs one branch (`interval == ENTRY_TF`), no BTC dataframe fetch/pass-through in `main()`/`backtest_symbol()`, `min_start` recomputed as `RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10`, module docstring and argparse `description` updated to the new strategy name. Net effect: simpler than the version it replaces.
- **`backtest/engine.py`**: no change — it only imports `calculate_supertrend`, which stays in `strategy.py` per the "components removed" section above. Verify at implementation time this import still resolves; do not touch this file otherwise (unrelated Super Scalper v3 tooling, out of scope).
- **`database.py`**: no schema change. `trend_timeframe` column stays; it will just always hold the same value as `entry_timeframe` going forward (both `ENTRY_TF`) rather than a genuinely distinct trend timeframe. Acceptable — not worth a migration for a display-only column.

## Testing

New `tests/test_ribbon_trendbar_indicators.py`:
- `test_ema_ribbon_returns_all_six_series`
- `test_trend_bar_green_when_candle_above_channel`
- `test_trend_bar_red_when_candle_below_channel`
- `test_trend_bar_gray_when_candle_straddles_channel`
- `test_trend_bar_does_not_use_future_data` (same shape as the existing Chandelier/Supertrend no-lookahead tests)
- `test_detect_ribbon_flip_finds_recent_bullish_flip`
- `test_detect_ribbon_flip_finds_recent_bearish_flip`
- `test_detect_ribbon_flip_rejects_when_flip_outside_lookback_window`
- `test_detect_ribbon_flip_rejects_when_ribbon_not_currently_aligned`
- `test_detect_ribbon_flip_finds_latest_flip_after_a_revert` (ribbon flips bullish, reverts to bearish, flips bullish again — must return the *second* flip's index, not the first)

New `tests/test_strategy_ribbon_trendbar.py`: long + short `test_*_signal_valid`, `test_*_rejected_without_ribbon_flip`, `test_*_rejected_without_trend_bar_confirmation`, `test_*_rejected_when_ribbon_reverts_before_confirmation`, `test_*_rejected_when_stop_too_wide`, `test_*_rejected_when_rr_too_low`; `test_active_last_candle_is_ignored`; `test_long_trade_geometry`, `test_short_trade_geometry`, `test_invalid_geometry_rejected` (reused verbatim); `test_risk_formula_matches_roi_targets` (reused verbatim, still strategy-agnostic).

**Legacy test cleanup**: `tests/test_btc_filter.py` — delete entirely (BTC filter no longer exists). `tests/test_binocular_indicators.py`, `tests/test_strategy_binocular.py` — delete entirely (all functions they test are removed). `tests/test_indicators.py` — unaffected, keep as-is (`calculate_supertrend`/`calculate_rsi`/`calculate_atr`/`calculate_ema` all still exist). `tests/strategy_fixtures.py` — `make_15m_zone_df`/`make_5m_trigger_df` become unused (only `test_strategy_binocular.py`/`test_btc_filter.py` used them, both deleted); remove those two functions, add new fixture builders for ribbon/trend-bar test data. `make_15m_trend_df`/`make_5m_pullback_df` were already removed in a prior migration — confirm they're still gone, not resurrected.

## Backtest utility

`scripts/backtest_simple_strategy.py` rewritten per above. Re-run against a handful of symbols once implemented, then — given the last two backtests' results — a **wider validation pass is warranted before this goes live**: several symbols, several months, before flipping `STRATEGY_V1_ENABLED=true` on the server again.

## Migration order (drives the implementation plan's phases)

1. **Backup** — cut `backup/binocular-trend-confluence-v1` branch, push.
2. **Indicators + tests** — `calculate_ema_ribbon`, `calculate_trend_bar`, `_detect_ribbon_flip`, unit tests, verify green.
3. **Strategy** — rewrite `strategy.py`'s pipeline (`_calculate_tp_sl`, `_score_candidate`, `_reason_bucket`, `evaluate_symbol`), delete the zone/Chandelier/PVT/BTC-filter code, long/short tests.
4. **Config** — remove old settings, add ribbon/trend-bar settings, update `STRATEGY_NAME`/`ENTRY_TF` default, `.env.example`.
5. **Dependents** — fix `main.py`, `bot.py`, `webui.py` (Python **and** JS), `mexc_ws_client.py`.
6. **Backtest script** — rewrite for single-timeframe, no-BTC.
7. **Cleanup** — delete superseded tests, full suite green, backtest smoke run, dry-run boot check.

## Acceptance criteria

- No references to `build_zones`, `_detect_trigger`, `calculate_chandelier_exit`, `calculate_pvt`, `build_btc_context`, `_btc_filter_ok`, `BtcContext` remain anywhere in the active codebase (`calculate_supertrend`/`calculate_rsi`/`calculate_atr`/`calculate_ema` correctly still remain — used elsewhere)
- `_detect_ribbon_flip` and `calculate_trend_bar` operate only on completed candles (no forming-candle access), verified by an explicit no-lookahead test
- Every accepted signal satisfies `valid_trade_geometry`, `rr >= MIN_RR`, structural SL distance `<= MAX_SL_ROI_PCT/100/LEVERAGE`
- `evaluate_symbol` signature and `Signal` dataclass unchanged; `BtcContext` dataclass removed
- `main.py`, `bot.py`, `webui.py` (Python and JS), `mexc_ws_client.py`, `scripts/backtest_simple_strategy.py` all updated and verified — none left referencing removed config constants
- `backtest/engine.py` untouched and still imports cleanly
- All tests pass; backtest script runs with no future-data leakage against real symbols; dry-run boots cleanly and logs the new strategy name and ribbon/trend-bar config
- `backup/binocular-trend-confluence-v1` branch exists on `origin`

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import webui; import database; import backtest.engine"
python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT WLD_USDT --days 60
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py
```
