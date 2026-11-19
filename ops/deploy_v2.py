"""Guarded 2.0 upgrade: test a server-side DB copy, preserve live data, back up files.

No CSV reimport, no .env/TLS/auth replacement, no automatic database rollback.
Only release-owned unchanged files are reverted if startup fails.
"""
import argparse
import ast
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE))
from ops.deploy_catalog import run, service_property, digest, db_connect, database_backup, atomic_copy

BOT_SERVICE = "twitch-watcher.service"
PANEL_SERVICE = "twitch-dashboard.service"
BOT_ROOT = Path("/opt/twitch_watcher_v2/twitch_watcher_v2")
PANEL_ROOT = Path("/opt/twitch_watcher/dashboard")


def preserved_snapshot(database):
    """Return counts/hashes only; never serialize chat text into diagnostics."""
    result = {}
    with db_connect(database) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        selections = {
            "channels": "SELECT login,broadcaster_id,source FROM channels ORDER BY login",
            "watched_users": "SELECT login FROM watched_users ORDER BY login",
            "watched_words": "SELECT phrase FROM watched_words ORDER BY phrase",
            "matches": "SELECT id,created_at,channel_login,author_login,message,matched_rule,message_id FROM matches ORDER BY id",
            "notification_outbox": "SELECT id,body,created_at FROM notification_outbox ORDER BY id",
            "channel_activity": "SELECT broadcaster_id,login,is_live,title,category,checked_at FROM channel_activity ORDER BY broadcaster_id",
            "excluded_words": "SELECT phrase FROM excluded_words ORDER BY phrase",
            "monitor_settings": "SELECT key,value FROM monitor_settings ORDER BY key",
        }
        columns = {row[1] for row in db.execute("PRAGMA table_info(channel_catalog)")}
        selections["channel_catalog"] = "SELECT login,priority," + ("enabled" if "enabled" in columns else "1") + " FROM channel_catalog ORDER BY login"
        for table, query in selections.items():
            hasher = hashlib.sha256()
            count = 0
            if table in tables:
                for row in db.execute(query):
                    hasher.update(json.dumps(tuple(row), ensure_ascii=True).encode())
                    hasher.update(b"\n")
                    count += 1
            result[table] = {"rows": count, "sha256": hasher.hexdigest()}
    return result


def verify_files(manifest, *, verify_stage=True):
    for area, root in (("bot", BOT_ROOT), ("panel", PANEL_ROOT)):
        for relative, spec in manifest[area].items():
            for value in (relative, spec["source"]):
                if not isinstance(value, str) or not value or "\\" in value or ":" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
                    raise RuntimeError("Unsafe path in release manifest")
            if area == "panel" and relative not in {"server.py", "app.js", "app.css", "index.html"}:
                raise RuntimeError("Unexpected dashboard asset in release manifest")
            if area == "bot" and not (relative.startswith(("app/", "tests/", "ops/")) or relative == "README-2.0.md"):
                raise RuntimeError("Refusing to replace data or configuration outside release-owned paths")
            target = root / relative
            previous = spec["previous"]
            if target.is_symlink():
                raise RuntimeError(f"Refusing to replace a symlink: {area}/{relative}")
            if previous is None:
                if target.exists():
                    raise RuntimeError(f"Unreviewed new-file destination already exists: {area}/{relative}")
            elif not target.is_file() or digest(target) != previous:
                raise RuntimeError(f"Server source differs from baseline: {area}/{relative}")
            if verify_stage and digest(STAGE / spec["source"]) != spec["sha256"]:
                raise RuntimeError(f"Release checksum mismatch: {area}/{relative}")
    for relative, expected in manifest["baseline_app"].items():
        if digest(BOT_ROOT / relative) != expected:
            raise RuntimeError(f"Baseline application changed: {relative}")


def migrate(executable, database):
    script = "import asyncio,sys; from app.storage import Storage; asyncio.run(Storage(sys.argv[1]).init())"
    run(executable, "-B", "-c", script, str(database), cwd=STAGE)


