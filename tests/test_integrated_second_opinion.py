"""Offline checks for the Phase 2.11a second-opinion pass on the existing bot."""

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, MessageEntity, Update, User

import bot
from telegram_review_integration import COMMANDS
from review_toolkit.research.strong_choch.review.telegram_second import COMMANDS as SECOND, PREFIX


def complete(coro):
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    finally:
        coro.close()
    raise AssertionError("Mocked Telegram operation suspended")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IntegratedSecondOpinionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.private = Path(self.temp.name) / "private"
        env = patch.dict(os.environ, {
            "MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS": "101",
            "MEXC_REVIEW_STORAGE_PATH": str(self.private / "review.sqlite3"),
            "MEXC_REVIEW_ENABLED": "1", "MEXC_REVIEW_QUEUE_PATH": "",
            "MEXC_REVIEW_SECOND_ENABLED": "1", "MEXC_REVIEW_SECOND_MANIFEST_PATH": "",
            "MEXC_REVIEW_SECOND_STORAGE_PATH": "",
        })
        env.start()
        self.addCleanup(env.stop)
        token = patch.object(bot, "TELEGRAM_TOKEN", "1234:OfflineTestOnly")
        token.start()
        self.addCleanup(token.stop)

    def build(self):
        with patch("telegram.ext.Application.run_polling") as poll, \
                patch("telegram.ext.Updater.start_polling") as updater:
            app = bot.build_app()
        poll.assert_not_called()
        updater.assert_not_called()
        return app

    def test_handlers_order_namespaces_and_private_filter(self):
        app = self.build()
        handlers = app.handlers[0]
        self.assertEqual(len(handlers), 13)
        self.assertEqual(
            {name: handler.callback for handler in handlers[:9] for name in handler.commands},
            {name: getattr(bot, f"cmd_{name}") for name in
             ("start", "help", "daily", "weekly", "monthly", "stats", "status", "pause", "resume")},
        )
        self.assertEqual(set(handlers[9].commands), set(COMMANDS))
        self.assertEqual(set(handlers[11].commands), set(SECOND))
        self.assertFalse(set(SECOND) & set(COMMANDS))
        first_cb, second_cb = handlers[10], handlers[12]
        payload = PREFIX + "d:ch:ETH_20261008_1200_D:0"
        self.assertTrue(second_cb.pattern.match(payload))
        self.assertFalse(first_cb.pattern.match(payload))
        self.assertFalse(second_cb.pattern.match("choch_review:vc:ETH_20261008_1200_D:0"))
        app.bot._bot_user = User(1234, "Mock", True, username="mock_bot")
        for chat_type, expected in (("private", True), ("group", False), ("supergroup", False)):
            message = Message(1, None, Chat(1, chat_type), from_user=User(101, "Reader", False),
                              text="/review_second", entities=[MessageEntity("bot_command", 0, 14)])
            message.set_bot(app.bot)
            self.assertEqual(bool(handlers[11].check_update(Update(1, message=message))), expected)

    def test_separate_storage_first_pass_untouched_and_restart(self):
        app = self.build()
        first, second = app.bot_data["choch_review"], app.bot_data["choch_review_second"]
        first_path = self.private / "review.sqlite3"
        second_path = self.private / "second_opinion.sqlite3"
        self.assertTrue(second_path.is_file())
        self.assertNotEqual(first_path.resolve(), second_path.resolve())
        before = digest(first_path)
        sender = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(bot=sender)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=101), update_id=3,
                                 effective_chat=SimpleNamespace(id=101, type="private"),
                                 effective_message=SimpleNamespace(text="/review_second"))
        complete(second.command(update, context))
        markup = sender.send_photo.call_args.kwargs["reply_markup"]
        update.callback_query = SimpleNamespace(data=markup.inline_keyboard[0][0].callback_data, id="cb1",
                                                answer=AsyncMock(), edit_message_caption=AsyncMock())
        complete(second.callback(update, context))
        self.assertEqual(digest(first_path), before)
        self.assertEqual(first.service.storage.history(), [])
        rebuilt = self.build().bot_data["choch_review_second"]
        history = rebuilt.service.storage.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["label"], "CHOCH_REVERSAL")
        self.assertEqual(rebuilt.service.storage.cursor(101), history[0]["case_id"])

    def test_original_review_and_help_still_work(self):
        app = self.build()
        first = app.bot_data["choch_review"]
        sender = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=101),
                                 effective_chat=SimpleNamespace(id=101, type="private"),
                                 effective_message=SimpleNamespace(text="/review"))
        complete(first.command(update, SimpleNamespace(bot=sender)))
        payload = sender.send_photo.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertTrue(payload.startswith("choch_review:vc:"))
        update.effective_message.text = "/review_help"
        complete(first.command(update, SimpleNamespace(bot=sender)))
        text = sender.send_message.call_args.kwargs["text"]
        self.assertIn("/review_next", text)
        self.assertIn("/review_second", text)
        self.assertIn("CHoCH — Reversal", text)

    def test_disable_paths_keep_live_and_first_pass_handlers(self):
        with patch.dict(os.environ, {"MEXC_REVIEW_SECOND_ENABLED": "0"}):
            app = self.build()
        self.assertIn("choch_review", app.bot_data)
        self.assertNotIn("choch_review_second", app.bot_data)
        self.assertEqual(len(app.handlers[0]), 11)
        with patch.dict(os.environ, {"MEXC_REVIEW_SECOND_MANIFEST_PATH": str(Path(self.temp.name) / "none.json")}):
            app = self.build()
        self.assertNotIn("choch_review_second", app.bot_data)
        self.assertEqual(len(app.handlers[0]), 11)
        with patch.dict(os.environ, {"MEXC_REVIEW_SECOND_STORAGE_PATH": str(self.private / "review.sqlite3")}):
            app = self.build()
        self.assertNotIn("choch_review_second", app.bot_data)
        with patch.dict(os.environ, {"MEXC_REVIEW_QUEUE_PATH": str(Path(self.temp.name) / "missing.csv")}):
            app = self.build()
        self.assertEqual(len(app.handlers[0]), 9)
        self.assertNotIn("choch_review_second", app.bot_data)

    def test_rollback_marker_disables_both_passes(self):
        app = self.build()
        second = app.bot_data["choch_review_second"]
        update = SimpleNamespace(effective_user=SimpleNamespace(id=101), effective_chat=SimpleNamespace(type="private"))
        self.assertTrue(second.authorize(update))
        (self.private / "disabled").touch()
        self.assertFalse(second.authorize(update))
        self.assertFalse(app.bot_data["choch_review"].authorize(update))


if __name__ == "__main__":
    unittest.main()
