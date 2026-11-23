"""Public alert runner for confirmed 4H RSI reversals with relative volume."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import logging
from math import isfinite

import pandas as pd

import bot
import coin_scanner
import database
from config import DRY_RUN, MAX_DAILY_SIGNALS, MIN_CANDLE_SETTLE_SECONDS, TELEGRAM_CHANNEL_ID
from market_data import get_market_klines
from rsi_divergence_detector import discover


NAME = "4H RSI Divergence + Relative Volume"
WINDOW = timedelta(minutes=15)
FOUR_HOURS = timedelta(hours=4)
STOP = .0125
VOLUME_MINIMUM = 1.5
LEVERAGE = 20
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    symbol: str
    signal_at: datetime
    direction: str
    volume: float
    level: float
    entry: float
    stop: float
    time_exit: datetime


def _utc(frame):
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame


def find_alert(symbol, bars, entry_bars, now):
    bars, entry_bars = _utc(bars), _utc(entry_bars)
    cutoff = now - timedelta(seconds=MIN_CANDLE_SETTLE_SECONDS)
    closed = bars.loc[bars.index + FOUR_HOURS <= cutoff]
    if closed.empty:
        return None
    signal_at = closed.index[-1] + FOUR_HOURS
    entry_at = signal_at + WINDOW
    if not entry_at <= cutoff < entry_at + WINDOW or entry_at not in entry_bars.index:
        return None
    previous_volume = closed.volume.iloc[-21:-1]
    if len(previous_volume) != 20 or previous_volume.mean() <= 0:
        return None
    volume = closed.volume.iloc[-1] / previous_volume.mean()
    if not isfinite(volume) or volume < VOLUME_MINIMUM:
        return None
    for kind, stamp, setup in discover(closed, symbol):
        if kind == "price_confirmed" and stamp == signal_at:
            entry = float(entry_bars.at[entry_at, "open"])
            if isfinite(entry) and entry > 0:
                return Alert(symbol, signal_at, "LONG" if setup.side == 1 else "SHORT",
                             volume, setup.middle, entry, entry*(1-setup.side*STOP),
                             entry_at+timedelta(hours=24))
    return None


def _scan_one(symbol, now):
    try:
        return find_alert(symbol, get_market_klines(symbol, "4h", count=240),
                          get_market_klines(symbol, "15m", count=4), now)
    except Exception:
        logger.exception("RSI alert scan failed for %s", symbol)
        return None


def format_alert(alert):
    return (
        f"<b>{escape(NAME)}</b>\n"
        f"<b>{escape(alert.symbol)} {escape(alert.direction)}</b>\n"
        f"Entry reference: {alert.entry:.8g}\n"
        f"Stop: {alert.stop:.8g} (1.25% price distance)\n"
        f"Time exit: {alert.time_exit:%Y-%m-%d %H:%M} UTC\n"
        f"4H confirmation: {alert.signal_at:%Y-%m-%d %H:%M} UTC through {alert.level:.8g}\n"
        f"Relative volume: {alert.volume:.2f}×\n"
        f"Modeled leverage: {LEVERAGE}×"
    )


async def scan(app):
    if bot.paused:
        return
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=MIN_CANDLE_SETTLE_SECONDS)
    if not 15 <= (cutoff.hour % 4)*60 + cutoff.minute < 30:
        return
    symbols = await asyncio.to_thread(coin_scanner.get_whitelisted_pairs)
    alerts = await asyncio.gather(*(asyncio.to_thread(_scan_one, symbol, now) for symbol in symbols))
    for alert in filter(None, alerts):
        if DRY_RUN:
            logger.info("RSI dry-run alert: %s %s", alert.symbol, alert.direction)
            continue
        if not database.reserve_rsi_alert(alert.symbol, alert.signal_at, now, MAX_DAILY_SIGNALS):
            continue
        try:
            await app.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=format_alert(alert), parse_mode="HTML")
        except Exception:
            logger.exception("RSI public alert delivery failed; reservation retained for %s", alert.symbol)
