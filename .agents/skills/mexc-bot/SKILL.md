
---
name: mexc-bot
description: Use this skill when working on the MEXC Futures signal bot, strategy.py, config.py, main.py, database.py, bot.py, webui.py, candle cache, WebSocket candles, signal firing, TP/SL/RR bugs, SMC strategy tuning, and deployment commands.
---

# MEXC Futures Signal Bot Skill

You are working on a Python async MEXC Futures signal bot.

The user wants safe, high-quality trading signal logic, not random frequent alerts.

## Project Goal

Build and maintain a MEXC Futures Telegram signal bot using:

- MTF SMC strategy
- 1D macro filter
- 4H trend filter
- 1H structure bias
- 15m entry confirmation
- Liquidity sweep
- Displacement candle
- Order block retest
- MSS break confirmation
- Strict TP/SL validation
- 20x leverage
- Target 1:2 RR
- Target around +50% ROI per TP
- Around 1–3 quality signals per day

Do not promise guaranteed profit or guaranteed 80% win rate. Focus on risk validation, correctness, and high-quality filtering.

## Main Files

Important files:

- `main.py` — async runtime, scheduler, setup scan, monitor, outcome checker, WebSocket cache startup.
- `strategy.py` — all signal logic, setup detection, pending setup evaluation, TP/SL/RR calculation.
- `config.py` — all tunable settings from `.env`.
- `database.py` — SQLite signal and pending setup state.
- `bot.py` — Telegram commands and signal message formatting.
- `coin_scanner.py` — MEXC futures coin pool and ranking.
- `mexc_client.py` — REST client for candles/tickers/contracts.
- `mexc_ws_client.py` — MEXC WebSocket candle updates.
- `candle_cache.py` — in-memory OHLCV candle cache.
- `market_data.py` — candle access layer, cache first and REST fallback.
- `webui.py` — dashboard.
- `clear_db.py` — DB cleanup tool.
- `restart_bot.sh` / `clean_runtime_state.sh` — server operation scripts.

## Current Strategy Architecture

The preferred strategy is Liquidation-Aware 1m Scalp (v14):

Two-phase arm/monitor workflow on 1m candles:

1. Phase 1 (arm) on base signal:
   - EMA(9) > EMA(21) > EMA(50) (LONG) / reversed (SHORT)
   - Price on correct side of rolling VWAP
   - RSI(14) in 50-68 (LONG) / 32-50 (SHORT)
   - Volume > 1.3× trailing 20-bar average
   - If base signal fires, evaluate liquidity filter
   - Arm with levels (real if cleared, provisional if not)

2. Phase 2 (monitor) every cycle:
   - Re-run base signal; invalidate if no longer active
   - Re-run liquidity filter; fire when cleared
   - Expire after SCALP_ARM_MAX_AGE_BARS minutes

3. Liquidity filter (`liq_estimator.py`):
   - Estimates liquidation clusters from free OI data
   - Fires only when significant opposite-side cluster sits ahead
   - No larger same-side cluster behind entry
   - Funding not extreme against direction
   - Stop placed cleanly, capped at MAX_SL_PRICE_PCT
   - RR >= MIN_RR

## Critical Rules

Always protect against invalid trade geometry.

For LONG:

```python
tp_price > entry_price
sl_price < entry_price
````

For SHORT:

```python
tp_price < entry_price
sl_price > entry_price
```

Never allow:

* LONG with TP below entry.
* SHORT with TP above entry.
* WIN with negative ROI.
* Signal saved to DB without geometry validation.
* Outcome checker calculating win/loss on invalid TP/SL.

Add geometry validation in both:

1. `strategy.py` before returning `FIRE`.
2. `main.py` before `db.save_signal()`.
3. `main.py` outcome checker before checking TP/SL hit.

## Preferred Risk Model

Default target:

```text
Leverage: 20x (the bot's own position leverage; separate from LEVERAGE_TIERS which models other traders' liquidation distribution)
RR: 1.5+
Target margin profit: 12%
Max SL in price %: 0.32%
```

Preferred config values (v14 Liquidation-Aware Scalp):

```env
LEVERAGE=20
LEVERAGE_TIERS={10: 0.20, 20: 0.25, 25: 0.20, 50: 0.20, 75: 0.10, 100: 0.05}
MMR_BUFFER=0.006

