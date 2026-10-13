from dataclasses import asdict, replace
from fractions import Fraction
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, demos, sensed, slicer
from waam_pipeline.substrate import MAX_TILT_DEG, Substrate, from_request
from waam_pipeline.toolpath import Move, Toolpath
from waam_pipeline.touch import SEARCH_OVERTRAVEL_MM
from tests.test_sensed_surface import pulse_records, registration


CENTER = np.array([1014.0, -180.0, 160.0])
SHIFT = np.array([0.0, 0.0, -6.0])


def corner_plate(*, slope=(0.0, 0.0), corner_errors=None):
    xy = np.array([[-100, -60], [100, -60], [100, 60], [-100, 60]], float)
    z = xy @ np.asarray(slope)
    if corner_errors is not None:
        z += corner_errors
    physical = np.column_stack((xy, z)) + CENTER
    return replace(Substrate.fit(physical - SHIFT, 6.35), gap_direction="torch",
                   tool0_tip_offset_mm=8.92, physical_shift_robot=tuple(SHIFT))


def fit_points(points, *, plate=None, **kwargs):
    plate = corner_plate() if plate is None else plate
    samples = np.asarray(points, float) + CENTER
    records = [[i, 0, 0, 0, 0, 0] for i in range(len(samples))]
    with patch.object(cell_lc, "tcp_from_pulses", side_effect=list(samples.copy())):
        return sensed.fit_sensed(plate, records, base=44, **kwargs)


def bump_plate():
    return fit_points([[-65, 0, .1], [-30, 0, .3], [0, 0, 1.2],
                       [30, 0, .4], [65, 0, .1]], fit="interpolate")


