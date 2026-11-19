import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from app.storage import Storage
from app.delivery import deliver_notifications
from app.twitch import TwitchClient
from app.bot import phrase_matches

class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite')
        self.storage = Storage(self.path)
        await self.storage.init()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_durable_match_and_deduplication(self):
        args = ('2026-11-03T00:00:00+00:00', 'channel', 'author', 'ку', 'фраза «ку»', 'event-1')
        await self.storage.save_match(*args, notification='hello')
        await self.storage.save_match(*args, notification='hello')
        reopened = Storage(self.path)
        self.assertEqual(await reopened.match_count(), 1)
        row = await reopened.next_notification()
        self.assertEqual(row[1], 'hello')
        await reopened.acknowledge_notification(row[0])
        self.assertIsNone(await reopened.next_notification())

    async def test_rule_cache_updates_and_word_boundaries(self):
        self.assertEqual(await self.storage.rules('watched_words'), [])
        await self.storage.add_rule('watched_words', 'ку')
        self.assertEqual(await self.storage.rules('watched_words'), ['ку'])
        await self.storage.del_rule('watched_words', 'ку')
        self.assertEqual(await self.storage.rules('watched_words'), [])
        self.assertTrue(phrase_matches('Привет, ку!', 'ку'))
        self.assertFalse(phrase_matches('шапку', 'ку'))

    async def test_delivery_recovers_and_retains_queue(self):
        await self.storage.enqueue_notification('hello')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[TelegramNetworkError(SendMessage(chat_id=1,text='x'), 'offline'), True]))
        real_sleep = asyncio.sleep
        acknowledged = asyncio.Event()
        async def short_sleep(delay):
            if delay == 0.15:
                acknowledged.set()
                await asyncio.Event().wait()
            await real_sleep(0.01)
        with patch('app.delivery.asyncio.sleep', side_effect=short_sleep):
            task = asyncio.create_task(deliver_notifications(bot, self.storage, 1))
            try:
                async def drained():
                    while await self.storage.next_notification() is not None:
                        await real_sleep(0.01)
                await asyncio.wait_for(drained(), 5)
                await asyncio.wait_for(acknowledged.wait(), 5)
                self.assertEqual(bot.send_message.await_count, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_batch_refresh_preserves_manual_channels(self):
        await self.storage.add_channel('mine', '1', 'manual')
        await self.storage.add_channel('old', '2', 'auto')
        await self.storage.sync_catalog_chats([('mine', '1'), ('new', '3')])
        self.assertEqual(await self.storage.channels(), [('mine', '1', 'manual'), ('new', '3', 'catalog')])
        await self.storage.sync_catalog_chats([])
        self.assertEqual(await self.storage.channels(), [('mine', '1', 'manual')])

    async def test_author_watch_is_independent_from_phrases(self):
        from app.bot import setup_bot
        await self.storage.add_rule('watched_users', 'enzzai')
        await self.storage.add_rule('watched_words', 'ананас')
        config = SimpleNamespace(telegram_proxy='', telegram_bot_token='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', telegram_admin_id=1)
        bot, _, notify, _ = setup_bot(config, self.storage, None)
        try:
            await notify({'broadcaster_user_login':'other', 'chatter_user_login':'enzzai', 'message_id':'a', 'message':{'text':'привет'}})
            self.assertEqual(await self.storage.match_count(), 1)
            await notify({'broadcaster_user_login':'enzzai', 'chatter_user_login':'enzzai', 'message_id':'b', 'message':{'text':'привет'}})
            self.assertEqual(await self.storage.match_count(), 1)
            await notify({'broadcaster_user_login':'other', 'chatter_user_login':'someone', 'message_id':'c', 'message':{'text':'ананас!'}})
            self.assertEqual(await self.storage.match_count(), 2)
        finally:
            await bot.session.close()

    async def test_activity_channels_do_not_reserve_chat_slots(self):
        storage = SimpleNamespace(channels=AsyncMock(return_value=[('activity_only','1','manual')]), sync_catalog_chats=AsyncMock())
        config = SimpleNamespace(monitor_max_channels=100)
        client = TwitchClient(config, storage, None)
        client.discover_live = AsyncMock(return_value=[{'user_id':'2','user_login':'automatic'}])
        client.subscribe_chat = AsyncMock(return_value='subscription')
        client._sync_manual_activity = AsyncMock()
        await client._sync_discovery('session', {})
        client.discover_live.assert_awaited_once_with()
        client.subscribe_chat.assert_awaited_once_with('session', '2')
        # Activity checks now have an independent loop; chat subscription scans
        # must neither reserve those slots nor block activity notifications.
        client._sync_manual_activity.assert_not_awaited()

    async def test_slow_scan_does_not_block_websocket(self):
        packets = asyncio.Queue()
        await packets.put(json.dumps({'metadata': {'message_type': 'session_welcome'}, 'payload': {'session': {'id': 'session', 'keepalive_timeout_seconds': 10}}}))
        delivered = asyncio.Event()
        scanner_started = asyncio.Event()
        async def scan(*args):
            scanner_started.set()
            await asyncio.Event().wait()
        async def on_message(event):
            delivered.set()
        ws = SimpleNamespace(recv=packets.get)
        class Connection:
            async def __aenter__(self): return ws
            async def __aexit__(self, *args): pass
        client = TwitchClient(SimpleNamespace(), self.storage, on_message)
        client.discover_live = AsyncMock(return_value=[{'user_id':'2','user_login':'catalog_channel'}])
        client._scan_loop = scan
        with patch('app.twitch.websockets.connect', return_value=Connection()):
            task = asyncio.create_task(client._listen_once())
            try:
                await asyncio.wait_for(scanner_started.wait(), 1)
                await packets.put(json.dumps({'metadata': {'message_type': 'notification'}, 'payload': {'event': {'message': {'text': 'hello'}}}}))
                await asyncio.wait_for(delivered.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

if __name__ == '__main__':
    unittest.main()
