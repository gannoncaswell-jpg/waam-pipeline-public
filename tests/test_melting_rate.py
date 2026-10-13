import itertools
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams
from waam_pipeline.arcparams import (
    D11_AMPERAGE_TOL,
    D11_MIN_PREHEAT_C_CATEGORY_B_THIN,
    D11_VOLTAGE_TOL,
    ELECTRODE_EXTENSION_BAND_MM,
    NARROW_CURRENT_SPAN,
    WASZINK_1982_MILD_STEEL_1_2MM,
    ctwd_budget_mm,
    ctwd_warnings,
    current_for_wfs,
    d11_amperage_band,
    d_current_d_stickout,
    fit_lesnewich,
    melting_rate_m_min,
    resistive_fraction,
)


CHERNOVOL = arcparams.CHERNOVOL_2022_1_2MM
ASSUMED_L = arcparams.CHERNOVOL_ASSUMED_L_MM


def fitted():
    return fit_lesnewich(CHERNOVOL, stickout_mm=ASSUMED_L, allow_narrow=True)


class TestTheAlgebraCloses(unittest.TestCase):
    def test_current_inverts_the_melting_rate(self):
        a, b, _ = fitted()
        for wfs in (2.5, 4.0, 6.0):
            for L in (8.0, 11.0, 15.0):
                with self.subTest(wfs=wfs, L=L):
                    i = current_for_wfs(wfs, L, a, b)
                    self.assertAlmostEqual(melting_rate_m_min(i, L, a, b), wfs,
                                           places=10)

    def test_the_fit_reproduces_the_published_table(self):
        a, b, r2 = fitted()
        self.assertGreater(r2, 0.98)
        for wfs, current in CHERNOVOL:
            with self.subTest(current=current):
                self.assertAlmostEqual(
                    melting_rate_m_min(current, ASSUMED_L, a, b), wfs, delta=0.11)

    def test_derivative_matches_a_numerical_one(self):

        a, b, _ = fitted()
        h = 1e-6
        for wfs, L in ((3.0, 9.0), (4.0, 11.0), (5.0, 14.0)):
            with self.subTest(wfs=wfs, L=L):
                numeric = ((current_for_wfs(wfs, L + h, a, b)
                            - current_for_wfs(wfs, L - h, a, b)) / (2 * h))
                self.assertAlmostEqual(d_current_d_stickout(wfs, L, a, b),
                                       numeric, places=4)


class TestPhysicalDirections(unittest.TestCase):
    def test_current_falls_as_stickout_grows(self):

        a, b, _ = fitted()
        currents = [current_for_wfs(4.0, L, a, b) for L in (8, 10, 12, 14, 16)]
        self.assertEqual(currents, sorted(currents, reverse=True))
        self.assertLess(d_current_d_stickout(4.0, 11.0, a, b), 0.0)

    def test_current_rises_with_wire_feed(self):
        a, b, _ = fitted()
        currents = [current_for_wfs(w, 11.0, a, b) for w in (2.5, 3.5, 4.5, 5.5)]
        self.assertEqual(currents, sorted(currents))

    def test_resistive_share_grows_with_stickout_and_current(self):
        a, b, _ = fitted()
        self.assertLess(resistive_fraction(160.0, 8.0, a, b),
                        resistive_fraction(160.0, 14.0, a, b))
        self.assertLess(resistive_fraction(120.0, 11.0, a, b),
                        resistive_fraction(200.0, 11.0, a, b))

    def test_the_resistive_term_is_real_but_secondary(self):

        a, b, _ = fitted()
        for current, L in ((123, 8.0), (160, 11.0), (181, 14.0)):
            with self.subTest(current=current, L=L):
                frac = resistive_fraction(current, L, a, b)
                self.assertGreater(frac, 0.10)
                self.assertLess(frac, 0.35)


class TestFittingGuards(unittest.TestCase):
    def test_a_narrow_current_span_is_refused_by_default(self):

        with self.assertRaises(ValueError) as ctx:
            fit_lesnewich([(4.0, 164), (4.7, 190), (4.4, 178)], stickout_mm=8.0)
        self.assertIn("spans only", str(ctx.exception))

    def test_the_refusal_can_be_acknowledged_not_bypassed_silently(self):
        a, b, r2 = fit_lesnewich([(4.0, 164), (4.7, 190), (4.4, 178)],
                                 stickout_mm=8.0, allow_narrow=True)
        self.assertGreater(a, 0)
        self.assertGreater(b, 0)

    def test_chernovol_is_the_marginal_case_the_threshold_is_set_around(self):
        span = (181 - 123) / 181
        self.assertLess(span, NARROW_CURRENT_SPAN)
        self.assertGreater(span, 0.30)

    def test_two_points_are_refused_however_wide(self):
        with self.assertRaises(ValueError):
            fit_lesnewich([(2.9, 123), (4.6, 181)], stickout_mm=11.0)

    def test_coefficients_are_unset_for_this_cell(self):
        self.assertIsNone(arcparams.LESNEWICH_A)
        self.assertIsNone(arcparams.LESNEWICH_B)
        with self.assertRaises(ValueError) as ctx:
            current_for_wfs(4.0, 11.0)
        self.assertIn("not been measured", str(ctx.exception))

    def test_rejects_nonsense(self):
        a, b, _ = fitted()
        for fn, args in ((melting_rate_m_min, (0.0, 11.0)),
                         (melting_rate_m_min, (160.0, -1.0)),
                         (current_for_wfs, (0.0, 11.0)),
                         (current_for_wfs, (4.0, 0.0))):
            with self.subTest(fn=fn.__name__, args=args):
                with self.assertRaises(ValueError):
                    fn(*args, a, b)


