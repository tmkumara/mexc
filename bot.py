"""
Telegram bot: handles commands and broadcasts Breakout Retest EMA/VWAP signals.

Important:
    Signal messages use HTML parse mode, not Markdown.
    This avoids Telegram parse errors caused by symbols containing underscores.
"""

import logging
from datetime import datetime
from html import escape

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import database as db
import reports
from config import (
    TELEGRAM_TOKEN,
    TELEGRAM_CHANNEL_ID,
    LKT,
)

logger = logging.getLogger(__name__)

paused: bool = False


# ── send helpers ──────────────────────────────────────────────────

async def _send_html(app: Application, text: str, chat_id: str = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_markdown(app: Application, text: str, chat_id: str = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


def _code(value) -> str:
    return f"<code>{escape(str(value))}</code>"


def _bold(value) -> str:
    return f"<b>{escape(str(value))}</b>"


def _italic(value) -> str:
    return f"<i>{escape(str(value))}</i>"


# ── signal formatting ─────────────────────────────────────────────

def format_signal(signal, signal_id: int) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin = signal.symbol.replace("_", "/")
    stars = "⭐⭐⭐" if signal.score >= 85 else "⭐⭐" if signal.score >= 72 else "⭐"

    return "\n".join([
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"🎯 TP:       {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% ROI')}",
        f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% ROI')}",
        f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}",
        f"📊 {escape(signal.timeframe_summary)}",
        f"🏅 Score:    {_code(f'{signal.score}/100')}  {stars}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ {_code(signal.generated_at.astimezone(LKT).strftime('%Y-%m-%d %H:%M LKT'))}",
        f"🆔 Signal ID: {_code(signal_id)}",
        _italic("⚠️ Not financial advice. Use risk management."),
    ])


async def broadcast_signal(app: Application, signal, signal_id: int) -> None:
    msg = format_signal(signal, signal_id)
    await _send_html(app, msg)


async def notify_outcome(app: Application, signal_db: dict) -> None:
    direction = signal_db["direction"]
    symbol = signal_db["symbol"].replace("_", "/")
    status = signal_db["status"]
    roi = signal_db.get("pnl_roi") or 0.0

    if status == "win":
        emoji = "✅"
        label = f"TARGET HIT +{roi:.1f}%"
    elif status == "loss":
        emoji = "❌"
        label = f"STOP HIT {roi:.1f}%"
    else:
        emoji = "💤"
        label = "EXPIRED"

    arrow = "🟢" if direction == "LONG" else "🔴"

    msg = "\n".join([
        f"{emoji} {_bold('Signal Closed')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Result: {_code(label)}",
        f"🆔 ID: {_code(signal_db['id'])}",
    ])

    await _send_html(app, msg)


# ── commands ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>MEXC Futures Signal Bot</b>\n\n"
        "<b>Performance:</b>\n"
        "/daily — Today's report\n"
        "/weekly — Last 7 days\n"
        "/monthly — This month\n"
        "/stats — All-time stats\n\n"
        "<b>Scanner:</b>\n"
        "/status — Scanner state\n"
        "/pause — Pause signals\n"
        "/resume — Resume signals\n\n"
        "/help — This message"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.daily_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.weekly_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.monthly_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.alltime_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import coin_scanner

    from config import (
        STRATEGY_NAME,
        ENTRY_TF,
        ENTRY_KLINE_COUNT,
        BREAKOUT_LOOKBACK,
        RETEST_MAX_CANDLES,
        EMA_PERIOD,
        VWAP_LOOKBACK_BARS,
        ATR_PERIOD,
        TARGET_RR,
        MIN_RR,
        MAX_RR,
        LEVERAGE,
        SIGNAL_COOLDOWN_MINUTES,
        SIGNALS_PER_SCAN,
        SETUPS_PER_SCAN,
        MAX_CONCURRENT_SIGNALS,
        SCAN_WORKERS,
        SETUP_SCAN_CRON_MINUTES,
        SETUP_MONITOR_MINUTES,
        MIN_SIGNAL_SCORE,
        ENABLE_SMART_COIN_RANKING,
        ENABLE_WEBSOCKET,
        CANDLE_CACHE_LIMIT,
        COIN_RANK_TIMEFRAME,
        COIN_RANK_KLINE_COUNT,
        COIN_RANK_WORKERS,
        COIN_RANK_MIN_RANGE_PCT,
        COIN_RANK_MAX_RANGE_PCT,
        COIN_RANK_MAX_ABS_MOVE_PCT,
    )

    state = "⏸ PAUSED" if paused else "▶️ RUNNING"
    coins = coin_scanner.get_cached_coins()
    scores = coin_scanner.get_cached_coin_scores()
    active = db.count_active_signals()
    waiting = db.count_waiting_setups()

    pairs_str = "  ".join(s.replace("_USDT", "") for s in coins[:20])

    if scores:
        top_ranked = "  ".join(
            f"{row.get('symbol', '').replace('_USDT', '')}:{row.get('score', 0)}"
            for row in scores[:8]
        )
    else:
        top_ranked = "not ranked yet"

    last_refresh = coin_scanner.get_last_refresh_at()
    refresh_text = (
        last_refresh.astimezone(LKT).strftime("%Y-%m-%d %H:%M LKT")
        if last_refresh
        else "not refreshed yet"
    )

    ranking_state = "ON" if ENABLE_SMART_COIN_RANKING else "OFF"
    ws_state = "ON" if ENABLE_WEBSOCKET else "OFF"

    msg = "\n".join([
        "📡 <b>Scanner Status</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:      {_code(state)}",
        f"Strategy:   {_code(STRATEGY_NAME)}",
        f"Entry TF:   {_code(ENTRY_TF)}",
        f"Candles:    {_code(ENTRY_KLINE_COUNT)}",
        f"Setup scan: {_code(SETUP_SCAN_CRON_MINUTES)}",
        f"Monitor:    {_code(f'every {SETUP_MONITOR_MINUTES} min')}",
        "",
        f"Breakout:   {_code(f'{BREAKOUT_LOOKBACK}-candle high/low')}",
        f"Retest:     {_code(f'within {RETEST_MAX_CANDLES} candles')}",
        f"Trend:      {_code(f'EMA{EMA_PERIOD} + VWAP{VWAP_LOOKBACK_BARS}')}",
        f"ATR:        {_code(f'ATR{ATR_PERIOD}')}",
        f"RR:         {_code(f'{MIN_RR:g} min / {TARGET_RR:g} target / {MAX_RR:g} max')}",
        f"Leverage:   {_code(f'{LEVERAGE}x')}",
        f"Min score:  {_code(MIN_SIGNAL_SCORE)}",
        "",
        f"WebSocket:  {_code(ws_state)}",
        f"Cache:      {_code(f'{CANDLE_CACHE_LIMIT} candles')}",
        "",
        f"Coin rank:  {_code(ranking_state)}",
        f"Rank TF:    {_code(f'{COIN_RANK_KLINE_COUNT} x {COIN_RANK_TIMEFRAME}')}",
        f"Rank range: {_code(f'{COIN_RANK_MIN_RANGE_PCT:g}%–{COIN_RANK_MAX_RANGE_PCT:g}%')}",
        f"Max move:   {_code(f'{COIN_RANK_MAX_ABS_MOVE_PCT:g}% lookback')}",
        f"Rank workers: {_code(COIN_RANK_WORKERS)}",
        f"Refreshed:  {_code(refresh_text)}",
        f"Top ranked: {_code(top_ranked)}",
        "",
        f"Workers:    {_code(SCAN_WORKERS)}",
        f"Setups/scan:{_code(SETUPS_PER_SCAN)}",
        f"Signals/scan:{_code(SIGNALS_PER_SCAN)}",
        f"Cooldown:   {_code(f'{SIGNAL_COOLDOWN_MINUTES} min per coin')}",
        f"Waiting:    {_code(f'{waiting} retests')}",
        f"Active:     {_code(f'{active}/{MAX_CONCURRENT_SIGNALS} signals')}",
        f"Pool ({len(coins)}): {_code(pairs_str)}",
        f"Time (LKT): {_code(datetime.now(LKT).strftime('%H:%M'))}",
    ])

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text("⏸ Signal sending <b>paused</b>.", parse_mode=ParseMode.HTML)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text("▶️ Signal sending <b>resumed</b>.", parse_mode=ParseMode.HTML)


# ── scheduled report helpers ──────────────────────────────────────

async def auto_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.daily_report())


async def auto_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.weekly_report())


async def auto_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.monthly_report())


# ── app builder ───────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    return app