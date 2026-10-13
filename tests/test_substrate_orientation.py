import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, demos
from waam_pipeline.clearance import Scene, TorchStackup
from waam_pipeline.robots import ar1440
from waam_pipeline.substrate import (Substrate, from_request, _normal_gap_projection,
                                     MAX_RESIDUAL_MM, V1_MAX_TORCH_TILT_DEG)


AXIS = cell_lc._rot((0, 1, 0), -45) @ [0, 0, -1]
UP = np.array([0.0, 0.0, 1.0])


def rotation(x=0, y=0, yaw=-10):
    return (cell_lc._rot((1, 0, 0), x) @ cell_lc._rot((0, 1, 0), y)
            @ cell_lc._rot((0, 0, 1), yaw) @ cell_lc._rot((0, 1, 0), 45))


def pulses_at_tcp(tcp, attitude):
    pose = np.eye(4)
    pose[:3, :3] = attitude
    pose[:3, 3] = tcp - attitude @ cell_lc.TOOL_TCP
    q = ar1440.ik(pose)
    if q is None:
        raise AssertionError("Synthetic capture must be reachable")
    return np.rint(np.asarray(q) * ar1440.PULSES_PER_DEG).astype(int).tolist()


def physical_registration(direction="normal", offset=20.0, tilt=3.5, captures=None,
                          center=(1014, -180, 175)):

    attitude = cell_lc._rot((0, 0, 1), 20) @ cell_lc._rot((0, 1, 0), tilt)
    corners = np.array([[-80, -40, 0], [80, -40, 0], [80, 40, 0], [-80, 40, 0]])
    corners = corners @ attitude.T + center
    normal = attitude @ UP
    captures = captures if captures is not None else ((3, 2), (-4, 3), (5, -4), (-2, -5))
    records = []
    for corner, gap, (x, y) in zip(corners, (6, 12, 9, 12), captures):
        r = rotation(x, y)
        down = r @ AXIS

        if direction == "normal":
            contact = corner + gap * normal
        elif direction == "vertical":
            contact = corner + gap * UP
        else:
            contact = corner - gap * down
        registered_tcp = contact + offset * down
        records.append({"pulses": pulses_at_tcp(registered_tcp, r), "tip_distance_mm": gap})
    return {
        "version": 2, "frame": "lc50s_tool0_pulse", "gap_direction": direction,
        "tool0_tip_offset_mm": offset, "weld_ctwd_mm": TorchStackup.from_profile().ctwd_mm,
        "corners": records,
    }, corners, normal