class TestAgainstIndependentPublishedData(unittest.TestCase):


    WFS = 375 * 25.4 / 1000.0
    ARC = 4.0
    MEASURED = ((0.375, 354), (0.75, 278), (1.5, 219))

    def test_predicts_currents_within_ten_percent(self):
        a, b, _ = fitted()
        for ctwd_in, measured in self.MEASURED:
            with self.subTest(ctwd_in=ctwd_in):
                L = ctwd_in * 25.4 - self.ARC
                pred = current_for_wfs(self.WFS, L, a, b)
                self.assertLess(abs(pred - measured) / measured, 0.10)

    def test_predicts_the_slope_the_sensor_depends_on(self):

        a, b, _ = fitted()
        pts = self.MEASURED
        for (c0, i0), (c1, i1) in zip(pts, pts[1:]):
            with self.subTest(span=(c0, c1)):
                L0, L1 = c0 * 25.4 - self.ARC, c1 * 25.4 - self.ARC
                meas = (i1 - i0) / (L1 - L0)
                model = ((current_for_wfs(self.WFS, L1, a, b)
                          - current_for_wfs(self.WFS, L0, a, b)) / (L1 - L0))
                self.assertLess(abs(model - meas) / abs(meas), 0.20)
                self.assertLess(model, 0)

    def test_the_slope_flattens_as_stickout_grows(self):

        a, b, _ = fitted()
        near = d_current_d_stickout(self.WFS, 12.0, a, b)
        far = d_current_d_stickout(self.WFS, 30.0, a, b)
        self.assertLess(abs(far), abs(near))


class TestCtwdWarnings(unittest.TestCase):


    def test_a_spray_length_ctwd_is_flagged_at_our_currents(self):
        warns = ctwd_warnings(19.0, 165.0, 1.143,
                              observed_mode="short-circuit", arc_length_mm=3.)
        self.assertTrue(any("above" in w for w in warns))
        self.assertTrue(any("Electrode extension" in w for w in warns))

    def test_the_floor_does_not_condemn_the_cited_source_or_our_own_ctwd(self):


        three_eighths_in_mm = 0.375 * 25.4
        floor, ceiling = ELECTRODE_EXTENSION_BAND_MM["short-circuit"]
        self.assertLessEqual(floor, three_eighths_in_mm)
        self.assertGreaterEqual(ceiling, 0.5 * 25.4)

        cell = json.loads((ROOT / "examples" / "cell_lc50s.json").read_text(
            encoding="utf-8"))
        self.assertAlmostEqual(cell["ctwd_mm"], three_eighths_in_mm, places=6,
                               msg="the LC50S profile no longer welds at 3/8 in "
                                   "-- re-check this band against whatever it "
                                   "does weld at")
        warns = ctwd_warnings(cell["ctwd_mm"], 165.0, 1.143)
        self.assertFalse(any("below the" in w or "above the" in w for w in warns),
                         f"the band complains about the cell's own CTWD: {warns}")


        self.assertEqual(len(warns), 1)
        self.assertIn("not validated", warns[0])

    def test_a_sane_short_circuit_pairing_only_notes_the_mode(self):
        warns = ctwd_warnings(12.0, 165.0, 1.143)
        self.assertFalse(any("above" in w or "below the" in w for w in warns))
        self.assertEqual(len(warns), 1)

    def test_an_untabulated_diameter_still_reports_unvalidated(self):
        for dia in (1.6, 2.0, 3.0, 0.4):
            with self.subTest(dia=dia):
                self.assertIn("not validated", ctwd_warnings(20.0, 275.0, dia)[0])


