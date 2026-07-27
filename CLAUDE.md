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
- **DB clear utility:** `python clear_db.py` (or `python clear_db.py --yes` to skip confirm)

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
Runs on APScheduler every `SCAN_INTERVAL_MINUTES` (default 5), a few seconds after candle close. Single public entry point `evaluate_symbol(symbol, btc_context=None)` — a straight per-cycle evaluation, no arm/monitor state machine. Fetches `TREND_TF` (15m) and `ENTRY_TF` (5m) klines and always drops the last (still-forming) bar via `iloc[:-1]`, never evaluating an in-progress candle.

**2. Coin selection** (`coin_scanner.py`)
Fetches zero-fee USDT perpetual contracts from MEXC, optionally smart-ranks them by liquidity/volatility/trend/liquidity score (`ENABLE_SMART_COIN_RANKING`), and caches the top `TOP_N_COINS` (80, backfilled to at least `COIN_POOL_MIN_SELECTED`). Refreshed every `COIN_REFRESH_HOURS` (6h) via scheduler. Excludes `EXCLUDE_COINS` (BTC/ETH/SOL/XAUT by default).

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every `OUTCOME_CHECK_MINUTES` (default 1). For each `pending` DB signal, fetches recent `ENTRY_TF` candles and calls `outcome_check.check_tp_sl()` — a candle-by-candle high/low scan (no live-price polling), SL-first on a same-candle tie. Marks `win`/`loss`, or `expired` after `SIGNAL_EXPIRE_HOURS` (6h TTL), then sends a Telegram notification — gated by `DRY_RUN` the same way entry broadcasts are, so dry-run mode never talks to Telegram.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/start /help /status /pause /resume /daily /weekly /monthly /stats`. The `Application` object is passed into scheduler jobs as an argument so they can send messages.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, expired}`, plus columns for `strategy_name`, `score`, `rr`, `entry_timeframe`, `trend_timeframe`, `setup_reason`. `init_db()` also creates a legacy `armed_setups` table (schema left in place for backward compatibility) but no code in this strategy reads or writes it — it was the persistence layer for the retired two-phase arm/monitor scalp strategy.

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `TREND_TF` / `ENTRY_TF` | 15m / 5m | Zone (structural) timeframe and trigger (entry) timeframe |
| `CHANDELIER_ATR_PERIOD` / `CHANDELIER_MULTIPLIER` | 10 / 2.2 | 5m Chandelier Exit params (trigger direction) |
| `PVT_SIGNAL_LENGTH` / `PVT_SIGNAL_TYPE` | 21 / SMA | PVT signal-average length and MA type (SMA or EMA) |
| `RSI_FAST_PERIOD` / `RSI_SLOW_PERIOD` | 25 / 55 | Dual-RSI regime filter periods (fast vs slow agreement) |
| `ZONE_SWING_LENGTH` / `ZONE_ATR_PERIOD` / `ZONE_BOX_WIDTH` | 10 / 50 / 2.5 | 15m pivot lookback, ATR period, and zone box width (ATR-buffer/10) |
| `ZONE_PROXIMITY_ATR_MULT` / `ZONE_MAX_AGE_BARS` | 0.5 / 100 | Confluence-zone proximity tolerance (x ATR) and max zone age in bars |
| `ENTRY_BUFFER_PCT` | 0.0002 | Breakout-buffer fraction the trigger close must clear beyond the prior high/low |
| `LEVERAGE` | 20 | Bot's own position leverage; scales ROI% ↔ price% |
| `TARGET_ROI_PCT` / `MAX_SL_ROI_PCT` | 15.0 / 10.0 | TP/SL sizing at leverage (→ `TP_PRICE_PCT`, `MAX_SL_PRICE_PCT`) |
| `MIN_RR` | 1.5 | Minimum reward:risk to fire |
| `MAX_ACTIVE_LONG_SIGNALS` / `MAX_ACTIVE_SHORT_SIGNALS` | 1 / 1 | Correlation limit — pending signals per direction |
| `MAX_CONCURRENT_SIGNALS` | 2 | Total pending signals across both directions |
| `MAX_DAILY_SIGNALS` | 3 | Signals fired per day |
| `SIGNAL_COOLDOWN_MINUTES` | 240 | Same coin blocked for 4h after a signal |
| `SIGNAL_EXPIRE_HOURS` | 6 | Pending signals auto-expire |
| `TOP_N_COINS` | 80 | Pairs tracked |
| `EXCLUDE_COINS` | BTC/ETH/SOL/XAUT | Always excluded |
| `ENABLE_BTC_FILTER` | true | Gate all signals on BTC's own 15m trend/volatility |

