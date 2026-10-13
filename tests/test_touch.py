import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, touch


class TestPlaneFit(unittest.TestCase):
    def test_level_plane_recovers_height_and_zero_tilt(self):
        z = 165.8
        pts = [(1000, -400, z), (1100, -400, z), (1100, -200, z), (1000, -200, z)]
        f = touch.fit_plane(pts)
        self.assertAlmostEqual(f.tilt_deg, 0.0, places=6)
        self.assertLess(f.residual_mm, 1e-9)
        self.assertAlmostEqual(f.height_at(1050, -300), z, places=6)
        np.testing.assert_allclose(f.normal, [0, 0, 1], atol=1e-9)

    def test_tilted_plane_reports_tilt_and_interpolates(self):

        def zf(x):
            return 165.8 + (x - 1000.0) * 0.01
        pts = [(1000, -400, zf(1000)), (1200, -400, zf(1200)),
               (1200, -200, zf(1200)), (1000, -200, zf(1000))]
        f = touch.fit_plane(pts)
        self.assertAlmostEqual(f.tilt_deg, np.degrees(np.arctan(0.01)), places=4)
        self.assertLess(f.residual_mm, 1e-9)
        self.assertAlmostEqual(f.height_at(1100, -300), zf(1100), places=6)

    def test_out_of_plane_hit_shows_up_as_residual(self):
        z = 165.8
        pts = [(1000, -400, z), (1200, -400, z), (1200, -200, z), (1000, -200, z + 2.0)]
        f = touch.fit_plane(pts)
        self.assertGreater(f.residual_mm, 0.3)

    def test_collinear_and_too_few_are_refused(self):
        with self.assertRaises(ValueError):
            touch.fit_plane([(0, 0, 0), (1, 0, 0)])
        with self.assertRaises(ValueError):
            touch.fit_plane([(0, 0, 0), (1, 0, 0), (2, 0, 0)])

    def test_tip_offset_shifts_along_the_normal(self):
        z = 165.8
        hits = [touch.Hit("a", np.array([1000, -400, z])),
                touch.Hit("b", np.array([1200, -400, z])),
                touch.Hit("c", np.array([1200, -200, z]))]
        f = touch.face_from_hits(hits, tip_offset_mm=1.5)
        self.assertAlmostEqual(f.height_at(1100, -300), z - 1.5, places=6)


class TestPartOrigin(unittest.TestCase):
    def test_origin_from_face_and_two_edges(self):
        z, x0, y0 = 165.8, 1014.0, -300.0
        face = [touch.Hit(f"f{i}", np.array(p)) for i, p in enumerate(
            [(980, -340, z), (1080, -340, z), (1080, -240, z), (980, -240, z)])]
        xe = [touch.Hit("x1", np.array([x0, -280, z + 5])),
              touch.Hit("x2", np.array([x0, -320, z + 5]))]
        ye = [touch.Hit("y1", np.array([1040, y0, z + 5])),
              touch.Hit("y2", np.array([1060, y0, z + 5]))]
        r = touch.part_origin_from_hits(face, xe, ye)
        np.testing.assert_allclose(r["origin"], [x0, y0, z], atol=1e-6)
        self.assertAlmostEqual(r["face_tilt_deg"], 0.0, places=6)
        self.assertEqual(r["face_hits"], 4)
        self.assertAlmostEqual(r["x_spread_mm"], 0.0, places=6)

    def test_edge_spread_reports_scatter(self):
        hits = [touch.Hit("a", np.array([1014.0, 0, 0])),
                touch.Hit("b", np.array([1014.4, 0, 0]))]
        self.assertAlmostEqual(touch.edge_from_hits(hits, 0), 1014.2, places=6)
        self.assertAlmostEqual(touch.edge_spread_mm(hits, 0), 0.4, places=6)


class TestProbePlan(unittest.TestCase):
    def test_plan_probes_downward_from_above_the_face(self):
        plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)
        self.assertEqual(len(plan), 4)
        for p in plan:
            np.testing.assert_allclose(p.direction, [0, 0, -1], atol=1e-9)
            self.assertGreater(p.start[2], p.expected[2])
            self.assertLess(p.target[2], p.expected[2])
            self.assertAlmostEqual(p.expected[2], cell_lc.PLATTER_FACE_Z, places=6)
        self.assertGreater(len({tuple(p.expected[:2]) for p in plan}), 2)

    def test_plan_hits_round_trip_to_the_known_face(self):
        z = cell_lc.PLATTER_FACE_Z
        plan = touch.platter_probe_plan(z)
        hits = [touch.Hit(p.name, p.expected.copy()) for p in plan]
        self.assertEqual(touch.check_hits(plan, hits), [])
        f = touch.face_from_hits(hits)
        self.assertAlmostEqual(f.height_at(1014.0, -300.0), z, places=6)

    def test_overtravel_is_small(self):


        self.assertLessEqual(touch.SEARCH_OVERTRAVEL_MM, 8.92 - 3.0)
        for plan in (touch.platter_probe_plan(100.0), touch.line_probe_plan((0, 0), (100, 0), 100.0)):
            for p in plan:
                self.assertAlmostEqual(p.expected[2] - p.target[2], touch.SEARCH_OVERTRAVEL_MM)


