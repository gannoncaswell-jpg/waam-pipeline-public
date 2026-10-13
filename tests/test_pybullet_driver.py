'Tier-2 driver: everything that can be checked without a physics engine.'


import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from waam_pipeline import cell_hc, cell_lc, clearance, station
from waam_pipeline.clearance import Scene, TorchModel, Violation, blocking
from waam_pipeline.robots import ar1440, hc10
from waam_pipeline.slicer import wall
from waam_pipeline.toolpath import Move, Toolpath

from sim import pybullet_driver as pbd

URDF = ROOT / "robots" / "ar1440.urdf"


def _scene(*, boxes=(), capsules=(), part_fixed=None) -> Scene:
    sc = Scene(part_fixed=part_fixed)
    for name, mn, mx in boxes:
        sc.boxes.append((name, np.asarray(mn, float), np.asarray(mx, float)))
    for name, p0, p1, r in capsules:
        sc.capsules.append((name, np.asarray(p0, float), np.asarray(p1, float), float(r)))
    return sc


class TestViolationShape(unittest.TestCase):


    def test_reuses_the_clearance_dataclass_not_a_copy(self):
        self.assertIs(pbd.Violation, clearance.Violation)

    def test_pybullet_metres_become_signed_millimetres(self):


        v = pbd._violation(3, 42, (1.0, 2.0, 3.0), "charles_platter", -0.00342 * 1000)
        self.assertIsInstance(v, Violation)
        self.assertAlmostEqual(v.clearance_mm, -3.42)
        self.assertEqual(v.move_index, 3)
        self.assertEqual(v.gcode_line, 42)
        self.assertEqual(v.obstacle, "charles_platter")

    def test_penetration_is_negative_and_blocks(self):
        deep = pbd._violation(0, 1, (0, 0, 0), "manpro_training_block", -20.03)
        self.assertLess(deep.clearance_mm, 0.0)
        self.assertEqual(blocking([deep]), [deep])

    def test_contact_ok_forgiveness_still_works_on_tier2_output(self):


        near = pbd._violation(0, 1, (0, 0, 0), "charles_platter", 0.4)
        into = pbd._violation(1, 2, (0, 0, 0), "charles_platter", -0.4)
        self.assertEqual(blocking([near, into]), [into])

    def test_bead_naming_matches_tier_one_character_for_character(self):
        bead = pbd.BeadSpec(0, 57, (0, 0, 0), (1, 0, 0), 4.5, 0.0, (0.0, 0.0))
        self.assertEqual(bead.obstacle, "bead(line 57)")
        proximity = pbd._violation(0, 57, (0, 0, 0), bead.obstacle, 1.0)
        self.assertEqual(blocking([proximity]), [])

    def test_point_is_rounded_like_tier_one(self):
        v = pbd._violation(0, 1, (1.23456, -2.0, 3.0), "floor", 1.0)
        self.assertEqual(v.point, (1.23, -2.0, 3.0))

    def test_link_rides_beside_the_violation_never_inside_it(self):
        hit = pbd.LinkHit(pbd._violation(0, 1, (0, 0, 0), "reamer_yair_185091", -33.8),
                          link="link_4_r")
        self.assertEqual(hit.violation.obstacle, "reamer_yair_185091")
        self.assertNotIn("link_4_r", hit.violation.obstacle)
        self.assertFalse(hit.is_torch)
        self.assertTrue(pbd.LinkHit(hit.violation, link="torch/neck").is_torch)
        self.assertIn("link_4_r", hit.describe())

    def test_sim_result_blocking_is_the_clearance_function(self):
        res = pbd.SimResult(violations=[
            pbd._violation(0, 1, (0, 0, 0), "charles_platter", 0.5),
            pbd._violation(1, 2, (0, 0, 0), "mh185_headstock", -2.0),
        ])
        self.assertEqual([v.obstacle for v in res.blocking()], ["mh185_headstock"])
        self.assertEqual(res.worst().obstacle, "mh185_headstock")


class TestQuaternions(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(pbd.quat_from_matrix(np.eye(3)), (0.0, 0.0, 0.0, 1.0))

    def test_round_trip_over_many_rotations(self):
        rng = np.random.default_rng(7)
        for _ in range(50):
            axis = rng.normal(size=3)
            R = ar1440._rot(tuple(axis), float(rng.uniform(-180, 180)))
            back = pbd.matrix_from_quat(pbd.quat_from_matrix(R))
            np.testing.assert_allclose(back, R, atol=1e-9)

    def test_the_180_degree_branch(self):


        for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            R = ar1440._rot(axis, 180.0)
            np.testing.assert_allclose(
                pbd.matrix_from_quat(pbd.quat_from_matrix(R)), R, atol=1e-9)

    def test_rejects_non_matrix(self):
        with self.assertRaises(ValueError):
            pbd.quat_from_matrix(np.eye(4))

    def test_axis_to_z_maps_local_z_onto_the_direction(self):
        for d in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, -1), (1, 2, -3)):
            R = pbd.matrix_from_quat(pbd._axis_to_z(d))
            want = np.asarray(d, float) / np.linalg.norm(d)
            np.testing.assert_allclose(R @ np.array([0.0, 0.0, 1.0]), want, atol=1e-9)


class TestCapsuleFrame(unittest.TestCase):
    def test_centre_length_and_axis(self):
        centre, quat, length = pbd.capsule_frame((0.0, 0.0, 0.0), (100.0, 0.0, 0.0))
        np.testing.assert_allclose(centre, (0.05, 0.0, 0.0), atol=1e-12)
        self.assertAlmostEqual(length, 0.1)
        R = pbd.matrix_from_quat(quat)
        np.testing.assert_allclose(R @ np.array([0, 0, 1.0]), (1, 0, 0), atol=1e-9)

    def test_length_excludes_the_caps_like_clearance_does(self):


        _c, _q, length = pbd.capsule_frame((0, 0, 0), (0, 0, 37.5))
        self.assertAlmostEqual(length, 0.0375)

    def test_degenerate_segment_has_zero_length(self):
        _c, _q, length = pbd.capsule_frame((5, 5, 5), (5, 5, 5))
        self.assertEqual(length, 0.0)


