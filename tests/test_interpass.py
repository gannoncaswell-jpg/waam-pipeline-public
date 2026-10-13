import copy
from dataclasses import asdict, replace
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline.interpass import (Bead, HeatHistory, Profile, Response,
                                     illustrative_profile, simulate, support_at)
from waam_pipeline.slicer import wall
from waam_pipeline.toolpath import Move, Toolpath


class TestHeatHistory(unittest.TestCase):
    def test_energy_normalisation_decay_and_spatial_locality(self):
        p = illustrative_profile()
        heat = HeatHistory(p)
        self.assertEqual(heat.temperature((0, 0, 0), 0), p.ambient_c)
        heat.add((0, 0, 0), 10, 100)
        rise = 100 / (p.heat_capacity_j_mm3_k * (2 * math.pi) ** 1.5 * p.heat_radius_mm ** 3)
        self.assertAlmostEqual(heat.temperature((0, 0, 0), 10) - p.ambient_c, rise)
        self.assertAlmostEqual(heat.temperature((0, 0, 0), 10 + p.cooling_tau_s) - p.ambient_c,
                               rise / math.e)
        self.assertAlmostEqual(heat.temperature((p.heat_radius_mm, 0, 0), 10) - p.ambient_c,
                               rise * math.exp(-0.5))
        self.assertAlmostEqual(heat.temperature((1000, 0, 0), 10), p.ambient_c)
        with self.assertRaisesRegex(ValueError, "before deposition"):
            heat.temperature((0, 0, 0), 0)

    def test_superposition_and_translation(self):
        p = illustrative_profile()
        heat, shifted = HeatHistory(p), HeatHistory(p)
        for h in (heat, shifted):
            offset = 0 if h is heat else 200
            h.add((offset, offset, offset), 0, 100)
            h.add((offset, offset, offset), 0, 200)
        self.assertAlmostEqual(heat.temperature((0, 0, 0), 5),
                               shifted.temperature((200, 200, 200), 5))


class TestSupport(unittest.TestCase):
    def test_stacking_adjacent_support_union_and_overhang(self):
        a, b = np.array([0., 0., 6.]), np.array([10., 0., 6.])
        beads = [Bead((0, -2, 3), (10, -2, 3), 3, 2.8, 4, 0),
                 Bead((0, 2, 3), (10, 2, 3), 3, 2.9, 4, 0)]
        coverage, z = support_at(a, b, 8, 3, beads, 0)
        self.assertEqual(coverage, 1)
        self.assertEqual(z, 2.8)
        self.assertEqual(support_at(a + [0, 30, 0], b + [0, 30, 0], 8, 3, beads, 0)[0], 0)
        self.assertEqual(support_at(a, b, 8, 6, beads, 0)[0], 0)
        self.assertEqual(support_at(a, b, 8, 0, [], 0), (1, 0))


