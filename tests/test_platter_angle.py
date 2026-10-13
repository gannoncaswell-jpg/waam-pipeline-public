import copy
import sys
import unittest
from itertools import product
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, station
from waam_pipeline.clearance import LC_PART_FIXED
from waam_pipeline.gcode import parse
from waam_pipeline.slicer import wall


def _boxes(scene):
    return {name: np.array([lo, hi]) for name, lo, hi in scene.boxes}


def _flat_path():
    return parse("G90\nG0 X-20 Y-10 Z7.5\nG1 X20 E1\nG1 Y10 E2\nG1 X-20 E3\n")


class TestPlatterWorkPlane(unittest.TestCase):
    def test_home_is_exactly_the_current_face(self):
        for point, normal in (station.platter_work_plane(),
                              station.platter_work_plane(0.0)):
            np.testing.assert_array_equal(point, cell_lc.DEFAULT_PART_ORIGIN)
            self.assertEqual(point[2], cell_lc.PLATTER_FACE_Z)
            np.testing.assert_array_equal(normal, [0.0, 0.0, 1.0])

    def test_face_point_follows_the_measured_axis_circle(self):
        home, _ = station.platter_work_plane()
        axis, direction = station.axis_origin(), station.axis_direction(+1)
        offset = home - axis
        radial = offset - np.dot(offset, direction) * direction
        for angle in (-70.0, 45.0, 135.0):
            with self.subTest(angle=angle):
                point, _ = station.platter_work_plane(angle)
                moved = point - axis
                self.assertAlmostEqual(np.dot(moved, direction),
                                       np.dot(offset, direction), places=10)
                moved -= np.dot(moved, direction) * direction
                self.assertAlmostEqual(np.linalg.norm(moved),
                                       np.linalg.norm(radial), places=10)
                swept = np.degrees(np.arctan2(
                    np.dot(direction, np.cross(radial, moved)),
                    np.dot(radial, moved)))
                self.assertAlmostEqual(swept, angle * station.STATION_SENSE, places=10)

    def test_unit_normal_tilts_in_the_confirmed_sense_even_when_inverted(self):
        for angle in (-135.0, -30.0, 30.0, 90.0, 180.0):
            with self.subTest(angle=angle):
                _, normal = station.platter_work_plane(angle)
                self.assertAlmostEqual(np.linalg.norm(normal), 1.0, places=12)
                self.assertAlmostEqual(
                    np.degrees(np.arccos(np.clip(normal[2], -1, 1))),
                    abs(angle), places=10)
                theta = np.radians(angle * station.STATION_SENSE)
                np.testing.assert_allclose(normal, [np.sin(theta), 0, np.cos(theta)],
                                           atol=1e-12)

    def test_calibration_moves_plane_scene_and_seat_together(self):
        tp, angle, shift = _flat_path(), 37.0, 7.5

        def state():
            point, normal = station.platter_work_plane(angle)
            bounds = _boxes(cell_lc.lc_scene(np.zeros(3), station_deg=angle))
            pose = cell_lc.snap_toolpath_to_platter_pose(
                tp, 1014.0, -180.0, station_deg=angle)
            return point, bounds["charles_platter"].mean(0), pose[:3, 3], normal

        before = state()
        with mock.patch.object(cell_lc, "PLATTER_FACE_Z", cell_lc.PLATTER_FACE_Z + shift):
            after = state()
        for old, new in zip(before[:3], after[:3]):
            np.testing.assert_allclose(new - old, shift * before[3], atol=1e-10)
        np.testing.assert_array_equal(before[3], after[3])


