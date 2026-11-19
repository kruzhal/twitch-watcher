"""Additive SQLite schema shared by the async bot and the stdlib web panel."""
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS channels (
    login TEXT PRIMARY KEY, broadcaster_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS watched_users (login TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS watched_words (phrase TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS excluded_words (phrase TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS channel_catalog (
    login TEXT PRIMARY KEY, priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    channel_login TEXT NOT NULL, author_login TEXT NOT NULL,
    message TEXT NOT NULL, matched_rule TEXT NOT NULL, message_id TEXT
);
CREATE INDEX IF NOT EXISTS matches_message_rule ON matches(message_id,matched_rule);
CREATE TABLE IF NOT EXISTS channel_activity (
    broadcaster_id TEXT PRIMARY KEY, login TEXT NOT NULL, is_live INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_fingerprints (
    fingerprint TEXT PRIMARY KEY, last_queued REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fingerprints_age ON notification_fingerprints(last_queued);
CREATE TABLE IF NOT EXISTS runtime_status (
    component TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL,
    instance_id TEXT NOT NULL
);
"""
MIGRATIONS = (
    ("channels", "source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("channel_catalog", "enabled", "INTEGER NOT NULL DEFAULT 1"),
)


def migrate_sync(db):
    if db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
        raise RuntimeError("База создана более новой версией приложения")
    db.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()
