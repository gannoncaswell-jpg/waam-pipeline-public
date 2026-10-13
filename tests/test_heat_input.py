import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.arcparams import GMAW_ARC_EFFICIENCY, solve


def _sol(travel_mm_min: float):

    return solve(layer_height_mm=2.0, bead_width_mm=7.0,
                 travel_mm_min=travel_mm_min, wire_dia_mm=1.143,
                 filler="er70s6")


class TestChernovolTable2(unittest.TestCase):


    BLOCKS = (
        (16.7, 123, 53.0, 0.186),
        (17.7, 160, 38.0, 0.358),
        (19.3, 181, 22.0, 0.762),
    )

    def test_net_heat_input_reproduces_their_published_numbers(self):
        for volts, amps, ts_cm_min, published in self.BLOCKS:
            with self.subTest(kJ_mm=published):
                got = _sol(ts_cm_min * 10).heat_input_kj_mm(
                    volts, amps, efficiency=GMAW_ARC_EFFICIENCY)
                self.assertAlmostEqual(got, published, places=3)

    def test_gross_is_what_we_return_by_default_and_it_is_higher(self):

        for volts, amps, ts_cm_min, published in self.BLOCKS:
            with self.subTest(kJ_mm=published):
                gross = _sol(ts_cm_min * 10).heat_input_kj_mm(volts, amps)
                self.assertGreater(gross, published)
                self.assertAlmostEqual(gross * GMAW_ARC_EFFICIENCY, published,
                                       places=3)

    def test_the_gap_between_conventions_is_25_percent(self):

        volts, amps, ts_cm_min, _ = self.BLOCKS[1]
        sol = _sol(ts_cm_min * 10)
        gross = sol.heat_input_kj_mm(volts, amps)
        net = sol.heat_input_kj_mm(volts, amps, efficiency=GMAW_ARC_EFFICIENCY)
        self.assertAlmostEqual(gross / net, 1.25, places=6)


class TestEfficiencyParameter(unittest.TestCase):
    def test_efficiency_one_is_the_historical_behaviour(self):
        sol = _sol(380.0)
        self.assertAlmostEqual(sol.heat_input_kj_mm(17.7, 160),
                               sol.heat_input_kj_mm(17.7, 160, efficiency=1.0),
                               places=12)

    def test_efficiency_scales_linearly(self):
        sol = _sol(380.0)
        base = sol.heat_input_kj_mm(17.7, 160)
        for eta in (0.6, 0.8, 1.0):
            self.assertAlmostEqual(sol.heat_input_kj_mm(17.7, 160, efficiency=eta),
                                   base * eta, places=12)

    def test_the_published_efficiency_is_the_one_the_sources_used(self):


        self.assertAlmostEqual(GMAW_ARC_EFFICIENCY, 0.8, places=6)


if __name__ == "__main__":
    unittest.main()
