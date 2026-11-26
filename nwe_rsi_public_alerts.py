"""Public Telegram alerts for the 40-contract NWE + RSI strategy."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import logging
from math import isfinite

import pandas as pd

import bot
import database
import nwe_order_watch
from config import (DRY_RUN, LKT, MAX_DAILY_SIGNALS, MIN_CANDLE_SETTLE_SECONDS, SCAN_WORKERS,
                    TELEGRAM_CHANNEL_ID)
from mexc_client import get_klines
from nwe_rsi_local import signal_frame


NAME = "1H / 4H Nadaraya-Watson Envelope + RSI"
BASES = ("ADA BNB ETH SOL BTC XRP DOGE LINK AVAX LTC BCH DOT TRX SUI NEAR UNI AAVE XLM "
         "ATOM ARB OP ETC APT INJ ICP HBAR RUNE ALGO FET GRT MANA SAND WIF SEI TIA JUP PYTH ENS CRV COMP")
SYMBOLS = tuple(f"{base}_USDT" for base in BASES.split())
INTERVALS = {"1H": pd.Timedelta(hours=1), "4H": pd.Timedelta(hours=4)}
DELAY = pd.Timedelta(minutes=15)
PAGE = 520
LEVERAGE = 20
ROI = .30
FEE = .0006
SLIPPAGE = .0002
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    symbol: str
    direction: str
    signal_at: datetime
    entry: float
    target: float
    close: float
    band: float
    rsi: float
    excursion_rsi: float
    timeframe: str = "4H"


def target_price(entry, side):
    """Indicative traded target for +30% net before unknown funding."""
    slipped_entry = entry*(1+side*SLIPPAGE)
    execution = slipped_entry*(side+FEE+ROI/LEVERAGE)/(side-FEE)
    return execution/(1-side*SLIPPAGE)


def _utc(frame):
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame


def _candidate(symbol, bars, signal_at, timeframe="4H"):
    interval = INTERVALS[timeframe]
    bars = _utc(bars)
    closed = bars.loc[bars.index < signal_at,"close"]
    expected = pd.date_range(signal_at-2*PAGE*interval, signal_at, freq=interval, inclusive="left")
    if not closed.index.equals(expected) or not closed.map(lambda value: isfinite(value) and value>0).all():
        raise ValueError(f"{timeframe} history incomplete, unaligned or invalid")
    latest = signal_frame(closed, duration=interval).iloc[-1]
    if latest.signal not in ("LONG","SHORT"):
        return None
    return latest


def _history(symbol, signal_at, timeframe="4H"):
    interval = INTERVALS[timeframe]
    pieces = []
    for end in (signal_at-PAGE*interval, signal_at):
        start = end-PAGE*interval
        pieces.append(get_klines(symbol,timeframe.lower(),count=PAGE,start=int(start.timestamp()),
                                 end=int(end.timestamp())-1))
    return pd.concat(pieces)


def find_alert(symbol, signal_at, timeframe="4H"):
    latest = _candidate(symbol,_history(symbol,signal_at,timeframe),signal_at,timeframe)
    if latest is None:
        return None
    reference_at=signal_at+DELAY
    bar=_utc(get_klines(symbol,"15m",count=4))
    if bar.index.has_duplicates or reference_at not in bar.index:
        raise ValueError("Required 15m entry reference unavailable")
    entry=float(bar.at[reference_at,"open"])
    if not isfinite(entry) or entry<=0:
        raise ValueError("Invalid entry reference")
    side=1 if latest.signal=="LONG" else -1
    return Alert(symbol,latest.signal,signal_at,entry,target_price(entry,side),latest.close,
                 latest.lower if side==1 else latest.upper,latest.rsi14,latest.excursion_rsi,timeframe)


def _scan_one(symbol, signal_at, timeframe):
    try:
        return find_alert(symbol,signal_at,timeframe),True
    except Exception:
        logger.exception("NWE-RSI %s scan failed for %s",timeframe,symbol)
        return None,False


def format_alert(alert):
    is_long = alert.direction == "LONG"
    marker = "🟢" if is_long else "🔴"
    band = "lower" if is_long else "upper"
    pair = escape(alert.symbol.replace("_", "/"))
    local_time = alert.signal_at.astimezone(LKT).strftime("%Y-%m-%d %H:%M LKT")
    return "\n".join((
        f"{marker} <b>{escape(alert.direction)} — {pair} Futures · {escape(alert.timeframe)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry: <code>{alert.entry:.8g}</code>",
        f"🎯 TP: <code>{alert.target:.8g}</code> <i>+30% modeled net ROI before funding</i>",
        "🛑 SL: None",
        f"⚡ Leverage: <code>{LEVERAGE}×</code> <i>(modeled)</i>",
        f"🧭 Setup: {escape(alert.timeframe)} close back inside the {band} band · RSI {alert.excursion_rsi:.1f} → {alert.rsi:.1f}",
        "📈 Strategy: Nadaraya–Watson + RSI",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ Signal close: <code>{local_time}</code>",
        "⚠️ <i>Alert only. Actual fills, fees and funding may vary.</i>",
    ))


async def scan(app):
    if bot.paused:
        return
    now=datetime.now(timezone.utc)
    cutoff=pd.Timestamp(now)-pd.Timedelta(seconds=MIN_CANDLE_SETTLE_SECONDS)
    boundary=cutoff.floor("h")
    if not boundary+DELAY<=cutoff<boundary+2*DELAY:
        return
    loop=asyncio.get_running_loop()
    # The existing delivery key is symbol + close time, so 4H wins same-coin collisions.
    timeframes = ("4H", "1H") if boundary.hour % 4 == 0 else ("1H",)
    alerts = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for timeframe in timeframes:
            results = await asyncio.gather(*(
                loop.run_in_executor(pool, _scan_one, symbol, boundary, timeframe) for symbol in SYMBOLS
            ))
            candidates = [alert for alert, _ in results if alert is not None]
            logger.info("NWE-RSI %s scan: %d/%d histories checked, %d candidates",
                        timeframe, sum(ok for _, ok in results), len(SYMBOLS), len(candidates))
            alerts.extend(candidates)
    for alert in alerts:
        if DRY_RUN:
            logger.info("NWE-RSI dry-run alert: %s %s",alert.symbol,alert.direction)
            continue
        if not database.reserve_rsi_alert(alert.symbol,alert.signal_at,now,MAX_DAILY_SIGNALS):
            continue
        try:
            order_id=await asyncio.to_thread(nwe_order_watch.register,alert,LEVERAGE)
            await app.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID,text=format_alert(alert),parse_mode="HTML",
                                       reply_markup=nwe_order_watch.keyboard(order_id))
            logger.info("NWE-RSI %s alert delivered for %s %s", alert.timeframe, alert.symbol, alert.direction)
        except Exception:
            logger.exception("NWE-RSI alert delivery failed; reservation retained for %s",alert.symbol)