class TestSceneTranslation(unittest.TestCase):
    def setUp(self):
        self.origin = cell_lc.DEFAULT_PART_ORIGIN
        self.scene = cell_lc.lc_scene(part_origin_robot=self.origin)

    def _by_name(self, specs):
        return {s.name: s for s in specs}

    def test_station_zero_is_a_pure_translation_into_metres(self):
        specs = self._by_name(pbd.scene_body_specs(self.scene, self.origin))
        for name, bmin, bmax in self.scene.boxes:
            s = specs[name]
            want = ((np.asarray(bmin) + np.asarray(bmax)) / 2.0 + self.origin) * 1e-3
            np.testing.assert_allclose(s.position, want, atol=1e-12)
            np.testing.assert_allclose(
                s.half_extents, (np.asarray(bmax) - np.asarray(bmin)) / 2.0 * 1e-3, atol=1e-12)
            self.assertEqual(s.orientation, (0.0, 0.0, 0.0, 1.0))

    def test_station_zero_never_consults_the_frame_declaration(self):


        sc = _scene(boxes=[("some_new_bracket", (0, 0, 0), (10, 10, 10))])
        specs = pbd.scene_body_specs(sc, self.origin)
        self.assertEqual(len(specs), 1)
        self.assertFalse(specs[0].part_fixed)

    def test_unclassified_obstacle_raises_once_the_station_turns(self):
        sc = _scene(boxes=[("some_new_bracket", (0, 0, 0), (10, 10, 10))])
        with self.assertRaises(ValueError) as ctx:
            pbd.scene_body_specs(sc, self.origin, station_deg=30.0)
        self.assertIn("some_new_bracket", str(ctx.exception))

    def test_explicit_part_fixed_is_honoured(self):
        sc = _scene(boxes=[("rides_the_platter", (0, 0, 0), (10, 10, 10)),
                           ("bolted_to_the_floor", (0, 0, 0), (10, 10, 10))],
                    part_fixed=("rides_the_platter",))
        specs = self._by_name(pbd.scene_body_specs(sc, self.origin, station_deg=45.0))
        self.assertTrue(specs["rides_the_platter"].part_fixed)
        self.assertFalse(specs["bolted_to_the_floor"].part_fixed)

    def test_part_fixed_obstacles_ride_the_station_transform(self):
        theta = 37.0
        specs = self._by_name(pbd.scene_body_specs(self.scene, self.origin,
                                                   station_deg=theta))
        T = station.station_transform(theta)
        for name, bmin, bmax in self.scene.boxes:
            centre_robot = (np.asarray(bmin) + np.asarray(bmax)) / 2.0 + self.origin
            want = (T[:3, :3] @ centre_robot + T[:3, 3]) if name in clearance.LC_PART_FIXED \
                else centre_robot
            np.testing.assert_allclose(specs[name].position, want * 1e-3, atol=1e-9)

    def test_robot_fixed_obstacles_do_not_move_at_all(self):
        zero = self._by_name(pbd.scene_body_specs(self.scene, self.origin))
        turned = self._by_name(pbd.scene_body_specs(self.scene, self.origin,
                                                    station_deg=90.0))
        for name in clearance.LC_ROBOT_FIXED:
            if name not in zero:
                continue
            np.testing.assert_allclose(zero[name].position, turned[name].position, atol=1e-12)
            self.assertEqual(turned[name].orientation, (0.0, 0.0, 0.0, 1.0))

    def test_a_part_fixed_obstacle_centred_on_the_axis_turns_without_moving(self):


        turned = self._by_name(pbd.scene_body_specs(self.scene, self.origin,
                                                    station_deg=90.0))
        face = turned["faceplate_168941-1"]
        self.assertTrue(face.part_fixed)
        self.assertNotEqual(face.orientation, (0.0, 0.0, 0.0, 1.0))

    def test_the_two_station_transforms_agree_and_the_difference_is_a_metre(self):


        theta = 62.0
        p_part = np.array([37.0, -12.0, 4.0])
        a = station.station_transform(theta)
        world_a = a[:3, :3] @ (p_part + self.origin) + a[:3, 3]
        b = station.part_frame_transform(theta, self.origin)
        world_b = b[:3, :3] @ p_part + b[:3, 3] + self.origin
        np.testing.assert_allclose(world_a, world_b, atol=1e-9)
        naive = ar1440._rot((0, 1, 0), theta) @ (p_part + self.origin)
        self.assertGreater(float(np.linalg.norm(world_a - naive)), 900.0)

    def test_capsule_obstacles_translate_and_orient(self):
        sc = _scene(capsules=[("post", (0.0, 0.0, 0.0), (0.0, 0.0, 200.0), 25.0)],
                    part_fixed=())
        spec = pbd.scene_body_specs(sc, self.origin)[0]
        self.assertEqual(spec.kind, "capsule")
        self.assertAlmostEqual(spec.radius, 0.025)
        self.assertAlmostEqual(spec.length, 0.2)
        np.testing.assert_allclose(
            spec.position, (self.origin + np.array([0, 0, 100.0])) * 1e-3, atol=1e-12)

    def test_no_obstacle_is_silently_dropped(self):
        specs = pbd.scene_body_specs(self.scene, self.origin)
        self.assertEqual({s.name for s in specs},
                         {n for n, *_ in self.scene.boxes} | {n for n, *_ in self.scene.capsules})


class TestBeadSpecs(unittest.TestCase):
    def test_wall_beads_grow_in_order_and_skip_travels(self):
        tp = wall(150.0, 3, 2.0)
        beads = pbd.bead_specs(tp)
        self.assertEqual(len(beads), sum(1 for i, m in enumerate(tp.moves)
                                         if m.deposit and i > 0))
        self.assertEqual([b.move_index for b in beads], sorted(b.move_index for b in beads))
        for b in beads:
            self.assertEqual(b.obstacle, f"bead(line {b.line_no})")

    def test_axis_rises_half_a_bead_height_because_the_tcp_runs_on_the_surface(self):


        tp = Toolpath(moves=[Move(0, 0, 10.0, None, False, 0, 1),
                             Move(100.0, 0, 10.0, 200.0, True, 0, 2)])
        b = pbd.bead_specs(tp, bead_height_mm=3.0, bead_width_mm=9.0)[0]
        self.assertAlmostEqual(b.p0[2], 11.5)
        self.assertAlmostEqual(b.p1[2], 11.5)
        self.assertAlmostEqual(b.top_z_mm, 13.0)
        self.assertEqual(b.lay_z_mm, (10.0, 10.0))
        self.assertAlmostEqual(b.radius_mm, 4.5)

    def test_a_ramping_bead_takes_the_higher_end_as_its_top(self):

        tp = Toolpath(moves=[Move(0, 0, 4.0, None, False, 0, 1),
                             Move(100.0, 0, 9.0, 200.0, True, 0, 2)])
        bead = pbd.bead_specs(tp, bead_height_mm=3.0)[0]
        self.assertAlmostEqual(bead.top_z_mm, 12.0)
        self.assertEqual(bead.lay_z_mm, (4.0, 9.0))

    def test_the_first_move_never_lays_a_bead(self):
        tp = Toolpath(moves=[Move(0, 0, 2.0, 200.0, True, 0, 1)])
        self.assertEqual(pbd.bead_specs(tp), [])

    def test_the_part_is_the_prefix_not_the_finished_object(self):


        tp = wall(150.0, 6, 2.0)
        beads = pbd.bead_specs(tp)
        at_move_3 = [b for b in beads if b.move_index < 3]
        self.assertLess(len(at_move_3), len(beads))
        self.assertTrue(all(b.p0[2] <= max(x.p0[2] for x in beads) for b in at_move_3))


class TestWeldZoneExclusion(unittest.TestCase):


    def setUp(self):

        self.bead = pbd.BeadSpec(0, 1, (0.0, 0.0, 1.0), (100.0, 0.0, 1.0), 4.5, 2.0, (0.0, 0.0))

    def test_near_and_at_or_below_the_tcp_is_forgiven(self):

        mask = pbd.excluded_beads((50.0, 0.0, 2.0), [self.bead], 12.0, 2.0)
        self.assertTrue(bool(mask[0]))

    def test_near_but_the_torch_is_below_the_bead_top_is_not_forgiven(self):


        for sample_z in (1.0, -2.0):
            mask = pbd.excluded_beads((50.0, 0.0, sample_z), [self.bead], 12.0, 2.0)
            self.assertFalse(bool(mask[0]), sample_z)

    def test_the_pass_being_laid_is_forgiven(self):


        for sample_z in (0.0, 1.0, 10.0):
            mask = pbd.excluded_beads((100.0, 0.0, sample_z), [self.bead], 12.0, 0.0)
            self.assertTrue(bool(mask[0]), sample_z)

    def test_the_pass_being_laid_is_not_forgiven_below_its_laying_height(self):
        mask = pbd.excluded_beads((100.0, 0.0, -0.5), [self.bead], 12.0, 0.0)
        self.assertFalse(bool(mask[0]))

    def test_far_from_the_axis_is_not_forgiven(self):
        mask = pbd.excluded_beads((50.0, 40.0, 2.0), [self.bead], 12.0, 2.0)
        self.assertFalse(bool(mask[0]))

    def test_measured_against_the_whole_axis_not_the_nearest_point_only(self):


        mask = pbd.excluded_beads((0.0, 0.0, 2.0), [self.bead], 12.0, 2.0)
        self.assertTrue(bool(mask[0]))

    def test_empty_bead_list(self):
        self.assertEqual(len(pbd.excluded_beads((0, 0, 0), [], 12.0, 0.0)), 0)

    def test_sampling_is_tier_ones_own_function(self):


        self.assertIs(pbd._sample_segment, clearance._sample_segment)
        self.assertIs(pbd._sample_params, clearance._sample_params)
        self.assertIs(pbd._point_segment_dist, clearance._point_segment_dist)
        self.assertIs(pbd._part_fixed_names, clearance._part_fixed_names)
        self.assertIs(pbd.bead_slab, clearance.bead_slab)
        self.assertIs(pbd.weld_zone, clearance.weld_zone)