def preflight(manifest):
    if manifest.get("version") != "2.0.0" or STAGE in {BOT_ROOT, PANEL_ROOT}:
        raise RuntimeError("Unexpected release or staging directory")
    for unit in (BOT_SERVICE, PANEL_SERVICE):
        if service_property(unit, "ActiveState") != "active":
            raise RuntimeError(f"Unexpected inactive service: {unit}")
    if Path(service_property(BOT_SERVICE, "WorkingDirectory")).resolve() != BOT_ROOT:
        raise RuntimeError("Unexpected bot working directory")
    pid = service_property(BOT_SERVICE, "MainPID")
    executable = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[0]
    panel_pid = service_property(PANEL_SERVICE, "MainPID")
    panel_args = Path(f"/proc/{panel_pid}/cmdline").read_bytes().decode().split("\0")
    panel_cwd = Path(f"/proc/{panel_pid}/cwd").resolve()
    if not any((panel_cwd / arg).resolve() == PANEL_ROOT / "server.py" for arg in panel_args[1:] if arg):
        raise RuntimeError("Unexpected running dashboard script")
    verify_files(manifest)
    for folder in ("app", "dashboard", "ops", "tests"):
        for source in (STAGE / folder).glob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    configured = run(executable, "-B", "-c", "from pathlib import Path; from app.config import Config; print(Path(Config().database_path).resolve())", cwd=BOT_ROOT).stdout.strip()
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if item.startswith(b"DATABASE_PATH="):
            configured = str((BOT_ROOT / item.split(b"=", 1)[1].decode()).resolve())
    database = BOT_ROOT / "data/watcher.sqlite3"
    if Path(configured) != database:
        raise RuntimeError("Unexpected production database path")
    tests = run(executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v", cwd=STAGE)
    print(tests.stderr, flush=True)
    clone = STAGE / "migration-check.sqlite3"
    if clone.exists():
        raise RuntimeError("Use a fresh staging folder")
    database_backup(database, clone)
    before = preserved_snapshot(clone)
    migrate(executable, clone)
    migrate(executable, clone)  # Idempotency is checked against the actual server data.
    if preserved_snapshot(clone) != before:
        raise RuntimeError("Migration changed existing data in the validation copy")
    with db_connect(clone) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise RuntimeError("Migration schema version did not verify")
    print(json.dumps({"preflight": "passed", "migration": "verified-on-server-copy", "catalog_rows": before["channel_catalog"]["rows"]}), flush=True)
    return executable, database


def install(manifest, executable, database):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BOT_ROOT.parent / "backups" / ("version-2.0-" + stamp)
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    backup.chmod(0o700)
    records, applied, stopped = [], [], []
    try:
        for unit in (PANEL_SERVICE, BOT_SERVICE):
            run("systemctl", "stop", unit)
            stopped.append(unit)
        verify_files(manifest)  # Catch edits made while preflight was running.
        before = preserved_snapshot(database)
        database_backup(database, backup / "watcher.sqlite3")
        for area, root in (("bot", BOT_ROOT), ("panel", PANEL_ROOT)):
            for relative, spec in manifest[area].items():
                target = root / relative
                saved = backup / ("file-" + str(len(records)))
                existed = target.exists()
                if existed:
                    shutil.copy2(target, saved)
                records.append({"target": str(target), "backup": str(saved), "existed": existed, "release_sha256": spec["sha256"], "source": spec["source"]})
        (backup / "files.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        (backup / "before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
        for record in records:
            atomic_copy(STAGE / record["source"], Path(record["target"]))
            applied.append(record)
        migrate(executable, database)
        if preserved_snapshot(database) != before:
            raise RuntimeError("Production migration did not preserve the existing data")
        run("systemctl", "start", BOT_SERVICE)
        bot_pid = service_property(BOT_SERVICE, "MainPID")
        run("systemctl", "start", PANEL_SERVICE)
        panel_pid = service_property(PANEL_SERVICE, "MainPID")
        good = 0
        for _ in range(25):
            time.sleep(1)
            for unit, expected_pid in ((BOT_SERVICE, bot_pid), (PANEL_SERVICE, panel_pid)):
                if service_property(unit, "ActiveState") != "active" or service_property(unit, "MainPID") != expected_pid:
                    raise RuntimeError(f"New process did not remain active: {unit}")
            with db_connect(database) as db:
                row = db.execute("SELECT data,updated_at FROM runtime_status WHERE component='heartbeat'").fetchone()
            data = json.loads(row[0]) if row else {}
            fresh = row and 0 <= time.time() - row[1] <= 20
            good = good + 1 if fresh and data.get("version") == "2.0.0" and str(data.get("pid")) == bot_pid else 0
            if good >= 5:
                break
        else:
            raise RuntimeError("No fresh heartbeat from version 2.0")
        report = {"version": "2.0.0", "backup": str(backup), "services_active": True, "heartbeat_verified": True, "preserved": before}
        (backup / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    except Exception:
        for unit in (PANEL_SERVICE, BOT_SERVICE):
            if unit in stopped:
                run("systemctl", "stop", unit)
        for record in reversed(applied):
            target = Path(record["target"])
            if not target.exists() or digest(target) != record["release_sha256"]:
                print(f"Not reverting a file changed outside this installer: {target}", flush=True)
                continue
            if record["existed"]:
                atomic_copy(Path(record["backup"]), target)
            else:
                target.unlink()  # Only our newly created, unchanged release file.
        # Keep all new messages, rule/catalog edits and delivery acknowledgements.
        # Additive v2 schema is compatible with the previous application version.
        for unit in (BOT_SERVICE, PANEL_SERVICE):
            if unit in stopped:
                run("systemctl", "start", unit)
        print(f"Upgrade failed; own file changes reverted; database not replaced. Backup: {backup}", flush=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((STAGE / "release-manifest.json").read_text(encoding="utf-8"))
    executable, database = preflight(manifest)
    if args.apply:
        install(manifest, executable, database)


if __name__ == "__main__":
    main()
