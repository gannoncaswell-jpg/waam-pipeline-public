import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import desktop_release

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    Ed25519PrivateKey = None


def isolate_pinned_key(case, root):

    environment = mock.patch.dict(os.environ)
    environment.start()
    case.addCleanup(environment.stop)
    os.environ.pop(desktop_release.PUBLIC_KEY_FILE_ENV, None)
    default = mock.patch.object(desktop_release, "DEFAULT_PUBLIC_KEY_FILE",
                                str(Path(root) / "no-pinned-public-key"))
    default.start()
    case.addCleanup(default.stop)


class TestDesktopRelease(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        environment = mock.patch.dict(os.environ, {"WAAM_DOWNLOADS_DIR": str(self.downloads)})
        environment.start()
        self.addCleanup(environment.stop)
        isolate_pinned_key(self, self.root)
        desktop_release._HASH_CACHE.clear()
        self.addCleanup(desktop_release._HASH_CACHE.clear)

    def write_release(self, body=b"desktop-build", **changes):
        actual = hashlib.sha256(body).hexdigest()
        declared = changes.get("sha256", actual)
        name = changes.get("file", f"{declared[:16]}-WAAM-Studio.dmg")
        path = self.downloads / name
        if "/" not in name and "\\" not in name:
            path.write_bytes(body)
            fixed = 1_800_000_000_000_000_000
            os.utime(path, ns=(fixed, fixed))
            mtime_ns = path.stat().st_mtime_ns
        else:
            mtime_ns = fixed = 1_800_000_000_000_000_000
        manifest = {
            "version": "1.2.3",
            "file": name,
            "size": len(body),
            "sha256": declared,
            "platform": desktop_release.PLATFORM,
            "signed": desktop_release.SIGNED,
            "notarized": False,
            "published_at": "2026-10-11T14:30:00+00:00",
            "notes": "Test build",
            "mtime_ns": mtime_ns,
        }
        manifest.update(changes)
        (self.downloads / desktop_release.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def source(self, name, body):
        folder = self.root / "source"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(body)
        return path

    def test_load_validates_descriptor_and_caches_by_device_inode_size_and_mtime(self):
        path, manifest = self.write_release(b"x" * 8193)
        with mock.patch.object(desktop_release, "_hash_source",
                               wraps=desktop_release._hash_source) as hashed:
            self.assertEqual(desktop_release.load(), manifest)
            self.assertEqual(desktop_release.load(), manifest)
            self.assertEqual(hashed.call_count, 1)

            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            manifest["mtime_ns"] = path.stat().st_mtime_ns
            (self.downloads / desktop_release.MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8")
            self.assertEqual(desktop_release.load(), manifest)
            self.assertEqual(hashed.call_count, 2)

            replacement = self.downloads / "replacement.tmp"
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement, ns=(info.st_atime_ns, manifest["mtime_ns"]))
            os.replace(replacement, path)
            self.assertEqual(desktop_release.load(), manifest)
            self.assertEqual(hashed.call_count, 3)

        info = path.stat()
        self.assertEqual(desktop_release._descriptor_key(info)[-1], info.st_ctime_ns)
        prefix = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        self.assertTrue(any(key[:4] == prefix and len(key) == 5
                            for key in desktop_release._HASH_CACHE))

    def test_load_returns_none_for_absent_invalid_or_inconsistent_releases(self):
        with self.assertLogs(desktop_release._LOG, level="INFO") as logged:
            self.assertIsNone(desktop_release.load())
        self.assertIn("desktop.json is absent", " ".join(logged.output))

        _, manifest = self.write_release()
        cases = (
            dict(size=manifest["size"] + 1),
            dict(mtime_ns=manifest["mtime_ns"] + 1),
            dict(platform="Windows"),
            dict(notarized=True),
            dict(file="../WAAM-Studio.dmg"),
            dict(published_at="2026-10-11"),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                (self.downloads / desktop_release.MANIFEST_NAME).write_text(
                    json.dumps(dict(manifest, **changes)), encoding="utf-8")
                desktop_release._HASH_CACHE.clear()
                with self.assertLogs(desktop_release._LOG, level="WARNING"):
                    self.assertIsNone(desktop_release.load())

        (self.downloads / desktop_release.MANIFEST_NAME).write_bytes(
            b"{" + b" " * desktop_release.MAX_MANIFEST_BYTES)
        with self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            self.assertIsNone(desktop_release.load())
        self.assertIn("manifest is too large", " ".join(logged.output))

    def test_cached_sha_mismatch_is_rehashed_once_before_rejection(self):
        self.write_release(sha256="0" * 64)
        with mock.patch.object(desktop_release, "_hash_source",
                               wraps=desktop_release._hash_source) as hashed, \
                self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            self.assertIsNone(desktop_release.load())
            self.assertIsNone(desktop_release.load())
        self.assertEqual(hashed.call_count, 2)
        self.assertIn("sha256 does not match", " ".join(logged.output))

    def test_stale_cached_digest_is_rehashed_and_repaired(self):
        _path, manifest = self.write_release(b"current descriptor bytes")
        key = (1, 2, manifest["size"], manifest["mtime_ns"], 5)
        desktop_release._HASH_CACHE[key] = "f" * 64
        self.assertNotEqual(desktop_release._HASH_CACHE[key], manifest["sha256"])
        with mock.patch.object(desktop_release, "_descriptor_key", return_value=key), \
                mock.patch.object(desktop_release, "_hash_source",
                               wraps=desktop_release._hash_source) as hashed:
            self.assertEqual(desktop_release.load(), manifest)
        self.assertEqual(hashed.call_count, 1)
        self.assertEqual(desktop_release._HASH_CACHE[key], manifest["sha256"])

    def test_release_entry_symlink_to_an_outside_file_is_refused(self):
        path, _manifest = self.write_release(b"outside bytes")
        outside = self.root / "outside.dmg"
        outside.write_bytes(path.read_bytes())
        info = path.stat()
        os.utime(outside, ns=(info.st_atime_ns, info.st_mtime_ns))
        path.unlink()
        try:
            path.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        modes = (
            contextlib.nullcontext(),
            mock.patch.object(desktop_release.os, "O_NOFOLLOW", 0, create=True),
        )
        for mode in modes:
            with mode, self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
                self.assertIsNone(desktop_release.open_release())
            self.assertIn("symbolic link", " ".join(logged.output).lower())

    def test_release_entry_must_be_a_regular_file(self):
        path, _manifest = self.write_release()
        path.unlink()
        path.mkdir()
        with self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            self.assertIsNone(desktop_release.open_release())
        self.assertIn("regular file", " ".join(logged.output).lower())

    def test_load_keeps_a_pre_immutable_manifest_servable(self):
        path, manifest = self.write_release()
        legacy = self.downloads / "WAAM-Studio.dmg"
        path.replace(legacy)
        manifest["file"] = legacy.name
        manifest.pop("mtime_ns")
        (self.downloads / desktop_release.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8")
        self.assertEqual(desktop_release.load(), manifest)

        before = (self.downloads / desktop_release.MANIFEST_NAME).read_bytes()
        windows = desktop_release.publish(self.source("setup.exe", b"windows"), "1.0.0")
        self.assertEqual(desktop_release.load_all(),
                         {"macos-arm64": manifest, "windows-x64": windows})
        self.assertEqual((self.downloads / desktop_release.MANIFEST_NAME).read_bytes(), before)
        self.assertTrue(legacy.exists())

    def test_open_release_keeps_the_verified_descriptor_across_a_publish(self):
        source = self.source("WAAM-Studio.dmg", b"first release")
        first = desktop_release.publish(source, "1.0.0")
        opened = desktop_release.open_release()
        self.assertIsNotNone(opened)
        source.write_bytes(b"second release is a different size")
        second = desktop_release.publish(source, "2.0.0")
        with opened:
            self.assertEqual(opened.manifest, first)
            self.assertEqual(opened.source.read(), b"first release")
        self.assertEqual(desktop_release.load(), second)

    def test_publish_commits_manifest_then_removes_the_previous_immutable_asset(self):
        first_source = self.source("WAAM-Studio.dmg", b"first")
        first = desktop_release.publish(first_source, "1.0.0")
        second_source = self.source("WAAM-Studio-2.dmg", b"second" * 100)
        second_digest = hashlib.sha256(second_source.read_bytes()).hexdigest()
        second_name = f"{second_digest[:16]}-{second_source.name}"
        original_replace = os.replace
        destinations = []

        def replace(source, destination):
            target = Path(destination)
            destinations.append(target.name)
            if target.name == desktop_release.MANIFEST_NAME:
                self.assertTrue((self.downloads / first["file"]).exists())
                self.assertTrue((self.downloads / second_name).exists())
            original_replace(source, destination)

        with mock.patch.object(desktop_release.os, "replace", side_effect=replace):
            second = desktop_release.publish(second_source, "2.0.0", notes="replacement")
        self.assertEqual(destinations, [second_name, desktop_release.MANIFEST_NAME])
        self.assertFalse((self.downloads / first["file"]).exists())
        self.assertEqual((self.downloads / second["file"]).read_bytes(), b"second" * 100)
        self.assertEqual(desktop_release.load(), second)
        self.assertEqual(list(self.downloads.glob(".*.tmp")), [])

        second_source.write_bytes(b"same original name, newer build")
        third = desktop_release.publish(second_source, "2.1.0")
        self.assertNotEqual(third["file"], second["file"])
        self.assertFalse((self.downloads / second["file"]).exists())
        self.assertEqual((self.downloads / third["file"]).read_bytes(),
                         b"same original name, newer build")

    def test_concurrent_publishers_are_serialized_through_cleanup(self):
        initial_source = self.source("concurrent-initial.dmg", b"initial")
        desktop_release.publish(initial_source, "0.1.0")
        first_source = self.source("concurrent-first.dmg", b"first concurrent")
        second_source = self.source("concurrent-second.dmg", b"second concurrent")
        original_copy = desktop_release._copy_and_hash
        first_inside = threading.Event()
        release_first = threading.Event()
        second_inside = threading.Event()
        results = {}
        errors = []
        versions = {"first": "1.0.0", "second": "2.0.0"}

        def copy(source, root, mtime_ns):
            if source == first_source:
                first_inside.set()
                if not release_first.wait(5):
                    raise RuntimeError("timed out holding the first publisher")
            elif source == second_source:
                second_inside.set()
            return original_copy(source, root, mtime_ns)

        def run(label, source):
            try:
                results[label] = desktop_release.publish(source, versions[label])
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(desktop_release, "_copy_and_hash", side_effect=copy):
            first_thread = threading.Thread(target=run, args=("first", first_source))
            second_thread = threading.Thread(target=run, args=("second", second_source))
            first_thread.start()
            self.assertTrue(first_inside.wait(2))
            second_thread.start()
            try:
                self.assertFalse(second_inside.wait(0.25))
            finally:
                release_first.set()
            first_thread.join(5)
            second_thread.join(5)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_inside.is_set())
        current = desktop_release.load()
        self.assertEqual(current, results["second"])
        self.assertEqual((self.downloads / current["file"]).read_bytes(),
                         b"second concurrent")
        assets = {path.name for path in self.downloads.iterdir()
                  if path.name not in {
                      desktop_release.MANIFEST_NAME, desktop_release.LOCK_NAME,
                  }}
        self.assertEqual(assets, {current["file"]})

    def test_publish_waits_for_the_cross_process_release_lock(self):
        ready = self.root / "lock-ready"
        release_lock = self.root / "release-lock"
        script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from waam_pipeline import accounts\n"
            "lock,ready,release=map(Path,sys.argv[1:])\n"
            "with accounts.exclusive_file_lock(lock,timeout=5):\n"
            " ready.write_text('ready',encoding='ascii')\n"
            " deadline=time.monotonic()+5\n"
            " while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get(
            "PYTHONPATH", "")
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.downloads / desktop_release.LOCK_NAME),
             str(ready), str(release_lock)],
            cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), process.stderr.read() if process.poll() is not None else "")

        source = self.source("cross-process.dmg", b"cross-process")
        finished = threading.Event()
        errors = []

        def run():
            try:
                desktop_release.publish(source, "3.0.0")
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.25))
        finally:
            release_lock.write_text("release", encoding="ascii")
        thread.join(5)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(desktop_release.load()["version"], "3.0.0")

    def test_post_commit_directory_fsync_failure_keeps_both_assets(self):
        first_source = self.source("durable-first.dmg", b"first")
        first = desktop_release.publish(first_source, "1.0.0")
        second_source = self.source("durable-second.dmg", b"second")
        second_digest = hashlib.sha256(second_source.read_bytes()).hexdigest()
        second_name = f"{second_digest[:16]}-{second_source.name}"
        original_sync = desktop_release._fsync_directory
        calls = 0

        def sync(root):
            nonlocal calls
            calls += 1
            if calls == 1:
                committed = json.loads((root / desktop_release.MANIFEST_NAME).read_text(
                    encoding="utf-8"))
                self.assertEqual(committed["file"], first["file"])
                self.assertTrue((root / second_name).exists())
            if calls == 2:
                committed = json.loads((root / desktop_release.MANIFEST_NAME).read_text(
                    encoding="utf-8"))
                self.assertEqual(committed["file"], second_name)
                self.assertTrue((root / first["file"]).exists())
                raise OSError("manifest directory fsync failed")
            return original_sync(root)

        with mock.patch.object(desktop_release, "_fsync_directory", side_effect=sync), \
                self.assertRaisesRegex(OSError, "manifest directory fsync failed"):
            desktop_release.publish(second_source, "2.0.0")
        self.assertEqual(calls, 2)
        self.assertTrue((self.downloads / first["file"]).exists())
        self.assertTrue((self.downloads / second_name).exists())
        current = desktop_release.load()
        self.assertEqual(current["version"], "2.0.0")
        self.assertEqual(current["file"], second_name)

    def test_asset_directory_fsync_failure_preserves_previous_manifest(self):
        first_source = self.source("asset-sync-first.dmg", b"first")
        first = desktop_release.publish(first_source, "1.0.0")
        second_source = self.source("asset-sync-second.dmg", b"second")
        second_digest = hashlib.sha256(second_source.read_bytes()).hexdigest()
        second_name = f"{second_digest[:16]}-{second_source.name}"
        with mock.patch.object(
                desktop_release, "_fsync_directory",
                side_effect=OSError("asset directory fsync failed")), \
                self.assertRaisesRegex(OSError, "asset directory fsync failed"):
            desktop_release.publish(second_source, "2.0.0")
        self.assertEqual(desktop_release.load(), first)
        self.assertTrue((self.downloads / first["file"]).exists())
        self.assertFalse((self.downloads / second_name).exists())

    def test_publish_failures_before_commit_leave_previous_release_servable(self):
        points = ("hash", "copy", "asset-fsync", "asset-replace",
                  "manifest-fsync", "manifest-replace")
        for point in points:
            with self.subTest(point=point):
                folder = self.root / ("failure-" + point)
                folder.mkdir()
                current_source = self.source("current-" + point + ".dmg", b"current")
                current = desktop_release.publish(current_source, "1.0.0", directory=folder)
                before = (folder / desktop_release.MANIFEST_NAME).read_bytes()
                candidate = self.source("candidate-" + point + ".dmg", b"candidate" * 20)
                original_replace = os.replace
                original_fsync = os.fsync
                regular_fsyncs = 0

                def replace(source, destination, *, failure=point,
                            operation=original_replace):
                    target = Path(destination)
                    if failure == "asset-replace" and target.name != desktop_release.MANIFEST_NAME:
                        raise OSError("asset replace failed")
                    if failure == "manifest-replace" and target.name == desktop_release.MANIFEST_NAME:
                        raise OSError("manifest replace failed")
                    return operation(source, destination)

                def fsync(descriptor, *, failure=point, operation=original_fsync):
                    nonlocal regular_fsyncs
                    if stat.S_ISREG(os.fstat(descriptor).st_mode):
                        regular_fsyncs += 1
                    if failure == "asset-fsync" and regular_fsyncs == 1:
                        raise OSError("asset fsync failed")
                    if failure == "manifest-fsync" and regular_fsyncs == 3:
                        raise OSError("manifest fsync failed")
                    return operation(descriptor)

                patches = [
                    mock.patch.object(desktop_release.os, "replace", side_effect=replace),
                    mock.patch.object(desktop_release.os, "fsync", side_effect=fsync),
                ]
                if point == "hash":
                    patches.append(mock.patch.object(
                        desktop_release, "_hash_file", side_effect=OSError("hash failed")))
                if point == "copy":
                    patches.append(mock.patch.object(
                        desktop_release, "_copy_and_hash", side_effect=OSError("copy failed")))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(OSError):
                        desktop_release.publish(candidate, "2.0.0", directory=folder)

                self.assertEqual((folder / desktop_release.MANIFEST_NAME).read_bytes(), before)
                self.assertEqual(desktop_release.load(folder), current)
                assets = {path.name for path in folder.iterdir()
                          if path.name not in {
                              desktop_release.MANIFEST_NAME, desktop_release.LOCK_NAME,
                          }}
                self.assertEqual(assets, {current["file"]})

    def test_previous_cleanup_failure_does_not_break_the_committed_release(self):
        first_source = self.source("cleanup-first.dmg", b"first")
        first = desktop_release.publish(first_source, "1.0.0")
        second_source = self.source("cleanup-second.dmg", b"second")
        old_path = self.downloads / first["file"]
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == old_path:
                raise OSError("busy")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=unlink), \
                self.assertLogs(desktop_release._LOG, level="WARNING"):
            second = desktop_release.publish(second_source, "2.0.0")
        self.assertEqual(desktop_release.load(), second)
        self.assertTrue(old_path.exists())

    def test_manifest_limits_are_checked_before_copy_or_replace(self):
        current_source = self.source("limits-current.dmg", b"current")
        current = desktop_release.publish(current_source, "1.0.0")
        candidate = self.source("limits-candidate.dmg", b"candidate")
        cases = (
            ({"version": "v" * (desktop_release.MAX_VERSION_BYTES + 1)}, "version is too long"),
            ({"version": "2.0.0", "notes": "n" * (desktop_release.MAX_NOTES_BYTES + 1)},
             "notes is too long"),
        )
        for kwargs, message in cases:
            with self.subTest(message=message), \
                    mock.patch.object(desktop_release, "_hash_file") as hashed, \
                    mock.patch.object(desktop_release, "_copy_and_hash") as copied, \
                    self.assertRaisesRegex(ValueError, message):
                desktop_release.publish(candidate, **kwargs)
            hashed.assert_not_called()
            copied.assert_not_called()
            self.assertEqual(desktop_release.load(), current)

        with mock.patch.object(desktop_release, "MAX_MANIFEST_BYTES", 128), \
                mock.patch.object(desktop_release, "_copy_and_hash") as copied, \
                self.assertRaisesRegex(ValueError, "manifest is too large"):
            desktop_release.publish(candidate, "2.0.0")
        copied.assert_not_called()
        self.assertEqual(desktop_release.load(), current)

    def test_cli_refuses_expected_sha_mismatch_without_disturbing_current_release(self):
        current_source = self.source("current.dmg", b"current")
        current = desktop_release.publish(current_source, "1.0.0")
        candidate = self.source("candidate.dmg", b"candidate")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = desktop_release.main([
                "publish", str(candidate), "--version", "2.0.0", "--sha256", "f" * 64,
            ])
        self.assertEqual(result, 1)
        self.assertIn("sha256 mismatch", stderr.getvalue())
        self.assertEqual(desktop_release.load(), current)
        self.assertEqual({path.name for path in self.downloads.iterdir()},
                         {desktop_release.MANIFEST_NAME, desktop_release.LOCK_NAME,
                          current["file"]})

    def test_cli_publish_show_and_bad_names(self):
        bad = self.source("desktop.msi", b"bad")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main(["publish", str(bad), "--version", "1.0.0"]), 1)
            empty = self.source("empty.dmg", b"")
            self.assertEqual(desktop_release.main(["publish", str(empty), "--version", "1.0.0"]), 1)
        for name in ("folder/release.dmg", "folder\\release.zip", "release.tar.gz",
                     "folder\\setup.exe", "setup.msi"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                desktop_release._file_name(name)

        source = self.source("WAAM-Studio.zip", b"zip-build")
        expected = hashlib.sha256(source.read_bytes()).hexdigest().upper()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main([
                "publish", str(source), "--version", "3.1.0", "--sha256", expected,
            ]), 0)
        published = json.loads(stdout.getvalue())
        self.assertEqual(published["version"], "3.1.0")
        self.assertTrue(published["file"].startswith(expected.lower()[:16] + "-"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main(["show"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"macos-arm64": desktop_release.load()})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main(["show", "--platform", "macos-arm64"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), desktop_release.load())
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(desktop_release.main(["show", "--platform", "windows-x64"]), 1)
        self.assertIn("No windows-x64 desktop build", stderr.getvalue())

        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"windows-build")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(desktop_release.main([
                "publish", str(installer), "--version", "0.2.0", "--platform", "macos-arm64",
            ]), 1)
        self.assertIn("macos-arm64 file must end in .dmg or .zip", stderr.getvalue())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main([
                "publish", str(installer), "--version", "0.2.0", "--platform", "windows-x64",
            ]), 0)
        self.assertEqual(json.loads(stdout.getvalue()),
                         desktop_release.load(platform="windows-x64"))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(desktop_release.main(["show"]), 0)
        self.assertEqual(list(json.loads(stdout.getvalue())), ["macos-arm64", "windows-x64"])

    def test_each_platform_keeps_its_own_manifest_and_asset(self):
        mac_source = self.source("WAAM-Studio.dmg", b"mac build")
        mac = desktop_release.publish(mac_source, "1.0.0", notes="Mac")
        mac_manifest = (self.downloads / desktop_release.MANIFEST_NAME).read_bytes()
        windows_name = desktop_release.PLATFORMS["windows-x64"].manifest
        self.assertEqual(windows_name, "desktop-windows-x64.json")
        self.assertIsNone(desktop_release.load(platform="windows-x64"))
        self.assertEqual(desktop_release.load_all(), {"macos-arm64": mac})

        installer = self.source("WAAM-Studio-1.0-win-x64-setup.exe", b"windows build" * 50)
        windows = desktop_release.publish(installer, "1.0.0", notes="Windows")
        self.assertEqual(windows["platform"], "Windows 10/11 (x64)")
        self.assertEqual(windows["signed"], "unsigned (development)")
        self.assertIs(windows["notarized"], False)
        self.assertTrue(windows["file"].endswith("-WAAM-Studio-1.0-win-x64-setup.exe"))
        self.assertEqual((self.downloads / desktop_release.MANIFEST_NAME).read_bytes(),
                         mac_manifest, "publishing Windows never rewrites the macOS manifest")
        self.assertEqual(desktop_release.load_all(), {"macos-arm64": mac, "windows-x64": windows})
        with desktop_release.open_release(platform="windows-x64") as opened:
            self.assertEqual(opened.source.read(), b"windows build" * 50)

        installer.write_bytes(b"windows rebuild")
        replacement = desktop_release.publish(installer, "1.1.0")
        self.assertFalse((self.downloads / windows["file"]).exists())
        self.assertTrue((self.downloads / mac["file"]).exists())
        self.assertEqual(desktop_release.load_all(),
                         {"macos-arm64": mac, "windows-x64": replacement})
        mac_source.write_bytes(b"mac rebuild")
        mac_replacement = desktop_release.publish(mac_source, "1.1.0")
        self.assertFalse((self.downloads / mac["file"]).exists())
        self.assertTrue((self.downloads / replacement["file"]).exists())
        self.assertEqual(desktop_release.load(platform="windows-x64"), replacement)
        self.assertEqual(desktop_release.load(), mac_replacement)

    def test_platform_manifests_reject_each_others_files_and_labels(self):
        installer = self.source("setup.exe", b"windows build")
        with self.assertRaisesRegex(ValueError, "windows-x64 file must end in .exe"):
            desktop_release.publish(self.source("WAAM.dmg", b"mac"), "1.0.0", platform="windows-x64")
        with self.assertRaisesRegex(ValueError, "unknown desktop platform"):
            desktop_release.publish(installer, "1.0.0", platform="linux-x64")
        windows = desktop_release.publish(installer, "1.0.0")
        windows_manifest = self.downloads / desktop_release.PLATFORMS["windows-x64"].manifest
        body = json.loads(windows_manifest.read_text(encoding="utf-8"))
        cases = (
            dict(platform=desktop_release.PLATFORM),
            dict(signed=desktop_release.SIGNED),
            dict(notarized=True),
            dict(file=windows["file"][:-4] + ".dmg"),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                windows_manifest.write_text(json.dumps(dict(body, **changes)), encoding="utf-8")
                desktop_release._HASH_CACHE.clear()
                with self.assertLogs(desktop_release._LOG, level="WARNING"):
                    self.assertIsNone(desktop_release.load(platform="windows-x64"))
        (self.downloads / desktop_release.MANIFEST_NAME).write_text(
            json.dumps(body), encoding="utf-8")
        with self.assertLogs(desktop_release._LOG, level="WARNING"):
            self.assertIsNone(desktop_release.load())

    def test_cleanup_never_removes_an_asset_another_platform_still_serves(self):
        windows = desktop_release.publish(self.source("setup.exe", b"windows build"), "1.0.0")
        mac_manifest = self.downloads / desktop_release.MANIFEST_NAME


        mac_manifest.write_text(json.dumps({
            "version": "0", "file": windows["file"][:-4] + ".zip", "size": 1,
            "sha256": windows["sha256"], "platform": desktop_release.PLATFORM,
            "signed": desktop_release.SIGNED, "notarized": False,
            "published_at": "2026-10-11T14:30:00+00:00", "notes": "",
        }), encoding="utf-8")
        desktop_release._remove_if_uncommitted(self.downloads, windows["file"])
        self.assertTrue((self.downloads / windows["file"]).exists())
        (self.downloads / desktop_release.PLATFORMS["windows-x64"].manifest).write_text(
            "{", encoding="utf-8")
        with self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            desktop_release._remove_if_uncommitted(self.downloads, windows["file"])
        self.assertIn("manifest cannot be verified", " ".join(logged.output))
        self.assertTrue((self.downloads / windows["file"]).exists())


FAKE_SIGNATURE = base64.b64encode(bytes(range(64))).decode("ascii")


class TestReleaseSignatures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        environment = mock.patch.dict(os.environ, {"WAAM_DOWNLOADS_DIR": str(self.downloads)})
        environment.start()
        self.addCleanup(environment.stop)
        isolate_pinned_key(self, self.root)
        desktop_release._HASH_CACHE.clear()
        self.addCleanup(desktop_release._HASH_CACHE.clear)

    def source(self, name, body):
        folder = self.root / "source"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(body)
        return path

    def key_pair(self):
        if Ed25519PrivateKey is None:
            self.skipTest("cryptography is required to sign a test release")
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return private, base64.b64encode(public).decode("ascii")

    @staticmethod
    def sign(private, path, platform, version):
        body = path.read_bytes()
        message = desktop_release.release_message(
            platform, version, len(body), hashlib.sha256(body).hexdigest())
        return base64.b64encode(private.sign(message)).decode("ascii")

    def cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = desktop_release.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def assets(self):
        return {path.name for path in self.downloads.iterdir()
                if path.suffix in {".dmg", ".zip", ".exe"}}

    def pin(self, text, name="desktop-release-public-key", mode=0o644):
        folder = self.root / "etc-waam"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text(text + "\n", encoding="ascii")
        os.chmod(path, mode)

        self.addCleanup(lambda: path.exists() and os.chmod(path, 0o644))
        return path

    def test_release_message_golden_vector(self):
        self.assertEqual(
            desktop_release.release_message("windows-x64", "1.2.3", 100, "a" * 64),
            b"waam-studio-release-v1\n"
            b"platform=windows-x64\n"
            b"version=1.2.3\n"
            b"size=100\n"
            b"sha256=" + b"a" * 64 + b"\n")
        cases = (
            ("linux-x64", "1.2.3", 100, "a" * 64),
            ("windows-x64", "1.2", 100, "a" * 64),
            ("windows-x64", "1.2.3", 0, "a" * 64),
            ("windows-x64", "1.2.3", True, "a" * 64),
            ("windows-x64", "1.2.3", 2**53, "a" * 64),
            ("windows-x64", "1.2.3", 100, "A" * 64),
            ("windows-x64", "1.2.3", 100, "a" * 63),
        )
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                desktop_release.release_message(*args)

    def test_signature_and_public_key_arguments_decode_strictly(self):
        raw = bytes(range(64))
        self.assertEqual(desktop_release.read_signature(FAKE_SIGNATURE), FAKE_SIGNATURE)
        saved = self.root / "release.exe.sig"

        for body in (FAKE_SIGNATURE.encode(), FAKE_SIGNATURE.encode() + b"\r\n",
                     b"\xef\xbb\xbf" + FAKE_SIGNATURE.encode() + b"\n"):
            with self.subTest(file=body[:4]):
                saved.write_bytes(body)
                self.assertEqual(desktop_release.read_signature(str(saved)), FAKE_SIGNATURE)

        bad = (
            ("", "must not be empty"),
            ("not base64 at all!", "standard padded base64"),
            (FAKE_SIGNATURE.rstrip("="), "standard padded base64"),
            (base64.urlsafe_b64encode(bytes([251] * 64)).decode(), "standard padded base64"),
            (FAKE_SIGNATURE[:-3] + "B==", "standard padded base64"),
            (base64.b64encode(raw[:63]).decode(), "decodes to 63 bytes; expected exactly 64"),
            (base64.b64encode(raw + b"x").decode(), "decodes to 65 bytes; expected exactly 64"),
            (base64.b64encode(raw[:32]).decode(), "decodes to 32 bytes; expected exactly 64"),
        )
        for value, message in bad:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                desktop_release.read_signature(value)
        for body, message in ((b"\x00" * 64, "standard padded base64"),
                              (b"\xff\xfe", "must hold base64 text"),
                              (b"A" * (desktop_release.MAX_KEY_FILE_BYTES + 1), "too large")):
            with self.subTest(file=body[:4]), self.assertRaisesRegex(ValueError, message):
                saved.write_bytes(body)
                desktop_release.read_signature(str(saved))

        key = base64.b64encode(bytes(range(32))).decode()
        self.assertEqual(desktop_release.read_public_key(key), bytes(range(32)))
        (self.root / "release.pub").write_text(key + "\n", encoding="ascii")
        self.assertEqual(desktop_release.read_public_key(str(self.root / "release.pub")),
                         bytes(range(32)))
        for value in (base64.b64encode(bytes(31)).decode(), base64.b64encode(bytes(33)).decode(),
                      FAKE_SIGNATURE):
            with self.subTest(key=value), self.assertRaisesRegex(ValueError, "expected exactly 32"):
                desktop_release.read_public_key(value)

    def test_publish_requires_a_strict_version_before_hashing(self):
        installer = self.source("WAAM-Studio-setup.exe", b"installer")
        self.assertEqual(desktop_release.publish(installer, "0.2.0")["version"], "0.2.0")
        for version in ("0.2", "v0.2.0", "01.2.3", "0.2.0-beta", "1.02.3", "1.2.3.4",
                        "1..3", "-1.2.3", "1.2.3+build", "", "１.2.3",
                        "9007199254740992.0.0"):
            with self.subTest(version=version), \
                    mock.patch.object(desktop_release, "_hash_file") as hashed, \
                    self.assertRaisesRegex(ValueError, "--version"):
                desktop_release.publish(installer, version)
            hashed.assert_not_called()
        code, _out, err = self.cli("publish", str(installer), "--version", "v0.3.0")
        self.assertEqual(code, 1)
        self.assertIn("MAJOR.MINOR.PATCH", err)
        self.assertEqual(desktop_release.load(platform="windows-x64")["version"], "0.2.0")

    def test_signed_publish_verifies_against_the_public_key(self):
        private, public = self.key_pair()
        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"installer" * 1000)
        signature = self.sign(private, installer, "windows-x64", "0.2.0")
        sig_file = Path(str(installer) + ".sig")
        sig_file.write_text(signature + "\n", encoding="ascii")

        code, out, err = self.cli("publish", str(installer), "--platform", "windows-x64",
                                  "--version", "0.2.0", "--signature", str(sig_file),
                                  "--public-key", public)
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        self.assertIn("release signature verified against --public-key", err)
        published = json.loads(out)
        self.assertEqual(published["signature"], signature)
        loaded = desktop_release.load(platform="windows-x64")
        self.assertEqual(loaded, published)

        message = desktop_release.release_message(
            "windows-x64", loaded["version"], loaded["size"], loaded["sha256"])
        private.public_key().verify(base64.b64decode(loaded["signature"]), message)

        code, out, err = self.cli("show")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["windows-x64"]["signature"], signature)
        self.assertIn("windows-x64 0.2.0: release signature present", err)

        archive = self.source("WAAM-Studio-0.2.0-arm64.zip", b"mac update" * 500)
        mac = desktop_release.publish(
            archive, "0.2.0", signature=self.sign(private, archive, "macos-arm64", "0.2.0"),
            public_key=base64.b64decode(public))
        self.assertEqual(desktop_release.load_all(), {"macos-arm64": mac, "windows-x64": loaded})

    def test_signature_mismatches_are_refused_and_leave_the_current_release(self):
        private, public = self.key_pair()
        other, other_public = self.key_pair()
        current = desktop_release.publish(self.source("current-setup.exe", b"current"), "0.1.0")
        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"candidate build")
        tampered = self.source("tampered-setup.exe", b"candidate build!")
        cases = {
            "other key": (installer, self.sign(other, installer, "windows-x64", "0.2.0"), public),
            "wrong public key": (installer, self.sign(private, installer, "windows-x64", "0.2.0"),
                                 other_public),
            "tampered version": (installer, self.sign(private, installer, "windows-x64", "0.2.1"),
                                 public),
            "wrong platform": (installer, self.sign(private, installer, "macos-arm64", "0.2.0"),
                               public),
            "tampered file": (tampered, self.sign(private, installer, "windows-x64", "0.2.0"),
                              public),
        }
        for label, (path, signature, key) in cases.items():
            with self.subTest(label):
                code, out, err = self.cli("publish", str(path), "--platform", "windows-x64",
                                          "--version", "0.2.0", "--signature", signature,
                                          "--public-key", key)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("does not verify against --public-key", err)
                self.assertEqual(desktop_release.load(platform="windows-x64"), current)
                self.assertEqual(self.assets(), {current["file"]})

    def test_a_pinned_key_verifies_signed_publishes(self):
        private, public = self.key_pair()
        pinned = self.pin(public)
        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"installer" * 100)
        signature = self.sign(private, installer, "windows-x64", "0.2.0")
        with mock.patch.dict(os.environ, {desktop_release.PUBLIC_KEY_FILE_ENV: str(pinned)}):
            code, out, err = self.cli("publish", str(installer), "--version", "0.2.0",
                                      "--signature", signature)
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        self.assertIn(f"verified against the pinned release public key {pinned}", err)
        self.assertEqual(json.loads(out)["signature"], signature)


        archive = self.source("WAAM-Studio-0.2.0-arm64.zip", b"mac update")
        with mock.patch.object(desktop_release, "DEFAULT_PUBLIC_KEY_FILE", str(pinned)):
            code, out, err = self.cli(
                "publish", str(archive), "--version", "0.2.0", "--public-key", public,
                "--signature", self.sign(private, archive, "macos-arm64", "0.2.0"))
        self.assertEqual(code, 0, err)
        self.assertIn("verified against --public-key", err)
        self.assertEqual(list(desktop_release.load_all()), ["macos-arm64", "windows-x64"])

    def test_pinned_key_refusals_leave_the_current_release(self):
        private, public = self.key_pair()
        other, other_public = self.key_pair()
        pinned = self.pin(public)
        malformed = self.pin("not a key", name="malformed")
        missing = pinned.parent / "missing"
        current = desktop_release.publish(self.source("current-setup.exe", b"current"), "0.1.0")
        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"candidate build")
        good = self.sign(private, installer, "windows-x64", "0.2.0")
        foreign = self.sign(other, installer, "windows-x64", "0.2.0")
        cases = (

            ("differs", pinned, ("--signature", foreign, "--public-key", other_public),
             "--public-key differs from the pinned release public key"),
            ("other key", pinned, ("--signature", foreign),
             "does not verify against the pinned release public key"),
            ("missing", missing, ("--signature", good, "--public-key", public),
             "which does not exist"),
            ("malformed", malformed, ("--signature", good), "standard padded base64"),
        )
        for label, key_file, arguments, message in cases:
            with self.subTest(label), mock.patch.dict(
                    os.environ, {desktop_release.PUBLIC_KEY_FILE_ENV: str(key_file)}):
                code, out, err = self.cli("publish", str(installer), "--platform", "windows-x64",
                                          "--version", "0.2.0", *arguments)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(message, err)
                self.assertEqual(desktop_release.load(platform="windows-x64"), current)
                self.assertEqual(self.assets(), {current["file"]})


        with mock.patch.dict(os.environ, {desktop_release.PUBLIC_KEY_FILE_ENV: str(missing)}):
            code, out, err = self.cli("publish", str(installer), "--version", "0.2.0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("signature", json.loads(out))
        self.assertIn("published without a release signature", err)

    def test_pinned_key_file_location_and_permissions(self):
        default = desktop_release.DEFAULT_PUBLIC_KEY_FILE
        self.assertEqual(desktop_release.pinned_public_key_file(), (Path(default), False))
        with mock.patch.dict(os.environ, {desktop_release.PUBLIC_KEY_FILE_ENV: ""}):
            self.assertEqual(desktop_release.pinned_public_key_file(), (Path(default), False))
        with mock.patch.dict(os.environ, {desktop_release.PUBLIC_KEY_FILE_ENV: "/srv/key"}):
            self.assertEqual(desktop_release.pinned_public_key_file(), (Path("/srv/key"), True))

        absent = self.root / "absent"
        self.assertIsNone(desktop_release.read_pinned_public_key(absent, required=False))
        with self.assertRaisesRegex(ValueError, "which does not exist"):
            desktop_release.read_pinned_public_key(absent, required=True)
        key = bytes(range(32))
        pinned = self.pin(base64.b64encode(key).decode("ascii"), mode=0o444)

        with mock.patch.object(desktop_release, "_CHECK_KEY_FILE_MODE", True):
            self.assertEqual(desktop_release.read_pinned_public_key(pinned, required=False), key)
            os.chmod(pinned, 0o666)
            with self.assertRaisesRegex(ValueError, "must not be group- or world-writable"):
                desktop_release.read_pinned_public_key(pinned, required=False)

        with self.assertRaisesRegex(ValueError, "pinned release public key .* (cannot be read|"
                                                "is not a regular file)"):
            desktop_release.read_pinned_public_key(pinned.parent, required=False)

    def test_unsigned_publishes_warn_and_unverified_signatures_are_refused(self):
        installer = self.source("WAAM-Studio-0.2.0-win-x64-setup.exe", b"installer")
        code, out, err = self.cli("publish", str(installer), "--version", "0.2.0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("signature", json.loads(out))
        self.assertIn("published without a release signature", err)
        self.assertIn("installed manually", err)
        current = desktop_release.load(platform="windows-x64")


        installer.write_bytes(b"next installer")
        code, out, err = self.cli("publish", str(installer), "--version", "0.2.1",
                                  "--signature", FAKE_SIGNATURE)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("must verify before it is published: pass --public-key", err)
        self.assertIn(desktop_release.PUBLIC_KEY_FILE_ENV, err)
        self.assertEqual(desktop_release.load(platform="windows-x64"), current)
        self.assertEqual(self.assets(), {current["file"]})

    def test_signed_size_is_capped_at_the_largest_javascript_integer(self):
        limit = 2**53 - 1
        message = "at most 9007199254740991 bytes"
        self.assertIn(b"\nsize=9007199254740991\n",
                      desktop_release.release_message("windows-x64", "1.2.3", limit, "a" * 64))

        private, public = self.key_pair()
        installer = self.source("setup.exe", b"installer")
        with mock.patch.object(desktop_release, "_hash_file", return_value=(limit + 1, "a" * 64)), \
                mock.patch.object(desktop_release, "_copy_and_hash") as copied, \
                self.assertRaisesRegex(ValueError, message):
            desktop_release.publish(installer, "0.2.0", public_key=base64.b64decode(public),
                                    signature=self.sign(private, installer, "windows-x64", "0.2.0"))
        copied.assert_not_called()
        self.assertEqual(self.assets(), set())

        target = desktop_release.PLATFORMS["windows-x64"]
        manifest = {
            "version": "0.2.0", "file": "setup.exe", "size": limit + 1, "sha256": "a" * 64,
            "platform": target.label, "signed": target.signed, "notarized": False,
            "published_at": "2026-10-12T14:30:00+00:00", "notes": "",
        }

        self.assertEqual(desktop_release._manifest(manifest, "windows-x64")["size"], limit + 1)
        signed = dict(manifest, signature=FAKE_SIGNATURE)
        with self.assertRaisesRegex(ValueError, message):
            desktop_release._manifest(signed, "windows-x64")
        (self.downloads / target.manifest).write_text(json.dumps(signed), encoding="utf-8")
        with self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            self.assertIsNone(desktop_release.load(platform="windows-x64"))
        self.assertIn(message, " ".join(logged.output))

    def test_signature_shape_and_artifact_are_checked_before_hashing(self):
        dmg = self.source("WAAM-Studio-0.2.0-arm64.dmg", b"disk image")
        installer = self.source("setup.exe", b"installer")
        cases = (
            (dmg, dict(signature=FAKE_SIGNATURE), "only for the .zip update artifact"),
            (installer, dict(signature=FAKE_SIGNATURE[:-4]), "--signature"),
            (installer, dict(public_key=bytes(32)), "--public-key needs --signature"),
            (installer, dict(signature=FAKE_SIGNATURE), "must verify before it is published"),
        )
        for path, kwargs, message in cases:
            with self.subTest(message=message), \
                    mock.patch.object(desktop_release, "_hash_file") as hashed, \
                    self.assertRaisesRegex(ValueError, message):
                desktop_release.publish(path, "0.2.0", **kwargs)
            hashed.assert_not_called()
        self.assertEqual(self.assets(), set())

        self.assertNotIn("signature", desktop_release.publish(dmg, "0.2.0"))

    def test_public_key_verification_needs_cryptography(self):
        installer = self.source("setup.exe", b"installer")
        blocked = dict.fromkeys(("cryptography", "cryptography.exceptions",
                                 "cryptography.hazmat.primitives.asymmetric.ed25519"))
        with mock.patch.dict(sys.modules, blocked), \
                mock.patch.object(desktop_release, "_hash_file") as hashed, \
                self.assertRaisesRegex(ValueError, "needs the 'cryptography' package"):
            desktop_release.publish(installer, "0.2.0", signature=FAKE_SIGNATURE,
                                    public_key=bytes(32))
        hashed.assert_not_called()
        script = ("import sys; sys.modules['cryptography'] = None\n"
                  "from waam_pipeline import desktop_release, server\n")
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsigned_manifests_already_on_the_vm_still_load_and_show(self):
        mac_body, windows_body = b"0.1.0 disk image", b"0.1.0 installer"
        for platform, name, body in (
                ("macos-arm64", "WAAM-Studio-0.1.0-arm64.dmg", mac_body),
                ("windows-x64", "WAAM-Studio-0.1.0-win-x64-setup.exe", windows_body)):
            target = desktop_release.PLATFORMS[platform]
            digest = hashlib.sha256(body).hexdigest()
            asset = self.downloads / f"{digest[:16]}-{name}"
            asset.write_bytes(body)
            fixed = 1_790_000_000_000_000_000
            os.utime(asset, ns=(fixed, fixed))
            (self.downloads / target.manifest).write_text(json.dumps({
                "version": "0.1.0", "file": asset.name, "size": len(body), "sha256": digest,
                "platform": target.label, "signed": target.signed, "notarized": False,
                "published_at": "2026-10-11T14:30:00+00:00", "notes": "Development build",
                "mtime_ns": asset.stat().st_mtime_ns,
            }, indent=2) + "\n", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.downloads.iterdir()}
        releases = desktop_release.load_all()
        self.assertEqual(list(releases), ["macos-arm64", "windows-x64"])
        for release in releases.values():
            self.assertEqual(release["version"], "0.1.0")
            self.assertNotIn("signature", release)
        with desktop_release.open_release(platform="windows-x64") as opened:
            self.assertEqual(opened.source.read(), windows_body)
        code, out, err = self.cli("show")
        self.assertEqual((code, json.loads(out)), (0, releases))
        self.assertIn("macos-arm64 0.1.0: not signed - install manually", err)
        self.assertIn("windows-x64 0.1.0: not signed - install manually", err)
        self.assertEqual({path.name: path.read_bytes() for path in self.downloads.iterdir()},
                         before)

    def test_manifest_on_disk_with_a_bad_signature_is_not_served(self):
        private, public = self.key_pair()
        installer = self.source("setup.exe", b"installer")
        release = desktop_release.publish(
            installer, "0.2.0", signature=self.sign(private, installer, "windows-x64", "0.2.0"),
            public_key=base64.b64decode(public))
        self.assertEqual(desktop_release.load(platform="windows-x64"), release)
        manifest = self.downloads / desktop_release.PLATFORMS["windows-x64"].manifest
        cases = (
            dict(signature=FAKE_SIGNATURE[:-2]),
            dict(signature=base64.b64encode(bytes(63)).decode()),
            dict(signature=None),
            dict(version="0.2"),
        )
        for changes in cases:
            with self.subTest(changes=changes):
                manifest.write_text(json.dumps(dict(release, **changes)), encoding="utf-8")
                with self.assertLogs(desktop_release._LOG, level="WARNING"):
                    self.assertIsNone(desktop_release.load(platform="windows-x64"))

        unsigned = {key: value for key, value in release.items() if key != "signature"}
        manifest.write_text(json.dumps(dict(unsigned, version="0.2")), encoding="utf-8")
        self.assertEqual(desktop_release.load(platform="windows-x64")["version"], "0.2")

        archive = self.source("mac.dmg", b"disk image")
        mac = desktop_release.publish(archive, "0.2.0")
        mac_manifest = self.downloads / desktop_release.MANIFEST_NAME
        mac_manifest.write_text(json.dumps(dict(mac, signature=FAKE_SIGNATURE)), encoding="utf-8")
        with self.assertLogs(desktop_release._LOG, level="WARNING") as logged:
            self.assertIsNone(desktop_release.load())
        self.assertIn("signed macos-arm64 release must be a .zip", " ".join(logged.output))


class TestReleaseExtra(unittest.TestCase):
    def test_cryptography_is_declared_by_the_release_extra(self):

        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        extras = text.split("[project.optional-dependencies]\n", 1)[1].split("\n[", 1)[0]
        declared = dict(re.findall(r"^([a-z]+) = \[(.*)\]$", extras, re.M))

        self.assertRegex(declared["passkeys"], r'^"webauthn>=[0-9]+\.[0-9]+"$')
        self.assertRegex(declared["release"], r'^"cryptography>=[0-9]+\.[0-9]+"$')
        requirement = declared["release"].strip('"')
        self.assertIn("-e .[test,release]",
                      (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines())

        self.assertIn(f"/srv/waam/venv/bin/pip install '{requirement}'",
                      (ROOT / "docs" / "desktop-download.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