class TestLinkBounds(unittest.TestCase):


    COLLISION = ROOT / "robots" / "meshes" / "ar1440" / "collision"

    def test_one_radius_per_meshed_link(self):
        bounds = pbd.link_bound_radii(self.COLLISION)
        self.assertEqual(set(bounds), set(ar1440._MESH_LINKS))
        for link, r in bounds.items():
            self.assertTrue(np.isfinite(r), link)
            self.assertGreater(r, 0.0, link)

    def test_each_sphere_actually_contains_its_mesh(self):
        from waam_pipeline.slicer import load_stl

        bounds = pbd.link_bound_radii(self.COLLISION)
        for link, (stl, off) in ar1440._MESH_LINKS.items():
            v = (load_stl(self.COLLISION / stl).reshape(-1, 3) * 1000.0
                 + np.asarray(off, float) * 1000.0)
            self.assertGreaterEqual(bounds[link] + 1e-6,
                                    float(np.linalg.norm(v, axis=1).max()), link)

    def test_the_forearm_sphere_spans_the_whole_upper_arm(self):


        self.assertGreater(pbd.link_bound_radii(self.COLLISION)["link_4_r"], 700.0)

    def test_a_missing_mesh_disables_the_prune_rather_than_shrinking_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bounds = pbd.link_bound_radii(tmp)
        self.assertTrue(all(np.isinf(r) for r in bounds.values()))


class TestTorchSolid(unittest.TestCase):
    def test_three_named_capsules_from_the_controllers_own_model(self):
        caps = pbd.torch_capsules_flange()
        self.assertEqual(len(caps), len(cell_lc.TOOL_INTERFERE_CAPSULES))
        self.assertEqual(len(TORCH := pbd.TORCH_SHAPE_NAMES), 3)
        self.assertEqual(TORCH[-1], "torch/gooseneck")
        self.assertEqual([r for _a, _b, r in caps], [50.0, 20.0, 20.0])

    def test_the_pendant_cross_check_survives_the_frame_change(self):


        caps = pbd.torch_capsules_flange()
        d = float(np.linalg.norm(np.asarray(caps[-1][1]) - cell_lc.TOOL_TCP))
        self.assertAlmostEqual(d, 20.27, places=1)

    def test_the_mapping_is_an_isometry(self):
        a = np.array([12.0, -3.0, 40.0])
        b = np.array([-5.0, 9.0, 100.0])
        self.assertAlmostEqual(
            float(np.linalg.norm(pbd._yaskawa_to_flange(a) - pbd._yaskawa_to_flange(b))),
            float(np.linalg.norm(a - b)))

    def test_torch_capsule_lengths_are_preserved(self):
        for (ya, yb, _r), (fa, fb, _r2) in zip(cell_lc.TOOL_INTERFERE_CAPSULES,
                                               pbd.torch_capsules_flange()):
            self.assertAlmostEqual(
                float(np.linalg.norm(np.asarray(yb) - np.asarray(ya))),
                float(np.linalg.norm(fb - fa)))


class TestUrdfFitness(unittest.TestCase):


    def setUp(self):
        self.report = pbd.inspect_urdf(URDF)

    def test_no_problems(self):
        self.assertEqual(self.report.problems(), [])

    def test_rooted_at_base_link_with_both_branches(self):
        self.assertEqual(self.report.root_link, "base_link")
        for name in pbd.ARM_LINK_NAMES:
            self.assertIn(name, self.report.links)
        for name in station.STATION_LINK_NAMES:
            self.assertIn(name, self.report.links)

    def test_the_arm_joints_the_driver_poses_all_exist_and_are_revolute(self):
        types = self.report.joint_types
        for name in ar1440.ARM_JOINT_NAMES:
            self.assertEqual(types[name], "revolute", name)

    def test_the_station_joint_carries_the_controllers_soft_limit(self):


        self.assertEqual(self.report.joint_types[station.STATION_JOINT_NAME],
                         "revolute")

    def test_fourteen_mesh_references_and_every_one_resolves_on_disk(self):
        self.assertEqual(len(self.report.mesh_files), 14)
        self.assertEqual(self.report.missing_meshes, ())
        for f in self.report.mesh_files:
            self.assertTrue((URDF.parent / f).is_file(), f)

    def test_both_a_visual_and_a_collision_mesh_for_every_meshed_link(self):
        visual = [f for f in self.report.mesh_files if "/visual/" in f]
        collision = [f for f in self.report.mesh_files if "/collision/" in f]
        self.assertEqual(len(visual), 7)
        self.assertEqual(len(collision), 7)
        self.assertEqual({Path(f).name for f in visual},
                         {Path(f).name for f in collision})

    def test_no_inertials_which_is_why_the_driver_uses_usefixedbase(self):
        self.assertEqual(self.report.inertials, 0)

    def test_inspecting_the_urdf_does_not_rewrite_it(self):
        before = hashlib.sha256(URDF.read_bytes()).hexdigest()
        pbd.inspect_urdf(URDF)
        pbd.inspect_urdf(URDF).problems()
        self.assertEqual(hashlib.sha256(URDF.read_bytes()).hexdigest(), before)

    def test_a_primitive_geometry_urdf_is_reported_as_unfit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "primitive.urdf"
            ar1440.write_urdf(str(path), mesh_dir="")
            problems = pbd.inspect_urdf(path).problems()
        self.assertTrue(any("no <mesh>" in p for p in problems), problems)

    def test_a_urdf_with_a_dangling_mesh_path_is_reported_as_unfit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nowhere.urdf"
            ar1440.write_urdf(str(path), mesh_dir="meshes/ar1440")
            report = pbd.inspect_urdf(path)
        self.assertTrue(report.missing_meshes)
        self.assertTrue(any("do not resolve" in p for p in report.problems()))


