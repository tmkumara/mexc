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
A two-phase pending-breakout model, persisted in the `armed_setups` DB table (actively used by this strategy version — not legacy). Runs on APScheduler every `SCAN_INTERVAL_MINUTES` (default 5), offset far enough into each candle period to clear `MIN_CANDLE_SETTLE_SECONDS` (see the config table note below — this is *not* simply "a few seconds after candle close"). Phase 1 (`check_setup_confirmation`) checks every currently-armed setup for entry-breakout confirmation or expiry; confirmed setups fire within the daily/gap/concurrent/direction limits. Phase 2 (`detect_pending_setup`) scans the remaining coin pool for new dual-timeframe pullback setups and arms new pending setups. Fetches `TREND_TF` (15m) and `ENTRY_TF` (5m) klines separately and always drops the last (still-forming) bar via `iloc[:-1]` on both, never evaluating an in-progress candle.

**2. Coin selection** (`coin_scanner.py`)
Fetches zero-fee USDT perpetual contracts from MEXC, optionally smart-ranks them by liquidity/volatility/trend/liquidity score (`ENABLE_SMART_COIN_RANKING`), and caches the top `TOP_N_COINS` (80, backfilled to at least `COIN_POOL_MIN_SELECTED`). Refreshed every `COIN_REFRESH_HOURS` (6h) via scheduler. Excludes `EXCLUDE_COINS` (BTC/ETH/SOL/XAUT by default).

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every `OUTCOME_CHECK_MINUTES` (default 1). For each `pending` DB signal, fetches recent `ENTRY_TF` candles and calls `outcome_check.check_tp_sl_with_breakeven()` — a candle-by-candle walk against the fixed single TP/SL, with one breakeven step: once price reaches `BREAKEVEN_TRIGGER_ROI_PCT` (+4% ROI by default) the stop moves to entry. Each candle is checked in order — current stop, then TP, then breakeven-trigger detection — so a single wild candle spanning both the trigger and the original SL is conservatively treated as a full loss. Marks `win`, `loss`, or `breakeven` (a stop-out *after* the breakeven trigger fired — a distinct third outcome, excluded from the win-rate ratio but included in net ROI), or `expired` after `SIGNAL_EXPIRE_HOURS` (6h TTL), then sends a Telegram notification — gated by `DRY_RUN` the same way entry broadcasts are, so dry-run mode never talks to Telegram.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/start /help /status /pause /resume /daily /weekly /monthly /stats`. `notify_outcome` renders `win`/`breakeven`/`loss`/`expired` as four visually distinct outcomes. The `Application` object is passed into scheduler jobs as an argument so they can send messages.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, breakeven, expired}`, plus columns for `strategy_name`, `score`, `rr`, `entry_timeframe`, `trend_timeframe`, `setup_reason`, `breakeven_triggered_at`. Several columns from retired strategies (`tp1_price`, `tp2_price`, `trend`, `strength`, `ao`, `kc_pos`, `regime`, etc.) remain in the schema — no migration was done when those strategies were removed — but nothing in the live code path writes them anymore. `init_db()` also creates the `armed_setups` table, which this strategy's two-phase pending-breakout pipeline actively reads and writes (see "Signal generation" above).

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `TREND_TF` | 15m | Higher timeframe for the EMA200 trend/slope filter |
| `ENTRY_TF` | 5m | Entry timeframe — EMA20/50 alignment, pullback distance, RSI reset, confirmation candle, ATR band, and the pending-setup entry/SL/TP are all computed here |
| `EMA_FAST_LEN` / `EMA_SLOW_LEN` | 20 / 50 | `ENTRY_TF` trend-alignment EMAs |
| `EMA_TREND_LEN` | 200 | EMA200 length, used on both `TREND_TF` (primary trend gate) and `ENTRY_TF` (cross-timeframe agreement gate) |
| `EMA_TREND_SLOPE_LOOKBACK` | 5 | Bars back the EMA200 slope direction is measured over, on `TREND_TF` |
| `EMA_SEPARATION_MIN_PCT` | 0.05% | Minimum EMA20/EMA50 separation (as % of price) to count as "aligned" rather than flat/choppy |
| `RSI_PERIOD` | 14 | RSI period for the pullback reset-zone check |
| `RSI_LONG_RESET_MIN` / `MAX` | 42 / 55 | LONG RSI reset zone — must have touched this range within `PULLBACK_LOOKBACK_BARS` and now be turning up |
| `RSI_SHORT_RESET_MIN` / `MAX` | 45 / 58 | SHORT RSI reset zone (mirrored, turning down) |
| `PULLBACK_LOOKBACK_BARS` | 5 | Bars back the RSI reset-zone touch may have happened and still count |
| `NO_CHASE_MAX_DISTANCE_PCT` | 0.30% | Hard reject if price is already this far from EMA20 — the "don't chase" gate |
| `PULLBACK_PREFERRED_DISTANCE_PCT` | 0.20% | Distance at/below which the pullback-quality score term is maxed out (linear decay to 0 at `NO_CHASE_MAX_DISTANCE_PCT`) |
| `VOLUME_MA_PERIOD` / `VOLUME_CONFIRM_MULT` | 20 / 1.15 | Confirmation candle's volume must exceed this multiple of its N-bar average |
| `MAX_CANDLE_BODY_PCT` | 0.8% | Reject the confirmation candle if its body exceeds this % of price (abnormal/exhausted move) |
| `ATR_PERIOD` | 14 | ATR period backing the volatility-band filter |
| `ATR_MIN_PCT` / `ATR_MAX_PCT` | 0.25% / 1.20% | Required ATR-as-%-of-price band — too quiet or too wild both reject |
| `MIN_SIGNAL_SCORE` | 80 | 0–100 composite score gate (trend/pullback/candle/volume quality) — see `strategy._score_pending_setup`. **Unvalidated against real data** — a live-data probe found only ~1.8% of bars clear 80 even assuming every binary gate already passed; tuning this (and the `NO_CHASE_MAX_DISTANCE_PCT`/`ATR_MAX_PCT` tension below) is the top agenda item for the deferred backtest/walk-forward session |
| `LEVERAGE` | 20 | Bot's own position leverage |
| `TP_ROI_PCT` / `MAX_SL_ROI_PCT` | 7.0 / 10.0 | Fixed TP/SL sizing at leverage — **not** structural or ATR-derived, a flat ROI%-distance. Raw RR is therefore a constant `TP_ROI_PCT / MAX_SL_ROI_PCT` (0.70:1 at defaults) for every setup; there is no RR-based reject gate. ⚠️ The production `.env` currently overrides `MAX_SL_ROI_PCT` to 15.0 (a deliberate, backtested value from the *previous* strategy) — not yet reconciled with this strategy's designed 7/10 ratio |
| `BREAKEVEN_TRIGGER_ROI_PCT` | 4.0 | Once price reaches this ROI%, the stop moves to entry (see `outcome_check.check_tp_sl_with_breakeven`) |
| `ENTRY_BUFFER_PCT` | 0.02% | Pending-setup entry level = confirmation candle's high/low ± this buffer |
| `PENDING_SIGNAL_EXPIRY_CANDLES` | 3 | A pending setup expires if price never breaks the entry level within this many `ENTRY_TF` candles (15 min at the 5m default) |
| `MIN_CANDLE_SETTLE_SECONDS` | 90 | Last closed candle must be at least this old before a signal can fire on it — MEXC's kline data for a just-closed candle can still get revised shortly after close. **The scan cron is offset within each `SCAN_INTERVAL_MINUTES` window to clear this margin** (`main.py` fires `MIN_CANDLE_SETTLE_SECONDS + 5s` past each candle boundary, not at the boundary, and raises at startup if no valid offset exists). This broke once already — when `ENTRY_TF` moved from 15m to 5m, `CANDLE_MINUTES` became equal to `SCAN_INTERVAL_MINUTES`, and a naive "fire a few seconds after the boundary" cron made the settle check reject every symbol on every scan, forever. Changing either `ENTRY_TF` or `SCAN_INTERVAL_MINUTES` again needs the same check |
| `ENABLE_LONG_SIGNALS` | true | Both directions live by default |
| `MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` | 1 / 1 | Correlation limit — pending signals per direction |
| `MAX_CONCURRENT_SIGNALS` | 2 | Total pending signals across both directions |
| `MAX_DAILY_SIGNALS` | 3 | Signals fired per day |
| `MIN_DAILY_SIGNAL_GAP_MINUTES` | 60 | Minimum gap between fired signals |
| `SIGNAL_COOLDOWN_MINUTES` | 240 | Same coin blocked for 4h after a signal |
| `SIGNAL_EXPIRE_HOURS` | 6 | Fired (pending) signals auto-expire if TP/SL never hit |
| `TOP_N_COINS` | 80 | Pairs tracked |
| `EXCLUDE_COINS` | BTC/ETH/SOL/XAUT | Always excluded |