class TestAwsD11Tolerances(unittest.TestCase):


    def test_the_published_tolerances(self):
        self.assertAlmostEqual(D11_AMPERAGE_TOL, 0.10, places=6)
        self.assertAlmostEqual(D11_VOLTAGE_TOL, 0.07, places=6)

    def test_voltage_is_the_tighter_constraint(self):

        self.assertLess(D11_VOLTAGE_TOL, D11_AMPERAGE_TOL)

    def test_amperage_band(self):
        lo, hi = d11_amperage_band(170.0)
        self.assertAlmostEqual(lo, 153.0, places=6)
        self.assertAlmostEqual(hi, 187.0, places=6)
        with self.assertRaises(ValueError):
            d11_amperage_band(0.0)

    def test_the_prequalified_interpass_floor_is_zero_for_our_steel(self):


        self.assertAlmostEqual(D11_MIN_PREHEAT_C_CATEGORY_B_THIN, 0.0, places=6)


class TestWhichConstraintBinds(unittest.TestCase):


    def test_ctwd_budget_from_amperage_exceeds_the_short_circuit_band(self):
        a, b, _ = fitted()
        lo, hi = ELECTRODE_EXTENSION_BAND_MM["short-circuit"]
        band_width = hi - lo
        for wfs in (3.0, 4.0, 5.0):
            with self.subTest(wfs=wfs):
                budget = ctwd_budget_mm(wfs, 8.0, a, b)
                self.assertGreater(budget, band_width)

    def test_the_budget_shrinks_as_current_rises(self):


        a, b, _ = fitted()
        low = ctwd_budget_mm(3.0, 8.0, a, b)
        high = ctwd_budget_mm(6.0, 8.0, a, b)
        self.assertLess(high, low)

    def test_budget_scales_with_the_tolerance(self):
        a, b, _ = fitted()
        self.assertAlmostEqual(ctwd_budget_mm(4.0, 8.0, a, b, tol=0.20),
                               2 * ctwd_budget_mm(4.0, 8.0, a, b, tol=0.10),
                               delta=0.02)


class TestWaszinkCoefficients(unittest.TestCase):


    def test_the_published_sanity_point(self):

        a, b = WASZINK_1982_MILD_STEEL_1_2MM
        self.assertAlmostEqual(melting_rate_m_min(200.0, 15.0, a, b), 4.80,
                               delta=0.05)

    def _perturbed(self, stickout_mm):


        out = []
        for signs in itertools.product((-1.0, 0.0, 1.0), repeat=len(CHERNOVOL)):
            pts = [(w, i + s) for (w, i), s in zip(CHERNOVOL, signs)]
            out.append(fit_lesnewich(pts, stickout_mm=stickout_mm,
                                     allow_narrow=True))
        return out

    def test_the_chernovol_fit_cannot_distinguish_itself_from_waszink(self):


        for L in (8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0):
            with self.subTest(L=L):
                fits = self._perturbed(L)
                bs = [b for _, b, _ in fits]
                as_ = [a for a, _, _ in fits]
                nominal_a, nominal_b, _ = fit_lesnewich(
                    CHERNOVOL, stickout_mm=L, allow_narrow=True)
                spread_b = (max(bs) - min(bs)) / nominal_b
                spread_a = (max(as_) - min(as_)) / nominal_a

                self.assertGreater(max(bs) / min(bs), 1.4)

                self.assertGreater(spread_b, 4 * 0.09)

                self.assertLess(spread_a, spread_b / 3.0)

    def test_the_direction_claim_is_set_by_the_assumption_not_by_the_data(self):


        _, wb = WASZINK_1982_MILD_STEEL_1_2MM
        sides = set()
        for L in (8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0):
            _, b, _ = fit_lesnewich(CHERNOVOL, stickout_mm=L, allow_narrow=True)
            sides.add(b > wb)
        self.assertEqual(sides, {True, False},
                         "the sign of (Chernovol b - Waszink b) no longer "
                         "flips across plausible arc lengths -- if the arc "
                         "length has been MEASURED, this test should be "
                         "replaced by a real ordering assertion")

    def test_the_arc_length_assumption_is_visible_outside_the_test_file(self):


        self.assertEqual(ASSUMED_L, arcparams.CHERNOVOL_ASSUMED_L_MM)
        self.assertEqual(CHERNOVOL, arcparams.CHERNOVOL_2022_1_2MM)

    def test_the_linear_coefficient_is_the_one_comparison_L_cannot_move(self):


        wa, _ = WASZINK_1982_MILD_STEEL_1_2MM
        first = None
        for L in (8.0, 11.0, 15.0):
            a, _, _ = fit_lesnewich(CHERNOVOL, stickout_mm=L, allow_narrow=True)
            first = a if first is None else first
            self.assertAlmostEqual(a, first, places=12)
            self.assertLess(abs(a - wa) / wa, 0.25)

    def test_module_coefficients_stay_unset(self):

        self.assertIsNone(arcparams.LESNEWICH_A)
        self.assertIsNone(arcparams.LESNEWICH_B)


if __name__ == "__main__":
    unittest.main()
