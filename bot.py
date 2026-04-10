"""
Telegram bot: handles commands and broadcasts signals to the channel.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import reports
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, RISK_REMINDER_EVERY_N

if TYPE_CHECKING:
    from strategy.signal_engine import ScalpingSignal

logger = logging.getLogger(__name__)


# ─────────────────────────── helpers ────────────────────────────

async def _send(app: Application, text: str, chat_id: str = None):
    """Send markdown message to the channel (or a specific chat)."""
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


def format_signal_message(signal) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    lines = [
        f"{arrow} — *{coin}* Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:     `${signal.entry_price:,.6g}`",
        f"🎯 TP:        `${signal.tp_price:,.6g}`  _(+{signal.tp_roi_pct:.0f}% ROI)_",
        f"🛑 SL:        `${signal.sl_price:,.6g}`  _(-{signal.sl_roi_pct:.0f}% ROI)_",
        f"⚡ Leverage:  `{signal.leverage}x`",
        f"💼 Risk:      `{signal.risk_pct:.0f}% of balance`",
        f"📊 {signal.timeframe_summary}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ `{signal.generated_at.strftime('%Y-%m-%d %H:%M UTC')}`",
    ]
    return "\n".join(lines)


async def broadcast_signal(app: Application, signal, signal_id: int):
    msg = format_signal_message(signal)
    msg += f"\n🆔 Signal ID: `{signal_id}`"
    await _send(app, msg)


async def notify_outcome(app: Application, signal_db: dict):
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0

    if status == "win":
        emoji = "✅"
        label = f"TARGET HIT  +{roi:.1f}%"
    elif status == "loss":
        emoji = "❌"
        label = f"STOP HIT  {roi:.1f}%"
    else:
        emoji = "💤"
        label = "EXPIRED (no hit in 24h)"

    arrow = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"{emoji} *Signal Closed*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow} {direction} — *{symbol}*\n"
        f"Result: `{label}`\n"
        f"🆔 ID: `{signal_db['id']}`"
    )
    await _send(app, msg)


# ─────────────────────────── commands ───────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *MEXC Futures Signal Bot*\n\n"
        "*Performance reports:*\n"
        "/daily — Today's performance\n"
        "/weekly — Last 7 days\n"
        "/monthly — This month\n"
        "/stats — All-time stats\n\n"
        "*Scalping scanner:*\n"
        "/status — Scanner state & last signals\n"
        "/pairs — Live indicators for all pairs\n"
        "/signal\\_count — Signals fired today\n"
        "/pause — Pause signal sending\n"
        "/resume — Resume signal sending\n"
        "/session\\_filter on|off — Toggle session filter\n"
        "/setpair add|remove SYMBOL — Manage pairs\n\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.daily_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.weekly_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.monthly_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.alltime_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ────────────────── scalping signal formatter ────────────────────

_RISK_REMINDER = (
    "\n\n⚠️ _Risk reminder: Always use isolated margin. "
    "Max 2% account per trade. These are signals, not financial advice._"
)


def format_scalping_signal(signal: "ScalpingSignal", signal_count: int) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    tags: list[str] = []
    if signal.fresh_cross:
        tags.append("⚡ Fresh Cross")
    if signal.rsi_divergence:
        tags.append("⚠️ RSI Div")
    tag_str = "  ".join(tags) if tags else "Clean setup"

    lines = [
        f"{arrow} — *{coin}* Scalp  _{signal.strength}_",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    `${signal.entry_price:,.6g}`",
        f"🎯 TP:       `${signal.tp_price:,.6g}`  _(+{signal.tp_roi_pct:.0f}% ROI)_",
        f"🛑 SL:       `${signal.sl_price:,.6g}`  _(-{signal.sl_roi_pct:.0f}% ROI)_",
        f"⚡ Leverage: `{signal.leverage}x`  _(Isolated margin)_",
        "━━━━━━━━━━━━━━━━━━━━",
        f"EMA9: `{signal.ema9:,.6g}`  EMA21: `{signal.ema21:,.6g}`",
        f"RSI(7): `{signal.rsi_val}`  VWAP: `${signal.vwap_val:,.6g}`",
        f"🏷️  {tag_str}",
        f"⏰ `{signal.generated_at.strftime('%H:%M UTC')}`  _{signal.session} session_",
    ]

    msg = "\n".join(lines)
    if signal_count > 0 and signal_count % RISK_REMINDER_EVERY_N == 0:
        msg += _RISK_REMINDER
    return msg


async def broadcast_scalping_signal(
    app: Application,
    signal: "ScalpingSignal",
    signal_count: int,
    signal_id: int,
) -> None:
    msg = format_scalping_signal(signal, signal_count)
    msg += f"\n🆔 Signal ID: `{signal_id}`"
    await _send(app, msg)


# ─────────────── scalping bot commands ──────────────────────────

def _engine(context: ContextTypes.DEFAULT_TYPE):
    """Retrieve the ScalpingEngine stored in bot_data."""
    return context.application.bot_data.get("scalping_engine")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return

    from strategy.filters import is_trading_session, is_funding_window, current_session_name

    now = datetime.now(timezone.utc)
    uptime = str(now - eng.start_time).split(".")[0]
    state  = "⏸ PAUSED" if eng.paused else "▶️ RUNNING"
    filt   = "ON" if eng.session_filter_enabled else "OFF"
    sess   = current_session_name(now)
    in_s   = "✅" if is_trading_session(now) else "❌"
    fund   = "⚠️ YES" if is_funding_window(now) else "no"

    last = eng.get_last_signals()
    sig_lines = [
        f"  • `{sym}`: {info['direction']} @ {info['timestamp'].strftime('%H:%M UTC')}"
        for sym, info in last.items()
    ] or ["  _None yet_"]

    lines = [
        "📡 *Scanner Status*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:            `{state}`",
        f"Session filter:   `{filt}`",
        f"Current session:  `{sess}`",
        f"In trading hours: {in_s}",
        f"Funding window:   `{fund}`",
        f"Active pairs:     `{len(eng.active_pairs)}`",
        f"Signals today:    `{eng.get_signal_count()}`",
        f"Uptime:           `{uptime}`",
        "",
        "*Last signal per pair:*",
        *sig_lines,
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return

    snap = eng.get_indicator_snapshot()
    lines = ["📊 *Active Pairs — Live Indicators*", "━━━━━━━━━━━━━━━━━━━━"]

    for sym in eng.active_pairs:
        label = sym.replace("_", "/")
        if sym in snap:
            d = snap[sym]
            ema_bias = "▲" if d["ema9"] > d["ema21"] else "▼"
            lines.append(
                f"*{label}*  {ema_bias}\n"
                f"  Price `${d['price']:,.6g}` | VWAP `${d['vwap']:,.6g}`\n"
                f"  EMA9 `{d['ema9']:,.6g}` | EMA21 `{d['ema21']:,.6g}`\n"
                f"  RSI(7) `{d['rsi']}` | Vol× `{d['vol_ratio']}`"
            )
        else:
            lines.append(f"*{label}*  — _no data yet_")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_signal_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return

    from database import get_signals_in_range
    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sigs  = get_signals_in_range(start, now)
    wins    = sum(1 for s in sigs if s["status"] == "win")
    losses  = sum(1 for s in sigs if s["status"] == "loss")
    pending = sum(1 for s in sigs if s["status"] == "pending")

    msg = (
        "📈 *Signal Count — Today*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Scalping signals fired: `{eng.get_signal_count()}`\n"
        f"✅ Wins:    `{wins}`\n"
        f"❌ Losses:  `{losses}`\n"
        f"⏳ Pending: `{pending}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return
    eng.paused = True
    await update.message.reply_text(
        "⏸ Signal sending *paused*. Scanner still running.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return
    eng.paused = False
    await update.message.reply_text(
        "▶️ Signal sending *resumed*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_session_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return

    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if eng.session_filter_enabled else "off"
        await update.message.reply_text(
            f"Usage: `/session_filter on|off`\nCurrent: `{current}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    eng.session_filter_enabled = (args[0].lower() == "on")
    state = "enabled" if eng.session_filter_enabled else "disabled"
    await update.message.reply_text(f"🕐 Session filter *{state}*.", parse_mode=ParseMode.MARKDOWN)


async def cmd_setpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    eng = _engine(context)
    if eng is None:
        await update.message.reply_text("Scalping engine not initialised.")
        return

    args = context.args or []
    if len(args) < 2 or args[0].lower() not in ("add", "remove"):
        await update.message.reply_text(
            "Usage:\n`/setpair add SOLUSDT`\n`/setpair remove SOLUSDT`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    action = args[0].lower()
    raw    = args[1].upper()

    # Normalise XYZUSDT → XYZ_USDT
    if raw.endswith("_USDT"):
        symbol = raw
    elif raw.endswith("USDT"):
        symbol = raw[:-4] + "_USDT"
    else:
        symbol = raw

    if action == "add":
        if symbol in eng.active_pairs:
            await update.message.reply_text(
                f"`{symbol}` is already in the scan list.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            eng.active_pairs.append(symbol)
            await update.message.reply_text(
                f"✅ Added `{symbol}` to scan list.", parse_mode=ParseMode.MARKDOWN
            )
    else:
        if symbol not in eng.active_pairs:
            await update.message.reply_text(
                f"`{symbol}` is not in the scan list.", parse_mode=ParseMode.MARKDOWN
            )
        else:
            eng.active_pairs.remove(symbol)
            await update.message.reply_text(
                f"🗑 Removed `{symbol}` from scan list.", parse_mode=ParseMode.MARKDOWN
            )


# ─────────────────── scheduled auto-reports ─────────────────────

async def auto_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.daily_report())


async def auto_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.weekly_report())


async def auto_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.monthly_report())


# ─────────────────────── app builder ────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ── existing commands ─────────────────────────────────────────
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("stats",   cmd_stats))

    # ── scalping scanner commands ─────────────────────────────────
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("pairs",          cmd_pairs))
    app.add_handler(CommandHandler("signal_count",   cmd_signal_count))
    app.add_handler(CommandHandler("pause",          cmd_pause))
    app.add_handler(CommandHandler("resume",         cmd_resume))
    app.add_handler(CommandHandler("session_filter", cmd_session_filter))
    app.add_handler(CommandHandler("setpair",        cmd_setpair))

    return app
