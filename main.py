"""
Main entry point — Stateful SMC Liquidity Sweep + Order Block Retest strategy.

Scheduler jobs:
  Every 5 min     — full setup detection scan
  Every 1 min     — monitor pending setups for OB retest entries
  Every 1 min     — check pending signal outcomes
  Every 6h        — refresh coin pool
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""

import asyncio
import logging
import shutil
import sys
from pathlib import Path
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

from mexc_client import get_klines
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
    MAX_NEW_SETUPS_PER_SCAN,
    MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
    MAX_WAITING_SETUPS_TOTAL,
    MAX_WAITING_SETUPS_SAME_DIRECTION,
    SETUP_MONITOR_LIMIT,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
)

def _backup_log_on_startup() -> None:
    """
    Backup the previous mexc_bot.log on each process start, then create a fresh log file.

    This runs before FileHandler opens the log file. It gives each bot restart a clean
    application log while preserving the previous run under logs/archive/.
    """
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return

    log_path = Path(LOG_FILE)
    archive_dir = Path(LOG_BACKUP_DIR)
    archive_dir.mkdir(parents=True, exist_ok=True)

    if log_path.exists() and log_path.stat().st_size > 0:
        ts = datetime.now(LKT).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{log_path.stem}_{ts}{log_path.suffix or '.log'}"
        shutil.copy2(log_path, archive_dir / backup_name)
        log_path.write_text("", encoding="utf-8")
    else:
        log_path.touch(exist_ok=True)


_backup_log_on_startup()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
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

    logger.info("[SETUP-SCAN] %d/%d coins after cooldown + waiting-setup filters", len(to_scan), len(coins))

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

    setups = [setup for setup in results if setup is not None]

    if not setups:
        logger.info("[SETUP-SCAN] Done — 0 new setups found")
        return

    setups.sort(key=lambda setup: float(setup.get("score", 0.0)), reverse=True)

    waiting_total = db.count_waiting_setups()
    waiting_by_direction = db.count_waiting_setups_by_direction()
    waiting_slots = max(MAX_WAITING_SETUPS_TOTAL - waiting_total, 0)
    scan_limit = min(MAX_NEW_SETUPS_PER_SCAN, waiting_slots)

    if scan_limit <= 0:
        logger.info(
            "[SETUP-SCAN] Waiting setup limit reached (%d/%d), skipping save",
            waiting_total,
            MAX_WAITING_SETUPS_TOTAL,
        )
        return

    saved = 0
    direction_counts: dict[str, int] = {}

    for setup in setups:
        if saved >= scan_limit:
            break

        direction = setup["direction"]

        if direction_counts.get(direction, 0) >= MAX_SETUPS_SAME_DIRECTION_PER_SCAN:
            logger.info(
                "[SETUP-SCAN] Skip %s %s — same-direction scan limit %d reached",
                setup["symbol"],
                direction,
                MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
            )
            continue

        current_same_direction = waiting_by_direction.get(direction, 0) + direction_counts.get(direction, 0)
        if current_same_direction >= MAX_WAITING_SETUPS_SAME_DIRECTION:
            logger.info(
                "[SETUP-SCAN] Skip %s %s — waiting same-direction cap %d reached",
                setup["symbol"],
                direction,
                MAX_WAITING_SETUPS_SAME_DIRECTION,
            )
            continue

        setup_id = db.save_pending_setup(setup)

        if setup_id:
            saved += 1
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
            logger.info(
                "[SETUP-SCAN] Saved setup #%s %s %s OB=%.6g-%.6g score=%.1f",
                setup_id,
                setup["symbol"],
                setup["direction"],
                float(setup["ob_low"]),
                float(setup["ob_high"]),
                float(setup.get("score", 0.0)),
            )

    logger.info(
        "[SETUP-SCAN] Done — %d/%d setups saved (scan_limit=%d, waiting_before=%d/%d, waiting_dir=%s, saved_dir=%s)",
        saved,
        len(setups),
        scan_limit,
        waiting_total,
        MAX_WAITING_SETUPS_TOTAL,
        waiting_by_direction,
        direction_counts,
    )


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

    setups = db.get_waiting_setups(limit=SETUP_MONITOR_LIMIT)

    if not setups:
        logger.info("[SETUP-MONITOR] No waiting setups")
        return

    logger.info("[SETUP-MONITOR] Checking top %d waiting setups", len(setups))

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
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info(f"Signal {sig['id']} expired ({symbol})")

            try:
                await tg.notify_outcome(
                    app,
                    {
                        **sig,
                        "status": "expired",
                        "pnl_roi": 0.0,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to notify expiry for {symbol}: {e}", exc_info=True)

            continue

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

        db.update_signal_outcome(sig["id"], outcome, pnl)

        logger.info(f"Signal {sig['id']} {outcome.upper()} ({symbol}) {pnl:+.1f}%")

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
            logger.error(f"Failed to notify {outcome} for {symbol}: {e}", exc_info=True)


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot — Hybrid SMC Pro (%s entry)", ENTRY_TF)

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info(f"Signal pool: {len(coins)} coins")

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

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
        "Scheduler started — setup scan='%s', monitor=%dm, entry_tf=%s, outcome_check=%dm, "
        "setup_limit=%d, same_dir_scan=%d, same_dir_waiting=%d, waiting_limit=%d, monitor_limit=%d, misfire_grace=%ds, log_file=%s",
        SETUP_SCAN_CRON_MINUTES,
        SETUP_MONITOR_MINUTES,
        ENTRY_TF,
        OUTCOME_CHECK_MINUTES,
        MAX_NEW_SETUPS_PER_SCAN,
        MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
        MAX_WAITING_SETUPS_SAME_DIRECTION,
        MAX_WAITING_SETUPS_TOTAL,
        SETUP_MONITOR_LIMIT,
        SCHEDULER_MISFIRE_GRACE_SECONDS,
        LOG_FILE,
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