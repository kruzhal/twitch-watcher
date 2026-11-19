import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.admin_store import database
from app.storage import Storage
from ops import deploy_v2 as upgrade


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bot = self.root / "bot"
        self.panel = self.root / "panel"
        self.stage = self.root / "stage"
        for path in (self.bot / "app", self.panel, self.stage / "app", self.stage / "dashboard"):
            path.mkdir(parents=True, exist_ok=True)
        self.db = self.bot / "data/watcher.sqlite3"
        self.storage = Storage(str(self.db))
        await self.storage.init()
        await self.storage.replace_catalog([("catalog", 1)])
        (self.bot / "app/code.py").write_text("old-code", encoding="utf-8")
        (self.panel / "server.py").write_text("old-panel", encoding="utf-8")
        (self.stage / "app/code.py").write_text("new-code", encoding="utf-8")
        (self.stage / "app/new.py").write_text("new-module", encoding="utf-8")
        (self.stage / "dashboard/server.py").write_text("new-panel", encoding="utf-8")
        def spec(source, previous):
            return {"source": source, "sha256": hashlib.sha256((self.stage / source).read_bytes()).hexdigest(), "previous": hashlib.sha256(previous.encode()).hexdigest() if previous else None}
        self.manifest = {
            "version": "2.0.0", "baseline_app": {},
            "bot": {"app/code.py": spec("app/code.py", "old-code"), "app/new.py": spec("app/new.py", None)},
            "panel": {"server.py": spec("dashboard/server.py", "old-panel")},
        }

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def exercise_failed_upgrade(self, external_edit=False):
        starts = 0
        def run(*args, **kwargs):
            nonlocal starts
            if args == ("systemctl", "start", upgrade.BOT_SERVICE):
                starts += 1
                if starts == 1:
                    with database(self.db) as db:
                        db.execute("INSERT INTO matches(created_at,channel_login,author_login,message,matched_rule) VALUES ('now','chat','author','new-message','rule')")
                        db.execute("INSERT INTO watched_words VALUES ('new-rule')")
                        db.execute("INSERT INTO notification_outbox(body) VALUES ('new-notification')")
                        db.execute("UPDATE channel_catalog SET priority=42,enabled=0 WHERE login='catalog'")
                    if external_edit:
                        (self.bot / "app/code.py").write_text("user-edit-after-upgrade", encoding="utf-8")
        def prop(unit, name):
            if name == "ActiveState":
                return "active"
            if unit == upgrade.PANEL_SERVICE:
                return "300"
            prop.bot_calls += 1
            return "200" if prop.bot_calls == 1 else "999"
        prop.bot_calls = 0
        with patch.object(upgrade, "BOT_ROOT", self.bot), patch.object(upgrade, "PANEL_ROOT", self.panel), \
                patch.object(upgrade, "STAGE", self.stage), patch.object(upgrade, "run", side_effect=run), \
                patch.object(upgrade, "service_property", side_effect=prop), \
                patch.object(upgrade, "migrate"), patch.object(upgrade.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not remain active"):
                upgrade.install(self.manifest, "fake-python", self.db)
        return starts

    async def test_failed_upgrade_restores_files_but_keeps_live_user_data(self):
        self.assertEqual(await self.exercise_failed_upgrade(), 2)
        self.assertEqual((self.bot / "app/code.py").read_text(), "old-code")
        self.assertEqual((self.panel / "server.py").read_text(), "old-panel")
        self.assertFalse((self.bot / "app/new.py").exists())
        self.assertEqual(await self.storage.match_count(), 1)
        self.assertEqual(await self.storage.rules("watched_words"), ["new-rule"])
        self.assertEqual((await self.storage.next_notification())[1], "new-notification")
        with database(self.db) as db:
            self.assertEqual(tuple(db.execute("SELECT priority,enabled FROM channel_catalog").fetchone()), (42, 0))

    async def test_failed_upgrade_does_not_overwrite_external_code_edit(self):
        await self.exercise_failed_upgrade(external_edit=True)
        self.assertEqual((self.bot / "app/code.py").read_text(), "user-edit-after-upgrade")

    async def test_source_guard_refuses_unknown_files_before_installation(self):
        (self.bot / "app/new.py").write_text("user-file", encoding="utf-8")
        with patch.object(upgrade, "BOT_ROOT", self.bot), patch.object(upgrade, "PANEL_ROOT", self.panel), patch.object(upgrade, "STAGE", self.stage):
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                upgrade.verify_files(self.manifest)
        self.assertEqual((self.bot / "app/new.py").read_text(), "user-file")

    async def test_newer_schema_is_not_downgraded(self):
        with database(self.db) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeError):
            await self.storage.init()
        with database(self.db) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    async def test_manifest_cannot_write_outside_release_directories(self):
        manifest = {"bot": {"../outside.py": self.manifest["bot"]["app/code.py"]}, "panel": {}, "baseline_app": {}}
        with patch.object(upgrade, "BOT_ROOT", self.bot), patch.object(upgrade, "PANEL_ROOT", self.panel), patch.object(upgrade, "STAGE", self.stage):
            with self.assertRaisesRegex(RuntimeError, "Unsafe path"):
                upgrade.verify_files(manifest)


if __name__ == "__main__":
    unittest.main()