class TestScheduling(unittest.TestCase):
    def setUp(self):
        self.p = illustrative_profile()
        self.tp = wall(60, 6, self.p.layer_height_mm)

    def test_continue_until_predicted_failure_then_cool_before_whole_pass(self):
        untouched = copy.deepcopy(self.tp)
        continuous = simulate(self.tp, self.p, policy="continuous")
        adaptive = simulate(self.tp, self.p)
        self.assertEqual(continuous["status"], "stopped")
        self.assertGreaterEqual(continuous["completed_passes"], 2)
        self.assertTrue(all(r["pause_s"] == 0 for r in continuous["passes"]))
        self.assertEqual(adaptive["status"], "complete")
        self.assertGreater(adaptive["cooling_s"], 0)
        self.assertEqual(adaptive["passes"][:2], continuous["passes"][:2])
        for r in adaptive["passes"]:
            self.assertFalse(r["reasons"])
            self.assertGreaterEqual(r["support_reserve"], self.p.min_support_reserve)
            self.assertLessEqual(r["width_error_mm"], self.p.max_width_error_mm)
            self.assertLessEqual(r["height_error_mm"], self.p.max_height_error_mm)
            self.assertEqual(r["start_move"], r["end_move"])
            if r["status"] == "cool":
                self.assertTrue(r["trigger"]["reasons"])
        self.assertEqual(self.tp, untouched)
        self.assertEqual(adaptive, simulate(self.tp, self.p))

    def test_hot_support_can_bind_with_perfect_dimensions(self):
        p = replace(self.p, response=(Response(25, 8.6, 2.9, 2),
                                     Response(900, 8.6, 2.9, 0)))
        tp = wall(60, 8, 2.9)
        r = simulate(tp, p, policy="continuous")
        self.assertEqual(r["passes"][-1]["reasons"], ["hot_support"])
        cooled = simulate(tp, p)
        self.assertEqual(cooled["status"], "complete")
        self.assertGreater(cooled["cooling_s"], 0)

    def test_missing_support_is_not_repaired_by_cooling(self):
        tp = wall(30, 2, 2.9)
        tp.moves[2:] = [replace(m, y=100) for m in tp.moves[2:]]
        r = simulate(tp, self.p)
        self.assertEqual(r["completed_passes"], 1)
        self.assertIn("support_coverage", r["passes"][-1]["reasons"])
        self.assertEqual(r["cooling_s"], 0)

    def test_cooling_does_not_erase_accumulated_height_error(self):
        r = simulate(wall(60, 12, 2.9), self.p)
        self.assertEqual(r["status"], "stopped")
        self.assertIn("height_error", r["passes"][-1]["reasons"])
        self.assertGreater(r["cooling_s"], 0)

    def test_natural_travel_time_cools_without_a_dwell(self):
        tp = wall(60, 2, 2.9)
        reference = simulate(tp, self.p, policy="continuous")
        detour = replace(tp.moves[2], y=1000)
        tp.moves.insert(2, detour)
        r = simulate(tp, self.p, policy="continuous")
        self.assertLess(r["passes"][1]["peak_interpass_c"],
                        reference["passes"][1]["peak_interpass_c"])
        self.assertEqual(r["cooling_s"], 0)
        self.assertGreater(r["elapsed_s"], reference["elapsed_s"])

    def test_long_pass_that_fails_from_cold_is_not_split(self):
        p = replace(self.p, net_power_w=50000)
        r = simulate(self.tp, p)
        self.assertEqual(r["status"], "stopped")
        self.assertEqual(r["completed_passes"], 0)
        self.assertEqual(len(r["passes"]), 1)
        self.assertEqual(r["cooling_s"], 0)
        self.assertIn("response_range", r["passes"][0]["reasons"])
        self.assertIsNone(r["passes"][0]["width_error_mm"])
        self.assertIsNone(r["passes"][0]["support_fraction"])

    def test_splitting_a_straight_move_does_not_change_schedule(self):
        tp = copy.deepcopy(self.tp)
        prev, first = tp.moves[:2]
        tp.moves.insert(1, replace(first, x=(prev.x + first.x) / 2))
        a, b = simulate(self.tp, self.p), simulate(tp, self.p)
        self.assertAlmostEqual(a["cooling_s"], b["cooling_s"])
        for x, y in zip(a["passes"], b["passes"]):
            self.assertAlmostEqual(x["peak_interpass_c"], y["peak_interpass_c"])

    def test_refinement_and_translation_keep_the_thermal_result_close(self):
        tp = wall(60, 2, 2.9)
        coarse = simulate(tp, self.p)
        fine = simulate(tp, replace(self.p, sample_mm=1))
        for a, b in zip(coarse["passes"], fine["passes"]):
            self.assertLess(abs(a["peak_interpass_c"] - b["peak_interpass_c"]), 10)
        shifted = Toolpath([replace(m, x=m.x+100, y=m.y-200, z=m.z+50) for m in tp.moves])
        moved = simulate(shifted, self.p, substrate_z_mm=50)
        for a, b in zip(coarse["passes"], moved["passes"]):
            self.assertAlmostEqual(a["peak_interpass_c"], b["peak_interpass_c"])

    def test_a_floating_first_layer_has_no_substrate_support(self):
        tp = Toolpath([replace(m, z=m.z+2) for m in self.tp.moves])
        r = simulate(tp, self.p)
        self.assertEqual(r["completed_passes"], 0)
        self.assertIn("support_coverage", r["passes"][0]["reasons"])

    def test_bounded_and_unsupported_inputs(self):
        for tp in (Toolpath(), Toolpath(self.tp.moves[1:]),
                   wall(1e6, 1, 2.9)):
            with self.assertRaises(ValueError):
                simulate(tp, self.p)
        ramp = copy.deepcopy(self.tp)
        ramp.moves[1] = replace(ramp.moves[1], z=4)
        with self.assertRaisesRegex(ValueError, "planar"):
            simulate(ramp, self.p)
        multi = copy.deepcopy(self.tp)
        multi.moves[2:] = [replace(m, layer=0) for m in multi.moves[2:]]
        with self.assertRaisesRegex(ValueError, "overlap/remelting"):
            simulate(multi, self.p)


