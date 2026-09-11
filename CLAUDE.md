# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
# Install dependencies (server uses venv/, not .venv/)
source venv/bin/activate
pip install -r requirements.txt

# Run bot
python main.py

# Run dashboard
python webui.py     # http://localhost:6060/?token=<WEBUI_TOKEN>

# Server: managed by systemd
systemctl start|stop|restart mexc-bot
systemctl start|stop|restart mexc-dashboard
journalctl -u mexc-bot -f          # live bot logs
journalctl -u mexc-dashboard -f    # live dashboard logs
tail -f /opt/signals/mexc_bot.log  # file logs
```

## Deployment

- **Server:** Ubuntu 24.04 at `68.168.222.74`, app at `/opt/signals/`, venv at `/opt/signals/venv/`
- **Bot service:** `mexc-bot`
- **Dashboard service:** `mexc-dashboard` — runs `webui.py` on port `6060`
- **Dashboard URL:** `http://68.168.222.74:6060/?token=<WEBUI_TOKEN>`
- **Auto-deploy:** push to `main` → GitHub Actions SSHs in, git pulls, pip installs, restarts both services
- **Workflow file:** `.github/workflows/deploy.yml`
- **DB clear utility:** `python clear_db.py` (or `python clear_db.py --yes` to skip confirm) — after any strategy replacement, restart with `systemctl stop mexc-bot && python clear_db.py --yes && systemctl start mexc-bot` so stale pending setups/signals from the old strategy don't linger
- ⚠️ **Before deploying this strategy version, reconcile the server's `.env` against `config.py`'s current names.** This codebase has been bitten by stale `.env` overrides from retired strategies at least three times — most recently `ATR_PERIOD` (repurposed by the prior pass to size the zero-lag band; a leftover `ATR_PERIOD=14` from the older ATR%-band filter silently changed that indicator's behavior). This pass deliberately gives every new strategy-specific var a distinct name (`RISK_REWARD_RATIO`, `MIN_WIN_ROI_PCT`, `MIN_RISK_PCT`/`MAX_RISK_PCT`, `SL_BUFFER_PCT`, `STRUCTURE_LEFT`/`RIGHT`/`LOOKBACK_BARS`, `SQZ_*`, `WHITELISTED_COINS`, `SIGNAL_SCORE_THRESHOLD`) rather than repurposing `MIN_SIGNAL_SCORE` (0-100 scale there vs 0-10 here), `ATR_PERIOD`, `MACRO_TF`, `PULLBACK_TF`, `ZERO_LAG_*`, `TP_ROI_PCT`/`SL_ROI_PCT` (all still defined, all unused now) — but the GitHub Actions `APP_ENV` secret still needs reconciling for anything you want tuned via `.env` instead of code defaults (a stale `SCAN_INTERVAL_MINUTES=5` from the 5m-entry era, for example, still works fine at 30m entries — just scans more often than necessary).
- ⚠️ **This strategy version has not had a real backtest / walk-forward run either** (ported and reasoned about, not yet validated against historical data — same caveat the prior strategy carried). `DRY_RUN=false` per explicit user decision on first deploy; there is no dry-run observation period backing this yet.

### One-time dashboard service setup (run once on server)
```bash
cat > /etc/systemd/system/mexc-dashboard.service << 'EOF'
[Unit]
Description=MEXC Bot Dashboard
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/signals
ExecStart=/opt/signals/venv/bin/python /opt/signals/webui.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/signals/mexc_bot.log
StandardError=append:/opt/signals/mexc_bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mexc-dashboard
systemctl start mexc-dashboard
systemctl status mexc-dashboard
```

## Architecture

The bot is a single-process async application (`main.py`) with three concerns:

**1. Signal generation** (`strategy.py`)
A single-pass confluence model — no pending-setup state machine (unlike the retired Zero-Lag MTF Pullback v1; the `pending_setups` DB table still exists but is unused by this strategy version). One scheduler job, `scan_and_fire_signals` (every `SCAN_INTERVAL_MINUTES`), resolves `WHITELISTED_COINS` to live contracts via `coin_scanner.get_whitelisted_pairs()`, thread-pools `strategy.detect_signal` across `SCAN_WORKERS`, and fires signals directly within the daily/gap/concurrent/direction limits. `detect_signal` fetches `ENTRY_TF` (30m) and `TREND_TF` (1h) klines (dropping the forming candle via `iloc[:-1]`, gating on `MIN_CANDLE_SETTLE_SECONDS`), requires a market-structure break (`market_structure()` — fractal-pivot BOS/CHoCH, modeled on LuxAlgo's "Smart Money Concepts" indicator) and Squeeze Momentum (`squeeze_momentum()` — LazyBear SQZMOM, BB vs KC compression/release) to agree on direction within `STRUCTURE_LOOKBACK_BARS`/`SQZ_LOOKBACK_BARS`, scores 0-10, and builds SL/TP from the risk model below. Each `detect_signal` call is individually exception-guarded (matching the prior strategy's per-symbol isolation pattern), so one bad symbol can't stall the rest of the pass.

**2. Coin selection** (`coin_scanner.py`)
This strategy bypasses the smart-ranking coin pool entirely — `coin_scanner.get_whitelisted_pairs()` resolves the hardcoded `WHITELISTED_COINS` (SOL/BNB/XRP/DOGE/ADA by default) to live, active `_USDT` contracts, deliberately *not* applying `EXCLUDE_COINS` (which by default still contains `SOL_USDT`, meant for the old ranked-pool strategy) or `_is_crypto_symbol` — the whitelist is already an explicit, curated inclusion list. The rest of `coin_scanner.py` (smart ranking, `refresh_coin_list`, `get_cached_coins`, etc.) remains intact but unused; the dashboard's coin-pool widget will show empty since that refresh job was removed from the scheduler.

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every `OUTCOME_CHECK_MINUTES` (default 1). For each `pending` DB signal, fetches recent `ENTRY_TF` candles and calls `outcome_check.check_tp_sl()` — a candle-by-candle walk against the fixed single TP/SL, SL checked before TP so a single wild candle spanning both is conservatively treated as a full loss. No breakeven step in this strategy version. Marks `win`, `loss`, or `expired` after `SIGNAL_EXPIRE_HOURS` (6h TTL), then sends a Telegram notification — gated by `DRY_RUN` the same way entry broadcasts are, so dry-run mode never talks to Telegram. Unchanged from the prior strategy version.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/start /help /status /pause /resume /daily /weekly /monthly /stats`. `notify_outcome` renders `win`/`loss`/`expired` — no breakeven branch. The `Application` object is passed into scheduler jobs as an argument so they can send messages. Unchanged from the prior strategy version.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, expired}` (the `breakeven` status value still exists in historical rows from retired strategies and every stats/report consumer still handles it generically, but nothing in the live code path writes it anymore), plus generic columns this strategy reuses as-is: `strategy_name`, `score`, `rr`, `entry_timeframe`, `trend_timeframe`, `setup_reason`. Many columns from retired strategies (`tp1_price`, `tp2_price`, `trend`, `strength`, `ao`, `kc_pos`, `regime`, `score_macro`, `score_breakout_freshness`, `diag_*`, etc.) remain in the schema, unwritten by this version — no migration was done when those strategies were removed, matching this repo's established convention. The `pending_setups` table also remains, unused by this strategy version (three-state pending-breakout machinery, specific to Zero-Lag MTF Pullback v1 — see `backup/zero-lag-mtf-pullback-v1`).

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `ENTRY_TF` | 30m | Structure break (BOS/CHoCH) + squeeze momentum are both evaluated here |
| `TREND_TF` | 1h | EMA50 alignment bonus (not a hard filter) |
| `ENTRY_KLINE_COUNT` / `TREND_KLINE_COUNT` | 150 / 100 | Kline fetch depth for the two timeframes |
| `WHITELISTED_COINS` | SOL,BNB,XRP,DOGE,ADA | Hardcoded coin universe — resolved to live `_USDT` contracts by `coin_scanner.get_whitelisted_pairs()`, bypassing the ranked pool entirely. BTC/ETH stay excluded via `EXCLUDE_COINS` |
| `RISK_REWARD_RATIO` | 2.0 | Fixed 1:2 reward:risk on every signal — TP is always exactly this multiple of the risk distance, never a separate fixed-%/structural value |
| `MIN_WIN_ROI_PCT` | 30.0 | Every winning trade must return at least this much ROI on capital at `LEVERAGE`; derives `MIN_RISK_PCT = MIN_WIN_ROI_PCT / (LEVERAGE * RISK_REWARD_RATIO)` — the structure-derived stop is widened up to this floor if the raw swing distance is tighter |
| `MAX_LOSS_ROI_PCT` | 40.0 | Caps how much a losing trade can cost; derives `MAX_RISK_PCT = MAX_LOSS_ROI_PCT / LEVERAGE` — signals whose structure-derived stop is wider than this are rejected (`risk_too_wide`), not clamped |
| `SL_BUFFER_PCT` | 0.15% | Small buffer added beyond the swing point used for SL, so price doesn't get stopped exactly at the wick |
| `STRUCTURE_LEFT` / `STRUCTURE_RIGHT` | 2 / 2 | Bars required on each side to confirm a fractal swing pivot |
| `STRUCTURE_LOOKBACK_BARS` | 6 | A BOS/CHoCH break must have occurred within the last N closed bars to still count as fresh (~3h at 30m entries; widened from an initial 2 after live logs showed real breaks on the whitelist commonly sitting 4-13 bars old, missing the original 1h window entirely) |
| `SQZ_BB_LENGTH` / `SQZ_BB_MULT` | 20 / 2.0 | Bollinger Band params for squeeze detection — matches the live TradingView SQZMOM_LB chart settings |
| `SQZ_KC_LENGTH` / `SQZ_KC_MULT` | 20 / 1.5 | Keltner Channel params for squeeze detection — same chart settings |
| `SQZ_LOOKBACK_BARS` | 8 | The squeeze must have fired (released) within the last N closed bars (~4h at 30m entries; widened alongside `STRUCTURE_LOOKBACK_BARS`) |
| `SIGNAL_SCORE_THRESHOLD` | 7.0 | 0–10 composite score gate (break quality + squeeze freshness + momentum strength + 1h trend + volume) — **unvalidated against real data**, deliberately distinct from the retired `MIN_SIGNAL_SCORE` (0–100 scale, different strategy) |
| `LEVERAGE` | 20 | Bot's own position leverage |
| `MIN_CANDLE_SETTLE_SECONDS` | 90 | Last closed `ENTRY_TF` candle must be at least this old before it's used — MEXC's kline data for a just-closed candle can still get revised shortly after close. Unlike the prior strategy, `scan_and_fire_signals` runs on a plain `IntervalTrigger` with no cron settle-offset trick — at 30m entries with a 15m scan cadence there's enough headroom that this simple gate inside `detect_signal` suffices |
| `ENABLE_LONG_SIGNALS` | true | Both directions live by default (checked via `direction_slot_available`, not currently gated inside `detect_signal` itself — carried over from the prior strategy's config but not yet wired to a LONG-disable check in this version) |
| `MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` | 2 / 2 | Correlation limit — active signals per direction |
| `MAX_CONCURRENT_SIGNALS` | 4 | Total active signals across both directions |
| `MAX_DAILY_SIGNALS` | 12 | Signals fired per day — user's stated target for this strategy is 1-2/day; unvalidated until it runs live, this cap is just the ceiling |
| `MIN_DAILY_SIGNAL_GAP_MINUTES` | 60 | Minimum gap between fired signals |
| `SIGNAL_COOLDOWN_MINUTES` | 240 | Same coin blocked for 4h after a signal |
| `SIGNAL_EXPIRE_HOURS` | 6 | Fired (pending) signals auto-expire if TP/SL never hit |
| `SCAN_INTERVAL_MINUTES` | 15 | `scan_and_fire_signals` cadence (a stale `.env`/`APP_ENV` override of `5` from the prior 5m-entry strategy is harmless at 30m entries, just more frequent than necessary) |
| `EXCLUDE_COINS` | BTC/ETH/SOL/XAUT | Applied by the *ranked-pool* path only (`coin_scanner._is_contract_active`); `get_whitelisted_pairs()` deliberately does not apply it, since it would otherwise silently drop `SOL_USDT` from the whitelist |

Retired (Zero-Lag MTF Pullback v1) config vars — `MACRO_TF`, `PULLBACK_TF`,
`MACRO_KLINE_COUNT`, `PULLBACK_KLINE_COUNT`, `ZERO_LAG_*`, `ATR_PERIOD`,
`PULLBACK_DISTANCE_PCT`, `PENDING_EXPIRY_CANDLES`, `MIN_SIGNAL_SCORE`,
`TP_ROI_PCT`/`SL_ROI_PCT`/`TP_PRICE_PCT`/`SL_PRICE_PCT`,
`MONITOR_INTERVAL_MINUTES`, `COIN_REFRESH_HOURS` — remain defined in
`config.py` but unused by this strategy version, per this repo's
established no-migration convention. See `backup/zero-lag-mtf-pullback-v1`.

## Signal Logic (strategy.py) — SMC Structure + Squeeze Momentum v1

Single-pass confluence model, no pending-setup state machine. Modeled on
the LuxAlgo "Smart Money Concepts" (market structure) + LazyBear
"Squeeze Momentum" (SQZMOM_LB) indicator pair the user runs manually on
their live TradingView chart (30m SUI/USDT-MEXC, settings 20/2/20/1.5).
SL/TP are structure-derived and risk-floored/capped, not a fixed
ROI-%-at-leverage distance — RR is always exactly `RISK_REWARD_RATIO`
(1:2 by default). There's no breakeven step. Not yet backtested/
walk-forward validated (see the DRY_RUN caveat above).

```
strategy.detect_signal(symbol, reject_sink=None):
  0. Fetch ENTRY_TF and TREND_TF klines separately, each dropping the
     forming candle via iloc[:-1]. Reject (missing_data /
     insufficient_history) if either is empty or too short for the
     squeeze/structure warmup. Reject (candle_not_settled) if the last
     closed ENTRY_TF candle is younger than MIN_CANDLE_SETTLE_SECONDS.

  1. squeeze_momentum(df): LazyBear SQZMOM -- BB(SQZ_BB_LENGTH,
     SQZ_BB_MULT) vs KC(SQZ_KC_LENGTH, SQZ_KC_MULT) compression (sqz_on),
     release (sqz_off), and a rolling-linreg momentum value (sqz_mom).
     Reject (insufficient_history) if sqz_mom hasn't warmed up.

  2. market_structure(df): fractal swing pivots (STRUCTURE_LEFT/RIGHT
     bars each side) -> BOS (trend continuation) / CHoCH (trend reversal)
     break events, each carrying the broken swing price and the opposite
     side's most recent swing (used for the stop below). Reject
     (no_structure_break) if no event exists, (structure_break_stale) if
     the most recent one is older than STRUCTURE_LOOKBACK_BARS, or
     (no_stop_reference) if there's no opposite-side swing yet.

  3. Direction requires the break and momentum sign to agree (bullish
     break + mom > 0 -> LONG; bearish break + mom < 0 -> SHORT) ->
     otherwise momentum_disagrees.

  4. Score 0-10: break quality (CHoCH=3 / BOS=2), squeeze-fire freshness
     (fired this candle=3 / within SQZ_LOOKBACK_BARS=1.5), momentum
     strengthening in direction (+2), TREND_TF EMA50 alignment (+1),
     volume >= 1.3x its 20-bar average (+1). Reject (score_below_min) if
     total < SIGNAL_SCORE_THRESHOLD.

  5. Risk model: SL = opposite swing price -/+ SL_BUFFER_PCT. Reject
     (invalid_structure_risk) if that's non-positive, (risk_too_wide) if
     the resulting risk_pct > MAX_RISK_PCT. Otherwise risk_pct is floored
     to MIN_RISK_PCT (widening the stop, never tightening it), and
     TP = entry +/- risk * RISK_REWARD_RATIO. Reject (invalid_geometry)
     if valid_trade_geometry() fails.

Returns a fully-built Signal directly -- no intermediate pending state.
```

`main.scan_and_fire_signals` (every `SCAN_INTERVAL_MINUTES`) resolves
`WHITELISTED_COINS` via `coin_scanner.get_whitelisted_pairs()`,
thread-pools `detect_signal` across `SCAN_WORKERS`, sorts candidates by
score, and fires them in order within `MAX_DAILY_SIGNALS`,
`MIN_DAILY_SIGNAL_GAP_MINUTES`, `MAX_CONCURRENT_SIGNALS`, per-coin
`SIGNAL_COOLDOWN_MINUTES`, and `direction_slot_available()` (the
`MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` correlation
limit) — same budget-check pattern the prior strategy used.

### Outcome checking (`outcome_check.check_tp_sl`)

Walks `ENTRY_TF` candles after entry. Each candle, in order: (1) SL —
if hit, closes `loss`; (2) TP — if hit, closes `win`. SL checked before
TP so a single wild candle spanning both is conservatively treated as a
full loss, matching the SL-first tie-break convention used throughout
this bot. No breakeven step in this strategy version. Unchanged from the prior
strategy version -- `check_tp_sl` itself is strategy-agnostic (plain
direction/entry/SL/TP/df/cutoff arguments).

Not part of this strategy version (retired, not deleted -- see
`backup/zero-lag-mtf-pullback-v1`): Zero-Lag MTF Pullback v1's four-
timeframe pending-setup pipeline (`detect_pending_setup`,
`check_setup_confirmation`, the `pending_setups` table, the settle-offset
cron trick in `main.py`) and its entry-quality diagnostics
(`_entry_quality_diagnostics`, the `diag_*`/`score_*` signals columns).
From strategies before that (retired and deleted): Precision Pullback
Scalper v1's EMA20/50/200 + RSI14 pipeline and its `armed_setups` table,
`outcome_check.check_tp_sl_with_breakeven`, `liq_estimator.py`
liquidation-cluster filter, Super Scalper v3 (`super_scalper_v3.py` /
`scalper_v3_strategy.py`), `nw_kernel.py`, the 6-EMA ribbon and
Chandelier/PVT/dual-RSI trigger, the 3-target partial-exit ladder and its
`check_target_ladder` walker, and VWAP/multi-timeframe "strict mode"
confirmation. `outcome_replay.py` still exists in the repo but has no
caller in this strategy version. `scripts/backtest_simple_strategy.py`
was written for Zero-Lag MTF Pullback v1's four-timeframe/three-state
pipeline and has not been adapted for this strategy version's single-pass
model yet.

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
