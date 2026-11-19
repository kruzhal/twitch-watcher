"""Install this staged catalog release on the existing Linux/systemd server.

Requires a deployment-manifest.json produced locally from the reviewed baseline.
Without --apply, performs only preflight checks and tests in the staging folder.
Never copies .env, credentials, TLS files, or unrelated server configuration.
"""
import argparse
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


STAGE = Path(__file__).resolve().parents[1]


@contextmanager
def db_connect(*args, **kwargs):
    db = sqlite3.connect(*args, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=120, check=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service_property(service, name):
    return run("systemctl", "show", service, "-p", name, "--value").stdout.strip()


def snapshot(db_path):
    with db_connect(f"file:{db_path}?mode=ro", uri=True, timeout=15) as db:
        return {
            "manual": db.execute("SELECT login,broadcaster_id FROM channels WHERE source='manual' ORDER BY login").fetchall(),
            "users": db.execute("SELECT login FROM watched_users ORDER BY login").fetchall(),
            "words": db.execute("SELECT phrase FROM watched_words ORDER BY phrase").fetchall(),
            "matches": db.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
            "outbox": db.execute("SELECT id,body FROM notification_outbox ORDER BY id").fetchall(),
        }


def database_backup(source, destination):
    with db_connect(f"file:{source}?mode=ro", uri=True, timeout=15) as src:
        with db_connect(destination, timeout=15) as dst:
            src.backup(dst)
    destination.chmod(0o600)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".catalog-next")
    if temporary.exists():
        raise RuntimeError(f"Temporary destination already exists: {temporary}")
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod((target.stat().st_mode & 0o777) if target.exists() else 0o644)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def dashboard_target(manifest):
    """Only update a running panel when every replaced asset matches the baseline."""
    units = json.loads(run("systemctl", "list-units", "--type=service", "--state=running", "--output=json", "--no-pager").stdout)
    for unit in units:
        name = unit["unit"]
        if not any(word in name for word in ("watch", "twitch", "dashboard", "panel")):
            continue
        pid = service_property(name, "MainPID")
        if not pid.isdigit() or pid == "0":
            continue
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
            cwd = Path(f"/proc/{pid}/cwd").resolve()
        except OSError:
            continue
        for arg in args[1:]:
            if Path(arg).name != "server.py":
                continue
            script = (cwd / arg).resolve()
            root = script.parent
            if all((root / file).is_file() and digest(root / file) == expected
                   for file, expected in manifest["dashboard_baseline"].items()):
                return name, root
    return None, None