class TestInterpolatedSurface(unittest.TestCase):
    def test_exact_at_every_physical_corner_and_touch(self):
        plate = corner_plate(slope=(.012, .007), corner_errors=[.12, -.12, .12, -.12])
        samples = [[-40, -20, -.3], [0, 0, 1.2], [45, 25, .7]]
        fitted = fit_points(samples, plate=plate, fit="interpolate")
        vertices = np.vstack((plate.points + SHIFT, np.asarray(samples) + CENTER))
        np.testing.assert_allclose(fitted.surface(vertices[:, 0], vertices[:, 1]),
                                   vertices[:, 2], atol=2e-10)
        np.testing.assert_array_equal(fitted._tin_xyz, vertices)
        self.assertEqual(set(fitted._tin_tri.ravel()), set(range(len(vertices))))
        self.assertEqual(fitted.metadata()["sensed"]["fit"], "interpolated")

    def test_barycentric_height_and_gradient_are_linear_inside_each_facet(self):
        plate = bump_plate()
        slopes = []
        for indices in plate._tin_tri:
            xyz = plate._tin_xyz[indices]
            weights = np.array([[.2, .3, .5], [.6, .1, .3], [1 / 3] * 3])
            expected = weights @ xyz
            np.testing.assert_allclose(plate.surface(expected[:, 0], expected[:, 1]),
                                       expected[:, 2], atol=2e-10)
            coefficients = np.linalg.solve(np.column_stack((xyz[:, :2], np.ones(3))), xyz[:, 2])
            np.testing.assert_allclose(plate._gradient(expected[:, :2]),
                                       np.tile(coefficients[:2], (len(weights), 1)), atol=2e-10)
            slopes.append(np.degrees(np.arctan(np.linalg.norm(coefficients[:2]))))
        self.assertAlmostEqual(plate.metadata()["sensed"]["max_facet_slope_deg"], max(slopes), places=9)
        self.assertAlmostEqual(plate.tilt, max(slopes), places=9)
        self.assertLess(plate.tilt, MAX_TILT_DEG)

    def test_triangles_have_empty_circumcircles_and_cover_the_plate(self):
        plate = fit_points([[-70, -35, .1], [-35, 25, .4], [0, -20, .6],
                            [25, 30, .2], [65, -25, .3]], fit="interpolate")
        xy = plate._tin_xyz[:, :2] - CENTER[:2]
        area = 0.0
        for indices in plate._tin_tri:
            a, b, c = xy[indices]
            edges = np.vstack((b - a, c - a))
            area += abs(np.linalg.det(edges)) / 2
            center = np.linalg.solve(2 * edges, [b @ b - a @ a, c @ c - a @ a])
            radius_squared = np.sum((a - center) ** 2)
            self.assertTrue(np.all(np.sum((xy - center) ** 2, axis=1) >= radius_squared - 1e-7))
        self.assertAlmostEqual(area, 200 * 120, places=7)

    def test_full_hull_with_near_edge_grid_collinear_and_boundary_stations(self):
        stations = [np.random.default_rng(61).uniform([-99, -59], [99, 59], (25, 2)),
                    np.array([[x, y] for y in (-40, 0, 40) for x in (-80, -40, 0, 40, 80)]),
                    np.column_stack((np.linspace(-90, 90, 25), np.zeros(25))),
                    np.column_stack((np.linspace(-90, 90, 25), np.full(25, -60)))]
        for i, xy in enumerate(stations):
            with self.subTest(layout=i):
                plate = fit_points(np.column_stack((xy, np.zeros(len(xy)))), fit="interpolate")
                triangles = plate._tin_xyz[plate._tin_tri, :2]
                area = abs(np.linalg.det(triangles[:, 1:] - triangles[:, :1])).sum() / 2
                self.assertAlmostEqual(area, 200 * 120, places=7)
                self.assertEqual(set(plate._tin_tri.ravel()), set(range(4 + len(xy))))
                for edge in (np.column_stack((np.linspace(-100, 100, 101), np.full(101, -60))),
                             np.column_stack((np.full(101, 100), np.linspace(-60, 60, 101)))):
                    world = edge + CENTER[:2]
                    np.testing.assert_allclose(plate.surface(world[:, 0], world[:, 1]), CENTER[2], atol=1e-10)

    def test_default_and_explicit_smooth_have_identical_outputs(self):
        points = [[x, y, 2e-5 * (x * x - 10000)]
                  for x, y in ((-60, -30), (60, -30), (-60, 30), (60, 30), (0, -25), (0, 25))]
        for count in (2, 6):
            with self.subTest(touches=count):
                implicit = fit_points(points[:count])
                explicit = fit_points(points[:count], fit="smooth")
                self.assertEqual(json.dumps(implicit.metadata()), json.dumps(explicit.metadata()))
                self.assertEqual(implicit.mesh().tobytes(), explicit.mesh().tobytes())
                for field in ("origin", "_coefficients", "_samples", "_tin_xyz", "_tin_tri"):
                    self.assertEqual(getattr(implicit, field).tobytes(), getattr(explicit, field).tobytes())
                path = slicer.wall(100, 2, 2.0)
                self.assertEqual(asdict(implicit.place(path)), asdict(explicit.place(path)))
                self.assertEqual(explicit._tin_tri.size, 0)
                self.assertEqual(explicit._tin_xyz.size, 0)

    def test_smooth_deviations_are_reported_without_refusing_interpolation(self):
        points = [[0, 0, 2.0], [60, 25, 0.0]]
        plate = fit_points(points, fit="interpolate")
        vertices = plate._tin_xyz
        design = np.column_stack((np.ones(len(vertices)), vertices[:, :2] - CENTER[:2]))
        coefficients, *_ = np.linalg.lstsq(design, vertices[:, 2], rcond=None)
        errors = vertices[4:, 2] - design[4:] @ coefficients
        info = plate.metadata()["sensed"]
        self.assertAlmostEqual(info["rms_residual_mm"], np.sqrt(np.mean(errors ** 2)), places=9)
        self.assertAlmostEqual(info["max_residual_mm"], np.max(np.abs(errors)), places=9)
        self.assertEqual(info["worst"], "P044")
        self.assertGreater(info["max_residual_mm"], .5)
        with self.assertRaisesRegex(ValueError, "P044"):
            fit_points(points, fit="smooth")

    def test_high_spot_is_judged_by_the_all_points_smooth_fit(self):


        points = [[0, 0, 2], [60, 25, 0], [-60, -25, 0],
                  [-60, 25, 0], [60, -25, 0], [0, 40, .1]]
        with self.assertRaisesRegex(ValueError, "P049 differs from the smooth fit by -0.5"):
            fit_points(points, fit="interpolate")
        points[0][2] = 1.2
        plate = fit_points(points, fit="interpolate")
        xyz = plate._tin_xyz
        x, y = (xyz[:, :2] - CENTER[:2]).T
        design = np.column_stack((np.ones(len(xyz)), x, y, x * x, x * y, y * y))
        coefficients, *_ = np.linalg.lstsq(design, xyz[:, 2], rcond=None)
        residuals = (xyz[:, 2] - design @ coefficients)[4:]
        info = plate.metadata()["sensed"]
        self.assertAlmostEqual(info["max_residual_mm"], np.max(np.abs(residuals)), places=8)
        self.assertAlmostEqual(info["rms_residual_mm"], np.sqrt(np.mean(residuals ** 2)), places=8)
        self.assertEqual(info["worst"], f"P{44 + int(np.argmax(abs(residuals))):03d}")

    def test_wall_follows_bump_without_crown_lift_and_layers_keep_spacing(self):
        plate = bump_plate()
        path = slicer.wall(160, 2, 2.0)
        placed = plate.place(path)
        self.assertGreater(len(placed.moves), len(path.moves))
        local_plate = plate.shifted(plate.origin)
        for prev, move in zip(placed.moves, placed.moves[1:]):
            if not move.deposit:
                continue
            a, b = np.array(prev.target), np.array(move.target)
            points = a + np.linspace(0, 1, 17)[:, None] * (b - a)
            np.testing.assert_allclose(points[:, 2] - local_plate.datum(points[:, 0], points[:, 1]),
                                       2.0 * move.layer, atol=2e-10)
            self.assertAlmostEqual(move.z - move.seat, 2.0 * move.layer, places=10)
        for layer in (0, 1):
            apex = [m for m in placed.moves if m.layer == layer and abs(m.x) < 1e-9]
            self.assertEqual(len(apex), 1)
            world = plate.origin + apex[0].target
            self.assertAlmostEqual(world[2], CENTER[2] - SHIFT[2] + 1.2 + 2.0 * layer, places=10)
        by_layer = [{round(m.x, 9): m.z for m in placed.moves if m.layer == layer}
                    for layer in (0, 1)]
        self.assertEqual(set(by_layer[0]), set(by_layer[1]))
        for x in by_layer[0]:
            self.assertAlmostEqual(by_layer[1][x] - by_layer[0][x], 2.0, places=10)

    def test_splits_interpolate_source_z_and_seat_and_preserve_move_attributes(self):
        plate = bump_plate()
        path = Toolpath([Move(-80, 0, 5, None, False, 0, 10, "start", .2),
                         Move(80, 0, 8, 123, True, 0, 20, "ramp", .8),
                         Move(-80, 20, 11, 456, False, 1, 30, "travel", 1.1),
                         Move(-80, 20, 15, 789, False, 1, 40, "lift", 1.1)], [(99, "M999")])
        placed = plate.place(path, gap=.3)
        self.assertEqual(placed.unhandled, path.unhandled)
        self.assertEqual([event for event, _ in placed.arc_events()], [event for event, _ in path.arc_events()])
        for i, original in enumerate(path.moves):
            pieces = [move for move in placed.moves if move.line_no == original.line_no]
            if i in (0, 3):
                self.assertEqual(len(pieces), 1)
            else:
                self.assertGreater(len(pieces), 1)
            start = path.moves[max(0, i - 1)]
            ab = np.array(original.target[:2]) - start.target[:2]
            for move in pieces:
                local_xy = np.linalg.solve(plate.basis[:2, :2], np.array(move.target[:2]))
                t = float((local_xy - start.target[:2]) @ ab / (ab @ ab)) if ab @ ab else 1.0
                source_z = start.z + t * (original.z - start.z)
                source_seat = start.seat + t * (original.seat - start.seat)
                self.assertAlmostEqual(move.z - move.seat, source_z - 5.0 + .3 - source_seat, places=9)
                world = plate.origin + move.target
                self.assertAlmostEqual(world[2] - plate.datum(*world[:2]), source_z - 5.0 + .3, places=9)
                self.assertEqual((move.deposit, move.layer, move.feed, move.region),
                                 (original.deposit, original.layer, original.feed, original.region))

    def test_exterior_travel_chords_follow_the_clamped_surface_exactly(self):
        plate = fit_points([[-100, 0, .5], [0, 60, .5], [0, 0, 1]], fit="interpolate")
        path = Toolpath([Move(-50, 0, 2, None, False, 0, 1),
                         Move(50, 0, 2, 200, True, 0, 2),
                         Move(150, 100, 12, None, False, 0, 3),
                         Move(-150, 100, 12, None, False, 0, 4),
                         Move(-150, -100, 12, None, False, 0, 5)])
        placed = plate.place(path)
        local_plate = plate.shifted(plate.origin)
        for previous, move in zip(placed.moves, placed.moves[1:]):
            a, b = np.array(previous.target), np.array(move.target)
            samples = a + np.linspace(0, 1, 31)[:, None] * (b - a)
            original = path.moves[move.line_no - 1]
            source_start = path.moves[move.line_no - 2]
            delta = np.array(original.target[:2]) - source_start.target[:2]
            t = (samples[:, :2] - source_start.target[:2]) @ delta / (delta @ delta)
            expected_offset = source_start.z + t * (original.z - source_start.z) - 2.0
            np.testing.assert_allclose(samples[:, 2] - local_plate.datum(samples[:, 0], samples[:, 1]),
                                       expected_offset, atol=2e-10)
        top_travel = [m for m in placed.moves if m.line_no == 4]
        self.assertTrue(any(abs(m.x) < 1e-9 for m in top_travel))
        self.assertTrue(any(abs(m.x - 100) < 1e-9 for m in top_travel))
        self.assertTrue(any(abs(m.x + 100) < 1e-9 for m in top_travel))

    def test_shift_mesh_range_and_contact_use_the_physical_surface(self):
        plate = bump_plate()
        offset = np.array([1000.0, -170.0, 145.0])
        shifted = plate.shifted(offset)
        np.testing.assert_array_equal(shifted._tin_xyz, plate._tin_xyz - offset)
        np.testing.assert_array_equal(shifted._samples, plate._samples - offset)
        np.testing.assert_array_equal(shifted._tin_tri, plate._tin_tri)
        np.testing.assert_allclose(shifted.mesh() + offset, plate.mesh(), atol=2e-10)
        np.testing.assert_allclose(plate.metadata()["sensed"]["height_range_mm"], [160, 161.2], atol=2e-10)
        np.testing.assert_allclose(shifted._height_range(), [15, 16.2], atol=2e-10)
        mesh = plate.mesh()
        top = mesh[np.all(np.isclose(mesh[:, :, 2], plate.surface(mesh[:, :, 0], mesh[:, :, 1])), axis=1)]
        underside = mesh[np.all(np.isclose(mesh[:, :, 2] + plate.thickness,
                                           plate.surface(mesh[:, :, 0], mesh[:, :, 1])), axis=1)]
        self.assertEqual(len(top), len(plate._tin_tri))
        self.assertEqual(len(underside), len(plate._tin_tri))
        for xyz in plate._tin_xyz:
            datum = np.array([xyz[0], xyz[1], xyz[2] - SHIFT[2]])
            self.assertAlmostEqual(plate.datum(*xyz[:2]), datum[2], places=9)
            self.assertAlmostEqual(shifted.surface(*(xyz[:2] - offset[:2])), xyz[2] - offset[2], places=9)
            np.testing.assert_allclose(plate.distance([datum], contact=True), [0], atol=1e-10)
        outside = CENTER[:2] + [500, 0]
        self.assertAlmostEqual(plate.surface(*outside), plate.surface(*(CENTER[:2] + [100, 0])), places=10)

    def test_near_duplicate_touch_and_corner_are_named(self):
        for samples, labels in (([[0, 0, 0], [.5, .2, 0]], ("P044", "P045")),
                                ([[-99.5, -59.8, 0], [40, 20, 0]], ("corner 1", "P044"))):
            with self.subTest(samples=samples), self.assertRaises(ValueError) as error:
                fit_points(samples, fit="interpolate")
            self.assertIn("each touch needs its own station", str(error.exception).lower())
            for label in labels:
                self.assertIn(label, str(error.exception))

    def test_touch_outside_physical_hull_is_refused_even_inside_projected_polygon(self):
        plate = corner_plate(slope=(.012, .007), corner_errors=[.12, -.12, .12, -.12])
        physical = (plate.points + SHIFT)[:, :2]
        projected = plate.corners @ plate.basis[:2, :2].T + plate.origin[:2]
        candidate = None
        for i in range(4):
            j = (i + 1) % 4
            for t in (.25, .75):
                xy = ((1 - t) * (physical[i] + projected[i])
                      + t * (physical[j] + projected[j])) / 2
                uv = np.linalg.solve(plate.basis[:2, :2], xy - plate.origin[:2])
                if np.min(plate.edge_distances(uv)) >= 0:
                    edge = np.roll(physical, -1, axis=0) - physical
                    side = edge[:, 0] * (xy - physical)[:, 1] - edge[:, 1] * (xy - physical)[:, 0]
                    if side.min() < -1e-6:
                        candidate = xy
                        break
            if candidate is not None:
                break
        self.assertIsNotNone(candidate)
        with self.assertRaisesRegex(ValueError, "P044.*outside"):
            fit_points([[*(candidate - CENTER[:2]), 0], [0, 0, 0]], plate=plate, fit="interpolate")

    def test_steep_facet_names_its_three_vertices(self):
        with self.assertRaises(ValueError) as error:
            fit_points([[0, 0, 8.0], [60, 25, 0]], fit="interpolate")
        message = str(error.exception)
        self.assertRegex(message.lower(), "facet|slope|steep|tilt")
        self.assertIn("P044", message)
        self.assertGreaterEqual(len(set(re.findall(r"corner [1-4]|P04[45]", message))), 3)

    def test_narrow_low_touch_is_refused_but_high_spike_is_followed(self):
        with self.assertRaises(ValueError) as error:
            fit_points([[0, 0, -1.2], [60, 25, 0]], fit="interpolate")
        self.assertIn("P044", str(error.exception))
        self.assertIn("recheck the touch", str(error.exception).lower())
        high = fit_points([[0, 0, 1.2], [60, 25, 0]], fit="interpolate")
        self.assertAlmostEqual(high.surface(*CENTER[:2]), CENTER[2] + 1.2, places=10)
        with self.assertRaisesRegex(ValueError, "P044 differs from the smooth fit by -0.83 mm"):
            fit_points([[0, 0, -1.0], [60, 25, 0]], fit="interpolate")
        within = fit_points([[0, 0, -.5], [60, 25, 0]], fit="interpolate")
        self.assertAlmostEqual(within.surface(*CENTER[:2]), CENTER[2] - .5, places=10)

    def test_mutually_low_neighbours_cannot_vouch_for_each_other(self):


        with self.assertRaisesRegex(ValueError, r"P04[45] differs from the smooth fit by -1\.\d\d mm"):
            fit_points([[-1, 0, -2], [1, 0, -2]], fit="interpolate")

        with self.assertRaisesRegex(ValueError, "isolated high spot"):
            fit_points([[-1, 0, 2], [1, 0, 2]], fit="interpolate")
        followed = fit_points([[0, 0, 2], [60, 25, 0]], fit="interpolate")
        self.assertAlmostEqual(followed.surface(*CENTER[:2]), CENTER[2] + 2, places=9)

    def test_low_cluster_is_refused_and_a_high_spot_leaves_its_neighbours_alone(self):


        with self.assertRaisesRegex(ValueError, "differs from the smooth fit"):
            fit_points([[-70, -30, -2], [-68, -30, -2], [-66, -30, -2]], fit="interpolate")
        grid = [[x, y, 0.0] for x in (-60, -20, 20, 60) for y in (-30, 0, 30)]
        grid[4][2] = 1.5
        plate = fit_points(grid, fit="interpolate")
        xyz = plate._tin_xyz
        np.testing.assert_allclose(plate.surface(xyz[:, 0], xyz[:, 1]), xyz[:, 2], atol=1e-9)
        info = plate.metadata()["sensed"]
        self.assertEqual(info["worst"], "P048")
        self.assertGreater(info["max_residual_mm"], 1.0)

    def test_low_clusters_that_pull_the_fit_off_the_corners_are_refused(self):


        cases = {
            "five low touches": [[x, -30, -1.4] for x in range(-70, -61, 2)],
            "a 3 mm trough": [[x, 0, -3] for x in range(-40, 41, 4)],
            "a diagonal low line": [[x, .6 * x, -2] for x in range(-60, 61, 6)],
        }

        cases["a ring of lows around a correct reading"] = (
            [[30, 0, -2], [15, 26, -2], [-15, 26, -2], [-30, 0, -2], [-15, -26, -2], [15, -26, -2], [0, 0, 0]])
        cases["a correct reading beside one low pit"] = [[-90, -50, -.8], [-81, -41, 0]]
        cases["six lows among correct readings"] = (
            [[x, 0, -1.2] for x in (-10, -6, -2, 2, 6, 10)]
            + [[75, 0, 0], [-75, 0, 0], [0, 45, 0], [0, -45, 0]])
        for name, points in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "smooth"):
                fit_points(points, fit="interpolate")

    def test_a_high_spot_is_followed_while_the_plate_points_pass_the_smooth_test(self):
        plate = fit_points([[-20, 0, -.3], [20, 0, -.3], [0, 30, 1.8]], fit="interpolate")
        xyz = plate._tin_xyz
        np.testing.assert_allclose(plate.surface(xyz[:, 0], xyz[:, 1]), xyz[:, 2], atol=1e-9)
        self.assertEqual(plate.metadata()["sensed"]["worst"], "P046")
        with self.assertRaisesRegex(ValueError, "P046"):
            fit_points([[-20, 0, -.3], [20, 0, -.3], [0, 30, 1.8]], fit="smooth")

    def test_near_degenerate_layout_tiles_the_quad_exactly(self):


        corners = np.array([[-630, -38, 0], [630, -38, 0], [630, 38, 0], [-630, 38, 0]], float)
        touches = np.array([[1, -.1, -.08], [-.1, -1.2, 0], [0, 0, -.08], [1.3, -1.3, 0], [-1.2, -1.3, 0]])
        xy = np.vstack((corners, touches))[:, :2]
        tri = sensed._triangulate(xy)
        a, b, c = (xy[tri[:, k]] for k in range(3))
        areas = sensed._cross(b - a, c - a) / 2
        self.assertTrue(np.all(areas > 0))
        self.assertAlmostEqual(areas.sum(), 1260 * 76, places=6)
        self.assertEqual(set(tri.ravel()), set(range(len(xy))))
        exact = [tuple(Fraction(float(v)) for v in point) for point in xy]
        for t in tri:
            for k in set(range(len(xy))) - set(t):
                self.assertLessEqual(sensed._in_circle(*(exact[i] for i in t), exact[k]), 0)
        plate = replace(Substrate.fit(corners + CENTER - SHIFT, 6.35), gap_direction="torch",
                        tool0_tip_offset_mm=8.92, physical_shift_robot=tuple(SHIFT))
        fitted = fit_points(touches, plate=plate, fit="interpolate")
        vertices = fitted._tin_xyz
        np.testing.assert_allclose(fitted.surface(vertices[:, 0], vertices[:, 1]), vertices[:, 2], atol=1e-9)

    def test_cocircular_lattice_is_a_valid_delaunay_tiling(self):
        xy = np.array([[-100, -60], [100, -60], [100, 60], [-100, 60]] +
                      [[x, y] for y in (-45, -15, 15, 45) for x in (-80, -40, 0, 40, 80)], float)
        tri = sensed._triangulate(xy)
        a, b, c = (xy[tri[:, k]] for k in range(3))
        self.assertAlmostEqual((sensed._cross(b - a, c - a) / 2).sum(), 200 * 120, places=6)
        exact = [tuple(Fraction(float(v)) for v in point) for point in xy]
        for t in tri:
            for k in set(range(len(xy))) - set(t):
                self.assertLessEqual(sensed._in_circle(*(exact[i] for i in t), exact[k]), 0)

    def test_version_three_replays_interpolation_and_refuses_unknown_fit(self):
        plate = corner_plate()
        raw = registration(plate.points + SHIFT)
        touches = pulse_records(np.asarray([[0, 0, 1.2], [60, 25, 0]]) + CENTER)
        raw.update(version=3, sensed={"base": 44, "points": touches, "fit": "interpolate"})
        replay = from_request(json.dumps(raw), 6.35)
        self.assertEqual(replay.metadata()["sensed"]["fit"], "interpolated")
        actual = np.array([cell_lc.tcp_from_pulses(record) for record in touches])
        np.testing.assert_allclose(replay.surface(actual[:, 0], actual[:, 1]), actual[:, 2], atol=2e-10)
        for value in ("bogus", "interpolated", "", None, True, []):
            raw["sensed"]["fit"] = value
            with self.subTest(fit=value), self.assertRaisesRegex(ValueError, "fit"):
                from_request(json.dumps(raw), 6.35)


