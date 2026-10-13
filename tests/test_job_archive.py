import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline import job_archive, server


class TestFileArchive(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"

    def test_exact_bytes_deduplication_and_concurrent_writers(self):
        body = b"\x00binary STL\xff\r\n"
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            paths = list(pool.map(lambda _: job_archive.preserve(self.root, "account-a", "stl", body), range(8)))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(paths[0].read_bytes(), body)
        metadata = json.loads(paths[0].with_name("metadata.json").read_text())
        self.assertEqual(metadata["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(len(list(self.root.rglob("metadata.json"))), 1)
        self.assertFalse(list(self.root.rglob(".pending-*")))
        other = job_archive.preserve(self.root, "account-b", "stl", body)
        self.assertNotEqual(paths[0], other)

    def test_limits_preserve_existing_files(self):
        path = job_archive.preserve(self.root, "a", "gcode", b"G21\n")
        with patch.object(job_archive, "MAX_TOTAL_BYTES", 1):
            self.assertEqual(job_archive.preserve(self.root, "a", "gcode", b"G21\n"), path)
            with self.assertRaisesRegex(ValueError, "storage limit"):
                job_archive.preserve(self.root, "a", "gcode", b"different")
        self.assertEqual(path.read_bytes(), b"G21\n")

    def test_path_and_kind_refusals(self):
        for workspace in ("../escape", "a/b", ".", "guest.x", ""):
            with self.assertRaises(ValueError):
                job_archive.preserve(self.root, workspace, "stl", b"test")
        with self.assertRaises(ValueError):
            job_archive.preserve(self.root, "a", "../../secret", b"test")
        self.assertFalse(self.root.exists())

    def test_partial_failure_leaves_no_bundle_and_retry_succeeds(self):
        with patch.object(job_archive.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                job_archive.preserve(self.root, "a", "jbi", b"/JOB\n")
        self.assertFalse(list(self.root.rglob("metadata.json")))
        self.assertFalse(list(self.root.rglob(".pending-*")))
        self.assertEqual(job_archive.preserve(self.root, "a", "jbi", b"/JOB\n").read_bytes(), b"/JOB\n")

    def test_guest_and_token_never_write_and_account_failure_is_visible(self):
        handler = object.__new__(server._Handler)
        handler.log_error = lambda *a: None
        with patch.object(server, "SAVES_DIR", self.root):
            for who in ({"guest": True, "email": None}, {"email": None}, {"email": "x", "guest": True}):
                handler.waam_who = who
                self.assertEqual(handler._archive("test", "stl", b"test"), "not-requested")
            self.assertFalse(self.root.exists())
            handler.waam_who = {"email": "test@example.invalid"}
            self.assertEqual(handler._archive("test", "stl", b"test"), "saved")
            with patch.object(job_archive, "MAX_TOTAL_BYTES", 1):
                self.assertEqual(handler._archive("test", "stl", b"new"), "failed")