class TestProfiles(unittest.TestCase):
    def test_missing_and_nonfinite_data_refused(self):
        with self.assertRaises(ValueError):
            Profile.from_dict({})
        for bad in (float("nan"), float("inf"), 0, -1, True, "100"):
            with self.subTest(bad=bad):
                data = asdict(illustrative_profile())
                data["cooling_tau_s"] = bad
                with self.assertRaises(ValueError):
                    Profile.from_dict(data)

    def test_response_domain_monotonicity_and_provenance(self):
        p = illustrative_profile()
        self.assertEqual(Profile.from_dict(asdict(p)), p)
        self.assertIsNone(p.at(1000))
        self.assertIsNone(p.at(0))
        self.assertEqual(p.at(25), p.response[0])
        for bad in (replace(p, response=tuple(reversed(p.response))),
                    replace(p, provenance=""), replace(p, evidence="validated"),
                    replace(p, min_support_fraction=1.1), replace(p, sample_mm=10)):
            with self.assertRaises(ValueError):
                bad.validate()


class TestVisualizationScheduling(unittest.TestCase):
    @staticmethod
    def goldak_profile():
        from tests.test_thermal import profile
        return replace(profile(), net_power_w=400, max_pause_s=5,
                       response=(Response(25, 4, 2, 3), Response(800, 4, 2, 0)))

    def test_every_non_viz_field_is_identical_for_complete_and_stopped_runs(self):
        p = self.goldak_profile()
        for policy in ("adaptive", "continuous"):
            for budget in (None, 12000, 0):
                with self.subTest(policy=policy, budget=budget):
                    tp = wall(4, 3, 2)
                    untouched = copy.deepcopy(tp)
                    off = simulate(tp, p, policy=policy)
                    on = simulate(tp, p, policy=policy, viz=True, viz_max_payload_bytes=budget)
                    payload = on.pop("viz")
                    self.assertIsNone(off.pop("viz"))
                    self.assertEqual(on, off)
                    self.assertEqual(tp, untouched)
                    if budget == 0:
                        self.assertEqual(set(payload), {"available", "reason"})
                        self.assertFalse(payload["available"])
                        self.assertIn("budget", payload["reason"])
                    else:
                        self.assertTrue(payload["available"])
                        self.assertTrue(payload["snapshots"])

    def test_rejected_deposition_does_not_leak_and_accepted_cooling_is_retained(self):
        from waam_pipeline.thermal import GoldakHistory
        p = self.goldak_profile()
        tp = wall(4, 3, 2)
        reference = simulate(tp, p)
        row = reference["passes"][1]
        self.assertGreater(row["pause_s"], 0)
        rejected_start = row["start_s"] - row["pause_s"]
        original_deposit = GoldakHistory.deposit
        poisoned = []

        def deposit(heat, a, b, start_s, duration_s, height_mm):
            if a[2] == 4 and math.isclose(start_s, rejected_start):
                heat._rejected_test_branch = True
            original_deposit(heat, a, b, start_s, duration_s, height_mm)
            if getattr(heat, "_rejected_test_branch", False):

                for _, frame in heat._viz_snapshots:
                    frame[0] = -12345
                poisoned.append(heat)

        with patch.object(GoldakHistory, "deposit", deposit):
            result = simulate(tp, p, viz=True)
        payload = result.pop("viz")
        reference.pop("viz")
        self.assertEqual(result, reference)
        self.assertTrue(poisoned)
        for frame in payload["snapshots"]:
            self.assertGreaterEqual(float(frame["temperature_c"].min()), p.ambient_c - 1e-5)

        self.assertTrue(any(rejected_start < f["time_s"] < row["start_s"]
                            for f in payload["snapshots"]))
        self.assertAlmostEqual(payload["snapshots"][-1]["time_s"], result["elapsed_s"])

    def test_gaussian_viz_is_unavailable_without_changing_report(self):
        p = illustrative_profile()
        tp = wall(10, 1, p.layer_height_mm)
        off = simulate(tp, p)
        on = simulate(tp, p, viz=True)
        self.assertFalse(on.pop("viz")["available"])
        off.pop("viz")
        self.assertEqual(on, off)


if __name__ == "__main__":
    unittest.main()
