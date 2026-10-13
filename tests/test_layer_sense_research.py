from pathlib import Path
import re
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, layer_sense, slicer
from waam_pipeline.backends.jbi import emit_search_jbi
from waam_pipeline.layer_sense import apply_auto, auto_plan, fit_layer_top, layer_sense_plan
from waam_pipeline.touch import SEARCH_OVERTRAVEL_MM
from tests.test_layer_sense import touches


def setUpModule():
    from unittest import mock
    patcher = mock.patch.object(layer_sense, "RESEARCH_MODE", True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TestResearchMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 6, 2)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)

    def test_window_is_the_plate_touch_depth_whatever_the_bead(self):
        for h in (1.0, 2.0, 3.76):
            with self.subTest(h=h):
                path = slicer.wall(100, 4, h)
                plan = layer_sense_plan(path, 1, bead_height_mm=h)
                depth = {round(float(p.expected[2] - p.target[2]), 6) for p in plan}
                self.assertEqual(depth, {SEARCH_OVERTRAVEL_MM})

    def test_window_never_exceeds_the_stick_out_cap(self):
        with self.assertRaisesRegex(ValueError, "at most 5 mm"):
            layer_sense_plan(self.path, 1, bead_height_mm=2, overtravel_mm=5.1)

    def test_touch_job_writes_the_deep_window(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        text = emit_search_jbi(plan, cell_lc.pulse_solver(self.origin), name="LT", date="2026/10/06 20:00",
                               overtravel_mm=float(plan[0].expected[2] - plan[0].target[2]), hop_mm=5.0)

        self.assertEqual(set(re.findall(r"SRCH RIN#\(3\)=ON T=0\.10 DIS=(\S+)", text)), {"5"})

    def test_a_low_top_is_kept_with_warnings(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        top = fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -4.5),
                            origin=self.origin, bead_height_mm=2)
        np.testing.assert_allclose(top.raw_errors, -4.5, atol=0.05)
        self.assertTrue(top.warnings)
        self.assertIn("may have found the layer below", top.warnings[0])
        self.assertEqual(top.metadata()["warnings"], list(top.warnings))
        np.testing.assert_allclose(top.metadata()["errors_mm"], -4.5, atol=0.05)

    def test_a_reading_through_a_gap_never_lowers_the_next_layer_more_than_a_bead(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        top = fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -4.5),
                            origin=self.origin, bead_height_mm=2)
        np.testing.assert_allclose(top.errors, -2.0, atol=1e-9)
        seated = top.apply(self.path)
        before = {m.line_no: m.z for m in self.path.moves if m.layer == 2 and m.deposit}
        after = [m for m in seated.moves if m.layer == 2 and m.deposit]
        self.assertGreaterEqual(min(m.z - before.get(m.line_no, m.z) for m in after), -2.0 - 1e-9)
        self.assertTrue(any("seated higher than read" in w for w in top.warnings))

    def test_the_seat_never_falls_faster_than_the_tilt_limit(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        top = fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -1.9 if x > 50 else 0.0),
                            origin=self.origin, bead_height_mm=2)
        slope = np.tan(np.radians(layer_sense.MAX_TILT_DEG))
        for i in range(len(plan) - 1):
            (p, s), (q, t) = top.stations[i], top.stations[i + 1]
            if p == q and t > s:
                self.assertLessEqual(abs(top.errors[i + 1] - top.errors[i]), slope * (t - s) + 1e-9)
        self.assertTrue(np.all(top.errors >= top.raw_errors - 1e-9))

    def test_a_steep_step_is_a_warning(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        top = fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -2.0 if x > 50 else 0.0),
                            origin=self.origin, bead_height_mm=2)
        self.assertTrue(any("steps" in w for w in top.warnings))

    def test_an_in_model_top_has_no_warnings(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        top = fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -0.2),
                            origin=self.origin, bead_height_mm=2)
        self.assertEqual(top.warnings, ())

    def test_low_growth_warns_and_steps_no_slower_than_the_floor(self):
        plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
        top = fit_layer_top(self.path, 0, plan, touches(plan, self.origin, lambda x: 2 * (0.3 - 1)),
                            origin=self.origin, bead_height_mm=2)
        _, info = apply_auto(self.path, top, first=0, last=0, step_mm=2, bead_height_mm=2)
        self.assertAlmostEqual(info["growth_raw_mm"], 0.6, delta=0.02)
        self.assertAlmostEqual(info["growth_mm"], 1.2, places=6)
        self.assertTrue(any("grew" in w for w in info["warnings"]))

    def test_high_growth_is_followed_not_capped(self):
        plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
        top = fit_layer_top(self.path, 0, plan, touches(plan, self.origin, lambda x: 2 * (1.7 - 1)),
                            origin=self.origin, bead_height_mm=2)
        _, info = apply_auto(self.path, top, first=0, last=0, step_mm=2, bead_height_mm=2)
        self.assertAlmostEqual(info["growth_mm"], info["growth_raw_mm"], places=6)
        self.assertGreater(info["growth_mm"], 2 * 1.4)

    def test_a_reading_through_a_gap_does_not_slow_the_next_cycle(self):
        plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
        first = plan[0].expected[0]
        top = fit_layer_top(self.path, 0, plan,
                            touches(plan, self.origin, lambda x: -4.5 if x == first else -0.2),
                            origin=self.origin, bead_height_mm=2)
        _, info = apply_auto(self.path, top, first=0, last=0, step_mm=2, bead_height_mm=2)
        self.assertAlmostEqual(info["growth_raw_mm"], 2 - 0.2, delta=0.02)

    def test_very_high_growth_is_still_followed(self):
        plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
        top = fit_layer_top(self.path, 0, plan, touches(plan, self.origin, lambda x: 4.5),
                            origin=self.origin, bead_height_mm=2)
        _, info = apply_auto(self.path, top, first=0, last=0, step_mm=2, bead_height_mm=2)
        self.assertAlmostEqual(info["growth_mm"], info["growth_raw_mm"], places=6)

    def test_jobs_say_they_were_built_in_research_mode(self):
        self.assertEqual(layer_sense.mode_provenance(), ("RESEARCH SENSE 5MM WARN ONLY",))

    def test_records_from_another_job_are_still_refused(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=2)
        records = touches(plan, self.origin, lambda x: -0.2)
        with self.assertRaisesRegex(ValueError, "outside its search window"):
            fit_layer_top(self.path, 1, plan, touches(plan, self.origin, lambda x: -20.0),
                          origin=self.origin, bead_height_mm=2)
        other = layer_sense_plan(slicer.wall(60, 6, 2), 1, bead_height_mm=2)
        self.assertNotEqual(len(other), len(plan))
        with self.assertRaises(ValueError):
            fit_layer_top(self.path, 1, plan, records[:-1], origin=self.origin, bead_height_mm=2)


if __name__ == "__main__":
    unittest.main()
