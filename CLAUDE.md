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
Runs on APScheduler every `SCALP_SCAN_INTERVAL_MINUTES` (default 1, aligns to 1m candle close). Two-phase arm/monitor over `armed_setups`: arms on an EMA/VWAP/RSI/volume base signal, fires once a liquidation-cluster filter (`liq_estimator.py`) clears. Uses `iloc[:-1]` (last *completed* candle), never the in-progress bar.

**2. Coin selection** (`coin_scanner.py`)
Fetches zero-fee USDT perpetual contracts from MEXC, sorts by 24h volume, caches top `TOP_N_COINS` (20). Refreshed every 6h via scheduler. Excludes `EXCLUDE_COINS` (BTC, ETH, SOL by default).

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every 15 minutes. Polls current price via `get_current_price()` for all `pending` DB signals, marks as `win`/`loss`/`expired` (48h TTL), sends Telegram notification.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/status /pause /resume /daily /weekly /monthly /stats`. The `Application` object is passed into scheduler jobs as an argument so they can send messages.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, expired}`.

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `ST_LENGTH / ST_MULTIPLIER` | 10 / 2.5 | Supertrend params |
| `EMA_TREND_PERIOD` | 200 | Trend filter |
| `LEVERAGE` | 10 | Shown in signal message |
| `REWARD_RATIO` | 2.0 | TP = 2× risk (2:1 R:R) |
| `SIGNAL_COOLDOWN_MINUTES` | 240 | Same coin blocked for 4h after signal |
| `SIGNAL_EXPIRE_HOURS` | 48 | Pending signals auto-expire |
| `TOP_N_COINS` | 20 | Pairs tracked |
| `EXCLUDE_COINS` | BTC/ETH/SOL | Always excluded |

## Signal Logic (strategy.py) — Liquidation-Aware 1m Scalp (v14)

Two-phase arm/monitor workflow on 1m candles, persisted via the `armed_setups` table:

```
Phase 1 (arm) -- strategy.arm_symbol(symbol):
  1. Base signal on the last CLOSED 1m bar:
     EMA(9) > EMA(21) > EMA(50)  (LONG)  /  reversed (SHORT)
     price on the correct side of rolling VWAP
     RSI(14) in 50-68 (LONG) / 32-50 (SHORT)
     volume > 1.3x the trailing 20-bar average
  2. If a base signal fires, evaluate the liquidity filter immediately.
     If it already clears, arm with real levels; if not, arm anyway with
     provisional levels so Phase 2 keeps re-checking without re-deriving
     the base signal.

Phase 2 (monitor) -- strategy.monitor_symbol(symbol), every cycle,
regardless of the firing-budget throttle:
  1. Re-run the base signal; invalidate the setup if it's no longer active.
  2. Re-run the liquidity filter; fire a Signal the moment it clears.
  3. Expire the setup after SCALP_ARM_MAX_AGE_BARS minutes.
```

### Liquidity filter (`_evaluate_liquidity` in strategy.py, backed by `liq_estimator.py`)

`liq_estimator.py` estimates resting liquidation liquidity for free: when
open interest rises between two polls (`OI_POLL_SEC`, default 60s), new
positions were opened near the current price. Their liquidation prices are
projected across `LEVERAGE_TIERS` into `BUCKET_PCT`-wide price buckets.
Price sweeping a bucket clears it (those positions closed); clusters decay
by `CLUSTER_DECAY` every poll tick.

A signal only fires when, for the base signal's direction:
- a significant opposite-side liquidation cluster sits ahead as a magnet
  (top `CLUSTER_MIN_PERCENTILE` by magnitude, within `CLUSTER_LOOKAROUND`)
- the magnet isn't so close the move looks already exhausted
- no larger same-side cluster sits behind entry (fighting the trade)
- funding isn't extreme against the direction (`FUNDING_EXTREME`)
- the stop can be placed cleanly, capped at `MAX_SL_PRICE_PCT`, never inside a dense cluster
- RR >= `MIN_RR`

TP is placed just before the magnet cluster, capped at
`TARGET_MARGIN_PROFIT / LEVERAGE` price distance from entry.

### Tuning constants (config.py)

```python
LEVERAGE              = 20   # bot's own position leverage
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
RSI_PERIOD = 14
RSI_LONG_MIN, RSI_LONG_MAX   = 50, 68
RSI_SHORT_MIN, RSI_SHORT_MAX = 32, 50
SCALP_VOLUME_MIN_MULT = 1.3
TARGET_MARGIN_PROFIT  = 0.12   # 12% margin target -> price move = this / LEVERAGE
MIN_RR                = 1.5
MAX_SL_PRICE_PCT      = 0.0032
LEVERAGE_TIERS = {10: 0.20, 20: 0.25, 25: 0.20, 50: 0.20, 75: 0.10, 100: 0.05}
MMR_BUFFER            = 0.006
BUCKET_PCT            = 0.0005
CLUSTER_DECAY         = 0.97
CLUSTER_LOOKAROUND    = 0.02
CLUSTER_MIN_PERCENTILE = 90
OI_POLL_SEC           = 60
FUNDING_EXTREME       = 0.0004
SCALP_ARM_MAX_AGE_BARS = 10   # minutes
```

Not part of this strategy (removed with VP-OB): ATR-based stop, BTC macro
trend gate, Volume Profile / Order Block detection.

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