class TestStationScene(unittest.TestCase):
    def test_zero_is_exactly_the_existing_scene(self):
        for origin in (None, np.zeros(3), np.array([1037.5, -215.75, 130.0])):
            for block in (None, False, True):
                with self.subTest(origin=origin, block=block):
                    home = cell_lc.lc_scene(origin, training_block=block)
                    zero = cell_lc.lc_scene(origin, training_block=block, station_deg=0.0)
                    self.assertEqual(home.part_fixed, zero.part_fixed)
                    self.assertEqual(home.capsules, zero.capsules)
                    self.assertEqual([b[0] for b in home.boxes], [b[0] for b in zero.boxes])
                    for a, b in zip(home.boxes, zero.boxes):
                        np.testing.assert_array_equal(a[1:], b[1:])

    def test_rotating_obstacles_bound_all_eight_rotated_corners(self):
        origin = np.array([1037.5, -215.75, 130.0])
        home = _boxes(cell_lc.lc_scene(origin, training_block=True))
        for angle in (-41.0, 37.0, 90.0, 180.0):
            posed = _boxes(cell_lc.lc_scene(origin, training_block=True, station_deg=angle))
            for name in LC_PART_FIXED:
                with self.subTest(angle=angle, obstacle=name):
                    lo, hi = home[name] + origin
                    corners = np.array(list(product(*zip(lo, hi))))
                    moved = station.apply(corners, angle)
                    np.testing.assert_allclose(posed[name] + origin,
                                               [moved.min(0), moved.max(0)], atol=1e-9)
                    np.testing.assert_allclose(posed[name].mean(0) + origin,
                                               station.apply((lo + hi) / 2, angle), atol=1e-9)

    def test_platter_top_and_block_base_follow_the_work_plane(self):
        for angle in (-45.0, 30.0, 90.0, 180.0):
            with self.subTest(angle=angle):
                point, normal = station.platter_work_plane(angle)
                boxes = _boxes(cell_lc.lc_scene(
                    np.zeros(3), training_block=True, station_deg=angle))
                plate_face = boxes["charles_platter"].mean(0) + normal * cell_lc.CHARLES_PLATE[2] / 2
                block_base = boxes["manpro_training_block"].mean(0) - normal * cell_lc.TRAINING_BLOCK[2] / 2
                self.assertAlmostEqual(np.dot(plate_face - point, normal), 0.0, places=9)
                self.assertAlmostEqual(np.dot(block_base - point, normal), 0.0, places=9)

    def test_robot_and_fixed_obstacles_do_not_move(self):
        home = _boxes(cell_lc.lc_scene(training_block=True))
        fixed = home.keys() - set(LC_PART_FIXED)
        self.assertTrue({"mh185_headstock", "positioner_stand", "robot_pedestal",
                         "floor", "reamer_yair_185091"} <= fixed)
        for angle in (-90.0, 37.0, 180.0):
            posed = _boxes(cell_lc.lc_scene(training_block=True, station_deg=angle))
            self.assertEqual(posed.keys(), home.keys())
            for name in fixed:
                with self.subTest(angle=angle, obstacle=name):
                    np.testing.assert_array_equal(posed[name], home[name])

    def test_origin_only_translates_the_posed_scene(self):
        origin = np.array([1075.0, -123.0, 92.0])
        base = _boxes(cell_lc.lc_scene(np.zeros(3), training_block=True, station_deg=37.0))
        part = _boxes(cell_lc.lc_scene(origin, training_block=True, station_deg=37.0))
        for name in base:
            np.testing.assert_allclose(part[name] + origin, base[name], atol=1e-10)

    def test_training_block_presence_still_controls_only_the_block(self):
        without = _boxes(cell_lc.lc_scene(training_block=False, station_deg=37.0))
        with_block = _boxes(cell_lc.lc_scene(training_block=True, station_deg=37.0))
        self.assertEqual(with_block.keys() - without.keys(), {"manpro_training_block"})
        for name in without:
            np.testing.assert_array_equal(without[name], with_block[name])


