import base64
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests._support import IsolatedServerCase
from waam_pipeline import accounts, oidc, run_log_store as store, server

OWNER = "owner@example.invalid"
TEAMMATE = "teammate@example.invalid"
STRANGER = "stranger@example.invalid"
ACCESS = "/api/run-logs/excel-access"
FEED = "/api/run-logs/excel/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestExcelRoutes(IsolatedServerCase):
    STUB_AUTH = False
    TOKEN_GATE = True

    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("WAAM_RUN_LOG_PG_DSN", None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("SAVES_DIR", self.root / "saves"),
                            ("EXCEL_CREDENTIALS", self.root / ".waam-run-log-excel.json"),
                            ("ACCOUNTS", accounts.Store(self.root / "accounts.json")),
                            ("_refresh_tokens", lambda: {"capability-only": "owner"})):
            change = patch.object(server, name, value)
            change.start()
            self.addCleanup(change.stop)
        oidc.forget_everything()
        self.addCleanup(oidc.forget_everything)
        self.config = {"provider": "local", "public_url": "https://waam.example.invalid",
                       "users": {OWNER: {"name": "owner"}, TEAMMATE: {"name": "teammate"},
                                 STRANGER: {"name": "stranger"}}, "open_signin": False}
        self.write_config()
        for email in (OWNER, TEAMMATE, STRANGER):
            server.ACCOUNTS.register(email, "A temporary test account password")
            server.ACCOUNTS.mark_verified(email)
        self.opener = urllib.request.build_opener(_NoRedirect)

    def write_config(self, **changes):
        self.config.update(changes)
        self.iso.auth_config.write_text(json.dumps(self.config), encoding="utf-8")
        server._AUTH_STAMP = None

    def session(self, email=OWNER, verified=True):
        cfg = server._refresh_auth()
        account = server.ACCOUNTS.get(email) if cfg.local else None
        sid = server._new_signin_session(cfg, email, verified, account)
        return {"Cookie": f"{server.SESSION_COOKIE}={sid}"}

    def request(self, route, *, obj=None, headers=None):
        data = json.dumps(obj).encode("utf-8") if obj is not None else None
        request = urllib.request.Request(self.base + route, data=data,
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                body = json.loads(body)
            return response.code, body, response.headers

    def issue(self, email=OWNER, headers=None):
        code, result, response_headers = self.request(ACCESS, obj={"action": "create"},
                                                       headers=headers or self.session(email))
        self.assertEqual(code, 200, result)
        self.assertIn("no-store", response_headers["Cache-Control"])
        self.assertNotIn("Set-Cookie", response_headers)
        return result

    @staticmethod
    def basic(credential):
        value = f"{credential['username']}:{credential['password']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(value).decode("ascii")}

    def feed(self, credential=None, dataset="runs", headers=None):
        return self.request(FEED + dataset + ".csv",
                            headers=headers or (self.basic(credential) if credential else None))

    @staticmethod
    def rows(body):
        return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))

    def test_browser_issuance_is_one_time_uncached_and_personal(self):
        credential = self.issue()
        self.assertEqual(credential["username"], OWNER)
        self.assertEqual(set(credential["feeds"]), {"runs", "layers", "cooling", "temperatures"})
        code, status, _ = self.request(ACCESS, headers=self.session())
        self.assertEqual(code, 200)
        self.assertTrue(status["enabled"])
        self.assertNotIn("password", status)
        code, other, _ = self.request(ACCESS, headers=self.session(TEAMMATE))
        self.assertEqual(code, 200)
        self.assertFalse(other["enabled"])
        self.assertNotIn(credential["password"], server.EXCEL_CREDENTIALS.read_text(encoding="utf-8"))

    def test_missing_wrong_cookie_and_capability_auth_get_basic_challenge(self):
        credential = self.issue()
        wrong = {**credential, "password": "A" * 43}
        for headers in ({}, self.session(), {"X-Waam-Token": "capability-only"},
                        self.basic(wrong), {"Authorization": "Basic malformed"}):
            code, body, response_headers = self.feed(headers=headers)
            self.assertEqual(code, 401, body)
            self.assertTrue(response_headers["WWW-Authenticate"].startswith("Basic "))
            self.assertNotIn("Location", response_headers)
            self.assertNotIn("Set-Cookie", response_headers)
        self.assertEqual(self.request(FEED + "runs.csv?k=capability-only")[0], 401)

    def test_capability_and_basic_never_authorize_creation_or_write_routes(self):
        credential = self.issue()
        for headers in (self.basic(credential), {"X-Waam-Token": "capability-only"}):
            for route, body in ((ACCESS, {"action": "create"}),
                                (ACCESS, {"action": "revoke"}),
                                ("/api/run-logs/start", {"name": "UNAUTHORIZED"}),
                                (FEED + "runs.csv", {})):
                code, _, _ = self.request(route, obj=body, headers=headers)
                self.assertIn(code, (401, 403, 404), route)
        self.assertEqual(store.list_runs(server.SAVES_DIR, OWNER), [])
        self.assertEqual(self.feed(credential)[0], 200)

    def test_cross_origin_rotation_and_wrong_workspace_are_refused(self):
        credential = self.issue()
        headers = {**self.session(), "Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request(ACCESS, obj={"action": "create"}, headers=headers)[0], 403)
        code, _, _ = self.request(ACCESS + "?ws=teammate", obj={"action": "revoke"}, headers=self.session())
        self.assertEqual(code, 409)
        self.assertEqual(self.feed(credential)[0], 200)

    def test_blocked_account_and_removed_approval_deny_existing_credential(self):
        credential = self.issue()
        self.write_config(blocked=[OWNER])
        self.assertEqual(self.feed(credential)[0], 401)
        self.write_config(blocked=[], users={}, open_signin=True, approval_required=True, approved=[OWNER])
        self.assertEqual(self.feed(credential)[0], 200)
        self.write_config(approved=[])
        self.assertEqual(self.feed(credential)[0], 401)

    def test_password_reset_and_deleted_account_invalidate_existing_credential(self):
        credential = self.issue()
        server.ACCOUNTS.reset_password(OWNER, "Another temporary test account password")
        self.assertEqual(self.feed(credential)[0], 401)
        code, status, _ = self.request(ACCESS, headers=self.session())
        self.assertEqual(code, 200)
        self.assertFalse(status["enabled"])
        new = self.issue()
        self.assertEqual(self.feed(new)[0], 200)
        server.ACCOUNTS.delete(OWNER)
        self.assertEqual(self.feed(new)[0], 401)

    def test_auth_authority_change_and_non_https_configuration_deny(self):
        credential = self.issue()
        self.write_config(public_url="https://new-waam.example.invalid")
        self.assertEqual(self.feed(credential)[0], 401)
        current = self.issue()
        self.write_config(public_url="http://127.0.0.1")
        self.assertEqual(self.feed(current)[0], 401)
        self.assertEqual(self.request(ACCESS, obj={"action": "create"}, headers=self.session())[0], 403)

    def test_unverified_sso_session_cannot_issue_even_when_rostered(self):
        self.write_config(provider="google", client_id="test-client", client_secret="test-secret")
        headers = self.session(verified=False)
        code, _, _ = self.request(ACCESS, obj={"action": "create"}, headers=headers)
        self.assertEqual(code, 403)
        self.assertFalse(server.EXCEL_CREDENTIALS.exists())
        credential = self.issue(headers=self.session(verified=True))
        self.assertEqual(self.feed(credential)[0], 200)

    def test_rotation_and_revocation_keep_all_historical_tokens_disabled(self):
        old = self.issue()
        current = self.issue()
        self.assertEqual(self.feed(old)[0], 401)
        self.assertEqual(self.feed(current)[0], 200)
        self.assertEqual(self.request(ACCESS, obj={"action": "revoke"}, headers=self.session())[0], 200)
        self.assertEqual(self.feed(current)[0], 401)
        replacement = self.issue()
        self.assertEqual(self.feed(replacement)[0], 200)
        self.assertEqual(self.feed(old)[0], 401)
        self.assertEqual(self.feed(current)[0], 401)

    def test_csv_acl_units_formulas_and_sharing_revocation(self):
        detail = store.create_run(server.SAVES_DIR, "owner", OWNER, name="=1+1",
                                  setup={"specimen_id": "+command", "material": "@material"},
                                  collaborators=[TEAMMATE])
        run_id = detail["record"]["run"]
        detail = store.update_run(server.SAVES_DIR, OWNER, run_id, revision=detail["revision"],
                                  action="layer-row", data={"layer": 0, "pass": 1, "unit": "F",
                                  "pre_weld": 212, "readings": {"30": 392}, "note": "\t=command",
                                  "current_a": 130, "voltage_v": 21})
        detail = store.update_run(server.SAVES_DIR, OWNER, run_id, revision=detail["revision"],
                                  action="temperature", data={"value": -10, "unit": "C", "stage": "other",
                                                              "note": "-command"})
        store.create_run(server.SAVES_DIR, "stranger", STRANGER, name="PRIVATE")
        credential = self.issue(TEAMMATE)
        code, body, headers = self.feed(credential)
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertNotIn("Set-Cookie", headers)
        rows = self.rows(body)
        self.assertEqual([row["run"] for row in rows], [run_id])
        self.assertEqual(rows[0]["name"], "'=1+1")
        self.assertEqual(rows[0]["setup_specimen_id"], "'+command")
        self.assertEqual(rows[0]["setup_material"], "'@material")
        layer = self.rows(self.feed(credential, "layers")[1])[0]
        self.assertEqual(layer["note"], "'\t=command")
        self.assertEqual(float(layer["pre_weld_value_c"]), 100)
        self.assertEqual(float(layer["current_a"]), 130)
        self.assertEqual(float(self.rows(self.feed(credential, "cooling")[1])[0]["value_c"]), 200)
        temperature = self.rows(self.feed(credential, "temperatures")[1])[0]
        self.assertEqual(float(temperature["value"]), -10)
        self.assertEqual(temperature["note"], "'-command")
        store.update_run(server.SAVES_DIR, OWNER, run_id, revision=detail["revision"],
                         action="setup", data={"collaborators": []})
        for dataset in ("runs", "layers", "cooling", "temperatures"):
            code, body, _ = self.feed(credential, dataset)
            self.assertEqual(code, 200)
            self.assertEqual(self.rows(body), [])

    def test_feed_does_not_inherit_web_admin_privileges(self):
        self.write_config(admins=[OWNER])
        store.create_run(server.SAVES_DIR, "stranger", STRANGER, name="PRIVATE")
        credential = self.issue()
        self.assertEqual(self.rows(self.feed(credential)[1]), [])

    def test_corrupt_credential_store_and_catalog_failure_are_sanitized(self):
        credential = self.issue()
        with patch.object(server.run_log_routes, "reporting_csv", side_effect=OSError("sensitive sql info")):
            code, body, _ = self.feed(credential)
        self.assertEqual(code, 503)
        self.assertNotIn(b"sensitive", body)
        server.EXCEL_CREDENTIALS.write_text("private malformed store", encoding="utf-8")
        code, body, _ = self.feed(credential)
        self.assertEqual(code, 503)
        self.assertNotIn(b"private malformed", body)
        if server.ACCESS_LOG.exists():
            self.assertNotIn(credential["password"], server.ACCESS_LOG.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
