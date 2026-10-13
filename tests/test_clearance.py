import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import parse
from waam_pipeline.clearance import (
    Scene,
    TorchModel,
    _point_box_dist,
    _point_segment_dist,
    _sample_segment,
    check,
)


def wall_gcode(y: float, length: float = 60.0, layers: int = 8, layer_h: float = 3.0) -> str:
    lines = ["G21", "G90", "M83"]
    for k in range(layers):
        z = layer_h * (k + 1)
        lines += [
            f";LAYER:{k}",
            f"G0 X0 Y{y} Z{z}",
            f"G1 X{length} Y{y} Z{z} E1",
        ]
    lines.append(f"G0 X{length} Y{y} Z{layer_h * layers + 40}")
    return "\n".join(lines) + "\n"


class TestGeometryHelpers(unittest.TestCase):
    def test_point_segment_dist(self):
        seg_a = np.array([[0.0, 0.0, 0.0]])
        seg_b = np.array([[10.0, 0.0, 0.0]])
        pts = np.array([[5.0, 3.0, 0.0], [-4.0, 0.0, 0.0], [12.0, 0.0, 5.0]])
        d = _point_segment_dist(pts, seg_a, seg_b)[:, 0]
        np.testing.assert_allclose(d, [3.0, 4.0, np.hypot(2.0, 5.0)])

    def test_point_box_dist_outside(self):
        bmin, bmax = np.array([0.0, 0.0, 0.0]), np.array([10.0, 10.0, 10.0])
        pts = np.array([[13.0, 5.0, 5.0], [13.0, 14.0, 5.0]])
        d = _point_box_dist(pts, bmin, bmax)
        np.testing.assert_allclose(d, [3.0, 5.0])

    def test_point_box_dist_is_signed_inside(self):


        bmin, bmax = np.array([0.0, 0.0, 0.0]), np.array([10.0, 10.0, 10.0])
        pts = np.array([
            [5.0, 5.0, 5.0],
            [5.0, 5.0, 9.0],
            [5.0, 5.0, 0.0],
        ])
        d = _point_box_dist(pts, bmin, bmax)
        np.testing.assert_allclose(d, [-5.0, -1.0, 0.0])


