import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams
from waam_pipeline.arcparams import (
    calibrated_layer_height_mm,
    compensating_wfs,
    ctwd_drift_per_layer,
    ctwd_from_current,
    cumulative_ctwd_mm,
    deposited_area_mm2,
    free_bead_area_mm2,
    layer_height_for_area,
    residual_drift_mm,
    solve,
    surface_deviation_from_corrections,
    wfs_for_layer_height,
    width_for_ctwd,
    wire_area_mm2,
)

WIRE = 1.143


class TestMassBalanceIsExact(unittest.TestCase):
    def test_area_matches_the_closed_form(self):
        for wfs, ts in ((4.0, 300.0), (5.5, 450.0), (6.0, 600.0)):
            with self.subTest(wfs=wfs, ts=ts):
                expected = wire_area_mm2(WIRE) * wfs * 1000.0 / ts
                self.assertAlmostEqual(deposited_area_mm2(WIRE, wfs, ts),
                                       expected, places=12)

    def test_area_is_linear_in_wire_feed_and_inverse_in_travel(self):
        base = deposited_area_mm2(WIRE, 4.0, 400.0)
        self.assertAlmostEqual(deposited_area_mm2(WIRE, 8.0, 400.0), 2 * base, places=12)
        self.assertAlmostEqual(deposited_area_mm2(WIRE, 4.0, 800.0), base / 2, places=12)

    def test_wfs_for_layer_height_round_trips(self):

        for h, w, ts in ((2.0, 7.0, 400.0), (1.6, 5.0, 660.0), (3.0, 9.0, 300.0)):
            with self.subTest(h=h, w=w):
                wfs = wfs_for_layer_height(h, w, ts, WIRE)
                area = deposited_area_mm2(WIRE, wfs, ts)
                self.assertAlmostEqual(layer_height_for_area(area, w), h, places=10)

    def test_layer_height_is_linear_in_wire_feed(self):

        w, ts = 7.0, 400.0
        h1 = layer_height_for_area(deposited_area_mm2(WIRE, 4.0, ts), w)
        h2 = layer_height_for_area(deposited_area_mm2(WIRE, 8.0, ts), w)
        self.assertAlmostEqual(h2, 2 * h1, places=10)


class TestTheShapeTrap(unittest.TestCase):


    def test_parabolic_and_rectangular_differ_by_exactly_three_halves(self):
        for w, h in ((5.0, 1.6), (7.0, 2.2), (9.0, 3.0)):
            with self.subTest(w=w, h=h):
                self.assertAlmostEqual((w * h) / free_bead_area_mm2(w, h), 1.5,
                                       places=12)

    def test_solve_uses_the_rectangular_convention(self):

        h, w, ts = 2.2, 7.0, 400.0
        sol = solve(layer_height_mm=h, bead_width_mm=w, travel_mm_min=ts,
                    wire_dia_mm=WIRE, filler="er70s6")
        implied = deposited_area_mm2(WIRE, sol.wfs_m_min, ts)
        self.assertAlmostEqual(implied, w * h, places=9)
        self.assertNotAlmostEqual(implied, free_bead_area_mm2(w, h), places=2)


class TestCompensationLaw(unittest.TestCase):


    def test_compensated_wire_feed_holds_layer_height_exactly(self):
        ts, h0, w0 = 400.0, 2.0, 6.5
        wfs0 = wfs_for_layer_height(h0, w0, ts, WIRE)
        for w1 in (5.5, 6.0, 6.5, 7.0, 8.0):
            with self.subTest(width_after=w1):
                wfs1 = compensating_wfs(wfs0, w0, w1)
                h1 = layer_height_for_area(deposited_area_mm2(WIRE, wfs1, ts), w1)
                self.assertAlmostEqual(h1, h0, places=10)

    def test_uncompensated_width_change_moves_the_layer_height(self):

        ts, h0, w0 = 400.0, 2.0, 6.5
        wfs0 = wfs_for_layer_height(h0, w0, ts, WIRE)

        h_drift = layer_height_for_area(deposited_area_mm2(WIRE, wfs0, ts), 7.0)
        self.assertLess(h_drift, h0)
        self.assertAlmostEqual(h_drift, h0 * w0 / 7.0, places=10)

    def test_no_width_change_means_no_correction(self):
        self.assertAlmostEqual(compensating_wfs(5.0, 7.0, 7.0), 5.0, places=12)


