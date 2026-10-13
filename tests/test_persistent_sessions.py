import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from waam_pipeline import accounts, oidc, server
from waam_pipeline.session_store import SessionStore, SessionStoreError
from test_accounts import _LocalCase, form
from test_auth import http, sid_from

EMAIL = "worker@example.com"
PASSWORD = "a long test pass phrase"
REPLACEMENT = "a different strong phrase"
CONFIG = {"provider": "local", "public_url": "https://studio.example.com",
          "users": {}, "open_signin": True}
HELPER = ROOT / "tests/session_process_helper.py"


def wait_file(path, process, timeout=20):
    until = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None or time.monotonic() >= until:
            raise AssertionError("Isolated child did not reach its expected checkpoint")
        time.sleep(0.02)


class TestDurableStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.sqlite3"
        self.authority = hashlib.sha256(b"authority-a").hexdigest()
        self.db = SessionStore(self.path, limit=3)
        self.db.set_authority(self.authority)

    def issue(self, now=1000, email=EMAIL):
        sid = secrets.token_urlsafe(32)
        session = {"email": email, "verified": True, "expires": now + 43200,
                   "authority": self.authority, "credential": "b" * 64}
        self.db.create(sid, session, now)
        return sid, session

    def reopen(self):
        db = SessionStore(self.path, limit=3)
        db.set_authority(self.authority)
        return db

    def test_reopen_preserves_identity_and_absolute_expiry_without_raw_tokens(self):
        sid, session = self.issue()
        reopened = self.reopen()
        self.assertEqual(reopened.get(sid, 1001), session)
        self.assertIsNotNone(reopened.get(sid, 44199))
        self.assertIsNone(reopened.get(sid, 44200))
        self.assertIsNone(reopened.get(sid, 999))
        self.assertNotIn(sid.encode(), self.path.read_bytes())
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_logout_and_revoke_all_are_durable_and_visible_to_another_connection(self):
        first, _ = self.issue()
        second, _ = self.issue(1001)
        other, _ = self.issue(1002, "another@example.com")
        reopened = self.reopen()
        self.assertTrue(self.db.drop(first))
        self.assertIsNone(reopened.get(first, 1003))
        self.assertEqual(self.db.drop_for(EMAIL), 1)
        self.assertIsNone(self.reopen().get(second, 1003))
        self.assertIsNotNone(self.reopen().get(other, 1003))

    def test_authority_round_trip_cannot_revive_old_sessions(self):
        sid, _ = self.issue()
        self.db.set_authority(hashlib.sha256(b"authority-b").hexdigest())
        self.db.set_authority(self.authority)
        self.assertIsNone(self.reopen().get(sid, 1001))

    def test_limits_prune_oldest_and_expired_sessions(self):
        issued = [self.issue(1000 + i)[0] for i in range(5)]
        self.assertIsNone(self.db.get(issued[0], 1010))
        self.assertIsNone(self.db.get(issued[1], 1010))
        self.assertIsNotNone(self.db.get(issued[-1], 1010))
        self.issue(50000)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_failed_commit_rolls_back_issuance_and_quota_eviction(self):
        original = [self.issue(1000 + i)[0] for i in range(3)]
        real_connect = sqlite3.connect
        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")
        with patch("waam_pipeline.session_store.sqlite3.connect",
                   side_effect=lambda *a, **kw: real_connect(*a, factory=FailedCommit, **kw)):
            with self.assertRaises(SessionStoreError):
                self.issue(1010)
        for sid in original:
            self.assertIsNotNone(self.reopen().get(sid, 1011))

    def test_missing_or_corrupt_store_never_recreates_or_uses_stale_state(self):
        sid, _ = self.issue()
        self.path.unlink()
        with self.assertRaises(SessionStoreError):
            self.db.get(sid, 1001)
        with self.assertRaises(SessionStoreError):
            self.issue(1002)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"not a SQLite database")
        with self.assertRaises(SessionStoreError):
            self.reopen()
        self.assertEqual(self.path.read_bytes(), b"not a SQLite database")

    def test_locked_database_fails_closed(self):
        sid, _ = self.issue()
        con = sqlite3.connect(self.path)
        try:
            con.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(SessionStoreError):
                self.db.get(sid, 1001)
            with self.assertRaises(SessionStoreError):
                self.db.drop(sid)
        finally:
            con.rollback(); con.close()
        self.assertIsNotNone(self.db.get(sid, 1001))

    def test_invalid_cookie_is_an_authentication_miss(self):
        for sid in ("café", "", "x" * 100000, None, "' OR 1=1 --"):
            self.assertIsNone(self.db.get(sid, 1000))
            self.assertFalse(self.db.drop(sid))

    @unittest.skipIf(os.name == "nt", "POSIX ownership/mode and symlink checks")
    def test_insecure_mode_and_linked_store_are_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(SessionStoreError):
            self.db.clear()
        self.path.chmod(0o600)
        alias = self.path.with_name("alias.sqlite3")
        alias.symlink_to(self.path)
        with self.assertRaises(SessionStoreError):
            SessionStore(alias).set_authority(self.authority)