class TestPlatterSeatingPose(unittest.TestCase):
    def test_zero_pose_reproduces_the_existing_snap_exactly(self):
        tp = _flat_path()
        for substrate, gap in ((0.0, 0.0), (6.35, 1.5)):
            expected = np.eye(4)
            expected[:3, 3] = cell_lc.snap_toolpath_to_platter(
                tp, 1014.0, -180.0, substrate_mm=substrate, gap_mm=gap)
            for angle_kw in ({}, {"station_deg": 0.0}):
                pose = cell_lc.snap_toolpath_to_platter_pose(
                    tp, 1014.0, -180.0, substrate_mm=substrate, gap_mm=gap, **angle_kw)
                np.testing.assert_array_equal(pose, expected)

    def test_planar_weld_tcp_lies_on_the_tilted_work_plane(self):
        tp = _flat_path()
        points = np.array([m.target for m in tp.moves])
        for angle in (-37.0, 30.0, 90.0, 180.0):
            with self.subTest(angle=angle):
                pose = cell_lc.snap_toolpath_to_platter_pose(
                    tp, 1014.0, -180.0, station_deg=angle)
                point, normal = station.platter_work_plane(angle)
                placed = points @ pose[:3, :3].T + pose[:3, 3]
                np.testing.assert_allclose((placed - point) @ normal, 0.0, atol=1e-10)

    def test_layers_substrate_and_gap_follow_the_normal(self):
        tp = wall(40.0, 3, 2.0)
        points = np.array([m.target for m in tp.moves])
        low = min(p[2] for p in tp.deposit_points())
        for angle in (-45.0, 90.0, 135.0):
            pose = cell_lc.snap_toolpath_to_platter_pose(
                tp, 1014.0, -180.0, station_deg=angle, substrate_mm=6.35, gap_mm=1.5)
            point, normal = station.platter_work_plane(angle)
            placed = points @ pose[:3, :3].T + pose[:3, 3]
            np.testing.assert_allclose((placed - point) @ normal,
                                       points[:, 2] - low + 6.35 + 1.5, atol=1e-10)

    def test_seating_includes_the_weld_start_and_ignores_unrelated_travel(self):
        tp = parse("G90\nG0 X0 Y0 Z5\nG1 X10 Z9 E1\nG0 X20 Z-20\n")
        pose = cell_lc.snap_toolpath_to_platter_pose(tp, 1014.0, -180.0, station_deg=37.0)
        point, normal = station.platter_work_plane(37.0)
        deposit = np.array(tp.deposit_points()) @ pose[:3, :3].T + pose[:3, 3]
        np.testing.assert_allclose((deposit - point) @ normal, [0.0, 4.0], atol=1e-10)

    def test_pose_preserves_geometry_and_does_not_mutate_the_toolpath(self):
        tp = _flat_path()
        tp.unhandled.append((1, "planning metadata"))
        before = copy.deepcopy(tp)
        pose = cell_lc.snap_toolpath_to_platter_pose(tp, 1014.0, -180.0, station_deg=37.0)
        self.assertEqual(tp, before)
        points = np.array([m.target for m in tp.moves])
        placed = points @ pose[:3, :3].T + pose[:3, 3]
        np.testing.assert_allclose(np.linalg.norm(np.diff(placed, axis=0), axis=1),
                                   np.linalg.norm(np.diff(points, axis=0), axis=1), atol=1e-10)

    def test_pose_and_scene_share_the_translated_robot_frame(self):
        tp, angle = _flat_path(), 37.0
        pose = cell_lc.snap_toolpath_to_platter_pose(tp, 1014.0, -180.0, station_deg=angle)
        scene = cell_lc.lc_scene(pose[:3, 3], training_block=True, station_deg=angle)
        point, normal = station.platter_work_plane(angle)
        part_points = np.array([m.target for m in tp.moves]) @ pose[:3, :3].T
        plane_point = point - pose[:3, 3]
        np.testing.assert_allclose((part_points - plane_point) @ normal, 0.0, atol=1e-10)
        plate_center = _boxes(scene)["charles_platter"].mean(0)
        self.assertAlmostEqual(np.dot(plate_center - plane_point, normal),
                               -cell_lc.CHARLES_PLATE[2] / 2, places=9)

    def test_invalid_seating_inputs_are_refused(self):
        for tp in (parse(""), parse("G90\nG0 X0 Y0 Z5\n")):
            with self.assertRaisesRegex(ValueError, "no deposit"):
                cell_lc.snap_toolpath_to_platter_pose(tp, 1014.0, -180.0, station_deg=37.0)
        with self.assertRaisesRegex(ValueError, "substrate_mm"):
            cell_lc.snap_toolpath_to_platter_pose(_flat_path(), 1014.0, -180.0,
                                                 station_deg=37.0, substrate_mm=-1.0)
        for kwargs in ({"x": np.inf}, {"y": np.nan}, {"gap_mm": np.inf},
                       {"substrate_mm": np.nan}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "finite"):
                args = {"x": 1014.0, "y": -180.0, **kwargs}
                cell_lc.snap_toolpath_to_platter_pose(_flat_path(), station_deg=37.0, **args)


class TestPlanningAngleGuards(unittest.TestCase):
    def calls(self, angle, sense=station.STATION_SENSE):
        return (
            lambda: station.platter_work_plane(angle, sense=sense),
            lambda: cell_lc.lc_scene(station_deg=angle, station_sense=sense),
            lambda: cell_lc.snap_toolpath_to_platter_pose(
                _flat_path(), 1014.0, -180.0, station_deg=angle, station_sense=sense),
        )

    def test_outside_soft_limits_is_refused_without_wrapping(self):
        for angle in (-station.SOFT_LIMIT_DEG - 0.001, station.SOFT_LIMIT_DEG + 0.001, 720.0):
            for call in self.calls(angle):
                with self.subTest(angle=angle), self.assertRaisesRegex(ValueError, "station_deg"):
                    call()

    def test_nonfinite_angles_are_refused(self):
        for angle in (np.nan, np.inf, -np.inf):
            for call in self.calls(angle):
                with self.subTest(angle=angle), self.assertRaisesRegex(ValueError, "finite"):
                    call()

    def test_both_soft_limit_endpoints_are_allowed(self):
        for angle in (-station.SOFT_LIMIT_DEG, station.SOFT_LIMIT_DEG):
            for call in self.calls(angle):
                call()
            point, normal = station.platter_work_plane(angle)
            np.testing.assert_allclose(point, cell_lc.DEFAULT_PART_ORIGIN, atol=1e-9)
            np.testing.assert_allclose(normal, [0.0, 0.0, 1.0], atol=1e-12)

    def test_invalid_sense_is_refused(self):
        for sense in (0, 2, 0.5):
            for call in self.calls(0.0, sense):
                with self.subTest(sense=sense), self.assertRaisesRegex(ValueError, "sense"):
                    call()

    def test_sense_override_reverses_all_three_models_consistently(self):
        reverse = [call() for call in self.calls(37.0, -1)]
        negative = [call() for call in self.calls(-37.0, +1)]
        np.testing.assert_allclose(reverse[0], negative[0], atol=1e-10)
        for name, bounds in _boxes(reverse[1]).items():
            np.testing.assert_allclose(bounds, _boxes(negative[1])[name], atol=1e-10)
        np.testing.assert_allclose(reverse[2], negative[2], atol=1e-10)


if __name__ == "__main__":
    unittest.main()
