import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.demos import demo_wall
from waam_pipeline.thermal_plan import (
    DEFAULT,
    ThermalModel,
    fit_lumped,
    layer_passes,
    schedule,
    simulate,
)


def passes(count, *, arc_s=35.4, gap_s=5.0, dwell=True):
    return [{"arc_s": arc_s,
             "gap_s": gap_s if index + 1 < count else 0.0,
             "bead_dwell": dwell and index + 1 < count}
            for index in range(count)]


class ThermalPlanTests(unittest.TestCase):
    def test_closed_form_heat_and_cooling(self):
        model = ThermalModel(100.0, 10.0, 20.0, 200.0, "test fit")
        decay = math.exp(-0.5)

        self.assertAlmostEqual(model.heat(50.0, 5.0),
                               20.0 + 20.0 * (1.0 - decay) + 30.0 * decay)
        self.assertAlmostEqual(model.cool(50.0, 5.0), 20.0 + 30.0 * decay)
        self.assertEqual(model.heat(50.0, 0.0), 50.0)
        self.assertEqual(model.cool(50.0, 0.0), 50.0)

    def test_simulation_is_monotonic_with_dwell(self):
        layers = [{"layer": 0, "passes": passes(4)},
                  {"layer": 1, "passes": passes(4)}]
        hot = simulate(DEFAULT, layers, start_c=80.0, layer_dwell_s=0.0,
                       bead_dwell_s=0.0)
        cool_beads = simulate(DEFAULT, layers, start_c=80.0, layer_dwell_s=0.0,
                              bead_dwell_s=60.0)
        cool_layers = simulate(DEFAULT, layers, start_c=80.0, layer_dwell_s=60.0,
                               bead_dwell_s=60.0)

        self.assertLess(cool_beads["max_peak_c"], hot["max_peak_c"])
        self.assertLess(cool_layers["rows"][1]["start_c"],
                        cool_beads["rows"][1]["start_c"])
        self.assertEqual(len(hot["rows"][0]["pass_peaks_c"]), 4)

    def test_default_reproduces_run_001_layer_ends(self):
        cases = [(102.0, 607.0, 5), (182.0, 554.0, 4), (149.0, 538.0, 4)]
        for start_c, measured_c, count in cases:
            result = simulate(
                DEFAULT,
                [{"layer": 0, "passes": passes(count, dwell=False)}],
                start_c=start_c,
                layer_dwell_s=0.0,
                bead_dwell_s=0.0,
            )
            predicted_c = result["rows"][0]["end_c"]
            self.assertLess(abs(predicted_c - measured_c) / measured_c, 0.06)

    def test_fit_lumped_recovers_default_capacity_from_run_001(self):
        rows = [
            {"start_c": 102.0, "end_c": 607.0, "arc_s": 177.0, "wall_s": 197.0},
            {"start_c": 182.0, "end_c": 554.0, "arc_s": 141.6, "wall_s": 156.6},
            {"start_c": 149.0, "end_c": 538.0, "arc_s": 141.6, "wall_s": 156.6},
        ]

        fit = fit_lumped(
            {"layers": rows,
             "decay": {"tau_s": DEFAULT.tau_s, "ambient_c": DEFAULT.ambient_c}},
            power_w=DEFAULT.power_w,
            ambient_c=DEFAULT.ambient_c,
        )

        self.assertLess(abs(fit["C_j_per_k"] / DEFAULT.C_j_per_k - 1.0), 0.02)
        self.assertEqual(fit["count"], 3)
        self.assertEqual(len(fit["residuals_c"]), 3)

    def test_fit_lumped_recovers_tau_and_capacity_from_layer_heating_only(self):
        ambient, tau, capacity, power = 30.0, 240.0, 720.0, DEFAULT.power_w
        rows = []
        for start, arc_s, wall_s, cooling_s in (
                (45.0, 40.0, 55.0, 8.0),
                (80.0, 70.0, 95.0, 25.0),
                (120.0, 30.0, 45.0, 60.0)):
            decay = math.exp(-wall_s / tau)
            heated = (ambient + (start - ambient) * decay
                       + power * arc_s / wall_s * tau * (1.0 - decay) / capacity)
            end = ambient + (heated - ambient) * math.exp(-cooling_s / tau)
            rows.append({"start_c": start, "end_c": end, "arc_s": arc_s,
                         "wall_s": wall_s, "cooling_s": cooling_s})

        fit = fit_lumped({"layers": rows}, power_w=power, ambient_c=ambient)

        self.assertEqual((fit["tau_basis"], fit["decay_count"], fit["valid"]),
                         ("layers", 0, True))
        self.assertAlmostEqual(fit["tau_s"], tau, places=6)
        self.assertAlmostEqual(fit["C_j_per_k"], capacity, places=6)

    def test_schedule_matches_closed_form(self):
        layer = {"layer": 0, "passes": passes(20, gap_s=0.0)}
        result = schedule(DEFAULT, [layer], start_c=30.0, target_preweld_c=100.0,
                          max_peak_c=250.0)

        numerator = DEFAULT.K_c * (1.0 - math.exp(-35.4 / DEFAULT.tau_s))
        expected = (-DEFAULT.tau_s
                    * math.log(1.0 - numerator / (250.0 - DEFAULT.ambient_c))
                    - 35.4)
        self.assertLessEqual(abs(result["bead_dwell_s"] - expected), 1.0)
        self.assertEqual(result["bead_dwell_s"], math.ceil(expected))
        self.assertLessEqual(result["max_peak_c"], 250.0)
        self.assertTrue(result["valid"])

    def test_unreachable_limits_return_notes(self):
        layers = [{"layer": 0, "passes": passes(2, arc_s=120.0)},
                  {"layer": 1, "passes": passes(2, arc_s=120.0)}]
        result = schedule(DEFAULT, layers, start_c=100.0, target_preweld_c=20.0,
                          max_peak_c=100.0, max_bead_dwell_s=10.0,
                          max_layer_dwell_s=10.0)

        self.assertFalse(result["valid"])
        self.assertTrue(result["notes"])
        self.assertEqual(result["bead_dwell_s"], 10.0)
        self.assertEqual(result["layer_dwell_s"], 10.0)

    def test_schedule_searches_the_real_starting_trajectory(self):
        layers = [{"layer": layer, "passes": passes(1, arc_s=30.0, gap_s=0.0)}
                  for layer in range(3)]
        result = schedule(DEFAULT, layers, start_c=30.0, target_preweld_c=150.0,
                          max_peak_c=200.0)

        self.assertTrue(result["valid"])
        self.assertEqual(result["bead_dwell_s"], 0.0)
        self.assertEqual(result["layer_dwell_s"], 333.0)
        self.assertLessEqual(result["max_peak_c"], 200.0)

    def test_layer_passes_counts_multibead_wall(self):
        toolpath = demo_wall(length_mm=100.0, layers=3, layer_height_mm=2.0,
                             feed_mm_min=200.0, thickness_mm=24.0,
                             bead_width_mm=8.0)
        rows = layer_passes(toolpath, travel_mm_min=1200.0)

        self.assertEqual([len(row["passes"]) for row in rows], [4, 4, 4])
        self.assertTrue(all(math.isclose(pass_["arc_s"], 30.0)
                            for row in rows for pass_ in row["passes"]))
        self.assertTrue(all(pass_["bead_dwell"]
                            for row in rows for pass_ in row["passes"][:-1]))
        self.assertTrue(all(not row["passes"][-1]["bead_dwell"] for row in rows))


if __name__ == "__main__":
    unittest.main()
