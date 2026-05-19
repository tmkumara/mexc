"""
Main entry point — Phase 1.1 Momentum Pullback Scalper.

Scheduler jobs:
  Every 1 min     — full setup detection scan
  Every 1 min     — monitor pending pullback setups for entries
  Every 1 min     — check pending signal outcomes
  Every 6h        — refresh coin pool
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner

from mexc_client import get_klines, get_current_price
from config import (
    LKT,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    MAX_CONCURRENT_SIGNALS,
    LEVERAGE,
    ENTRY_TF,
    SETUP_SCAN_CRON_MINUTES,
    SETUP_MONITOR_MINUTES,
    SIGNALS_PER_SCAN,
    OUTCOME_CHECK_MINUTES,
    CANDLE_MINUTES,
    SCAN_WORKERS,
    MIN_SIGNAL_SCORE,
    SETUPS_PER_SCAN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mexc_bot.log"),
    ],
)

logging.Formatter.converter = lambda *args: datetime.now(LKT).timetuple()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── setup detection scan ──────────────────────────────────────────

async def scan_for_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SETUP-SCAN] Paused, skipping")
        return

    coins = coin_scanner.get_cached_coins()

    if not coins:
        logger.warning("[SETUP-SCAN] Empty coin pool, skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    to_scan = [
        symbol
        for symbol in coins
        if not db.signal_exists_for_coin(symbol, cooldown_since)
        and not db.pending_setup_exists(symbol)
    ]

    logger.info(f"[SETUP-SCAN] {len(to_scan)}/{len(coins)} coins after filters")

    if not to_scan:
        return

    def _detect(symbol: str):
        try:
            return strategy.detect_setup(symbol)
        except Exception as e:
            logger.error(f"[SETUP-SCAN] {symbol} setup error: {e}", exc_info=True)
            return None

    loop = asyncio.get_event_loop()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(_detect, to_scan)),
        )

    setups = [
        setup
        for setup in results
        if setup is not None and float(setup.get("score", 0.0)) >= MIN_SIGNAL_SCORE
    ]

    if not setups:
        logger.info("[SETUP-SCAN] Done — 0 new setups found")
        return

    setups.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    setups = setups[:SETUPS_PER_SCAN]

    saved = 0

    for setup in setups:
        setup_id = db.save_pending_setup(setup)

        if setup_id:
            saved += 1
            logger.info(
                f"[SETUP-SCAN] Saved setup #{setup_id} "
                f"{setup['symbol']} {setup['direction']} "
                f"zone={setup['ob_low']:.6g}-{setup['ob_high']:.6g} "
                f"score={setup['score']}"
            )

    logger.info(f"[SETUP-SCAN] Done — {saved}/{len(setups)} setups saved")


# ── pending setup monitor ─────────────────────────────────────────

async def monitor_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SETUP-MONITOR] Paused, skipping")
        return

    active = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active

    if slots <= 0:
        logger.info(f"[SETUP-MONITOR] {active}/{MAX_CONCURRENT_SIGNALS} active signals, skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_waiting_setups(now)

    setups = db.get_waiting_setups(limit=100)

    if not setups:
        logger.info("[SETUP-MONITOR] No waiting setups")
        return

    logger.info(f"[SETUP-MONITOR] Checking {len(setups)} waiting setups")

    fired_signals = []

    for setup in setups:
        status, sig = strategy.evaluate_pending_setup(setup)

        if status == "EXPIRED":
            db.mark_setup_expired(setup["id"])
            logger.info(f"[SETUP-MONITOR] Expired setup #{setup['id']} {setup['symbol']}")

        elif status == "INVALIDATED":
            db.mark_setup_invalidated(setup["id"])
            logger.info(f"[SETUP-MONITOR] Invalidated setup #{setup['id']} {setup['symbol']}")

        elif status == "FIRE" and sig is not None:
            fired_signals.append((setup, sig))

    if not fired_signals:
        logger.info("[SETUP-MONITOR] Done — 0 entries fired")
        return

    fired_signals.sort(key=lambda item: item[1].score, reverse=True)
    to_send = fired_signals[:min(SIGNALS_PER_SCAN, slots)]

    logger.info(
        f"[SETUP-MONITOR] {len(fired_signals)} entry signal(s), sending {len(to_send)}"
    )

    for setup, sig in to_send:
        signal_id = db.save_signal(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=sig.entry_price,
            tp_price=sig.tp_price,
            sl_price=sig.sl_price,
            leverage=sig.leverage,
            generated_at=sig.generated_at,
        )

        db.mark_setup_fired(setup["id"], signal_id)

        try:
            await tg.broadcast_signal(app, sig, signal_id)
            logger.info(
                f"[SETUP-MONITOR] Sent signal #{signal_id} "
                f"from setup #{setup['id']} {sig.symbol} {sig.direction} score={sig.score}"
            )
        except Exception as e:
            logger.error(f"Failed to broadcast {sig.symbol}: {e}", exc_info=True)


# ── outcome checker ───────────────────────────────────────────────

def _calculate_pnl_roi(
    direction: str,
    outcome: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
) -> float:
    if outcome == "win":
        if direction == "LONG":
            price_move_pct = (tp_price - entry_price) / entry_price * 100
        else:
            price_move_pct = (entry_price - tp_price) / entry_price * 100
    else:
        if direction == "LONG":
            price_move_pct = (sl_price - entry_price) / entry_price * 100
        else:
            price_move_pct = (entry_price - sl_price) / entry_price * 100

    return price_move_pct * LEVERAGE


async def _close_signal(app: Application, sig: dict, outcome: str, pnl: float) -> None:
    db.update_signal_outcome(sig["id"], outcome, pnl)

    logger.info(
        f"Signal {sig['id']} {outcome.upper()} ({sig['symbol']}) {pnl:+.1f}%"
    )

    try:
        await tg.notify_outcome(
            app,
            {
                **sig,
                "status": outcome,
                "pnl_roi": pnl,
            },
        )
    except Exception as e:
        logger.error(f"Failed to notify {outcome} for {sig['symbol']}: {e}", exc_info=True)


async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        tp_price = sig["tp_price"]
        sl_price = sig["sl_price"]
        entry_price = sig["entry_price"]

        generated = datetime.fromisoformat(sig["generated_at"])

        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            await _close_signal(app, sig, "expired", 0.0)
            continue

        # Fast live-price check. This marks SL/TP faster without waiting
        # for the next completed 5m candle.
        try:
            current_price = get_current_price(symbol)
        except Exception as e:
            logger.warning(f"Could not fetch current price for {symbol}: {e}")
            current_price = None

        instant_outcome = None

        if current_price is not None:
            if direction == "LONG":
                if current_price >= tp_price:
                    instant_outcome = "win"
                elif current_price <= sl_price:
                    instant_outcome = "loss"
            else:
                if current_price <= tp_price:
                    instant_outcome = "win"
                elif current_price >= sl_price:
                    instant_outcome = "loss"

        if instant_outcome is not None:
            pnl = _calculate_pnl_roi(
                direction=direction,
                outcome=instant_outcome,
                entry_price=entry_price,
                tp_price=tp_price,
                sl_price=sl_price,
            )

            db.update_signal_outcome(sig["id"], instant_outcome, pnl)

            logger.info(
                f"Signal {sig['id']} {instant_outcome.upper()} ({symbol}) "
                f"{pnl:+.1f}% live_price={current_price}"
            )

            try:
                await tg.notify_outcome(
                    app,
                    {
                        **sig,
                        "status": instant_outcome,
                        "pnl_roi": pnl,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to notify {instant_outcome} for {symbol}: {e}", exc_info=True)

            continue

        # Fallback candle check.
        elapsed_min = max(
            (now - generated).total_seconds() / 60,
            CANDLE_MINUTES,
        )

        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_klines(symbol, ENTRY_TF, count=fetch_count)

            if df.empty or len(df) < 2:
                continue

        except Exception as e:
            logger.warning(f"Could not fetch candles for {symbol}: {e}")
            continue

        entry_candle_cutoff = (
            generated - timedelta(minutes=CANDLE_MINUTES)
        ).replace(tzinfo=None)

        outcome = None

        for i in range(len(df) - 1):
            if df.index[i] <= entry_candle_cutoff:
                continue

            high = float(df["high"].iloc[i])
            low = float(df["low"].iloc[i])
            open_price = float(df["open"].iloc[i])
            close_price = float(df["close"].iloc[i])

            if direction == "LONG":
                hit_tp = high >= tp_price
                hit_sl = low <= sl_price
            else:
                hit_tp = low <= tp_price
                hit_sl = high >= sl_price

            if hit_tp and hit_sl:
                if direction == "LONG":
                    outcome = "win" if close_price >= open_price else "loss"
                else:
                    outcome = "win" if close_price <= open_price else "loss"
                break

            if hit_tp:
                outcome = "win"
                break

            if hit_sl:
                outcome = "loss"
                break

        if outcome is None:
            continue

        pnl = _calculate_pnl_roi(
            direction=direction,
            outcome=outcome,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        await _close_signal(app, sig, outcome, pnl)


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot — Phase 1.1 Momentum Pullback Scalper")

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info(f"Signal pool: {len(coins)} coins")

    app = tg.build_app()

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        scan_for_setups,
        CronTrigger(minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="setup_scanner",
    )

    scheduler.add_job(
        monitor_setups,
        IntervalTrigger(minutes=SETUP_MONITOR_MINUTES),
        args=[app],
        id="setup_monitor",
    )

    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app],
        id="outcome_checker",
    )

    scheduler.add_job(
        coin_scanner.refresh_coin_list,
        CronTrigger(hour=f"*/{COIN_REFRESH_HOURS}"),
        id="coin_refresh",
    )

    async def _daily(app=app):
        await tg.auto_daily_report(type("ctx", (), {"application": app})())

    async def _weekly(app=app):
        await tg.auto_weekly_report(type("ctx", (), {"application": app})())

    async def _monthly(app=app):
        await tg.auto_monthly_report(type("ctx", (), {"application": app})())

    scheduler.add_job(_daily, CronTrigger(hour=23, minute=55), id="daily_report")
    scheduler.add_job(_weekly, CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7), id="monthly_report")

    scheduler.start()

    logger.info(
        f"Scheduler started — setup scan='{SETUP_SCAN_CRON_MINUTES}', "
        f"monitor={SETUP_MONITOR_MINUTES}m, entry_tf={ENTRY_TF}"
    )

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Bot is running. Press Ctrl+C to stop.")

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())