class TestClearance(unittest.TestCase):
    def test_single_wall_low_return_travels_are_repaired(self):
        from waam_pipeline.correction import repair_travels
        tp = parse(wall_gcode(y=0.0))
        self.assertTrue(check(tp))
        result = repair_travels(tp, TorchModel(), Scene())
        self.assertEqual(result.violations, [])

    def test_square_example_is_clean(self):
        tp = parse((ROOT / "examples" / "square.gcode").read_text(encoding="utf-8"))
        self.assertEqual(check(tp), [])

    def test_adjacent_wall_collides(self):


        tp = parse(wall_gcode(y=0.0) + wall_gcode(y=15.0))
        violations = check(tp)
        self.assertTrue(violations)
        self.assertTrue(all(v.obstacle.startswith("bead(") for v in violations))

    def test_bead_report_is_the_worst_sphere_not_the_lowest(self):


        tp = parse(wall_gcode(y=0.0) + wall_gcode(y=15.0))
        violations = [v for v in check(tp) if v.obstacle.startswith("bead(")
                      and tp.moves[v.move_index].deposit]
        self.assertTrue(violations)
        reported = min(v.clearance_mm for v in violations)


        torch, bead_r, lift, exclusion, sample_mm = TorchModel(), 4.5, 1.5, 12.0, 4.0
        spheres = torch.spheres()
        truth = np.inf
        bead_a, bead_b = [], []
        prev = None
        for move in tp.moves:
            target = np.asarray(move.target, float)
            if prev is not None and bead_a and move.deposit:
                samples = _sample_segment(prev, target, sample_mm)
                aa, bb = np.vstack(bead_a), np.vstack(bead_b)
                excluded = _point_segment_dist(samples, aa, bb) < exclusion
                for zoff, radius in spheres:
                    d = _point_segment_dist(samples + np.array([0.0, 0.0, zoff]), aa, bb)
                    d = np.where(excluded, np.inf, d - bead_r - radius)
                    truth = min(truth, float(np.min(d)))
            if move.deposit and prev is not None:
                bead_a.append(prev + np.array([0.0, 0.0, lift]))
                bead_b.append(target + np.array([0.0, 0.0, lift]))
            prev = target

        self.assertAlmostEqual(reported, round(truth, 2), places=2)

    def test_tall_wall_is_not_refused_against_its_own_lower_layers(self):


        from waam_pipeline.clearance import blocking
        from waam_pipeline.slicer import wall as make_wall

        torch = TorchModel.yaskawa_accufast_45()
        for layers in (6, 10, 20):
            tp = make_wall(150.0, layers, 3.0)
            hits = blocking(check(tp, torch, None, margin_mm=3.0))
            self.assertEqual(
                [], [v for v in hits if v.obstacle.startswith("bead(")],
                f"{layers}-layer wall refused against its own beads")

    def test_ploughing_into_the_stack_is_still_refused(self):


        from dataclasses import replace as _replace

        from waam_pipeline.clearance import blocking
        from waam_pipeline.slicer import wall as make_wall
        from waam_pipeline.toolpath import Toolpath as _Toolpath

        torch = TorchModel.yaskawa_accufast_45()
        for layers, lh, cut, floor in ((10, 3.0, 6.0, 27.0), (6, 2.0, 4.0, 10.0)):
            tp = make_wall(150.0, layers, lh)
            sunk = _Toolpath(moves=[
                _replace(m, z=(m.z - cut if m.z >= floor else m.z)) for m in tp.moves])


            for model in (
                torch,
                _replace(torch, stackup=_replace(torch.stackup, ctwd_mm=1000.0)),
                TorchModel(samples=((1000.0, 1.0),)),
            ):
                hits = [v for v in blocking(check(sunk, model, None, margin_mm=3.0))
                        if v.obstacle.startswith("bead(")]
                self.assertTrue(
                    hits, f"{layers}x{lh} mm wall ploughed {cut} mm was not caught")
                self.assertLess(min(v.clearance_mm for v in hits), 0.0)


        isolated_tip = TorchModel(samples=((1000.0, 1.0),))
        for cut in (0.004, 2.0, 6.0):
            sunk = parse(
                "G21\nG90\nM83\nG0 X0 Y0 Z10\nG1 X60 E1\n"
                f"G0 Z{10.0 - cut}\n")
            hits = blocking(check(sunk, isolated_tip))
            self.assertTrue(hits, f"wire-tip plough of {cut} mm was not caught")
            self.assertAlmostEqual(min(v.clearance_mm for v in hits),
                                   -min(3.0 + cut, 9.0 / 2))

        lifted = parse("G21\nG90\nM83\nG0 X0 Y0 Z10\nG1 X60 E1\nG0 Z40\n")
        self.assertEqual(blocking(check(lifted, isolated_tip)), [])

    def test_envelope_keeps_the_lateral_offset_out_of_the_radius(self):

        torch = TorchModel.yaskawa_accufast_45()
        env, sph = torch.envelope(), torch.spheres()
        self.assertEqual(len(env), len(sph))

        np.testing.assert_allclose(env[:, 0], sph[:, 0])
        np.testing.assert_allclose(env[:, 1] + env[:, 2], sph[:, 1])

        self.assertAlmostEqual(float(env[0, 1]), 20.0, delta=0.01)
        self.assertAlmostEqual(float(env[0, 2]), 13.63, delta=0.05)

        plain = TorchModel().envelope()
        np.testing.assert_allclose(plain[:, 2], 0.0)

    def test_distant_wall_is_clean(self):
        from waam_pipeline.correction import repair_travels
        tp = parse(wall_gcode(y=0.0) + wall_gcode(y=80.0))
        self.assertEqual(repair_travels(tp, TorchModel(), Scene()).violations, [])

    def test_scene_box_collides(self):

        scene = Scene()
        scene.boxes.append(
            ("clamp", np.array([-10.0, -15.0, 0.0]), np.array([10.0, 15.0, 25.0]))
        )
        tp = parse(wall_gcode(y=0.0))
        violations = check(tp, scene=scene)
        self.assertTrue(any(v.obstacle == "clamp" for v in violations))

    def test_torch_model_spheres(self):
        spheres = TorchModel().spheres()
        self.assertGreater(len(spheres), 10)
        self.assertAlmostEqual(spheres[0][1], 8.0)
        self.assertAlmostEqual(spheres[-1][1], 17.5)


