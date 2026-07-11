# SL Widen + Breakeven Trail — Design Spec

## Context

The Liquidation-Aware 1m Scalp strategy (v14, merged to `main`) has been running locally against live MEXC data. Two real trades so far, both stopped out at exactly `-6.4%` ROI (`MAX_SL_PRICE_PCT = 0.0032` × `LEVERAGE = 20`):

- **ONDO_USDT SHORT**: stopped out ~4 minutes after entry — an immediate whipsaw, price never moved favorably before reversing.
- **LTC_USDT LONG**: ran ~65% of the way to TP over ~45 minutes, then fully reversed over the following hour and stopped out.

This spec covers two changes made together because they address different failure modes seen above:

1. **Widen `MAX_SL_PRICE_PCT`** (0.32% → 0.45%) — reduces whipsaw stop-outs like ONDO's. Independent of everything else in this spec.
2. **Breakeven trail** — once price has moved 50% of the way from entry toward TP, alert the user to move their real stop-loss to breakeven, and track the position internally as if that had happened. Would have converted the LTC loss into a scratch.

This bot is signal-only (never places real orders — the user manually places a LIMIT order and manually sets TP/SL on the exchange per the Telegram alert). Because of that, breakeven trail **must** be an actionable notification, not a silent internal bookkeeping change — otherwise the bot's tracked win/loss/ROI stats would describe a position the user never actually held.

## Goals

- Reduce the frequency of "whipsaw" stop-outs (immediate reversal right after entry) via a wider stop.
- Convert "ran most of the way to target, then fully reversed" trades (like LTC) into scratches instead of full losses, via an actionable breakeven alert.
- Keep the change auditable and testable: the core trigger/outcome logic must be a pure function, independent of Telegram/DB/asyncio, so it can be unit tested directly.
- No change to `bot.py`'s existing commands/messages beyond adding one new notification function. No change to `strategy.py`'s arm/monitor logic — entry, initial TP, and initial SL calculation are unaffected; this only changes how `main.py`'s outcome checker evaluates an already-fired signal over time.

## Non-Goals

- No continuous/ATR-style trailing stop beyond the single breakeven step (TP does not move).
- No change to how `strategy.py` arms/monitors/fires a signal — this spec is entirely inside `main.py`'s `check_outcomes` and the `signals` table.
- No new `reports.py` bucket — a breakeven-scratch outcome is stored as `status='loss'` with `pnl_roi≈0`, and `reports.py` needs zero changes (confirmed by reading its `_stats()`, which only branches on `status in ("win","loss","pending","expired")`).
- No backtesting harness — this ships as a forward-looking behavior change, validated by unit tests on synthetic candles plus continued live observation.

## Config Changes (`config.py`)

```python
MAX_SL_PRICE_PCT: float = float(os.getenv("MAX_SL_PRICE_PCT", "0.0045"))   # was "0.0032"
BREAKEVEN_TRIGGER_PCT: float = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "0.5"))
```

`BREAKEVEN_TRIGGER_PCT` is a fraction of the entry→TP distance (0.5 = halfway). Add `BREAKEVEN_TRIGGER_PCT=0.5` to `.env.example`'s v14 tuning block; update the `MAX_SL_PRICE_PCT` line already there from `0.0032` to `0.0045`.

## Database Changes (`database.py`)

One new nullable column on `signals`, added via the existing `ALTER TABLE` migration pattern (same style as the pre-existing `placed`/`placed_at` migration in `init_db()`):

```python
for col, definition in [
    ("placed",    "INTEGER NOT NULL DEFAULT 1"),
    ("placed_at", "TEXT"),
    ("breakeven_triggered_at", "TEXT"),     # NEW
]:
    try:
        con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
    except Exception:
        pass
```

`breakeven_triggered_at` is `NULL` until the trigger fires. Its value, once set, is the **candle timestamp** (not wall-clock notification time) at which price first reached the 50%-to-TP level — this is what lets the replay logic in `check_outcomes` know which candles (before vs. at-or-after that timestamp) should be evaluated against the original `sl_price` vs. the breakeven price (`entry_price`).

New `database.py` function:

