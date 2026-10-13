import re
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.backends import jbi
from waam_pipeline.backends.ros2 import AR1440_MODEL, HC10_MODEL
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, PlatterState, emit_job, emit_ros2
from waam_pipeline.robots import ar1440, hc10
from waam_pipeline.slicer import wall

ATTESTED = PlatterState(clear=True, training_block_on=False, attested_by="test")
TORCH = TorchModel.yaskawa_accufast_45()


def setup(feed_mm_min=200.0):
    tp = wall(60.0, 1, 3.0, feed_mm_min=feed_mm_min)
    origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=6.35)
    scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=False)
    return tp, origin, scene


def emit(tp, origin, scene, **kw):
    return emit_job(tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="VTEST", arc_file=12,
                    scene=scene, torch=TORCH, date="2026/09/19 00:00",
                    platter=ATTESTED, **kw)[0]


def all_speeds(text):
    return {ln.split("V=")[1].split()[0] for ln in text.split("\r\n")
            if ln.startswith(("MOVL", "MOVC")) and "V=" in ln}


def weld_speeds(text):
    return all_speeds(text) - {"333.3"}


class TheTokenComesFromTheIr(unittest.TestCase):
    def test_a_toolpath_with_feeds_is_emitted_at_its_own_speed(self):
        tp, origin, scene = setup(200.0)
        self.assertEqual(weld_speeds(emit(tp, origin, scene)), {"3.3"})

    def test_the_owners_walls_run_at_the_speed_they_actually_ran(self):

        tp, origin, scene = setup(222.0)
        self.assertEqual(weld_speeds(emit(tp, origin, scene)), {"3.7"})

    def test_an_agreeing_weld_v_is_emitted_verbatim(self):

        tp, origin, scene = setup(833.0)
        self.assertEqual(weld_speeds(emit(tp, origin, scene, weld_v="13.9")), {"13.9"})
        self.assertEqual(weld_speeds(emit(tp, origin, scene, weld_v="13.90")), {"13.90"})
        self.assertEqual(weld_speeds(emit(tp, origin, scene)), {"13.9"})

    def test_a_toolpath_with_no_feeds_keeps_the_caller_or_default_token(self):
        tp, origin, scene = setup()
        tp.moves = [replace(m, feed=None) for m in tp.moves]
        self.assertEqual(weld_speeds(emit(tp, origin, scene)), {"13.9"})
        self.assertEqual(weld_speeds(emit(tp, origin, scene, weld_v="50")), {"50"})

    def test_every_token_has_the_taught_jobs_one_decimal(self):
        for feed in (150.0, 200.0, 222.0, 250.0, 333.0, 800.0, 833.0, 1000.0):
            tp, origin, scene = setup(feed)
            text = emit(tp, origin, scene)
            with self.subTest(feed=feed):
                self.assertTrue(all(re.fullmatch(r"\d+(\.\d)?", token) for token in all_speeds(text)))
                weld, = weld_speeds(text)
                self.assertAlmostEqual(float(weld) * 60, feed, delta=3.0)


class DisagreementIsRefused(unittest.TestCase):
    def test_deposit_feeds_that_differ_each_keep_their_own_token(self):

        tp, origin, scene = setup(200.0)
        tp.moves = tp.moves + [replace(tp.moves[-1], x=10.0, feed=400.0)]
        self.assertEqual(weld_speeds(emit(tp, origin, scene)), {"3.3", "6.7"})
        with self.assertRaises(EmitRefused) as cm:
            emit(tp, origin, scene, weld_v="3.3")
        self.assertEqual(cm.exception.reason, "speed")
        self.assertIn("200..400 mm/min", cm.exception.detail)

    def test_an_explicit_weld_v_that_disagrees_with_the_ir(self):
        tp, origin, scene = setup(200.0)
        with self.assertRaises(EmitRefused) as cm:
            emit(tp, origin, scene, weld_v="83.3")
        self.assertEqual(cm.exception.reason, "speed")
        self.assertIn("4998 mm/min", cm.exception.detail)
        self.assertIn("200 mm/min", cm.exception.detail)
        self.assertIn("V=3.3", cm.exception.detail)

    def test_some_deposit_moves_with_a_feed_and_some_without(self):


        tp, origin, scene = setup(200.0)
        tp.moves = tp.moves + [replace(tp.moves[-1], x=10.0, feed=None)]
        with self.assertRaises(EmitRefused) as cm:
            emit(tp, origin, scene)
        self.assertEqual(cm.exception.reason, "speed")
        self.assertIn("carry no feed", cm.exception.detail)

    def test_the_ros_path_refuses_identically(self):
        tp, origin, scene = setup(200.0)
        with self.assertRaises(EmitRefused) as cm:
            emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="VTEST", scene=scene,
                      torch=TORCH, platter=ATTESTED, weld_v="83.3")
        self.assertEqual(cm.exception.reason, "speed")

    def test_the_ros_job_runs_the_bead_at_the_ir_feed(self):
        tp, origin, scene = setup(600.0)
        job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="VTEST", scene=scene,
                           torch=TORCH, platter=ATTESTED)
        weld = [s for s in job.segments if s.welding]
        self.assertTrue(weld)
        self.assertAlmostEqual(sum(s.duration_s for s in weld), 60.0 / (600.0 / 60),
                               places=2)