EMA_FAST, EMA_MID, EMA_SLOW=9, 21, 50
RSI_PERIOD=14
RSI_LONG_MIN, RSI_LONG_MAX=50, 68
RSI_SHORT_MIN, RSI_SHORT_MAX=32, 50

SCALP_VOLUME_MIN_MULT=1.3
TARGET_MARGIN_PROFIT=0.12
MIN_RR=1.5
MAX_SL_PRICE_PCT=0.0032

BUCKET_PCT=0.0005
CLUSTER_DECAY=0.97
CLUSTER_LOOKAROUND=0.02
CLUSTER_MIN_PERCENTILE=90
OI_POLL_SEC=60
FUNDING_EXTREME=0.0004
SCALP_ARM_MAX_AGE_BARS=10
```

If these config values do not exist, add them to `config.py`.

## Signal Frequency Target

The user wants 1–3 high-quality signals per day.

Preferred config:

```env
SIGNALS_PER_SCAN=1
MAX_CONCURRENT_SIGNALS=3
SIGNAL_COOLDOWN_MINUTES=240

MAX_DAILY_SIGNALS=3
MIN_DAILY_SIGNAL_GAP_MINUTES=180

MAX_NEW_SETUPS_PER_SCAN=2
MAX_SETUPS_SAME_DIRECTION_PER_SCAN=1
MAX_WAITING_SETUPS_TOTAL=12
MAX_WAITING_SETUPS_SAME_DIRECTION=6
SETUP_MONITOR_LIMIT=12
```

If `MAX_DAILY_SIGNALS` and `MIN_DAILY_SIGNAL_GAP_MINUTES` do not exist, add them.

## Required Helper Functions

Add or keep a helper like this in `strategy.py` and `main.py`:

```python
def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False

    if direction == "LONG":
        return tp > entry and sl < entry

    if direction == "SHORT":
        return tp < entry and sl > entry

    return False
```

Add quality validation in `strategy.py`:

```python
def _trade_quality_ok(
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    leverage: int,
) -> tuple[bool, float, float, float]:
    if not _valid_trade_geometry(direction, entry, tp, sl):
        return False, 0.0, 0.0, 0.0

    if direction == "LONG":
        risk_pct = (entry - sl) / entry * 100.0
        reward_pct = (tp - entry) / entry * 100.0
    else:
        risk_pct = (sl - entry) / entry * 100.0
        reward_pct = (entry - tp) / entry * 100.0

    if risk_pct <= 0 or reward_pct <= 0:
        return False, 0.0, 0.0, 0.0

    rr = reward_pct / risk_pct
    tp_roi = reward_pct * leverage
    sl_roi = risk_pct * leverage

    ok = (
        rr >= MIN_STRUCTURE_RR
        and tp_roi >= MIN_TP_ROI_PCT
        and sl_roi <= MAX_SL_ROI_PCT
    )

    return ok, round(tp_roi, 1), round(sl_roi, 1), round(rr, 2)
```

## Strategy.py Instructions

When editing `strategy.py`:

* Do not rewrite the whole file unless asked.
* Keep the MTF SMC structure.
* Keep MSS break confirmation.
* Keep revalidation before fire.
* Keep BTC filter.
* Keep ATR, volume, and EMA filters.
* Fix only the broken calculation or quality guard.
* Add detailed logs for rejected trades.

Important final-entry checks:

Before returning `Signal(...)`, enforce:

```python
if not _valid_trade_geometry(direction, entry, tp_price, final_sl_price):
    logger.warning(
        "[ENTRY-REJECT] %s %s invalid geometry entry=%.8g tp=%.8g sl=%.8g",
        symbol, direction, entry, tp_price, final_sl_price,
    )
    return "WAIT", None
```

Then enforce ROI quality:

```python
quality_ok, checked_tp_roi, checked_sl_roi, checked_rr = _trade_quality_ok(
    direction=direction,
    entry=entry,
    tp=tp_price,
    sl=final_sl_price,
    leverage=LEVERAGE,
)

if not quality_ok:
    logger.info(
        "[ENTRY-REJECT] %s %s quality failed rr=%.2f tp_roi=%.1f sl_roi=%.1f",
        symbol, direction, checked_rr, checked_tp_roi, checked_sl_roi,
    )
    return "WAIT", None
