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
- ⚠️ **Before deploying this strategy version, reconcile the server's `.env` against `config.py`'s current names.** This codebase has been bitten by stale `.env` overrides from retired strategies at least three times — most recently `ATR_PERIOD` (repurposed by this pass to size the zero-lag band; a leftover `ATR_PERIOD=14` from the old ATR%-band filter silently changes the core indicator's behavior). Diff the server's `.env` against `config.py`'s current `os.getenv(...)` names before restarting the service; nothing in the codebase warns about unknown/dead env keys.
- ⚠️ **This strategy version's actual 6-month backtest / walk-forward run has not happened yet** (deliberately deferred — see the design spec). Keep `DRY_RUN=true` until that validation lands.

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
A three-state pending-breakout model, persisted in the `pending_setups` DB table (`pending_pullback` → `pending_breakout` → `fired`/`expired`/`missed`). Two independent scheduler jobs drive it: `scan_for_new_setups` (every `SCAN_INTERVAL_MINUTES`, offset far enough into each candle period to clear `MIN_CANDLE_SETTLE_SECONDS` on the `PULLBACK_TF` candle — see the config table note below) scans the coin pool for new setups via `detect_pending_setup`, which requires `MACRO_TF`/`TREND_TF` zero-lag trend state to agree and `PULLBACK_TF` price to have pulled back to its own ZLEMA. `monitor_pending_setups` (every `MONITOR_INTERVAL_MINUTES`, a plain 1-minute interval — no settle-offset needed for *this* job's own cadence, but `check_setup_confirmation` still gates on `MIN_CANDLE_SETTLE_SECONDS` for the `ENTRY_TF` candle it evaluates) checks every currently-armed setup for the `ENTRY_TF` ZLEMA-crossover confirmation, then the breakout-trigger fill; confirmed setups fire within the daily/gap/concurrent/direction limits. Both jobs fetch klines separately per timeframe and always drop the last (still-forming) bar via `iloc[:-1]`, never evaluating an in-progress candle. `monitor_pending_setups`'s per-setup confirmation checks are thread-pooled across `SCAN_WORKERS` (matching `scan_for_new_setups`'s pattern) and individually exception-guarded, so one bad symbol (e.g. delisted) can't stall the rest of the pass.

**2. Coin selection** (`coin_scanner.py`)
Fetches zero-fee USDT perpetual contracts from MEXC, optionally smart-ranks them by liquidity/volatility/trend/liquidity score (`ENABLE_SMART_COIN_RANKING`), and caches the top `TOP_N_COINS` (80, backfilled to at least `COIN_POOL_MIN_SELECTED`). Refreshed every `COIN_REFRESH_HOURS` (6h) via scheduler. Excludes `EXCLUDE_COINS` (BTC/ETH/SOL/XAUT by default).

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every `OUTCOME_CHECK_MINUTES` (default 1). For each `pending` DB signal, fetches recent `ENTRY_TF` candles and calls `outcome_check.check_tp_sl()` — a candle-by-candle walk against the fixed single TP/SL, SL checked before TP so a single wild candle spanning both is conservatively treated as a full loss. No breakeven step in this strategy version. Marks `win`, `loss`, or `expired` after `SIGNAL_EXPIRE_HOURS` (6h TTL), then sends a Telegram notification — gated by `DRY_RUN` the same way entry broadcasts are, so dry-run mode never talks to Telegram.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/start /help /status /pause /resume /daily /weekly /monthly /stats`. `notify_outcome` renders `win`/`loss`/`expired` — no breakeven branch (nothing in this strategy version produces that status). The `Application` object is passed into scheduler jobs as an argument so they can send messages.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, expired}` (the `breakeven` status value still exists in historical rows from retired strategies and every stats/report consumer still handles it generically, but nothing in the live code path writes it anymore), plus columns for `strategy_name`, `score`, `rr`, `entry_timeframe`, `trend_timeframe`, `setup_reason`, `breakeven_triggered_at`. Several columns from retired strategies (`tp1_price`, `tp2_price`, `trend`, `strength`, `ao`, `kc_pos`, `regime`, etc.) remain in the schema — no migration was done when those strategies were removed — but nothing in the live code path writes them anymore. `init_db()` also creates the `pending_setups` table, which this strategy's three-state pending-breakout pipeline actively reads and writes (see "Signal generation" above) — this replaced the prior strategy's `armed_setups` table entirely (dropped, not migrated).

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `MACRO_TF` | 4h | Broadest trend gate — zero-lag trend state must be non-neutral and agree with `TREND_TF` |
| `TREND_TF` | 1h | Must agree with `MACRO_TF`'s zero-lag trend state; disagreement rejects the whole coin |
| `PULLBACK_TF` | 15m | Price must have pulled back to within `PULLBACK_DISTANCE_PCT` of its own ZLEMA here to arm a pending setup |
| `ENTRY_TF` | 5m | ZLEMA crossover + confirmation candle + breakout-trigger fill are all evaluated here |
| `MACRO_KLINE_COUNT` / `TREND_KLINE_COUNT` | 300 / 300 | Kline fetch depth for the two trend-gate timeframes — only ~9 bars of headroom over the `ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + 10 = 290`-bar warmup floor `detect_pending_setup` requires; a short REST response silently drops the symbol as `insufficient_history` |
| `PULLBACK_KLINE_COUNT` / `ENTRY_KLINE_COUNT` | 250 / 250 | Kline fetch depth for the pullback/entry timeframes |
| `ZERO_LAG_LENGTH` | 70 | Zero-lag EMA (ZLEMA) length: `lag = floor((length-1)/2)`, `adjusted = 2*close - close.shift(lag)`, `ZLEMA = EMA(adjusted, length)` |
| `ZERO_LAG_BAND_LOOKBACK` | 210 | Bars the ZLEMA band's volatility term (`highest ATR(ATR_PERIOD) over this window`) looks back |
| `ZERO_LAG_MULTIPLIER` | 1.2 | Band width = `highest_ATR * this` — the stateful trend state flips only on a close crossing `ZLEMA ± band`, never on a plain close-vs-ZLEMA comparison, and otherwise holds its previous value |
| `ZERO_LAG_SLOPE_LOOKBACK` | 5 | Bars back the `TREND_TF` ZLEMA slope is measured over, for the score's slope-strength term |
| `ATR_PERIOD` | 70 | ATR period feeding the zero-lag band above — **not** a standalone volatility filter (this strategy has none); repurposed from the retired strategy's ATR%-band gate, so a leftover `.env` override at the old value (14) silently changes the core indicator's band width — verify this on every deploy |
| `PULLBACK_DISTANCE_PCT` | 0.10% | Max distance from the `PULLBACK_TF` candle's own ZLEMA that still counts as "in pullback"; half this distance is where the score's pullback-quality term maxes out |
| `PENDING_EXPIRY_CANDLES` | 6 | A pending setup expires (measured from `setup_time`, not reset on the `pending_pullback` → `pending_breakout` transition) if it never reaches a fired breakout within this many `ENTRY_TF` candles (30 min at the 5m default) |
| `MIN_SIGNAL_SCORE` | 80 | 0–100 composite score gate — see `strategy._pullback_stage_score` (0-70, knowable at arm time) + `strategy._breakout_stage_score` (0-30, knowable only once the 5m crossover happens). **Unvalidated against real data**, same caveat as the prior strategy — tuning this is the top agenda item for the deferred backtest/walk-forward session |
| `LEVERAGE` | 20 | Bot's own position leverage |
| `TP_ROI_PCT` / `SL_ROI_PCT` | 7.0 / 10.0 | Fixed TP/SL sizing at leverage — **not** structural or ATR-derived, a flat ROI%-distance. Raw RR is therefore a constant `TP_ROI_PCT / SL_ROI_PCT` (0.70:1 at defaults) for every setup; there is no RR-based reject gate. `SL_ROI_PCT` was renamed from the prior strategy's `MAX_SL_ROI_PCT` — it's fixed, not a ceiling, since there's no breakeven step that could make the realized SL smaller |
| `ENTRY_BUFFER_PCT` | 0.02% | Breakout trigger price = confirmation candle's high/low ± this buffer |
| `MIN_CANDLE_SETTLE_SECONDS` | 90 | Last closed candle must be at least this old before it's used — MEXC's kline data for a just-closed candle can still get revised shortly after close. **`scan_for_new_setups`'s cron is offset within each `SCAN_INTERVAL_MINUTES` window to clear this margin** on the `PULLBACK_TF` candle (`main.py` fires `MIN_CANDLE_SETTLE_SECONDS + 5s` past each candle boundary, not at the boundary, and raises at startup if no valid offset exists). `monitor_pending_setups` runs on a plain 1-minute `IntervalTrigger` with no cron-offset trick — its cadence is decoupled from `ENTRY_TF`'s candle boundary, but `check_setup_confirmation` still gates on `MIN_CANDLE_SETTLE_SECONDS` for the `ENTRY_TF` candle it evaluates, so most 1-minute polls just see "no new settled candle" and no-op. This settle-offset mechanism broke once already under the prior strategy — when `ENTRY_TF` moved from 15m to 5m, `CANDLE_MINUTES` became equal to `SCAN_INTERVAL_MINUTES`, and a naive "fire a few seconds after the boundary" cron rejected every symbol on every scan, forever. Changing `SCAN_INTERVAL_MINUTES` or any timeframe again needs the same check |
| `ENABLE_LONG_SIGNALS` | true | Both directions live by default |
| `MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` | 2 / 2 | Correlation limit — pending signals per direction (raised from the prior strategy's 1/1, part of this pass's higher signal-frequency target) |
| `MAX_CONCURRENT_SIGNALS` | 4 | Total pending signals across both directions (raised from 2) |
| `MAX_DAILY_SIGNALS` | 12 | Signals fired per day (raised from 3 — this strategy targets 10+/day vs. the prior strategy's 1-3/day) |
| `MIN_DAILY_SIGNAL_GAP_MINUTES` | 60 | Minimum gap between fired signals |
| `SIGNAL_COOLDOWN_MINUTES` | 240 | Same coin blocked for 4h after a signal |
| `SIGNAL_EXPIRE_HOURS` | 6 | Fired (pending) signals auto-expire if TP/SL never hit |
| `SCAN_INTERVAL_MINUTES` | 5 | `scan_for_new_setups` cadence |
| `MONITOR_INTERVAL_MINUTES` | 1 | `monitor_pending_setups` cadence |
| `TOP_N_COINS` | 80 | Pairs tracked |
| `EXCLUDE_COINS` | BTC/ETH/SOL/XAUT | Always excluded |

## Signal Logic (strategy.py) — Zero-Lag MTF Pullback v1

Four-timeframe pipeline: `MACRO_TF` (4h) and `TREND_TF` (1h) zero-lag EMA
(ZLEMA) trend state must agree — a stateful walk that flips only on a
close crossing `ZLEMA ± band`, not a plain close-vs-ZLEMA comparison, and
otherwise holds its previous state. `PULLBACK_TF` (15m) price must then
have pulled back to within `PULLBACK_DISTANCE_PCT` of its own ZLEMA. A
100-point score (0-70 knowable at arm time, up to 30 more once the 5m
crossover happens) must clear `MIN_SIGNAL_SCORE`. SL/TP are fixed
ROI-%-at-leverage distances, not structural, and there's no breakeven
step in this version. Ported into this codebase per
`docs/superpowers/specs/2026-08-11-zero-lag-mtf-pullback-v1-design.md`
(itself adapted from a source document written against a different,
never-actually-present-in-this-repo SMC baseline — the spec's own
"Relationship to prior work" section explains the adaptation).

```
strategy.detect_pending_setup(symbol, reject_sink=None):
  0. Fetch MACRO_TF, TREND_TF, PULLBACK_TF klines separately, each
     dropping the forming candle via iloc[:-1]. Reject (missing_data /
     insufficient_history) if any is empty or too short for the
     ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + margin warmup. Reject
     (candle_not_settled) if the last closed PULLBACK_TF candle is
     younger than MIN_CANDLE_SETTLE_SECONDS.

  1. MACRO_TF zero-lag trend state must be non-neutral (+1/-1, never 0)
     -> otherwise no_macro_trend.

  2. TREND_TF zero-lag trend state must equal the MACRO_TF state exactly
     -> otherwise no_trend_agreement.

  3. direction = LONG if macro_trend == 1 else SHORT. ENABLE_LONG_SIGNALS
     gate -> otherwise long_disabled.

  4. PULLBACK_TF pullback check: LONG needs close <= zlema_15m *
     (1 + PULLBACK_DISTANCE_PCT); SHORT mirrored -> otherwise
     no_pullback.

  5. _pullback_stage_score(): 0-70 -- 30 flat (4H/1H agreement, already
     gated) + up to 20 (1H ZLEMA slope strength) + up to 20 (15m
     pullback quality -- full marks at half the max pullback distance,
     linear decay to 0 at the full distance). Cheap early reject
     (score_below_min) if the partial score plus a perfect 30-point
     breakout stage still couldn't clear MIN_SIGNAL_SCORE.

A passing candidate arms a pending_pullback setup (persisted in
database.pending_setups).

strategy.check_setup_confirmation(setup), called every
monitor_pending_setups pass for every pending_pullback/pending_breakout
row: checks expiry first (from setup_time, PENDING_EXPIRY_CANDLES *
CANDLE_MINUTES, not reset on the pullback->breakout transition), then
gates the ENTRY_TF candle on MIN_CANDLE_SETTLE_SECONDS.

  pending_pullback stage: ENTRY_TF ZLEMA crossover (prev_close/curr_close
  vs prev_zlema/curr_zlema) plus a directional confirmation candle
  (close > open for LONG, mirrored for SHORT) -> "armed_breakout",
  recording confirmation_high/low/close/time and a breakout trigger_price
  (confirmation high/low +/- ENTRY_BUFFER_PCT). No crossover -> "waiting".

  pending_breakout stage: price must break trigger_price (high >
  trigger_price for LONG, low < trigger_price for SHORT) -> otherwise
  "waiting". On break, _breakout_stage_score() adds up to 20
  (freshness -- fewer candles between crossover and breakout is better)
  + up to 10 (confirmation candle's close position within its own
  high-low range) to the pullback-stage score. If the final 0-100 score
  is still < MIN_SIGNAL_SCORE -> "missed" (setup dropped, never fires,
  even though every gate passed). Otherwise -> "confirmed", entry =
  trigger_price.
```

`main.scan_for_new_setups` (every `SCAN_INTERVAL_MINUTES`) scans the coin
pool via `detect_pending_setup`, thread-pooled across `SCAN_WORKERS`, and
arms new `pending_pullback` rows. `main.monitor_pending_setups` (every
`MONITOR_INTERVAL_MINUTES`) processes every currently-armed setup via
`check_setup_confirmation` (also thread-pooled across `SCAN_WORKERS`, each
call individually exception-guarded so one failing symbol can't stall the
rest of the pass), builds SL/TP from `strategy.build_trade_prices()` and
fires confirmed setups within `MAX_DAILY_SIGNALS`,
`MIN_DAILY_SIGNAL_GAP_MINUTES`, `MAX_CONCURRENT_SIGNALS`, per-coin
`SIGNAL_COOLDOWN_MINUTES`, and `direction_slot_available()` (the
`MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` correlation
limit).

### Outcome checking (`outcome_check.check_tp_sl`)

Walks `ENTRY_TF` candles after entry. Each candle, in order: (1) SL —
if hit, closes `loss`; (2) TP — if hit, closes `win`. SL checked before
TP so a single wild candle spanning both is conservatively treated as a
full loss, matching the SL-first tie-break convention used throughout
this bot. No breakeven step in this strategy version (a deliberate
experimental-control decision — see the design spec's §18 discussion of
why breakeven is deferred to a v2 experiment, tested in isolation from
the baseline).

Not part of this strategy version (retired and deleted): the prior
Precision Pullback Scalper v1's EMA20/50/200 + RSI14 pipeline and its
`armed_setups` table, `outcome_check.check_tp_sl_with_breakeven`, and —
from strategies before that — `liq_estimator.py` liquidation-cluster
filter, Super Scalper v3 (`super_scalper_v3.py` / `scalper_v3_strategy.py`),
`nw_kernel.py`, the 6-EMA ribbon and Chandelier/PVT/dual-RSI trigger, the
3-target partial-exit ladder and its `check_target_ladder` walker, and
VWAP/multi-timeframe "strict mode" confirmation. `outcome_replay.py`
still exists in the repo but has no caller in this strategy version.
`scripts/backtest_simple_strategy.py` was rewritten for this strategy's
four-timeframe/three-state pipeline but has not yet been run for a real
6-month backtest (deliberately deferred to a follow-up session).

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