class TestCtwdSensitivityIsGuarded(unittest.TestCase):
    def test_it_refuses_while_unmeasured(self):
        self.assertIsNone(arcparams.CTWD_WIDTH_SENSITIVITY_MM_PER_MM)
        with self.assertRaises(ValueError) as ctx:
            width_for_ctwd(7.0, 1.0)
        self.assertIn("never been measured", str(ctx.exception))

    def test_it_works_once_a_sensitivity_is_supplied(self):

        saved = arcparams.CTWD_WIDTH_SENSITIVITY_MM_PER_MM
        try:
            arcparams.CTWD_WIDTH_SENSITIVITY_MM_PER_MM = -0.25
            self.assertAlmostEqual(width_for_ctwd(7.0, 2.0), 6.5, places=12)
        finally:
            arcparams.CTWD_WIDTH_SENSITIVITY_MM_PER_MM = saved
        self.assertIsNone(arcparams.CTWD_WIDTH_SENSITIVITY_MM_PER_MM)


class TestRejectsNonsense(unittest.TestCase):
    def test_non_positive_inputs_raise(self):
        for fn, args in (
            (deposited_area_mm2, (0.0, 4.0, 400.0)),
            (deposited_area_mm2, (WIRE, -1.0, 400.0)),
            (deposited_area_mm2, (WIRE, 4.0, 0.0)),
            (free_bead_area_mm2, (0.0, 2.0)),
            (layer_height_for_area, (10.0, 0.0)),


            (wfs_for_layer_height, (0.0, 7.0, 400.0)),
            (wfs_for_layer_height, (2.0, 0.0, 400.0)),
            (wfs_for_layer_height, (2.0, 7.0, -400.0)),
            (compensating_wfs, (5.0, 0.0, 7.0)),
        ):
            with self.subTest(fn=fn.__name__, args=args):
                with self.assertRaises(ValueError):
                    fn(*args)


class TestCtwdDriftIsNotVisibleToTheRobot(unittest.TestCase):


    def test_drift_accumulates_linearly_and_without_limit(self):
        ctwd = cumulative_ctwd_mm(12.0, 2.0, [1.85] * 60)
        self.assertAlmostEqual(ctwd[0], 12.15, places=9)
        self.assertAlmostEqual(ctwd[9], 13.50, places=9)
        self.assertAlmostEqual(ctwd[39], 18.00, places=9)
        self.assertAlmostEqual(ctwd[59], 21.00, places=9)

        self.assertAlmostEqual(ctwd[59] - 12.0, 60 * 0.15, places=9)

    def test_a_matched_step_does_not_drift(self):
        ctwd = cumulative_ctwd_mm(12.0, 2.0, [2.0] * 40)
        for v in ctwd:
            self.assertAlmostEqual(v, 12.0, places=12)

    def test_drift_can_go_the_other_way_and_crash_the_torch(self):

        ctwd = cumulative_ctwd_mm(12.0, 2.0, [2.3] * 30)
        self.assertLess(ctwd[-1], 12.0)
        self.assertAlmostEqual(ctwd[-1], 12.0 - 30 * 0.3, places=9)
        self.assertLess(ctwd[-1], 4.0)

    def test_robot_z_is_identical_whether_or_not_the_metal_is_right(self):

        step, layers = 2.0, 40
        z_torch = [step * (n + 1) for n in range(layers)]
        good = cumulative_ctwd_mm(12.0, step, [2.00] * layers)
        bad = cumulative_ctwd_mm(12.0, step, [1.85] * layers)

        self.assertEqual(z_torch, [step * (n + 1) for n in range(layers)])

        self.assertAlmostEqual(good[-1], 12.0, places=9)
        self.assertGreater(bad[-1] - good[-1], 5.0)

    def test_per_layer_drift_sign_convention(self):
        self.assertGreater(ctwd_drift_per_layer(2.0, 1.85), 0)
        self.assertLess(ctwd_drift_per_layer(2.0, 2.30), 0)
        self.assertAlmostEqual(ctwd_drift_per_layer(2.0, 2.0), 0.0, places=12)

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            ctwd_drift_per_layer(0.0, 2.0)
        with self.assertRaises(ValueError):
            cumulative_ctwd_mm(-1.0, 2.0, [2.0])


