"""Small, credential-free runtime snapshots; no per-message SQLite writes."""
import asyncio
import json
import logging
import os
import time
import uuid

from .version import VERSION

log = logging.getLogger(__name__)


def safe_error(exc):
    code = getattr(exc, "status_code", None)
    return type(exc).__name__ + (f" (HTTP {code})" if isinstance(code, int) else "")


class NullMonitor:
    def update(self, component, **values):
        pass


class RuntimeMonitor:
    def __init__(self, storage):
        self.storage = storage
        self.instance_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.states = {}

    def update(self, component, **values):
        self.states.setdefault(component, {}).update(values)

    async def flush(self):
        self.update("heartbeat", version=VERSION, pid=os.getpid(), started_at=self.started_at)
        await self.storage.write_runtime(self.instance_id, self.states, time.time())

    async def run(self):
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Runtime heartbeat could not be persisted")
            await asyncio.sleep(5)


def summarize_health(rows, *, now=None, pid=None, service_active=True):
    """Never present old snapshots or a merely running process as healthy."""
    now = time.time() if now is None else now
    records = {}
    for name, raw, updated_at, instance_id in rows:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(updated_at, (float, int)):
            records[name] = {"data": data, "updated_at": updated_at, "instance_id": instance_id}
    heartbeat = records.get("heartbeat", {})
    alive = heartbeat.get("data", {})
    fresh = bool(service_active and heartbeat and 0 <= now - heartbeat["updated_at"] <= 20
                 and (pid is None or str(alive.get("pid")) == str(pid)))
    components = {key: record["data"] for key, record in records.items()
                  if record["instance_id"] == heartbeat.get("instance_id")}
    twitch = components.get("twitch", {})
    telegram = components.get("telegram", {})
    delivery = components.get("delivery", {})
    scanner = components.get("scanner", {})
    activity = components.get("activity", {})
    processing = components.get("processing", {})
    packet_age = now - twitch.get("last_packet_at", 0)
    socket_live = fresh and twitch.get("connected", False) and 0 <= packet_age <= twitch.get("keepalive", 10) + 20
    telegram_live = fresh and telegram.get("available", False) and 0 <= now - telegram.get("last_success_at", 0) <= 75
    desired = scanner.get("selected", 0)
    actual = twitch.get("active", 0) if socket_live else 0
    if not fresh:
        state = "stale" if service_active else "stopped"
    elif not telegram_live or delivery.get("phase") not in {"idle", "ready", "sending"} or activity.get("error") or processing.get("error"):
        state = "degraded"
    elif desired == 0 and scanner.get("checked_at") and not scanner.get("error"):
        state = "waiting"
    elif socket_live and actual >= desired and not twitch.get("unwanted_count") and not scanner.get("error") and not twitch.get("error"):
        state = "healthy"
    else:
        state = "degraded"
    return {
        "version": alive.get("version", "unknown"), "state": state, "fresh": fresh,
        "heartbeat_at": heartbeat.get("updated_at"), "twitch_online": bool(socket_live),
        "telegram_online": bool(telegram_live), "active_chats": actual, "desired_chats": desired,
        "live_channels": scanner.get("live", 0), "scanner": scanner,
        "twitch": twitch, "telegram": telegram, "delivery": delivery, "activity": activity, "processing": processing,
    }
