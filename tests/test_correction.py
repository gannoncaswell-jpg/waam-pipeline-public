import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, server
from waam_pipeline.clearance import (CONTACT_OK, DEFAULT_MARGIN_MM, Scene,
                                     TorchModel, blocking, check)
from waam_pipeline.correction import repair_travels
from waam_pipeline.gcode import parse
from waam_pipeline.slicer import to_gcode
from waam_pipeline.toolpath import Move, Toolpath
from test_server import binary_stl, cylinder


def weld_segments(tp):
    return [(a.target, b.target, b.feed, b.layer) for a, b in zip(tp.moves, tp.moves[1:])
            if b.deposit]


class TestTheRepairSearchesAtTheEmitMargin(unittest.TestCase):


    def test_one_constant(self):
        import inspect

        from waam_pipeline import emit

        self.assertEqual(
            inspect.signature(repair_travels).parameters["margin_mm"].default,
            DEFAULT_MARGIN_MM)
        for fn in (emit.emit_job, emit.emit_ros2):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(
                    inspect.signature(fn).parameters["margin_mm"].default,
                    DEFAULT_MARGIN_MM)


class TestTravelCorrection(unittest.TestCase):
    def setUp(self):
        self.torch = TorchModel(samples=((5.0, 2.0),))
        self.scene = Scene(boxes=[("clamp", np.array([-5., -10., 0.]),
                                   np.array([5., 10., 30.]))])
        self.tp = parse("G90\nM83\nG0 X-40 Y0 Z5\nG1 X-30 E1 F200\n"
                        "G0 X30\nG1 X40 E1\n")

    def test_routes_over_fixture_and_preserves_every_welding_segment(self):
        original = list(self.tp.moves)
        result = repair_travels(self.tp, self.torch, self.scene)
        self.assertGreater(result.before, 0)
        self.assertEqual(result.repaired_travels, 1)
        self.assertFalse(blocking(check(result.toolpath, self.torch, self.scene)))
        self.assertEqual(weld_segments(self.tp), weld_segments(result.toolpath))
        self.assertEqual(self.tp.moves, original)
        self.assertGreater(max(m.z for m in result.toolpath.moves), 30)

        replay = parse(to_gcode(result.toolpath))
        np.testing.assert_allclose([m.target for m in replay.moves],
                                   [m.target for m in result.toolpath.moves], atol=.001)

    def test_obstructed_destination_stays_blocked(self):
        tp = parse("G90\nG0 X-40 Y0 Z5\nG0 X0\nG1 X1 E1\n")
        result = repair_travels(tp, self.torch, self.scene)
        self.assertTrue(blocking(result.violations))
        self.assertEqual(weld_segments(tp), weld_segments(result.toolpath))
        self.assertEqual(result.summary()["status"], "unresolved")

    def test_check_only_does_not_change_path(self):
        result = repair_travels(self.tp, self.torch, self.scene, enabled=False)
        self.assertIs(result.toolpath, self.tp)
        self.assertGreater(result.before, 0)

    def test_travel_crossing_finished_bead_is_detected_and_repaired(self):
        tp = parse("G90\nM83\nG0 X0 Y-30 Z5\nG1 Y30 E1\n"
                   "G0 Z30\nG0 X-30 Y0\nG0 Z5\nG0 X30\n")
        torch = TorchModel(samples=((1., 2.),))
        result = repair_travels(tp, torch, Scene())
        self.assertGreater(result.before, 0)
        self.assertFalse(blocking(result.violations))
        self.assertEqual(weld_segments(tp), weld_segments(result.toolpath))

    def test_first_point_and_far_below_platter_are_blocked(self):
        scene = Scene(boxes=[("charles_platter", np.array([-100., -100., -6.35]),
                               np.array([100., 100., 0.]))])
        for z in (-1., -40.):
            tp = Toolpath([Move(0, 0, z, None, False, 0, 1)])
            hits = blocking(check(tp, self.torch, scene, contact_ok=CONTACT_OK))
            self.assertTrue(hits)
            self.assertEqual(hits[0].move_index, 0)
            self.assertAlmostEqual(hits[0].clearance_mm, z)

    def test_snap_includes_start_of_rising_weld(self):
        tp = parse("G90\nG0 X0 Y0 Z-2\nG1 X10 Z4 E1\n")
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014, -180)
        self.assertAlmostEqual(origin[2] - 2, cell_lc.PLATTER_FACE_Z)


