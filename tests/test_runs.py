import concurrent.futures
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline import mirror_manifest, runs


NOW = dt.datetime(2026, 10, 2, 21, 4, tzinfo=dt.timezone(dt.timedelta(hours=-5)))


class TestRuns(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"
        self.ws = "account-a"

    def start(self, **kwargs):
        values = {"operator": "operator@example.invalid", "name": None,
                  "part_hint": "WALL10", "now": NOW, "commit": "abc123"}
        values.update(kwargs)
        return runs.start(self.root, self.ws, **values)

    def add(self, **kwargs):
        values = {"kind": "ATTACH", "body": b"notes\r\n", "detail": "notes.txt", "ext": ".txt",
                  "source": "operator", "operator": "operator@example.invalid", "now": NOW}
        values.update(kwargs)
        return runs.add_file(self.root, self.ws, **values)

    def finish(self, **kwargs):
        values = {"outcome": "completed", "waiver": True, "by": "operator@example.invalid", "now": NOW}
        values.update(kwargs)
        return runs.finish(self.root, self.ws, **values)

    def folder(self, record=None):
        record = record or runs.active(self.root, self.ws)
        return self.root / self.ws / "_runs" / record["run"]

    def test_id_and_unique_sequence_across_workspaces(self):
        first = self.start()
        second = runs.start(self.root, "account-b", operator="b", name=None, part_hint="wall10", now=NOW)
        self.assertEqual(first["run"], "2026-10-02_WALL10_r01")
        self.assertEqual(second["run"], "2026-10-02_WALL10_r02")
        self.finish(outcome="not-run", waiver=None)
        third = self.start()
        self.assertEqual(third["run"], "2026-10-02_WALL10_r03")
        self.assertEqual(first["started_at"], NOW.isoformat())
        self.assertEqual(first["commit"], "abc123")

    def test_part_sanitization_and_name_precedence(self):
        for index, (name, hint, expected) in enumerate((("small   wall__10", "ignored", "SMALL-WALL-10"),
                                                      (None, " !!! ", "PART"),
                                                      (None, "ab..cd___ef", "AB-CD-EF"),
                                                      (None, "x" * 40, "X" * 24),
                                                      ("---name---", "OTHER", "NAME"))):
            with self.subTest(name=name, hint=hint):
                record = runs.start(self.root, f"ws{index}", operator="a", name=name, part_hint=hint, now=NOW)
                self.assertEqual(record["part"], expected)
                self.assertEqual(record["name"], name)

    def test_injected_clock_does_not_depend_on_zone(self):
        with patch.object(runs, "ZoneInfo", side_effect=AssertionError("must not read zone")):
            self.assertEqual(self.start(now=lambda: NOW)["started_at"], NOW.isoformat())

    def test_missing_zone_uses_system_local_time(self):
        with patch.object(runs, "ZoneInfo", side_effect=runs.ZoneInfoNotFoundError("missing")):
            self.assertIsNotNone(runs._now().tzinfo)

    def test_parallel_start_has_unique_ids_and_parallel_add_has_unique_sequences(self):
        def create(index):
            return runs.start(self.root, f"parallel{index}", operator="a", name="Wall", now=NOW)["run"]
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            names = list(pool.map(create, range(8)))
        self.assertEqual(len(set(names)), 8)
        self.start()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            files = list(pool.map(lambda index: self.add(body=str(index).encode()), range(12)))
        self.assertEqual(len(set(files)), 12)
        self.assertEqual(len(runs.active(self.root, self.ws)["files"]), 12)

    def test_exact_names_sequence_and_sidecar(self):
        record = self.start()
        prefix = record["run"]
        name = self.add(kind="WELD-JOB", body=b"/JOB\r\n", detail="WALL_10", ext="jbi", source="workbench",
                        sidecar={"job": "WALL_10", "params": {"lh": 1.2, "token": "secret"},
                                 "query": {"arc": "81", "k": "secret", "platter": {"clear": True}},
                                 "summary": {"moves": 12}, "commit": "fed321", "password": "secret"})
        self.assertEqual(name, f"{prefix}_01_WELD-JOB_WALL-10.JBI")
        sidecar = json.loads((self.folder() / name).with_suffix(".json").read_text())
        self.assertEqual(sidecar, {"kind": "WELD-JOB", "job": "WALL_10", "exported_at": NOW.isoformat(),
                                  "commit": "fed321", "params": {"lh": 1.2},
                                  "query": {"arc": "81", "platter": {"clear": True}},
                                  "summary": {"moves": 12}, "sha256": hashlib.sha256(b"/JOB\r\n").hexdigest()})
        self.assertEqual(self.add(detail="photo (2).jpg", ext=".jpg"), f"{prefix}_02_ATTACH_photo-2-.jpg.jpg")
        self.assertEqual([item["kind"] for item in runs.active(self.root, self.ws)["files"]],
                         ["WELD-JOB", "WELD-JOB", "ATTACH"])

    def test_every_kind_uses_its_exact_extension_and_detail_limit(self):
        record = self.start()
        for index, (kind, ext) in enumerate(runs._EXTENSIONS.items(), start=1):
            name = self.add(kind=kind, ext=ext, body=kind.encode(), detail="x" * 60)
            self.assertEqual(name, f"{record['run']}_{index:02d}_{kind}_{'x' * 40}.{ext}")
            self.assertTrue(runs.valid_file_name(record["run"], name))

    def test_job_sidecars_preserve_measure_stops_with_summary_fallback(self):
        self.start()
        stops = [{"layer": 2, "park_travel_s_est": 3.5, "readings_s": [20, 40, 60]}]
        cases = [({"measure_stops": stops, "summary": None}, stops),
                 ({"summary": {"measure_stops": stops}}, stops),
                 ({"measure_stops": [], "summary": {"measure_stops": stops}}, []),
                 ({"measure_stops": None, "summary": {"measure_stops": stops}}, stops),
                 ({"summary": None}, None)]
        for kind in ("WELD-JOB", "CYCLE-JOB"):
            for sidecar, expected in cases:
                with self.subTest(kind=kind, sidecar=sidecar):
                    name = self.add(kind=kind, ext="JBI", source="workbench", sidecar=sidecar)
                    stored = json.loads((self.folder() / name).with_suffix(".json").read_text(encoding="utf-8"))
                    if expected is None:
                        self.assertNotIn("measure_stops", stored)
                    else:
                        self.assertEqual(stored["measure_stops"], expected)

    def test_sequence_grows_to_three_digits(self):
        record = self.start()
        folder = self.folder(record)
        metadata_path = folder / f"{record['run']}_run.json"
        record["files"] = [{"name": f"{record['run']}_99_ATTACH_old.txt", "kind": "ATTACH", "sha256": "0" * 64,
                            "bytes": 0, "added_at": NOW.isoformat(), "source": "operator"}]
        metadata_path.write_text(json.dumps(record))
        self.assertIn("_100_ATTACH_", self.add())

    def test_part_files_deduplicate_by_sha256(self):
        self.start()
        first = self.add(kind="PART", body=b"part a", detail="first.stl", ext="stl", source="workbench")
        duplicate = self.add(kind="PART", body=b"part a", detail="renamed.stl", ext="stl", source="workbench")
        other = self.add(kind="PART", body=b"part b", detail="first.stl", ext="stl", source="workbench")
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, other)
        self.assertEqual(len(runs.active(self.root, self.ws)["files"]), 2)

    def test_add_without_open_run_and_autostart(self):
        self.assertIsNone(self.add())
        self.assertFalse(self.root.exists())
        name = self.add(kind="PLATE-CORNERS-VAR", ext="DAT", start_if_none=True, part_hint="PLATE")
        self.assertEqual(name, "2026-10-02_PLATE_r01_01_PLATE-CORNERS-VAR_notes.txt.DAT")
        with self.assertRaisesRegex(ValueError, "already open"):
            self.start()

    def test_run_metadata_is_atomically_replaced_and_fsynced(self):
        self.start()
        original = os.replace
        replacements = []
        def replace(source, target):
            self.assertEqual(Path(source).parent, Path(target).parent)
            self.assertTrue(Path(source).name.startswith(".pending-"))
            json.loads(Path(source).read_text())
            replacements.append(Path(target).name)
            original(source, target)
        with patch.object(runs.os, "replace", side_effect=replace), patch.object(runs.os, "fsync", wraps=os.fsync) as fsync:
            runs.add_note(self.root, self.ws, text="checked", now=NOW)
        self.assertEqual(replacements, [runs.active(self.root, self.ws)["run"] + "_run.json"])
        fsync.assert_called_once()
        self.assertFalse(list(self.root.rglob(".pending-*")))

    def test_failed_add_keeps_metadata_and_cleans_new_files(self):
        self.start()
        before = runs.active(self.root, self.ws)
        original = runs._atomic
        def fail_metadata(path, body):
            if path.name.endswith("_run.json"):
                raise OSError("disk failure")
            original(path, body)
        with patch.object(runs, "_atomic", side_effect=fail_metadata):
            with self.assertRaises(OSError):
                self.add()
        self.assertEqual(runs.active(self.root, self.ws), before)
        self.assertEqual(len(list(self.folder().iterdir())), 1)

    def test_failed_start_leaves_no_partial_run(self):
        with patch.object(runs.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.start()
        self.assertIsNone(runs.active(self.root, self.ws))
        self.assertFalse(list(self.root.rglob("*_run.json")))
        self.assertFalse(list(self.root.rglob(".pending-*")))

    def test_seal_writes_last_and_covers_every_other_file(self):
        record = self.start()
        self.add(kind="WELD-JOB", ext="JBI", source="workbench", detail="WALL10",
                 sidecar={"params": {"lh": 1.2}, "query": {"arc": "81"}})
        runs.add_temperature(self.root, self.ws, value=212, unit="F", stage="after-weld", note="test", now=NOW)
        original = os.replace
        replacements = []
        def replace(source, target):
            replacements.append(Path(target).name)
            original(source, target)
        with patch.object(runs.os, "replace", side_effect=replace):
            finished = self.finish(notes="completed", measurements={"height_mm": 10.2, "width_mm": None})
        folder = self.folder(record)
        seal_name = f"{record['run']}_SEALED.json"
        self.assertEqual(replacements[-1], seal_name)
        manifest = json.loads((folder / seal_name).read_text())
        self.assertEqual({item["name"] for item in manifest["files"]}, {p.name for p in folder.iterdir()} - {seal_name})
        for item in manifest["files"]:
            body = (folder / item["name"]).read_bytes()
            self.assertEqual(item["bytes"], len(body))
            self.assertEqual(item["sha256"], hashlib.sha256(body).hexdigest())
        self.assertIsNone(runs.active(self.root, self.ws))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["cycles"], [])
        rows = list(csv.DictReader(io.StringIO((folder / f"{record['run']}_files.csv").read_text())))
        self.assertEqual(len(rows), 2)
        temperatures = list(csv.DictReader(io.StringIO((folder / f"{record['run']}_temperatures.csv").read_text())))
        self.assertEqual(float(temperatures[0]["value_c"]), 100)
        readme = (folder / f"{record['run']}_README.txt").read_text()
        for text in ("operator@example.invalid", "WALL10", "completed", "preliminary", '"lh": 1.2', "Waiver", "212 F"):
            self.assertIn(text, readme)

    def test_sealed_run_refuses_all_mutations_even_with_stale_active_pointer(self):
        record = self.start()
        self.finish()
        (self.root / self.ws / "_runs" / "active.json").write_text(json.dumps({"run": record["run"]}))
        before = {path.name: path.read_bytes() for path in self.folder(record).iterdir()}
        operations = (lambda: self.add(),
                      lambda: runs.add_note(self.root, self.ws, text="late", now=NOW),
                      lambda: runs.add_temperature(self.root, self.ws, value=20, unit="C", stage="other", now=NOW),
                      lambda: runs.set_layer_row(self.root, self.ws, {"layer": 0, "unit": "F"}, now=NOW),
                      lambda: self.finish())
        for operation in operations:
            with self.assertRaisesRegex(ValueError, "this run is finished; start a new one"):
                operation()
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.folder(record).iterdir()})

    def test_failed_seal_rolls_back_and_can_be_retried(self):
        self.start()
        self.add()
        before = runs.active(self.root, self.ws)
        original = os.replace
        failed = False
        def replace(source, target):
            nonlocal failed
            if str(target).endswith("_README.txt") and not failed:
                failed = True
                raise OSError("seal rename failed")
            original(source, target)
        with patch.object(runs.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "seal rename failed"):
                self.finish()
        self.assertEqual(runs.active(self.root, self.ws), before)
        self.assertEqual({p.name for p in self.folder().iterdir()},
                         {before["run"] + "_run.json", before["files"][0]["name"]})
        self.assertEqual(self.finish()["status"], "finished")

    def test_failed_seal_fsync_keeps_run_open_for_retry(self):
        self.start()
        before = runs.active(self.root, self.ws)
        original = os.fsync
        calls = 0
        def fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("seal fsync failed")
            original(fd)
        with patch.object(runs.os, "fsync", side_effect=fsync):
            with self.assertRaisesRegex(OSError, "seal fsync failed"):
                self.finish()
        self.assertEqual(runs.active(self.root, self.ws), before)
        self.assertFalse(list(self.root.rglob(".pending-*")))
        self.assertEqual(self.finish()["status"], "finished")

    def test_stale_sealed_pointer_is_hidden_and_next_export_starts_new_run(self):
        old = self.start()
        self.finish()
        pointer = self.root / self.ws / "_runs" / "active.json"
        pointer.write_text(json.dumps({"run": old["run"]}))
        self.assertIsNone(runs.active(self.root, self.ws))
        self.assertIsNone(runs.summary(self.root, self.ws)["active"])
        with self.assertRaisesRegex(ValueError, "this run is finished"):
            self.add()
        name = self.add(kind="WELD-JOB", ext="JBI", start_if_none=True, part_hint="WALL10")
        self.assertTrue(name.startswith("2026-10-02_WALL10_r02_"))
        self.assertEqual(runs.active(self.root, self.ws)["run"], "2026-10-02_WALL10_r02")

    def test_single_file_limit_is_checked_before_start(self):
        with patch.object(runs, "MAX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "25 MB"):
                self.add(body=b"123456", start_if_none=True)
        self.assertFalse(self.root.exists())

    def test_run_and_workspace_size_limits_preserve_previous_content(self):
        self.start()
        before = runs.active(self.root, self.ws)
        for constant, message in (("MAX_RUN_BYTES", "250 MB"), ("MAX_WORKSPACE_BYTES", "2 GB")):
            with self.subTest(constant=constant), patch.object(runs, constant, 1):
                with self.assertRaisesRegex(ValueError, message):
                    self.add()
            self.assertEqual(runs.active(self.root, self.ws), before)

    def test_file_limit_includes_sidecars_and_reserves_seal_artifacts(self):
        self.start()
        with patch.object(runs, "MAX_FILES", 8):
            with self.assertRaisesRegex(ValueError, "300 files"):
                self.add(kind="WELD-JOB", ext="JBI")
            self.add()
            with self.assertRaisesRegex(ValueError, "300 files"):
                self.add()
            record = self.finish()
        self.assertEqual(len(list(self.folder(record).iterdir())), 8)

    def test_successful_add_reserves_room_to_finish_near_storage_limit(self):
        self.start()
        with patch.object(runs, "MAX_RUN_BYTES", 80 * 1024):
            with self.assertRaisesRegex(ValueError, "250 MB"):
                self.add(body=b"x" * (60 * 1024))
            self.add(body=b"x" * 1024)
            record = self.finish(notes="n" * 2000, waiver={"reason": "w" * 2000})
        self.assertLessEqual(sum(path.stat().st_size for path in self.folder(record).iterdir()), 80 * 1024)

    def test_attachment_extension_whitelist_and_case(self):
        self.start()
        for extension in sorted(runs.ATTACHMENT_EXTENSIONS):
            name = self.add(ext=extension.upper())
            self.assertTrue(name.endswith("." + extension.upper()))
        for extension in ("exe", "html", "svg", "bat", "../txt", "txt:exe", ""):
            with self.subTest(extension=extension), self.assertRaisesRegex(ValueError, "extension"):
                self.add(ext=extension)

    def test_temperature_conversion_validation_and_note_limit(self):
        self.start()
        record = runs.add_temperature(self.root, self.ws, value=212, unit="F", stage="before-interpass", note="hot", now=NOW)
        self.assertEqual(record["temperatures"][0], {"at": NOW.isoformat(), "value": 212, "unit": "F",
                                                    "value_c": 100, "stage": "before-interpass", "note": "hot", "cycle": None})
        invalid = ({"value": float("nan")}, {"value": float("inf")}, {"value": True}, {"value": -41},
                   {"value": 2501}, {"value": 1401, "unit": "C"}, {"unit": "K"},
                   {"stage": "unknown"}, {"stage": []}, {"unit": []}, {"note": "x" * 201})
        for values in invalid:
            arguments = {"value": 20, "unit": "F", "stage": "other", "now": NOW}
            arguments.update(values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                runs.add_temperature(self.root, self.ws, **arguments)
        for value, unit in ((-40, "F"), (2500, "F"), (-40, "C"), (1400, "C")):
            runs.add_temperature(self.root, self.ws, value=value, unit=unit, stage="other", now=NOW)
        with self.assertRaisesRegex(ValueError, "2000"):
            runs.add_note(self.root, self.ws, text="x" * 2001)

    def test_finish_validation(self):
        self.start()
        for arguments in ({"outcome": "failed"}, {"data_class": "assumed"}, {"measurements": {"height_mm": -1}},
                          {"measurements": {"width_mm": float("nan")}}, {"measurements": {"height_mm": True}},
                          {"measurements": {"depth_mm": 1}}, {"notes": "x" * 2001}, {"waiver": "yes"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.finish(**arguments)
        self.assertEqual(runs.active(self.root, self.ws)["status"], "open")
        record = self.finish(data_class="controlled", measurements={"height_mm": 0, "width_mm": 4.2})
        self.assertEqual(record["data_class"], "controlled")

    def test_layer_rows_upsert_by_layer_and_pass(self):
        self.assertEqual(self.start()["layer_log"], [])
        arguments = {"layer": 1, "pass": None, "unit": "F", "pre_weld": 212,
                     "readings": {"020.0": 500, "40.5": 400}, "park_travel_s_est": 3.5,
                     "height_mm": 2.4, "height_source": "sense", "width_mm": 6.2, "note": "steady"}
        record = runs.set_layer_row(self.root, self.ws, arguments, now=NOW)
        self.assertEqual(record["layer_log"], [{**arguments, "readings": {"20": 500, "40.5": 400},
                                              "at": NOW.isoformat()}])
        runs.set_layer_row(self.root, self.ws, {"layer": 0, "unit": "C", "pre_weld": 25}, now=NOW)
        runs.set_layer_row(self.root, self.ws, {"layer": 1, "pass": 1, "unit": "C"}, now=NOW)
        later = NOW + dt.timedelta(seconds=20)
        record = runs.set_layer_row(self.root, self.ws, {"layer": 1, "unit": "C", "readings": {"60": 70}}, now=later)
        self.assertEqual(len(record["layer_log"]), 3)
        self.assertEqual(record["layer_log"][0], {
            "layer": 1, "pass": None, "unit": "C", "pre_weld": None, "readings": {"60": 70},
            "park_travel_s_est": None, "height_mm": None, "height_source": None, "width_mm": None,
            "note": "", "at": later.isoformat()})
        self.assertEqual(runs.active(self.root, self.ws)["layer_log"], record["layer_log"])

    def test_parallel_layer_rows_preserve_every_key(self):
        self.start()
        def save(pass_number):
            return runs.set_layer_row(self.root, self.ws, {
                "layer": 1, "pass": pass_number, "unit": "C", "pre_weld": 100}, now=NOW)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(save, [1, 2, 3, 4, 1, 2, 3, 4]))
        rows = runs.active(self.root, self.ws)["layer_log"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["pass"] for row in rows}, {1, 2, 3, 4})
        self.assertEqual({row["pass_base"] for row in rows}, {1})

    def test_layer_rows_validate_all_fields_without_mutation(self):
        self.start()
        invalid = [None, [], {"extra": 1}, {"layer": None}, {"layer": True}, {"layer": -1},
                   {"layer": 1.5}, {"layer": "1"}, {"pass": True}, {"pass": -1}, {"pass": 0},
                   {"pass": 0.5},
                   {"unit": "K"}, {"unit": []}, {"height_source": "robot"}, {"height_source": []},
                   {"note": "x" * 201}, {"note": None}, {"readings": []},
                   {"readings": {"20": None}}, {"readings": {20: 100}}, {"readings": {"20": "100"}},
                   {"readings": {"20": True}}, {"readings": {"20": float("nan")}},
                   {"readings": {"20": -51}}, {"readings": {"20": 2001}},
                   {"readings": {"20": 100, "20.0": 90}}]
        for key in ("no", "-1", "7201", "nan", "inf", "1e999", " 20", ""):
            invalid.append({"readings": {key: 100}})
        for key, high in (("pre_weld", 2000), ("height_mm", 200), ("width_mm", 200), ("park_travel_s_est", 7200)):
            for value in (True, "1", float("nan"), float("inf"), 10 ** 1000, high + 1, -51 if key == "pre_weld" else -1):
                invalid.append({key: value})
        for update in invalid:
            row = {"layer": 1, "unit": "C", **update} if isinstance(update, dict) else update
            with self.subTest(row=row), self.assertRaises(ValueError):
                runs.set_layer_row(self.root, self.ws, row, now=NOW)
        self.assertEqual(runs.active(self.root, self.ws)["layer_log"], [])
        for unit in ("F", "C"):
            result = runs.set_layer_row(self.root, self.ws, {
                "layer": 0, "unit": unit, "pre_weld": -50, "readings": {"0": -50, "7200": 2000},
                "park_travel_s_est": 7200, "height_mm": 0, "width_mm": 200,
                "height_source": "manual", "note": "x" * 200}, now=NOW)
            self.assertEqual(result["layer_log"][0]["unit"], unit)

    def test_layer_log_csv_conversions_columns_and_manifest(self):
        record = self.start()
        runs.set_layer_row(self.root, self.ws, {"layer": 1, "unit": "F", "pre_weld": 212,
                           "readings": {"60": 212, "20": 392}, "park_travel_s_est": 4,
                           "height_mm": 2, "height_source": "sense", "width_mm": 6, "note": "a,b"}, now=NOW)
        runs.set_layer_row(self.root, self.ws, {"layer": 2, "pass": 1, "unit": "C",
                           "readings": {"40.5": 120}}, now=NOW)
        self.finish()
        name = f"{record['run']}_layer_log.csv"
        reader = csv.DictReader(io.StringIO((self.folder(record) / name).read_text(encoding="utf-8")))
        rows = list(reader)
        self.assertEqual(reader.fieldnames[-6:], ["reading_20_s", "reading_20_s_value_c",
                                                "reading_40.5_s", "reading_40.5_s_value_c",
                                                "reading_60_s", "reading_60_s_value_c"])
        self.assertEqual(float(rows[0]["pre_weld_value_c"]), 100)
        self.assertEqual(float(rows[0]["reading_20_s_value_c"]), 200)
        self.assertEqual(float(rows[0]["reading_60_s_value_c"]), 100)
        self.assertEqual(rows[0]["reading_40.5_s"], "")
        self.assertEqual(rows[0]["note"], "a,b")
        self.assertEqual(rows[0]["height_source"], "sense")
        self.assertEqual(rows[0]["pass_base"], "")
        self.assertEqual(rows[1]["pass_base"], "1")
        self.assertEqual(rows[1]["pre_weld_value_c"], "")
        self.assertEqual(float(rows[1]["reading_40.5_s_value_c"]), 120)
        self.assertTrue(runs.valid_file_name(record["run"], name))
        self.assertIn(name, {item["name"] for item in runs.sealed_runs(self.root)[0]["files"]})
        source = f"{self.ws}/_runs/{record['run']}/{name}"
        entry = next(item for item in mirror_manifest.build(self.root)["files"] if item["src"] == source)
        self.assertEqual(entry["dest"], f"{self.ws}/Runs/{record['run']}/{name}")
        self.assertEqual(entry["sha256"], hashlib.sha256((self.folder(record) / name).read_bytes()).hexdigest())

    def test_legacy_run_loads_empty_layer_log_without_rewriting_seal(self):
        record = self.start()
        folder = self.folder(record)
        metadata = folder / f"{record['run']}_run.json"
        del record["layer_log"]
        metadata.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(runs.active(self.root, self.ws)["layer_log"], [])
        self.assertEqual(runs.summary(self.root, self.ws)["active"]["layer_log"], [])
        self.finish(outcome="not-run")
        record = json.loads(metadata.read_text(encoding="utf-8"))
        del record["layer_log"]
        metadata.write_text(json.dumps(record), encoding="utf-8")
        name = f"{record['run']}_layer_log.csv"
        (folder / name).unlink()
        seal_path = folder / f"{record['run']}_SEALED.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["files"] = [item for item in seal["files"] if item["name"] != name]
        seal_path.write_text(json.dumps(seal), encoding="utf-8")
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        self.assertEqual(runs.summary(self.root, self.ws)["recent"][0]["layer_log"], [])
        self.assertEqual(len(runs.sealed_runs(self.root)), 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})
        self.assertNotIn("layer_log.csv", runs._FIXED_FILES)

    def test_numbered_layer_row_without_base_marker_is_refused_on_load(self):
        self.start()
        record = runs.set_layer_row(self.root, self.ws, {
            "layer": 1, "pass": 1, "unit": "C", "pre_weld": 100}, now=NOW)
        self.assertEqual(record["layer_log"][0]["pass_base"], 1)
        record["layer_log"][0].pop("pass_base")
        metadata = self.folder(record) / f"{record['run']}_run.json"
        metadata.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no pass_base: 1.*refusing"):
            runs.active(self.root, self.ws)
        with self.assertRaisesRegex(ValueError, "no pass_base: 1.*refusing"):
            runs.summary(self.root, self.ws)

    def test_finish_clears_a_crash_left_temp_file(self):
        self.start()
        folder = self.folder()
        (folder / ".pending-crashed").write_bytes(b"half")
        self.finish()
        self.assertFalse((folder / ".pending-crashed").exists())
        sealed = json.loads((folder / f"{folder.name}_SEALED.json").read_text(encoding="utf-8"))
        self.assertFalse(any(item["name"].startswith(".") for item in sealed["files"]))

    def test_finish_requires_arcmon_or_recorded_waiver(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "arc monitor export or a recorded waiver"):
            self.finish(waiver=None)
        record = self.finish(waiver={"by": "spoof", "at": "wrong", "reason": "no logger"})
        self.assertEqual(record["arc_monitor"]["waiver"],
                         {"by": "operator@example.invalid", "at": NOW.isoformat(), "reason": "no logger"})

    def test_finish_with_arcmon_and_not_run_exception(self):
        self.start()
        name = self.add(kind="ARCMON", ext="CSV", detail="arc.CSV", arcmon_summary={"kind": "realtime-csv", "samples": 300})
        record = self.finish(waiver=None)
        self.assertEqual(record["arc_monitor"]["files"], [name])
        self.assertEqual(record["arc_monitor"]["summaries"], [{"kind": "realtime-csv", "samples": 300}])
        self.assertIsNone(record["arc_monitor"]["waiver"])
        self.start()
        self.assertEqual(self.finish(outcome="not-run", waiver=None)["outcome"], "not-run")

    def test_no_open_run_refuses_notes_temperature_and_finish(self):
        for operation in (lambda: runs.add_note(self.root, self.ws, text="x"),
                          lambda: runs.add_temperature(self.root, self.ws, value=1, unit="C", stage="other"),
                          lambda: runs.set_layer_row(self.root, self.ws, {"layer": 0, "unit": "C"}),
                          lambda: self.finish()):
            with self.assertRaisesRegex(ValueError, "No run is open"):
                operation()

    def test_workspace_run_and_name_traversal_refused(self):
        for workspace in ("../escape", "a/b", "a\\b", ".", "guest.x", "", "a" * 81):
            with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                runs.start(self.root, workspace, operator="a", now=NOW)
        self.assertFalse(self.root.exists())
        for run_id in ("../escape", "2026-10-02_PART_r01/extra", "2026-10-02_PART_r00", "2026-99-99_PART_r01"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                runs.ack(self.root, self.ws, run_id, to="test")
        for name in ("../escape", "a\\b", "a/b", "C:secret", "..", "bad\x00file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.add(detail=name, start_if_none=True)
        with self.assertRaises(ValueError):
            self.start(name="../escape")
        self.assertFalse(self.root.exists())

    def test_active_pointer_cannot_traverse(self):
        self.start()
        (self.root / self.ws / "_runs" / "active.json").write_text('{"run":"../escape"}')
        with self.assertRaises(ValueError):
            runs.active(self.root, self.ws)

    def test_filename_validator_refuses_traversal_unknown_kinds_and_extensions(self):
        run_id = "2026-10-02_PART_r01"
        for name in ("../secret", f"{run_id}_01_ATTACH_note.exe", f"{run_id}_01_UNKNOWN_note.txt",
                     f"{run_id}_00_ATTACH_note.txt", f"{run_id}_001_ATTACH_note.txt",
                     f"{run_id}_README.html", f"{run_id}_01_ATTACH_/note.txt"):
            self.assertFalse(runs.valid_file_name(run_id, name), name)
        for name in (f"{run_id}_01_WELD-JOB_JOB.JBI", f"{run_id}_01_WELD-JOB_JOB.json", f"{run_id}_SEALED.json"):
            self.assertTrue(runs.valid_file_name(run_id, name), name)

    def test_manifest_cli_excludes_not_run_and_ack_is_outside_sealed_folder(self):
        self.start()
        completed = self.finish()
        self.start()
        not_run = self.finish(outcome="not-run", waiver=None)
        self.assertEqual({item["run"] for item in runs.sealed_runs(self.root)}, {completed["run"], not_run["run"]})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(runs.main(["manifest", "--saves", str(self.root)]), 0)
        manifest = json.loads(output.getvalue())
        self.assertEqual([item["run"] for item in manifest], [completed["run"]])
        before = {p.name: p.read_bytes() for p in self.folder(completed).iterdir()}
        ack = runs.ack(self.root, self.ws, completed["run"], to="LETU OneDrive RAW SLICER FILES", now=NOW)
        self.assertEqual(ack, {"run": completed["run"], "to": "LETU OneDrive RAW SLICER FILES", "at": NOW.isoformat()})
        ack_path = self.root / self.ws / "_runs" / ".acks" / f"{completed['run']}.json"
        ack_bytes = ack_path.read_bytes()
        repeated = runs.ack(self.root, self.ws, completed["run"], to="LETU OneDrive RAW SLICER FILES",
                            now=NOW + dt.timedelta(minutes=5))
        self.assertEqual(repeated, ack)
        self.assertEqual(ack_path.read_bytes(), ack_bytes)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.folder(completed).iterdir()})
        recent = runs.summary(self.root, self.ws)["recent"]
        self.assertEqual(next(item for item in recent if item["run"] == completed["run"])["ack"], ack)
        self.assertEqual(runs.main(["ack", self.ws, completed["run"], "--to", "host", "--saves", str(self.root)]), 0)
        self.assertEqual(json.loads(ack_path.read_text())["to"], "host")

    def test_ack_requires_sealed_run(self):
        record = self.start()
        with self.assertRaisesRegex(ValueError, "only sealed"):
            runs.ack(self.root, self.ws, record["run"], to="host")

    def test_recent_keeps_ten_newest_finished_runs(self):
        for index in range(12):
            self.start(now=NOW + dt.timedelta(minutes=index))
            self.finish(now=NOW + dt.timedelta(minutes=index, seconds=30))
        open_run = self.start(now=NOW + dt.timedelta(minutes=13))
        result = runs.summary(self.root, self.ws)
        self.assertEqual(result["active"]["run"], open_run["run"])
        self.assertEqual(len(result["recent"]), 10)
        self.assertTrue(result["recent"][0]["run"].endswith("_r12"))
        self.assertTrue(result["recent"][-1]["run"].endswith("_r03"))


if __name__ == "__main__":
    unittest.main()