class TestClearanceRoundoff(unittest.TestCase):
    def setUp(self):
        from waam_pipeline import cell_lc
        from waam_pipeline.slicer import wall

        self.tp = wall(20, 1, 2.3)


        self.origin = cell_lc.snap_toolpath_to_platter(self.tp, 1014, -100)
        self.scene = cell_lc.lc_scene(self.origin, training_block=False)
        self.torch = TorchModel.yaskawa_accufast_45()

    def test_platter_contact_roundoff_allows_path_and_point_guard(self):
        from waam_pipeline.clearance import CONTACT_OK, blocking, check_point, point_guard

        platter = next(b for b in self.scene.boxes if b[0] == "charles_platter")
        raw_clearance = self.tp.moves[0].z - platter[2][2]


        self.assertLess(raw_clearance, 0.0)
        self.assertLess(abs(raw_clearance), 1e-12)

        hits = check(self.tp, self.torch, self.scene, contact_ok=CONTACT_OK)
        self.assertEqual([v.clearance_mm for v in hits], [0.0, 0.0])
        self.assertEqual(blocking(hits), [])
        guard = point_guard(self.torch, self.scene)
        for move in self.tp.moves:
            with self.subTest(point=move.target):
                hits = check_point(move.target, self.torch, self.scene, contact_ok=CONTACT_OK)
                self.assertEqual([v.clearance_mm for v in hits], [0.0])
                self.assertEqual(blocking(hits), [])
                guard(move.target, "seated wall")

    def test_four_micron_platter_penetration_still_blocks_path_and_point_guard(self):
        from waam_pipeline import cell_lc
        from waam_pipeline.clearance import (
            CONTACT_OK, ClearanceError, blocking, check_point, point_guard,
        )


        scene = cell_lc.lc_scene(self.origin - [0, 0, 0.004], training_block=False)
        hits = blocking(check(self.tp, self.torch, scene, contact_ok=CONTACT_OK))
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertEqual(hit.obstacle, "charles_platter")
            self.assertAlmostEqual(hit.clearance_mm, -0.004, delta=1e-12)
        guard = point_guard(self.torch, scene)
        for move in self.tp.moves:
            with self.subTest(point=move.target):
                hits = blocking(check_point(move.target, self.torch, scene, contact_ok=CONTACT_OK))
                self.assertEqual(len(hits), 1)
                self.assertAlmostEqual(hits[0].clearance_mm, -0.004, delta=1e-12)
                with self.assertRaisesRegex(ClearanceError, "charles_platter"):
                    guard(move.target, "sunken wall")

    def test_only_sub_nanometre_clearances_snap_to_zero(self):
        from waam_pipeline.clearance import Violation

        for magnitude in (1e-13, 0.5e-6, 1e-6, 2e-6, 0.004):
            for sign in (-1, 1):
                clearance = sign * magnitude
                with self.subTest(clearance=clearance):
                    hit = Violation(0, 1, (0, 0, 0), "charles_platter", clearance)
                    self.assertEqual(hit.clearance_mm,
                                     0.0 if magnitude < 1e-6 else clearance)


class TestYaskawa45Torch(unittest.TestCase):
    def test_swept_envelope_matches_the_controller_tool_model(self):


        from waam_pipeline.clearance import TorchModel

        torch = TorchModel.yaskawa_accufast_45()
        s = torch.spheres()
        self.assertGreater(len(s), 30)
        self.assertTrue((s[:, 0] > 0).all())

        self.assertAlmostEqual(s[0, 0], 15.0, delta=1.0)


        self.assertAlmostEqual(s[0, 1], 33.6, delta=2.0)

        self.assertAlmostEqual(s[-1, 0], 326.8, delta=2.0)
        self.assertGreater(s[-1, 1], 200.0)

    def test_tcp_and_capsules_come_from_one_source(self):

        import numpy as np

        from waam_pipeline.clearance import YASKAWA_45_CAPSULES, YASKAWA_45_TCP

        ends = [np.asarray(p, float) for c in YASKAWA_45_CAPSULES for p in c[:2]]
        nearest = min(float(np.linalg.norm(e - YASKAWA_45_TCP)) for e in ends)
        self.assertAlmostEqual(
            nearest, 20.3, delta=1.5,
            msg=f"nozzle sits {nearest:.1f} mm from the TCP; the controller's "
                "TOOL 0 and TOOL INTERFERE give 20.3. A mismatch here means the "
                "two were taken from different sources.",
        )