class TestLineProbePlan(unittest.TestCase):
    def test_stations_are_downward_collinear_and_evenly_spaced(self):
        z = cell_lc.PLATTER_FACE_Z
        a, b = (1000.0, -350.0), (1150.0, -350.0)
        plan = touch.line_probe_plan(a, b, z, stations=5)
        self.assertEqual(len(plan), 5)
        for p in plan:
            np.testing.assert_allclose(p.direction, [0, 0, -1], atol=1e-9)
            self.assertGreater(p.start[2], p.expected[2])
            self.assertLess(p.target[2], p.expected[2])
            self.assertAlmostEqual(p.expected[2], z, places=6)
        np.testing.assert_allclose(plan[0].expected[:2], a, atol=1e-9)
        np.testing.assert_allclose(plan[-1].expected[:2], b, atol=1e-9)
        steps = np.diff([p.expected[:2] for p in plan], axis=0)
        self.assertLess(float(np.abs(steps - steps[0]).max()), 1e-9)

    def test_hits_on_the_plan_pass_the_generic_check(self):
        z = cell_lc.PLATTER_FACE_Z
        plan = touch.line_probe_plan((980.0, -300.0), (980.0, -180.0), z, stations=4)
        hits = [touch.Hit(p.name, p.expected.copy()) for p in plan]
        self.assertEqual(touch.check_hits(plan, hits), [])

    def test_bad_inputs_are_refused(self):
        z = cell_lc.PLATTER_FACE_Z
        with self.assertRaises(ValueError):
            touch.line_probe_plan((0, 0), (100, 0), z, stations=1)
        with self.assertRaises(ValueError):
            touch.line_probe_plan((10, 10), (10, 10), z)
        with self.assertRaises(ValueError):
            touch.line_probe_plan((0, 0), (np.nan, 0), z)
        with self.assertRaises(ValueError):
            touch.line_probe_plan((0, 0), (100, 0), z, overtravel_mm=0)


class TestLineProfile(unittest.TestCase):
    def test_reports_tilt_and_flatness_along_the_line(self):
        a, b = (1000.0, -300.0), (1150.0, -300.0)
        s = np.linspace(0.0, 150.0, 6)
        z = 165.0 + 0.01 * s
        z[3] += 0.3
        pts = np.column_stack([a[0] + s, np.full_like(s, a[1]), z])
        prof = touch.line_profile(pts, a, b)
        self.assertEqual(len(prof.s), 6)
        self.assertAlmostEqual(prof.span_mm, 150.0, places=6)
        self.assertAlmostEqual(prof.slope_deg, np.degrees(np.arctan(0.01)), delta=0.1)
        self.assertGreater(prof.flatness_mm, 0.15)
        self.assertLess(prof.flatness_mm, 0.4)
        self.assertAlmostEqual(prof.z_at(0.0), prof.z[0], places=6)

    def test_flat_line_reads_near_zero(self):
        a, b = (1000.0, -300.0), (1100.0, -300.0)
        pts = np.array([[1000, -300, 144.00], [1050, -300, 144.02],
                        [1100, -300, 143.99]], float)
        prof = touch.line_profile(pts, a, b)
        self.assertLess(prof.peak_to_peak_mm, 0.05)
        self.assertLess(prof.flatness_mm, 0.05)

    def test_bad_inputs_refused(self):
        with self.assertRaises(ValueError):
            touch.line_profile(np.zeros((1, 3)), (0, 0), (100, 0))
        with self.assertRaises(ValueError):
            touch.line_profile(np.zeros((3, 3)), (0, 0), (0, 0))


class TestBadProbeDetection(unittest.TestCase):
    def test_missed_and_stray_hits_are_reported(self):
        plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)
        hits = [touch.Hit(p.name, p.expected.copy()) for p in plan[:-1]]
        hits.append(touch.Hit("stray", np.array([0.0, 0.0, 0.0])))
        problems = touch.check_hits(plan, hits)
        self.assertTrue(any("no hit recorded" in p for p in problems))
        self.assertTrue(any("stray" in p for p in problems))

    def test_hit_far_from_expected_is_flagged(self):
        plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)
        hits = [touch.Hit(p.name, p.expected.copy()) for p in plan]
        hits[0] = touch.Hit(plan[0].name, plan[0].expected + np.array([0.0, 0.0, -40.0]))
        problems = touch.check_hits(plan, hits)
        self.assertEqual(len(problems), 1)
        self.assertIn("from expected", problems[0])


if __name__ == "__main__":
    unittest.main()