## Signal Logic (strategy.py) — Precision Pullback Scalper v1

Dual-timeframe pipeline: `TREND_TF` (15m) EMA200 trend + slope gates direction;
`ENTRY_TF` (5m) EMA20/EMA50 alignment, a pullback into the EMA20/EMA50 zone
(bounded by `NO_CHASE_MAX_DISTANCE_PCT`), an RSI14 reset-then-turn, a
confirming candle (body/close/volume checks), and an ATR% volatility band
all gate a candidate; a 100-point score must then clear `MIN_SIGNAL_SCORE`.
SL/TP are fixed ROI-%-at-leverage distances, not structural — quality
control is entirely the score gate, not RR. Ported into this codebase per
`docs/superpowers/specs/2026-08-09-precision-pullback-scalper-v1-design.md`.

```
strategy.detect_pending_setup(symbol, reject_sink=None):
  0. Fetch ENTRY_TF and TREND_TF klines separately, each dropping the
     forming candle via iloc[:-1]. Reject (missing_data /
     insufficient_history) if either is empty or too short for the
     longest indicator warmup. Reject (candle_not_settled) if the last
     closed ENTRY_TF candle is younger than MIN_CANDLE_SETTLE_SECONDS.

  1. Trend filter (TREND_TF, self-referential -- NOT compared against
     ENTRY_TF at this step): close vs EMA200 and EMA200's own slope over
     EMA_TREND_SLOPE_LOOKBACK bars must agree (both up = LONG, both down
     = SHORT) -> no agreement rejects no_trend_alignment.

  2. EMA20/EMA50 alignment (ENTRY_TF): must be on the correct side and
     separated by at least EMA_SEPARATION_MIN_PCT of price (a flat/
     choppy-market filter) -> otherwise no_ema_alignment.

  3. EMA200 cross-timeframe agreement: ENTRY_TF's own close vs its own
     EMA200 must agree with the TREND_TF direction from step 1 ->
     otherwise no_ema200_agreement.

  4. No-chase distance: reject chasing_price if price is already more
     than NO_CHASE_MAX_DISTANCE_PCT from EMA20 -- don't enter a pullback
     that's already run away.

  5. RSI reset: RSI14 must have touched the direction's reset zone
     (RSI_LONG/SHORT_RESET_MIN/MAX) within PULLBACK_LOOKBACK_BARS and now
     be turning back in the trade direction -> otherwise no_rsi_reset.

  6. Confirmation candle: the latest closed ENTRY_TF candle must close
     beyond its own open, EMA20, and the prior candle's high/low, on
     volume > VOLUME_MA_PERIOD-bar average x VOLUME_CONFIRM_MULT ->
     otherwise no_confirmation_candle. Its body must not exceed
     MAX_CANDLE_BODY_PCT of price -> otherwise abnormal_candle.

  7. ATR% band: ATR14 / price must sit within [ATR_MIN_PCT, ATR_MAX_PCT]
     -> otherwise atr_out_of_band (too quiet or too wild).

  8. _build_pending_setup(): entry is a breakout buffer (ENTRY_BUFFER_PCT)
     beyond the confirmation candle's high/low. SL/TP are FIXED
     ROI-%-at-LEVERAGE distances (MAX_SL_ROI_PCT / TP_ROI_PCT) -- not
     structural, not ATR-derived. Raw RR is therefore a constant
     TP_ROI_PCT / MAX_SL_ROI_PCT (0.70 at defaults) for every setup;
     there is no RR-based reject gate here.

  9. _score_pending_setup(): 0-100 composite -- 20 (trend, flat, already
     gated) + 15 (alignment, flat) + 10 (EMA200 slope strength) + 15
     (pullback quality -- full marks at distance <=
     PULLBACK_PREFERRED_DISTANCE_PCT, linear decay to 0 at
     NO_CHASE_MAX_DISTANCE_PCT) + 10 (RSI reset, flat) + 15
     (confirmation-candle clearance beyond the prior high/low) + 10
     (volume ratio above VOLUME_CONFIRM_MULT) + 5 (ATR band, flat).
     Reject score_below_min if the total is < MIN_SIGNAL_SCORE.

A passing candidate becomes a PENDING setup (persisted in
database.armed_setups). strategy.check_setup_confirmation(setup), called
every scan for every currently-armed setup, checks the latest closed
ENTRY_TF candle: entry-level break -> "confirmed" (fires a Signal);
same-candle SL breach before the entry level breaks -> "invalidated"
(conservative same-candle tie-break -- treated as an instant stop, never
a fill); no confirmation within PENDING_SIGNAL_EXPIRY_CANDLES ->
"expired"; otherwise "waiting", re-checked next scan.
```

