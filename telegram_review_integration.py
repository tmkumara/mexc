"""Register private CHoCH review on the existing signal bot application."""

import logging
import os
from pathlib import Path
import sqlite3
import stat

from telegram.ext import CallbackQueryHandler, CommandHandler, filters
from telegram.error import TelegramError


LOGGER = logging.getLogger(__name__)
RELEASE = Path(__file__).resolve().parent
ASSETS = RELEASE / "review_toolkit/strong_choch_results/phase_2_10"
DEFAULT_DB = "/var/lib/mexc-choch-review/review.sqlite3"
COMMANDS = (
    "review", "review_next", "review_previous", "review_progress", "review_skip",
    "review_undo", "review_summary", "review_case", "review_help", "review_reason",
)


def private_database_path(raw):
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("Review database path must be absolute")
    path = path.resolve()
    if path.is_relative_to(RELEASE):
        raise ValueError("Review database must be outside the release checkout")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ValueError("Review database directory must be private (0700)")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError("Existing review database must be a private file (0600)")
        else:
            os.close(fd)
    return path


def attach_review(app):
    allowlist = os.getenv("MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not allowlist or os.getenv("MEXC_REVIEW_ENABLED", "1") == "0":
        return
    try:
        # These runtime modules deliberately do not import the historical
        # strategy package, which would collide with the live strategy.py.
        from review_toolkit.research.strong_choch.review.__main__ import reviewer_ids
        from review_toolkit.research.strong_choch.review.queue import load_queue
        from review_toolkit.research.strong_choch.review.service import Service
        from review_toolkit.research.strong_choch.review.storage import Storage
        from review_toolkit.research.strong_choch.review.telegram_bot import TelegramReview

        allowed = reviewer_ids(allowlist)
        queue = Path(os.getenv("MEXC_REVIEW_QUEUE_PATH") or ASSETS / "review_queue.csv")
        cases = load_queue(queue)
        path = private_database_path(os.getenv("MEXC_REVIEW_STORAGE_PATH") or DEFAULT_DB)
        service = Service(cases, Storage(path), allowed)
        adapter = TelegramReview(service, queue.parent / "charts", queue.parent / "chart_manifest.json",
                                 integrated=True, disabled_marker=path.parent / "disabled")
    except (ImportError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        LOGGER.warning("CHoCH review disabled at startup (%s)", type(exc).__name__)
        return

    async def command(update, context):
        try:
            await adapter.command(update, context)
        except (TelegramError, OSError, sqlite3.Error) as exc:
            LOGGER.warning("CHoCH review command failed (%s)", type(exc).__name__)

    async def callback(update, context):
        try:
            await adapter.callback(update, context)
        except (TelegramError, OSError, sqlite3.Error) as exc:
            LOGGER.warning("CHoCH review callback failed (%s)", type(exc).__name__)

    app.add_handler(CommandHandler(COMMANDS, command, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^choch_review:"))
    app.bot_data["choch_review"] = adapter
    LOGGER.info("Private CHoCH review enabled (%d cases)", len(cases))
