import asyncio
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp
import websockets

from .health import NullMonitor, safe_error
from .notifications import activity_changes, format_activity

log = logging.getLogger(__name__)
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX = "https://api.twitch.tv/helix"
OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"


class TwitchHTTPError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Twitch HTTP {status_code}")


class TwitchClient:
    def __init__(self, config, storage, on_message, on_activity=None, monitor=None):
        self.config = config
        self.storage = storage
        self.on_message = on_message
        self.on_activity = on_activity
        self.monitor = monitor or NullMonitor()
        self._session_id = None
        self._selected = {}
        self._login_by_id = {}
        self.session = None
        self._refresh_lock = asyncio.Lock()
        self._reader_id = None
        self._app_token = None
        self._app_token_expires_at = 0
        self.catalog_live_count = 0
        self.active_chat_count = 0

    def user_headers(self):
        return {
            "Client-ID": self.config.twitch_client_id,
            "Authorization": f"Bearer {self.config.twitch_user_access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_user_token(self):
        data = {
            "client_id": self.config.twitch_client_id,
            "client_secret": self.config.twitch_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.config.twitch_refresh_token,
        }
        async with self.session.post(OAUTH_TOKEN_URL, data=data) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                raise TwitchHTTPError(response.status)

        self.config.twitch_user_access_token = body["access_token"]
        self.config.twitch_refresh_token = body.get("refresh_token", self.config.twitch_refresh_token)
        self._reader_id = None
        self._save_rotated_tokens()
        log.info("Twitch user access token refreshed and saved")

    def _save_rotated_tokens(self):
        from .tokens import save_tokens
        path = getattr(self.config, "twitch_token_path", "") or str(
            Path(self.config.database_path).parent / "twitch-tokens.json"
        )
        save_tokens(path, self.config.twitch_user_access_token, self.config.twitch_refresh_token)

    async def _request(self, method, url, *, user=True, **kwargs):
        if user:
            headers = self.user_headers()
        else:
            await self._ensure_app_token()
            headers = {
                "Client-ID": self.config.twitch_client_id,
                "Authorization": f"Bearer {self._app_token}",
            }

        async with self.session.request(method, url, headers=headers, **kwargs) as response:
            body = await response.json(content_type=None)
            if response.status == 401 and user:
                async with self._refresh_lock:
                    if headers["Authorization"] == self.user_headers()["Authorization"]:
                        await self._refresh_user_token()
                async with self.session.request(method, url, headers=self.user_headers(), **kwargs) as retry:
                    retry_body = await retry.json(content_type=None)
                    if retry.status >= 300:
                        raise TwitchHTTPError(retry.status)
                    return retry_body
            if response.status >= 300:
                raise TwitchHTTPError(response.status)
            return body

    async def _ensure_app_token(self):
        loop = asyncio.get_running_loop()
        if self._app_token and loop.time() < self._app_token_expires_at:
            return
        data = {
            "client_id": self.config.twitch_client_id,
            "client_secret": self.config.twitch_client_secret,
            "grant_type": "client_credentials",
        }
        async with self.session.post(OAUTH_TOKEN_URL, data=data) as response:
            body = await response.json(content_type=None)
            if response.status != 200:
                raise TwitchHTTPError(response.status)
        self._app_token = body["access_token"]
        self._app_token_expires_at = loop.time() + max(60, int(body.get("expires_in", 3600)) - 60)

    async def resolve_user(self, login):
        data = await self._request("GET", f"{HELIX}/users", params={"login": login})
        if not data.get("data"):
            raise ValueError(f"Twitch не нашёл канал '{login}'.")
        return data["data"][0]

    async def reader_user_id(self):
        if self._reader_id is None:
            data = await self._request("GET", f"{HELIX}/users")
            if not data.get("data"):
                raise RuntimeError("Не удалось определить аккаунт Twitch токена.")
            self._reader_id = str(data["data"][0]["id"])
        return self._reader_id

    async def discover_live(self, capacity=None):
        # Never use unfiltered Get Streams: only channels explicitly imported into SQLite.
        catalog = await self.storage.catalog_channels()
        capacity = self.config.monitor_max_channels if capacity is None else max(0, min(capacity, 100))
        live = {}
        for start in range(0, len(catalog), 100):
            batch = catalog[start:start + 100]
            allowed = {login for login, _ in batch}
            params = [("user_login", login) for login, _ in batch] + [("first", "100")]
            data = await self._request("GET", f"{HELIX}/streams", user=False, params=params)
            for stream in data.get("data", []):
                login = stream.get("user_login", "").casefold()
                if login in allowed and stream.get("user_id"):
                    live[login] = stream
        self.catalog_live_count = len(live)
        selected = [live[login] for login, _ in catalog if login in live][:capacity]
        self.monitor.update("scanner", enabled=len(catalog), live=len(live), selected=len(selected), checked_at=time.time(), error=None)
        if len(live) > capacity:
            log.warning("%s catalog channels are live; reading the first %s by CSV priority", len(live), capacity)
        log.info("Catalog scan: %s total, %s live, %s selected chats", len(catalog), len(live), len(selected))
        return selected

    async def _streams_for_channels(self, broadcaster_ids):
        found = {}
        ids = [str(item) for item in broadcaster_ids]
        for start in range(0, len(ids), 100):
            params = [("user_id", item) for item in ids[start:start + 100]]
            if not params:
                continue
            params.append(("first", "100"))
            data = await self._request("GET", f"{HELIX}/streams", user=False, params=params)
            for stream in data.get("data", []):
                found[str(stream.get("user_id"))] = stream
        return found

    async def _channel_info_for_channels(self, broadcaster_ids):
        found = {}
        ids = [str(item) for item in broadcaster_ids]
        for start in range(0, len(ids), 100):
            params = [("broadcaster_id", item) for item in ids[start:start + 100]]
            if not params:
                continue
            data = await self._request("GET", f"{HELIX}/channels", user=False, params=params)
            for channel in data.get("data", []):
                found[str(channel.get("broadcaster_id"))] = channel
        return found

    async def _sync_manual_activity(self, manual_channels):
        # Activity alerts apply only to channels explicitly selected by the owner.
        monitored = manual_channels[:100]
        if len(manual_channels) > len(monitored):
            log.warning("Activity monitoring is limited to the first 100 manually selected channels")
        if not monitored:
            return

        ids = [str(broadcaster_id) for _, broadcaster_id, _ in monitored]
        streams = await self._streams_for_channels(ids)
        details = await self._channel_info_for_channels(ids)
        checked_at = datetime.now(timezone.utc).isoformat()

        for login, broadcaster_id, _ in monitored:
            key = str(broadcaster_id)
            stream = streams.get(key, {})
            channel = details.get(key, {})
            title = stream.get("title") or channel.get("title") or ""
            category = stream.get("game_name") or channel.get("game_name") or ""
            is_live = key in streams
            def notification(previous):
                changes = activity_changes(previous, is_live, title, category)
                return format_activity(login, changes, is_live, title, category) if changes else None

            await self.storage.record_channel_activity(
                login, key, is_live, title, category, checked_at, notification_factory=notification,
            )
        self.monitor.update("activity", checked_at=time.time(), error=None)

    async def _activity_loop(self):
        while True:
            try:
                manual = [row for row in await self.storage.channels() if row[2] == "manual"]
                await self._sync_manual_activity(manual)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.monitor.update("activity", error=safe_error(exc), error_at=time.time())
                log.exception("Activity check failed; state and unsaved notifications will be retried")
            await asyncio.sleep(self.config.monitor_refresh_seconds)

    def _publish_subscriptions(self, active):
        self.active_chat_count = len(active)
        self.monitor.update(
            "twitch", active=len(active), unwanted_count=len(set(active) - set(self._selected)),
            active_logins=[self._login_by_id[key] for key in active if key in self._login_by_id],
        )

    async def _reconcile_subscriptions(self, active):
        session_id = self._session_id
        actual, cursor = {}, None
        while True:
            params = {"type": "channel.chat.message", "first": 100}
            if cursor:
                params["after"] = cursor
            data = await self._request("GET", f"{HELIX}/eventsub/subscriptions", params=params)
            for row in data.get("data", []):
                if row.get("status") == "enabled" and row.get("transport", {}).get("session_id") == session_id:
                    broadcaster_id = row.get("condition", {}).get("broadcaster_user_id")
                    if broadcaster_id and row.get("id"):
                        actual[str(broadcaster_id)] = row["id"]
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break
        if self._session_id != session_id:
            return  # A handoff raced this read; do not discard migrated subscriptions.
        active.clear()
        active.update(actual)
        self.monitor.update("twitch", confirmed_at=time.time())
        self._publish_subscriptions(active)

    async def subscribe_chat(self, session_id, broadcaster_id):
        payload = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": str(broadcaster_id),
                "user_id": await self.reader_user_id(),
            },
            "transport": {"method": "websocket", "session_id": session_id},
        }
        try:
            data = await self._request("POST", f"{HELIX}/eventsub/subscriptions", json=payload)
        except RuntimeError as exc:
            if "Twitch HTTP 409" in str(exc):
                log.info("EventSub subscription already exists for broadcaster_id=%s", broadcaster_id)
                return None
            raise
        return (data.get("data") or [{}])[0].get("id")

    async def _sync_discovery(self, session_id, active_subscriptions, streams=None):
        if streams is None:
            streams = await self.discover_live()
        await self.storage.sync_catalog_chats([
            (stream["user_login"], str(stream["user_id"]))
            for stream in streams if stream.get("user_id")
        ])
        # Activity channels and watched authors do not reserve chat connections.
        desired = {str(stream["user_id"]): (stream["user_login"], "catalog")
                   for stream in streams if stream.get("user_id")}
        self._selected = {key: row[0] for key, row in desired.items()}
        self._login_by_id.update(self._selected)
        errors = 0

        # Keep each EventSub subscription ID so removed channels can be unsubscribed immediately.
        for broadcaster_id in set(active_subscriptions) - set(desired):
            subscription_id = active_subscriptions[broadcaster_id]
            if await self.delete_subscription(subscription_id):
                active_subscriptions.pop(broadcaster_id, None)
            else:
                errors += 1
        self._publish_subscriptions(active_subscriptions)

        # A normal account may join at most 100 chat rooms concurrently.
        for broadcaster_id, (login, source) in desired.items():
            if broadcaster_id in active_subscriptions:
                continue
            if len(active_subscriptions) >= 100:
                log.warning("EventSub subscription limit reached; remaining channels are skipped")
                break
            try:
                subscription_id = await self.subscribe_chat(self._session_id or session_id, broadcaster_id)
                if subscription_id:
                    active_subscriptions[broadcaster_id] = subscription_id
                    self._publish_subscriptions(active_subscriptions)
                    log.info("Subscribed to @%s (%s)", login, source)
            except Exception as exc:
                errors += 1
                self.monitor.update("twitch", error=safe_error(exc), error_at=time.time())
                log.exception("Could not subscribe to @%s", login)
            # Normal accounts are limited to 20 chat joins per 10 seconds.
            await asyncio.sleep(0.6)

        if self._session_id:
            await self._reconcile_subscriptions(active_subscriptions)
        self._publish_subscriptions(active_subscriptions)
        if not errors:
            self.monitor.update("twitch", error=None)

    async def delete_subscription(self, subscription_id):
        if not subscription_id:
            return False
        try:
            await self._request("DELETE", f"{HELIX}/eventsub/subscriptions", params={"id": subscription_id})
            return True
        except RuntimeError as exc:
            if "Twitch HTTP 404" in str(exc):
                return True
            self.monitor.update("twitch", error=safe_error(exc), error_at=time.time())
            log.warning("Could not remove EventSub subscription: %s", safe_error(exc))
            return False

    async def _scan_loop(self, session_id, active_subscriptions, initial_streams=None):
        while True:
            try:
                await self._sync_discovery(session_id, active_subscriptions, initial_streams)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.monitor.update("scanner", error=safe_error(exc), error_at=time.time())
                log.exception("Channel refresh failed; socket stays connected")
            initial_streams = None
            await asyncio.sleep(self.config.monitor_refresh_seconds)

    async def _listen_once(self):
        # Scan a large catalog BEFORE opening the socket. Twitch expects the first
        # subscription shortly after welcome; 23 HTTP requests must not use that window.
        initial_streams = await self.discover_live()
        if not initial_streams:
            self.active_chat_count = 0
            self.monitor.update("twitch", connected=False, active=0, active_logins=[], phase="waiting", error=None)
            await self.storage.sync_catalog_chats([])
            await asyncio.sleep(self.config.monitor_refresh_seconds)
            return
        queue = asyncio.Queue(maxsize=2048)
        connections = []
        scanner = handoff = None
        active_subscriptions = {}
        current = None

        async def open_socket(url):
            context = websockets.connect(url, ping_interval=None, close_timeout=10, max_queue=1024)
            ws = await context.__aenter__()
            connection = {"context": context, "ws": ws, "reader": None}
            connections.append(connection)
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if first.get("metadata", {}).get("message_type") != "session_welcome":
                    raise RuntimeError("Expected EventSub welcome")
                session = first["payload"]["session"]
                connection["session"] = session
                connection["keepalive"] = int(session.get("keepalive_timeout_seconds") or 10)
                return connection
            except BaseException:
                await context.__aexit__(None, None, None)
                connections.remove(connection)
                raise

        async def reader(connection):
            try:
                while True:
                    raw = await asyncio.wait_for(connection["ws"].recv(), timeout=connection["keepalive"] + 10)
                    await queue.put(("packet", connection, json.loads(raw)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(("closed", connection, exc))

        async def migrate(old, url):
            nonlocal current
            try:
                target = urlsplit(url)
                if target.scheme != "wss" or target.hostname != "eventsub.wss.twitch.tv" or target.port not in {None, 443} or target.username is not None or target.password is not None:
                    raise ValueError("Unexpected EventSub reconnect endpoint")
                new = await open_socket(url)  # Use Twitch's URL verbatim.
                self._session_id = new["session"]["id"]
                current = new
                self.monitor.update("twitch", connected=True, phase="connected", last_packet_at=time.time(), keepalive=new["keepalive"], error=None)
                new["reader"] = asyncio.create_task(reader(new))
                # The old reader keeps draining buffered events during close.
                # Subscriptions are migrated by Twitch, not recreated by us.
                await old["context"].__aexit__(None, None, None)
                if old["reader"]:
                    try:
                        await asyncio.wait_for(old["reader"], timeout=12)
                    except asyncio.TimeoutError:
                        pass
                connections.remove(old)
                log.info("EventSub handoff completed; subscriptions retained")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.monitor.update("twitch", error=safe_error(exc), error_at=time.time())
                log.exception("EventSub handoff failed; keeping the old socket until it closes")

        try:
            current = await open_socket(EVENTSUB_URL)
            self._session_id = current["session"]["id"]
            self.monitor.update("twitch", connected=True, phase="connected", active=0, active_logins=[], last_packet_at=time.time(), keepalive=current["keepalive"], error=None)
            current["reader"] = asyncio.create_task(reader(current))
            scanner = asyncio.create_task(self._scan_loop(self._session_id, active_subscriptions, initial_streams))
            while True:
                event_type, connection, packet = await queue.get()
                if event_type == "closed":
                    if connection is current:
                        raise packet
                    continue
                if connection is current:
                    self.monitor.update("twitch", last_packet_at=time.time())
                kind = packet.get("metadata", {}).get("message_type")
                if kind == "notification":
                    event = packet.get("payload", {}).get("event", {})
                    broadcaster_id = event.get("broadcaster_user_id")
                    if event and (not broadcaster_id or str(broadcaster_id) in self._selected):
                        try:
                            await self.on_message(event)
                            self.monitor.update("twitch", last_event_at=time.time())
                            self.monitor.update("processing", error=None)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self.monitor.update("processing", error=safe_error(exc), error_at=time.time())
                            log.exception("Chat event processing failed; socket stays connected")
                elif kind == "session_reconnect" and connection is current and (handoff is None or handoff.done()):
                    url = packet.get("payload", {}).get("session", {}).get("reconnect_url", "")
                    self.monitor.update("twitch", phase="reconnecting")
                    handoff = asyncio.create_task(migrate(connection, url))
                elif kind == "revocation":
                    subscription = packet.get("payload", {}).get("subscription", {})
                    broadcaster_id = subscription.get("condition", {}).get("broadcaster_user_id")
                    active_subscriptions.pop(str(broadcaster_id), None)
                    self._publish_subscriptions(active_subscriptions)
                    log.warning("EventSub subscription revoked; retry on next scan")
        finally:
            self._session_id = None
            self.active_chat_count = 0
            self.monitor.update("twitch", connected=False, active=0, active_logins=[])
            tasks = [connection["reader"] for connection in connections if connection["reader"]]
            tasks += [task for task in (scanner, handoff) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in connections:
                try:
                    await connection["context"].__aexit__(None, None, None)
                except Exception:
                    log.debug("Socket cleanup failed", exc_info=True)

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            activity = asyncio.create_task(self._activity_loop())
            try:
                while True:
                    try:
                        await self._listen_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.monitor.update("twitch", connected=False, active=0, active_logins=[], error=safe_error(exc), error_at=time.time())
                        log.exception("Twitch connection failed; retrying in 10 seconds")
                        await asyncio.sleep(10)
            finally:
                activity.cancel()
                await asyncio.gather(activity, return_exceptions=True)
