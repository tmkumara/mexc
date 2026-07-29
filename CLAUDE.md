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
Runs on APScheduler every `SCAN_INTERVAL_MINUTES` (default 5), a few seconds after candle close. Single public entry point `evaluate_symbol(symbol, btc_context=None)` — a straight per-cycle evaluation, no arm/monitor state machine (`btc_context` is accepted for call-site compatibility but unused — there is no BTC filter in this strategy version). Fetches `ENTRY_TF` (15m) klines and always drops the last (still-forming) bar via `iloc[:-1]`, never evaluating an in-progress candle.

**2. Coin selection** (`coin_scanner.py`)
Fetches zero-fee USDT perpetual contracts from MEXC, optionally smart-ranks them by liquidity/volatility/trend/liquidity score (`ENABLE_SMART_COIN_RANKING`), and caches the top `TOP_N_COINS` (80, backfilled to at least `COIN_POOL_MIN_SELECTED`). Refreshed every `COIN_REFRESH_HOURS` (6h) via scheduler. Excludes `EXCLUDE_COINS` (BTC/ETH/SOL/XAUT by default).

**3. Outcome tracking** (`main.py → check_outcomes`)
Runs every `OUTCOME_CHECK_MINUTES` (default 1). For each `pending` DB signal, fetches recent `ENTRY_TF` candles and calls `outcome_check.check_tp_sl()` — a candle-by-candle high/low scan (no live-price polling), SL-first on a same-candle tie. Marks `win`/`loss`, or `expired` after `SIGNAL_EXPIRE_HOURS` (6h TTL), then sends a Telegram notification — gated by `DRY_RUN` the same way entry broadcasts are, so dry-run mode never talks to Telegram.

**Telegram bot** (`bot.py`) is stateless except for a module-level `paused` bool. Commands: `/start /help /status /pause /resume /daily /weekly /monthly /stats`. The `Application` object is passed into scheduler jobs as an argument so they can send messages.

**Database** (`database.py`) is a local SQLite file (`signals.db`). Schema: single `signals` table with `status` ∈ `{pending, win, loss, expired}`, plus columns for `strategy_name`, `score`, `rr`, `entry_timeframe`, `trend_timeframe`, `setup_reason`. `init_db()` also creates a legacy `armed_setups` table (schema left in place for backward compatibility) but no code in this strategy reads or writes it — it was the persistence layer for the retired two-phase arm/monitor scalp strategy.

## Key Config (`config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `ENTRY_TF` | 15m | Single timeframe -- both the EMA ribbon and Trend Bar are computed on it |
| `RIBBON_MA1_LEN` … `RIBBON_MA5_LEN` | 30/35/40/45/50 | The 6-EMA ribbon's five short EMA lengths |
| `RIBBON_BASELINE_LEN` | 60 | The ribbon's baseline EMA length ("MA6" in the Pine source) |
| `RIBBON_LOOKBACK_BARS` | 1 | How many bars back a ribbon flip may have happened and still count as "recent enough" (backtesting found no edge widening this — an exact-bar flip performs as well as a several-bar window) |
| `TREND_BAR_PAC_LENGTH` | 50 | Price-Action-Channel EMA length behind the Trend Bar confirmation |
| `ATR_PERIOD` | 14 | ATR period for the structural-SL buffer and candidate scoring |
| `SL_FLOOR_ATR_MULT` | 2.0 | Floors the structural SL at this many ATRs from entry, so it's never tighter than normal candle noise |
| `ENABLE_LONG_SIGNALS` | true | Both directions live by default; backtesting recommends `false` (SHORT-only) — LONG underperformed SHORT in every configuration tested |
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

## Signal Logic (strategy.py) — Ribbon-Flip Trend-Bar Confirmation v1

Single-pass evaluation per scan cycle, no persisted setup state. Ported
from a TradingView Pine Script's 6-EMA ribbon and Price-Action-Channel
"Trend Bar" indicators, per the exact manual rule: ribbon flips direction
("arrow 1"), then wait for the Trend Bar to confirm the same direction
("arrow 2") — if the ribbon reverts first, the setup is invalid. No
persisted arm/monitor state is used; the "wait" is instead a bounded
backward search over `RIBBON_LOOKBACK_BARS`, recomputed fresh every scan:

```
strategy.evaluate_symbol(symbol, btc_context=None):
  1. _detect_ribbon_flip(df):
     computes the 6-EMA ribbon (RIBBON_MA1_LEN..MA5_LEN vs
     RIBBON_BASELINE_LEN) on closed candles. If the ribbon is NOT
     currently fully aligned (all 5 short EMAs above/below the
     baseline) -> no trade. If it is aligned, walks backward up to
     RIBBON_LOOKBACK_BARS bars (default 1 -- essentially requiring the
     flip on the current or immediately preceding bar) to find the most
     recent bar where that alignment began (a genuine flip-in, not just
     "still aligned from ages ago"). No flip found within the window ->
     no trade. If the flip direction is LONG and ENABLE_LONG_SIGNALS is
     false -> no trade (true by default -- both directions live).

  2. calculate_trend_bar(df, TREND_BAR_PAC_LENGTH):
     on the latest CLOSED candle, checks whether the candle's entire
     range sits above (green) or below (red) a Price-Action-Channel
     built from EMA(high, TREND_BAR_PAC_LENGTH) / EMA(low,
     TREND_BAR_PAC_LENGTH). Must match the ribbon's direction (green
     for LONG, red for SHORT) -> otherwise no trade. Because the ribbon
     search above already re-derives the flip fresh each scan, a
     reverted-then-reflipped ribbon is naturally excluded without any
     extra "did it revert" bookkeeping.

  3. _calculate_tp_sl(): fixed-distance TP at TP_PRICE_PCT
     (= TARGET_ROI_PCT / 100 / LEVERAGE); SL placed at the swing
     low/high spanning from the ribbon-flip bar through the current
     bar, plus an ATR buffer (SL_ATR_BUFFER_MULTIPLIER), floored at
     SL_FLOOR_ATR_MULT x ATR from entry (so the stop is never tighter
     than normal candle noise), capped at MAX_SL_PRICE_PCT
     (= MAX_SL_ROI_PCT / 100 / LEVERAGE).

  4. RR = reward / risk must be >= MIN_RR.

  5. _score_candidate(): 0-100 composite — ribbon alignment strength vs
     ATR (40), flip freshness within the lookback window (20), Trend Bar
     clearance beyond the PAC channel vs ATR (20), RR quality (20) —
     used to rank multiple candidates within a scan.
```

There is no BTC market-safety filter in this strategy version (dropped
along with the zone/Chandelier/PVT/dual-RSI pipeline it replaced) — the
`btc_context` parameter on `evaluate_symbol` is accepted for call-site
compatibility only and has no effect.

`main.scan_and_fire_signals` evaluates the whole coin pool in a thread pool, sorts candidates by score, and fires the top ones subject to `MAX_DAILY_SIGNALS`, `MIN_DAILY_SIGNAL_GAP_MINUTES`, `MAX_CONCURRENT_SIGNALS`, `SIGNALS_PER_SCAN`, per-coin `SIGNAL_COOLDOWN_MINUTES`, and `direction_slot_available()` (the `MAX_ACTIVE_LONG_SIGNALS`/`MAX_ACTIVE_SHORT_SIGNALS` correlation limit).

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
