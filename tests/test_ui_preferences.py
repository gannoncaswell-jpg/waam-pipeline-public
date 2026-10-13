import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import oidc, server, stl_library, ui_preferences as prefs
from tests.test_auth import _AuthServerCase, GOOGLE_CFG, http


ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CHANGED = dict(prefs.DEFAULTS, theme="drafting", density="compact", reduce_motion=True,
               default_frame="part", default_camera="top", path_only=True)


class TestPreferencesStore(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "preferences"
        self.store = prefs.Store(self.directory)

    def path(self, email=ALICE):
        return self.directory / (prefs.owner_id(email) + ".json")

    def test_defaults_do_not_create_files_and_are_independent(self):
        found = self.store.get(ALICE)
        self.assertEqual(found, prefs.DEFAULTS)
        found["theme"] = "operator"
        self.assertEqual(self.store.get(ALICE), prefs.DEFAULTS)
        self.assertFalse(self.directory.exists())

    def test_restart_case_normalization_and_identity_match_whoami(self):
        self.assertEqual(prefs.owner_id("  ALICE@example.invalid "), stl_library.owner_id(ALICE))
        self.assertEqual(self.store.save(ALICE, CHANGED), CHANGED)
        self.assertEqual(prefs.Store(self.directory).get(" ALICE@example.invalid "), CHANGED)
        self.assertEqual(self.store.get(BOB), prefs.DEFAULTS)
        self.assertNotIn(ALICE, self.path().read_text(encoding="utf-8"))
        self.assertFalse(list(self.directory.glob("*.tmp")))
        if os.name != "nt":
            self.assertEqual(self.path().stat().st_mode & 0o777, 0o600)

    def test_unknown_machine_fields_missing_values_and_coercions_are_rejected(self):
        invalid = [[], {}, {"theme": "graphite"}, dict(CHANGED, ASF=12),
                   dict(CHANGED, clearance_margin=0), dict(CHANGED, theme="current"),
                   dict(CHANGED, density="tiny"), dict(CHANGED, default_frame="robot"),
                   dict(CHANGED, default_camera="front"), dict(CHANGED, path_only=1),
                   dict(CHANGED, reduce_motion="false"), dict(CHANGED, theme=["graphite"])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(prefs.InvalidPreferences):
                self.store.save(ALICE, value)
        self.assertFalse(self.directory.exists())

    def test_duplicate_fields_malformed_json_and_oversized_documents_are_rejected(self):
        valid = json.dumps(CHANGED).encode()
        for body in (b"[]", b"null", b"{", b"\xff", b" " * (prefs.MAX_BYTES + 1),
                     valid[:-1] + b', "theme":"operator"}'):
            with self.subTest(body=body[:80]), self.assertRaises(prefs.InvalidPreferences):
                prefs.decode(body)
        self.assertEqual(prefs.decode(valid), CHANGED)

    def test_corrupt_or_misowned_records_never_become_defaults_or_get_overwritten(self):
        self.store.save(ALICE, CHANGED)
        for data in ("{", json.dumps({"version": 1, "owner_id": prefs.owner_id(BOB),
                                     "preferences": CHANGED}),
                     json.dumps({"version": 1, "owner_id": prefs.owner_id(ALICE),
                                 "preferences": dict(CHANGED, arc_file=12)})):
            self.path().write_text(data, encoding="utf-8")
            with self.assertRaises(prefs.PreferencesUnavailable):
                self.store.get(ALICE)
            with self.assertRaises(prefs.PreferencesUnavailable):
                self.store.save(ALICE, prefs.DEFAULTS)
            self.assertEqual(self.path().read_text(encoding="utf-8"), data)

    def test_failed_replace_and_file_sync_preserve_previous_document(self):
        self.store.save(ALICE, CHANGED)
        before = self.path().read_bytes()
        for target in ("os.replace", "os.fsync"):
            with self.subTest(target=target), patch("waam_pipeline.ui_preferences." + target,
                                                   side_effect=OSError("private/path/details")):
                with self.assertRaises(prefs.PreferencesUnavailable) as error:
                    self.store.save(ALICE, prefs.DEFAULTS)
                self.assertNotIn("private", str(error.exception))
            self.assertEqual(self.path().read_bytes(), before)
            self.assertFalse(list(self.directory.glob("*.tmp")))

    @unittest.skipIf(os.name == "nt", "Directory fsync is a POSIX durability boundary")
    def test_failed_directory_sync_is_not_acknowledged_as_saved(self):
        self.store.save(ALICE, CHANGED)
        with patch("waam_pipeline.ui_preferences.os.fsync", side_effect=[None, OSError("sync")]):
            with self.assertRaises(prefs.PreferencesUnavailable):
                self.store.save(ALICE, prefs.DEFAULTS)

        self.assertEqual(self.store.get(ALICE), prefs.DEFAULTS)

    def test_parallel_accounts_keep_complete_independent_documents(self):
        identities = [f"person-{i}@example.invalid" for i in range(12)]
        def save(index):
            expected = CHANGED if index % 2 else prefs.DEFAULTS
            prefs.Store(self.directory).save(identities[index], expected)
            return expected
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            expected = list(pool.map(save, range(len(identities))))
        for email, value in zip(identities, expected):
            self.assertEqual(self.store.get(email), value)
        self.assertEqual(len(list(self.directory.glob("*.json"))), len(identities))

    def test_concurrent_full_saves_never_publish_a_partial_record(self):
        self.store.save(ALICE, prefs.DEFAULTS)
        def replace(index):
            store = prefs.Store(self.directory)
            store.save(ALICE, CHANGED if index % 2 else prefs.DEFAULTS)
            self.assertIn(store.get(ALICE), (CHANGED, prefs.DEFAULTS))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(replace, range(40)))
        self.assertIn(self.store.get(ALICE), (CHANGED, prefs.DEFAULTS))
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_symlinked_record_is_refused_without_touching_target(self):
        self.directory.mkdir()
        other = self.directory.parent / "other.json"
        other.write_text("keep", encoding="utf-8")
        try:
            self.path().symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        with self.assertRaises(prefs.PreferencesUnavailable):
            self.store.get(ALICE)
        with self.assertRaises(prefs.PreferencesUnavailable):
            self.store.save(ALICE, CHANGED)
        self.assertEqual(other.read_text(encoding="utf-8"), "keep")


class TestPreferencesHttp(_AuthServerCase):
    CONFIG = dict(GOOGLE_CFG, users={ALICE: {"name": "team"}, BOB: {"name": "team"}},
                  allow_domains=[])
    TOKENS = {"test-preferences-token": "team"}

    def setUp(self):
        self.write_config(self.CONFIG)
        oidc.forget_everything()
        self.alice = self.cookie(self.sign_in_ok(email=ALICE, email_verified=True))
        self.bob = self.cookie(self.sign_in_ok(email=BOB, email_verified=True))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = prefs.Store(Path(temporary.name) / "preferences")
        changed = patch.object(server, "UI_PREFERENCES", self.store)
        changed.start()
        self.addCleanup(changed.stop)

    def call(self, *, cookie=None, method="GET", value=None, body=None,
             owner=ALICE, query=None, headers=None):
        params = {} if owner is None else {"owner": prefs.owner_id(owner)}
        params.update(query or {})
        request_headers = dict(self.alice if cookie is None else cookie)
        request_headers.update(headers or {})
        if value is not None:
            body = json.dumps(value).encode()
        if method == "POST":
            request_headers.setdefault("Content-Type", "application/json")
        code, response, response_headers = http(self.base + "/api/preferences?" + urlencode(params),
            method=method, data=body, headers=request_headers)
        self.assertIn("no-store", response_headers.get("Cache-Control", ""))
        if response_headers.get("Content-Type", "").startswith("application/json"):
            response = json.loads(response)
        return code, response

    def test_full_get_post_contract_and_shared_workspace_isolation(self):
        for cookie, email in ((self.alice, ALICE), (self.bob, BOB)):
            code, response, _ = http(self.base + "/api/whoami", headers=cookie)
            who = json.loads(response)
            self.assertEqual(who["workspace"], "team")
            code, data = self.call(cookie=cookie, owner=email)
            self.assertEqual(code, 200, data)
            self.assertEqual(data, {"ok": True, "owner_id": who["library_owner"],
                                    "preferences": prefs.DEFAULTS})
        code, result = self.call(method="POST", value=CHANGED)
        self.assertEqual(code, 200, result)
        self.assertEqual(result, {"ok": True, "owner_id": prefs.owner_id(ALICE),
                                  "preferences": CHANGED})
        self.assertEqual(self.call()[1]["preferences"], CHANGED)
        self.assertEqual(self.call(cookie=self.bob, owner=BOB)[1]["preferences"], prefs.DEFAULTS)

    def test_account_switch_rejects_old_get_and_post_even_with_shared_workspace(self):
        for method in ("GET", "POST"):
            code, result = self.call(cookie=self.bob, method=method,
                                     value=CHANGED if method == "POST" else None)
            self.assertEqual(code, 409, result)
            self.assertFalse(result["ok"])
        self.assertFalse(self.store.directory.exists())
        self.assertEqual(self.call(owner=None)[0], 200)
        self.assertEqual(self.call(owner=None, method="POST", value=CHANGED)[0], 409)

    def test_local_account_session_uses_the_same_personal_store(self):
        self.write_config({"provider": "local", "public_url": self.base,
                           "users": {ALICE: {"name": "team"}}, "verify_email": False})
        account = server.ACCOUNTS.get(ALICE) or server.ACCOUNTS.register(ALICE, "preferences-test-password")
        session = server._new_signin_session(server._refresh_auth(), ALICE, False, account)
        cookie = self.cookie(session)
        code, result = self.call(cookie=cookie, method="POST", value=CHANGED)
        self.assertEqual(code, 200, result)
        self.assertEqual(self.call(cookie=cookie)[1]["preferences"], CHANGED)

    def test_query_or_body_identity_cannot_select_another_account(self):
        code, result = self.call(method="POST", value=dict(CHANGED, email=BOB))
        self.assertEqual(code, 400, result)
        code, result = self.call(method="POST", value=CHANGED, query={"email": BOB, "user_id": BOB})
        self.assertEqual(code, 200, result)
        self.assertEqual(self.store.get(ALICE), CHANGED)
        self.assertEqual(self.store.get(BOB), prefs.DEFAULTS)

    def test_guest_token_local_and_signed_out_callers_cannot_use_account_storage(self):
        guest = server._new_guest()
        callers = ({"Cookie": f"{server.GUEST_COOKIE}={guest}"},
                   {"X-Waam-Token": "test-preferences-token"}, {})
        for cookie in callers:
            for method in ("GET", "POST"):
                code, result = self.call(cookie=cookie, method=method,
                    value=CHANGED if method == "POST" else None)
                self.assertIn(code, (401, 403), result)
        with patch.object(server, "_TOKENS", {}), patch.object(server, "_refresh_auth", return_value=None):
            self.assertEqual(self.call(cookie={})[0], 403)
            self.assertEqual(self.call(cookie={}, method="POST", value=CHANGED)[0], 403)
        self.assertFalse(self.store.directory.exists())

    def test_existing_cross_site_gate_and_live_revocation_apply(self):
        code, result = self.call(method="POST", value=CHANGED,
                                 headers={"Origin": "https://other.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403, result)
        self.write_config(dict(self.CONFIG, blocked=[ALICE]))
        self.assertEqual(self.call()[0], 401)
        self.assertFalse(self.store.directory.exists())

    def test_request_bounds_content_type_and_strict_values(self):
        invalid = [(b"{}", {}, 400), (b"[]", {}, 400), (b"{", {}, 400),
                   (b"\xff", {}, 400), (b"x" * (prefs.MAX_BYTES + 1), {}, 413),
                   (json.dumps(CHANGED).encode(), {"Content-Type": "text/plain"}, 415),
                   (json.dumps(dict(CHANGED, path_only=0)).encode(), {}, 400),
                   (json.dumps(dict(CHANGED, arc_file=12)).encode(), {}, 400)]
        for body, headers, expected in invalid:
            with self.subTest(expected=expected, body=body[:50]):
                code, result = self.call(method="POST", body=body, headers=headers)
                self.assertEqual(code, expected, result)
                self.assertFalse(result["ok"])
        self.assertFalse(self.store.directory.exists())

    def test_failed_persistence_returns_error_and_never_false_saved(self):
        self.store.save(ALICE, CHANGED)
        with patch("waam_pipeline.ui_preferences.os.replace", side_effect=OSError("private server path")):
            code, result = self.call(method="POST", value=prefs.DEFAULTS)
        self.assertEqual(code, 503, result)
        self.assertFalse(result["ok"])
        self.assertNotIn("private server path", str(result))
        self.assertEqual(self.call()[1]["preferences"], CHANGED)
        path = self.store.directory / (prefs.owner_id(ALICE) + ".json")
        path.write_text("broken", encoding="utf-8")
        self.assertEqual(self.call()[0], 503)
        self.assertEqual(self.call(method="POST", value=prefs.DEFAULTS)[0], 503)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")


if __name__ == "__main__":
    unittest.main()
