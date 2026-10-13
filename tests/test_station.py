import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, station


class TestAxisGeometry(unittest.TestCase):
    def test_points_on_the_axis_do_not_move(self):
        o = station.axis_origin()
        for t in (0.0, 37.0, -90.0, 180.0):
            np.testing.assert_allclose(station.apply(o, t), o, atol=1e-9)

        p = o + np.array([0.0, 250.0, 0.0])
        np.testing.assert_allclose(station.apply(p, 61.0), p, atol=1e-9)

    def test_rotation_is_about_the_axis_line_not_the_world_origin(self):

        p = np.array([cell_lc.FACEPLATE_X + 100.0, -300.0, cell_lc.AXIS_Z + 40.0])
        q = station.apply(p, 180.0)
        self.assertAlmostEqual(q[1], p[1], places=6)
        self.assertAlmostEqual(q[0], cell_lc.FACEPLATE_X - 100.0, places=6)
        self.assertAlmostEqual(q[2], cell_lc.AXIS_Z - 40.0, places=6)

    def test_radius_from_the_axis_is_preserved(self):
        o, d = station.axis_origin(), station.axis_direction()
        p = np.array([cell_lc.FACEPLATE_X + 210.0, -120.0, cell_lc.AXIS_Z + 55.0])

        def radius(x):
            v = np.asarray(x, float) - o
            return float(np.linalg.norm(v - np.dot(v, d) * d))

        for t in (13.0, 90.0, -145.0):
            self.assertAlmostEqual(radius(station.apply(p, t)), radius(p), places=6)

    def test_zero_is_identity_and_rotations_compose(self):
        p = np.array([1200.0, -80.0, 300.0])
        np.testing.assert_allclose(station.apply(p, 0.0), p, atol=1e-9)
        np.testing.assert_allclose(
            station.apply(station.apply(p, 30.0), 45.0),
            station.apply(p, 75.0), atol=1e-6)

    def test_sense_flips_the_direction_of_travel(self):
        p = np.array([cell_lc.FACEPLATE_X + 100.0, 0.0, cell_lc.AXIS_Z])
        a = station.apply(p, 30.0, sense=+1)
        b = station.apply(p, 30.0, sense=-1)
        self.assertAlmostEqual(a[0], b[0], places=6)
        self.assertAlmostEqual(a[2], -b[2] + 2 * cell_lc.AXIS_Z, places=6)
        self.assertFalse(np.allclose(a, b))

    def test_bad_sense_is_refused(self):
        with self.assertRaises(ValueError):
            station.axis_direction(0)


class TestDownhand(unittest.TestCase):
    def test_already_flat_face_needs_no_rotation(self):
        theta, err = station.best_downhand_angle([0, 0, 1])
        self.assertAlmostEqual(err, 0.0, places=3)
        self.assertAlmostEqual(theta, 0.0, places=3)

    def test_tilted_face_is_rotated_flat(self):

        n = cell_lc._rot((0, 1, 0), 30.0) @ np.array([0, 0, 1.0])
        theta, err = station.best_downhand_angle(n)
        self.assertLess(err, 0.05)
        self.assertAlmostEqual(abs(theta), 30.0, delta=0.2)

    def test_error_is_zero_only_when_normal_points_up(self):
        self.assertAlmostEqual(station.downhand_error_deg([0, 0, 1], 0.0), 0.0, places=6)
        self.assertAlmostEqual(station.downhand_error_deg([1, 0, 0], 0.0), 90.0, places=6)
        self.assertAlmostEqual(station.downhand_error_deg([0, 0, -1], 0.0), 180.0, places=6)

    def test_component_along_the_axis_cannot_be_rotated_away(self):


        n = cell_lc._rot((1, 0, 0), 20.0) @ np.array([0, 0, 1.0])
        _theta, err = station.best_downhand_angle(n)
        self.assertAlmostEqual(err, 20.0, delta=0.3)

    def test_limits_clamp_the_answer(self):
        n = cell_lc._rot((0, 1, 0), 80.0) @ np.array([0, 0, 1.0])
        theta, err = station.best_downhand_angle(n, limits_deg=(-25.0, 25.0))
        self.assertLessEqual(abs(theta), 25.0 + 1e-6)
        self.assertGreater(err, 25.0)


