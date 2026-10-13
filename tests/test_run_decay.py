import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.interpass import fit_decay, propose_dwell


def decay_row(tau=180.0, *, layer=1, pass_number=None, unit="C", offset=None,
              initial=420.0, ambient=25.0, seconds=(20, 40, 60)):
    readings = {}
    for second in seconds:
        value = ambient + (initial - ambient) * math.exp(-(second + (offset or 0.0)) / tau)
        readings[str(second)] = value * 9.0 / 5.0 + 32 if unit == "F" else value
    return {"layer": layer, "pass": pass_number, "unit": unit, "readings": readings,
            "park_travel_s_est": offset}


class TestDecayFit(unittest.TestCase):
    def test_recovers_tau_in_both_units(self):
        for unit in ("C", "F"):
            with self.subTest(unit=unit):
                fit = fit_decay([decay_row(unit=unit)])
                self.assertLess(abs(fit["tau_s"] / 180.0 - 1), 0.02)
                self.assertAlmostEqual(fit["rows"][0]["t0_c"], 420.0)
                self.assertEqual(fit["count"], 1)
                self.assertLess(fit["rms_c"], 1e-9)
                self.assertTrue(fit["valid"])
                self.assertEqual(fit["notes"], [])

    def test_park_travel_moves_the_weld_end_intercept(self):
        row = decay_row(offset=37.5, initial=550.0, seconds=(0, 20, 55, 90))
        fit = fit_decay([row])
        self.assertAlmostEqual(fit["tau_s"], 180.0)
        self.assertAlmostEqual(fit["rows"][0]["t0_c"], 550.0)
        without_offset = fit_decay([{**row, "park_travel_s_est": None}])
        self.assertAlmostEqual(without_offset["tau_s"], fit["tau_s"])
        self.assertAlmostEqual(without_offset["rows"][0]["t0_c"], row["readings"]["0"])

    def test_median_pool_and_spread(self):
        fit = fit_decay([decay_row(100, layer=1), decay_row(180, layer=2),
                         decay_row(240, layer=2, pass_number=2)])
        self.assertAlmostEqual(fit["tau_s"], 180.0)
        self.assertEqual(fit["count"], 3)
        self.assertEqual([(r["layer"], r["pass"]) for r in fit["rows"]],
                         [(1, None), (2, None), (2, 2)])
        self.assertAlmostEqual(fit["tau_spread"], 2.4)
        self.assertAlmostEqual(fit["spread_fraction"], 140 / 180)
        self.assertFalse(fit["valid"])
        self.assertTrue(any("tau varies" in note for note in fit["notes"]))

    def test_near_ambient_readings_are_skipped_with_notes(self):
        row = decay_row()
        row["readings"].update({"120": 30.0, "160": 20.0})
        fit = fit_decay([row, {"layer": 0, "unit": "C", "readings": {}}])
        self.assertAlmostEqual(fit["tau_s"], 180.0)
        self.assertEqual(fit["rows"][0]["count"], 3)
        self.assertTrue(fit["valid"])
        self.assertEqual(len(fit["notes"]), 3)
        self.assertTrue(any("120 s within 5 C" in note for note in fit["notes"]))
        self.assertEqual(fit["skipped"][0]["layer"], 0)

    def test_refuses_fewer_than_one_usable_row(self):
        for rows in ([], [decay_row(seconds=(20,))],
                     [{"unit": "C", "readings": {"20": 30, "40": 25}}],
                     [{"unit": "C", "readings": {"20": 100, "40": 110}}],
                     [{"unit": "C", "readings": {"20": 100, "40": 100}}],
                     [{"unit": "C", "readings": {"20": 200, "20.0": 100}}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "at least 1 usable row"):
                fit_decay(rows)

    def test_invalid_numbers_do_not_produce_a_fit(self):
        for value in (math.nan, math.inf, True, "hot"):
            row = decay_row(seconds=(20, 40))
            row["readings"]["20"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                fit_decay([row])
        for ambient in (math.nan, math.inf, None, True, 200):
            with self.subTest(ambient=ambient), self.assertRaises(ValueError):
                fit_decay([decay_row()], ambient_c=ambient)

    def test_custom_ambient(self):
        fit = fit_decay([decay_row(95.0, ambient=10)], ambient_c=10)
        self.assertAlmostEqual(fit["tau_s"], 95.0)
        self.assertEqual(fit["ambient_c"], 10)

    def test_proposal_uses_the_pooled_tau(self):
        fit = fit_decay([decay_row(100, layer=1), decay_row(180, layer=2),
                         decay_row(240, layer=3)])
        proposal = propose_dwell(fit, start_c=400, setpoint_c=150, after_c=200)
        self.assertAlmostEqual(proposal["tau_used_s"], 180.0)
        self.assertEqual(proposal["seconds"], math.ceil(180 * math.log(375 / 125)))
        self.assertEqual(proposal["hold_s"], math.ceil(180 * math.log(175 / 125)))
        self.assertEqual(proposal["basis"], "layer_decay")


if __name__ == "__main__":
    unittest.main()