class TestPersistentHTTP(_LocalCase):
    CONFIG = CONFIG
    TOKENS = {"fallback-fixture-token": "fallback"}

    def setUp(self):
        super().setUp()
        self.write_config(self.CONFIG)
        server._AUTH_STAMP = None
        self.path = self.tmp / "sessions.sqlite3"
        if self.path.exists():
            self.path.unlink()
        oidc.enable_session_persistence(self.path)
        self.addCleanup(oidc.enable_session_persistence, None)
        server._refresh_auth()

    def login(self):
        code, _, headers = self.register(email=EMAIL, password=PASSWORD)
        self.assertEqual(code, 302)
        return sid_from(headers)

    def access(self, sid):
        return http(self.base + "/api/jobs", headers=self.cookie(sid))[0]

    def test_reset_and_delete_from_a_separate_account_store_end_sessions(self):
        sid = self.login()
        self.assertEqual(self.access(sid), 200)
        other = accounts.Store(server.ACCOUNTS.path)
        other.reset_password(EMAIL, REPLACEMENT)
        self.assertEqual(self.access(sid), 401)
        code, _, headers = self.signin(email=EMAIL, password=REPLACEMENT)
        self.assertEqual(code, 302)
        second = sid_from(headers)
        other.delete(EMAIL)
        other.register(EMAIL, PASSWORD)
        self.assertEqual(self.access(second), 401)

    def test_routine_signin_does_not_revoke_other_devices(self):
        first = self.login()
        code, _, headers = self.signin(email=EMAIL, password=PASSWORD)
        self.assertEqual(code, 302)
        self.assertEqual(self.access(first), 200)
        self.assertEqual(self.access(sid_from(headers)), 200)

    def test_policy_is_live_and_authority_changes_permanently_revoke(self):
        sid = self.login()
        self.write_config(dict(CONFIG, admins=[EMAIL]))
        self.assertEqual(http(self.base + "/admin", headers=self.cookie(sid))[0], 200)
        self.write_config(dict(CONFIG, admins=[]))
        self.assertEqual(http(self.base + "/admin", headers=self.cookie(sid))[0], 403)
        self.assertEqual(self.access(sid), 200)
        self.write_config(dict(CONFIG, public_url="https://other.example.com"))
        self.assertEqual(self.access(sid), 401)
        self.write_config(CONFIG)
        self.assertEqual(self.access(sid), 401)

    def test_block_then_unblock_does_not_revive_a_revoked_cookie(self):
        sid = self.login()
        self.write_config(dict(CONFIG, blocked=[EMAIL]))
        self.assertEqual(self.access(sid), 401)
        self.write_config(CONFIG)
        self.assertEqual(self.access(sid), 401)

    def test_storage_failure_does_not_issue_cookie_or_fall_back_to_token(self):
        sid = self.login()
        self.path.write_bytes(b"corrupt")
        headers = {**self.cookie(sid), "X-Waam-Token": "fallback-fixture-token"}
        code, _, reply = http(self.base + "/api/jobs", headers=headers)
        self.assertEqual(code, 503)
        self.assertIn("no-store", reply["Cache-Control"])
        code, _, reply = self.signin(email=EMAIL, password=PASSWORD)
        self.assertEqual(code, 503)
        self.assertNotIn("Set-Cookie", reply)
        self.assertFalse(oidc._SESSIONS)


