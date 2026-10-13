import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import approve, oidc, server


SECRET = "abcdefghijklmnop"
BASE_CFG = {
    "_comment": "workbench sign-in. holds the mail app password: never commit.",
    "_open_signin": "false closes the door to everyone off allow_domains.",
    "provider": "local",
    "public_url": "https://cell.example.ts.net:8443",
    "users": {"operator@letu.edu": {"name": "Operator"}},
    "allow_domains": ["letu.edu"],
    "open_signin": False,
    "approval_required": True,
    "approved": [],
    "approval_notify": "owner@example.com",
    "mail": {"host": "smtp.gmail.com", "port": 587,
             "username": "workbench@example.com", "password": SECRET,
             "from": "WAAM Workbench <workbench@example.com>"},
}


def _log_row(email, outcome="pending", t="2026-09-07T00:00:00+00:00"):
    return {"t": t, "ip": "100.64.0.9", "via": None, "workspace": None,
            "outcome": outcome, "path": "/auth/verify", "email": email}


class _ApproveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-approve-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg_path = self.tmp / ".waam-auth.json"
        self.log_path = self.tmp / "access.log"
        self._saved = (oidc._auth_config_file, server.ACCESS_LOG)
        oidc._auth_config_file = lambda: self.cfg_path
        server.ACCESS_LOG = self.log_path
        self.addCleanup(self._restore)

    def _restore(self):
        oidc._auth_config_file, server.ACCESS_LOG = self._saved

    def write_cfg(self, cfg):
        self.cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def read_cfg(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))

    def write_log(self, *rows):
        self.log_path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                                 encoding="utf-8")

    def invoke(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = approve.main(list(argv))
        return rc, buf.getvalue()


class TestApproveWrites(_ApproveCase):
    def test_approving_adds_the_address_lowercased(self):
        self.write_cfg(BASE_CFG)
        rc, out = self.invoke("Newcomer@LETU.edu")
        self.assertEqual(rc, 0, out)
        self.assertIn("newcomer@letu.edu", self.read_cfg()["approved"])
        self.assertIn("approved", out)

    def test_every_other_key_including_the_secret_survives(self):
        self.write_cfg(BASE_CFG)
        rc, out = self.invoke("newcomer@letu.edu")
        self.assertEqual(rc, 0, out)
        cfg = self.read_cfg()
        for key, value in BASE_CFG.items():
            if key == "approved":
                continue
            self.assertEqual(cfg[key], value, f"{key} was disturbed")

        self.assertEqual(cfg["mail"]["password"], SECRET)
        self.assertNotIn(SECRET, out)

    def test_the_underscore_docs_are_preserved(self):
        self.write_cfg(BASE_CFG)
        self.invoke("a@letu.edu")
        cfg = self.read_cfg()
        self.assertEqual(cfg["_comment"], BASE_CFG["_comment"])
        self.assertEqual(cfg["_open_signin"], BASE_CFG["_open_signin"])

    def test_a_second_approve_is_idempotent(self):
        self.write_cfg(BASE_CFG)
        self.invoke("newcomer@letu.edu")
        rc, out = self.invoke("newcomer@letu.edu")
        self.assertEqual(rc, 0)
        self.assertIn("already approved", out)
        self.assertIn("nothing changed", out)
        self.assertEqual(self.read_cfg()["approved"].count("newcomer@letu.edu"), 1)

    def test_a_roster_address_is_admitted_without_being_added(self):
        self.write_cfg(BASE_CFG)
        rc, out = self.invoke("operator@letu.edu")
        self.assertEqual(rc, 0)
        self.assertIn("roster", out)
        self.assertEqual(self.read_cfg()["approved"], [])

    def test_a_batch_writes_only_the_new_valid_ones(self):
        self.write_cfg(BASE_CFG)
        rc, out = self.invoke("new1@letu.edu", "operator@letu.edu",
                           "not-an-email", "new2@letu.edu")
        self.assertEqual(rc, 0, out)
        approved = self.read_cfg()["approved"]
        self.assertIn("new1@letu.edu", approved)
        self.assertIn("new2@letu.edu", approved)
        self.assertNotIn("operator@letu.edu", approved)
        self.assertIn("not an email address", out)

    def test_a_missing_config_is_refused_cleanly(self):
        rc, out = self.invoke("a@letu.edu")
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", out)

    def test_the_temp_file_is_created_0600_with_no_umask_window(self):


        self.write_cfg(BASE_CFG)
        seen = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kw):
            if str(path).endswith(".tmp"):
                seen.append((bool(flags & os.O_CREAT), mode))
            return real_open(path, flags, mode, **kw)

        with mock.patch.object(approve.os, "open", spy):
            rc, out = self.invoke("newcomer@letu.edu")
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [(True, 0o600)])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.cfg_path.stat().st_mode), 0o600)

    def test_it_creates_the_approved_key_when_absent(self):
        cfg = {k: v for k, v in BASE_CFG.items() if k != "approved"}
        self.write_cfg(cfg)
        rc, out = self.invoke("newcomer@letu.edu")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_cfg()["approved"], ["newcomer@letu.edu"])


class TestApproveList(_ApproveCase):
    def test_list_shows_a_pending_and_hides_an_approved_or_rostered_one(self):
        self.write_cfg(dict(BASE_CFG, approved=["approved@letu.edu"]))
        self.write_log(
            _log_row("waiting@letu.edu"),
            _log_row("approved@letu.edu"),
            _log_row("operator@letu.edu"),
            _log_row("someone@letu.edu", outcome="signin-ok"),
        )
        rc, out = self.invoke("--list")
        self.assertEqual(rc, 0, out)
        self.assertIn("waiting@letu.edu", out)
        self.assertNotIn("approved@letu.edu", out)
        self.assertNotIn("operator@letu.edu", out)
        self.assertNotIn("someone@letu.edu", out)

    def test_list_dedupes_a_repeated_request(self):
        self.write_cfg(BASE_CFG)
        self.write_log(_log_row("waiting@letu.edu"),
                       _log_row("waiting@letu.edu"))
        self.assertEqual(approve.pending_requests(self.log_path),
                         ["waiting@letu.edu"])

    def test_list_says_so_when_no_one_waits(self):
        self.write_cfg(BASE_CFG)
        rc, out = self.invoke("--list")
        self.assertEqual(rc, 0)
        self.assertIn("no one is waiting", out)

    def test_list_takes_no_addresses(self):
        self.write_cfg(BASE_CFG)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                approve.main(["--list", "a@letu.edu"])


if __name__ == "__main__":
    unittest.main()
