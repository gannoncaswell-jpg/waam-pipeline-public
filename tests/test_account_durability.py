import contextlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import accounts

EMAIL = "worker@example.com"
PASSWORD = "a long test pass phrase"
REPLACEMENT = "a different strong phrase"


class TestAccountDurableWrites(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = accounts.Store(self.root / ".waam-accounts.json")
        self.store.register(EMAIL, PASSWORD)
        self.before = self.store.path.read_bytes()

        self.legacy_temporary = self.root / ".waam-accounts.json.tmp"
        self.legacy_temporary.write_bytes(b"unrelated pre-existing file")
        self.before_names = {p.name for p in self.root.iterdir()}

    def assert_only_owned_temporary_was_cleaned(self):
        self.assertEqual({p.name for p in self.root.iterdir()}, self.before_names)
        self.assertEqual(self.legacy_temporary.read_bytes(),
                         b"unrelated pre-existing file")

    def reset_from_cli(self):
        output = io.StringIO()
        with patch.object(accounts, "_accounts_file", return_value=self.store.path), \
                contextlib.redirect_stdout(output):
            result = accounts._main(["reset", EMAIL, "--password", REPLACEMENT])
        return result, output.getvalue()

    def test_success_syncs_private_content_before_replace_and_directory_after(self):
        sync = os.fsync
        events = []

        def observe(fd):
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                events.append("file")
                self.assertGreater(info.st_size, 0)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(self.store.path.read_bytes(), self.before)
            else:
                self.assertTrue(stat.S_ISDIR(info.st_mode))
                events.append("directory")
                self.assertNotEqual(self.store.path.read_bytes(), self.before)
            sync(fd)

        with patch.object(accounts.os, "fsync", side_effect=observe):
            result, output = self.reset_from_cli()
        self.assertEqual(result, 0)
        self.assertIn("password set", output)
        self.assertEqual(events, ["file"] if os.name == "nt"
                         else ["file", "directory"])
        self.assertTrue(accounts.verify_password(self.store.get(EMAIL), REPLACEMENT))
        self.assert_only_owned_temporary_was_cleaned()

    def test_failed_file_sync_reports_cli_failure_and_preserves_old_revision(self):
        with patch.object(accounts.os, "fsync", side_effect=OSError("injected sync failure")):
            result, output = self.reset_from_cli()
        self.assertEqual(result, 1)
        self.assertNotIn("password set", output)
        self.assertEqual(self.store.path.read_bytes(), self.before)
        self.assertTrue(accounts.verify_password(self.store.get(EMAIL), PASSWORD))
        self.assert_only_owned_temporary_was_cleaned()

    def test_failed_file_sync_does_not_report_account_deletion(self):
        with patch.object(accounts.os, "fsync", side_effect=OSError("injected sync failure")):
            with self.assertRaises(accounts.AccountError):
                self.store.delete(EMAIL)
        self.assertEqual(self.store.path.read_bytes(), self.before)
        self.assertTrue(accounts.verify_password(self.store.get(EMAIL), PASSWORD))
        self.assert_only_owned_temporary_was_cleaned()

    def test_failed_replace_reports_cli_failure_and_cleans_private_temporary(self):
        with patch.object(accounts.os, "replace", side_effect=OSError("injected rename failure")):
            result, output = self.reset_from_cli()
        self.assertEqual(result, 1)
        self.assertNotIn("password set", output)
        self.assertEqual(self.store.path.read_bytes(), self.before)
        self.assert_only_owned_temporary_was_cleaned()

    @unittest.skipIf(os.name == "nt", "Windows has no stdlib directory fsync")
    def test_failed_directory_sync_reports_failure_without_restoring_old_credentials(self):
        sync = os.fsync
        directory_fds = []

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                directory_fds.append(fd)
                raise OSError("injected directory sync failure")
            sync(fd)

        with patch.object(accounts.os, "fsync", side_effect=fail_directory_sync):
            result, output = self.reset_from_cli()
        self.assertEqual(result, 1)
        self.assertNotIn("password set", output)
        self.assertEqual(len(directory_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(directory_fds[0])
        self.assertNotEqual(self.store.path.read_bytes(), self.before)
        current = self.store.get(EMAIL)
        self.assertTrue(accounts.verify_password(current, REPLACEMENT))
        self.assertFalse(accounts.verify_password(current, PASSWORD))
        self.assert_only_owned_temporary_was_cleaned()


if __name__ == "__main__":
    unittest.main()