class TestCellShadow(unittest.TestCase):
    def test_wall_on_platter_reachable_and_scene(self):
        from waam_pipeline.cell_lc import lc_scene, robot_shadow
        from waam_pipeline.slicer import wall

        tp = wall(200.0, layers=4, layer_height_mm=3.0)
        rob = robot_shadow(tp)
        self.assertEqual(rob["unreachable"], 0)
        self.assertEqual(len(rob["skeleton"]), len(tp.moves))
        first = rob["skeleton"][0]
        self.assertEqual(len(first), 8)

        self.assertAlmostEqual(first[-1][0], tp.moves[0].x, delta=0.6)
        self.assertAlmostEqual(first[-1][2], tp.moves[0].z, delta=0.6)
        names = {n for n, *_ in lc_scene().boxes}
        self.assertIn("charles_platter", names)
        self.assertIn("faceplate_168941-1", names)

    def test_shadow_and_job_solver_use_identical_joint_chain(self):
        from waam_pipeline.cell_lc import pulse_solver, robot_shadow
        from waam_pipeline.slicer import wall

        tp = wall(200.0, layers=4, layer_height_mm=3.0)
        shown = robot_shadow(tp)
        solve = pulse_solver()
        emitted = [solve((m.x, m.y, m.z)) for m in tp.moves]
        self.assertEqual(shown["unreachable"], 0)
        np.testing.assert_allclose(shown["joints"], emitted, atol=0.006)


class TestCellMeshes(unittest.TestCase):
    def test_torch_mesh_geometry(self):
        import numpy as np

        from waam_pipeline.cell_lc import torch_mesh

        t = torch_mesh()
        self.assertGreater(len(t), 100)
        pts = t.reshape(-1, 3)
        self.assertAlmostEqual(pts[:, 1].max(), 50.0, delta=2)
        self.assertAlmostEqual(pts[:, 0].max(), 374.0, delta=13)
        self.assertLess(pts[:, 2].min(), -100.0)

    def test_scene_meshes_present(self):
        from waam_pipeline.cell_lc import lc_scene_meshes

        sm = lc_scene_meshes()
        self.assertIsNotNone(sm)
        for k in ("mh185_headstock", "charles_platter", "robot_pedestal"):
            self.assertIn(k, sm)
            self.assertEqual(len(sm[k]) % 9, 0)
        cleaners = set(sm) & {"lc_cleaner", "reamer_station"}
        self.assertEqual(len(cleaners), 1)
        self.assertEqual(len(sm[cleaners.pop()]) % 9, 0)


