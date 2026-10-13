from __future__ import annotations

import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from waam_pipeline import cell_lc
from waam_pipeline.robots import ar1440
from waam_pipeline.slicer import wall

WALL = dict(length_mm=150.0, layers=6, layer_h_mm=2.0)
ORIGIN_XY = (1014.0, -180.0)
SUBSTRATE_MM = 6.35


def _pulses_at(points_robot):


    solve = cell_lc.pulse_solver(np.zeros(3))
    out = []
    for p in points_robot:
        q = solve(p)
        out.append([int(round(a * r)) for a, r in zip(q, ar1440.PULSES_PER_DEG)])
    return out


def _fixture_at(height_mm: float, *, tilt_mm_per_mm: float = 0.0) -> dict:


    x0, y0 = ORIGIN_XY
    z0 = cell_lc.PLATTER_FACE_Z + height_mm
    corners = [(x0 - 20.0, y0 - 100.0), (x0 + 170.0, y0 - 100.0), (x0 + 75.0, y0 + 100.0)]
    pts = [(x, y, z0 + tilt_mm_per_mm * (x - x0)) for x, y in corners]
    return {
        "touch_pulses": _pulses_at(pts),
        "substrate_mm": 0.0,
        "stickout_correction_mm": 0.0,
    }


def _run_script(name: str, argv: list[str]) -> dict:

    saved = sys.argv
    sys.argv = [str(ROOT / "examples" / name), *argv]
    try:
        with redirect_stdout(io.StringIO()):
            return runpy.run_path(str(ROOT / "examples" / name), run_name="__main__")
    finally:
        sys.argv = saved


ATTEST = ["--platter-clear", "--block", "off", "--by", "test-suite"]


class TestArcFileHasOneHome(unittest.TestCase):


    def test_generator_emits_the_profiles_asf(self):


        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "cell_seven.json"
            data = json.loads(
                (ROOT / "examples" / "cell_lc50s.json").read_text(encoding="utf-8"))
            data["arc_file"] = 7
            profile.write_text(json.dumps(data), encoding="utf-8")

            g = _run_script("gen_wall_jbi.py",
                            ["--cell", str(profile), "--out", td, *ATTEST])
            on_disk = (Path(td) / "WAAM-WALL1.JBI").read_text(encoding="utf-8")

        self.assertEqual(g["ARC_FILE"], 7)
        for text in (g["jbi"], on_disk):
            self.assertIn("ARCON ASF#(7)", text)
            self.assertIn("ARCOF AEF#(7)", text)
            self.assertNotIn("ASF#(12)", text)
            self.assertNotIn("AEF#(12)", text)

    def test_profile_that_fails_validation_is_refused(self):


        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "cell_bad.json"
            data = json.loads(
                (ROOT / "examples" / "cell_lc50s.json").read_text(encoding="utf-8"))
            data["arc_file"] = 0
            profile.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as cm:
                cell_lc.arc_file(profile)
        self.assertIn("arc_file", str(cm.exception))

    def test_preflight_and_generator_may_not_disagree(self):


        from waam_pipeline import readiness

        live, _ = readiness.configured_arc_file()
        self.assertIsNotNone(live, "readiness no longer finds the AR1440 cell profile")

        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "cell_lc50s.json"
            data = json.loads(
                (ROOT / "examples" / "cell_lc50s.json").read_text(encoding="utf-8"))
            data["arc_file"] = int(live) + 1
            profile.write_text(json.dumps(data), encoding="utf-8")
            saved = cell_lc.LC_CELL_PROFILE
            cell_lc.LC_CELL_PROFILE = profile
            try:
                with self.assertRaises(ValueError) as cm:
                    cell_lc.arc_file()
            finally:
                cell_lc.LC_CELL_PROFILE = saved
        self.assertIn("disagreement", str(cm.exception))


class _WithFixture:


    def __init__(self, name, entry):
        self.name, self.entry = name, entry

    def __enter__(self):
        self.saved = cell_lc.CALIBRATION
        cell_lc.CALIBRATION = dict(self.saved)
        cell_lc.CALIBRATION["fixtures"] = {self.name: self.entry}
        cell_lc.CALIBRATION["active_fixture"] = self.name
        return self

    def __exit__(self, *exc):
        cell_lc.CALIBRATION = self.saved
        return False