`main.scan_and_fire_signals` runs Phase 1 (process every currently-armed
setup via `check_setup_confirmation`) then Phase 2 (scan the remaining
coin pool via `detect_pending_setup`, thread-pooled across `SCAN_WORKERS`)
each cycle, subject to `MAX_DAILY_SIGNALS`, `MIN_DAILY_SIGNAL_GAP_MINUTES`,
`MAX_CONCURRENT_SIGNALS`, per-coin `SIGNAL_COOLDOWN_MINUTES`, and
`direction_slot_available()` (the `MAX_ACTIVE_LONG_SIGNALS` /
`MAX_ACTIVE_SHORT_SIGNALS` correlation limit).

### Outcome checking (`outcome_check.check_tp_sl_with_breakeven`)

Walks `ENTRY_TF` candles after entry. Each candle, in this order: (1)
current stop (the original SL, or entry price once the breakeven trigger
has fired) — if hit, closes the trade (`loss` if the stop was still the
original SL, `breakeven` if it had already moved to entry); (2) TP — if
hit, closes as `win`; (3) only if neither hit, checks whether price
reached `BREAKEVEN_TRIGGER_ROI_PCT` for the first time this candle and
moves the stop to entry. This ordering means a single wild candle
spanning both the breakeven trigger and the original SL is conservatively
treated as a full loss, matching the SL-first tie-break convention used
throughout this bot.

Not part of this strategy version (retired and deleted): `liq_estimator.py`
liquidation-cluster filter, Super Scalper v3 (`super_scalper_v3.py` /
`scalper_v3_strategy.py`), `nw_kernel.py`, the 6-EMA ribbon and
Chandelier/PVT/dual-RSI trigger, the 3-target partial-exit ladder and its
`check_target_ladder` walker, plain `check_tp_sl` (single-TP, no
breakeven), and VWAP/multi-timeframe "strict mode" confirmation — all
Binocular-era, fully removed from `strategy.py`/`outcome_check.py`.
`outcome_replay.py` still exists in the repo but has no caller in this
strategy version.

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