class TestConditionalStationCheck(unittest.TestCase):


    def _write(self, tmp, *, with_station: bool) -> Path:


        (Path(tmp) / "dummy.stl").write_bytes(b"")
        station_block = ""
        if with_station:
            station_block = (
                '<link name="station_base"/><link name="station_faceplate"/>'
                '<link name="station_platter"/>'
                '<joint name="joint_s1_station" type="continuous">'
                '<parent link="station_base"/><child link="station_faceplate"/>'
                '<axis xyz="0 1 0"/></joint>'
            )
        text = (
            '<?xml version="1.0"?><robot name="t">'
            '<link name="base_link"/>'
            '<link name="link_1_s"><collision><geometry>'
            '<mesh filename="dummy.stl"/></geometry></collision></link>'
            '<joint name="joint_1_s" type="revolute">'
            '<parent link="base_link"/><child link="link_1_s"/>'
            '<axis xyz="0 0 1"/><limit lower="-1" upper="1"/></joint>'
            + station_block + "</robot>"
        )
        path = Path(tmp) / "t.urdf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_stationless_urdf_passes_when_requires_station_is_false(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = pbd.inspect_urdf(self._write(tmp, with_station=False))
        problems = report.problems(
            arm_link_names=("base_link", "link_1_s"),
            arm_joint_names=("joint_1_s",), requires_station=False)
        self.assertEqual(problems, [])

    def test_the_same_stationless_urdf_still_fails_when_requires_station_is_true(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = pbd.inspect_urdf(self._write(tmp, with_station=False))
        problems = report.problems(
            arm_link_names=("base_link", "link_1_s"),
            arm_joint_names=("joint_1_s",), requires_station=True)
        self.assertTrue(any("missing station link" in p for p in problems), problems)
        self.assertTrue(any("missing station joint" in p for p in problems), problems)

    def test_a_stationed_urdf_passes_requires_station_true_too(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = pbd.inspect_urdf(self._write(tmp, with_station=True))
        problems = report.problems(
            arm_link_names=("base_link", "link_1_s"),
            arm_joint_names=("joint_1_s",), requires_station=True)
        self.assertEqual(problems, [])

    def test_the_real_committed_hc10dtp_urdf_needs_no_station(self):


        report = pbd.inspect_urdf(pbd.HC10_BINDING.urdf_path)
        problems = report.problems(
            arm_link_names=pbd.HC10_BINDING.arm_link_names,
            arm_joint_names=pbd.HC10_BINDING.arm_joint_names,
            requires_station=pbd.HC10_BINDING.requires_station)
        self.assertEqual(problems, [])

    def test_the_real_committed_hc10dtp_urdf_would_fail_a_station_requirement(self):


        report = pbd.inspect_urdf(pbd.HC10_BINDING.urdf_path)
        problems = report.problems(
            arm_link_names=pbd.HC10_BINDING.arm_link_names,
            arm_joint_names=pbd.HC10_BINDING.arm_joint_names,
            requires_station=True)
        self.assertTrue(any("missing station" in p for p in problems), problems)


class TestJointMapping(unittest.TestCase):


    ROWS = [
        (0, b"joint_1_s", 0, 7, 6, 1, 0.0, 0.0, -2.9, 2.9, 1000.0, 4.5, b"link_1_s"),
        (1, b"joint_2_l", 0, 8, 7, 1, 0.0, 0.0, -1.5, 2.7, 1000.0, 4.0, b"link_2_l"),
        (2, b"flange_fixed", 4, -1, -1, 0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, b"flange"),
        (3, b"joint_s1_station", 0, 9, 8, 1, 0.0, 0.0, 0.0, -1.0, 268.0, 1.3,
         b"station_faceplate"),
    ]

    def test_maps_requested_names_to_their_indices(self):
        got = pbd.map_joint_indices(self.ROWS, ("joint_2_l", "joint_s1_station"))
        self.assertEqual(got, {"joint_2_l": 1, "joint_s1_station": 3})

    def test_accepts_str_names_as_well_as_bytes(self):
        rows = [(5, "joint_1_s", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "link_1_s")]
        self.assertEqual(pbd.map_joint_indices(rows, ("joint_1_s",)), {"joint_1_s": 5})

    def test_a_missing_joint_names_itself_and_the_regeneration_command(self):
        with self.assertRaises(KeyError) as ctx:
            pbd.map_joint_indices(self.ROWS, ("joint_6_t",))
        msg = str(ctx.exception)
        self.assertIn("joint_6_t", msg)
        self.assertIn("waam_pipeline.robots.ar1440", msg)

    def test_link_indices_put_the_base_at_minus_one(self):
        links = pbd.map_link_indices(self.ROWS, "base_link")
        self.assertEqual(links[-1], "base_link")
        self.assertEqual(links[0], "link_1_s")
        self.assertEqual(links[3], "station_faceplate")

    def test_the_arm_link_names_match_the_urdf_the_driver_loads(self):
        report = pbd.inspect_urdf(URDF)
        for name in pbd.ARM_LINK_NAMES:
            self.assertIn(name, report.links)


        self.assertNotIn("flange", pbd.ARM_LINK_NAMES)


class TestIgnorePairs(unittest.TestCase):
    def test_every_ignored_pair_names_a_real_link_and_a_real_obstacle(self):
        names = {n for n, *_ in cell_lc.lc_scene().boxes}
        for link, obstacle in pbd.DEFAULT_IGNORE_PAIRS:
            self.assertIn(link, pbd.ARM_LINK_NAMES)
            self.assertIn(obstacle, names)

    def test_the_list_stays_short_and_structural(self):


        self.assertEqual(len(pbd.DEFAULT_IGNORE_PAIRS), 5)
        self.assertNotIn("charles_platter", [o for _l, o in pbd.DEFAULT_IGNORE_PAIRS])


class TestCompare(unittest.TestCase):
    def _v(self, move, obstacle, mm):
        return pbd._violation(move, move + 1, (0.0, 0.0, 0.0), obstacle, mm)

    def test_shared_findings_report_both_numbers(self):
        t1 = [self._v(3, "manpro_training_block", -20.03)]
        t2 = [self._v(3, "manpro_training_block", -18.4)]
        c = pbd.compare(t1, t2)
        self.assertEqual(c.agreed, ((3, "manpro_training_block", -20.03, -18.4),))
        self.assertEqual(c.tier1_only, ())
        self.assertEqual(c.tier2_only, ())
        self.assertTrue(c.verdict_agrees)

    def test_verdict_is_about_blocking_not_about_matching_millimetres(self):


        t1 = [self._v(0, "mh185_headstock", -30.0)]
        t2 = [self._v(0, "mh185_headstock", -1.0)]
        self.assertTrue(pbd.compare(t1, t2).verdict_agrees)

    def test_a_tier2_only_arm_finding_is_a_disagreement(self):
        c = pbd.compare([], [self._v(5, "reamer_yair_185091", -33.8)])
        self.assertFalse(c.verdict_agrees)
        self.assertEqual(len(c.tier2_only), 1)
        self.assertEqual(c.tier1_blocking, 0)
        self.assertEqual(c.tier2_blocking, 1)
        self.assertTrue(any("ARM link" in n for n in c.notes()))

    def test_a_tier1_only_finding_is_explained_as_the_swept_envelope(self):
        c = pbd.compare([self._v(2, "platter_gusset", -4.0)], [])
        self.assertEqual(len(c.tier1_only), 1)
        self.assertTrue(any("swept torch envelope" in n.lower() or "SWEPT" in n
                            for n in c.notes()))

    def test_proximity_to_a_contact_ok_obstacle_is_not_a_disagreement(self):
        t1 = [self._v(0, "charles_platter", 0.9)]
        t2 = [self._v(0, "charles_platter", -0.0)]
        c = pbd.compare(t1, t2)
        self.assertTrue(c.verdict_agrees)
        self.assertIn("AGREE", c.summary())

    def test_both_clean(self):
        c = pbd.compare([], [])
        self.assertTrue(c.verdict_agrees)
        self.assertEqual(c.notes(), [])


class TestEngineGate(unittest.TestCase):
    def test_the_import_error_points_at_the_runbook(self):
        if pbd.HAVE_PYBULLET:
            self.skipTest("pybullet is importable here")
        with self.assertRaises(ImportError) as ctx:
            pbd.require_pybullet()
        msg = str(ctx.exception)
        self.assertIn("docs/wsl2-setup.md", msg)
        self.assertIn("no Windows wheel", msg)

    def test_the_runbook_the_error_points_at_exists(self):
        self.assertTrue((ROOT / "docs" / "wsl2-setup.md").is_file())
        self.assertTrue((ROOT / "tools" / "wsl2-setup.sh").is_file())

    def test_importing_this_package_does_not_need_the_engine(self):
        import sim

        self.assertTrue(sim.__doc__)


class _StubEngine:


    GEOM_BOX, GEOM_SPHERE, GEOM_CAPSULE, GEOM_MESH = 3, 2, 7, 5
    GUI, DIRECT = 1, 2
    MESH_DATA_SIMULATION_MESH = 1

    def __init__(self, contacts=None):
        self.contacts = contacts or (lambda a, b, d: [])
        self.shapes: list[dict] = []
        self.bodies: dict[int, dict] = {}
        self.joint_states: dict[tuple[int, int], float] = {}
        self.poses: list[tuple[int, tuple, tuple]] = []
        self.closest_calls = 0
        self.engine_params: dict = {}
        self.search_paths: list[str] = []
        self.disconnected = False
        self._next = 0


    def connect(self, mode):
        return 0

    def disconnect(self, physicsClientId=0):
        self.disconnected = True

    def setGravity(self, x, y, z, physicsClientId=0):
        self.gravity = (x, y, z)

    def setPhysicsEngineParameter(self, physicsClientId=0, **kw):
        self.engine_params.update(kw)

    def setAdditionalSearchPath(self, path, physicsClientId=0):
        self.search_paths.append(path)


    def loadURDF(self, path, useFixedBase=False, physicsClientId=0):
        report = pbd.inspect_urdf(path)
        self.use_fixed_base = useFixedBase
        self.urdf = path
        self._joints = [
            (i, name.encode(), 0, i, i, 0, 0.0, 0.0, -3.0, 3.0, 1000.0, 4.0,
             child.encode())
            for i, (name, _t, _p, child) in enumerate(report.joints)
        ]
        body = self._new({"kind": "urdf", "path": path})
        return body

    def getNumJoints(self, body, physicsClientId=0):
        return len(self._joints)

    def getJointInfo(self, body, i, physicsClientId=0):
        return self._joints[i]

    def resetJointState(self, body, index, value, physicsClientId=0):
        self.joint_states[(body, index)] = value

    def getLinkState(self, body, index, physicsClientId=0):
        return (None, None, None, None, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))

    def getMeshData(self, body, index, flags=0, physicsClientId=0):
        return (42, [])


    def _new(self, record):
        self._next += 1
        self.bodies[self._next] = record
        return self._next

    def createCollisionShape(self, shapeType, physicsClientId=0, **kw):
        self.shapes.append({"type": shapeType, **kw})
        return len(self.shapes) - 1

    def createVisualShape(self, shapeType, physicsClientId=0, **kw):
        return -1

    def createMultiBody(self, baseMass=0.0, baseCollisionShapeIndex=-1,
                        baseVisualShapeIndex=-1, basePosition=(0, 0, 0),
                        baseOrientation=(0, 0, 0, 1), physicsClientId=0):
        return self._new({"kind": "body", "mass": baseMass,
                          "shape": self.shapes[baseCollisionShapeIndex],
                          "position": tuple(basePosition)})

    def resetBasePositionAndOrientation(self, body, position, orientation,
                                        physicsClientId=0):
        self.poses.append((body, tuple(position), tuple(orientation)))
        self.bodies[body]["position"] = tuple(position)

    def changeDynamics(self, body, link, physicsClientId=0, **kwargs):
        self.bodies[body].update(kwargs)

    def removeBody(self, body, physicsClientId=0):
        self.bodies.pop(body, None)


    def getClosestPoints(self, bodyA, bodyB, distance, physicsClientId=0):
        self.closest_calls += 1
        return self.contacts(bodyA, bodyB, distance)


def _row(link_index: int, metres: float):

    return (0, 0, 0, link_index, -1, (0, 0, 0), (0, 0, 0), (0, 0, 1), metres, 0.0)


class StubEngineDriverTest(unittest.TestCase):


    def make(self, contacts=None, **kwargs):
        from unittest import mock

        self.tp = wall(150.0, 4, 2.0)
        self.origin = cell_lc.snap_toolpath_to_platter(self.tp, 1014.0, -180.0,
                                                       substrate_mm=6.35)
        self.scene = cell_lc.lc_scene(part_origin_robot=self.origin)
        self.engine = _StubEngine(contacts)
        patches = [mock.patch.object(pbd, "_pb", self.engine),
                   mock.patch.object(pbd, "HAVE_PYBULLET", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        drv = pbd.PyBulletDriver(scene=self.scene, part_origin_robot=self.origin,
                                 **kwargs)
        self.addCleanup(drv.close)
        return drv


class TestDriverConstruction(StubEngineDriverTest):
    def test_loads_the_urdf_with_a_fixed_base(self):
        drv = self.make()
        self.assertTrue(self.engine.use_fixed_base)
        self.assertEqual(Path(self.engine.urdf), URDF)

    def test_file_caching_is_off_because_the_urdf_is_generated(self):
        self.make()
        self.assertEqual(self.engine.engine_params.get("enableFileCaching"), 0)

    def test_the_mesh_search_path_is_the_urdfs_own_directory(self):
        self.make()
        self.assertIn(str(URDF.parent), self.engine.search_paths)

    def test_every_joint_is_found_by_name_not_by_index(self):
        drv = self.make()
        for name in ar1440.ARM_JOINT_NAMES + (station.STATION_JOINT_NAME,):
            self.assertIn(name, drv.joint_index)
        self.assertEqual(drv.link_name[-1], "base_link")

    def test_the_station_branch_is_posed_but_not_queried_by_default(self):
        drv = self.make()
        self.assertEqual(drv.queried_links, pbd.ARM_LINK_NAMES)
        for name in station.STATION_LINK_NAMES:
            self.assertNotIn(name, drv.queried_links)

        drv.pose(station_deg=45.0)
        idx = drv.joint_index[station.STATION_JOINT_NAME]
        self.assertAlmostEqual(self.engine.joint_states[(drv.body, idx)],
                               np.radians(45.0))

    def test_the_station_branch_can_be_opted_into(self):
        drv = self.make(urdf_station_collision=True)
        for name in station.STATION_LINK_NAMES:
            self.assertIn(name, drv.queried_links)

    def test_every_obstacle_becomes_one_static_body(self):
        drv = self.make()
        self.assertEqual(set(drv._obstacles),
                         {n for n, *_ in self.scene.boxes}
                         | {n for n, *_ in self.scene.capsules})
        for body in drv._obstacles.values():
            self.assertEqual(self.engine.bodies[body]["mass"], 0.0)

    def test_the_torch_is_three_capsule_bodies_from_tool_interfere_zero(self):
        drv = self.make()
        self.assertEqual(list(drv._torch_bodies), list(pbd.TORCH_SHAPE_NAMES))
        radii = [self.engine.bodies[b]["shape"]["radius"] * 1000.0
                 for b in drv._torch_bodies.values()]
        self.assertEqual([round(r, 3) for r in radii], [50.0, 20.0, 20.0])

    def test_the_torch_can_be_left_out(self):
        drv = self.make(torch=False)
        self.assertEqual(drv._torch_bodies, {})

    def test_an_unfit_urdf_is_refused_before_anything_is_loaded(self):
        import tempfile
        from unittest import mock

        self.engine = _StubEngine()
        for p in (mock.patch.object(pbd, "_pb", self.engine),
                  mock.patch.object(pbd, "HAVE_PYBULLET", True)):
            p.start()
            self.addCleanup(p.stop)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "primitive.urdf"
            ar1440.write_urdf(str(path), mesh_dir="")
            with self.assertRaises(ValueError) as ctx:
                pbd.PyBulletDriver(urdf_path=path)
        self.assertIn("not fit to load", str(ctx.exception))


class TestHcBinding(unittest.TestCase):


    def make_hc(self, **kwargs):
        from unittest import mock

        self.engine = _StubEngine()
        patches = [mock.patch.object(pbd, "_pb", self.engine),
                   mock.patch.object(pbd, "HAVE_PYBULLET", True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        drv = pbd.PyBulletDriver(binding=pbd.HC10_BINDING, **kwargs)
        self.addCleanup(drv.close)
        return drv

    def test_loads_the_committed_hc10dtp_urdf_with_a_fixed_base(self):
        drv = self.make_hc()
        self.assertEqual(Path(self.engine.urdf), pbd.HC10_BINDING.urdf_path)
        self.assertTrue(self.engine.use_fixed_base)

    def test_joints_are_found_by_name_with_no_station_joint_required(self):
        drv = self.make_hc()
        for name in hc10.ARM_JOINT_NAMES:
            self.assertIn(name, drv.joint_index)
        self.assertNotIn(station.STATION_JOINT_NAME, drv.joint_index)
        self.assertEqual(drv.link_name[-1], "base_link")

    def test_queried_links_are_the_hc_arm(self):
        drv = self.make_hc()
        self.assertEqual(drv.queried_links, pbd.HC_ARM_LINK_NAMES)

    def test_ignore_pairs_default_to_the_hc_bindings_own(self):
        drv = self.make_hc()
        self.assertEqual(drv.ignore_pairs, frozenset(cell_hc.HC_PYBULLET_IGNORE_PAIRS))
        self.assertNotEqual(drv.ignore_pairs, frozenset(pbd.DEFAULT_IGNORE_PAIRS))

    def test_torch_is_the_four_controller_tool0_capsules(self):


        drv = self.make_hc()
        self.assertEqual(list(drv._torch_bodies), list(pbd.HC_TORCH_SHAPE_NAMES))
        self.assertEqual(len(pbd.HC_TORCH_SHAPE_NAMES), 4)
        self.assertEqual(len(drv._torch_caps), 4)
        radii = [self.engine.bodies[b]["shape"]["radius"] * 1000.0
                 for b in drv._torch_bodies.values()]
        self.assertEqual([round(r, 3) for r in radii], [40.0, 35.0, 25.0, 15.0])

    def test_hc_torch_follows_the_solved_flange(self):
        drv = self.make_hc()
        q = [10.0, -20.0, 30.0, 0.0, 15.0, 0.0]
        drv.pose(q_deg=q)
        flange = hc10.fk(q)["flange"]
        placed = {body_id: pos for body_id, pos, _o in self.engine.poses}
        name = "torch/hc_tool0_tip"
        body = drv._torch_bodies[name]
        p0, p1, _r = dict(zip(pbd.HC_TORCH_SHAPE_NAMES, drv._torch_caps))[name]
        want = (flange[:3, :3] @ ((p0 + p1) / 2.0) + flange[:3, 3]) * 1e-3
        np.testing.assert_allclose(placed[body], want, atol=1e-9)

    def test_urdf_station_collision_is_refused_for_a_stationless_binding(self):
        with self.assertRaises(ValueError) as ctx:
            self.make_hc(urdf_station_collision=True)
        self.assertIn("no station branch", str(ctx.exception))

        self.assertEqual(self.engine.bodies, {})

    def test_origin_splits_into_robot_base_and_native_table_frames(self):


        drv = self.make_hc()
        np.testing.assert_allclose(drv.origin, pbd.HC10_BINDING.default_origin)
        native = pbd.HC10_BINDING.native_origin(drv.origin)
        np.testing.assert_allclose(native, cell_hc.DEFAULT_PART_ORIGIN)

    def test_pose_refuses_a_nonzero_station_degree(self):
        drv = self.make_hc()
        with self.assertRaises(ValueError):
            drv.pose(q_deg=[0.0] * 6, station_deg=30.0)

    def test_pose_writes_pendant_degrees_by_hc_joint_name(self):
        drv = self.make_hc()
        q = [10.0, -20.0, 30.0, 0.0, 15.0, 0.0]
        drv.pose(q_deg=q)
        for name, deg in zip(hc10.ARM_JOINT_NAMES, q):
            self.assertAlmostEqual(
                self.engine.joint_states[(drv.body, drv.joint_index[name])],
                np.radians(deg))


class TestAuditRegressions(StubEngineDriverTest):
    def test_existing_beads_follow_station_rotation_without_recreation(self):
        drv = self.make()
        bead = pbd.BeadSpec(0, 57, (0, 0, 0), (100, 0, 0), 4.5, 2., (-1., -1.))
        drv.add_bead(bead)
        body = drv._bead_bodies[0]
        count = len(self.engine.bodies)
        drv.place_scene(station_deg=90.)
        expected = station.apply(np.mean([bead.p0, bead.p1], axis=0) + drv.origin, 90.) / 1000
        np.testing.assert_allclose(self.engine.bodies[body]["position"], expected, atol=1e-9)
        self.assertEqual(count, len(self.engine.bodies))

    def test_single_target_is_queried_even_without_a_segment(self):
        from waam_pipeline.toolpath import Move, Toolpath
        drv = self.make()
        drv.contacts_at = lambda *a, **kw: [("link_4_r", "fixture", -5.)]
        tp = Toolpath([Move(0, 0, 5, None, False, 0, 1)])
        result = drv.check(tp, bead_height_mm=2.0, solve=lambda xyz: [0.] * 6)
        self.assertEqual(result.waypoints, 1)
        self.assertEqual(result.poses, 1)
        self.assertEqual(result.blocking()[0].move_index, 0)

    def test_transverse_air_move_does_not_get_weld_zone_forgiveness(self):
        from waam_pipeline.toolpath import Move, Toolpath
        drv = self.make()
        tp = Toolpath([Move(0, -10, 5, None, False, 0, 1),
                       Move(0, 10, 5, 200, True, 0, 2),
                       Move(-1, 0, 5, None, False, 0, 3),
                       Move(1, 0, 5, None, False, 0, 4)])
        drv.contacts_at = lambda *a, **kw: (
            [(pbd.TORCH_SHAPE_NAMES[-1], "bead(line 2)", -1.)]
            if drv._bead_list else [])
        result = drv.check(tp, bead_height_mm=2.0, solve=lambda xyz: [0.] * 6)
        self.assertTrue(any(v.move_index == 3 and v.obstacle == "bead(line 2)"
                            for v in result.blocking()))


class TestPosingAndPlacement(StubEngineDriverTest):
    def test_posing_writes_pendant_degrees_as_radians_by_joint_name(self):
        drv = self.make()
        q = [10.0, 20.0, -30.0, 5.0, 40.0, -15.0]
        drv.pose(q_deg=q)
        for name, deg in zip(ar1440.ARM_JOINT_NAMES, q):
            self.assertAlmostEqual(
                self.engine.joint_states[(drv.body, drv.joint_index[name])],
                np.radians(deg))

    def test_the_torch_follows_the_solved_flange(self):
        drv = self.make()
        q = [0.0, 20.0, -30.0, 0.0, 40.0, 0.0]
        drv.pose(q_deg=q)
        flange = ar1440.fk(q)["flange"]
        placed = {b: pos for b, pos, _o in self.engine.poses}
        body = drv._torch_bodies["torch/gooseneck"]
        a, b, _r = pbd.torch_capsules_flange()[2]
        want = (flange[:3, :3] @ ((a + b) / 2.0) + flange[:3, 3]) * 1e-3
        np.testing.assert_allclose(placed[body], want, atol=1e-9)

    def test_turning_the_station_moves_part_fixed_bodies_only(self):
        drv = self.make()
        before = {n: self.engine.bodies[b]["position"]
                  for n, b in drv._obstacles.items()}
        drv.place_scene(station_deg=90.0)
        after = {n: self.engine.bodies[b]["position"]
                 for n, b in drv._obstacles.items()}
        self.assertNotEqual(before["charles_platter"], after["charles_platter"])
        self.assertEqual(before["mh185_headstock"], after["mh185_headstock"])
        self.assertEqual(before["reamer_yair_185091"], after["reamer_yair_185091"])
        for name in ("reamer_base", "reamer_foot", "reamer_post", "reamer_mount"):
            self.assertIn(name, before)
            self.assertEqual(before[name], after[name])

    def test_bodies_are_moved_not_rebuilt_when_the_station_turns(self):
        drv = self.make()
        n_bodies = len(self.engine.bodies)
        drv.place_scene(station_deg=30.0)
        drv.place_scene(station_deg=60.0)
        self.assertEqual(len(self.engine.bodies), n_bodies)


class TestWholePathCheck(StubEngineDriverTest):
    def test_a_clean_engine_gives_a_clean_result(self):
        drv = self.make(contacts=lambda a, b, d: [])
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=40.0)
        self.assertEqual(res.violations, [])
        self.assertEqual(res.hits, [])
        self.assertEqual(res.unreachable, [])
        self.assertGreater(res.poses, 0)
        self.assertEqual(res.poses, res.waypoints)
        self.assertGreater(self.engine.closest_calls, 0)

    def test_beads_appear_only_after_their_deposit_move(self):
        counts = []
        drv = self.make(contacts=lambda a, b, d: [])

        def watch(i, n):
            counts.append(len(drv._bead_list))

        drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0, progress=watch)
        self.assertEqual(counts[0], 0)
        self.assertEqual(drv._bead_list[-1].line_no,
                         [m.line_no for m in self.tp.moves if m.deposit][-1])
        self.assertEqual(sorted(counts), counts)

    def test_a_penetration_becomes_a_negative_millimetre_violation(self):
        drv = self.make()
        target = None

        def contacts(a, b, d):
            if b == target:
                return [_row(drv.link_index["link_4_r"], -0.0338)]
            return []

        self.engine.contacts = contacts
        target = drv._obstacles["reamer_yair_185091"]
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        worst = res.worst()
        self.assertEqual(worst.obstacle, "reamer_yair_185091")
        self.assertAlmostEqual(worst.clearance_mm, -33.8)
        self.assertTrue(res.blocking())
        self.assertEqual({h.link for h in res.arm_hits()}, {"link_4_r"})

    def test_only_the_worst_clearance_per_move_and_obstacle_is_reported(self):
        drv = self.make()
        seq = iter([-1.0, -4.0, -2.0] * 500)
        target = drv._obstacles["mh185_headstock"]
        self.engine.contacts = (
            lambda a, b, d: [_row(drv.link_index["link_3_u"], next(seq))]
            if b == target else [])
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        per_move = [v for v in res.violations if v.obstacle == "mh185_headstock"]
        self.assertTrue(per_move)
        self.assertEqual(len(per_move), len({v.move_index for v in per_move}))
        self.assertTrue(all(v.clearance_mm <= -1.0 for v in per_move))

    def test_mounting_pairs_are_suppressed_and_others_are_not(self):
        drv = self.make()
        pedestal = drv._obstacles["robot_pedestal"]
        headstock = drv._obstacles["mh185_headstock"]


        self.engine.contacts = lambda a, b, d: (
            [_row(drv.link_index["base_link"], -0.05)]
            if a == drv.body and b in (pedestal, headstock) else [])
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        names = {v.obstacle for v in res.violations}
        self.assertNotIn("robot_pedestal", names)
        self.assertIn("mh185_headstock", names)

    def test_a_mounting_pair_does_not_silence_the_torch(self):
        drv = self.make()
        pedestal = drv._obstacles["robot_pedestal"]
        self.engine.contacts = lambda a, b, d: (
            [_row(-1, -0.05)]
            if b == pedestal and a in drv._torch_bodies.values() else [])
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        self.assertIn("robot_pedestal", {v.obstacle for v in res.violations})

    def test_the_weld_zone_forgives_the_torch_but_not_the_arm(self):
        for link, expected in (("torch/gooseneck", False), ("link_4_r", True)):
            with self.subTest(link=link):
                drv = self.make()
                beads = {}

                def contacts(a, b, d, _drv=drv, _beads=beads, _link=link):
                    name = _beads.get(b)
                    if name is None:
                        return []
                    if _link.startswith("torch"):
                        return ([_row(-1, -0.001)]
                                if a == _drv._torch_bodies[_link] else [])
                    return ([_row(_drv.link_index[_link], -0.001)]
                            if a == _drv.body else [])

                self.engine.contacts = contacts
                original = drv.add_bead

                def add_bead(bead, station_deg=0.0, _original=original, _beads=beads, _drv=drv):
                    _original(bead, station_deg=station_deg)
                    _beads[_drv._bead_bodies[-1]] = bead.obstacle

                drv.add_bead = add_bead
                res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
                got = any(v.obstacle.startswith("bead(") for v in res.violations)
                self.assertEqual(got, expected)

    def test_bead_exclusion_all_matches_tier_one_and_forgives_the_arm_too(self):
        drv = self.make()
        beads = {}
        original = drv.add_bead

        def add_bead(bead, station_deg=0.0):
            original(bead, station_deg=station_deg)
            beads[drv._bead_bodies[-1]] = bead.obstacle

        drv.add_bead = add_bead
        self.engine.contacts = lambda a, b, d: (
            [_row(drv.link_index["link_4_r"], -0.001)] if b in beads else [])
        strict = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0,
                           bead_exclusion="torch")
        beads.clear()
        parity = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0,
                           bead_exclusion="all")
        self.assertTrue(any(v.obstacle.startswith("bead(") for v in strict.violations))
        self.assertGreater(
            len([v for v in strict.violations if v.obstacle.startswith("bead(")]),
            len([v for v in parity.violations if v.obstacle.startswith("bead(")]),
            "bead_exclusion='all' must forgive strictly more than 'torch'")

    def test_an_unknown_bead_exclusion_mode_is_refused(self):
        drv = self.make()
        with self.assertRaises(ValueError):
            drv.check(self.tp, bead_height_mm=2.0, bead_exclusion="sometimes")

    def test_an_unreachable_point_is_recorded_and_never_becomes_a_violation(self):
        drv = self.make(contacts=lambda a, b, d: [])

        def solve(_xyz):
            raise ValueError("unreachable")

        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0, solve=solve)
        self.assertGreater(len(res.unreachable), 0)
        self.assertEqual(res.poses, 0)
        self.assertEqual(res.violations, [])

    def test_a_turned_station_refuses_the_translation_only_solver(self):
        drv = self.make(contacts=lambda a, b, d: [])
        with self.assertRaises(ValueError) as ctx:
            drv.check(self.tp, bead_height_mm=2.0, station_deg=30.0)
        msg = str(ctx.exception)
        self.assertIn("station-aware", msg)
        self.assertIn("TRANSLATION ALONE", msg)

    def test_a_station_aware_solver_is_accepted(self):
        drv = self.make(contacts=lambda a, b, d: [])
        solve = cell_lc.pulse_solver(self.origin)
        res = drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0,
                        station_deg=30.0, solve=solve)
        self.assertEqual(res.station_deg, 30.0)

    def test_the_bead_prune_only_drops_beads_the_arm_cannot_reach(self):
        drv = self.make(contacts=lambda a, b, d: [])
        drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        q = cell_lc.pulse_solver(self.origin)(self.tp.moves[-1].target)
        near = [n for n, _b in drv._bead_targets(q, 5.0, 0.0)]
        self.assertTrue(near)


        drv._bead_list.append(
            pbd.BeadSpec(0, 998, (0.0, 9e5, 0.0), (10.0, 9e5, 0.0), 4.5, 0.0, (-1.0, -1.0)))
        drv._bead_bodies.append(-99)
        after = [n for n, _b in drv._bead_targets(q, 5.0, 0.0)]
        self.assertNotIn("bead(line 998)", after)
        self.assertEqual(sorted(near), sorted(after))

    def test_clear_beads_removes_the_bodies(self):
        drv = self.make(contacts=lambda a, b, d: [])
        drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=60.0)
        self.assertTrue(drv._bead_bodies)
        drv.clear_beads()
        self.assertEqual(drv._bead_bodies, [])
        self.assertEqual(drv._bead_list, [])

    def test_the_shape_report_names_a_hull_when_the_engine_built_one(self):
        drv = self.make()
        reps = drv.inspect_collision_shapes()
        self.assertEqual({r.link for r in reps}, set(ar1440._MESH_LINKS))
        for r in reps:
            self.assertIn("CONVEX HULL", r.verdict)
            self.assertGreater(r.stl_vertices, r.sim_vertices)

    def test_closing_disconnects(self):
        drv = self.make(contacts=lambda a, b, d: [])
        drv.close()
        self.assertTrue(self.engine.disconnected)


