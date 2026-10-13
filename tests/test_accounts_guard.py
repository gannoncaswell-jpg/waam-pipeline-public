import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name == "posix" else None


@unittest.skipUnless(BASH and shutil.which("python3"), "Linux account guard requires Bash and python3")
class TestAccountsGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="waam-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.backups = self.root / "backups with spaces"
        self.commands = self.root / "bin"
        for path in (self.repo, self.backups, self.commands):
            path.mkdir()
        self.accounts = self.repo / ".waam-accounts.json"
        logger = self.commands / "logger"
        logger.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        logger.chmod(0o700)
        self.env = dict(os.environ, WAAM_REPO=str(self.repo), WAAM_BACKUP_DIR=str(self.backups),
                        PATH=str(self.commands) + os.pathsep + os.environ["PATH"])

    def backup(self):
        data = json.dumps({"accounts": {"owner": {"role": "admin"},
                                        "revoked": {"role": "operator"}}}).encode()
        with tarfile.open(self.backups / "waam-state-fixture.tar.gz", "w:gz") as archive:
            member = tarfile.TarInfo(".waam-accounts.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

    def run_guard(self):
        result = subprocess.run([BASH, str(ROOT / "deploy/waam-accounts-guard.sh")],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout + result.stderr

    def test_deleted_accounts_are_not_restored_when_one_or_zero_remain(self):
        self.backup()
        for accounts in ({"owner": {"role": "admin"}}, {}):
            with self.subTest(accounts=len(accounts)):
                original = json.dumps({"accounts": accounts}).encode()
                self.accounts.write_bytes(original)
                message = self.run_guard()
                self.assertEqual(self.accounts.read_bytes(), original)
                self.assertIn("Manual review required", message)
                self.assertEqual(list(self.repo.iterdir()), [self.accounts])

    def test_missing_or_damaged_store_requires_manual_recovery(self):
        self.backup()
        for original in (None, b"", b"invalid json"):
            with self.subTest(original=original):
                if original is not None:
                    self.accounts.write_bytes(original)
                message = self.run_guard()
                self.assertIn("Manual review required", message)
                if original is None:
                    self.assertFalse(self.accounts.exists())
                else:
                    self.assertEqual(self.accounts.read_bytes(), original)

    def test_healthy_roster_is_unchanged_and_quiet(self):
        self.backup()
        original = b'{"accounts":{"owner":{},"operator":{}}}'
        self.accounts.write_bytes(original)
        self.assertEqual(self.run_guard(), "")
        self.assertEqual(self.accounts.read_bytes(), original)

    def test_bad_or_absent_backup_never_blocks_startup(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                if corrupt:
                    (self.backups / "waam-state-broken.tgz").write_bytes(b"not an archive")
                self.run_guard()
                self.assertFalse(self.accounts.exists())