class TestPlanner(unittest.TestCase):
    def _ramp(self, n=6, per=10.0):
        return [cell_lc._rot((0, 1, 0), i * per) @ np.array([0, 0, 1.0]) for i in range(n)]

    def test_plan_tracks_a_ramping_face(self):
        plan = station.plan_downhand(self._ramp())
        self.assertEqual(len(plan), 6)
        for m in plan:
            self.assertLess(m.downhand_error_deg, 0.1)
            self.assertFalse(m.clamped)
        self.assertAlmostEqual(abs(plan[-1].theta_deg - plan[0].theta_deg), 50.0, delta=0.5)

    def test_max_step_caps_rotation_and_says_so(self):
        plan = station.plan_downhand(self._ramp(per=40.0), max_step_deg=5.0)
        self.assertTrue(any(m.clamped for m in plan))
        for a, b in zip(plan, plan[1:]):
            self.assertLessEqual(abs(b.theta_deg - a.theta_deg), 5.0 + 1e-6)

        self.assertGreater(max(m.downhand_error_deg for m in plan), 1.0)

    def test_speed_check_flags_impossible_rotation(self):
        fast = station.plan_downhand(self._ramp(per=60.0), move_seconds=0.2)
        self.assertTrue(station.speed_violations(fast))
        slow = station.plan_downhand(self._ramp(per=5.0), move_seconds=30.0)
        self.assertEqual(station.speed_violations(slow), [])

    def test_rate_is_zero_on_the_first_move(self):
        plan = station.plan_downhand(self._ramp(), move_seconds=1.0)
        self.assertEqual(plan[0].rate_deg_s, 0.0)


class TestNotEmitting(unittest.TestCase):
    def test_module_emits_no_inform(self):

        src = (ROOT / "src" / "waam_pipeline" / "station.py").read_text(encoding="utf-8")
        for token in ("SMOVL", "SMOVC", "EC0", "///POSTYPE"):
            self.assertNotIn(f'"{token}', src)
            self.assertNotIn(f"'{token}", src)


if __name__ == "__main__":
    unittest.main()


class TestAxisFromCalibration(unittest.TestCase):


    def _touches(self, angles, sense=+1, radius=250.0, along=-140.0):

        o, d = station.axis_origin(), station.axis_direction(sense)

        e1 = np.array([1.0, 0.0, 0.0])
        e1 = e1 - np.dot(e1, d) * d
        e1 /= np.linalg.norm(e1)
        p0 = o + radius * e1 + along * d
        return np.array([station.apply(p0, t, sense=sense) for t in angles])

    def test_three_touches_recover_the_axis(self):
        pts = self._touches([0.0, 40.0, 95.0])
        fit = station.fit_station_axis(pts)
        self.assertAlmostEqual(fit.radius_mm, 250.0, delta=1e-3)
        self.assertLess(fit.residual_mm, 1e-6)
        self.assertTrue(fit.agrees_with_model(station.axis_origin(),
                                              station.axis_direction()))

    def test_recovered_axis_is_the_line_not_just_a_direction(self):
        pts = self._touches([10.0, 70.0, 150.0])
        fit = station.fit_station_axis(pts)

        o, d = station.axis_origin(), station.axis_direction()
        v = fit.origin - o
        self.assertLess(float(np.linalg.norm(v - np.dot(v, d) * d)), 1e-6)

    def test_a_wrong_axis_is_reported_as_disagreeing(self):
        pts = self._touches([0.0, 45.0, 100.0])
        fit = station.fit_station_axis(pts)
        moved = station.axis_origin() + np.array([300.0, 0.0, 0.0])
        self.assertFalse(fit.agrees_with_model(moved, station.axis_direction()))

    def test_angles_resolve_the_handedness(self):
        angles = [0.0, 35.0, 80.0]
        for sense in (+1, -1):
            pts = self._touches(angles, sense=sense)
            fit = station.fit_station_axis(pts, angles_deg=angles)


            self.assertLess(fit.sense_residual_deg, 0.5)
            R = cell_lc._rot(tuple(fit.direction * fit.sense), angles[1] - angles[0])
            back = R @ (pts[0] - fit.origin) + fit.origin
            np.testing.assert_allclose(back, pts[1], atol=1e-6)

    def test_needs_at_least_three_touches(self):
        with self.assertRaises(ValueError):
            station.fit_station_axis(self._touches([0.0, 30.0]))

    def test_angle_count_must_match(self):
        with self.assertRaises(ValueError):
            station.fit_station_axis(self._touches([0.0, 30.0, 60.0]),
                                     angles_deg=[0.0, 30.0])

    def test_pulses_convert_with_the_pendant_rates(self):
        from waam_pipeline.robots import ar1440
        deg = station.pulses_to_degrees([-23605, 70044, -71193, 141046, -81183, -31192])
        self.assertAlmostEqual(deg[0], -23605 / ar1440.PULSES_PER_DEG[0], places=9)
        self.assertEqual(len(deg), 6)
        with self.assertRaises(ValueError):
            station.pulses_to_degrees([1, 2, 3])