class TestStationAngle(unittest.TestCase):


    def setUp(self):
        from waam_pipeline import station

        self.station = station

        self.torch = TorchModel(samples=((100.0, 10.0),))


        self.on_axis = station.axis_origin()

    def _at(self, theta, part_origin=None, **kw):
        return self.station.part_frame_transform(
            theta, self.on_axis if part_origin is None else part_origin, **kw)

    def test_station_zero_is_the_same_answer_as_no_station_at_all(self):
        from waam_pipeline.cell_lc import lc_scene, snap_toolpath_to_platter
        from waam_pipeline.slicer import wall

        tp = wall(60.0, layers=4, layer_height_mm=3.0)
        snap_toolpath_to_platter(tp, 0.0, -180.0, substrate_mm=6.35)


        torch, scene = TorchModel.yaskawa_accufast_45(), lc_scene(training_block=True)
        plain = check(tp, torch, scene, contact_ok=("charles_platter",))
        zeroed = check(tp, torch, scene, contact_ok=("charles_platter",),
                       station_transform=self.station.part_frame_transform(0.0))
        self.assertEqual(plain, zeroed)
        self.assertTrue(plain)

    def test_a_part_fixed_obstacle_does_not_move_relative_to_the_part(self):


        from waam_pipeline.clearance import check_point

        tip_only = TorchModel(samples=((0.0, 0.0),))
        scene = Scene(boxes=[("plate", np.array([-100.0, -100.0, -20.0]),
                              np.array([100.0, 100.0, 0.0]))],
                      part_fixed=("plate",))
        for theta in (0.0, 30.0, 90.0, -145.0):
            hits = check_point((0.0, 0.0, 5.0), tip_only, scene, margin_mm=1e6,
                               station_transform=self._at(theta))
            self.assertAlmostEqual(hits[0].clearance_mm, 5.0, places=6)

    def test_a_robot_fixed_obstacle_is_left_behind_as_the_part_swings(self):
        from waam_pipeline.clearance import check_point

        tip_only = TorchModel(samples=((0.0, 0.0),))
        scene = Scene(boxes=[("floor", np.array([-1e4, -1e4, -1e4]),
                              np.array([1e4, 1e4, -100.0]))],
                      part_fixed=())
        for theta in (0.0, 30.0, 90.0):
            hits = check_point((100.0, 0.0, 0.0), tip_only, scene, margin_mm=1e6,
                               station_transform=self._at(theta))

            self.assertAlmostEqual(hits[0].clearance_mm,
                                   round(100.0 - 100.0 * np.sin(np.radians(theta)), 2),
                                   places=2)

    def test_the_torch_leans_over_part_fixed_geometry(self):


        from waam_pipeline.clearance import check_point

        scene = Scene(boxes=[("blk", np.array([60.0, -50.0, 0.0]),
                              np.array([200.0, 50.0, 300.0]))],
                      part_fixed=("blk",))
        for theta in (0.0, 30.0, 45.0, -30.0):
            hits = check_point((0.0, 0.0, 0.0), self.torch, scene, margin_mm=1e6,
                               station_transform=self._at(theta))
            want = 60.0 + 100.0 * np.sin(np.radians(theta)) - 10.0
            self.assertAlmostEqual(hits[0].clearance_mm, round(want, 2), places=2)

    def test_flipping_the_sense_mirrors_the_lean(self):

        from waam_pipeline.clearance import check_point

        scene = Scene(boxes=[("blk", np.array([60.0, -50.0, 0.0]),
                              np.array([200.0, 50.0, 300.0]))],
                      part_fixed=("blk",))
        got = []
        for sense in (+1, -1):
            hits = check_point((0.0, 0.0, 0.0), self.torch, scene, margin_mm=1e6,
                               station_transform=self._at(35.0, sense=sense))
            got.append(hits[0].clearance_mm)
        self.assertAlmostEqual(got[0] - 50.0, -(got[1] - 50.0), places=2)

    def test_rotation_is_about_the_axis_line_not_the_part_origin(self):


        from waam_pipeline.cell_lc import DEFAULT_PART_ORIGIN
        from waam_pipeline.clearance import check_point

        tip_only = TorchModel(samples=((0.0, 0.0),))
        scene = Scene(boxes=[("floor", np.array([-1e4, -1e4, -1e4]),
                              np.array([1e4, 1e4, -100.0]))],
                      part_fixed=())
        about_axis = check_point((100.0, 0.0, 0.0), tip_only, scene, margin_mm=1e6,
                                 station_transform=self._at(180.0, DEFAULT_PART_ORIGIN))
        about_origin = check_point((100.0, 0.0, 0.0), tip_only, scene, margin_mm=1e6,
                                   station_transform=self._at(180.0))
        self.assertGreater(abs(about_axis[0].clearance_mm
                               - about_origin[0].clearance_mm), 40.0)

    def test_a_scene_that_never_said_which_frame_it_is_in_is_refused(self):
        from waam_pipeline.clearance import check_point

        scene = Scene(boxes=[("mystery", np.zeros(3), np.ones(3))])
        with self.assertRaises(ValueError) as ctx:
            check_point((0.0, 0.0, 0.0), TorchModel(), scene,
                        station_transform=np.eye(4))
        self.assertIn("mystery", str(ctx.exception))

        self.assertTrue(check_point((0.0, 0.0, 0.0), TorchModel(), scene))

    def test_the_lc_scene_classifies_every_obstacle_it_builds(self):
        from waam_pipeline.cell_lc import lc_scene
        from waam_pipeline.clearance import LC_PART_FIXED, LC_ROBOT_FIXED

        names = {n for n, *_ in lc_scene().boxes}
        self.assertEqual(names - set(LC_PART_FIXED) - set(LC_ROBOT_FIXED), set())
        self.assertEqual(set(LC_PART_FIXED) & set(LC_ROBOT_FIXED), set())

        self.assertIn("charles_platter", LC_PART_FIXED)
        self.assertIn("manpro_training_block", LC_PART_FIXED)
        self.assertIn("mh185_headstock", LC_ROBOT_FIXED)

    def test_a_declared_scene_is_taken_at_its_word(self):
        from waam_pipeline.clearance import check_point

        tip_only = TorchModel(samples=((0.0, 0.0),))
        box = (np.array([20.0, -10.0, -10.0]), np.array([40.0, 10.0, 10.0]))
        rides = Scene(boxes=[("thing", *box)], part_fixed=("thing",))
        stays = Scene(boxes=[("thing", *box)], part_fixed=())
        T = self._at(90.0)
        a = check_point((60.0, 0.0, 0.0), tip_only, rides, margin_mm=1e6, station_transform=T)
        b = check_point((60.0, 0.0, 0.0), tip_only, stays, margin_mm=1e6, station_transform=T)
        self.assertAlmostEqual(a[0].clearance_mm, 20.0, places=2)
        self.assertAlmostEqual(b[0].clearance_mm, round(float(np.hypot(20.0, 50.0)), 2),
                               places=2)

    def test_a_bad_transform_is_refused(self):
        from waam_pipeline.clearance import check_point

        with self.assertRaises(ValueError):
            check_point((0.0, 0.0, 0.0), TorchModel(), Scene(),
                        station_transform=np.eye(3))

    def test_turning_the_station_tilts_the_part_under_a_vertical_torch(self):


        from waam_pipeline import station

        tp = parse(wall_gcode(0.0, layers=8))
        torch = TorchModel.yaskawa_accufast_45()
        flat = check(tp, torch, Scene())
        self.assertTrue(flat)
        for theta in (25.0, -25.0):
            T = station.part_frame_transform(theta, self.on_axis)
            tilted = check(tp, torch, Scene(), station_transform=T)
            self.assertGreater(len(tilted), len(flat))
            self.assertTrue(all(v.obstacle.startswith("bead(") for v in tilted))

        full = check(tp, torch, Scene(),
                     station_transform=station.part_frame_transform(360.0, self.on_axis))
        self.assertEqual([(v.move_index, v.obstacle) for v in full],
                         [(v.move_index, v.obstacle) for v in flat])