class TheTokenIsMillimetresPerSecondOnBothArms(unittest.TestCase):
    def test_both_arms_read_the_file_in_mm_per_s(self):
        self.assertEqual(ar1440.V_TOKEN_MM_PER_MIN, 60.0)
        self.assertEqual(hc10.V_TOKEN_MM_PER_MIN, 60.0)
        self.assertEqual(AR1440_MODEL.v_unit_mm_per_min, 60.0)
        self.assertEqual(HC10_MODEL.v_unit_mm_per_min, 60.0)

    def test_the_owners_pendant_reading(self):


        self.assertEqual(jbi.v_speed_mm_min("80", ar1440.V_TOKEN_MM_PER_MIN) / 10, 480.0)
        token = jbi.v_token(800.0, ar1440.V_TOKEN_MM_PER_MIN)
        self.assertEqual(token, "13.3")
        self.assertAlmostEqual(jbi.v_speed_mm_min(token, ar1440.V_TOKEN_MM_PER_MIN) / 10, 80.0, delta=0.3)

    def test_the_taught_jobs_hold_whole_cm_per_min_at_0_1_mm_per_s(self):

        fixture = ROOT / "tests/fixtures/controller/industrial-weld-speeds.json"
        values = set(json.loads(fixture.read_text(encoding="utf-8")))
        self.assertGreater(len(values), 30)
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(any(abs(round(c / 6, 1) - float(value)) < 1e-9 for c in range(9001)))

    def test_the_range_is_the_pendants_1_to_9000_cm_per_min(self):
        self.assertEqual(jbi.v_token(6.0, 60.0), "0.1")
        self.assertEqual(jbi.v_token(90000.0, 60.0), "1500.0")
        for mm_min in (2.9, 90004.0):
            with self.subTest(mm_min=mm_min), self.assertRaises(ValueError):
                jbi.v_token(mm_min, 60.0)

    def test_air_moves_keep_each_arms_speed(self):

        self.assertEqual(jbi.v_token(ar1440.AIR_MM_MIN, ar1440.V_TOKEN_MM_PER_MIN), "333.3")
        self.assertEqual(AR1440_MODEL.air_mm_min, ar1440.AIR_MM_MIN)
        self.assertEqual(HC10_MODEL.air_mm_min, hc10.AIR_MM_MIN)
        tp, origin, scene = setup(200.0)
        self.assertEqual(all_speeds(emit(tp, origin, scene)) - {"3.3"}, {"333.3"})

    def test_the_hc_renderer_uses_its_own_air_and_the_same_weld_token(self):
        from waam_pipeline import cell_hc
        from waam_pipeline.toolpath import Move, Toolpath

        tp = Toolpath(moves=[Move(0.0, 0.0, 2.9, None, False, 0, 1),
                             Move(30.0, 15.0, 2.9, None, True, 0, 2)])
        text = jbi._render_hc_jbi(
            tp, cell_hc.hc_solver(np.asarray(cell_hc.DEFAULT_PART_ORIGIN, float)),
            name="HC-V", date="2026/09/19 00:00", arc_file=13)
        air = jbi.v_token(hc10.AIR_MM_MIN, hc10.V_TOKEN_MM_PER_MIN)
        self.assertEqual(air, "55.5")
        self.assertEqual(all_speeds(text), {air, jbi.v_token(jbi.DEFAULT_WELD_MM_MIN, 60.0)})


if __name__ == "__main__":
    unittest.main()