class TestLayerChangeLeavesThePoolVertically(unittest.TestCase):


    def test_repaired_layer_change_rises_straight_out_of_the_pool(self):
        from waam_pipeline.plan import build_plan

        tp = Toolpath([Move(0, 0, 2, None, False, 0, 1), Move(60, 0, 2, 200, True, 0, 2),
                       Move(30, 30, 4, None, False, 1, 3), Move(0, 30, 4, 200, True, 1, 4)])
        torch = TorchModel.yaskawa_accufast_45()

        self.assertTrue(blocking(check(tp, torch, Scene(), bead_height_mm=2.0)))
        fixed = repair_travels(tp, torch, Scene(), bead_height_mm=2.0)
        self.assertFalse(blocking(fixed.violations))
        steps = build_plan(fixed.toolpath, lambda xyz: (0.0,) * 6,
                           interpass_timer_s=0, settle_s=0)
        motion = [s for s in steps if s.is_motion and s.xyz is not None]
        last = max(i for i, s in enumerate(motion) if s.role == "weld" and s.layer == 0)
        weld_end, out = motion[last].xyz, motion[last + 1].xyz
        self.assertEqual(out[:2], weld_end[:2])
        self.assertGreater(out[2], weld_end[2] + 2.0)


class TestPlacement(unittest.TestCase):
    def setUp(self):
        self.ws = "correction-tests"
        self.mesh = cylinder(radius=5., height=9., around=12)

        self.shadow = patch.object(server, "ROBOT_SHADOW_MAX_MOVES", 0)
        self.shadow.start()

    def tearDown(self):
        self.shadow.stop()
        server._STATES.pop(self.ws, None)

    def slice(self, **params):
        return server.run_slice(dict(kind="stl", lh="3", units="mm", fixture="0", **params),
                                binary_stl(self.mesh), self.ws)

    def state(self):
        return server._state(self.ws)

    def test_mesh_bottom_and_first_weld_tcp_seat_consistently(self):


        result = self.slice(sub="0")
        origin = result["placement"]["origin"]
        self.assertAlmostEqual(min(m.z for m in self.state()["toolpath"].moves if m.deposit)
                               + origin[2], cell_lc.PLATTER_FACE_Z)
        part = np.asarray(result["state"]["part"]["tris"], float).reshape(-1, 3)
        self.assertAlmostEqual(part[:, 2].min() + origin[2], cell_lc.PLATTER_FACE_Z, places=2)


        platter = [v for v in check(self.state()["toolpath"],
                   self.state()["emit"]["torch"], self.state()["emit"]["scene"],
                   bead_height_mm=3.0, contact_ok=CONTACT_OK) if v.obstacle == "charles_platter"]
        self.assertTrue(platter)
        self.assertEqual({v.clearance_mm for v in platter}, {0.0})
        self.assertFalse(blocking(platter))

    def test_server_stored_origin_agrees_with_emit_and_every_layer_stacks(self):
        lh = 3.0
        for substrate in (0.0, 6.35):
            with self.subTest(substrate=substrate):
                result = self.slice(sub=str(substrate))
                origin = np.asarray(result["placement"]["origin"])
                state = self.state()
                tp = state["toolpath"]


                self.assertEqual(state["emit"]["bead_height"], lh)
                cli_origin = cell_lc.snap_toolpath_to_platter(
                    tp, *origin[:2], substrate_mm=substrate)
                np.testing.assert_allclose(cli_origin, origin, atol=1e-9, rtol=0)

                surface = cell_lc.platter_work_surface_z(substrate)
                layers = tp.layers()
                self.assertEqual(len(layers), 3)
                previous_top = surface
                for k, moves in enumerate(layers):
                    points = Toolpath(moves).deposit_points()
                    self.assertTrue(points)


                    laying = origin[2] + min(p[2] for p in points)
                    top = origin[2] + max(p[2] for p in points) + lh
                    self.assertAlmostEqual(laying, surface + k * lh)
                    self.assertAlmostEqual(laying, previous_top)
                    self.assertAlmostEqual(top, laying + lh)
                    previous_top = top

    def test_positioning_reuses_slice_and_rotation_does_not_accumulate(self):
        self.slice()
        before = weld_segments(self.state()["toolpath"])
        original_z = self.state()["emit"]["origin"][2]
        with patch.object(server, "slice_mesh", side_effect=AssertionError("resliced")):
            server.run_placement({"ox": "10", "oy": "-20", "yaw": "90"}, self.ws)
            self.assertAlmostEqual(self.state()["emit"]["origin"][2], original_z)
            first = weld_segments(self.state()["toolpath"])
            server.run_placement({"yaw": "90"}, self.ws)
            self.assertEqual(first, weld_segments(self.state()["toolpath"]))
            server.run_placement({"yaw": "0"}, self.ws)
            self.assertEqual(before, weld_segments(self.state()["toolpath"]))

    def test_display_pivot_uses_source_weld_endpoints_and_center_uses_rotated_footprint(self):


        source = parse("G90\nM83\nG0 X0 Y0 Z3\nG1 X100 E1 F200\n"
                       "G0 X20 Y20\nG1 X30 E1\nG0 X0 Y1000 Z100\n")
        with patch.object(server.demos, "build", return_value=source):
            server.run_slice({"shape": "wall", "lh": "3", "yaw": "45", "travel": "200",
                              "repair": "0", "fixture": "0", "sub": "7", "oz": "2"}, None, self.ws)
        state = self.state()
        placed = json.loads(re.search(r"const S = (\{.*?\});\n", state["html"]).group(1))["meta"]["placement"]
        self.assertEqual(placed["pivot"], [50.0, 10.0])
        prior_z = placed["origin"][2]
        ox, oy = placed["center_offsets"]
        result = server.run_placement({"ox": str(ox), "oy": str(oy)}, self.ws)
        world = np.asarray(result["placement"]["footprint"]) + np.asarray(result["placement"]["origin"])[:2]
        np.testing.assert_allclose(world.mean(axis=0),
                                   [cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y + cell_lc.CHARLES_PLATE[0] / 2])
        self.assertEqual(result["placement"]["origin"][2], prior_z)
        self.assertEqual(self.state()["request"]["params"]["sub"], "7")
        self.assertEqual(self.state()["request"]["params"]["oz"], "2")
        self.assertIs(self.state()["prepared"]["toolpath"], source)
        self.assertEqual(result["placement"]["pivot"], placed["pivot"])

    def test_removed_fixture_leaves_plate_and_gusset_in_both_models(self):
        self.slice(oy="-220")
        server.run_placement({"fixture": "1"}, self.ws)
        st = self.state()
        names = {b[0] for b in st["emit"]["scene"].boxes}
        self.assertIn("manpro_training_block", names)
        data = json.loads(re.search(r"const S = (\{.*?\});\n", st["html"]).group(1))
        self.assertIn("manpro_training_block", data["scene"]["meshes"])
        result = server.run_placement({"fixture": "0"}, self.ws)
        self.assertEqual(result["blocking"], 0)
        names = {b[0] for b in st["emit"]["scene"].boxes}
        self.assertNotIn("manpro_training_block", names)
        self.assertTrue({"charles_platter", "platter_gusset"} <= names)
        data = json.loads(re.search(r"const S = (\{.*?\});\n", st["html"]).group(1))
        self.assertNotIn("manpro_training_block", data["scene"]["meshes"])
        self.assertIn("charles_platter", data["scene"]["meshes"])

    def test_correct_crashes_finds_clear_placement_without_removing_fixture(self):
        self.slice(oy="-220")
        bad = server.run_placement({"fixture": "1"}, self.ws)
        self.assertGreater(bad["blocking"], 0)
        fixed = server.run_placement({"correct": "1"}, self.ws)
        self.assertEqual(fixed["blocking"], 0)
        self.assertTrue(fixed["correction"]["relocated"])
        self.assertTrue(fixed["placement"]["fixture"])

        request = self.state()["request"]
        saved_origin = self.state()["emit"]["origin"].copy()
        replay = server.run_slice(request["params"], request["stl"], self.ws)
        self.assertEqual(replay["blocking"], 0)
        np.testing.assert_allclose(self.state()["emit"]["origin"], saved_origin)

    def test_snap_preserves_xy_and_clears_only_the_gap(self):
        self.slice(ox="10", oy="-10", oz="5")
        before = self.state()["emit"]["origin"].copy()
        server.run_placement({"oz": "0"}, self.ws)
        after = self.state()["emit"]["origin"]
        np.testing.assert_allclose(before[:2], after[:2])
        self.assertAlmostEqual(before[2] - after[2], 5)

    def test_off_plate_requests_fit_the_whole_footprint(self):
        result = self.slice(ox="9999", oy="-9999")
        origin = np.array(result["placement"]["origin"])
        bounds = np.array(result["placement"]["footprint"]) + origin[:2]
        self.assertGreaterEqual(bounds[0, 0], cell_lc.FACEPLATE_X - cell_lc.CHARLES_PLATE[1] / 2)
        self.assertGreaterEqual(bounds[0, 1], cell_lc.FACEPLATE_Y)
        self.assertLessEqual(bounds[1, 0], cell_lc.FACEPLATE_X + cell_lc.CHARLES_PLATE[1] / 2)
        self.assertLessEqual(bounds[1, 1], cell_lc.FACEPLATE_Y + cell_lc.CHARLES_PLATE[0])

    def test_invalid_placement_preserves_current_job(self):
        self.slice()
        old = self.state()["toolpath"]
        for args in ({"ox": "nan"}, {"sub": "-1"}, {"oz": "-1"},
                     {"yaw": "inf"}, {"fixture": "false"}, {"lh": "9"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                server.run_placement(args, self.ws)
            self.assertIs(old, self.state()["toolpath"])

    def test_oversized_part_refuses_instead_of_claiming_it_is_on_platter(self):
        self.mesh = cylinder(radius=400, height=9, around=12)
        with self.assertRaisesRegex(ValueError, "does not fit"):
            self.slice()


if __name__ == "__main__":
    unittest.main()
