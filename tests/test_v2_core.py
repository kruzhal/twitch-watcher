import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.admin_store import database, import_rows, change_catalog, change_filter, catalog_page, filter_settings
from app.catalog import parse_catalog_csv
from app.storage import Storage
from app.twitch import TwitchClient
from app.bot import setup_bot


class CoreV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.sqlite3")
        self.storage = Storage(self.path)
        await self.storage.init()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def trigger_queue_failure(self):
        with database(self.path) as db:
            db.execute("CREATE TRIGGER reject_queue BEFORE INSERT ON notification_outbox BEGIN SELECT RAISE(ABORT,'simulated failure'); END")

    async def save(self, message_id, *, channel="channel", author="author", text="hello", rule="phrase"):
        await self.storage.save_match("2026-11-10", channel, author, text, rule, message_id, notification="notification")

    async def test_repeat_suppression_keeps_all_unique_history(self):
        await self.save("1")
        await self.save("2", text=" HELLO  ")
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)

    async def test_repeat_zero_disables_cooldown(self):
        await self.storage.set_repeat_cooldown(0)
        await self.save("1")
        await self.save("2")
        self.assertEqual(await self.storage.queue_count(), 2)

    async def test_repeat_identity_includes_channel_author_and_rule(self):
        await self.save("1")
        await self.save("2", channel="other")
        await self.save("3", author="other")
        await self.save("4", rule="other")
        self.assertEqual(await self.storage.queue_count(), 4)

    async def test_repeat_survives_storage_reopen_and_expires(self):
        with patch("app.storage.time.time", return_value=1000):
            await self.save("1")
        reopened = Storage(self.path)
        with patch("app.storage.time.time", return_value=1030):
            await reopened.save_match("now", "channel", "author", "hello", "phrase", "2", notification="n")
        with patch("app.storage.time.time", return_value=1061):
            await self.save("3")
        self.assertEqual(await self.storage.queue_count(), 2)
        self.assertEqual(await self.storage.match_count(), 3)

    async def test_clock_rollback_does_not_suppress_forever(self):
        with patch("app.storage.time.time", return_value=1000):
            await self.save("1")
        with patch("app.storage.time.time", return_value=900):
            await self.save("2")
        self.assertEqual(await self.storage.queue_count(), 2)

    async def test_concurrent_repeat_is_serialized(self):
        await asyncio.gather(self.save("1"), self.save("2"))
        self.assertEqual(await self.storage.match_count(), 2)
        self.assertEqual(await self.storage.queue_count(), 1)

    async def test_match_queue_failure_rolls_back_both_match_and_fingerprint(self):
        self.trigger_queue_failure()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.save("1")
        self.assertEqual(await self.storage.match_count(), 0)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_fingerprints").fetchone()[0], 0)

    async def test_activity_failure_keeps_previous_state_and_retries_once(self):
        await self.storage.record_channel_activity("channel", "1", False, "title", "game", "before")
        client = TwitchClient(SimpleNamespace(), self.storage, None)
        client._streams_for_channels = AsyncMock(return_value={"1": {"title": "title", "game_name": "game"}})
        client._channel_info_for_channels = AsyncMock(return_value={})
        channels = [("channel", "1", "manual")]
        self.trigger_queue_failure()
        with self.assertRaises(sqlite3.IntegrityError):
            await client._sync_manual_activity(channels)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT is_live FROM channel_activity").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_queue")
        await client._sync_manual_activity(channels)
        await client._sync_manual_activity(channels)
        self.assertEqual(await self.storage.queue_count(), 1)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT is_live FROM channel_activity").fetchone()[0], 1)

    async def test_activity_formatter_failure_does_not_advance_state(self):
        await self.storage.record_channel_activity("channel", "1", False, "title", "game", "before")
        def fail(_):
            raise RuntimeError("formatter failed")
        with self.assertRaises(RuntimeError):
            await self.storage.record_channel_activity("channel", "1", True, "new", "game", "after", notification_factory=fail)
        with database(self.path) as db:
            self.assertEqual(tuple(db.execute("SELECT is_live,title FROM channel_activity").fetchone()), (0, "title"))

    async def test_disabled_channels_are_not_queried_or_selected(self):
        await self.storage.replace_catalog([("allowed", 2), ("disabled", 1)])
        with database(self.path) as db:
            change_catalog(db, {"action": "update", "login": "disabled", "priority": 1, "enabled": False})
        client = TwitchClient(SimpleNamespace(monitor_max_channels=100), self.storage, None)
        client._request = AsyncMock(return_value={"data": [{"user_login": "allowed", "user_id": "1"}, {"user_login": "disabled", "user_id": "2"}]})
        self.assertEqual([row["user_login"] for row in await client.discover_live()], ["allowed"])
        self.assertEqual(client._request.call_args.kwargs["params"], [("user_login", "allowed"), ("first", "100")])

    async def test_import_preserves_disabled_choices_and_user_data(self):
        await self.storage.replace_catalog([("old", 1), ("kept", 2)])
        await self.storage.add_channel("manual", "5")
        await self.save("1")
        with database(self.path) as db:
            change_catalog(db, {"action": "update", "login": "kept", "priority": 2, "enabled": False})
        with database(self.path) as db:
            import_rows(db, [("kept", 1), ("new", 2)], "replace")
        with database(self.path) as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT login,priority,enabled FROM channel_catalog ORDER BY login")], [("kept", 1, 0), ("new", 2, 1)])
        self.assertEqual(await self.storage.match_count(), 1)
        self.assertEqual(await self.storage.queue_count(), 1)
        self.assertEqual(await self.storage.channels(), [("manual", "5", "manual")])

    async def test_cli_replacement_also_preserves_disabled_choices(self):
        await self.storage.replace_catalog([("kept", 1)])
        with database(self.path) as db:
            change_catalog(db, {"action": "update", "login": "kept", "priority": 1, "enabled": False})
        await self.storage.replace_catalog([("kept", 3), ("new", 2)])
        self.assertEqual(await self.storage.catalog_channels(), [("new", 2)])

    async def test_bad_csv_never_reaches_import(self):
        await self.storage.replace_catalog([("kept", 1)])
        for text in ("rank,login\n0,bad", "login\nwrong login", "wrong\nvalue", "login\n"):
            with self.assertRaises(ValueError):
                parse_catalog_csv(text)
        self.assertEqual(await self.storage.catalog_channels(), [("kept", 1)])

    async def test_catalog_pagination_search_and_validation(self):
        await self.storage.replace_catalog([(f"user{i:03}", i + 1) for i in range(125)])
        with database(self.path) as db:
            data = catalog_page(db, page=99)
            self.assertEqual((data["page"], data["total"], len(data["items"])), (2, 125, 25))
        with database(self.path) as db:
            self.assertEqual(catalog_page(db, search="USER124")["items"][0]["login"], "user124")
        for payload in ({"action": "update", "login": "user000", "priority": True, "enabled": True}, {"action": "update", "login": "user000", "priority": 1, "enabled": 0}):
            with database(self.path) as db, self.assertRaises(ValueError):
                change_catalog(db, payload)

    async def test_exclusions_apply_to_authors_and_words_with_boundaries(self):
        await self.storage.add_rule("watched_users", "author")
        await self.storage.add_rule("watched_words", "hello")
        await self.storage.add_rule("excluded_words", "spam")
        bot, _, notify, _ = setup_bot(SimpleNamespace(telegram_proxy="", telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", telegram_admin_id=1), self.storage, None)
        try:
            await notify({"broadcaster_user_login": "channel", "chatter_user_login": "author", "message_id": "1", "message": {"text": "hello SPAM!"}})
            self.assertEqual(await self.storage.match_count(), 0)
            await notify({"broadcaster_user_login": "channel", "chatter_user_login": "other", "message_id": "2", "message": {"text": "hello spammer"}})
            self.assertEqual(await self.storage.match_count(), 1)
        finally:
            await bot.session.close()

    async def test_filter_settings_strict_ranges(self):
        for value in (-1, 86401, True, "60", 1.5):
            with self.assertRaises(ValueError):
                await self.storage.set_repeat_cooldown(value)
        with database(self.path) as db:
            change_filter(db, {"action": "add", "phrase": "@SPAM"})
        with database(self.path) as db:
            settings = filter_settings(db)
            self.assertEqual(settings, {"excluded_words": ["@spam"], "repeat_cooldown_seconds": 60})


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_migration_is_additive_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "legacy.sqlite3")
            with database(path) as db:
                db.executescript("""
                    CREATE TABLE channels(login TEXT PRIMARY KEY,broadcaster_id TEXT NOT NULL);
                    INSERT INTO channels VALUES ('manual','1');
                    CREATE TABLE channel_catalog(login TEXT PRIMARY KEY,priority INTEGER NOT NULL);
                    INSERT INTO channel_catalog VALUES ('old',10);
                    CREATE TABLE watched_words(phrase TEXT PRIMARY KEY);
                    INSERT INTO watched_words VALUES ('hello');
                """)
            storage = Storage(path)
            await storage.init()
            await storage.init()
            with database(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(await storage.channels(), [("manual", "1", "manual")])
            self.assertEqual(await storage.catalog_channels(), [("old", 10)])
            self.assertEqual(await storage.rules("watched_words"), ["hello"])
            self.assertEqual(await storage.rules("excluded_words"), [])
            self.assertEqual(await storage.repeat_cooldown(), 60)


if __name__ == "__main__":
    unittest.main()