class TestPartFrameTransform(unittest.TestCase):


    def _axis_in_part(self, origin=None):
        o = cell_lc.DEFAULT_PART_ORIGIN if origin is None else np.asarray(origin, float)
        return station.axis_origin() - o

    def test_zero_is_exactly_the_identity(self):
        np.testing.assert_array_equal(station.part_frame_transform(0.0), np.eye(4))

    def test_it_is_the_robot_frame_transform_seen_from_the_part(self):
        o = cell_lc.DEFAULT_PART_ORIGIN
        p_part = np.array([12.0, -30.0, 5.0])
        for t in (17.0, -63.0, 144.0):
            T = station.part_frame_transform(t)
            got = T[:3, :3] @ p_part + T[:3, 3] + o
            np.testing.assert_allclose(got, station.apply(p_part + o, t), atol=1e-9)

    def test_rotation_is_about_the_axis_line_not_the_part_origin(self):


        T = station.part_frame_transform(180.0)
        moved = T[:3, 3]
        a = self._axis_in_part()


        self.assertGreater(float(np.linalg.norm(moved)), float(np.hypot(a[0], a[2])))
        np.testing.assert_allclose(moved, a - T[:3, :3] @ a, atol=1e-9)

    def test_points_on_the_axis_stay_put_in_part_coordinates(self):
        a = self._axis_in_part()
        for t in (0.0, 31.0, -95.0):
            T = station.part_frame_transform(t)
            np.testing.assert_allclose(T[:3, :3] @ a + T[:3, 3], a, atol=1e-9)

    def test_a_part_origin_on_the_axis_makes_it_a_pure_rotation(self):
        T = station.part_frame_transform(40.0, station.axis_origin())
        np.testing.assert_allclose(T[:3, 3], np.zeros(3), atol=1e-9)

    def test_sense_flips_the_direction(self):
        p = np.array([120.0, 0.0, 0.0])
        a = station.part_frame_transform(25.0, station.axis_origin(), sense=+1)
        b = station.part_frame_transform(25.0, station.axis_origin(), sense=-1)
        pa, pb = a[:3, :3] @ p, b[:3, :3] @ p
        self.assertAlmostEqual(pa[0], pb[0], places=9)
        self.assertAlmostEqual(pa[2], -pb[2], places=9)
        self.assertFalse(np.allclose(pa, pb))


class TestSpeedFigure(unittest.TestCase):
    def test_the_deg_per_second_is_derived_from_the_cited_rpm(self):


        self.assertAlmostEqual(station.MAX_SPEED_RPM, 12.4, places=6)
        self.assertAlmostEqual(station.MAX_SPEED_DEG_S, 74.4, places=6)
        self.assertAlmostEqual(station.MAX_SPEED_DEG_S,
                               station.MAX_SPEED_RPM * 360.0 / 60.0, places=9)

    def test_the_gear_ratio_predicts_the_unverified_pulse_scale(self):

        self.assertEqual(station.GEAR_RATIO, 161)
        self.assertAlmostEqual(4096 * station.GEAR_RATIO / 360.0, 1831.82, places=2)