```python
def mark_signal_breakeven_triggered(signal_id: int, triggered_at: datetime) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET breakeven_triggered_at = ?
            WHERE id = ? AND breakeven_triggered_at IS NULL
        """, (triggered_at.isoformat(), signal_id))
```

The `WHERE breakeven_triggered_at IS NULL` guard makes this idempotent — a second call (e.g. from a slightly overlapping tick) is a no-op, so the Telegram notification never double-sends as long as the caller only notifies when this function's effect actually changed a row (see below).

`get_pending_signals()` already does `SELECT *`, so `breakeven_triggered_at` comes back for free — no change needed there.

## Core Logic — `_replay_outcome` (new pure function in `main.py`)

Replaces the inline candle-scanning loop currently in `check_outcomes` with a pure, testable function:

```python
def _replay_outcome(
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff: pd.Timestamp,
    existing_trigger_ts: pd.Timestamp | None,
) -> tuple[str | None, pd.Timestamp | None, bool]:
    """
    Replay every closed candle since entry_candle_cutoff against the
    trade's TP/SL, applying the breakeven trail: once price reaches
    BREAKEVEN_TRIGGER_PCT of the way from entry to TP, the ACTIVE stop
    for all SUBSEQUENT candles becomes entry_price (breakeven) instead
    of the original sl_price. The candle where the trigger is reached is
    itself still evaluated against the ORIGINAL stop (same-bar tiebreak:
    if a single candle's range would hit both the original SL and the
    trigger level, the original SL takes precedence for that candle,
    since intra-bar ordering can't be determined from OHLC alone).

    Returns (outcome, newly_triggered_at, closed_at_breakeven):
      outcome            -- "win" | "loss" | None (still pending)
      newly_triggered_at -- candle timestamp if the trigger fired during
                            THIS call and existing_trigger_ts was None,
                            else None
      closed_at_breakeven -- True if outcome == "loss" and the stop that
                             was hit was the breakeven price, not the
                             original sl_price (caller uses this to
                             compute ~0% ROI instead of the normal loss ROI)
    """
    trigger_price = entry_price + BREAKEVEN_TRIGGER_PCT * (tp_price - entry_price)
    trigger_ts = existing_trigger_ts
    newly_triggered_at = None

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high  = float(df["high"].iloc[i])
        low   = float(df["low"].iloc[i])
        open_ = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])

        active_sl = entry_price if (trigger_ts is not None and ts > trigger_ts) else sl_price

        hit_tp = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
        hit_sl = (low <= active_sl) if direction == "LONG" else (high >= active_sl)

        if hit_tp and hit_sl:
            outcome = "win" if (
                (direction == "LONG"  and close >= open_) or
                (direction == "SHORT" and close <= open_)
            ) else "loss"
            return outcome, newly_triggered_at, (outcome == "loss" and active_sl == entry_price)

        if hit_tp:
            return "win", newly_triggered_at, False

        if hit_sl:
            return "loss", newly_triggered_at, (active_sl == entry_price)

        if trigger_ts is None:
            reached = (high >= trigger_price) if direction == "LONG" else (low <= trigger_price)
            if reached:
                trigger_ts = ts
                newly_triggered_at = ts

    return None, newly_triggered_at, False
```

Notes on correctness:
- `active_sl == entry_price` as the "was this a breakeven close" test is safe here because `entry_price` and the original `sl_price` are never equal (validated at signal-creation time by `_valid_trade_geometry`), so there's no ambiguity between "stopped at the original SL, which happens to equal entry" and "stopped at breakeven."
- Passing `trigger_ts=None` when nothing is stored in the DB yet, and folding in a same-call detection (`newly_triggered_at`), means one replay pass both detects a brand-new trigger *and* can resolve a final outcome after it, in the same call — needed for the "stall catches up and both the trigger and the close happen in one tick" edge case.

## `check_outcomes` Changes (`main.py`)

Replace the inline loop body (the `for i in range(len(df) - 1): ...` block and the `_calculate_pnl_roi` call site) with:

