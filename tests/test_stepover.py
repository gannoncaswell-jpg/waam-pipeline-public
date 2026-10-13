import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.arcparams import (
    STEPOVER_TYPICAL_SPAN,
    STEPOVER_WHR_RANGE,
    optimal_stepover_fraction,
    overlap_percent,
    stepover_fraction_from_overlap_percent,
    stepover_fraction_band,
    stepover_mm,
)


class TestAgainstThePaper(unittest.TestCase):
    def test_reproduces_table_2(self):


        for w, h, published in ((11.44, 3.77, 0.6755),
                                (8.60, 2.50, 0.6557),
                                (9.93, 2.38, 0.6394)):
            with self.subTest(w=w, h=h):
                self.assertAlmostEqual(
                    optimal_stepover_fraction(w, h), published, delta=0.002)

    def test_reproduces_the_quoted_band_for_typical_beads(self):


        self.assertAlmostEqual(optimal_stepover_fraction(2.5, 1.0), 0.7732, delta=0.0002)
        self.assertAlmostEqual(optimal_stepover_fraction(5.0, 1.0), 0.6317, delta=0.0002)

    def test_flatter_beads_want_a_smaller_fraction(self):

        ratios = [optimal_stepover_fraction(whr, 1.0)
                  for whr in (2.5, 3.0, 3.5, 4.0, 5.0, 8.0, 12.0, 20.0)]
        self.assertEqual(ratios, sorted(ratios, reverse=True))

    def test_it_is_not_the_number_everyone_quotes(self):


        a = optimal_stepover_fraction(8.60, 2.50)
        b = optimal_stepover_fraction(11.44, 3.77)
        self.assertNotAlmostEqual(a, b, places=3)
        for value in (a, b):
            self.assertNotAlmostEqual(value, 2 / 3, places=3)
            self.assertNotAlmostEqual(value, 0.738, places=3)


class TestDomainGuard(unittest.TestCase):
    def test_refuses_below_the_fitted_floor(self):


        lo, _ = STEPOVER_WHR_RANGE
        with self.assertRaises(ValueError):
            optimal_stepover_fraction(2.16, 1.0)
        with self.assertRaises(ValueError):
            optimal_stepover_fraction(lo - 0.01, 1.0)

        self.assertLess(optimal_stepover_fraction(lo, 1.0), 1.0)

    def test_refuses_above_the_fitted_ceiling(self):
        _, hi = STEPOVER_WHR_RANGE
        with self.assertRaises(ValueError):
            optimal_stepover_fraction(hi + 0.01, 1.0)

    def test_refuses_nonsense_geometry(self):
        for w, h in ((0.0, 3.0), (-9.0, 3.0), (9.0, 0.0), (9.0, -3.0)):
            with self.subTest(w=w, h=h):
                with self.assertRaises(ValueError):
                    optimal_stepover_fraction(w, h)

    def test_the_error_says_what_to_do(self):
        try:
            optimal_stepover_fraction(9.0, 4.0)
        except ValueError as exc:
            self.assertIn("Measure the bead", str(exc))
        else:
            self.fail("expected a refusal")


class TestStepoverMm(unittest.TestCase):
    def test_millimetres_are_the_fraction_times_the_width(self):
        w, h = 8.60, 2.50
        self.assertAlmostEqual(
            stepover_mm(w, h), optimal_stepover_fraction(w, h) * w, places=9)


        self.assertLess(stepover_mm(w, h), w)
        self.assertGreater(stepover_mm(w, h), w / 2)


class TestAgainstASecondIndependentPaper(unittest.TestCase):


    RUNS = (
        ("25-2", 4.50, 2.60, None),
        ("45-3", 6.15, 2.10, (26.3, 50.0)),
        ("45-4", 7.50, 2.30, (14.7, 45.3)),
        ("45-6", 9.30, 2.70, (15.1, 39.8)),
    )

    def test_our_optimum_lands_inside_their_measured_windows(self):
        for name, w, h, window in self.RUNS:
            if window is None:
                continue
            with self.subTest(run=name):
                eta = optimal_stepover_fraction(w, h)
                lo = stepover_fraction_from_overlap_percent(window[1])
                hi = stepover_fraction_from_overlap_percent(window[0])
                self.assertGreaterEqual(eta, lo)
                self.assertLessEqual(eta, hi)

    def test_the_set_we_refuse_is_the_one_they_could_not_weld(self):


        name, w, h, window = self.RUNS[0]
        self.assertIsNone(window)
        self.assertLess(w / h, STEPOVER_WHR_RANGE[0])
        with self.assertRaises(ValueError):
            optimal_stepover_fraction(w, h)


class TestOverlapConvention(unittest.TestCase):
    def test_round_trip(self):
        for eta in (0.5, 0.6317, 0.6557, 0.738, 0.9):
            self.assertAlmostEqual(
                stepover_fraction_from_overlap_percent(overlap_percent(eta)),
                eta, places=12)

    def test_the_two_conventions_are_inverses_not_the_same_number(self):

        self.assertAlmostEqual(overlap_percent(0.6557), 34.43, places=2)
        self.assertAlmostEqual(stepover_fraction_from_overlap_percent(40.0), 0.60, places=9)


class TestMeasurementSensitivity(unittest.TestCase):


    BEADS = ((5.0, 1.6), (7.0, 2.2), (9.0, 3.0))


    FLOOR_BEAD = (9.6, 4.0)

    def test_nominal_sits_inside_its_own_band(self):
        for w, h in self.BEADS + (self.FLOOR_BEAD,):
            with self.subTest(w=w, h=h):
                band = stepover_fraction_band(w, h, 0.5, 0.2)
                self.assertLessEqual(band.low, band.nominal)
                self.assertLessEqual(band.nominal, band.high)

    def test_a_band_bounded_by_the_domain_says_so(self):


        band = stepover_fraction_band(*self.FLOOR_BEAD, 0.5, 0.2)
        self.assertTrue(band.truncated)
        self.assertAlmostEqual(band.high, band.nominal, places=12)
        self.assertLess(band.low, band.nominal)

        self.assertFalse(stepover_fraction_band(9.0, 3.0, 0.5, 0.2).truncated)

    def test_ordinary_tolerance_swamps_the_model_on_the_narrowest_bead(self):


        band = stepover_fraction_band(5.0, 1.6, 0.5, 0.2)
        self.assertGreater((band.high - band.low) / STEPOVER_TYPICAL_SPAN, 0.80)

    def test_a_perfect_measurement_gives_a_zero_width_band(self):
        for w, h in self.BEADS + (self.FLOOR_BEAD,):
            band = stepover_fraction_band(w, h, 0.0, 0.0)
            self.assertAlmostEqual(band.low, band.nominal, places=12)
            self.assertAlmostEqual(band.high, band.nominal, places=12)
            self.assertFalse(band.truncated, "a zero tolerance box has no "
                             "corner to lose")

    def test_our_placeholder_bead_is_elsokatys_worst_condition(self):


        from waam_pipeline.clearance import check
        import inspect
        sig = inspect.signature(check)
        self.assertEqual(sig.parameters["bead_width_mm"].default, 9.0)
        self.assertEqual(sig.parameters["bead_height_mm"].default, 3.0)

        self.assertGreater(optimal_stepover_fraction(9.0, 3.0), 0.0)

    def test_rejects_negative_tolerances(self):
        with self.assertRaises(ValueError):
            stepover_fraction_band(9.0, 3.0, -0.1, 0.2)


if __name__ == "__main__":
    unittest.main()
