import aiosqlite
import time
import hashlib
import json
from pathlib import Path

from .schema import SCHEMA, MIGRATIONS, SCHEMA_VERSION


class Storage:
    def __init__(self, path: str):
        self.path = path
        self._rule_cache = {}

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            if (await (await db.execute("PRAGMA user_version")).fetchone())[0] > SCHEMA_VERSION:
                raise RuntimeError("База создана более новой версией приложения")
            await db.execute('PRAGMA journal_mode=WAL')
            await db.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                columns = {row[1] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()}
                if column not in columns:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await db.commit()

    async def add_channel(self, login, broadcaster_id, source="manual"):
        login = login.strip().lstrip("@").casefold()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO channels(login, broadcaster_id, source) VALUES (?, ?, ?)
                   ON CONFLICT(login) DO UPDATE SET
                     broadcaster_id=excluded.broadcaster_id,
                     source=CASE WHEN channels.source='manual' THEN 'manual' ELSE excluded.source END""",
                (login, str(broadcaster_id), source),
            )
            await db.commit()

    async def del_channel(self, login):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT broadcaster_id FROM channels WHERE login=?", (login.strip().lstrip("@").casefold(),))
            row = await cur.fetchone()
            await db.execute("DELETE FROM channels WHERE login=?", (login.strip().lstrip("@").casefold(),))
            if row:
                await db.execute("DELETE FROM channel_activity WHERE broadcaster_id=?", (row[0],))
            await db.commit()

    async def channels(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT login, broadcaster_id, source FROM channels ORDER BY login")
            return await cur.fetchall()

    async def remove_auto_not_live(self, live_ids):
        async with aiosqlite.connect(self.path) as db:
            if live_ids:
                placeholders = ",".join("?" for _ in live_ids)
                await db.execute(
                    f"DELETE FROM channels WHERE source='auto' AND broadcaster_id NOT IN ({placeholders})",
                    tuple(str(item) for item in live_ids),
                )
            else:
                await db.execute("DELETE FROM channels WHERE source='auto'")
            await db.commit()

    async def sync_auto_channels(self, channels):
        async with aiosqlite.connect(self.path) as db:
            ids = [str(item[1]) for item in channels]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await db.execute(f"DELETE FROM channels WHERE source='auto' AND broadcaster_id NOT IN ({placeholders})", ids)
            else:
                await db.execute("DELETE FROM channels WHERE source='auto'")
            await db.executemany(
                "INSERT INTO channels(login,broadcaster_id,source) VALUES (?,?,'auto') "
                "ON CONFLICT(login) DO NOTHING",
                [(login, str(broadcaster_id)) for login, broadcaster_id in channels],
            )
            await db.commit()

    async def replace_catalog(self, channels):
        """Replace only the chat catalog; keep activity channels, rules and history."""
        if not channels:
            raise ValueError("Каталог каналов не должен быть пустым")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            choices = dict(await (await db.execute("SELECT login,enabled FROM channel_catalog")).fetchall())
            await db.execute("DELETE FROM channel_catalog")
            await db.executemany(
                "INSERT INTO channel_catalog(login,priority,enabled) VALUES (?,?,?)",
                [(login, priority, choices.get(login, 1)) for login, priority in channels],
            )
            await db.commit()

    async def catalog_channels(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute(
                "SELECT login, priority FROM channel_catalog WHERE enabled=1 ORDER BY priority, login"
            )).fetchall()

    async def catalog_count(self):
        async with aiosqlite.connect(self.path) as db:
            return (await (await db.execute("SELECT COUNT(*) FROM channel_catalog")).fetchone())[0]

    async def sync_catalog_chats(self, channels):
        """Persist selected live chats without changing manually selected activity channels."""
        async with aiosqlite.connect(self.path) as db:
            ids = [str(item[1]) for item in channels]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM channels WHERE source IN ('auto','catalog') AND broadcaster_id NOT IN ({placeholders})", ids,
                )
            else:
                await db.execute("DELETE FROM channels WHERE source IN ('auto','catalog')")
            # Upgrade selected legacy auto rows too; never overwrite manual activity choices.
            await db.executemany(
                "INSERT INTO channels(login,broadcaster_id,source) VALUES (?,?,'catalog') "
                "ON CONFLICT(login) DO UPDATE SET broadcaster_id=excluded.broadcaster_id, "
                "source=CASE WHEN channels.source='manual' THEN 'manual' ELSE 'catalog' END",
                [(login, str(broadcaster_id)) for login, broadcaster_id in channels],
            )
            await db.commit()

    async def record_channel_activity(self, login, broadcaster_id, is_live, title, category, checked_at, notification_factory=None):
        broadcaster_id = str(broadcaster_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT is_live, title, category FROM channel_activity WHERE broadcaster_id=?",
                (broadcaster_id,),
            )
            previous = await cur.fetchone()
            notification = notification_factory(previous) if notification_factory else None
            await db.execute(
                """INSERT INTO channel_activity(broadcaster_id, login, is_live, title, category, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(broadcaster_id) DO UPDATE SET
                     login=excluded.login, is_live=excluded.is_live, title=excluded.title,
                     category=excluded.category, checked_at=excluded.checked_at""",
                (broadcaster_id, login, int(is_live), title or "", category or "", checked_at),
            )
            if notification:
                await db.execute("INSERT INTO notification_outbox(body) VALUES (?)", (notification,))
            await db.commit()
            return previous

    async def channel_activity(self, limit=30):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT a.login, a.is_live, a.title, a.category, a.checked_at
                   FROM channel_activity a JOIN channels c ON c.broadcaster_id=a.broadcaster_id
                   WHERE c.source='manual'
                   ORDER BY a.is_live DESC, a.login LIMIT ?""",
                (limit,),
            )
            return await cur.fetchall()

    async def add_rule(self, table, value):
        self._rule_cache.pop(table, None)
        if table not in {"watched_users", "watched_words", "excluded_words"}:
            raise ValueError("Invalid table")
        column = "login" if table == "watched_users" else "phrase"
        normalized = value.strip().lstrip("@").casefold() if table == "watched_users" else value.strip().casefold()
        if not normalized:
            raise ValueError("Rule cannot be empty")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"INSERT OR IGNORE INTO {table}({column}) VALUES (?)", (normalized,))
            await db.commit()

    async def del_rule(self, table, value):
        self._rule_cache.pop(table, None)
        if table not in {"watched_users", "watched_words", "excluded_words"}:
            raise ValueError("Invalid table")
        column = "login" if table == "watched_users" else "phrase"
        normalized = value.strip().lstrip("@").casefold() if table == "watched_users" else value.strip().casefold()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"DELETE FROM {table} WHERE {column}=?", (normalized,))
            await db.commit()

    async def rules(self, table):
        cached = self._rule_cache.get(table)
        if cached and time.monotonic() - cached[0] < 3:
            return list(cached[1])
        if table not in {"watched_users", "watched_words", "excluded_words"}:
            raise ValueError("Invalid table")
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(f"SELECT * FROM {table} ORDER BY 1")
            values = [row[0] for row in await cur.fetchall()]
            self._rule_cache[table] = (time.monotonic(), values)
            return list(values)

    async def save_match(self, created_at, channel, author, message, rule, message_id, notification=None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if message_id:
                existing = await (await db.execute(
                    "SELECT 1 FROM matches WHERE message_id=? AND matched_rule=? LIMIT 1",
                    (message_id, rule),
                )).fetchone()
                if existing:
                    return
            await db.execute(
                "INSERT INTO matches(created_at, channel_login, author_login, message, matched_rule, message_id) VALUES (?, ?, ?, ?, ?, ?)",
                (created_at, channel, author, message, rule, message_id),
            )
            if notification:
                setting = await (await db.execute(
                    "SELECT value FROM monitor_settings WHERE key='repeat_cooldown_seconds'"
                )).fetchone()
                cooldown = int(setting[0]) if setting else 60
                fingerprint = hashlib.sha256(json.dumps(
                    [channel.casefold(), author.casefold(), " ".join(message.casefold().split()), rule.casefold()],
                    ensure_ascii=False,
                ).encode()).hexdigest()
                now = time.time()
                previous = await (await db.execute(
                    "SELECT last_queued FROM notification_fingerprints WHERE fingerprint=?", (fingerprint,),
                )).fetchone()
                suppressed = cooldown > 0 and previous and 0 <= now - previous[0] < cooldown
                if not suppressed:
                    await db.execute("INSERT INTO notification_outbox(body) VALUES (?)", (notification,))
                    if cooldown > 0:
                        await db.execute(
                            "INSERT INTO notification_fingerprints(fingerprint,last_queued) VALUES (?,?) "
                            "ON CONFLICT(fingerprint) DO UPDATE SET last_queued=excluded.last_queued", (fingerprint, now),
                        )
                await db.execute("DELETE FROM notification_fingerprints WHERE last_queued < ?", (now - 86400,))
            await db.commit()

    async def enqueue_notification(self, body):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO notification_outbox(body) VALUES (?)", (body,))
            await db.commit()

    async def next_notification(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT id, body FROM notification_outbox ORDER BY id LIMIT 1")).fetchone()

    async def queue_count(self):
        async with aiosqlite.connect(self.path) as db:
            return (await (await db.execute("SELECT COUNT(*) FROM notification_outbox")).fetchone())[0]

    async def acknowledge_notification(self, notification_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM notification_outbox WHERE id=?", (notification_id,))
            await db.commit()

    async def write_runtime(self, instance_id, states, updated_at):
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT INTO runtime_status(component,data,updated_at,instance_id) VALUES (?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at, instance_id=excluded.instance_id",
                [(name, json.dumps(data), updated_at, instance_id) for name, data in states.items()],
            )
            await db.commit()

    async def runtime_rows(self):
        async with aiosqlite.connect(self.path) as db:
            return await (await db.execute("SELECT component,data,updated_at,instance_id FROM runtime_status")).fetchall()

    async def set_repeat_cooldown(self, seconds):
        if type(seconds) is not int or not 0 <= seconds <= 86400:
            raise ValueError("Интервал должен быть от 0 до 86400 секунд")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO monitor_settings(key,value) VALUES ('repeat_cooldown_seconds',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seconds),),
            )
            await db.commit()

    async def repeat_cooldown(self):
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT value FROM monitor_settings WHERE key='repeat_cooldown_seconds'")).fetchone()
            return int(row[0]) if row else 60

    async def match_count(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM matches")
            return (await cur.fetchone())[0]

    async def recent_matches(self, limit=10):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT created_at, channel_login, author_login, message, matched_rule "
                "FROM matches ORDER BY id DESC LIMIT ?", (limit,),
            )
            return await cur.fetchall()

    async def history_page(self, *, channel="", phrase="", since="", until="", page=0, limit=5):
        clauses, args = [], []
        if channel:
            clauses.append("channel_login = ? COLLATE NOCASE")
            args.append(channel)
        if phrase:
            clauses.append("instr(matched_rule, ?) > 0")
            args.append(phrase.casefold())
        if since:
            clauses.append("julianday(created_at) >= julianday(?)")
            args.append(since)
        if until:
            clauses.append("julianday(created_at) < julianday(?)")
            args.append(until)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            total = (await (await db.execute("SELECT COUNT(*) FROM matches" + where, args)).fetchone())[0]
            page = min(max(0, page), max(0, (total - 1) // limit))
            rows = await (await db.execute(
                "SELECT created_at, channel_login, author_login, message, matched_rule FROM matches"
                + where + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, page * limit),
            )).fetchall()
            return rows, total, page