class TestSeatJobRefusalIsNotSwallowed(unittest.TestCase):


    def test_tilt_refusal_propagates(self):
        tp = wall(WALL["length_mm"], WALL["layers"], WALL["layer_h_mm"])


        with _WithFixture("tilted", _fixture_at(0.0, tilt_mm_per_mm=0.02)):
            with self.assertRaises(ValueError) as cm:
                cell_lc.seat_job(tp, *ORIGIN_XY, substrate_mm=SUBSTRATE_MM)
        self.assertIn("varies", str(cm.exception))
        self.assertNotIsInstance(cm.exception, cell_lc.NoFixtureRecorded)

    def test_generator_stops_instead_of_seating_on_the_platter(self):


        tp = wall(WALL["length_mm"], WALL["layers"], WALL["layer_h_mm"])
        with tempfile.TemporaryDirectory() as td:
            with _WithFixture("tilted", _fixture_at(0.0, tilt_mm_per_mm=0.02)):
                with self.assertRaises(SystemExit) as cm:
                    _run_script("gen_wall_jbi.py", ["--out", td, *ATTEST])
            self.assertIn("refusing to emit", str(cm.exception.code))
            self.assertIn("varies", str(cm.exception.code))
            self.assertEqual([], sorted(Path(td).glob("*.JBI")),
                             "a refused job must not reach disk")
        del tp

    def test_no_fixture_recorded_still_falls_back_to_the_platter(self):


        tp = wall(WALL["length_mm"], WALL["layers"], WALL["layer_h_mm"])
        saved = cell_lc.CALIBRATION
        cell_lc.CALIBRATION = dict(saved)
        cell_lc.CALIBRATION["fixtures"] = {}
        cell_lc.CALIBRATION["active_fixture"] = None
        try:
            origin, surface = cell_lc.seat_job(
                tp, *ORIGIN_XY, substrate_mm=SUBSTRATE_MM)
        finally:
            cell_lc.CALIBRATION = saved
        self.assertIsNone(surface)
        expected = cell_lc.snap_toolpath_to_platter(
            tp, *ORIGIN_XY, substrate_mm=SUBSTRATE_MM)
        self.assertTrue(np.allclose(origin, expected, atol=1e-12))

    def test_unknown_fixture_name_is_not_a_fallback(self):

        tp = wall(WALL["length_mm"], WALL["layers"], WALL["layer_h_mm"])
        with _WithFixture("coupon-plate", _fixture_at(0.0)):
            with self.assertRaises(ValueError) as cm:
                cell_lc.seat_job(tp, *ORIGIN_XY, substrate_mm=SUBSTRATE_MM,
                                 fixture="coupon_plate")
        self.assertNotIsInstance(cm.exception, cell_lc.NoFixtureRecorded)
        self.assertIn("unknown fixture", str(cm.exception))


class TestBothScriptsSeatTheSameJob(unittest.TestCase):


    def test_measured_fixture_moves_both_scripts_together(self):


        tp = wall(WALL["length_mm"], WALL["layers"], WALL["layer_h_mm"])
        platter_only = cell_lc.snap_toolpath_to_platter(
            tp, *ORIGIN_XY, substrate_mm=SUBSTRATE_MM)

        with tempfile.TemporaryDirectory() as td:
            with _WithFixture("proud-plate", _fixture_at(3.0)):
                jbi = _run_script("gen_wall_jbi.py", ["--out", td, *ATTEST])
                ros = _run_script("gen_wall_ros2.py", ["--out", td, *ATTEST])

        self.assertTrue(
            np.allclose(jbi["origin"], ros["origin"], atol=1e-9, rtol=0.0),
            f"the two scripts seated the job differently: "
            f"{jbi['origin']} vs {ros['origin']}")


        for name, g in (("jbi", jbi), ("ros2", ros)):


            self.assertAlmostEqual(
                float(g["origin"][2]) - float(platter_only[2]), 3.0, delta=0.01,
                msg=f"{name} did not seat on the measured surface")
            self.assertIsNotNone(g["surface"])

    def test_ros2_script_stops_on_a_refused_surface(self):


        with tempfile.TemporaryDirectory() as td:
            with _WithFixture("tilted", _fixture_at(0.0, tilt_mm_per_mm=0.02)):
                with self.assertRaises(SystemExit) as cm:
                    _run_script("gen_wall_ros2.py", ["--out", td, *ATTEST])
            self.assertIn("varies", str(cm.exception.code))
            self.assertEqual([], sorted(Path(td).glob("*.json")),
                             "a refused job must not reach disk")


class TestTheAttestationIsStampedOnce(unittest.TestCase):


    def test_the_generator_does_not_pass_its_own_stamp(self):
        src = (ROOT / "examples" / "gen_wall_jbi.py").read_text(encoding="utf-8")
        body = src.split("provenance=", 1)[1]
        self.assertNotIn("PLATTER.stamp()", body,
                         "the generator is stamping the attestation itself again")

    def test_an_emitted_job_carries_exactly_one_attestation(self):
        from waam_pipeline import cell_lc
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState, emit_job
        from waam_pipeline.slicer import wall

        tp = wall(150.0, 6, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0,
                                                  substrate_mm=6.35)
        scene = cell_lc.lc_scene(origin, training_block=False)
        platter = PlatterState(clear=True, training_block_on=False,
                               attested_by="test")
        text, _ = emit_job(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="STAMPONCE",
                           arc_file=12, scene=scene,
                           torch=TorchModel.yaskawa_accufast_45(),
                           date="2026/01/01 00:00", platter=platter)
        n = sum(1 for ln in text.splitlines() if "platter attested by" in ln)
        self.assertEqual(n, 1, f"attestation appears {n} times, expected once")

if __name__ == "__main__":
    unittest.main()
