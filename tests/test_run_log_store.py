import concurrent.futures
from contextlib import closing
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import mirror_manifest, run_log_store as store, runs


OWNER = "owner@example.invalid"
TEAMMATE = "teammate@example.invalid"
OUTSIDER = "other@example.invalid"


class TestRunLogStore(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("WAAM_RUN_LOG_PG_DSN", None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "saves"
        self.workspace = "owner"

    def create(self, **kwargs):
        options = {"name": "Test plate", "setup": {"specimen_id": "A-12", "travel_mm_min": 300}}
        options.update(kwargs)
        return store.create_run(self.root, self.workspace, OWNER, **options)

    def update(self, detail, action, data, actor=OWNER, **kwargs):
        return store.update_run(self.root, actor, detail["record"]["run"], revision=detail["revision"],
                                action=action, data=data, **kwargs)

    def metadata(self, detail):
        record = detail["record"]
        return self.root / record["workspace"] / "_runs" / record["run"] / f"{record['run']}_run.json"

    def query(self, sql):
        with closing(sqlite3.connect(store.catalog_path(self.root))) as connection:
            return connection.execute(sql).fetchall()

    def test_normalized_sql_preserves_units_measurements_and_audit(self):
        detail = self.create(collaborators=[TEAMMATE.upper()])
        detail = self.update(detail, "layer-row", {
            "layer": 0, "pass": 1, "unit": "F", "pre_weld": 212,
            "readings": {"0": 392, "20": 212}, "height_mm": 2.1, "width_mm": 6,
            "current_a": 135, "voltage_v": 21.5, "height_source": "manual"}, actor=TEAMMATE)
        detail = self.update(detail, "temperature", {"value": 392, "unit": "F", "stage": "after-weld"})
        self.assertEqual(self.query("SELECT pre_weld, pre_weld_value_c, current_a, voltage_v FROM layers"),
                         [(212, 100, 135, 21.5)])
        self.assertEqual(self.query("SELECT seconds, value, unit, value_c FROM layer_readings ORDER BY seconds"),
                         [(0, 392, "F", 200), (20, 212, "F", 100)])
        self.assertEqual(self.query("SELECT value, unit, value_c FROM temperatures"), [(392, "F", 200)])
        self.assertEqual(self.query("SELECT specimen_id, travel_mm_min, wire_diameter_mm FROM setup"),
                         [("A-12", 300, None)])
        self.assertEqual([(item["actor"], item["action"]) for item in detail["record"]["audit"]],
                         [(OWNER, "create"), (TEAMMATE, "layer-row"), (OWNER, "temperature")])
        self.assertEqual(self.query("SELECT actor, action FROM audit ORDER BY event"),
                         [(OWNER, "create"), (TEAMMATE, "layer-row"), (OWNER, "temperature")])
        self.assertEqual(detail["revision"], hashlib.sha256(self.metadata(detail).read_bytes()).hexdigest())

    def test_private_default_explicit_sharing_admin_and_revocation(self):
        detail = self.create()
        run_id = detail["record"]["run"]
        self.assertEqual(store.list_runs(self.root, TEAMMATE), [])
        with self.assertRaises(PermissionError):
            store.get_run(self.root, TEAMMATE, run_id)
        detail = self.update(detail, "setup", {"collaborators": [TEAMMATE]})
        self.assertFalse(store.get_run(self.root, TEAMMATE.upper(), run_id)["can_manage"])
        for action, data in (("setup", {"setup": {"material": "steel"}}), ("finish", {"outcome": "not-run"})):
            with self.assertRaises(PermissionError):
                self.update(detail, action, data, actor=TEAMMATE)
        detail = self.update(detail, "note", {"text": "Measured together"}, actor=TEAMMATE)
        detail = self.update(detail, "setup", {"collaborators": []})
        with self.assertRaises(PermissionError):
            self.update(detail, "note", {"text": "After access revoked"}, actor=TEAMMATE)
        self.assertTrue(store.get_run(self.root, OUTSIDER, run_id, admin=True)["can_manage"])
        self.assertEqual(len(store.list_runs(self.root, OUTSIDER, admin=True)), 1)

    def test_stale_revision_and_simultaneous_requests_never_overwrite(self):
        original = self.create(collaborators=[TEAMMATE])

        def write(actor):
            try:
                return self.update(original, "note", {"text": actor}, actor=actor)
            except store.ConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [OWNER, TEAMMATE]))
        self.assertEqual(results.count("conflict"), 1)
        current = store.get_run(self.root, OWNER, original["record"]["run"])
        self.assertEqual(len(current["record"]["notes"]), 1)
        with self.assertRaises(store.ConflictError):
            self.update(original, "setup", {"setup": {"material": "stale"}})
        self.assertEqual(len(current["record"]["audit"]), 2)

    def test_workbench_changes_are_discovered_and_invalidate_tabs(self):
        detail = self.create()
        runs.add_note(self.root, self.workspace, text="Existing workbench record")
        with self.assertRaises(store.ConflictError):
            self.update(detail, "note", {"text": "Stale browser"})
        latest = store.get_run(self.root, OWNER, detail["record"]["run"])
        self.assertEqual(latest["record"]["notes"][0]["text"], "Existing workbench record")
        self.assertEqual(self.query("SELECT revision FROM runs"), [(latest["revision"],)])

    def test_finished_archive_stays_byte_identical_and_catalog_rebuilds(self):
        detail = self.create()
        detail = self.update(detail, "layer-row", {"layer": 0, "unit": "C", "current_a": 123,
                                                   "voltage_v": 20, "readings": {"20": 100}})
        detail = self.update(detail, "finish", {"outcome": "completed", "arcmon_waiver": True,
                                                "waiver_reason": "Logger not available", "height_mm": 2})
        folder = self.metadata(detail).parent
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        run_id = detail["record"]["run"]
        seal = json.loads(before[f"{run_id}_SEALED.json"])
        for item in seal["files"]:
            self.assertEqual(hashlib.sha256(before[item["name"]]).hexdigest(), item["sha256"])
        self.assertEqual(detail["record"]["audit"][-1]["action"], "finish")
        rows = list(csv.DictReader(io.StringIO(before[f"{run_id}_layer_log.csv"].decode())))
        self.assertEqual(float(rows[0]["current_a"]), 123)
        self.assertEqual(float(rows[0]["voltage_v"]), 20)
        store.catalog_path(self.root).unlink()
        restored = store.get_run(self.root, OWNER, run_id)
        self.assertEqual(restored, detail)
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})
        with self.assertRaisesRegex(ValueError, "finished"):
            self.update(restored, "note", {"text": "Too late"})
        manifest = mirror_manifest.build(self.root)
        self.assertTrue(manifest["files"])
        self.assertFalse(any(store.CATALOG_NAME in item["src"] for item in manifest["files"]))

    def test_new_process_reads_persisted_records(self):
        detail = self.create()
        script = ("import json,sys; from pathlib import Path; from waam_pipeline import run_log_store as s; "
                  "print(json.dumps(s.get_run(Path(sys.argv[1]),sys.argv[2],sys.argv[3])))")
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, "-c", script, str(self.root), OWNER, detail["record"]["run"]],
                                capture_output=True, text=True, timeout=20, env=environment, check=True)
        self.assertEqual(json.loads(result.stdout), detail)

    def test_validation_leaves_no_partial_run_or_audit(self):
        for setup in ({"travel_mm_min": float("nan")}, {"wire_feed_m_min": -1}, {"voltage_v": True},
                      {"ctwd_mm": "9"}, {"material": "x" * 121}, {"unknown": 1}):
            with self.subTest(setup=setup), self.assertRaises(ValueError):
                self.create(setup=setup)
        self.assertIsNone(runs.active(self.root, self.workspace))
        detail = self.create()
        before = self.metadata(detail).read_bytes()
        for action, data in (("finish", {"outcome": "completed"}),
                             ("layer-row", {"layer": 0, "unit": "C", "current_a": float("inf")}),
                             ("layer-row", {"layer": 0, "unit": "C", "voltage_v": -1}),
                             ("layer-row", {"layer": 10 ** 30, "unit": "C"}),
                             ("layer-row", {"layer": 0, "pass": 10 ** 30, "unit": "C"}),
                             ("temperature", {"value": 100, "unit": "C", "stage": "other", "cycle": 10 ** 30}),
                             ("temperature", {"unit": "C", "stage": "other", "value": True}),
                             ("setup", {"collaborators": ["not an email"]})):
            with self.subTest(action=action, data=data), self.assertRaises(ValueError):
                self.update(detail, action, data)
            self.assertEqual(self.metadata(detail).read_bytes(), before)

    def test_attachment_inventory_audit_and_invalid_arc_monitor(self):
        detail = self.create(collaborators=[TEAMMATE])
        detail = self.update(detail, "add", {"name": "notes.txt", "body": b"measurements", "kind": "ATTACH"},
                             actor=TEAMMATE)
        item = detail["record"]["files"][0]
        self.assertEqual(item["sha256"], hashlib.sha256(b"measurements").hexdigest())
        self.assertEqual(detail["record"]["audit"][-1]["actor"], TEAMMATE)
        before = self.metadata(detail).read_bytes()
        with self.assertRaises(ValueError):
            self.update(detail, "add", {"name": "junk.csv", "body": b"not arc data", "kind": "ARCMON"})
        self.assertEqual(self.metadata(detail).read_bytes(), before)

    def test_unknown_legacy_fields_survive_edits_and_seal(self):
        detail = self.create()
        record = detail["record"]
        record["old_lab_metadata"] = {"preserve": [1, 2]}
        record["setup"]["historical_setting"] = "unknown but preserved"
        self.metadata(detail).write_bytes(runs._json(record))
        detail = store.get_run(self.root, OWNER, record["run"])
        detail = self.update(detail, "setup", {"setup": {"material": "steel"}})
        detail = self.update(detail, "layer-row", {"layer": 0, "unit": "C", "current_a": 123})

        runs.set_layer_row(self.root, self.workspace, {"layer": 0, "unit": "C", "height_mm": 2})
        detail = store.get_run(self.root, OWNER, record["run"])
        self.assertEqual(detail["record"]["layer_log"][0]["current_a"], 123)
        detail = self.update(detail, "finish", {"outcome": "not-run"})
        self.assertEqual(detail["record"]["old_lab_metadata"], {"preserve": [1, 2]})
        self.assertEqual(detail["record"]["setup"]["historical_setting"], "unknown but preserved")

    def test_literal_search_and_status_filters(self):
        detail = self.create(name="O'Brien plate")
        self.assertEqual(len(store.list_runs(self.root, OWNER, search="a-12", status="open")), 1)
        self.assertEqual(store.list_runs(self.root, OWNER, search="' OR 1=1 --"), [])
        self.assertEqual(store.list_runs(self.root, OWNER, search="%"), [])
        self.assertEqual(store.list_runs(self.root, OWNER, status="finished"), [])
        self.update(detail, "finish", {"outcome": "not-run"})
        self.assertEqual(len(store.list_runs(self.root, OWNER, status="finished")), 1)

    def test_excel_report_projections_scope_units_and_empty_headers(self):
        detail = self.create(collaborators=[TEAMMATE])
        detail = self.update(detail, "layer-row", {"layer": 0, "pass": 1, "unit": "F",
                                                   "pre_weld": 212, "readings": {"20": 392},
                                                   "current_a": 123, "voltage_v": 20})
        detail = self.update(detail, "temperature", {"value": 212, "unit": "F", "stage": "other"})
        store.create_run(self.root, "outsider", OUTSIDER, name="Private to someone else")
        for dataset in ("runs", "layers", "cooling", "temperatures"):
            with self.subTest(dataset=dataset):
                rows = store.reporting_rows(self.root, TEAMMATE.upper(), dataset)
                self.assertEqual(len(rows), 1)
                self.assertEqual(tuple(rows[0]), store.reporting_fields(dataset))
                self.assertEqual(rows[0]["run"], detail["record"]["run"])
                self.assertFalse({"source_json", "workspace", "owner_key", "collaborators", "audit"} & set(rows[0]))
        self.assertEqual(store.reporting_rows(self.root, TEAMMATE, "layers")[0]["pre_weld_value_c"], 100)
        self.assertEqual(store.reporting_rows(self.root, TEAMMATE, "cooling")[0]["value_c"], 200)
        self.assertEqual(store.reporting_rows(self.root, "'; DROP TABLE runs;--", "runs"), [])
        detail = self.update(detail, "setup", {"collaborators": []})
        self.assertEqual(store.reporting_rows(self.root, TEAMMATE, "runs"), [])
        self.assertIn("setup_ctwd_mm", store.reporting_fields("runs"))
        with self.assertRaises(ValueError):
            store.reporting_rows(self.root, OWNER, "runs; DROP TABLE runs;")

    def test_sql_refresh_is_transactional_when_source_is_invalid(self):
        detail = self.create()
        second = store.create_run(self.root, "z-other", OUTSIDER, name="Other")
        before = self.query("SELECT run_id, revision FROM runs ORDER BY run_id")
        runs.add_note(self.root, self.workspace, text="First source changed")
        self.metadata(second).write_bytes(b"broken JSON")
        with self.assertRaises(ValueError):
            store.refresh_catalog(self.root)
        self.assertEqual(self.query("SELECT run_id, revision FROM runs ORDER BY run_id"), before)
        self.assertIn("First source changed", self.metadata(detail).read_text())

    def test_catalog_failure_is_explicit_even_after_archive_write(self):
        detail = self.create()
        with patch.object(store, "refresh_catalog", side_effect=store.CatalogError("disk full")):
            with self.assertRaisesRegex(store.CatalogError, "Run saved.*Reload"):
                self.update(detail, "note", {"text": "Safely in archive"})
        current = store.get_run(self.root, OWNER, detail["record"]["run"])
        self.assertEqual(current["record"]["notes"][-1]["text"], "Safely in archive")
        with self.assertRaises(store.ConflictError):
            self.update(detail, "note", {"text": "Duplicate retry"})

    def test_database_corruption_refuses_without_mutating_archive(self):
        detail = self.create()
        before = self.metadata(detail).read_bytes()
        store.catalog_path(self.root).write_bytes(b"broken SQLite")
        with self.assertRaises(store.CatalogError):
            self.update(detail, "note", {"text": "Cannot save with broken catalog"})
        self.assertEqual(self.metadata(detail).read_bytes(), before)
        store.catalog_path(self.root).unlink()
        self.assertEqual(store.get_run(self.root, OWNER, detail["record"]["run"]), detail)

    def test_legacy_oversized_integer_refuses_cleanly_without_rewriting_archive(self):
        detail = self.create()
        record = detail["record"]
        record["layer_log"] = [{"layer": 10 ** 30, "unit": "C", "readings": {}}]
        self.metadata(detail).write_bytes(runs._json(record))
        before = self.metadata(detail).read_bytes()
        with self.assertRaisesRegex(ValueError, "archived layer.*range"):
            store.refresh_catalog(self.root)
        self.assertEqual(self.metadata(detail).read_bytes(), before)

    def test_database_hardlink_is_refused(self):
        self.root.mkdir()
        target = self.root.parent / "private.db"
        target.write_bytes(b"private")
        try:
            os.link(target, self.root / store.CATALOG_NAME)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "unlinked"):
            store.refresh_catalog(self.root)
        self.assertEqual(target.read_bytes(), b"private")

    def test_database_and_archive_symlinks_are_refused(self):
        detail = self.create()
        target = self.root.parent / "private"
        target.write_bytes(b"private")
        database = store.catalog_path(self.root)
        database.unlink()
        try:
            database.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            store.refresh_catalog(self.root)
        database.unlink()
        metadata = self.metadata(detail)
        metadata.unlink()
        metadata.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            store.refresh_catalog(self.root)
        self.assertEqual(target.read_bytes(), b"private")


if __name__ == "__main__":
    unittest.main()