@unittest.skipUnless(pbd.HAVE_PYBULLET, "pybullet not installed (docs/wsl2-setup.md)")
class TestAgainstTheEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tp = wall(150.0, 6, 2.0)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.tp, 1014.0, -180.0,
                                                      substrate_mm=6.35)
        cls.scene = cell_lc.lc_scene(part_origin_robot=cls.origin)
        cls.drv = pbd.PyBulletDriver(scene=cls.scene, part_origin_robot=cls.origin)

    @classmethod
    def tearDownClass(cls):
        cls.drv.close()

    def test_every_joint_the_driver_poses_was_found_by_name(self):
        for name in ar1440.ARM_JOINT_NAMES + (station.STATION_JOINT_NAME,):
            self.assertIn(name, self.drv.joint_index)

    def test_every_arm_link_carries_collision_geometry(self):
        for name in pbd.ARM_LINK_NAMES:
            self.assertIn(name, self.drv.link_index)

    def test_posing_moves_the_flange_where_fk_says(self):
        q = [10.0, 20.0, -30.0, 0.0, 40.0, 0.0]
        self.drv.pose(q_deg=q)
        idx = self.drv.link_index["link_6_t"]
        state = self.drv._p.getLinkState(self.drv.body, idx,
                                         physicsClientId=self.drv.client)
        np.testing.assert_allclose(np.asarray(state[4]) * 1000.0,
                                   ar1440.fk(q)["flange"][:3, 3], atol=1.0)

    def test_the_station_branch_turns_and_the_arm_does_not(self):
        self.drv.pose(q_deg=[0.0] * 6, station_deg=0.0)
        idx = self.drv.link_index["station_platter"]
        at_zero = self.drv._p.getLinkState(self.drv.body, idx,
                                           physicsClientId=self.drv.client)[5]
        self.drv.pose(station_deg=90.0)
        turned = self.drv._p.getLinkState(self.drv.body, idx,
                                          physicsClientId=self.drv.client)[5]
        self.assertFalse(np.allclose(at_zero, turned))

    def test_collision_shape_report_says_hull_or_mesh_for_every_link(self):
        for rep in self.drv.inspect_collision_shapes():
            self.assertGreater(rep.stl_vertices, 0, rep.link)
            self.assertTrue(rep.verdict)

    def test_the_known_clean_wall_agrees_with_tier_one(self):
        t1 = clearance.check(self.tp, TorchModel.yaskawa_accufast_45(), self.scene,
                             bead_height_mm=2.0, margin_mm=5.0, contact_ok=clearance.CONTACT_OK)
        t2 = self.drv.check(self.tp, bead_height_mm=2.0, margin_mm=5.0)
        self.assertEqual(t2.unreachable, [])
        self.assertTrue(pbd.compare(t1, t2.violations).verdict_agrees)
        self.assertEqual(blocking(t1), [])
        self.assertEqual(t2.blocking(), [])

    def test_the_known_fouling_wall_agrees_with_tier_one(self):


        tp = wall(150.0, 6, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -300.0, substrate_mm=6.35)
        scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=True)
        t1 = clearance.check(tp, TorchModel.yaskawa_accufast_45(), scene,
                             bead_height_mm=2.0, margin_mm=5.0, contact_ok=clearance.CONTACT_OK)
        self.assertTrue(blocking(t1))
        with pbd.PyBulletDriver(scene=scene, part_origin_robot=origin) as drv:
            t2 = drv.check(tp, bead_height_mm=2.0, margin_mm=5.0)
        self.assertTrue(t2.blocking())
        self.assertTrue(pbd.compare(t1, t2.violations).verdict_agrees)

    def test_a_turned_station_refuses_the_translation_only_solver(self):
        with self.assertRaises(ValueError) as ctx:
            self.drv.check(self.tp, bead_height_mm=2.0, station_deg=30.0)
        self.assertIn("station-aware", str(ctx.exception))

    def test_registered_substrate_is_a_solid_in_the_engine(self):
        from waam_pipeline.substrate import Substrate
        corners = np.array([[-100, -50, 0], [100, -50, 0], [100, 50, 0], [-100, 50, 0]], float)
        rotation = ar1440._rot((0, 1, 0), 3)
        plate = Substrate.fit(corners @ rotation.T, 8)
        scene = Scene(substrates=[plate], part_fixed=(plate.name,))
        with pbd.PyBulletDriver(scene=scene, part_origin_robot=self.origin) as drv:
            p = drv._p
            probe = drv._make(pbd.BodySpec("probe", "capsule", (0, 0, 0), (0, 0, 0, 1),
                                           radius=.0001, length=0))
            for height in (10, -4):
                position = (self.origin + plate.basis[:, 2] * height) / 1000
                p.resetBasePositionAndOrientation(probe, position, (0, 0, 0, 1), physicsClientId=drv.client)
                hits = p.getClosestPoints(probe, drv._obstacles[plate.name], .03, physicsClientId=drv.client)
                self.assertTrue(hits)
                self.assertEqual(hits[0][8] < 0, height < 0)
            drv.place_scene(station_deg=30)
            placed, orientation = p.getBasePositionAndOrientation(drv._obstacles[plate.name], physicsClientId=drv.client)
            expected = pbd.scene_body_specs(scene, self.origin, station_deg=30)[0]
            np.testing.assert_allclose(placed, expected.position, atol=1e-10)
            np.testing.assert_allclose(orientation, expected.orientation, atol=1e-10)