class TestTiltedSubstrate(unittest.TestCase):
    def test_actual_tip_measurements_reconstruct_each_physical_corner(self):
        for direction in ("normal", "vertical", "torch"):
            for offset in (-8, 0, 20):
                with self.subTest(direction=direction, offset=offset):
                    data, physical, normal = physical_registration(direction, offset)
                    plate = from_request(json.dumps(data), 6.35)
                    weld = data["weld_ctwd_mm"]


                    reconstructed = plate.points + (offset - weld) * UP
                    np.testing.assert_allclose(reconstructed, physical, atol=.035)
                    np.testing.assert_allclose(plate.basis[:, 2], normal, atol=.0005)
                    self.assertLess(plate.residual, .035)
                    self.assertEqual(plate.metadata()["gap_direction"], direction)
                    self.assertGreater(max(plate.metadata()["torch_tilt_deg"]), 5)
                    self.assertEqual(plate.metadata()["tool0_tip_offset_mm"], offset)

    def test_normal_ruler_fit_handles_varying_gaps_and_noise(self):
        data, physical, normal = physical_registration()
        gaps = np.array([1, 35, 90, 7])
        contact = physical + gaps[:, None] * normal
        projected = _normal_gap_projection(contact, gaps)
        np.testing.assert_allclose(projected, physical, atol=1e-9)
        contact[2] += .06 * normal
        projected = _normal_gap_projection(contact, gaps)
        from waam_pipeline.touch import fit_plane
        fit = fit_plane(projected)
        measured = (contact - fit.point) @ fit.normal
        self.assertLess(np.max(np.abs(measured - gaps)), .03)

    def test_legacy_records_are_not_reinterpreted(self):
        data, _, _ = physical_registration("vertical", 20, tilt=0, captures=[(0, 0)] * 4)
        legacy = dict(data, version=1)
        modern = from_request(json.dumps(data), 6)
        old = from_request(json.dumps(legacy), 6)
        np.testing.assert_allclose(old.points, modern.points, atol=.005)
        self.assertNotIn("gap_direction", old.metadata())
        data, _, _ = physical_registration()
        data["version"] = 1
        with self.assertRaisesRegex(ValueError, "vertical"):
            from_request(json.dumps(data), 6)

    def test_v1_locates_a_hand_jogged_plate_off_vertical(self):


        for tilt_deg in (1.5, 2.5):
            data, _, _ = physical_registration("vertical", 20, tilt=0,
                                               captures=[(tilt_deg, 0)] * 4)
            data["version"] = 1
            plate = from_request(json.dumps(data), 6)
            self.assertIsInstance(plate, Substrate)
            self.assertLess(plate.residual, MAX_RESIDUAL_MM)

        data, _, _ = physical_registration("vertical", 20, tilt=0,
                                           captures=[(V1_MAX_TORCH_TILT_DEG + 1, 0)] * 4)
        data["version"] = 1
        with self.assertRaisesRegex(ValueError, "off vertical"):
            from_request(json.dumps(data), 6)

    def test_physical_solid_and_command_contact_datum_are_separate(self):
        data, physical, normal = physical_registration(offset=20)
        plate = from_request(json.dumps(data), 6.35)
        mesh = plate.mesh().reshape(-1, 3)
        heights = (mesh - physical.mean(axis=0)) @ normal
        self.assertAlmostEqual(float(heights.max()), 0, delta=.035)
        self.assertAlmostEqual(float(heights.min()), -6.35, delta=.035)
        top = plate.origin + plate.physical_shift_robot
        self.assertLess(float(plate.distance([top - normal])[0]), -.95)
        self.assertAlmostEqual(float(plate.distance([plate.origin], contact=True)[0]), 0)
        self.assertLess(float(plate.distance([plate.origin - normal], contact=True)[0]), -.95)
        np.testing.assert_allclose(plate.metadata()["points_robot_mm"], physical, atol=.035)
        np.testing.assert_allclose(plate.metadata()["command_origin_robot_mm"], plate.origin)
        placed = plate.place(demos.demo_bead(), bottom=0)
        for point in placed.deposit_points():


            self.assertAlmostEqual(float((np.asarray(point) - 2 * UP) @ plate.basis[:, 2]), 0)

    def test_pybullet_mesh_uses_the_physical_plate(self):
        sys.path.insert(0, str(ROOT))
        from sim.pybullet_driver import scene_body_specs
        data, physical, normal = physical_registration(offset=20)
        plate = from_request(json.dumps(data), 6.35)
        specs = scene_body_specs(Scene(substrates=[plate.shifted(plate.origin)]), plate.origin)
        actual = np.asarray(specs[0].vertices) + specs[0].position
        expected = np.unique(plate.mesh().reshape(-1, 3), axis=0) / 1000
        np.testing.assert_allclose(actual, expected, atol=1e-12)

    def test_missing_or_invalid_calibration_and_basis_are_refused(self):
        good, _, _ = physical_registration()
        for key, values in {
            "tool0_tip_offset_mm": (None, True, -101, 101, float("nan"), "20"),
            "gap_direction": (None, "", "auto", {}, True),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    from_request(json.dumps(dict(good, **{key: value})), 6)

    def test_flatness_and_plate_tilt_refusals_remain(self):
        data, _, _ = physical_registration()
        data["corners"][0]["tip_distance_mm"] += 5
        with self.assertRaisesRegex(ValueError, "fitted plane"):
            from_request(json.dumps(data), 6)
        data, _, _ = physical_registration(tilt=6)
        with self.assertRaisesRegex(ValueError, "5 degrees"):
            from_request(json.dumps(data), 6)

    def test_torch_pointing_away_from_surface_is_refused(self):
        data, _, _ = physical_registration("normal", captures=[(95, 0)] * 4,
                                            center=(300, -180, 300))
        with self.assertRaisesRegex(ValueError, "points away"):
            from_request(json.dumps(data), 6)


class TestSurfaceSetupMetadata(unittest.TestCase):
    @staticmethod
    def fitted_plate(residual=0):
        attitude = cell_lc._rot((0, 0, 1), 20) @ cell_lc._rot((0, 1, 0), 3)
        points = np.array([[-80, -40, 0], [80, -40, 0], [80, 40, 0], [-80, 40, 0]])
        projected = points @ attitude.T + [1000, -200, 175]
        readings = projected + np.array([1, -1, 1, -1])[:, None] * residual * (attitude @ UP)
        return Substrate.fit(readings, 6), projected

    def test_height_range_and_xy_coverage_come_from_tilted_fitted_perimeter(self):
        plate, projected = self.fitted_plate()
        setup = plate.setup_metadata(150, [970, -300])
        self.assertEqual(setup["reference"], "registered_platter_at_s1_home")
        self.assertEqual(setup["reference_command_z_robot_mm"], 150)
        self.assertEqual(setup["reference_center_xy_robot_mm"], [970, -300])
        np.testing.assert_allclose(setup["center_xy_from_platter_center_mm"], [30, 100])
        self.assertAlmostEqual(setup["center_height_above_platter_mm"], 25)
        half_rise = 80 * np.sin(np.radians(3))
        np.testing.assert_allclose(setup["height_range_above_platter_mm"], [25-half_rise, 25+half_rise])
        self.assertAlmostEqual(setup["height_variation_mm"], 2 * half_rise)
        bounds = setup["bounds_xy_from_platter_center_mm"]
        np.testing.assert_allclose(bounds["min"], projected[:, :2].min(0) - [970, -300])
        np.testing.assert_allclose(bounds["max"], projected[:, :2].max(0) - [970, -300])

    def test_measurement_residuals_do_not_expand_reported_fitted_bounds(self):
        exact, projected = self.fitted_plate()
        noisy, _ = self.fitted_plate(residual=.2)
        self.assertAlmostEqual(noisy.residual, .2)
        setup = noisy.setup_metadata(150, [970, -300])
        clean = exact.setup_metadata(150, [970, -300])
        np.testing.assert_allclose(setup["height_range_above_platter_mm"], clean["height_range_above_platter_mm"])
        for side in ("min", "max"):
            np.testing.assert_allclose(setup["bounds_xy_from_platter_center_mm"][side],
                                       clean["bounds_xy_from_platter_center_mm"][side])
        self.assertGreater(np.max(np.abs(noisy.points[:, :2] - projected[:, :2])), .005)

    def test_common_command_datum_shift_does_not_change_relative_setup(self):
        plate, _ = self.fitted_plate()
        expected = plate.setup_metadata(150, [970, -300])


        delta = np.array([0, 0, 12.5])
        shifted = replace(plate, origin=plate.origin + delta,
                          points=plate.points + delta, physical_shift_robot=tuple(-delta))
        actual = shifted.setup_metadata(162.5, [970, -300])
        for key in ("center_height_above_platter_mm", "height_variation_mm"):
            self.assertAlmostEqual(actual[key], expected[key])
        for key in ("center_xy_from_platter_center_mm", "height_range_above_platter_mm"):
            np.testing.assert_allclose(actual[key], expected[key])
        self.assertEqual(actual["bounds_xy_from_platter_center_mm"], expected["bounds_xy_from_platter_center_mm"])
        self.assertEqual(actual["reference_command_z_robot_mm"], 162.5)

    def test_reference_and_plate_translation_preserves_relative_setup(self):
        plate, _ = self.fitted_plate()
        expected = plate.setup_metadata(150, [970, -300])
        offset = np.array([11, -7, 30])
        translated = plate.shifted(-offset)
        actual = translated.setup_metadata(180, [981, -307])
        np.testing.assert_allclose(actual["height_range_above_platter_mm"], expected["height_range_above_platter_mm"])
        np.testing.assert_allclose(actual["center_xy_from_platter_center_mm"], expected["center_xy_from_platter_center_mm"])
        self.assertEqual(actual["bounds_xy_from_platter_center_mm"], expected["bounds_xy_from_platter_center_mm"])

    def test_server_exposes_setup_relative_to_current_platter_reference(self):
        from unittest.mock import patch
        from waam_pipeline import server
        data, physical, _ = physical_registration(offset=20)
        with patch.object(server, "_STATES", {}), patch.object(server, "_SHOW_CELL", True), patch.object(server, "_HOSTED_ASSETS", False):
            result = server.run_slice({"kind": "wall", "shape": "bead",
                                       "substrate_registration": json.dumps(data)}, None, workspace="setup-metadata-test")
        setup = result["state"]["meta"]["substrate"]["surface_setup"]
        self.assertEqual(setup["reference_command_z_robot_mm"], cell_lc.PLATTER_FACE_Z)
        self.assertEqual(setup["reference_center_xy_robot_mm"],
                         [cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y + cell_lc.CHARLES_PLATE[0] / 2])
        expected = physical.mean(0)[2] + data["weld_ctwd_mm"] - 20 - cell_lc.PLATTER_FACE_Z
        self.assertAlmostEqual(setup["center_height_above_platter_mm"], expected, delta=.035)


if __name__ == "__main__":
    unittest.main()
