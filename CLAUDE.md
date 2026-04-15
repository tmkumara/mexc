# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
# Install dependencies (server uses venv/, not .venv/)
source venv/bin/activate
pip install -r requirements.txt

# Run
python main.py

# Server: managed by systemd
systemctl start mexc-bot
systemctl stop mexc-bot
systemctl restart mexc-bot
journalctl -u mexc-bot -f          # live logs
tail -f /opt/signals/mexc_bot.log  # file logs
```

## Deployment

- **Server:** Ubuntu 24.04 at `68.168.222.74`, app at `/opt/signals/`, venv at `/opt/signals/venv/`
- **Service name:** `mexc-bot`
- **Auto-deploy:** push to `main` branch → GitHub Actions SSHs in, git pulls, pip installs, restarts service
- **Workflow file:** `.github/workflows/deploy.yml`
- **DB clear utility:** `python clear_db.py` (or `python clear_db.py --yes` to skip confirm)

## Architecture

The bot is a single-process async application (`main.py`) with three concerns:

**1. Signal generation** (`strategy.py`)
Runs on APScheduler at `minute=1` of every hour (aligns to 1h candle close). Calls `get_klines()` for each active pair, computes Supertrend(10, 2.5) + EMA200 + RSI(14), fires a `Signal` dataclass when Supertrend flips direction with EMA200 and RSI confirming. Uses `iloc[-2]` (last *completed* candle), never `iloc[-1]` (in-progress).

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

## Signal Logic (strategy.py)

All 7 conditions must be true simultaneously:

```
1. Supertrend flips direction   -1 → +1 (LONG)  /  +1 → -1 (SHORT)
2. Close on correct EMA200 side  close > EMA200  /  close < EMA200
3. RSI(14) momentum              RSI > 50        /  RSI < 50
── fakeout filters (4 independent layers) ──────────────────────────
4. Consecutive EMA200 closes   prev candle ALSO above/below EMA200
                               → kills single-candle spikes
5. Candle body quality         body >= 50% of candle range
                               AND body direction matches signal
                               → kills wick/doji spikes
6. ADX(14) >= 25               market must be trending, not sideways
                               → kills consolidation whipsaws
7. Volume >= 1.5× 20-bar MA    genuine participation required
                               → kills low-volume stop-hunts

SL = Supertrend band value (ATR-adaptive dynamic stop)
TP = entry ± REWARD_RATIO × |entry − SL|   (2:1 R:R)
```

### Why each fakeout filter exists

| Filter | Root cause it prevents |
|---|---|
| Consecutive closes | One aggressive candle spikes through EMA200 then reverses |
| Body ratio >= 50% | Wick-heavy candle (doji/pin bar) falsely flips Supertrend |
| ADX >= 25 | Supertrend flips repeatedly during sideways chop |
| Volume >= 1.5× MA | Institutional stop-hunt spike with no real momentum |

### Tuning constants (top of strategy.py)

```python
RSI_PERIOD      = 14
ADX_PERIOD      = 14
VOLUME_MA_BARS  = 20
VOLUME_MIN_MULT = 1.5   # raise to 2.0 for stricter volume gate
BODY_RATIO_MIN  = 0.50  # raise to 0.60 for stricter body quality
ADX_MIN         = 25    # raise to 30 in high-noise market conditions
KLINE_COUNT     = 300   # must cover EMA200 + all indicator warm-up bars
```

## MEXC API (`mexc_client.py`)

Uses MEXC Futures REST API (`https://contract.mexc.com/api/v1`). Key quirk: volume field varies by endpoint version — always use the fallback chain `realVolume → vol → volume`. Kline interval must be mapped through `INTERVAL_MAP` (e.g. `"1h"` → `"Min60"`).

## Environment

`.env` file (not committed) requires:
```
TELEGRAM_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```