class TestUrdfFragment(unittest.TestCase):


    def _root(self, **kw):
        return ET.fromstring("<robot>" + station.urdf_fragment(**kw) + "</robot>")

    def test_it_is_one_rotating_joint_hung_off_the_named_parent(self):
        root = self._root()
        joints = {j.get("name"): j for j in root.findall("joint")}
        self.assertEqual(joints["station_mount"].find("parent").get("link"), "base_link")
        rot = joints[station.STATION_JOINT_NAME]
        self.assertEqual(rot.get("type"), "revolute")
        self.assertEqual(rot.find("parent").get("link"), station.STATION_LINK_NAMES[0])
        self.assertEqual(rot.find("child").get("link"), station.STATION_LINK_NAMES[1])
        self.assertEqual([ln.get("name") for ln in root.findall("link")],
                         list(station.STATION_LINK_NAMES))

    def test_the_mount_sits_on_the_measured_axis(self):
        joints = {j.get("name"): j for j in self._root().findall("joint")}
        xyz = [float(v) for v in joints["station_mount"].find("origin").get("xyz").split()]

        np.testing.assert_allclose(
            np.array(xyz) * 1000.0,
            [cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y, cell_lc.AXIS_Z], atol=1e-6)

    def test_the_axis_vector_is_the_only_place_the_sense_lives(self):
        for sense in (+1, -1):
            joints = {j.get("name"): j for j in self._root(sense=sense).findall("joint")}
            axis = [float(v) for v in
                    joints[station.STATION_JOINT_NAME].find("axis").get("xyz").split()]
            np.testing.assert_allclose(axis, station.axis_direction(sense), atol=1e-12)

        plus, minus = station.urdf_fragment(sense=+1), station.urdf_fragment(sense=-1)
        self.assertEqual(len(plus.splitlines()), len(minus.splitlines()))
        differing = [(a, b) for a, b in zip(plus.splitlines(), minus.splitlines()) if a != b]
        self.assertEqual(len(differing), 1)
        self.assertIn('<axis xyz="0 1 0"/>', differing[0][0])
        self.assertIn('<axis xyz="0 -1 0"/>', differing[0][1])

    def test_travel_is_the_controllers_soft_limit_not_unstated(self):


        joints = {j.get("name"): j for j in self._root().findall("joint")}
        limit = joints[station.STATION_JOINT_NAME].find("limit")
        self.assertAlmostEqual(float(limit.get("lower")),
                               -np.radians(station.SOFT_LIMIT_DEG), places=4)
        self.assertAlmostEqual(float(limit.get("upper")),
                               np.radians(station.SOFT_LIMIT_DEG), places=4)
        self.assertEqual(station.SOFT_LIMIT_DEG, 360.0)

        self.assertEqual(round(station.SOFT_LIMIT_DEG
                               * 4096 * station.GEAR_RATIO / 360.0),
                         4096 * station.GEAR_RATIO)
        self.assertAlmostEqual(float(limit.get("velocity")),
                               np.radians(station.MAX_SPEED_DEG_S), places=4)
        self.assertAlmostEqual(float(limit.get("effort")),
                               station.HOLDING_TORQUE_NM, places=3)

    def test_the_gen2_platter_comes_from_the_hardware_model(self):
        design = station._platter_design()
        root = self._root(platter="gen2")
        link = [ln for ln in root.findall("link")
                if ln.get("name") == station.STATION_LINK_NAMES[2]][0]
        cyl = link.find("collision/geometry/cylinder")
        self.assertAlmostEqual(float(cyl.get("radius")) * 2000.0, design.diameter_mm, places=6)
        self.assertAlmostEqual(float(cyl.get("length")) * 1000.0, design.thickness_mm, places=6)

    def test_the_faceplate_is_a_disc_because_it_turns(self):


        link = [ln for ln in self._root().findall("link")
                if ln.get("name") == station.STATION_LINK_NAMES[1]][0]
        cyl = link.find("collision/geometry/cylinder")
        self.assertAlmostEqual(float(cyl.get("radius")) * 2000.0, 320.0, places=6)
        self.assertAlmostEqual(float(cyl.get("length")) * 1000.0, 12.0, places=6)

    def test_the_base_geometry_is_the_clearance_scene_and_has_not_drifted(self):
        mount = np.array([cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y, cell_lc.AXIS_Z])
        boxes = {n: (mn, mx) for n, mn, mx in
                 cell_lc.lc_scene(mount, training_block=False).boxes}
        link = [ln for ln in self._root().findall("link")
                if ln.get("name") == station.STATION_LINK_NAMES[0]][0]
        got = sorted(tuple(round(float(v) * 1000.0, 4) for v in
                           b.find("geometry/box").get("size").split())
                     for b in link.findall("collision"))
        want = sorted(tuple(round(float(v), 4) for v in (mx - mn))
                      for name, (mn, mx) in boxes.items()
                      if name in ("mh185_headstock", "positioner_stand"))
        self.assertEqual(got, want)

    def test_charles_plate_is_selectable_and_none_is_empty(self):
        charles = self._root(platter="charles")
        plate = [ln for ln in charles.findall("link")
                 if ln.get("name") == station.STATION_LINK_NAMES[2]][0]
        self.assertEqual(len(plate.findall("collision")), 2)
        bare = self._root(platter="none")
        empty = [ln for ln in bare.findall("link")
                 if ln.get("name") == station.STATION_LINK_NAMES[2]][0]
        self.assertEqual(len(empty.findall("collision")), 0)

    def test_a_bad_platter_or_sense_is_refused(self):
        with self.assertRaises(ValueError):
            station.urdf_fragment(platter="whatever")
        with self.assertRaises(ValueError):
            station.urdf_fragment(sense=0)
