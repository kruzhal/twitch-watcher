import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.import_channels import import_catalog, read_catalog
from app.storage import Storage
from app.twitch import TwitchClient


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = Storage(str(self.root / "watcher.sqlite3"))
        await self.storage.init()
        self.client = TwitchClient(
            SimpleNamespace(monitor_max_channels=100, monitor_refresh_seconds=60),
            self.storage, AsyncMock(),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def csv_file(self, rows, header=("rank", "login")):
        path = self.root / "channels.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    async def test_import_normalizes_and_deduplicates_using_best_rank(self):
        path = self.csv_file([(2, "@Example"), (1, "example"), (3, "Other")])
        self.assertEqual(read_catalog(path), [("example", 1), ("other", 3)])
        self.assertEqual(await import_catalog(path, self.storage.path), 2)
        self.assertEqual(await self.storage.catalog_channels(), [("example", 1), ("other", 3)])

    async def test_invalid_or_empty_import_keeps_existing_catalog(self):
        await self.storage.replace_catalog([("existing", 1)])
        for rows in ([(1, "valid"), (2, "invalid login")], [], [(0, "valid")]):
            with self.assertRaises(ValueError):
                await import_catalog(self.csv_file(rows), self.storage.path, replace=True)
            self.assertEqual(await self.storage.catalog_channels(), [("existing", 1)])

    async def test_replacement_requires_flag_and_preserves_user_data(self):
        await self.storage.replace_catalog([("existing", 1)])
        await self.storage.add_channel("activity", "99")
        await self.storage.add_rule("watched_words", "hello")
        await self.storage.add_rule("watched_users", "author")
        await self.storage.save_match("2026-11-10", "activity", "author", "hello", "hello", "msg", notification="queued")
        path = self.csv_file([(1, "new")])
        with self.assertRaises(ValueError):
            await import_catalog(path, self.storage.path)
        await import_catalog(path, self.storage.path, replace=True)
        self.assertEqual(await self.storage.catalog_channels(), [("new", 1)])
        self.assertEqual(await self.storage.channels(), [("activity", "99", "manual")])
        self.assertEqual(await self.storage.rules("watched_words"), ["hello"])
        self.assertEqual(await self.storage.rules("watched_users"), ["author"])
        self.assertEqual(await self.storage.match_count(), 1)
        self.assertEqual((await self.storage.next_notification())[1], "queued")

    async def test_catalog_replacement_is_atomic_on_storage_failure(self):
        await self.storage.replace_catalog([("existing", 1)])
        with self.assertRaises(Exception):
            await self.storage.replace_catalog([("duplicate", 1), ("duplicate", 2)])
        self.assertEqual(await self.storage.catalog_channels(), [("existing", 1)])

    async def test_2250_channels_are_all_checked_in_filtered_batches(self):
        catalog = [(f"channel{i:04}", i + 1) for i in range(2250)]
        await self.storage.replace_catalog(catalog)
        seen = []

        async def request(method, url, *, user, params):
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/streams"))
            self.assertFalse(user)
            self.assertEqual(dict(params)["first"], "100")
            logins = [value for key, value in params if key == "user_login"]
            self.assertGreater(len(logins), 0)
            self.assertLessEqual(len(logins), 100)
            seen.extend(logins)
            # Twitch ordering must not override the owner's CSV priority.
            return {"data": [{"user_login": login, "user_id": login[7:]} for login in reversed(logins)]
                    + [{"user_login": "outside_catalog", "user_id": "outsider"}]}

        self.client._request = AsyncMock(side_effect=request)
        selected = await self.client.discover_live()
        self.assertEqual(seen, [login for login, _ in catalog])
        self.assertEqual(self.client._request.await_count, 23)
        self.assertEqual(self.client.catalog_live_count, 2250)
        self.assertEqual([stream["user_login"] for stream in selected], [login for login, _ in catalog[:100]])

    async def test_offline_channels_do_not_use_slots(self):
        await self.storage.replace_catalog([("offline", 1), ("live", 2)])
        self.client._request = AsyncMock(return_value={"data": [{"user_login": "live", "user_id": "2"}]})
        self.assertEqual([row["user_login"] for row in await self.client.discover_live()], ["live"])

    async def test_empty_catalog_never_falls_back_to_global_streams_or_socket(self):
        self.client._request = AsyncMock()
        self.client._sync_manual_activity = AsyncMock()
        with patch("app.twitch.websockets.connect") as connect, patch("app.twitch.asyncio.sleep", new_callable=AsyncMock):
            await self.client._listen_once()
        self.client._request.assert_not_awaited()
        connect.assert_not_called()

    async def test_stream_status_requests_page_size_100(self):
        self.client._request = AsyncMock(return_value={"data": []})
        await self.client._streams_for_channels([str(i) for i in range(125)])
        self.assertEqual(self.client._request.await_count, 2)
        for call in self.client._request.call_args_list:
            self.assertEqual(dict(call.kwargs["params"])["first"], "100")

    async def test_migration_removes_legacy_auto_rows_but_keeps_manual(self):
        await self.storage.add_channel("old", "1", "auto")
        await self.storage.add_channel("activity", "2", "manual")
        await self.storage.add_channel("upgraded", "3", "auto")
        await self.storage.sync_catalog_chats([("upgraded", "3"), ("activity", "2"), ("new", "4")])
        self.assertEqual(await self.storage.channels(), [
            ("activity", "2", "manual"), ("new", "4", "catalog"), ("upgraded", "3", "catalog"),
        ])

    async def test_failed_discovery_does_not_discard_existing_subscriptions(self):
        self.client.discover_live = AsyncMock(side_effect=RuntimeError("Twitch unavailable"))
        self.client.delete_subscription = AsyncMock()
        active = {"1": "subscription"}
        with self.assertRaises(RuntimeError):
            await self.client._sync_discovery("session", active)
        self.assertEqual(active, {"1": "subscription"})
        self.client.delete_subscription.assert_not_awaited()

    async def test_first_sync_uses_prefetched_streams_without_second_scan(self):
        self.client.discover_live = AsyncMock()
        self.client.subscribe_chat = AsyncMock(return_value="subscription")
        self.client._sync_manual_activity = AsyncMock()
        with patch("app.twitch.asyncio.sleep", new_callable=AsyncMock) as sleep:
            active = {}
            await self.client._sync_discovery("session", active, [{"user_login": "live", "user_id": "2"}])
        self.client.discover_live.assert_not_awaited()
        self.assertEqual(active, {"2": "subscription"})
        self.assertEqual(self.client.active_chat_count, 1)
        sleep.assert_awaited_once_with(0.6)

    async def test_empty_subscription_id_does_not_claim_success(self):
        self.client.subscribe_chat = AsyncMock(return_value=None)
        self.client._sync_manual_activity = AsyncMock()
        with patch("app.twitch.asyncio.sleep", new_callable=AsyncMock):
            active = {}
            await self.client._sync_discovery("session", active, [{"user_login": "live", "user_id": "2"}])
        self.assertEqual(active, {})
        self.assertEqual(self.client.active_chat_count, 0)


if __name__ == "__main__":
    unittest.main()
