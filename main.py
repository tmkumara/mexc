"""
Main entry point — Squeeze Momentum + WaveTrend + Supertrend strategy.

Scheduler jobs:
  Every 1 min     — scan coin pool and fire direct signals
  Every 1 min     — check pending signal outcomes
  Every 6h        — refresh coin pool
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report

Old pending setup / OB retest strategy is removed from active use.
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
    STRATEGY_NAME,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    MAX_CONCURRENT_SIGNALS,
    LEVERAGE,
    ENTRY_TF,
    SETUP_SCAN_CRON_MINUTES,
    SIGNALS_PER_SCAN,
    OUTCOME_CHECK_MINUTES,
    CANDLE_MINUTES,
    SCAN_WORKERS,
    MIN_SIGNAL_SCORE,
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


# ── direct signal scan ────────────────────────────────────────────

async def scan_for_signals(app: Application) -> None:
    if tg.paused:
        logger.info("[SIGNAL-SCAN] Paused, skipping")
        return

    active = db.count_active_signals()

    if active >= MAX_CONCURRENT_SIGNALS:
        logger.info(f"[SIGNAL-SCAN] {active}/{MAX_CONCURRENT_SIGNALS} active signals, skipping")
        return

    slots = MAX_CONCURRENT_SIGNALS - active

    coins = coin_scanner.get_cached_coins()

    if not coins:
        logger.warning("[SIGNAL-SCAN] Empty coin pool, skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    to_scan = [
        symbol
        for symbol in coins
        if not db.signal_exists_for_coin(symbol, cooldown_since)
    ]

    logger.info(f"[SIGNAL-SCAN] {len(to_scan)}/{len(coins)} coins after cooldown filters")

    if not to_scan:
        return

    def _analyze(symbol: str):
        try:
            return strategy.analyze_coin(symbol)
        except Exception as e:
            logger.error(f"[SIGNAL-SCAN] {symbol} analysis error: {e}", exc_info=True)
            return None

    loop = asyncio.get_event_loop()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(_analyze, to_scan)),
        )

    signals = [
        sig
        for sig in results
        if sig is not None and float(sig.score) >= MIN_SIGNAL_SCORE
    ]

    if not signals:
        logger.info("[SIGNAL-SCAN] Done — 0 signals found")
        return

    signals.sort(key=lambda item: item.score, reverse=True)
    to_send = signals[:min(SIGNALS_PER_SCAN, slots)]

    logger.info(
        f"[SIGNAL-SCAN] {len(signals)} signal(s), sending {len(to_send)}"
    )

    for sig in to_send:
        signal_id = db.save_signal(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=sig.entry_price,
            tp_price=sig.tp_price,
            sl_price=sig.sl_price,
            leverage=sig.leverage,
            generated_at=sig.generated_at,
        )

        try:
            await tg.broadcast_signal(app, sig, signal_id)
            logger.info(
                f"[SIGNAL-SCAN] Sent signal #{signal_id} "
                f"{sig.symbol} {sig.direction} score={sig.score}"
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

            await _close_signal(app, sig, instant_outcome, pnl)
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

        await _close_signal(app, sig, outcome, pnl)


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info(f"Starting MEXC Signal Bot — {STRATEGY_NAME}")

    db.init_db()

    logger.info("Loading ranked coin pool...")
    coins = coin_scanner.refresh_coin_list()
    ranked_preview = coin_scanner.get_cached_coin_scores()[:10]

    logger.info(f"Signal pool: {len(coins)} ranked coins")

    if ranked_preview:
        logger.info(
            "[COIN-RANK] startup top ranked: "
            + ", ".join(
                f"{row.get('symbol', '').replace('_USDT', '')}:{row.get('score', 0)}"
                for row in ranked_preview
            )
        )

    app = tg.build_app()

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        scan_for_signals,
        CronTrigger(minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="signal_scanner",
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
        f"Scheduler started — signal scan='{SETUP_SCAN_CRON_MINUTES}', "
        f"outcome={OUTCOME_CHECK_MINUTES}m, entry_tf={ENTRY_TF}"
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