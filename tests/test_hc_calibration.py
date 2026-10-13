import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_hc
from waam_pipeline.robots import hc10


class TestHcCalibration(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "docs" / "cell-hc-calibration.json").read_text(encoding="utf-8"))

    def load_data(self, data):
        with mock.patch.object(Path, "read_text", return_value=json.dumps(data)):
            return cell_hc._load_calibration()

    def test_raw_records_match_the_transcription(self):
        expected = [
            ([-60646, 107256, -78237, 3108, 47529, -5111], 13.0),
            ([-2094, 107358, -78285, 3108, 47529, -5111], 13.0),
            ([76642, 98102, -153953, -190160, 64767, -19356], 10.0),
            ([219952, 100931, -155430, -218336, 57337, -24425], 6.0),
        ]
        self.assertEqual([(r["pulses_SLURBT"], r["vertical_distance_mm"])
                          for r in self.data["touch_offs"]], expected)
        self.assertEqual(self.data["measured_date"], "2026-09-20")
        self.assertEqual(self.data["tool"], 0)
        self.assertEqual(self.data["coordinates"], "PULSE")
        self.assertEqual(self.data["distance_uncertainty_mm"], 0.5)
        self.assertIn("Torch_Robot_Calibration_Transcription_2026-09-20.pdf", self.data["provenance"])

    def test_independent_fk_and_svd_reproduce_the_recorded_fit(self):
        points = []
        for record in self.data["touch_offs"]:
            q = np.asarray(record["pulses_SLURBT"]) / hc10.PULSES_PER_DEG
            flange = hc10.fk(q)["flange"]
            tcp = flange[:3, 3] + flange[:3, :3] @ cell_hc.HC_TOOL0_TCP_FLANGE_MM
            points.append(tcp + cell_hc.ROBOT_BASE - [0, 0, record["vertical_distance_mm"]])
        points = np.asarray(points)
        centroid = points.mean(axis=0)
        normal = np.linalg.svd(points - centroid)[2][-1]
        normal *= 1 if normal[2] > 0 else -1
        offset = float(centroid[2] - cell_hc.TABLE_Z_MODEL)
        tilt = float(np.degrees(np.arccos(normal[2])))
        residual = float(np.max(np.abs((points - centroid) @ normal)))
        self.assertAlmostEqual(offset, -18.79, delta=0.1)
        self.assertAlmostEqual(tilt, 0.41, delta=0.05)
        self.assertAlmostEqual(residual, 0.04, delta=0.01)

        plane = cell_hc.HC_TABLE_PLANE
        np.testing.assert_allclose(plane.point, centroid, rtol=0, atol=1e-9)
        np.testing.assert_allclose(plane.normal, normal, rtol=0, atol=1e-12)
        self.assertGreater(plane.normal[2], 0)
        self.assertAlmostEqual(float(np.linalg.norm(plane.normal)), 1.0)
        self.assertAlmostEqual(plane.tilt_deg, tilt, places=9)
        self.assertAlmostEqual(plane.residual_mm, residual, places=9)
        self.assertAlmostEqual(cell_hc.HC_TABLE_OFFSET_MM, offset, places=9)
        self.assertAlmostEqual(cell_hc.TABLE_Z, centroid[2], places=9)

    def test_changed_raw_distances_are_refitted(self):
        for record in self.data["touch_offs"]:
            record["vertical_distance_mm"] += 0.1
        offset, plane = self.load_data(self.data)
        self.assertAlmostEqual(offset, cell_hc.HC_TABLE_OFFSET_MM - 0.1, places=9)
        np.testing.assert_allclose(plane.point, cell_hc.HC_TABLE_PLANE.point - [0, 0, 0.1])

    def test_stored_fit_is_a_guard_not_the_height_source(self):
        self.data["_fitted"]["offset_mm"] += 0.1
        self.data["_fitted"]["centroid_mm"][2] += 0.1
        offset, plane = self.load_data(self.data)
        self.assertAlmostEqual(offset, cell_hc.HC_TABLE_OFFSET_MM, places=9)
        np.testing.assert_array_equal(plane.point, cell_hc.HC_TABLE_PLANE.point)

    def test_offset_bound_applies_to_the_raw_fit_in_both_directions(self):
        self.assertEqual(cell_hc.MAX_TABLE_OFFSET_MM, 50.0)
        self.assertLessEqual(abs(cell_hc.HC_TABLE_OFFSET_MM), cell_hc.MAX_TABLE_OFFSET_MM)
        for shift in (-100.0, 100.0):
            with self.subTest(shift=shift), \
                 mock.patch.object(cell_hc, "ROBOT_BASE", cell_hc.ROBOT_BASE + [0, 0, shift]):
                with self.assertRaisesRegex(ValueError, "outside.*50"):
                    cell_hc.fit_hc_table(self.data["touch_offs"])

    def test_fitted_drift_and_invalid_summaries_raise(self):
        for key, delta in (("offset_mm", 0.201), ("residual_mm", 0.201),
                           ("centroid_mm", [0.201, 0, 0]), ("tilt_deg", 0.051)):
            for corrupt in (False, True):
                with self.subTest(key=key, nonfinite=corrupt):
                    data = copy.deepcopy(self.data)
                    value = np.asarray(data["_fitted"][key]) + delta
                    data["_fitted"][key] = (value * np.nan if corrupt else value).tolist()
                    with self.assertRaisesRegex(ValueError, "_fitted"):
                        self.load_data(data)

    def test_invalid_or_degenerate_touch_records_raise(self):
        records = self.data["touch_offs"]
        for bad in (None, [], records[:2], [records[0]] * 4):
            with self.subTest(records=bad), self.assertRaises(ValueError):
                cell_hc.fit_hc_table(bad)
        for key, value in (("pulses_SLURBT", [1, 2, 3]),
                           ("pulses_SLURBT", [np.nan] * 6),
                           ("pulses_SLURBT", [1.5] * 6),
                           ("vertical_distance_mm", -1),
                           ("vertical_distance_mm", np.nan),
                           ("vertical_distance_mm", np.inf)):
            bad = copy.deepcopy(records)
            bad[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                cell_hc.fit_hc_table(bad)

    def test_malformed_present_file_does_not_fall_back(self):
        for text in ("{", "{}", json.dumps({**self.data, "tool": 1}),
                     json.dumps({**self.data, "coordinates": "ROBOT"})):
            with self.subTest(text=text), mock.patch.object(Path, "read_text", return_value=text):
                with self.assertRaises(ValueError):
                    cell_hc._load_calibration()

    def test_absent_file_returns_zero_offset(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cell_hc._load_calibration(Path(td) / "missing.json"), (0.0, None))

    def test_absent_file_import_uses_model_only_everywhere(self):
        code = '''
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, "src")
read_text = Path.read_text
def without_calibration(path, *args, **kwargs):
    if path.name == "cell-hc-calibration.json":
        raise FileNotFoundError(path)
    return read_text(path, *args, **kwargs)
with patch.object(Path, "read_text", without_calibration):
    from waam_pipeline import cell_hc as hc
    assert hc.HC_TABLE_OFFSET_MM == 0.0
    assert hc.HC_TABLE_PLANE is None
    assert hc.TABLE_Z == hc.TABLE_Z_MODEL
    assert hc.DEFAULT_PART_ORIGIN[2] == hc.TABLE_Z_MODEL
    assert hc.hc_work_surface_z(6.0) == hc.TABLE_Z_MODEL + 6.0
    assert hc.place_origin([[0, 0], [10, 10]])[2] == hc.TABLE_Z_MODEL
    assert all(p.expected[2] == hc.TABLE_Z_MODEL for p in hc.hc_probe_plan())
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_height_flows_to_placement_probes_and_table_geometry(self):
        z = cell_hc.HC_TABLE_PLANE.point[2]
        self.assertAlmostEqual(cell_hc.hc_work_surface_z(6.0), z + 6.0)
        self.assertAlmostEqual(cell_hc.DEFAULT_PART_ORIGIN[2], z)
        origin = cell_hc.place_origin([[0, 0], [100, 40]], substrate=6, gap=3, bottom=2)
        self.assertAlmostEqual(origin[2] + 2, z + 6 + 3)
        for probe in cell_hc.hc_probe_plan():
            self.assertAlmostEqual(probe.expected[2], z)
        meshes = cell_hc._furniture()
        with np.load(cell_hc.MESH_PATH, allow_pickle=False) as archive:
            expected = archive["Table"].astype(float) + [0, 0, cell_hc.HC_TABLE_OFFSET_MM]
            np.testing.assert_allclose(meshes["Table"], expected, rtol=0, atol=1e-9)
            for name in ("BackPanel", "Balancer", cell_hc.CLEANERS[cell_hc.DEFAULT_CLEANER]):
                np.testing.assert_array_equal(meshes[name], archive[name])
        table = next(box for box in cell_hc.hc_scene(origin).boxes if box[0] == "hc_table")
        np.testing.assert_allclose(table[1] + origin, expected.min(axis=(0, 1)))
        np.testing.assert_allclose(table[2] + origin, expected.max(axis=(0, 1)))
        np.testing.assert_array_equal(cell_hc.ROBOT_BASE, [-508.006, 0.016, 1242.23 - 275.0])

    def test_invalid_substrate_thickness_raises(self):
        for value in (-1, np.nan, np.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cell_hc.hc_work_surface_z(value)


if __name__ == "__main__":
    unittest.main()
