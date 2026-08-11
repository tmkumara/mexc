"""
Main entry point — Precision Pullback Scalper v1.

Scheduler jobs / background tasks:
  Every SCAN_INTERVAL_MINUTES (default 5m), a few seconds after candle
  close — scanner: two-phase pending-breakout loop. Phase 1 checks every
  currently-armed pending setup for entry-breakout confirmation or expiry;
  confirmed setups fire within the daily/gap/concurrent/direction limits.
  Phase 2 scans the remaining coin pool for new EMA-trend/pullback/RSI-
  reset/confirmation-candle setups and arms new pending setups.
  Every OUTCOME_CHECK_MINUTES — outcome checker (fixed single TP/SL,
  breakeven step at BREAKEVEN_TRIGGER_ROI_PCT).
  Every COIN_REFRESH_HOURS — coin pool refresh.
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
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner
from outcome_check import check_tp_sl
from market_data import get_market_klines
from config import (
    LKT,
    LEVERAGE,
    MACRO_TF,
    TREND_TF,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCAN_INTERVAL_MINUTES,
    MONITOR_INTERVAL_MINUTES,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    STRATEGY_NAME,
    MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS,
    TP_ROI_PCT,
    SL_ROI_PCT,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
)


def _backup_log_on_startup() -> None:
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return
    log_path = Path(LOG_FILE)
    archive  = Path(LOG_BACKUP_DIR)
    archive.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 0:
        ts = datetime.now(LKT).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(log_path, archive / f"{log_path.stem}_{ts}{log_path.suffix or '.log'}")
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


# ── Setup scanner (5m) ────────────────────────────────────────────

async def scan_for_new_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_pending_setups(now)

    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    to_scan = [
        s for s in coins
        if not db.pending_setup_exists(s) and not db.signal_exists_for_coin(s, cooldown_since)
    ]

    reject_maps = [dict() for _ in to_scan]
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(
                lambda i: strategy.detect_pending_setup(to_scan[i], reject_sink=reject_maps[i]),
                range(len(to_scan)),
            )),
        )

    new_setups = [s for s in results if s is not None]

    reject_counts: dict[str, int] = {}
    for m in reject_maps:
        for k, v in m.items():
            reject_counts[k] = reject_counts.get(k, 0) + v
    reject_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(reject_counts.items(), key=lambda kv: -kv[1])
    ) or "none"

    for setup in new_setups:
        try:
            db.save_pending_setup(setup)
            logger.info(
                "[PENDING] Armed pullback %s %s zlema_15m=%.6g score=%.1f",
                setup["symbol"], setup["direction"], setup["zlema_15m"], setup["score"],
            )
        except Exception as e:
            logger.error("[SCAN] Failed to arm setup for %s: %s", setup["symbol"], e, exc_info=True)

    logger.info(
        "[SCAN] Done — %d/%d coins scanned, %d new pullback setups | rejects: %s",
        len(to_scan), len(coins), len(new_setups), reject_summary,
    )


# ── Setup monitor (1m) ────────────────────────────────────────────

async def monitor_pending_setups(app: Application) -> None:
    if tg.paused:
        return

    now = datetime.now(timezone.utc)
    db.expire_old_pending_setups(now)

    setups = db.get_pending_setups("pending_pullback") + db.get_pending_setups("pending_breakout")
    if not setups:
        return

    active_signals = db.count_active_signals()
    active_long = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig = db.latest_signal_time()

    for setup in setups:
        status, fill_price, extra = strategy.check_setup_confirmation(setup)

        if status == "expired":
            db.mark_pending_setup_expired(setup["id"])
            continue
        if status == "waiting":
            continue
        if status == "armed_breakout":
            db.update_pending_setup_breakout(setup["id"], **extra)
            continue
        if status == "missed":
            db.mark_pending_setup_missed(setup["id"], reason="score_below_min", final_score=extra["score"])
            continue

        # status == "confirmed"
        slots = MAX_CONCURRENT_SIGNALS - active_signals
        daily_ok = signals_today < MAX_DAILY_SIGNALS
        gap_ok = (
            last_sig is None
            or (now - last_sig).total_seconds() >= MIN_DAILY_SIGNAL_GAP_MINUTES * 60
        )
        direction_ok = strategy.direction_slot_available(setup["direction"], active_long, active_short)

        if slots <= 0 or not daily_ok or not gap_ok or not direction_ok:
            db.mark_pending_setup_missed(setup["id"], reason="budget_or_slot_unavailable", final_score=extra["score"])
            continue

        tp_price, sl_price = strategy.build_trade_prices(setup["direction"], fill_price)
        if not strategy.valid_trade_geometry(setup["direction"], fill_price, tp_price, sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                setup["symbol"], setup["direction"], fill_price, tp_price, sl_price,
            )
            db.mark_pending_setup_missed(setup["id"], reason="invalid_geometry_at_confirm", final_score=extra["score"])
            continue

        tp_roi, sl_roi = strategy._roi_pct(setup["direction"], fill_price, tp_price, sl_price)
        macro_label = "Bullish" if setup["macro_trend"] == 1 else "Bearish"
        sig = strategy.Signal(
            symbol=setup["symbol"],
            direction=setup["direction"],
            entry_price=fill_price,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary=f"4H:{macro_label} 1H:Agree 15m:Pullback 5m:Recovery",
            generated_at=now,
            rr=round(TP_ROI_PCT / SL_ROI_PCT, 2),
            score=extra["score"],
            entry_low=fill_price,
            entry_high=fill_price,
        )

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would confirm | %s %s @ %.6g TP=%.6g SL=%.6g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            db.mark_pending_setup_fired(setup["id"], signal_id=-1, final_score=extra["score"])
            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1
            continue

        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
                strategy_name=STRATEGY_NAME,
                score=sig.score,
                rr=sig.rr,
                entry_timeframe=ENTRY_TF,
                trend_timeframe=TREND_TF,
                setup_reason=sig.timeframe_summary,
            )
            db.mark_pending_setup_fired(setup["id"], signal_id, final_score=extra["score"])

            if not DRY_RUN:
                await tg.broadcast_signal(app, sig, signal_id)

            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1

            logger.info(
                "[SIGNAL] Confirmed #%d %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.sl_price,
            )
        except Exception as e:
            logger.error("[MONITOR] Failed to confirm setup for %s: %s", setup["symbol"], e, exc_info=True)


# ── Outcome checker ───────────────────────────────────────────────

async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        sl_price = sig["sl_price"]
        tp_price = sig["tp_price"]

        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("Signal %s expired (%s)", sig["id"], symbol)
            if not DRY_RUN:
                try:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                except Exception as e:
                    logger.error("Failed to notify expiry for %s: %s", symbol, e)
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_market_klines(symbol, ENTRY_TF, count=fetch_count)
            if df is None or df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        result = check_tp_sl(direction, entry_price, sl_price, tp_price, df, entry_candle_cutoff)
        if result is None:
            continue

        pnl = result["pnl_roi_pct"] * LEVERAGE

        db.update_signal_outcome(sig["id"], result["status"], pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], result["status"].upper(), symbol, pnl)

        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": result["status"], "pnl_roi": pnl})
            except Exception as e:
                logger.error("Failed to notify %s for %s: %s", result["status"], symbol, e)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Macro/Trend/Pullback/Entry TF: %s / %s / %s / %s", MACRO_TF, TREND_TF, "15m", ENTRY_TF)
    logger.info("Min signal score: %.0f", MIN_SIGNAL_SCORE)
    logger.info("TP: +%.1f%% ROI  SL: -%.1f%% ROI  (no breakeven)", TP_ROI_PCT, SL_ROI_PCT)
    logger.info("Leverage: %dx", LEVERAGE)
    logger.info("Dry run: %s", "enabled" if DRY_RUN else "disabled")
    logger.info(
        "[CONFIG] coin pool: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f COINGLASS=%s",
        TOP_N_COINS, COIN_POOL_MIN_SELECTED, COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
    )

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info("Coin pool: %d coins", len(coins))

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

    # The scanner must fire late enough into each candle period that the
    # just-closed candle has already cleared MIN_CANDLE_SETTLE_SECONDS --
    # otherwise strategy.detect_pending_setup's settle-age check rejects
    # every symbol on every scan (this actually happened: CANDLE_MINUTES
    # now equals SCAN_INTERVAL_MINUTES since ENTRY_TF=5m, so a naive
    # "5 seconds after each boundary" cron -- fine when ENTRY_TF was 15m
    # and gave 2 of 3 scans a stale-enough candle -- left every candle
    # only 5s old against a 90s settle requirement). +5s is a small extra
    # safety margin beyond the configured minimum.
    _settle_offset_seconds = MIN_CANDLE_SETTLE_SECONDS + 5
    _settle_offset_minute, _settle_offset_second = divmod(_settle_offset_seconds, 60)
    if _settle_offset_minute >= SCAN_INTERVAL_MINUTES:
        raise RuntimeError(
            f"MIN_CANDLE_SETTLE_SECONDS ({MIN_CANDLE_SETTLE_SECONDS}) leaves no valid "
            f"scan offset within a {SCAN_INTERVAL_MINUTES}-minute SCAN_INTERVAL_MINUTES "
            f"window -- lower MIN_CANDLE_SETTLE_SECONDS or raise SCAN_INTERVAL_MINUTES."
        )

    scheduler.add_job(
        scan_for_new_setups,
        CronTrigger(
            minute=f"{_settle_offset_minute}-59/{SCAN_INTERVAL_MINUTES}",
            second=_settle_offset_second,
        ),
        args=[app],
        id="setup_scanner",
    )

    scheduler.add_job(
        monitor_pending_setups,
        IntervalTrigger(minutes=MONITOR_INTERVAL_MINUTES),
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

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55),        id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7),             id="monthly_report")

    scheduler.start()

    logger.info(
        "Scheduler started — scan every %dm, monitor every %dm, outcome every %dm",
        SCAN_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES,
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