@unittest.skipUnless(pbd.HAVE_PYBULLET, "pybullet not installed (docs/wsl2-setup.md)")
class TestHcAgainstTheEngine(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.drv = pbd.PyBulletDriver(binding=pbd.HC10_BINDING)

    @classmethod
    def tearDownClass(cls):
        cls.drv.close()

    def test_every_hc_joint_the_driver_poses_was_found_by_name(self):
        for name in hc10.ARM_JOINT_NAMES:
            self.assertIn(name, self.drv.joint_index)

    def test_every_hc_arm_link_carries_collision_geometry(self):
        for name in pbd.HC_ARM_LINK_NAMES:
            self.assertIn(name, self.drv.link_index)

    def test_posing_moves_the_flange_where_hc10_fk_says(self):
        q = [10.0, 20.0, -30.0, 0.0, 40.0, 0.0]
        self.drv.pose(q_deg=q)
        idx = self.drv.link_index["link_6_t"]
        state = self.drv._p.getLinkState(self.drv.body, idx,
                                         physicsClientId=self.drv.client)
        np.testing.assert_allclose(np.asarray(state[4]) * 1000.0,
                                   hc10.fk(q)["flange"][:3, 3], atol=1.0)

    def test_collision_shape_report_says_hull_or_mesh_for_every_hc_link(self):
        for rep in self.drv.inspect_collision_shapes():
            self.assertGreater(rep.stl_vertices, 0, rep.link)
            self.assertTrue(rep.verdict)

    def test_a_reachable_hc_toolpath_runs_end_to_end_uncalibrated(self):


        tp = wall(150.0, 3, 2.0)
        res = self.drv.check(tp, bead_height_mm=2.0, margin_mm=5.0, sample_mm=20.0)
        self.assertEqual(res.unreachable, [])
        self.assertGreater(res.poses, 0)


if __name__ == "__main__":
    unittest.main()
