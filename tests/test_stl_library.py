import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests.test_auth import _AuthServerCase, GOOGLE_CFG, http
from tests.test_server import binary_stl, cylinder
from waam_pipeline import job_archive, oidc, server, stl_library as library

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
ADMIN = "admin@example.invalid"
METRES = binary_stl(cylinder(radius=.0215, height=.01, around=8))


class TestLibraryStorage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"

    def put(self, email=ALICE, body=METRES, **kwargs):
        return library.preserve(self.root, email, body, name="part.stl", **kwargs)

    def test_auto_units_and_override_are_retained_with_exact_original(self):
        entry = self.put()
        self.assertEqual(entry["inspection"]["resolved_units"], "m")
        self.assertAlmostEqual(entry["inspection"]["size_mm"][0], 43, places=3)
        self.assertAlmostEqual(entry["inspection"]["size_mm"][2], 10, places=3)
        changed = library.set_units(self.root, ALICE, entry["id"], "mm")
        self.assertAlmostEqual(changed["inspection"]["size_mm"][2], .01, places=5)
        self.assertEqual(library.listing(self.root, ALICE)["entries"][0]["units"], "mm")
        body, reread = library.read(self.root, library.workspace_for(ALICE), entry["id"], email=ALICE)
        self.assertEqual(body, METRES)
        self.assertEqual(reread["units"], "mm")

        self.assertEqual(self.put()["units"], "auto")

    def test_per_account_namespaces_even_with_identical_bytes_and_legacy(self):
        a = self.put()
        b = self.put(BOB)
        legacy = job_archive.preserve(self.root, "shared-team", "stl", METRES, name="old.stl")
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["source_owner"], b["source_owner"])
        for email in (ALICE, BOB):
            rows = library.listing(self.root, email)["entries"]
            self.assertEqual(len([r for r in rows if r["kind"] == "stl"]), 1)
            self.assertTrue(all(r["owner"] == email for r in rows))
        with self.assertRaises(ValueError):
            library.read(self.root, b["source_owner"], b["id"], email=ALICE)
        all_rows = library.listing(self.root, ADMIN, all_users=True)["entries"]
        self.assertEqual(len([r for r in all_rows if r["kind"] == "stl"]), 3)
        self.assertEqual([r["owner"] for r in all_rows if r.get("legacy")], [None])
        copied = library.copy_into(self.root, ADMIN, "shared-team", legacy.parent.name)
        self.assertEqual(copied["owner"], ADMIN)
        self.assertEqual(legacy.read_bytes(), METRES)

    def test_starter_entries_need_no_get_writes_and_have_fixed_shapes(self):
        rows = library.listing(self.root, ALICE)["entries"]
        self.assertEqual({r["shape"] for r in rows}, set(server.demos.DEMOS))
        self.assertTrue(all(r["id"] == "sample:" + r["shape"] for r in rows))
        self.assertFalse(self.root.exists())

    def test_concurrent_uploads_dedupe_and_global_archive_quota_applies(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            rows = list(pool.map(lambda _: self.put(), range(8)))
        self.assertEqual(len({r["id"] for r in rows}), 1)
        self.assertEqual(len(list(self.root.rglob("metadata.json"))), 1)
        before = job_archive._usage(self.root)
        with patch.object(job_archive, "MAX_TOTAL_BYTES", before[0]):
            with self.assertRaisesRegex(ValueError, "storage limit"):
                self.put(BOB)
        self.assertEqual(job_archive._usage(self.root), before)

    def test_invalid_stl_retained_with_actionable_inspection_and_limits(self):
        entry = self.put(body=b"not an stl")
        self.assertIn("Export a binary or ASCII STL", entry["inspection_error"])
        self.assertEqual(library.read(self.root, entry["source_owner"], entry["id"])[0], b"not an stl")
        with self.assertRaises(ValueError):
            self.put(units="cm")
        with patch.object(library, "MAX_STL_BYTES", 1):
            with self.assertRaises(ValueError):
                self.put()

    def test_units_write_failure_is_atomic(self):
        entry = self.put()
        with patch.object(library.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                library.set_units(self.root, ALICE, entry["id"], "m")
        self.assertEqual(library.read(self.root, entry["source_owner"], entry["id"])[1]["units"], "auto")
        self.assertFalse(list(self.root.rglob(".units-*")))

    def test_integrity_and_unsafe_ids_are_refused(self):
        entry = self.put()
        path = self.root / entry["source_owner"] / ".artifacts" / "stl" / entry["id"] / "part.stl"
        path.write_bytes(b"X" + METRES[1:])
        with self.assertRaisesRegex(ValueError, "integrity"):
            library.read(self.root, entry["source_owner"], entry["id"])
        for entry_id in ("../part", "A" * 64, "", "a" * 65):
            with self.subTest(entry_id=entry_id), self.assertRaises(ValueError):
                library.read(self.root, entry["source_owner"], entry_id)
        for workspace in ("../escape", "x/y", "", "a" * 81):
            with self.assertRaises(ValueError):
                library.read(self.root, workspace, entry["id"])

    def test_malformed_metadata_never_claims_legacy_as_current_user(self):
        entry = self.put()
        path = self.root / entry["source_owner"] / ".artifacts" / "stl" / entry["id"] / "metadata.json"
        record = json.loads(path.read_bytes())
        for attributes in (None, [], {"owner": BOB, "schema": 1}, {"owner": ALICE, "schema": 1, "units": []}):
            with self.subTest(attributes=attributes):
                path.write_text(json.dumps(dict(record, attributes=attributes)))
                listing = library.listing(self.root, ALICE)
                self.assertEqual(listing["warnings"], 1)
                self.assertFalse(any(r["kind"] == "stl" for r in listing["entries"]))

    def test_hardlinks_are_refused_even_for_matching_bytes(self):
        entry = self.put()
        path = self.root / entry["source_owner"] / ".artifacts" / "stl" / entry["id"] / "part.stl"
        os.link(path, self.root.parent / "alias.stl")
        with self.assertRaises(ValueError):
            library.read(self.root, entry["source_owner"], entry["id"])
        with self.assertRaises(ValueError):
            self.put()

    @unittest.skipUnless(os.name == "posix", "Linux descriptor traversal and FIFO safety")
    def test_links_ancestors_and_fifo_cannot_escape_or_block_reads(self):
        entry = self.put()
        folder = self.root / entry["source_owner"] / ".artifacts" / "stl" / entry["id"]
        source = folder / "part.stl"
        source.unlink()
        outside = self.root.parent / "outside.stl"
        outside.write_bytes(METRES)
        source.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            library.read(self.root, entry["source_owner"], entry["id"])
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(ValueError):
            job_archive.safe_read(source, library.MAX_STL_BYTES)
        source.unlink()
        source.write_bytes(METRES)
        moved = self.root.parent / "moved"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            library.read(self.root, entry["source_owner"], entry["id"])


class TestLibraryHttp(_AuthServerCase):
    CONFIG = dict(GOOGLE_CFG, users={ALICE: {"name": "team"}, BOB: {"name": "team"},
                                   ADMIN: {"name": "team"}}, admins=[ADMIN])
    TOKENS = {"test-library-token": "team"}

    def setUp(self):
        self.write_config(self.CONFIG)
        oidc.forget_everything()
        self.alice = self.cookie(self.sign_in_ok(email=ALICE, email_verified=True))
        self.bob = self.cookie(self.sign_in_ok(email=BOB, email_verified=True))
        self.admin = self.cookie(self.sign_in_ok(email=ADMIN, email_verified=True))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        saves = patch.object(server, "SAVES_DIR", Path(temp.name) / "saves")
        saves.start()
        self.addCleanup(saves.stop)

    def call(self, path="/api/library", *, email=ALICE, cookie=None, method="GET", body=None, **params):
        query = {"owner": library.owner_id(email), **params}
        code, data, headers = http(self.base + path + "?" + urlencode(query), method=method,
                                   data=body, headers=cookie or self.alice)
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        return code, json.loads(data) if headers.get("Content-Type", "").startswith("application/json") else data

    def upload(self, body=METRES, **kwargs):
        code, result = self.call("/api/library/upload", method="POST", body=body, name="part.stl", **kwargs)
        self.assertEqual(code, 200, result)
        return result["entry"]

    def test_shared_workspace_users_cannot_list_read_mutate_or_slice_each_others_original(self):
        a = self.upload()
        for cookie in (self.alice, self.bob):
            code, data, _ = http(self.base + "/api/whoami", headers=cookie)
            self.assertEqual(json.loads(data)["workspace"], "team")
        code, rows = self.call(email=BOB, cookie=self.bob)
        self.assertFalse(any(r["kind"] == "stl" for r in rows["entries"]))
        for path, method, extra in (("/api/library/file", "GET", {"id": a["id"]}),
                                    ("/api/library/units", "POST", {"id": a["id"], "units": "mm"}),
                                    ("/api/slice", "POST", {"library": a["id"]})):
            code, result = self.call(path, email=BOB, cookie=self.bob, method=method,
                                     source_owner=a["source_owner"], **extra)
            self.assertFalse(result["ok"])
        code, body = self.call("/api/library/file", id=a["id"])
        self.assertEqual((code, body), (200, METRES))

    def test_changed_account_rejects_before_upload_even_in_same_workspace(self):
        code, result = self.call("/api/library/upload", cookie=self.bob, method="POST", body=METRES, name="part.stl")
        self.assertEqual(code, 409)
        self.assertIn("account changed", result["error"])
        self.assertFalse(server.SAVES_DIR.exists())

    def test_restart_reslice_uses_persistent_bytes_and_unit_override(self):
        entry = self.upload()
        server._STATES.pop("team", None)
        with patch.object(server, "run_slice", return_value={"ok": True}) as sliced:
            code, result = self.call("/api/slice", method="POST", library=entry["id"], units="m")
        self.assertTrue(result["ok"])
        self.assertEqual(sliced.call_args.args[1], METRES)
        self.assertEqual(sliced.call_args.args[0]["units"], "m")
        self.assertEqual(self.call()[1]["entries"][0]["units"], "m")

    def test_actual_auto_inspection_and_slice_return_dimensions_and_moves(self):
        entry = self.upload()
        code, result = self.call("/api/slice", method="POST", library=entry["id"], units="auto",
                                 cell="0", fixture="0", repair="0", lh="2.9")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stl"]["resolved_units"], "m")
        self.assertGreater(result["moves"], 0)

    def test_admin_all_users_and_legacy_import_rechecks_live_role(self):
        self.upload()
        legacy = job_archive.preserve(server.SAVES_DIR, "unattributed", "stl", METRES, name="legacy.stl")
        code, result = self.call("/api/admin/library", email=ADMIN, cookie=self.admin)
        self.assertEqual(code, 200)
        self.assertEqual(len([r for r in result["entries"] if r["kind"] == "stl"]), 2)
        code, result = self.call("/api/admin/library/import", email=ADMIN, cookie=self.admin, method="POST",
                                 source_owner="unattributed", id=legacy.parent.name)
        self.assertEqual(result["entry"]["owner"], ADMIN)
        self.write_config(dict(self.CONFIG, admins=[]))
        code, _ = self.call("/api/admin/library", email=ADMIN, cookie=self.admin)
        self.assertEqual(code, 403)
        code, _ = self.call("/api/admin/library/import", email=ADMIN, cookie=self.admin,
                            method="POST", source_owner="unattributed", id=legacy.parent.name)
        self.assertEqual(code, 403)

    def test_no_session_token_and_guest_cannot_use_any_library_route(self):
        code, _, headers = http(self.base + "/auth/guest", method="POST")
        self.assertEqual(code, 302)
        guest = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        routes = (("/api/library", "GET"), ("/api/library/file", "GET"),
                  ("/api/library/upload", "POST"), ("/api/library/units", "POST"),
                  ("/api/admin/library", "GET"), ("/api/admin/library/import", "POST"))
        for cookie in ({"Cookie": "waam=test-library-token"}, guest, {"Cookie": "none"}):
            for path, method in routes:
                with self.subTest(cookie=bool(cookie), path=path):
                    code, _ = self.call(path, cookie=cookie, method=method)
                    self.assertIn(code, (401, 403))
        self.assertFalse(server.SAVES_DIR.exists())

    def test_guest_inspection_is_transient_and_bad_reuse_is_actionable(self):
        code, _, headers = http(self.base + "/auth/guest", method="POST")
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        code, data, _ = http(self.base + "/api/slice?kind=stl&inspect=1&units=auto", method="POST", data=METRES, headers=cookie)
        result = json.loads(data)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stl"]["resolved_units"], "m")
        self.assertFalse(server.SAVES_DIR.exists())
        code, data, _ = http(self.base + "/api/slice?kind=stl&reuse=1", method="POST", headers=cookie)
        self.assertIn("Select it from your library or upload", json.loads(data)["error"])

    def test_bad_files_survive_slice_failure_and_errors_do_not_expose_paths(self):
        entry = self.upload(body=b"not a real stl")
        self.assertIn("inspection_error", entry)
        code, result = self.call("/api/slice", method="POST", library=entry["id"], cell="0", fixture="0")
        self.assertFalse(result["ok"])
        self.assertEqual(self.call("/api/library/file", id=entry["id"])[1], b"not a real stl")
        with patch.object(library, "read", side_effect=PermissionError("secret/internal/path")):
            code, result = self.call("/api/slice", method="POST", library=entry["id"])
        self.assertNotIn("secret", result["error"])


if __name__ == "__main__":
    unittest.main()
