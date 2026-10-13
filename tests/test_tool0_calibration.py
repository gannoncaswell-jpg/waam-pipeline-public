import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from test_substrate_orientation import AXIS, rotation, pulses_at_tcp
from waam_pipeline.tool0_calibration import fit_tip_offset
from waam_pipeline import cell_lc
from waam_pipeline.robots import ar1440


def captures(offset=20, extension=8, angles=None, lateral=None):
    endpoint = np.array([1014, -180, 175.0])
    angles = angles or ((20, 0), (-20, 0), (0, 20), (0, -20), (0, 0))
    lateral = np.zeros(3) if lateral is None else np.asarray(lateral, float)
    poses = []
    for x, y in angles:
        attitude = rotation(x, y)
        down = attitude @ AXIS
        tcp = endpoint + (offset - extension) * down - attitude @ lateral
        poses.append(pulses_at_tcp(tcp, attitude))
    return {
        "version": 1, "frame": "lc50s_tool0_pulse", "wire_extension_mm": extension,
        "position_uncertainty_mm": .05, "poses": poses,
    }


class TestContactTipCalibration(unittest.TestCase):
    def test_fixed_endpoint_known_extension_recovers_signed_offset(self):
        for offset in (-8, 0, 20):
            for extension in (0, 8):
                with self.subTest(offset=offset, extension=extension):
                    source = captures(offset, extension)
                    result = fit_tip_offset(source)
                    self.assertAlmostEqual(result["tool0_tip_offset_mm"], offset, delta=.04)
                    np.testing.assert_allclose(result["fixed_endpoint_robot_mm"], [1014, -180, 175], atol=.04)
                    self.assertLess(result["max_residual_mm"], .04)
                    self.assertLess(result["offset_fit_uncertainty_mm"], 1)
                    self.assertEqual(result["source"], source)
                    self.assertIsNot(result["source"], source)

    def test_uncertainty_budget_controls_conditioning(self):
        source = captures()
        result = fit_tip_offset(source)
        doubled = fit_tip_offset(dict(source, position_uncertainty_mm=.1))
        self.assertAlmostEqual(doubled["offset_fit_uncertainty_mm"], 2 * result["offset_fit_uncertainty_mm"])
        with self.assertRaisesRegex(ValueError, "uncertainty"):
            fit_tip_offset(dict(source, position_uncertainty_mm=1))
        with self.assertRaisesRegex(ValueError, "vary too little"):
            fit_tip_offset(captures(angles=((.01, 0), (-.01, 0), (0, .01))))

    def test_single_axis_and_lateral_offsets_are_refused(self):
        with self.assertRaisesRegex(ValueError, "lateral|three tool-offset|two directions"):
            fit_tip_offset(captures(angles=((20, 0), (-20, 0), (0, 0))))
        with self.assertRaisesRegex(ValueError, "lateral"):
            fit_tip_offset(captures(lateral=(0, 5, 0)))

    def test_different_points_are_not_a_fixed_point_calibration(self):
        data = captures()
        data["poses"][0] = pulses_at_tcp(np.array([1014, -180, 195]) + 12 * rotation(20, 0) @ AXIS, rotation(20, 0))
        with self.assertRaisesRegex(ValueError, "lateral|endpoint"):
            fit_tip_offset(data)

    def test_real_vertical_platter_cluster_cannot_calibrate_tool_offset(self):
        stored = json.loads((ROOT / "docs" / "cell-calibration.json").read_text())
        poses = stored["fixtures"]["platter_9pt_2026-09-19"]["touch_pulses"]
        data = {"version": 1, "frame": "lc50s_tool0_pulse", "poses": poses,
                "wire_extension_mm": 8, "position_uncertainty_mm": .1}


        with self.assertRaisesRegex(ValueError, "vary too little"):
            fit_tip_offset(data)

    def test_invalid_input_is_not_a_calibration(self):
        good = captures()
        for patch in ({"version": True}, {"frame": "user_frame"}, {"poses": good["poses"][:2]},
                      {"wire_extension_mm": None}, {"wire_extension_mm": True},
                      {"position_uncertainty_mm": 0}, {"position_uncertainty_mm": None},
                      {"max_offset_uncertainty_mm": float("nan")}, {"poses": [[0] * 5] * 3}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                fit_tip_offset(dict(good, **patch))

    def test_residual_cannot_contradict_declared_position_uncertainty(self):
        from unittest.mock import patch
        data = captures()
        rotations = [rotation(x, y) for x, y in ((20, 0), (-20, 0), (0, 20), (0, -20), (0, 0))]


        design = np.vstack([np.column_stack((np.eye(3), r)) for r in rotations])
        error = np.arange(15, dtype=float)**2
        error -= design @ np.linalg.lstsq(design, error, rcond=None)[0]
        error = error.reshape(-1, 3)
        error *= .1 / np.sqrt(np.mean(np.sum(error**2, axis=1)))
        poses = []
        for r, e in zip(rotations, error):
            pose = np.eye(4)
            pose[:3, :3] = r
            tcp = np.array([1014, -180, 175]) + 12 * r @ AXIS + e
            pose[:3, 3] = tcp - r @ cell_lc.TOOL_TCP
            poses.append({"flange": pose})
        with patch.object(ar1440, "fk", side_effect=poses), self.assertRaisesRegex(ValueError, "RMS residual.*declared"):
            fit_tip_offset(data)

    def test_cli_json_roundtrip_and_no_overwrite(self):
        spec = importlib.util.spec_from_file_location("calibrate_tool0_tip_example", ROOT / "examples" / "calibrate_tool0_tip.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "captures.json", Path(folder) / "result.json"
            source.write_text(json.dumps(captures()))
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                self.assertEqual(cli.main([str(source)]), 0)
            self.assertAlmostEqual(json.loads(text.getvalue())["tool0_tip_offset_mm"], 20, delta=.04)
            self.assertEqual(cli.main([str(source), "--out", str(output)]), 0)
            before = output.read_text()
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refusal:
                cli.main([str(source), "--out", str(output)])
            self.assertEqual(refusal.exception.code, 2)
            self.assertEqual(output.read_text(), before)


if __name__ == "__main__":
    unittest.main()
