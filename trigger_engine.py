"""
WebSocket trigger engine.

Checks armed setups against live WebSocket prices every TRIGGER_CHECK_SECONDS.
Fires a Telegram signal when live price enters the entry zone of an armed setup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import database as db
import strategy
import bot as tg
from ws_manager import WsPriceManager
from config import (
    MAX_ENTRY_DISTANCE_PCT,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    LEVERAGE,
)
from telegram.ext import Application

logger = logging.getLogger(__name__)


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


async def check_triggers(app: Application, ws_manager: WsPriceManager) -> None:
    if tg.paused:
        return

    now = datetime.now(timezone.utc)

    # Expire stale armed setups first
    db.expire_old_armed_setups(now)

    active_signals = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active_signals
    if slots <= 0:
        logger.debug("[TRIGGER] %d/%d active signals — no slots", active_signals, MAX_CONCURRENT_SIGNALS)
        return

    armed_setups = db.get_armed_setups()
    if not armed_setups:
        return

    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    fired_this_cycle = 0

    for setup in armed_setups:
        if fired_this_cycle >= slots:
            break

        symbol    = setup["symbol"]
        direction = setup["direction"]
        setup_id  = setup["id"]

        live_price = ws_manager.get_price(symbol)
        if live_price is None:
            continue

        trigger_price = float(setup["trigger_price"])
        entry_low     = float(setup["entry_low"])
        entry_high    = float(setup["entry_high"])

        # Late signal protection
        max_dist = MAX_ENTRY_DISTANCE_PCT / 100.0
        if direction == "LONG":
            if live_price > trigger_price * (1.0 + max_dist):
                logger.info(
                    "[TRIGGER] %s %s missed — price %.6g moved %.2f%% above trigger %.6g",
                    symbol, direction, live_price,
                    (live_price - trigger_price) / trigger_price * 100.0,
                    trigger_price,
                )
                db.mark_armed_setup_missed(setup_id, f"price {live_price:.6g} above trigger by >{MAX_ENTRY_DISTANCE_PCT}%")
                continue
        else:
            if live_price < trigger_price * (1.0 - max_dist):
                logger.info(
                    "[TRIGGER] %s %s missed — price %.6g moved %.2f%% below trigger %.6g",
                    symbol, direction, live_price,
                    (trigger_price - live_price) / trigger_price * 100.0,
                    trigger_price,
                )
                db.mark_armed_setup_missed(setup_id, f"price {live_price:.6g} below trigger by >{MAX_ENTRY_DISTANCE_PCT}%")
                continue

        # Price must be inside the entry zone
        if not (entry_low <= live_price <= entry_high):
            continue

        # Signal cooldown per coin
        if db.signal_exists_for_coin(symbol, cooldown_since):
            logger.debug("[TRIGGER] %s on cooldown — skip", symbol)
            continue

        # Build signal from armed setup + live price
        sig = strategy.calculate_signal_from_setup(setup, live_price)
        if sig is None:
            logger.warning("[TRIGGER] calculate_signal_from_setup returned None for %s", symbol)
            continue

        if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.warning(
                "[TRIGGER] %s invalid geometry entry=%.8g tp=%.8g sl=%.8g",
                symbol, sig.entry_price, sig.tp_price, sig.sl_price,
            )
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
            )

            db.mark_armed_setup_fired(setup_id, signal_id)

            await tg.broadcast_signal(app, sig, signal_id)

            fired_this_cycle += 1

            logger.info(
                "[TRIGGER] Fired signal #%d | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f",
                signal_id, symbol, direction,
                sig.entry_price, sig.tp_price, sig.sl_price,
                sig.rr, sig.score,
            )

        except Exception as e:
            logger.error("[TRIGGER] Failed to fire signal for %s: %s", symbol, e, exc_info=True)
