import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, demos
from waam_pipeline.clearance import CONTACT_OK, Scene, TorchModel, blocking, check, check_point, point_guard, ClearanceError
from waam_pipeline.substrate import Substrate, from_request


def rectangle(tilt=0, yaw=0, center=(1014, -180, 160)):
    corners = np.array([[-100, -50, 0], [100, -50, 0], [100, 50, 0], [-100, 50, 0]], float)
    rotation = cell_lc._rot((0, 0, 1), yaw) @ cell_lc._rot((0, 1, 0), tilt)
    return corners @ rotation.T + center


def registration(points=None):
    points = rectangle() if points is None else points
    ctwd = TorchModel.yaskawa_accufast_45().stackup.ctwd_mm
    solve = cell_lc.pulse_solver(np.zeros(3))
    records = []
    for i, point in enumerate(points):
        gap = 10 + i * 2
        q = solve(np.asarray(point) + [0, 0, gap - ctwd])
        pulses = np.rint(np.asarray(q) * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist()
        records.append({"pulses": pulses, "tip_distance_mm": gap})
    return {"version": 1, "frame": "lc50s_tool0_pulse", "weld_ctwd_mm": ctwd, "corners": records}


class TestSubstrate(unittest.TestCase):
    def test_plane_and_solid_use_four_points_and_thickness_downward(self):
        points = rectangle(3, 27)
        plate = Substrate.fit(points, 8)
        self.assertAlmostEqual(plate.tilt, 3)
        self.assertLess(plate.residual, 1e-10)
        mesh = plate.mesh()
        heights = (mesh - plate.origin) @ plate.basis[:, 2]
        self.assertTrue(np.allclose(heights.max(), 0))
        self.assertTrue(np.allclose(heights.min(), -8))
        np.testing.assert_allclose(plate.basis.T @ plate.basis, np.eye(3), atol=1e-12)
        thick = Substrate.fit(points, 15)
        np.testing.assert_allclose(thick.origin, plate.origin)
        np.testing.assert_allclose(thick.place(demos.demo_bead()).deposit_points(), plate.place(demos.demo_bead()).deposit_points())

    def test_winding_and_input_errors(self):
        points = rectangle()
        self.assertAlmostEqual(Substrate.fit(points[::-1], 6).residual, 0)
        for bad in (points[[0, 2, 1, 3]], points[[0, 1, 1, 3]], np.zeros((4, 3)), points * np.nan):
            with self.subTest(points=bad), self.assertRaises(ValueError):
                Substrate.fit(bad, 6)
        with self.assertRaisesRegex(ValueError, "5 degrees"):
            Substrate.fit(rectangle(6), 6)
        points[2, 2] += 4
        with self.assertRaisesRegex(ValueError, "flatness"):
            Substrate.fit(points, 6)
        for bad in (0, -1, 101, float("nan")):
            with self.assertRaises(ValueError):
                Substrate.fit(rectangle(), bad)

    def test_bead_follows_plane_and_respects_real_edges(self):
        plate = Substrate.fit(rectangle(3, 30), 6.35)
        path = plate.place(demos.demo_bead(), bottom=0, bead_width=8.6)
        self.assertAlmostEqual(path.deposit_length(), 101.6)
        for p in path.deposit_points():
            self.assertAlmostEqual(float((np.asarray(p) - [0, 0, 2]) @ plate.basis[:, 2]), 0)
        for args in ({"ox": 50}, {"oy": 46}, {"bead_width": 110}):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "edge margin"):
                plate.place(demos.demo_bead(), **args)

    def test_measured_tip_gaps_normalize_each_corner_before_fit(self):
        expected = rectangle(2, 15)
        data = registration(expected)
        plate = from_request(json.dumps(data), 6.35)
        np.testing.assert_allclose(plate.points, expected, atol=.025)
        self.assertLess(plate.residual, .025)
        self.assertAlmostEqual(plate.tilt, 2, delta=.025)
        wrong = json.loads(json.dumps(data))
        wrong["corners"][0]["tip_distance_mm"] += 10
        with self.assertRaisesRegex(ValueError, "flatness"):
            from_request(json.dumps(wrong), 6.35)

    def test_unsupported_frames_and_malformed_pulses_are_refused(self):
        good = registration()
        for change in ({"version": True}, {"frame": "user_frame"}, {"weld_ctwd_mm": 12}, {"corners": []}):
            with self.assertRaises(ValueError):
                from_request(json.dumps(dict(good, **change)), 6)
        for pulses in ([0] * 7, [0] * 5, [float("nan")] * 6, [True] * 6, [10**9] * 6):
            bad = json.loads(json.dumps(good))
            bad["corners"][0]["pulses"] = pulses
            with self.subTest(pulses=pulses), self.assertRaises(ValueError):
                from_request(json.dumps(bad), 6)

    def test_penetration_is_blocked_below_top_and_below_underside(self):
        plate = Substrate.fit(rectangle(3), 6).shifted([1014, -180, 160])
        scene = Scene(substrates=[plate])
        torch = TorchModel.yaskawa_accufast_45()
        self.assertFalse(blocking(check_point([0, 0, 2], torch, scene, contact_ok=CONTACT_OK)))
        for depth in (.004, 1, 10):
            with self.subTest(depth=depth):
                hits = blocking(check_point([0, 0, -depth], torch, scene, contact_ok=CONTACT_OK))
                self.assertEqual(hits[0].obstacle, "bead_test_coupon")
                with self.assertRaises(ClearanceError):
                    point_guard(torch, scene)((0, 0, -depth), "approach")
        path = plate.place(demos.demo_bead())
        self.assertFalse(blocking(check(path, torch, scene, contact_ok=CONTACT_OK)))

    def test_pybullet_receives_the_convex_solid_in_metres(self):
        sys.path.insert(0, str(ROOT))
        from sim.pybullet_driver import scene_body_specs
        plate = Substrate.fit(rectangle(3, 25), 6)
        specs = scene_body_specs(Scene(substrates=[plate.shifted(plate.origin)]), plate.origin)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].kind, "convex")
        actual = np.asarray(specs[0].vertices) + specs[0].position
        expected = np.unique(plate.mesh().reshape(-1, 3), axis=0) / 1000
        np.testing.assert_allclose(actual, expected, atol=1e-12)
        np.testing.assert_allclose(actual[list(specs[0].indices)], plate.mesh().reshape(-1, 3) / 1000, atol=1e-12)


class TestSubstrateServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from waam_pipeline import server
        cls.server = server
        cls.raw = json.dumps(registration(rectangle(2, 12)))

    def setUp(self):
        from unittest.mock import patch
        self.addCleanup(patch.stopall)
        patch.object(self.server, "_STATES", {}).start()
        patch.object(self.server, "_SHOW_CELL", True).start()
        patch.object(self.server, "_HOSTED_ASSETS", False).start()

    def slice(self, **overrides):
        return self.server.run_slice(dict(kind="wall", shape="bead", substrate_registration=self.raw,
                                          **overrides), None, workspace="substrate-test")

    def test_save_replay_placement_and_thickness_share_the_measured_datum(self):
        first = self.slice()
        self.assertEqual(first["blocking"], 0)
        self.assertEqual(first["unreachable"], 0)
        self.assertEqual(len(first["state"]["scene"]["meshes"]["bead_test_coupon"]), 108)
        request = json.loads(json.dumps(self.server._state("substrate-test")["request"]["params"]))
        replay = self.server.run_slice(request, None, workspace="replay")
        self.assertEqual(replay["state"]["toolpath"], first["state"]["toolpath"])
        changed = self.server.run_placement({"sub": "12", "ox": "10", "oy": "5", "yaw": "30"}, "substrate-test")
        np.testing.assert_allclose(changed["placement"]["origin"], first["placement"]["origin"])
        self.assertEqual(changed["state"]["meta"]["substrate"]["thickness_mm"], 12)
        self.assertEqual(changed["state"]["meta"]["params"]["substrate_registration"], self.raw)
        tp = self.server._state("substrate-test")["toolpath"]
        self.assertAlmostEqual(tp.deposit_length(), 101.6)
        em = self.server._state("substrate-test")["emit"]
        self.assertEqual(len(em["scene"].substrates), 1)
        self.assertEqual(em["substrate_registration"], self.raw)

    def test_touching_wire_tip_puts_the_first_weld_ctwd_above_the_plate(self):


        stickout = 8.92
        ctwd = TorchModel.yaskawa_accufast_45().stackup.ctwd_mm
        solve = cell_lc.pulse_solver(np.zeros(3))
        corners = []
        for point in rectangle():
            pulses = np.rint(np.asarray(solve(point)) * cell_lc.ar1440.PULSES_PER_DEG)
            corners.append({"pulses": pulses.astype(int).tolist(), "tip_distance_mm": stickout})
        raw = json.dumps({"version": 2, "frame": "lc50s_tool0_pulse", "weld_ctwd_mm": ctwd,
                          "gap_direction": "torch", "tool0_tip_offset_mm": stickout,
                          "corners": corners})
        plate = from_request(raw, 6.35)
        self.assertAlmostEqual(plate.origin[2], 160.0 + ctwd - stickout, delta=0.05)
        self.assertAlmostEqual(plate.origin[2] + plate.physical_shift_robot[2], 160.0, delta=0.05)

        result = self.server.run_slice(dict(kind="wall", shape="bead", substrate_registration=raw),
                                       None, workspace="touch-test")
        self.assertEqual(result["blocking"], 0)
        st = self.server._state("touch-test")
        first_weld_tcp = np.asarray(st["emit"]["origin"]) + st["toolpath"].deposit_points()[0]
        self.assertAlmostEqual(first_weld_tcp[2] + stickout, 160.0 + ctwd, delta=0.05)

    def test_failed_corner_or_outside_placement_keeps_the_accepted_job(self):
        self.slice()
        state = self.server._state("substrate-test")
        before = state["toolpath"]
        for params in ({"ox": "900"}, {"substrate_registration": "{}"}, {"yaw": "90"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.server.run_placement(params, "substrate-test")
            self.assertIs(state["toolpath"], before)
            self.assertTrue(state["registration_pending"])
            self.assertIn('"registration_pending":true', state["html"])

    def test_registered_placement_refuses_more_than_one_deposit_segment(self):
        from unittest.mock import patch
        from waam_pipeline.demos import demo_bead
        for extra in demo_bead().moves:
            path = demo_bead()
            path.moves.append(extra)
            with self.subTest(deposit=extra.deposit), patch.object(self.server.demos, "demo_bead", return_value=path):
                with self.assertRaisesRegex(ValueError, "one deposited bead segment"):
                    self.server.run_slice(dict(kind="wall", shape="bead", substrate_registration=self.raw),
                                          None, workspace="substrate-test", registration_action="apply")

    def test_length_speed_and_removal_invalidate_no_longer_valid_geometry(self):
        self.slice()
        result = self.server.run_placement({"bead_length": "76.2"}, "substrate-test")
        self.assertAlmostEqual(self.server._state("substrate-test")["toolpath"].deposit_length(), 76.2)
        self.assertEqual(result["state"]["meta"]["params"]["bead_length"], "76.2")
        result = self.server.run_placement({"substrate_registration": "", "ox": "0", "oy": "0"}, "substrate-test")
        self.assertIsNone(result["state"]["meta"]["substrate"])
        self.assertNotIn("bead_test_coupon", result["state"]["scene"]["meshes"])
        self.assertFalse(self.server._state("substrate-test")["emit"]["scene"].substrates)
        self.slice(travel="220")
        self.assertEqual({m.feed for m in self.server._state("substrate-test")["toolpath"].moves if m.deposit}, {220})


if __name__ == "__main__":
    unittest.main()
