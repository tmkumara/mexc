"""
Report formatter for daily / weekly / monthly / all-time stats.
All signals are auto-tracked (no placed filter needed).
"""

from datetime import datetime, timezone, timedelta
from database import get_signals_in_range, get_all_signals


def _stats(signals: list[dict]) -> dict:
    total   = len(signals)
    wins    = [s for s in signals if s["status"] == "win"]
    losses  = [s for s in signals if s["status"] == "loss"]
    pending = [s for s in signals if s["status"] == "pending"]
    expired = [s for s in signals if s["status"] == "expired"]

    win_count  = len(wins)
    loss_count = len(losses)
    closed     = win_count + loss_count
    win_rate   = (win_count / closed * 100) if closed else 0

    net_roi = sum(s["pnl_roi"] or 0 for s in signals if s["status"] in ("win", "loss"))

    best  = max((s["pnl_roi"] or 0 for s in wins),   default=0)
    worst = min((s["pnl_roi"] or 0 for s in losses),  default=0)

    longs  = [s for s in signals if s["direction"] == "LONG"]
    shorts = [s for s in signals if s["direction"] == "SHORT"]

    return {
        "total": total,
        "wins": win_count, "losses": loss_count,
        "pending": len(pending), "expired": len(expired),
        "win_rate": win_rate, "net_roi": net_roi,
        "best": best, "worst": worst,
        "longs": len(longs), "shorts": len(shorts),
    }


def _bar(win_rate: float, width: int = 10) -> str:
    filled = round(win_rate / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _format_report(title: str, signals: list[dict]) -> str:
    s = _stats(signals)

    if s["total"] == 0:
        return f"📊 *{title}*\n\nNo signals recorded yet."

    sign  = "+" if s["net_roi"] >= 0 else ""
    emoji = "🟢" if s["net_roi"] >= 0 else "🔴"

    lines = [
        f"📊 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📡 Total signals:  `{s['total']}`",
        f"✅ Wins:           `{s['wins']}`",
        f"❌ Losses:         `{s['losses']}`",
        f"⏳ Pending:        `{s['pending']}`",
        f"💤 Expired:        `{s['expired']}`",
        "",
        f"🎯 Win rate:  `{s['win_rate']:.1f}%`  {_bar(s['win_rate'])}",
        f"{emoji} Net ROI:   `{sign}{s['net_roi']:.1f}%`",
        "",
        f"📈 Longs:   `{s['longs']}`",
        f"📉 Shorts:  `{s['shorts']}`",
        "",
        f"🔥 Best signal:   `+{s['best']:.1f}%`",
        f"💀 Worst signal:  `{s['worst']:.1f}%`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
    ]
    return "\n".join(lines)


def daily_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    sigs  = get_signals_in_range(start, end)
    return _format_report(f"Daily Report — {now.strftime('%Y-%m-%d')}", sigs)


def weekly_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    sigs  = get_signals_in_range(start, now)
    week_label = f"{start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    return _format_report(f"Weekly Report — {week_label}", sigs)


def monthly_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sigs  = get_signals_in_range(start, now)
    return _format_report(f"Monthly Report — {now.strftime('%B %Y')}", sigs)


def alltime_report() -> str:
    sigs = get_all_signals()
    return _format_report("All-Time Stats", sigs)
