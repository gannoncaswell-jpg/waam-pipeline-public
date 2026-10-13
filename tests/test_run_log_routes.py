import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests._support import IsolatedServerCase
from waam_pipeline import runs, server


class TestRunLogRoutes(IsolatedServerCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("WAAM_RUN_LOG_PG_DSN", None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.actor = "owner@example.invalid"
        self.workspace = "owner"
        self.guest = False
        for target, name, value in ((server, "SAVES_DIR", Path(temp.name) / "saves"),
                                    (server, "_auth", self.auth),
                                    (server, "_git_commit", lambda: "a" * 40)):
            change = patch.object(target, name, value)
            change.start()
            self.addCleanup(change.stop)

    def auth(self, handler, _query):
        handler.waam_who = {"email": self.actor, "guest": self.guest, "outcome": "ok-sso"}
        return self.workspace, None

    def request(self, route, *, query=None, obj=None, raw=None, headers=None):
        body = json.dumps(obj).encode() if obj is not None else raw
        request = urllib.request.Request(
            self.base + route + ("?" + urlencode(query) if query else ""), data=body,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                content = json.loads(content)
            return response.code, content, response.headers

    def start(self, **extra):
        code, result, _ = self.request("/api/run-logs/start", obj={"name": "TEST", **extra})
        self.assertEqual(code, 200, result)
        return result

    def update(self, detail, action, data, **extra):
        return self.request("/api/run-logs/update", query={"run": detail["record"]["run"]},
                            obj={"revision": detail["revision"], "action": action, "data": data}, **extra)

    def test_manual_run_round_trip_shared_entry_conflict_and_finish(self):
        detail = self.start(setup={"specimen_id": "coupon-01", "travel_mm_min": 325},
                            collaborators=["teammate@example.invalid"])
        run_id = detail["record"]["run"]
        self.assertTrue(detail["can_manage"])
        self.actor, self.workspace = "teammate@example.invalid", "teammate"
        code, listing, _ = self.request("/api/run-logs")
        self.assertEqual(code, 200)
        self.assertEqual([row["run"] for row in listing["runs"]], [run_id])
        code, shared, _ = self.request("/api/run-logs/detail", query={"run": run_id})
        self.assertEqual(code, 200)
        self.assertFalse(shared["can_manage"])
        row = {"layer": 0, "pass": 1, "unit": "C", "pre_weld": 22.5,
               "readings": {"0": 450, "30": 310}, "height_mm": 1.8, "width_mm": 6,
               "height_source": "manual", "note": "stable arc"}
        code, changed, _ = self.update(shared, "layer-row", row)
        self.assertEqual(code, 200, changed)
        self.assertEqual(changed["record"]["layer_log"][0]["pass_base"], 1)
        self.assertEqual(self.update(shared, "note", {"text": "stale"})[0], 409)
        self.assertEqual(self.update(changed, "finish", {"outcome": "not-run"})[0], 404)
        self.actor, self.workspace = "owner@example.invalid", "owner"
        code, owner, _ = self.request("/api/run-logs/detail", query={"run": run_id})
        self.assertEqual(self.update(owner, "finish", {"outcome": "completed"})[0], 400)
        code, finished, _ = self.update(owner, "finish", {
            "outcome": "completed", "arcmon_waiver": True, "waiver_reason": "Lab monitor unavailable"})
        self.assertEqual(code, 200, finished)
        self.assertEqual(finished["record"]["status"], "finished")
        self.assertIsNone(runs.active(server.SAVES_DIR, "owner"))
        self.assertNotEqual(self.update(finished, "note", {"text": "late"})[0], 200)

    def test_accounts_only_and_private_runs(self):
        detail = self.start()
        self.actor, self.workspace = "stranger@example.invalid", "stranger"
        self.assertEqual(self.request("/api/run-logs")[1]["runs"], [])
        self.assertEqual(self.request("/api/run-logs/detail", query={"run": detail["record"]["run"]})[0], 404)
        for actor, guest in ((None, False), (None, True)):
            self.actor, self.guest = actor, guest
            for route in ("/run-logs", "/api/run-logs", "/api/run-logs/detail"):
                self.assertEqual(self.request(route)[0], 403)
            self.assertEqual(self.request("/api/run-logs/start", obj={"name": "NO"})[0], 403)

    def test_cross_site_write_refused_and_invalid_setup_creates_nothing(self):
        code, _, _ = self.request("/api/run-logs/start", obj={"name": "TEST"},
                                  headers={"Origin": "https://unrelated.invalid"})
        self.assertEqual(code, 403)
        code, _, _ = self.request("/api/run-logs/start", obj={"name": "TEST", "setup": {"ctwd_mm": -1}})
        self.assertEqual(code, 400)
        self.assertIsNone(runs.active(server.SAVES_DIR, "owner"))

    def test_upload_and_csv_are_bound_to_accessible_run(self):
        detail = self.start(setup={"specimen_id": "=1+1"})
        query = {"run": detail["record"]["run"], "revision": detail["revision"],
                 "kind": "attachment", "name": "observation.txt"}
        code, added, _ = self.request("/api/run-logs/add", query=query, raw=b"Lab observation")
        self.assertEqual(code, 200, added)
        self.assertEqual(added["record"]["files"][0]["kind"], "ATTACH")
        code, body, headers = self.request("/api/run-logs/export", query={"run": detail["record"]["run"]})
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["setup_specimen_id"], "'=1+1")
        self.assertEqual(rows[0]["run"], detail["record"]["run"])

    def test_page_is_discoverable_and_uncached(self):
        code, page, headers = self.request("/run-logs")
        self.assertEqual(code, 200)
        self.assertIn(b"Run logs", page)
        self.assertIn("no-store", headers["Cache-Control"])