```

## Main.py Instructions

When editing `main.py`:

* Keep APScheduler structure.
* Keep atomic `db.claim_setup_for_fire()`.
* Keep `db.mark_setup_fire_failed()` on failure.
* Add geometry block before `db.save_signal()`.
* Add outcome geometry block before checking candles.
* Add daily max signal cap if requested.

Before saving signal:

```python
if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
    logger.error(
        "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
        sig.symbol,
        sig.direction,
        sig.entry_price,
        sig.tp_price,
        sig.sl_price,
    )
    db.mark_setup_fire_failed(setup["id"])
    continue
```

In outcome checker, before candle hit logic:

```python
if not _valid_trade_geometry(direction, entry_price, tp_price, sl_price):
    logger.error(
        "[OUTCOME-BLOCK] Invalid signal geometry #%s %s %s entry=%.8g tp=%.8g sl=%.8g",
        sig["id"],
        symbol,
        direction,
        entry_price,
        tp_price,
        sl_price,
    )
    db.update_signal_outcome(sig["id"], "expired", 0.0)
    continue
```

## Database.py Instructions

Add helpers if missing:

```python
def count_signals_since(start: datetime) -> int:
    with _conn() as con:
        row = con.execute("""
            SELECT COUNT(*) AS cnt
            FROM signals
            WHERE generated_at >= ?
        """, (start.isoformat(),)).fetchone()

        return int(row["cnt"] or 0)


def latest_signal_time() -> datetime | None:
    with _conn() as con:
        row = con.execute("""
            SELECT generated_at
            FROM signals
            ORDER BY generated_at DESC
            LIMIT 1
        """).fetchone()

        if not row:
            return None

        dt = datetime.fromisoformat(row["generated_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt
```

Do not break existing schema migrations.

## Bot.py Instructions

Telegram message must be clear and safe.

Show:

```text
20x | RR 1:2 | TP ≈ +50% ROI
```

Fix outcome labels:

```python
label = f"TARGET HIT {roi:+.1f}%"
label = f"STOP HIT {roi:+.1f}%"
```

Never format a win as `+-8.0%`.

## Webui.py Instructions

Dashboard should show:

* Strategy name.
* 1D / 4H / 1H / 15m timeframes.
* RR target.
* TP ROI target.
* SL ROI cap.
* Max daily signals.
* Signal gap.

Add config values:

```python
"min_tp_roi_pct": _safe_config_value("MIN_TP_ROI_PCT", "—"),
"target_tp_roi_pct": _safe_config_value("TARGET_TP_ROI_PCT", "—"),
"max_sl_roi_pct": _safe_config_value("MAX_SL_ROI_PCT", "—"),
"max_daily_signals": _safe_config_value("MAX_DAILY_SIGNALS", "—"),
"min_daily_signal_gap_minutes": _safe_config_value("MIN_DAILY_SIGNAL_GAP_MINUTES", "—"),
```

## Clear DB Instructions

`clear_db.py` must clear both tables:

```python
con.execute("DELETE FROM signals")
con.execute("DELETE FROM pending_setups")
con.execute("DELETE FROM sqlite_sequence WHERE name IN ('signals', 'pending_setups')")
```

## Validation Commands

After any code change, run:

```bash
python -m py_compile config.py database.py strategy.py main.py bot.py webui.py
```

On server:

```bash
cd /opt/signals
source venv/bin/activate
python -m py_compile config.py database.py strategy.py main.py bot.py webui.py
```

Restart:

```bash
sudo systemctl restart mexc-bot
sudo journalctl -u mexc-bot -f
```

Clean restart:

```bash
sudo systemctl stop mexc-bot
python clear_db.py --yes
sudo systemctl start mexc-bot
sudo journalctl -u mexc-bot -f
```

## Response Style

When making code changes:

1. Explain the problem first.
2. List exact files changed.
3. Show concise diff summary.
4. Run compile command.
5. Mention any failed validation honestly.
6. Do not claim guaranteed profit.
7. Do not remove filters only to increase signal count.
8. Prefer safe, small patches.
9. If user asks for full file, provide full updated file.
10. If logs show no signal, analyze reject reasons before changing strategy.

## Acceptance Criteria

A change is acceptable only if:

* LONG cannot save unless TP > entry and SL < entry.
* SHORT cannot save unless TP < entry and SL > entry.
* No outcome can mark WIN with negative ROI.
* Every fired signal has RR >= 2.0.
* Every fired signal has TP ROI >= configured minimum.
* Every fired signal has SL ROI <= configured maximum.
* Daily signal cap works.
* Signal gap works.
* Telegram and Web UI show correct risk model.
* `clear_db.py` clears both signals and pending setups.
* `py_compile` passes.


