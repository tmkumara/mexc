from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Ns
import unittest
from unittest.mock import AsyncMock, patch

import database
import nwe_order_watch as watch


class OrderWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory=TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db=patch.object(database,"DB_PATH",str(Path(directory.name)/"signals.sqlite"))
        db.start()
        self.addCleanup(db.stop)
        options=patch.multiple(watch,DRY_RUN=False,TELEGRAM_CHANNEL_ID="-100123")
        options.start()
        self.addCleanup(options.stop)
        watch.init_db()
        self.alert=Ns(symbol="ADA_USDT",direction="LONG",entry=100.,signal_at=datetime.now(timezone.utc))
        self.order=watch.register(self.alert,20)

    async def test_marks_only_for_admin_in_channel_and_starts_once(self):
        query=Ns(message=Ns(chat=Ns(id=-100123,username=None),message_id=7),from_user=Ns(id=5),
                 data=f"nwe_order:mark:{self.order}",answer=AsyncMock(),edit_message_reply_markup=AsyncMock())
        bot=Ns(get_chat_member=AsyncMock(return_value=Ns(status="member")))
        await watch.callback(Ns(callback_query=query),Ns(bot=bot))
        self.assertEqual(watch.active_orders(),[])
        bot.get_chat_member.return_value=Ns(status="administrator")
        for _ in range(2):
            await watch.callback(Ns(callback_query=query),Ns(bot=bot))
        self.assertEqual(len(watch.active_orders()),1)
        self.assertEqual(query.edit_message_reply_markup.await_count,1)
        self.assertEqual(watch.register(self.alert,20),self.order)

    async def test_trigger_is_retried_and_only_sent_once(self):
        self.assertTrue(watch.update_watch(self.order,"mark",-100123,7,5))
        bot=Ns(send_message=AsyncMock())
        app=Ns(bot=bot)
        with patch.object(watch,"get_tickers",return_value={"ADA_USDT":{"lastPrice":"100.99"}}):
            await watch.watch(app)
        bot.send_message.assert_not_called()
        with patch.object(watch,"get_tickers",return_value={"ADA_USDT":{"lastPrice":"101"}}):
            bot.send_message.side_effect=RuntimeError("transient")
            with self.assertLogs(watch.log,level="ERROR"):
                await watch.watch(app)
        self.assertEqual(watch.active_orders()[0]["status"],"triggered")
        bot.send_message.side_effect=None
        await watch.watch(app)
        await watch.watch(app)
        self.assertEqual(bot.send_message.await_count,2)
        self.assertEqual(watch.active_orders(),[])
        sent=bot.send_message.await_args.kwargs
        self.assertIn("+20.0%",sent["text"])
        self.assertEqual(sent["reply_to_message_id"],7)

    async def test_short_threshold_and_cancel(self):
        self.assertTrue(watch.crosses(dict(direction="SHORT",entry=100.,leverage=20),99))
        self.assertFalse(watch.crosses(dict(direction="SHORT",entry=100.,leverage=20),99.01))
        self.assertTrue(watch.update_watch(self.order,"mark",-100123,7,5))
        self.assertFalse(watch.update_watch(self.order,"stop",-100123,8,5))
        self.assertTrue(watch.update_watch(self.order,"stop",-100123,7,5))
        self.assertFalse(watch.update_watch(self.order,"mark",-100123,7,5))
        self.assertEqual(watch.active_orders(),[])


if __name__=="__main__":
    unittest.main()