```python
existing_trigger_ts = None
if sig.get("breakeven_triggered_at"):
    # stored via triggered_at.isoformat() where triggered_at is a naive
    # pandas Timestamp (df.index[i] -- kline timestamps are naive
    # throughout this codebase, same convention as entry_candle_cutoff
    # below), so this parses back naive with no tz_localize needed.
    existing_trigger_ts = pd.Timestamp(sig["breakeven_triggered_at"])

outcome, newly_triggered_at, closed_at_breakeven = _replay_outcome(
    direction, entry_price, tp_price, sl_price, df, entry_candle_cutoff, existing_trigger_ts,
)

if newly_triggered_at is not None and existing_trigger_ts is None:
    db.mark_signal_breakeven_triggered(sig["id"], newly_triggered_at)
    try:
        await tg.notify_breakeven_trigger(app, sig, closing_now=(outcome is not None))
    except Exception as e:
        logger.error("Failed to notify breakeven trigger for %s: %s", symbol, e)

if outcome is None:
    continue

effective_sl_for_pnl = entry_price if closed_at_breakeven else sl_price
pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp_price, effective_sl_for_pnl)
db.update_signal_outcome(sig["id"], outcome, pnl)
logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), symbol, pnl)

try:
    await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
except Exception as e:
    logger.error("Failed to notify %s for %s: %s", outcome, symbol, e)
```

`_calculate_pnl_roi` itself is unchanged — passing `entry_price` as the `sl_price` argument for a breakeven-closed loss naturally yields `price_move_pct = 0`, hence `pnl ≈ 0.0`, with no new branch needed in that function.

## Telegram Changes (`bot.py`)

New function, styled like the existing `notify_outcome`:

```python
async def notify_breakeven_trigger(app: Application, signal_db: dict, closing_now: bool = False) -> None:
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    entry     = signal_db["entry_price"]
    arrow     = "🟢" if direction == "LONG" else "🔴"

    lines = [
        f"🔒 {_bold('Breakeven Trigger')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Price reached {int(BREAKEVEN_TRIGGER_PCT * 100)}% to target — move your stop to breakeven now.",
        f"Breakeven: {_code(f'{entry:,.6g}')}",
    ]
    if closing_now:
        lines.append(_italic("Note: this trade has already closed at breakeven by the time this alert was checked."))
    lines.append(f"🆔 ID: {_code(signal_db['id'])}")

    await _send_html(app, "\n".join(lines))
```

Import `BREAKEVEN_TRIGGER_PCT` from `config` in `bot.py`. No other `bot.py` function changes.

## Testing

New `tests/test_outcome_replay.py` (pure unit tests on `_replay_outcome`, synthetic candles, no DB/Telegram/network):

- Normal TP hit, no breakeven involved.
- Normal SL hit, no breakeven involved (price never reaches the trigger level).
- Breakeven triggers (price reaches 50%-to-TP), later closes at breakeven (`closed_at_breakeven=True`, `outcome="loss"`).
- Breakeven triggers, later goes on to hit the real TP (`outcome="win"`).
- Same-candle tiebreak: a single candle's range crosses both the original SL and the trigger level — asserts `outcome="loss"`, `closed_at_breakeven=False` (original SL wins), and the trigger is NOT recorded (`newly_triggered_at is None`) for that candle.
- `existing_trigger_ts` passed in (simulating a second `check_outcomes` tick after a prior tick already recorded the trigger) — candles before that timestamp still resolve against the original SL, candles at/after resolve against breakeven.
- Still-pending case: no TP/SL/trigger hit anywhere in the supplied candles → `(None, None, False)`.

Existing tests (`tests/test_liq_estimator.py`, `tests/test_mexc_client.py`, `tests/test_strategy_liq_scalp.py`) are unaffected — none of them touch `main.py`.

## Rollout

This can go straight to `main` (it's a `check_outcomes`/`bot.py`/`config.py`/`database.py` change, not a `strategy.py` signal-generation change) once tests pass, `py_compile` is clean, and the user restarts their local bot to pick it up. `SIGNAL_EXPIRE_HOURS` and other v14 config are unaffected. `.env.example` needs the two constant updates noted above. The user's local `.env` does not set `MAX_SL_PRICE_PCT` explicitly (confirmed), so it will pick up the new `0.0045` default automatically on restart with no manual `.env` edit needed.