## Signal Logic (strategy.py) — Binocular Trend Confluence v1

Single-pass evaluation per scan cycle, no persisted setup state:

```
strategy.evaluate_symbol(symbol, btc_context=None):
  1. build_zones(df_15m, ...):
     finds pivot highs/lows (ZONE_SWING_LENGTH) on closed 15m candles,
     turns each into a supply (pivot high) or demand (pivot low) box
     sized by ATR(ZONE_ATR_PERIOD) x (ZONE_BOX_WIDTH / 10), and marks a
     zone invalidated (BOS) once a later close trades through it. Zones
     older than ZONE_MAX_AGE_BARS are dropped; only active
     (non-BOS, in-range) zones are kept.

  2. _detect_trigger(df_5m):
     on the latest CLOSED 5m candle:
       - Chandelier Exit (CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
         direction must be bullish (LONG) or bearish (SHORT)
       - PVT vs its signal average (PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
         must agree with that direction (PVT above/below signal)
       - RSI_FAST vs RSI_SLOW must agree with that direction
         (fast > slow for LONG, fast < slow for SHORT)
       - close must clear the prior candle's high/low by
         ENTRY_BUFFER_PCT (breakout confirmation)
     any failed check -> no trade, otherwise -> LONG or SHORT

  3. _find_confluence_zone(): requires an active opposing-type zone
     (demand zone for LONG, supply zone for SHORT) within
     ZONE_PROXIMITY_ATR_MULT x ATR(ZONE_ATR_PERIOD) of the trigger
     price; no such zone -> no trade.

  4. If ENABLE_BTC_FILTER, build_btc_context()/_btc_filter_ok() must pass
     (see below).

  5. _calculate_tp_sl(): fixed-distance TP at TP_PRICE_PCT
     (= TARGET_ROI_PCT / 100 / LEVERAGE); SL placed at the confluence
     zone's far boundary plus an ATR buffer (SL_ATR_BUFFER_MULTIPLIER),
     capped at MAX_SL_PRICE_PCT (= MAX_SL_ROI_PCT / 100 / LEVERAGE).

  6. RR = reward / risk must be >= MIN_RR.

  7. _score_candidate(): 0-100 composite — zone proximity (25),
     PVT-vs-signal momentum magnitude (25), breakout clearance (20),
     RSI_FAST ideal-band quality (10), RR quality (10), zone freshness
     (10) — used to rank multiple candidates within a scan.
```

`main.scan_and_fire_signals` evaluates the whole coin pool in a thread pool, sorts candidates by score, and fires the top ones subject to `MAX_DAILY_SIGNALS`, `MIN_DAILY_SIGNAL_GAP_MINUTES`, `MAX_CONCURRENT_SIGNALS`, `SIGNALS_PER_SCAN`, per-coin `SIGNAL_COOLDOWN_MINUTES`, and `direction_slot_available()` (the `MAX_ACTIVE_LONG_SIGNALS`/`MAX_ACTIVE_SHORT_SIGNALS` correlation limit).

### BTC market-safety filter (`build_btc_context` / `_btc_filter_ok` in strategy.py)

Computes BTC's own 15m EMA(200)/Supertrend plus its 1-candle and 3-candle % moves once per scan cycle (shared across all symbols). A candidate is blocked when:
- BTC's 1-candle or 3-candle move exceeds `BTC_MAX_SINGLE_CANDLE_MOVE_PCT` / `BTC_MAX_THREE_CANDLE_MOVE_PCT` (extreme volatility, either direction)
- BTC's own trend (close vs EMA200 + Supertrend direction) doesn't agree with the signal's direction, or is moving against it by more than `BTC_MAX_OPPOSING_MOVE_PCT`

### Outcome checking (`outcome_check.check_tp_sl`)

Walks 5m candles after entry; SL and TP are both checked by high/low touch, and if both are touched within the same candle the stop wins (conservative tie-break). No breakeven or trailing-stop management is part of v1 — `outcome_replay.py` has a breakeven-aware replay path but it is not used by this strategy; TP/SL are fixed at signal generation and checked as-is until win, loss, or expiry.

Not part of v1 (retired with the old liquidation-scalp strategy): the
`armed_setups` two-phase arm/monitor workflow, `liq_estimator.py`
liquidation-cluster filter, VWAP/EMA9-21-50 base signal, and 1m candles.

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
