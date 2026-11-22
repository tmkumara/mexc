"""Offline checks for mounting review on the existing Telegram application."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, MessageEntity, Update, User

import bot
from telegram_review_integration import COMMANDS


def complete(coro):
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    finally:
        coro.close()
    raise AssertionError("Mocked Telegram operation suspended")


class IntegratedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS": "101",
            "MEXC_REVIEW_STORAGE_PATH": str(Path(self.temp.name) / "private" / "review.sqlite3"),
            "MEXC_REVIEW_ENABLED": "1",
            "MEXC_REVIEW_QUEUE_PATH": "",
        })
        env.start()
        self.addCleanup(env.stop)
        token = patch.object(bot, "TELEGRAM_TOKEN", "1234:OfflineTestOnly")
        token.start()
        self.addCleanup(token.stop)

    def test_existing_routes_preserved_and_new_routes_share_application(self):
        with patch("telegram.ext.Application.run_polling") as poll:
            app = bot.build_app()
        poll.assert_not_called()
        handlers = app.handlers[0]
        self.assertEqual(
            {name: handler.callback for handler in handlers[:9] for name in handler.commands},
            {name: getattr(bot, f"cmd_{name}") for name in
             ("start", "help", "daily", "weekly", "monthly", "stats", "status", "pause", "resume")},
        )
        self.assertEqual(set(handlers[9].commands), set(COMMANDS))
        app.bot._bot_user = User(1234, "Mock", True, username="mock_bot")
        for chat_type, expected in (("private", True), ("group", False)):
            message = Message(1, None, Chat(1, chat_type), from_user=User(101, "Reader", False),
                              text="/review", entities=[MessageEntity("bot_command", 0, 7)])
            message.set_bot(app.bot)
            self.assertEqual(bool(handlers[9].check_update(Update(1, message=message))), expected)
        callback = handlers[10]
        self.assertTrue(callback.pattern.match("choch_review:vc:BNB_20261030_0000_D:0"))
        self.assertFalse(callback.pattern.match("r:vc:BNB_20261030_0000_D:0"))
        self.assertFalse(callback.pattern.match("other:button"))

    def test_private_allowlist_chart_and_persistent_decision(self):
        app = bot.build_app()
        adapter = app.bot_data["choch_review"]
        sender = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(bot=sender)
        chat = SimpleNamespace(id=101, type="private")
        user = SimpleNamespace(id=101)
        update = SimpleNamespace(effective_user=user, effective_chat=chat,
                                 effective_message=SimpleNamespace(text="/review"))
        complete(adapter.command(update, context))
        sender.send_photo.assert_awaited_once()
        payload = sender.send_photo.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(payload.encode()), 64)
        query = SimpleNamespace(data=payload, id="one", answer=AsyncMock(), edit_message_caption=AsyncMock())
        update.callback_query = query
        user.id = 102
        complete(adapter.callback(update, context))
        query.answer.assert_awaited_once_with("Access denied.", show_alert=True)
        self.assertFalse(adapter.service.storage.history())
        user.id = 101
        complete(adapter.callback(update, context))
        complete(adapter.callback(update, context))
        self.assertEqual(query.edit_message_caption.await_count, 1)
        after = bot.build_app().bot_data["choch_review"].service
        self.assertEqual(len(after.storage.history()), 1)
        self.assertEqual(after.storage.history()[0]["review_label"], "VALID_CHOCH")
        update.effective_message.text = "/review_progress"
        complete(adapter.command(update, context))
        self.assertIn("1/22", sender.send_message.call_args.kwargs["text"])
        self.assertTrue(after.undo(101, 10))
        self.assertIsNone(after.storage.history()[-1]["review_label"])

    def test_missing_queue_and_rollback_do_not_interrupt_live_handlers(self):
        with patch.dict(os.environ, {"MEXC_REVIEW_QUEUE_PATH": str(Path(self.temp.name) / "missing.csv")}):
            app = bot.build_app()
        self.assertNotIn("choch_review", app.bot_data)
        self.assertEqual(len(app.handlers[0]), 9)
        with patch.dict(os.environ, {"MEXC_REVIEW_ENABLED": "0"}):
            self.assertNotIn("choch_review", bot.build_app().bot_data)

    def test_rollback_marker_disables_review_without_rebuilding_application(self):
        app = bot.build_app()
        adapter = app.bot_data["choch_review"]
        update = SimpleNamespace(effective_user=SimpleNamespace(id=101),
                                 effective_chat=SimpleNamespace(type="private"))
        self.assertTrue(adapter.authorize(update))
        adapter.disabled_marker.touch()
        self.assertFalse(adapter.authorize(update))
        self.assertEqual(app.handlers[0][0].callback, bot.cmd_start)

    def test_help_hint_is_private_and_live_notifications_use_existing_sender(self):
        app = bot.build_app()
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=101),
                                 effective_chat=SimpleNamespace(type="group"))
        context = SimpleNamespace(application=app)
        complete(bot.cmd_help(update, context))
        original = message.reply_text.call_args.args[0]
        self.assertNotIn("/review", original)
        update.effective_chat.type = "private"
        complete(bot.cmd_start(update, context))
        self.assertTrue(message.reply_text.call_args.args[0].startswith(original + "\n/review"))
        with patch.object(bot, "format_signal", return_value="unchanged"), patch.object(
            bot, "_send_html", new_callable=AsyncMock
        ) as send:
            complete(bot.broadcast_signal(app, object(), 8))
        send.assert_awaited_once_with(app, "unchanged")