class TestVectorisedPassIsExact(unittest.TestCase):


    def test_box_distances_match_the_scalar_path_bit_for_bit(self):
        from waam_pipeline.clearance import _box_dist_many, _point_box_dist
        rng = np.random.default_rng(7)
        lo = rng.uniform(-200, 100, size=(9, 3))
        boxes = (lo, lo + rng.uniform(0.5, 150, size=(9, 3)))
        centers = rng.uniform(-260, 260, size=(5, 9, 37, 3))
        centers[0, :, 0] = (boxes[0] + boxes[1]) / 2
        centers[1, :, 0] = boxes[0]
        many = _box_dist_many(centers, *boxes)
        for k in range(5):
            for j in range(9):
                one = _point_box_dist(centers[k, j], boxes[0][j], boxes[1][j])
                self.assertEqual(many[k, j].tobytes(), one.tobytes(), (k, j))

    def test_rows_equal_the_stacked_lists(self):
        from waam_pipeline.clearance import _Rows
        rows, flat, ref_rows, ref_flat = _Rows(3), _Rows(None), [], []
        rng = np.random.default_rng(3)
        for n in (1, 70, 3, 200):
            block = rng.normal(size=(n, 3))
            rows.extend(block)
            ref_rows.append(block)
            flat.extend([float(n)])
            ref_flat.append(float(n))
            self.assertEqual(rows.view.tobytes(), np.vstack(ref_rows).tobytes())
            self.assertEqual(flat.view.tobytes(), np.asarray(ref_flat).tobytes())

    def test_block_size_never_changes_a_result(self):
        from unittest import mock
        from waam_pipeline import clearance
        torch = TorchModel()
        tp = parse(wall_gcode(0.0, length=40.0, layers=3))
        scene = Scene(boxes=[("post", np.array([10.0, -30, 0]), np.array([14.0, 30, 30])),
                             ("post", np.array([25.0, -30, 0]), np.array([27.0, 30, 30])),
                             ("plate", np.array([-50.0, -50, -10]), np.array([90.0, 50, 0]))])
        whole = check(tp, torch, scene, contact_ok=("plate",))
        self.assertTrue(whole)
        with mock.patch.object(clearance, "_BOX_BLOCK", 1):
            one_sphere_at_a_time = check(tp, torch, scene, contact_ok=("plate",))
        self.assertEqual(whole, one_sphere_at_a_time)


if __name__ == "__main__":
    unittest.main()
