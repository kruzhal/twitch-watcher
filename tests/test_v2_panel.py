"""Exercise the real panel handlers, cookies and CSRF against a temporary DB.

The loopback test transport is plain HTTP with server.secure=True only to test
handler decisions; public HTTPS is verified separately after deployment.
"""
import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.admin_store import database, import_rows
from app.schema import migrate_sync

spec = importlib.util.spec_from_file_location("v2_panel_test", Path(__file__).resolve().parents[1] / "dashboard" / "server.py")
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "panel.sqlite3"
        with database(self.path) as db:
            migrate_sync(db)
            import_rows(db, [("first", 1), ("second", 2)], "merge")
            db.execute("INSERT INTO channels VALUES ('manual','3','manual')")
            db.execute("INSERT INTO watched_words VALUES ('hello')")
        self.patches = [
            patch.object(panel, "DB_PATH", self.path),
            patch.object(panel, "AUTH_FILE", Path(self.tmp.name) / "auth.json"),
            patch.object(panel, "SESSION_SECRET", b"isolated-test-secret-not-used-on-real-server"),
        ]
        for item in self.patches:
            item.start()
        self.cookie = panel._make_cookie()
        self.server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.DashboardHandler)
        self.server.secure = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def request(self, path, method="GET", payload=None, *, auth=True, csrf=True, origin=True, raw=None, extra_headers=None):
        headers = {"Host": panel.IP}
        if auth:
            headers["Cookie"] = f"{panel.COOKIE_NAME}={self.cookie}"
        if csrf:
            headers["X-CSRF-Token"] = panel._csrf_for(self.cookie)
        if origin:
            headers["Origin"] = f"https://{panel.IP}"
        body = json.dumps(payload).encode() if payload is not None else raw
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            reject_before_body = method == "POST" and (
                not auth or not csrf or not origin or int(headers.get("Content-Length", 0)) > 3_000_000
            )
            if reject_before_body:
                # Send headers first: the handler must reject without waiting
                # for the upload. Avoid Windows RST from closing unread bytes.
                headers.setdefault("Content-Length", str(len(body or b"")))
                connection.request(method, path, headers=headers)
            else:
                connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_unauthenticated_upload_is_rejected_before_bad_json_is_read(self):
        status, _ = self.request("/api/catalog/import", "POST", auth=False, raw=b"not-json")
        self.assertEqual(status, 401)

    def test_missing_csrf_or_origin_blocks_mutations(self):
        for options in ({"csrf": False}, {"origin": False}):
            status, _ = self.request("/api/catalog", "POST", {"action": "update", "login": "first", "priority": 10, "enabled": False}, **options)
            self.assertEqual(status, 403)
        with database(self.path) as db:
            self.assertEqual(tuple(db.execute("SELECT priority,enabled FROM channel_catalog WHERE login='first'").fetchone()), (1, 1))

    def test_catalog_update_and_filtered_query(self):
        status, _ = self.request("/api/catalog", "POST", {"action": "update", "login": "first", "priority": 9, "enabled": False})
        self.assertEqual(status, 200)
        status, body = self.request("/api/catalog?enabled=no&search=FIRST")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"], [{"login": "first", "priority": 9, "enabled": 0}])

    def test_csv_preview_is_read_only_and_replacement_requires_confirmation(self):
        payload = {"csv": "rank,login\n1,new\n", "mode": "replace"}
        status, body = self.request("/api/catalog/preview", "POST", payload)
        self.assertEqual((status, body["data"]["incoming"], body["data"]["removed"]), (200, 1, 2))
        self.assertEqual(self.request("/api/catalog")[1]["data"]["total"], 2)
        self.assertEqual(self.request("/api/catalog/import", "POST", payload)[0], 400)
        self.assertEqual(self.request("/api/catalog/import", "POST", {**payload, "confirm": True})[0], 200)
        with database(self.path) as db:
            self.assertEqual(db.execute("SELECT login FROM channel_catalog").fetchone()[0], "new")
            self.assertEqual(db.execute("SELECT login FROM channels").fetchone()[0], "manual")
            self.assertEqual(db.execute("SELECT phrase FROM watched_words").fetchone()[0], "hello")

    def test_invalid_csv_mode_or_priority_does_not_change_catalog(self):
        cases = [
            ("/api/catalog/import", {"csv": "login\nwrong login", "mode": "merge"}),
            ("/api/catalog/import", {"csv": "login\nvalid", "mode": []}),
            ("/api/catalog", {"action": "update", "login": "first", "priority": True, "enabled": True}),
        ]
        for path, payload in cases:
            self.assertEqual(self.request(path, "POST", payload)[0], 400)
        self.assertEqual(self.request("/api/catalog")[1]["data"]["total"], 2)

    def test_filters_and_cooldown_can_be_managed(self):
        self.assertEqual(self.request("/api/filters", "POST", {"action": "add", "phrase": "Ignore <script>"})[0], 200)
        self.assertEqual(self.request("/api/filters", "POST", {"action": "cooldown", "seconds": 0})[0], 200)
        data = self.request("/api/filters")[1]["data"]
        self.assertEqual(data, {"excluded_words": ["ignore <script>"], "repeat_cooldown_seconds": 0})
        self.assertEqual(self.request("/api/filters", "POST", {"action": []})[0], 400)

    def test_body_limit_and_content_type_are_enforced(self):
        self.assertEqual(self.request("/api/catalog/import", "POST", raw=b"{}", extra_headers={"Content-Length": "3000001"})[0], 400)
        self.assertEqual(self.request("/api/filters", "POST", {"action": "cooldown", "seconds": 10}, extra_headers={"Content-Type": "text/plain"})[0], 400)

    def test_read_state_is_not_healthy_without_a_fresh_bot_heartbeat(self):
        def run(*args, **kwargs):
            if "is-active" in args:
                return 0, "active"
            return 0, "MainPID=7\nNRestarts=0\nActiveEnterTimestamp=now"
        with patch.object(panel, "_run", side_effect=run):
            status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["health"]["state"], "stale")
        self.assertEqual(body["data"]["catalog_channels"], 2)
        self.assertEqual(body["data"]["active_since"], "now")


if __name__ == "__main__":
    unittest.main()