class TestAccountCredentialRaces(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = accounts.Store(self.root / ".waam-accounts.json")
        self.account = self.store.register(EMAIL, PASSWORD)

    def test_reset_during_password_verification_cannot_mint_current_credentials(self):
        verify = accounts.verify_password
        def raced_verify(account, password):
            valid = verify(account, password)
            accounts.Store(self.store.path).reset_password(EMAIL, REPLACEMENT)
            return valid
        with patch.object(accounts, "verify_password", side_effect=raced_verify):
            with self.assertRaises(accounts.AccountError):
                self.store.authenticate(EMAIL, PASSWORD)
        self.assertTrue(verify(self.store.get(EMAIL), REPLACEMENT))

    def test_cli_reset_cannot_be_overwritten_by_signin_metadata(self):
        write = self.store._write
        child = None
        def paused_write(records):
            nonlocal child
            child = subprocess.Popen([sys.executable, str(HELPER), str(self.root), "reset"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_file(self.root / "reset-ready", child)


            try:
                child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            write(records)
        try:
            with patch.object(self.store, "_write", side_effect=paused_write):
                authenticated = self.store.authenticate(EMAIL, PASSWORD)
            self.assertEqual(child.wait(timeout=10), 0)
            current = self.store.get(EMAIL)
            self.assertTrue(accounts.verify_password(current, REPLACEMENT))
            self.assertFalse(accounts.verify_password(current, PASSWORD))
            self.assertNotEqual(accounts.credential_revision(authenticated),
                                accounts.credential_revision(current))
        finally:
            if child is not None and child.poll() is None:
                child.terminate(); child.wait(timeout=5)

    def test_rehash_preserves_verified_identity(self):
        self.store.mark_verified(EMAIL)
        raw = json.loads(self.store.path.read_text())
        rec = raw["accounts"][0]
        rec["n"] = 2 ** 12
        rec["hash"] = accounts._derive(PASSWORD, bytes.fromhex(rec["salt"]),
                                       rec["n"], rec["r"], rec["p"]).hex()
        self.store.path.write_text(json.dumps(raw))
        self.assertTrue(self.store.authenticate(EMAIL, PASSWORD).verified)
        self.assertTrue(self.store.get(EMAIL).verified)


class TestRealProcessRestart(unittest.TestCase):
    def test_cookie_survives_restart_but_logout_and_guests_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".waam-auth.json").write_text(json.dumps(CONFIG))
            process = None
            def start():
                nonlocal process
                ready = root / "ready.json"
                ready.unlink(missing_ok=True)
                process = subprocess.Popen([sys.executable, str(HELPER), str(root)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_file(ready, process)
                return f"http://127.0.0.1:{json.loads(ready.read_text())['port']}"
            def stop():
                process.terminate(); process.wait(timeout=10)
            try:
                base = start()
                code, _, headers = http(base + "/auth/register", method="POST",
                    data=form(email=EMAIL, password=PASSWORD),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(code, 302)
                sid = sid_from(headers)
                cookie = {"Cookie": f"waam_sid={sid}"}
                for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Max-Age=43200"):
                    self.assertIn(flag, headers["Set-Cookie"])
                dbpath = root / ".waam-sessions.sqlite3"
                with closing(sqlite3.connect(dbpath)) as con:
                    expires = con.execute("SELECT expires FROM sessions").fetchone()[0]
                self.assertEqual(http(base + "/api/jobs", headers=cookie)[0], 200)
                stop(); base = start()
                self.assertEqual(http(base + "/api/jobs", headers=cookie)[0], 200)
                with closing(sqlite3.connect(dbpath)) as con:
                    self.assertEqual(con.execute("SELECT expires FROM sessions").fetchone()[0], expires)
                self.assertEqual(http(base + "/auth/logout", headers=cookie)[0], 302)
                _, _, guest = http(base + "/auth/guest", method="POST")
                guest_cookie = {"Cookie": guest["Set-Cookie"].split(";", 1)[0]}
                self.assertEqual(http(base + "/api/jobs", headers=guest_cookie)[0], 403)
                stop(); base = start()
                self.assertEqual(http(base + "/api/jobs", headers=cookie)[0], 401)
                self.assertEqual(http(base + "/api/jobs", headers=guest_cookie)[0], 401)
                self.assertNotIn(sid.encode(), dbpath.read_bytes())
                account = accounts.Store(root / ".waam-accounts.json").get(EMAIL)
                self.assertNotIn(account.hash.encode(), dbpath.read_bytes())
            finally:
                if process is not None and process.poll() is None:
                    stop()


if __name__ == "__main__":
    unittest.main()
