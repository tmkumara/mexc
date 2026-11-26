"""Manual NWE order marking and one-time Telegram breakeven reminders."""
import asyncio
from datetime import datetime, timezone
from html import escape
import logging
from math import isfinite

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from config import DRY_RUN, TELEGRAM_CHANNEL_ID
from database import _conn
from mexc_client import get_tickers


PREFIX = "nwe_order:"
BREAKEVEN_ROI = .20
log = logging.getLogger(__name__)


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nwe_order_watch (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL, direction TEXT NOT NULL, entry REAL NOT NULL,
                leverage INTEGER NOT NULL, signal_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unmarked',
                timeframe TEXT NOT NULL DEFAULT '4H',
                chat_id INTEGER, message_id INTEGER, marked_by INTEGER, marked_at TEXT,
                trigger_price REAL, notified_at TEXT,
                UNIQUE(symbol, signal_at)
            )
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(nwe_order_watch)")}
        if "timeframe" not in columns:
            conn.execute("ALTER TABLE nwe_order_watch ADD COLUMN timeframe TEXT NOT NULL DEFAULT '4H'")


def register(alert, leverage):
    with _conn() as conn:
        inserted = conn.execute("""
            INSERT OR IGNORE INTO nwe_order_watch(symbol,direction,entry,leverage,signal_at,timeframe)
            VALUES (?,?,?,?,?,?)
        """, (alert.symbol, alert.direction, alert.entry, leverage, alert.signal_at.isoformat(), getattr(alert,"timeframe","4H")))
        if inserted.rowcount:
            return inserted.lastrowid
        row = conn.execute("SELECT id FROM nwe_order_watch WHERE symbol=? AND signal_at=?",
                           (alert.symbol,alert.signal_at.isoformat())).fetchone()
        return row["id"]


def keyboard(order_id, watching=False):
    action, text = ("stop","Stop watching") if watching else ("mark","Mark order placed")
    return InlineKeyboardMarkup([[InlineKeyboardButton(text,callback_data=f"{PREFIX}{action}:{order_id}")]])


def _now():
    return datetime.now(timezone.utc).isoformat()


def update_watch(order_id, action, chat_id, message_id, user_id):
    with _conn() as conn:
        if action == "mark":
            changed=conn.execute("""
                UPDATE nwe_order_watch SET status='watching',chat_id=?,message_id=?,marked_by=?,marked_at=?
                WHERE id=? AND status='unmarked'
            """,(chat_id,message_id,user_id,_now(),order_id))
        else:
            changed=conn.execute("""
                UPDATE nwe_order_watch SET status='cancelled'
                WHERE id=? AND chat_id=? AND message_id=? AND status IN ('watching','triggered')
            """,(order_id,chat_id,message_id))
        return bool(changed.rowcount)


def _matches_channel(chat):
    configured=str(TELEGRAM_CHANNEL_ID)
    if configured.startswith("@"):
        return bool(chat.username and f"@{chat.username}".lower()==configured.lower())
    return str(chat.id)==configured


async def callback(update, context):
    query=update.callback_query
    message=query.message
    if message is None or not _matches_channel(message.chat):
        await query.answer("This button belongs to the signal channel.",show_alert=True)
        return
    member=await context.bot.get_chat_member(message.chat.id,query.from_user.id)
    if member.status not in ("creator","administrator"):
        await query.answer("Only channel admins can mark orders.",show_alert=True)
        return
    _,action,raw_id=query.data.split(":")
    order_id=int(raw_id)
    changed=await asyncio.to_thread(update_watch,order_id,action,message.chat.id,message.message_id,query.from_user.id)
    if not changed:
        await query.answer("This order was already handled.")
        return
    await query.answer("Watching the alert entry price." if action=="mark" else "Watch cancelled.")
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard(order_id,True) if action=="mark" else None)
    except Exception:
        log.exception("Order watch state saved but button update failed: %s",order_id)


def attach(app):
    app.add_handler(CallbackQueryHandler(callback,pattern=r"^nwe_order:(mark|stop):[0-9]{1,15}$"))


def active_orders():
    with _conn() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT * FROM nwe_order_watch WHERE status IN ('watching','triggered') ORDER BY id
        """)]


def trigger_price(order):
    side=1 if order["direction"]=="LONG" else -1
    return order["entry"]*(1+side*BREAKEVEN_ROI/order["leverage"])


def crosses(order, quote):
    if not isfinite(quote) or quote<=0:
        return False
    level=trigger_price(order)
    return quote>=level if order["direction"]=="LONG" else quote<=level


def save_trigger(order_id, price):
    with _conn() as conn:
        return bool(conn.execute("""
            UPDATE nwe_order_watch SET status='triggered',trigger_price=?
            WHERE id=? AND status='watching'
        """,(price,order_id)).rowcount)


def is_triggered(order_id):
    with _conn() as conn:
        return conn.execute("SELECT 1 FROM nwe_order_watch WHERE id=? AND status='triggered'",
                            (order_id,)).fetchone() is not None


def mark_notified(order_id):
    with _conn() as conn:
        conn.execute("UPDATE nwe_order_watch SET status='notified',notified_at=? WHERE id=? AND status='triggered'",
                     (_now(),order_id))


def quote_for(tickers, symbol):
    try:
        return float(tickers.get(symbol,{}).get("lastPrice"))
    except (TypeError,ValueError):
        return float("nan")


async def watch(app):
    if DRY_RUN:
        return
    orders=await asyncio.to_thread(active_orders)
    if not orders:
        return
    tickers={}
    if any(order["status"]=="watching" for order in orders):
        try:
            tickers=await asyncio.to_thread(get_tickers)
        except Exception:
            log.exception("Order watch: MEXC ticker fetch failed")
    for order in orders:
        if order["status"]=="watching":
            price=quote_for(tickers,order["symbol"])
            if not crosses(order,price) or not await asyncio.to_thread(save_trigger,order["id"],price):
                continue
            order["trigger_price"]=price
        if not await asyncio.to_thread(is_triggered,order["id"]):
            continue
        side=1 if order["direction"]=="LONG" else -1
        gross=100*order["leverage"]*side*(order["trigger_price"]/order["entry"]-1)
        pair=escape(order["symbol"].replace("_","/"))
        text=(f"🔔 <b>Move stop to entry · {pair} {order['direction']} · {escape(order['timeframe'])}</b>\n"
              f"Modeled gross ROI: <b>{gross:+.1f}%</b> at {order['leverage']}×\n"
              f"Alert entry: <code>{order['entry']:.8g}</code> · MEXC last price: "
              f"<code>{order['trigger_price']:.8g}</code>\n"
              "If your position is still open, move the stop to your actual fill price on MEXC. "
              "Fees can make an entry-price exit a net loss.")
        try:
            await app.bot.send_message(chat_id=order["chat_id"],text=text,parse_mode="HTML",
                                       reply_to_message_id=order["message_id"],
                                       allow_sending_without_reply=True)
        except Exception:
            log.exception("Order watch: reminder delivery failed for %s",order["symbol"])
            continue
        await asyncio.to_thread(mark_notified,order["id"])
        log.info("Order watch: breakeven reminder delivered for %s (watch %s)",order["symbol"],order["id"])
