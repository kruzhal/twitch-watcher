"""Shared synchronous SQLite operations for the owner-only dashboard."""
import sqlite3
from contextlib import contextmanager

from .catalog import normalize_login, validate_priority


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def import_rows(db, rows, mode):
    if mode not in {"merge", "replace"} or not rows:
        raise ValueError("Выбери импорт с добавлением или заменой непустого списка")
    db.execute("BEGIN IMMEDIATE")
    db.execute("CREATE TEMP TABLE incoming_catalog(login TEXT PRIMARY KEY, priority INTEGER NOT NULL)")
    db.executemany("INSERT INTO incoming_catalog VALUES (?,?)", rows)
    if mode == "replace":
        db.execute("DELETE FROM channel_catalog WHERE login NOT IN (SELECT login FROM incoming_catalog)")
    # Existing enabled/disabled choices survive both merge and replacement.
    db.executemany(
        "INSERT INTO channel_catalog(login,priority,enabled) VALUES (?,?,1) "
        "ON CONFLICT(login) DO UPDATE SET priority=excluded.priority", rows,
    )
    db.commit()


def change_catalog(db, payload):
    action = payload.get("action")
    login = normalize_login(payload.get("login"))
    priority = validate_priority(payload.get("priority"))
    if action == "add":
        db.execute("INSERT INTO channel_catalog(login,priority) VALUES (?,?)", (login, priority))
    elif action == "update":
        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("Нужно явно указать включён ли поиск в канале")
        cursor = db.execute("UPDATE channel_catalog SET priority=?,enabled=? WHERE login=?", (priority, int(enabled), login))
        if cursor.rowcount != 1:
            raise ValueError("Канал не найден в базе")
    else:
        raise ValueError("Неизвестное действие каталога")
    db.commit()


def catalog_page(db, search="", page=0, enabled="all", limit=50):
    if not isinstance(search, str) or len(search) > 100 or enabled not in {"all", "yes", "no"}:
        raise ValueError("Некорректный фильтр каталога")
    clauses, args = ["instr(login,?)>0"], [search.strip().lstrip("@").casefold()]
    if enabled != "all":
        clauses.append("enabled=?")
        args.append(int(enabled == "yes"))
    where = " WHERE " + " AND ".join(clauses)
    db.execute("BEGIN")
    total = db.execute("SELECT COUNT(*) FROM channel_catalog" + where, args).fetchone()[0]
    page = min(max(0, page), max(0, (total - 1) // limit))
    rows = db.execute(
        "SELECT login,priority,enabled FROM channel_catalog" + where + " ORDER BY priority,login LIMIT ? OFFSET ?",
        (*args, limit, page * limit),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "pages": max(1, (total + limit - 1) // limit)}


def filter_settings(db):
    row = db.execute("SELECT value FROM monitor_settings WHERE key='repeat_cooldown_seconds'").fetchone()
    return {
        "excluded_words": [row[0] for row in db.execute("SELECT phrase FROM excluded_words ORDER BY phrase")],
        "repeat_cooldown_seconds": int(row[0]) if row else 60,
    }


def change_filter(db, payload):
    action = payload.get("action")
    if not isinstance(action, str):
        raise ValueError("Неизвестное действие фильтра")
    if action == "cooldown":
        seconds = payload.get("seconds")
        if type(seconds) is not int or not 0 <= seconds <= 86400:
            raise ValueError("Интервал должен быть от 0 до 86400 секунд")
        db.execute(
            "INSERT INTO monitor_settings(key,value) VALUES ('repeat_cooldown_seconds',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seconds),),
        )
    elif action in {"add", "delete"}:
        phrase = payload.get("phrase")
        if not isinstance(phrase, str) or not 1 <= len(phrase.strip()) <= 200:
            raise ValueError("Укажи исключение длиной от 1 до 200 символов")
        if action == "add":
            db.execute("INSERT OR IGNORE INTO excluded_words(phrase) VALUES (?)", (phrase.strip().casefold(),))
        else:
            db.execute("DELETE FROM excluded_words WHERE phrase=?", (phrase.strip().casefold(),))
    else:
        raise ValueError("Неизвестное действие фильтра")
    db.commit()
