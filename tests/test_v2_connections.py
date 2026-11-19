import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.twitch import TwitchClient, EVENTSUB_URL
from app.health import summarize_health, safe_error, RuntimeMonitor


def packet(kind, payload):
    return json.dumps({"metadata": {"message_type": kind}, "payload": payload})


def welcome(session):
    return packet("session_welcome", {"session": {"id": session, "keepalive_timeout_seconds": 10}})


class MemoryMonitor:
    def __init__(self):
        self.states = {}
        self.error = asyncio.Event()

    def update(self, component, **values):
        self.states.setdefault(component, {}).update(values)
        if values.get("error"):
            self.error.set()


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.closed = False
        self.entered = asyncio.Event()

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *args):
        self.closed = True
        await self.incoming.put(ConnectionError("closed"))


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self):
        self.old, self.new = Socket(), Socket()
        await self.old.incoming.put(welcome("old"))
        self.delivered = []
        self.events = {name: asyncio.Event() for name in ("one", "two", "three", "four")}
        self.monitor = MemoryMonitor()
        self.active = None
        self.scan_started = asyncio.Event()
        async def received(event):
            self.delivered.append(event["message_id"])
            self.events[event["message_id"]].set()
        client = TwitchClient(SimpleNamespace(), SimpleNamespace(), received, monitor=self.monitor)
        client.discover_live = AsyncMock(return_value=[{"user_login": "channel", "user_id": "1"}])
        client.subscribe_chat = AsyncMock()
        async def scanner(session, active, streams):
            self.active = active
            active["1"] = "existing_subscription"
            client._selected = {"1": "channel"}
            client._login_by_id = {"1": "channel"}
            client._publish_subscriptions(active)
            self.scan_started.set()
            await asyncio.Event().wait()
        client._scan_loop = AsyncMock(side_effect=scanner)
        return client

    async def notify(self, socket, name):
        await socket.incoming.put(packet("notification", {"event": {"message_id": name, "broadcaster_user_id": "1"}}))
        await asyncio.wait_for(self.events[name].wait(), 2)

    async def test_handoff_keeps_reading_old_socket_and_preserves_subscriptions(self):
        client = await self.make_client()
        reconnect_url = "wss://eventsub.wss.twitch.tv/ws?reconnect=opaque-value"
        def connect(url, **kwargs):
            return self.old if url == EVENTSUB_URL else self.new
        with patch("app.twitch.websockets.connect", side_effect=connect) as connection:
            task = asyncio.create_task(client._listen_once())
            try:
                await asyncio.wait_for(self.scan_started.wait(), 2)
                await self.notify(self.old, "one")
                await self.old.incoming.put(packet("session_reconnect", {"session": {"reconnect_url": reconnect_url}}))
                await asyncio.wait_for(self.new.entered.wait(), 2)
                self.assertFalse(self.old.closed)
                await self.notify(self.old, "two")
                # A final old event buffered during the new welcome must survive.
                await self.old.incoming.put(packet("notification", {"event": {"message_id": "four", "broadcaster_user_id": "1"}}))
                await self.new.incoming.put(welcome("new"))
                await self.notify(self.new, "three")
                await asyncio.wait_for(self.events["four"].wait(), 2)
                self.assertEqual(set(self.delivered), {"one", "two", "three", "four"})
                self.assertEqual(client._session_id, "new")
                self.assertEqual(self.active, {"1": "existing_subscription"})
                client.subscribe_chat.assert_not_awaited()
                client._scan_loop.assert_awaited_once()
                self.assertEqual(connection.call_args_list[1].args[0], reconnect_url)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(client.active_chat_count, 0)

    async def test_untrusted_reconnect_url_is_rejected_without_closing_old_socket(self):
        client = await self.make_client()
        with patch("app.twitch.websockets.connect", return_value=self.old) as connection:
            task = asyncio.create_task(client._listen_once())
            try:
                await asyncio.wait_for(self.scan_started.wait(), 2)
                await self.old.incoming.put(packet("session_reconnect", {"session": {"reconnect_url": "wss://127.0.0.1/private"}}))
                await asyncio.wait_for(self.monitor.error.wait(), 2)
                self.assertFalse(self.old.closed)
                self.assertEqual(connection.call_count, 1)
                await self.notify(self.old, "one")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_welcome_closes_new_socket_but_keeps_old_reader(self):
        client = await self.make_client()
        await self.new.incoming.put(packet("unexpected", {}))
        def connect(url, **kwargs):
            return self.old if url == EVENTSUB_URL else self.new
        with patch("app.twitch.websockets.connect", side_effect=connect):
            task = asyncio.create_task(client._listen_once())
            try:
                await asyncio.wait_for(self.scan_started.wait(), 2)
                await self.old.incoming.put(packet("session_reconnect", {"session": {"reconnect_url": "wss://eventsub.wss.twitch.tv/ws?retry=1"}}))
                await asyncio.wait_for(self.monitor.error.wait(), 2)
                self.assertTrue(self.new.closed)
                self.assertFalse(self.old.closed)
                await self.notify(self.old, "one")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_unsubscribe_does_not_allow_over_100_actual_connections(self):
        storage = SimpleNamespace(sync_catalog_chats=AsyncMock())
        client = TwitchClient(SimpleNamespace(), storage, None)
        client.delete_subscription = AsyncMock(return_value=False)
        client.subscribe_chat = AsyncMock()
        active = {str(i): f"sub{i}" for i in range(100)}
        await client._sync_discovery("session", active, [{"user_login": "new", "user_id": "1000"}])
        self.assertEqual(len(active), 100)
        client.subscribe_chat.assert_not_awaited()

    async def test_reconciliation_racing_a_handoff_does_not_clear_existing_map(self):
        client = TwitchClient(SimpleNamespace(), None, None)
        client._session_id = "old"
        async def request(*args, **kwargs):
            client._session_id = "new"
            return {"data": []}
        client._request = AsyncMock(side_effect=request)
        active = {"1": "subscription"}
        await client._reconcile_subscriptions(active)
        self.assertEqual(active, {"1": "subscription"})

    async def test_activity_checks_are_independent_from_discovery_and_socket(self):
        storage = SimpleNamespace(channels=AsyncMock(return_value=[("manual", "1", "manual"), ("chat", "2", "catalog")]))
        client = TwitchClient(SimpleNamespace(monitor_refresh_seconds=60), storage, None)
        client._sync_manual_activity = AsyncMock()
        client.discover_live = AsyncMock()
        with patch("app.twitch.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await client._activity_loop()
        client._sync_manual_activity.assert_awaited_once_with([("manual", "1", "manual")])
        client.discover_live.assert_not_awaited()


class HealthTests(unittest.TestCase):
    def rows(self, changes=None):
        values = {
            "heartbeat": {"pid": 7, "version": "2.0.0"},
            "twitch": {"connected": True, "last_packet_at": 995, "keepalive": 10, "active": 100, "unwanted_count": 0},
            "telegram": {"available": True, "last_success_at": 990},
            "scanner": {"selected": 100, "live": 251, "checked_at": 995},
            "delivery": {"phase": "idle"},
        }
        for component, fields in (changes or {}).items():
            values.setdefault(component, {}).update(fields)
        return [(name, json.dumps(data), 1000, "instance") for name, data in values.items()]

    def test_healthy_requires_real_fresh_components(self):
        result = summarize_health(self.rows(), now=1000, pid=7)
        self.assertEqual((result["state"], result["active_chats"]), ("healthy", 100))

    def test_stale_heartbeat_clears_old_connection_count(self):
        result = summarize_health(self.rows(), now=1030, pid=7)
        self.assertEqual((result["state"], result["active_chats"]), ("stale", 0))

    def test_other_process_cannot_reuse_a_previous_process_status(self):
        self.assertFalse(summarize_health(self.rows(), now=1000, pid=8)["fresh"])

    def test_expired_socket_or_telegram_is_not_healthy(self):
        for change in ({"twitch": {"last_packet_at": 900}}, {"telegram": {"last_success_at": 900}}, {"delivery": {"phase": "retrying"}}, {"activity": {"error": "DatabaseError"}}, {"scanner": {"error": "TimeoutError"}}):
            self.assertEqual(summarize_health(self.rows(change), now=1000, pid=7)["state"], "degraded")

    def test_no_live_chats_is_waiting_not_false_error(self):
        rows = self.rows({"twitch": {"connected": False, "active": 0}, "scanner": {"selected": 0, "live": 0}})
        self.assertEqual(summarize_health(rows, now=1000, pid=7)["state"], "waiting")

    def test_stopped_service_is_never_healthy(self):
        self.assertEqual(summarize_health(self.rows(), now=1000, pid=7, service_active=False)["state"], "stopped")

    def test_malformed_heartbeat_is_reported_stale(self):
        rows = self.rows() + [("heartbeat", "not-json", 1000, "instance")]
        # The valid heartbeat is removed to simulate a corrupted DB row.
        rows = [row for row in rows if row[0] != "heartbeat" or row[1] == "not-json"]
        self.assertEqual(summarize_health(rows, now=1000, pid=7)["state"], "stale")

    def test_safe_error_never_exposes_exception_payload(self):
        self.assertEqual(safe_error(RuntimeError("secret-token-value")), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