class TestClosedLoopCorrectionsAreTheMeasurement(unittest.TestCase):


    def test_corrections_sum_to_the_deviation(self):
        dev = surface_deviation_from_corrections([-0.15] * 5)
        self.assertAlmostEqual(dev[-1], -0.75, places=12)
        self.assertEqual(len(dev), 5)

    def test_it_reconstructs_the_open_loop_drift_it_would_have_cancelled(self):


        layers = 40
        would_have_drifted = cumulative_ctwd_mm(12.0, 2.0, [1.85] * layers)
        corrected = surface_deviation_from_corrections([-0.15] * layers)
        self.assertAlmostEqual(-corrected[-1], would_have_drifted[-1] - 12.0,
                               places=9)

    def test_no_correction_means_no_deviation(self):
        self.assertEqual(surface_deviation_from_corrections([0.0, 0.0, 0.0]),
                         [0.0, 0.0, 0.0])


class TestFeedforwardFromTheDeterministicSlice(unittest.TestCase):


    def test_calibration_recovers_the_real_layer_height(self):

        self.assertAlmostEqual(calibrated_layer_height_mm(2.0, 74.0, 40), 1.85,
                               places=12)

    def test_reslicing_at_the_measured_height_removes_the_systematic_drift(self):
        real = calibrated_layer_height_mm(2.0, 74.0, 40)
        self.assertAlmostEqual(residual_drift_mm(2.0, real, 60), 9.0, places=9)
        self.assertAlmostEqual(residual_drift_mm(real, real, 60), 0.0, places=12)

    def test_the_residual_is_what_decides_whether_feedback_is_needed(self):

        real = calibrated_layer_height_mm(2.0, 74.0, 40)
        residual = residual_drift_mm(real, real * 0.98, 60)
        self.assertAlmostEqual(residual, 60 * real * 0.02, places=9)
        self.assertGreater(residual, 2.0)

    def test_drift_scales_with_part_height(self):

        real = calibrated_layer_height_mm(2.0, 74.0, 40)
        short = residual_drift_mm(real, real * 0.98, 10)
        tall = residual_drift_mm(real, real * 0.98, 100)
        self.assertAlmostEqual(tall / short, 10.0, places=9)

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            calibrated_layer_height_mm(2.0, 74.0, 0)
        with self.assertRaises(ValueError):
            calibrated_layer_height_mm(2.0, -1.0, 40)
        with self.assertRaises(ValueError):
            residual_drift_mm(2.0, 1.85, -1)


class TestThroughTheArcSensing(unittest.TestCase):


    def test_it_refuses_while_the_slope_is_unmeasured(self):
        self.assertIsNone(arcparams.CTWD_CURRENT_SENSITIVITY_A_PER_MM)
        with self.assertRaises(ValueError) as ctx:
            ctwd_from_current(180.0, 190.0, 12.0)
        self.assertIn("never been measured", str(ctx.exception))

    def test_falling_current_reads_as_growing_stickout(self):

        saved = arcparams.CTWD_CURRENT_SENSITIVITY_A_PER_MM
        try:
            arcparams.CTWD_CURRENT_SENSITIVITY_A_PER_MM = -4.0

            self.assertAlmostEqual(ctwd_from_current(180.0, 190.0, 12.0), 14.5,
                                   places=9)

            self.assertAlmostEqual(ctwd_from_current(200.0, 190.0, 12.0), 9.5,
                                   places=9)

            self.assertAlmostEqual(ctwd_from_current(190.0, 190.0, 12.0), 12.0,
                                   places=12)
        finally:
            arcparams.CTWD_CURRENT_SENSITIVITY_A_PER_MM = saved
        self.assertIsNone(arcparams.CTWD_CURRENT_SENSITIVITY_A_PER_MM)

    def test_rejects_nonsense(self):
        for args in ((0.0, 190.0, 12.0), (180.0, -1.0, 12.0), (180.0, 190.0, 0.0)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    ctwd_from_current(*args)

    def test_the_burn_off_slope_still_has_no_ctwd_term(self):


        f = arcparams.FILLERS["er70s6"]
        self.assertAlmostEqual(f.amps_per_m_min_per_mm, 33.0, places=6)
        sol_lo = solve(layer_height_mm=2.0, bead_width_mm=7.0,
                       travel_mm_min=600.0, wire_dia_mm=WIRE)
        sol_hi = solve(layer_height_mm=2.0, bead_width_mm=7.0,
                       travel_mm_min=300.0, wire_dia_mm=WIRE)

        self.assertAlmostEqual(sol_hi.current_estimate_a / sol_lo.current_estimate_a,
                               sol_hi.wfs_m_min / sol_lo.wfs_m_min, places=9)


if __name__ == "__main__":
    unittest.main()
