import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import Storage
from ops import deploy_catalog as installer


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.stage = self.root / "stage"
        self.target = self.root / "target"
        self.stage.mkdir()
        self.target.mkdir()
        self.database = self.target / "data" / "watcher.sqlite3"
        self.storage = Storage(str(self.database))
        await self.storage.init()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_preflight_refuses_unreviewed_server_sources(self):
        (self.target / "changed.py").write_text("user_changes = True", encoding="utf-8")
        manifest = {"baseline": {"changed.py": "not_the_current_hash"}}
        with patch.object(installer, "service_property", side_effect=["active", str(self.target)]):
            with self.assertRaisesRegex(RuntimeError, "differs from the reviewed baseline"):
                installer.preflight(self.target, "test.service", manifest)
        self.assertEqual((self.target / "changed.py").read_text(), "user_changes = True")

    async def test_atomic_copy_refuses_existing_temporary_file(self):
        source = self.stage / "file.py"
        source.write_text("new", encoding="utf-8")
        destination = self.target / "file.py"
        destination.write_text("old", encoding="utf-8")
        pending = self.target / "file.py.catalog-next"
        pending.write_text("unrelated", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            installer.atomic_copy(source, destination)
        self.assertEqual(destination.read_text(), "old")
        self.assertEqual(pending.read_text(), "unrelated")

    async def test_rollback_keeps_new_history_rules_manual_channels_and_queue(self):
        await self.storage.add_channel("old_auto", "1", "auto")
        await self.storage.add_channel("old_manual", "2", "manual")
        (self.stage / "file.py").write_text("new_code", encoding="utf-8")
        (self.target / "file.py").write_text("old_code", encoding="utf-8")
        manifest = {"files": {"file.py": "unused_in_deploy"}, "catalog_count": 1}
        starts = 0

        def run(*args, **kwargs):
            nonlocal starts
            if "app.import_channels" in args:
                with installer.db_connect(self.database) as db:
                    db.execute("INSERT INTO channel_catalog(login,priority) VALUES ('catalog_login',1)")
            if args[:2] == ("systemctl", "start"):
                starts += 1
                if starts == 1:
                    # Legitimate events/edits after the new bot starts, before a
                    # later health check fails. A rollback must not discard them.
                    with installer.db_connect(self.database) as db:
                        db.execute("INSERT INTO matches(created_at,channel_login,author_login,message,matched_rule) VALUES ('now','chat','author','hello','rule')")
                        db.execute("INSERT INTO watched_words VALUES ('new_word')")
                        db.execute("INSERT INTO channels VALUES ('new_manual','3','manual')")
                        db.execute("INSERT INTO channels VALUES ('catalog_login','4','catalog')")
                        db.execute("INSERT INTO notification_outbox(body) VALUES ('new_notification')")

        properties = iter(["200", "active", "201"])
        with patch.object(installer, "STAGE", self.stage), \
                patch.object(installer, "run", side_effect=run), \
                patch.object(installer, "service_property", side_effect=lambda *args: next(properties)), \
                patch.object(installer.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "restarted unexpectedly"):
                installer.deploy(self.target, "test.service", manifest, "test_python", None, None)
        self.assertEqual((self.target / "file.py").read_text(), "old_code")
        self.assertEqual(await self.storage.match_count(), 1)
        self.assertEqual(await self.storage.rules("watched_words"), ["new_word"])
        self.assertEqual((await self.storage.next_notification())[1], "new_notification")
        self.assertEqual(await self.storage.channels(), [
            ("new_manual", "3", "manual"), ("old_auto", "1", "auto"), ("old_manual", "2", "manual"),
        ])
        self.assertEqual(starts, 2)


if __name__ == "__main__":
    unittest.main()
