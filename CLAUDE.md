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
| `TREND_TF` / `ENTRY_TF` | 15m / 5m | Trend and entry timeframes |
| `TREND_EMA_PERIOD` / `ENTRY_EMA_PERIOD` | 200 / 20 | EMA periods used on each timeframe |
| `TREND_SUPERTREND_ATR_PERIOD` / `TREND_SUPERTREND_MULTIPLIER` | 10 / 3.0 | 15m Supertrend params |
| `ENTRY_SUPERTREND_ATR_PERIOD` / `ENTRY_SUPERTREND_MULTIPLIER` | 10 / 2.0 | 5m Supertrend params |
| `RSI_LONG_MIN` / `RSI_LONG_MAX` | 50 / 68 | 5m RSI band for LONG confirmation |
| `RSI_SHORT_MIN` / `RSI_SHORT_MAX` | 32 / 50 | 5m RSI band for SHORT confirmation |
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

## Signal Logic (strategy.py) — Simple Supertrend Pullback v1

Single-pass evaluation per scan cycle, no persisted setup state:

```
strategy.evaluate_symbol(symbol, btc_context=None):
  1. _detect_trend(df_15m):
     close > EMA(200) AND 15m Supertrend bullish AND EMA200 not falling -> LONG
     close < EMA(200) AND 15m Supertrend bearish AND EMA200 not rising  -> SHORT
     otherwise -> no trade

  2. _detect_pullback_and_confirmation(df_5m, direction):
     price was on the correct side of EMA(20) before the pullback,
     pulled back to touch/cross EMA(20) within the last
     PULLBACK_LOOKBACK_BARS candles, then the latest CLOSED candle:
       - reclaims EMA(20) and closes in the trend direction
       - 5m Supertrend agrees with the trend direction
       - RSI(14) inside the direction's band
       - volume >= MIN_VOLUME_MULTIPLIER x trailing VOLUME_MA_PERIOD avg
       - candle range <= MAX_CONFIRMATION_CANDLE_ATR x ATR(14) (not a spike)
       - distance from EMA(20) <= MAX_EMA_DISTANCE_PCT (not chasing)

  3. If ENABLE_BTC_FILTER, build_btc_context()/_btc_filter_ok() must pass
     (see below).

  4. _calculate_tp_sl(): fixed-distance TP at TP_PRICE_PCT
     (= TARGET_ROI_PCT / 100 / LEVERAGE); SL placed structurally beyond
     the pullback swing low/high plus an ATR buffer
     (SL_ATR_BUFFER_MULTIPLIER), capped at MAX_SL_PRICE_PCT
     (= MAX_SL_ROI_PCT / 100 / LEVERAGE).

  5. RR = reward / risk must be >= MIN_RR.

  6. _score_candidate(): 0-100 composite (trend/Supertrend alignment,
     EMA-reclaim quality, volume quality, RSI quality, RR quality) used
     to rank multiple candidates within a scan.
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