class TestSensePlanAlong(unittest.TestCase):
    def test_wall_stations_include_both_ends_at_requested_spacing(self):
        plate = corner_plate(slope=(.02, .01))
        origin = CENTER + [-50, 0, 50]
        plan = sensed.sense_plan_along(plate, slicer.wall(100, 3, 2), origin)
        expected_xy = np.array([[x, CENTER[1]] for x in np.arange(CENTER[0] - 50, CENTER[0] + 51, 20)])
        np.testing.assert_allclose(np.array([p.expected for p in plan])[:, :2], expected_xy, atol=1e-10)
        self.assertEqual([p.name for p in plan], [f"path_{i:02d}" for i in range(6)])
        for probe in plan:
            self.assertAlmostEqual(probe.expected[2], plate.datum(*probe.expected[:2]) + SHIFT[2], places=10)
            np.testing.assert_allclose(probe.start - probe.expected, [0, 0, 25])
            np.testing.assert_allclose(probe.target - probe.expected, [0, 0, -SEARCH_OVERTRAVEL_MM])
        shorter = sensed.sense_plan_along(plate, slicer.wall(95, 2, 2), origin)
        self.assertAlmostEqual(shorter[-1].expected[0] - origin[0], 95)
        self.assertLessEqual(np.diff([p.expected[0] for p in shorter]).max(), 20 + 1e-10)

    def test_four_beads_at_same_unseated_height_all_get_stations(self):
        path = demos.demo_bead(length_mm=80, count=4, pitch_mm=25)
        path = Toolpath([replace(m, z=m.z + .02 * m.x + .03 * m.y,
                                seat=.02 * m.x + .03 * m.y) for m in path.moves])
        origin = CENTER + [-40, -37.5, 0]
        plan = sensed.sense_plan_along(corner_plate(), path, origin, spacing_mm=20)
        self.assertEqual(len(plan), 20)
        for bead in range(4):
            row = [p for p in plan if abs(p.expected[1] - (origin[1] + 25 * bead)) < 1e-9]
            self.assertEqual(len(row), 5)
            np.testing.assert_allclose([p.expected[0] for p in row], origin[0] + np.arange(0, 81, 20))
            np.testing.assert_allclose([p.expected[2] for p in row], CENTER[2])

    def test_inset_drops_edge_stations_and_refuses_fewer_than_two(self):
        plate = corner_plate()
        origin = CENTER + [-100, 0, 0]
        plan = sensed.sense_plan_along(plate, slicer.wall(200, 2, 2), origin,
                                       spacing_mm=20, inset_mm=10)
        np.testing.assert_allclose([p.expected[0] for p in plan], CENTER[0] + np.arange(-80, 81, 20))
        for length, inset in ((10, 15), (40, 30)):
            with self.subTest(length=length), self.assertRaisesRegex(ValueError, "(?i)fewer than two"):
                sensed.sense_plan_along(plate, slicer.wall(length, 2, 2), origin,
                                        spacing_mm=20, inset_mm=inset)

    def test_budget_grows_spacing_but_preserves_each_beads_ends(self):
        plate = corner_plate()
        path = demos.demo_bead(length_mm=80, count=4, pitch_mm=25)
        origin = CENTER + [-40, -37.5, 0]
        plan = sensed.sense_plan_along(plate, path, origin, spacing_mm=1, max_points=11)
        self.assertLessEqual(len(plan), 11)
        for bead in range(4):
            row = [p for p in plan if abs(p.expected[1] - (origin[1] + 25 * bead)) < 1e-9]
            np.testing.assert_allclose([row[0].expected[0], row[-1].expected[0]], [origin[0], origin[0] + 80])
        repeat = sensed.sense_plan_along(plate, path, origin, spacing_mm=1, max_points=11)
        np.testing.assert_array_equal([p.expected for p in plan], [p.expected for p in repeat])
        with self.assertRaisesRegex(ValueError, "(?i)endpoint|ends|budget|at most"):
            sensed.sense_plan_along(plate, path, origin, spacing_mm=1, max_points=7)

    def test_stations_are_evenly_spaced_so_the_interpolated_fit_accepts_them(self):


        origin = CENTER + [-50.05, 0, 0]
        plan = sensed.sense_plan_along(corner_plate(), slicer.wall(100.1, 2, 2), origin, spacing_mm=20)
        x = np.array([p.expected[0] for p in plan]) - origin[0]
        self.assertAlmostEqual(x[0], 0)
        self.assertAlmostEqual(x[-1], 100.1)
        np.testing.assert_allclose(np.diff(x), 100.1 / 6)
        fitted = fit_points([p.expected - CENTER for p in plan], fit="interpolate")
        self.assertEqual(fitted.metadata()["sensed"]["points"], len(plan))
        side = 20.025
        square = [(0, 0), (side, 0), (side, side), (0, side), (0, 0)]
        path = Toolpath([Move(x, y, 2, 200, i > 0, 0, i + 1) for i, (x, y) in enumerate(square)])
        loop = sensed.sense_plan_along(corner_plate(), path, CENTER + [-10, -10, 0], spacing_mm=20)
        xy = np.array([p.expected[:2] for p in loop])
        gaps = np.linalg.norm(xy[:, None] - xy[None], axis=2) + np.eye(len(xy)) * 1e9
        self.assertGreaterEqual(gaps.min(), sensed.MIN_STATION_GAP_MM)
        fit_points([p.expected - CENTER for p in loop], fit="interpolate")

    def test_closed_contour_does_not_repeat_its_first_station(self):
        points = [(0, 0), (40, 0), (40, 40), (0, 40), (0, 0)]
        path = Toolpath([Move(x, y, 2, 200, i > 0, 0, i + 1) for i, (x, y) in enumerate(points)])
        origin = CENTER + [-20, -20, 0]
        plan = sensed.sense_plan_along(corner_plate(), path, origin, spacing_mm=20)
        self.assertEqual(len(plan), 8)
        self.assertEqual(len({tuple(p.expected[:2]) for p in plan}), 8)
        for probe in plan:
            local = probe.expected[:2] - origin[:2]
            self.assertTrue(np.any(np.isclose(local, 0)) or np.any(np.isclose(local, 40)))

    def test_pass_must_stay_at_lowest_height_and_have_a_weld_start(self):
        path = Toolpath([Move(-40, 0, 2, None, False, 0, 1),
                         Move(0, 0, 2, 200, True, 0, 2),
                         Move(40, 0, 4, 200, True, 1, 3),
                         Move(-40, 20, 2, None, False, 2, 4),
                         Move(40, 20, 2, 200, True, 2, 5)])
        plan = sensed.sense_plan_along(corner_plate(), path, CENTER, spacing_mm=20)
        np.testing.assert_allclose([p.expected[1] for p in plan], CENTER[1] + 20)
        for invalid in (Toolpath(), Toolpath([Move(0, 0, 2, 200, True, 0, 1)])):
            with self.subTest(moves=len(invalid.moves)), self.assertRaises(ValueError):
                sensed.sense_plan_along(corner_plate(), invalid, CENTER)

    def test_options_and_legacy_surface_are_validated(self):
        path, plate = slicer.wall(100, 2, 2), corner_plate()
        for options in ({"spacing_mm": 0}, {"inset_mm": -1}, {"approach_mm": float("nan")},
                        {"overtravel_mm": 0}, {"max_points": True}, {"max_points": 22}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                sensed.sense_plan_along(plate, path, CENTER + [-50, 0, 0], **options)
        with self.assertRaisesRegex(ValueError, "(?i)version 2|physical"):
            sensed.sense_plan_along(replace(plate, gap_direction=None), path, CENTER)


if __name__ == "__main__":
    unittest.main()
