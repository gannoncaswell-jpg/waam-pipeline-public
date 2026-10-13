import csv
import datetime as dt
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import runs


class TestAutoRuns(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "saves"
        self.at = dt.datetime(2026, 10, 2, 17, 30, tzinfo=dt.timezone.utc)
        self.record = runs.start(self.root, "auto", operator="operator", name="wall", now=self.at)
        self.folder = self.root / "auto" / "_runs" / self.record["run"]

    def job(self, cycle=1):
        return runs.add_file(
            self.root, "auto", kind="CYCLE-JOB", body=b"/JOB\r\n", detail=f"C{cycle:02d}-WALL_C{cycle:02d}",
            ext="JBI", source="workbench", now=self.at,
            sidecar={"job": f"WALL_C{cycle:02d}", "params": {"lh": "4", "bead": "6", "travel": "175"},
                     "cycle": {"cycle": cycle, "first": 0, "last": 1, "step_mm": 4,
                               "hop": 5, "spacing": 20, "interpass": 30, "n": 2, "token": "private"},
                     "stations": [{"pass_index": 0, "arc_mm": 10, "expected": [10, 0, 8]}]})

    def test_cycle_kinds_preserve_sidecar_and_raw_bytes(self):
        name = self.job()
        self.assertEqual(name, self.record["run"] + "_01_CYCLE-JOB_C01-WALL_C01.JBI")
        sidecar = json.loads((self.folder / name).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["cycle"]["step_mm"], 4)
        self.assertEqual(sidecar["stations"], [{"pass_index": 0, "arc_mm": 10, "expected": [10, 0, 8]}])
        self.assertNotIn("token", sidecar["cycle"])
        raw = b"//VAR\r\n\xff controller bytes\r\n"
        var_name = runs.add_file(self.root, "auto", kind="CYCLE-VAR", body=raw, detail="C01",
                                 ext="DAT", source="robot", now=self.at)
        self.assertEqual((self.folder / var_name).read_bytes(), raw)
        for stored in (name, str(Path(name).with_suffix(".json")), var_name):
            self.assertTrue(runs.valid_file_name(self.record["run"], stored))
        manual = runs.add_file(self.root, "auto", kind="WELD-JOB", body=b"/JOB\r\n", detail="WALL_C01",
                               ext="JBI", source="workbench", now=self.at)
        self.assertTrue(manual.endswith("_WELD-JOB_WALL-C01.JBI"))
        self.assertFalse(runs.valid_file_name(self.record["run"], manual.replace("WALL-C01", "WALL_C01")))

    def test_set_cycle_replaces_and_serializes_concurrent_appends(self):
        runs.set_cycle(self.root, "auto", {"cycle": 1, "status": "exported", "old": True})
        result = runs.set_cycle(self.root, "auto", {"cycle": 1, "status": "imported", "summary": {"growth_mm": 3}})
        self.assertEqual(result["cycles"], [{"cycle": 1, "status": "imported", "summary": {"growth_mm": 3}}])
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda number: runs.set_cycle(self.root, "auto", {"cycle": number}), range(2, 8)))
        self.assertEqual({cycle["cycle"] for cycle in runs.active(self.root, "auto")["cycles"]}, set(range(1, 8)))
        for invalid in (None, {}, {"cycle": True}, {"cycle": 0}, {"cycle": 1.5}, {"cycle": "1"}):
            with self.subTest(record=invalid), self.assertRaisesRegex(ValueError, "positive whole"):
                runs.set_cycle(self.root, "auto", invalid)

    def test_temperature_cycle_validation_and_manual_default(self):
        for invalid in (True, False, 0, -1, 1.0, "1"):
            with self.subTest(cycle=invalid), self.assertRaisesRegex(ValueError, "positive whole"):
                runs.add_temperature(self.root, "auto", value=212, unit="F", stage="after-interpass", cycle=invalid)
        runs.add_temperature(self.root, "auto", value=212, unit="F", stage="before-interpass", cycle=1, now=self.at)
        result = runs.add_temperature(self.root, "auto", value=90, unit="C", stage="other", now=self.at)
        self.assertEqual([item["cycle"] for item in result["temperatures"]], [1, None])
        self.assertEqual(result["temperatures"][0]["value_c"], 100)

    def test_seal_cycle_table_has_measurements_temperatures_and_export_settings(self):
        name = self.job()
        runs.set_cycle(self.root, "auto", {
            "cycle": 1, "layers": [0, 1], "last_cycle": False, "step_mm": 4, "job": "WALL_C01",
            "file": name, "touches": 3, "spacing_mm": 20, "interpass_s": 30,
            "exported_at": self.at.isoformat(), "imported_at": self.at.isoformat(), "status": "applied",
            "summary": {"growth_raw_mm": 3, "growth_mm": 3, "clamped": False,
                        "mean_error_mm": -2, "error_range_mm": [-2.1, -1.9]},
            "build_settings": {"bead_height_mm": 4, "bead_width_mm": 6, "travel_mm_min": 175,
                               "wire_feed_m_min": 2.4},
        })
        for stage, value in (("before-interpass", 650), ("after-interpass", 450)):
            runs.add_temperature(self.root, "auto", value=value, unit="F", stage=stage, cycle=1, now=self.at)
        runs.add_temperature(self.root, "auto", value=200, unit="C", stage="after-interpass", now=self.at)
        last_name = self.job(2)
        runs.set_cycle(self.root, "auto", {"cycle": 2, "layers": [2, 3], "last_cycle": True,
                                          "step_mm": 3, "job": "WALL_C02", "file": last_name,
                                          "touches": 0, "interpass_s": 30, "status": "exported"})
        runs.finish(self.root, "auto", outcome="completed", waiver=True, by="operator", now=self.at)
        csv_name = f"{self.record['run']}_cycles.csv"
        rows = list(csv.DictReader(io.StringIO((self.folder / csv_name).read_text(encoding="utf-8"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "cycle": "1", "first": "0", "last": "1", "n": "2", "step_mm": "4", "growth_raw_mm": "3",
            "growth_mm": "3", "clamped": "False", "mean_error_mm": "-2", "min_error_mm": "-2.1",
            "max_error_mm": "-1.9", "touches": "3", "t_before_value": "650", "t_before_unit": "F",
            "t_before_at": self.at.isoformat(), "t_after_value": "450", "t_after_unit": "F",
            "t_after_at": self.at.isoformat(), "exported_at": self.at.isoformat(), "imported_at": self.at.isoformat(),
            "job": "WALL_C01", "interpass_s": "30", "bead_height_mm": "4", "bead_width_mm": "6",
            "travel_mm_min": "175", "wire_feed_m_min": "2.4", "weld_ctwd_mm": "",
        })
        self.assertEqual(rows[1]["growth_mm"], "")
        self.assertEqual(rows[1]["t_after_value"], "")
        self.assertEqual(rows[1]["bead_height_mm"], "4")
        seal = json.loads((self.folder / f"{self.record['run']}_SEALED.json").read_text(encoding="utf-8"))
        self.assertIn(csv_name, {item["name"] for item in seal["files"]})
        with self.assertRaisesRegex(ValueError, "run"):
            runs.set_cycle(self.root, "auto", {"cycle": 3})

    def test_manual_and_legacy_seals_remain_valid(self):
        runs.finish(self.root, "auto", outcome="not-run", now=self.at)
        csv_name = f"{self.record['run']}_cycles.csv"
        self.assertTrue(runs.valid_file_name(self.record["run"], csv_name))
        self.assertEqual(list(csv.DictReader(io.StringIO((self.folder / csv_name).read_text(encoding="utf-8")))), [])
        (self.folder / csv_name).unlink()
        seal_path = self.folder / f"{self.record['run']}_SEALED.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["files"] = [item for item in seal["files"] if item["name"] != csv_name]
        seal_path.write_text(json.dumps(seal), encoding="utf-8")
        self.assertEqual(len(runs.sealed_runs(self.root)), 1)
        self.assertEqual(runs.summary(self.root, "auto")["recent"][0]["status"], "finished")
        self.assertNotIn("cycles.csv", runs._FIXED_FILES)


if __name__ == "__main__":
    unittest.main()