def preflight(target, service, manifest):
    target = target.resolve()
    if STAGE == target or not target.is_dir():
        raise RuntimeError("Use a separate staging folder and an existing installation")
    if service_property(service, "ActiveState") != "active":
        raise RuntimeError("Bot is not active; refusing to change an unexpected server state")
    if Path(service_property(service, "WorkingDirectory")).resolve() != target:
        raise RuntimeError("Unexpected service working directory")
    for relative, expected in manifest["baseline"].items():
        path = target / relative
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError(f"Server source differs from the reviewed baseline: {relative}")
    for relative in manifest["files"]:
        path = target / relative
        expected = manifest["replacement_baseline"].get(relative)
        if path.exists() and (expected is None or digest(path) != expected):
            raise RuntimeError(f"Refusing to overwrite an unreviewed file: {relative}")
        source = STAGE / relative
        if digest(source) != manifest["files"][relative]:
            raise RuntimeError(f"Staging checksum mismatch: {relative}")
    for source in (STAGE / "app").glob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for relative, expected in manifest["dashboard_files"].items():
        if digest(STAGE / "dashboard" / relative) != expected:
            raise RuntimeError(f"Staging panel checksum mismatch: {relative}")
    pid = service_property(service, "MainPID")
    executable = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[0]
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise RuntimeError("Could not identify the bot's Python interpreter")
    configured = run(
        executable, "-B", "-c",
        "from pathlib import Path; from app.config import Config; print(Path(Config().database_path).resolve())",
        cwd=target,
    ).stdout.strip()
    initial_env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    for item in initial_env:
        if item.startswith(b"DATABASE_PATH="):
            configured = str((target / item.split(b"=", 1)[1].decode()).resolve())
            break
    if Path(configured) != target / "data" / "watcher.sqlite3":
        raise RuntimeError("Service uses a different database path; review it before deployment")
    tests = run(executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v", cwd=STAGE)
    print(tests.stderr, flush=True)
    validation_db = STAGE / "catalog-validation.sqlite3"
    if validation_db.exists():
        raise RuntimeError("Use a fresh staging folder for deployment")
    run(executable, "-B", "-m", "app.import_channels", "channels.csv", "--database", str(validation_db), cwd=STAGE)
    with db_connect(validation_db) as db:
        count = db.execute("SELECT COUNT(*) FROM channel_catalog").fetchone()[0]
    if count != manifest["catalog_count"]:
        raise RuntimeError("Unexpected imported channel count")
    panel_service, panel_root = dashboard_target(manifest)
    return executable, panel_service, panel_root


def deploy(target, service, manifest, executable, panel_service, panel_root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.parent / "backups" / f"catalog-{stamp}"
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup.chmod(0o700)
    replacements = [(STAGE / relative, target / relative) for relative in manifest["files"]]
    if panel_root:
        replacements += [(STAGE / "dashboard" / relative, panel_root / relative)
                         for relative in manifest["dashboard_baseline"]]
    records = []
    for index, (_, destination) in enumerate(replacements):
        saved = backup / f"file-{index:02}"
        exists = destination.exists()
        if exists:
            shutil.copy2(destination, saved)
        records.append({"destination": str(destination), "saved": str(saved), "existed": exists})
    (backup / "files.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    database = target / "data" / "watcher.sqlite3"
    baseline = None
    stopped = False
    try:
        run("systemctl", "stop", service)
        stopped = True
        baseline = snapshot(database)
        database_backup(database, backup / "watcher.sqlite3")
        for source, destination in replacements:
            atomic_copy(source, destination)
        run(executable, "-B", "-m", "app.import_channels", "channels.csv", "--database", str(database), "--replace", cwd=target)
        with db_connect(database, timeout=15) as db:
            db.execute("DELETE FROM channels WHERE source='auto'")
            db.commit()
        if snapshot(database) != baseline:
            raise RuntimeError("Existing manual channels, rules, history or queue changed unexpectedly")
        run("systemctl", "start", service)
        started_pid = service_property(service, "MainPID")
        if panel_service:
            run("systemctl", "restart", panel_service)
        for _ in range(5):
            time.sleep(2)
            if service_property(service, "ActiveState") != "active":
                raise RuntimeError("Bot did not remain active after installation")
            if service_property(service, "MainPID") != started_pid:
                raise RuntimeError("Bot restarted unexpectedly after installation")
            if panel_service and service_property(panel_service, "ActiveState") != "active":
                raise RuntimeError("Panel did not remain active after installation")
        report = {
            "backup": str(backup), "catalog_channels": manifest["catalog_count"],
            "manual_preserved": len(baseline["manual"]), "matches_before": baseline["matches"],
            "service_active": True, "dashboard_updated": bool(panel_service),
            "dashboard_service": panel_service,
        }
        (backup / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    except Exception:
        if stopped:
            run("systemctl", "stop", service)
            for record in reversed(records):
                destination = Path(record["destination"])
                if record["existed"]:
                    atomic_copy(Path(record["saved"]), destination)
                elif destination.exists():
                    destination.unlink()
            saved_db = backup / "watcher.sqlite3"
            if saved_db.exists():
                # Never overwrite post-start chat history, delivery acknowledgements,
                # rule edits or manual choices with an older database snapshot.
                # The old bot ignores the additive channel_catalog table.
                with db_connect(saved_db) as src:
                    old_auto = src.execute(
                        "SELECT login,broadcaster_id,source FROM channels WHERE source='auto'"
                    ).fetchall()
                with db_connect(database, timeout=15) as dst:
                    dst.execute("DELETE FROM channels WHERE source='catalog'")
                    dst.executemany(
                        "INSERT OR IGNORE INTO channels(login,broadcaster_id,source) VALUES (?,?,?)", old_auto,
                    )
                    dst.commit()
            run("systemctl", "start", service)
            if panel_service:
                run("systemctl", "restart", panel_service)
        print(f"Deployment failed; rollback attempted. Backup: {backup}", flush=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="/opt/twitch_watcher_v2/twitch_watcher_v2")
    parser.add_argument("--service", default="twitch-watcher.service")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target = Path(args.target).resolve()
    manifest = json.loads((STAGE / "deployment-manifest.json").read_text(encoding="utf-8"))
    executable, panel_service, panel_root = preflight(target, args.service, manifest)
    print(json.dumps({"preflight": "passed", "dashboard_matched": bool(panel_service)}), flush=True)
    if args.apply:
        deploy(target, args.service, manifest, executable, panel_service, panel_root)


if __name__ == "__main__":
    main()
