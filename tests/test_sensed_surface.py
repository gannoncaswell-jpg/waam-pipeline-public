from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from waam_pipeline import cell_lc, clearance, slicer
from waam_pipeline.clearance import CONTACT_OK, Scene, TorchModel, blocking, check
from waam_pipeline.sensed import CHORD_TOL_MM, SensedPlate, fit_sensed, sense_plan
from waam_pipeline.substrate import Substrate, from_request
from waam_pipeline.toolpath import Move, Toolpath
from waam_pipeline.touch import SEARCH_OVERTRAVEL_MM
from sim.pybullet_driver import bead_specs, excluded_beads


CX, CY = 1014.0, -180.0
STICKOUT = 8.92


def surface_points(xy, curvature=1.5e-4):
    xy = np.asarray(xy, float)
    z = 160 + .015 * xy[:, 1] + curvature * (xy[:, 0] - CX) ** 2
    return np.column_stack((xy, z))


def pulse_records(points):
    solve = cell_lc.pulse_solver(np.zeros(3))
    return [np.rint(np.asarray(solve(point)) * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist()
            for point in points]


def registration(points):
    return {"version": 2, "frame": "lc50s_tool0_pulse",
            "weld_ctwd_mm": TorchModel.yaskawa_accufast_45().stackup.ctwd_mm,
            "gap_direction": "torch", "tool0_tip_offset_mm": STICKOUT,
            "corners": [{"pulses": record, "tip_distance_mm": STICKOUT}
                        for record in pulse_records(points)]}


def drooping_plate(curvature=1.5e-4):
    corners = np.array([[-100, -60], [100, -60], [100, 60], [-100, 60]]) + [CX, CY]
    raw = registration(surface_points(corners, curvature))
    plate = from_request(json.dumps(raw), 6.35)
    xy = np.array([[x, y] for y in np.linspace(CY - 45, CY + 45, 4)
                   for x in np.linspace(CX - 80, CX + 80, 5)])
    records = pulse_records(surface_points(xy, curvature))
    return plate, records, raw


def unseated(path):
    return Toolpath([replace(move, seat=0.0) for move in path.moves], list(path.unhandled))


def closed_contour():
    moves = [Move(0, 0, 2, None, False, 0, 1)]
    for layer in range(3):
        z = (layer + 1) * 2.0
        if layer:
            moves.append(Move(0, 0, z, None, False, layer, len(moves) + 1))
        for x, y in ((60, 0), (60, 40), (0, 40), (0, 0)):
            moves.append(Move(x, y, z, 200, True, layer, len(moves) + 1))
    return Toolpath(moves)


class TestSensePlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        corners = np.array([[-100, -50, 0], [100, -50, 0],
                            [100, 50, 0], [-100, 50, 0]], float) + [CX, CY, 160]
        cls.plate = from_request(json.dumps(registration(corners)), 6.35)

    def test_grid_is_inside_clear_polygon_and_probes_physical_plane(self):
        plan = sense_plan(self.plate)
        self.assertGreaterEqual(len(plan), 2)
        self.assertLessEqual(len(plan), 25)
        self.assertEqual([p.name for p in plan], [f"sense_{i:02d}" for i in range(len(plan))])
        expected = np.array([p.expected for p in plan])
        np.testing.assert_array_equal(expected, [p.expected for p in sense_plan(self.plate)])
        q = (expected - self.plate.origin - self.plate.physical_shift_robot) @ self.plate.basis
        self.assertGreaterEqual(self.plate.edge_distances(q[:, :2]).min(), 10 - 1e-8)
        np.testing.assert_allclose(q[:, 2], 0, atol=1e-10)
        self.assertEqual(np.lexsort((q[:, 0].round(8), q[:, 1].round(8))).tolist(), list(range(len(plan))))
        for probe in plan:
            np.testing.assert_allclose(probe.start - probe.expected, [0, 0, 25])
            np.testing.assert_allclose(probe.target - probe.expected, [0, 0, -SEARCH_OVERTRAVEL_MM])
            np.testing.assert_allclose(probe.direction, [0, 0, -1])
        for axis in (0, 1):
            steps = np.diff(np.unique(q[:, axis].round(8)))
            self.assertTrue(np.all(steps <= 40 + 1e-7))

    def test_dense_grid_coarsens_deterministically_to_register_budget(self):
        plan = sense_plan(self.plate, spacing_mm=5, max_points=7)
        self.assertGreaterEqual(len(plan), 2)
        self.assertLessEqual(len(plan), 7)
        np.testing.assert_array_equal([p.expected for p in plan],
                                      [p.expected for p in sense_plan(self.plate, spacing_mm=5, max_points=7)])

    def test_tiny_plate_and_legacy_registration_are_refused(self):
        tiny = np.array([[-5, -5, 160], [5, -5, 160], [5, 5, 160], [-5, 5, 160]], float)
        tiny[:, :2] += [CX, CY]
        plate = replace(Substrate.fit(tiny, 6), gap_direction="torch", tool0_tip_offset_mm=STICKOUT)
        with self.assertRaises(ValueError):
            sense_plan(plate)
        with self.assertRaisesRegex(ValueError, "(?i)(version 2|v2|physical)"):
            sense_plan(Substrate.fit(tiny, 6))


class TestSensedSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corner_plate, cls.records, cls.raw = drooping_plate()
        cls.plate = fit_sensed(cls.corner_plate, cls.records)
        corners, records, _ = drooping_plate(curvature=-1.5e-4)
        cls.crown = fit_sensed(corners, records)

    def test_quadratic_resolves_droop_that_plane_cannot_fit(self):
        info = self.plate.metadata()["sensed"]
        self.assertEqual(info["fit"], "quadratic")
        self.assertLess(info["max_residual_mm"], .05)
        self.assertLess(info["rms_residual_mm"], .05)
        points = np.vstack((self.corner_plate.points + self.corner_plate.physical_shift_robot,
                            [cell_lc.tcp_from_pulses(record) for record in self.records]))
        xy = (points[:, :2] - self.corner_plate.origin[:2]) / 100
        design = np.column_stack((np.ones(len(points)), xy))
        coeff, *_ = np.linalg.lstsq(design, points[:, 2], rcond=None)
        self.assertGreater(np.max(np.abs(points[:, 2] - design @ coeff)), .5)
        predicted = self.plate.surface(points[:, 0], points[:, 1])
        np.testing.assert_allclose(predicted, points[:, 2], atol=.05)
        np.testing.assert_array_equal(self.plate.origin[:2], self.corner_plate.origin[:2])
        self.assertAlmostEqual(self.plate.origin[2], self.plate.datum(*self.plate.origin[:2]))
        for field in ("basis", "corners", "points", "physical_shift_robot", "torch_tilt_deg"):
            np.testing.assert_array_equal(getattr(self.plate, field), getattr(self.corner_plate, field))

    def test_two_touches_use_plane(self):
        corners, records, _ = drooping_plate(curvature=0)
        plate = fit_sensed(corners, [records[0], records[-1]])
        self.assertEqual(plate.metadata()["sensed"]["fit"], "plane")
        self.assertLess(plate.residual, .05)

    def test_quadratic_requires_six_touches(self):
        corners, records, _ = drooping_plate(curvature=0)
        for count, expected in ((5, "plane"), (6, "quadratic")):
            selected = [records[i] for i in (0, 4, 7, 12, 15, 19)[:count]]
            with self.subTest(touches=count):
                plate = fit_sensed(corners, selected)
                self.assertEqual(plate.metadata()["sensed"]["fit"], expected)

    def test_outlier_and_outside_touch_name_pendant_register(self):
        bad = [record[:] for record in self.records]
        point = cell_lc.tcp_from_pulses(bad[8])
        bad[8] = pulse_records([point + [0, 0, 2]])[0]
        with self.assertRaisesRegex(ValueError, "P048.*[Rr]esidual|[Rr]esidual.*P048|P048.*mm"):
            fit_sensed(self.corner_plate, bad)
        bad = [record[:] for record in self.records]
        bad[3] = pulse_records(surface_points([[CX + 120, CY]]))[0]
        with self.assertRaisesRegex(ValueError, "P043"):
            fit_sensed(self.corner_plate, bad)

    def test_malformed_pulses_and_counts_are_refused(self):
        for records in ([], self.records[:1], self.records + self.records[:6]):
            with self.subTest(count=len(records)), self.assertRaises(ValueError):
                fit_sensed(self.corner_plate, records)
        for record in ([0] * 5, [0] * 7, [True] * 6, [float("nan")] * 6, [.5] * 6, [10**9] * 6):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "P040"):
                fit_sensed(self.corner_plate, [record] + self.records[1:])

    def test_surface_slope_is_checked_beyond_the_corner_plane(self):
        corners, records, _ = drooping_plate(curvature=6e-4)
        self.assertLess(corners.tilt, 2)
        with self.assertRaisesRegex(ValueError, "(?i)(5 degrees|tilt|slope)"):
            fit_sensed(corners, records)

    def test_exterior_height_clamps_to_nearest_polygon_point(self):
        polygon = self.plate.corners @ self.plate.basis[:2, :2].T + self.plate.origin[:2]
        outside = self.plate.origin[:2] + [1e6, 2e5]
        a, b = polygon, np.roll(polygon, -1, axis=0)
        edges = b - a
        t = np.clip(((outside - a) * edges).sum(1) / (edges * edges).sum(1), 0, 1)
        candidates = a + t[:, None] * edges
        nearest = candidates[np.argmin(np.linalg.norm(outside - candidates, axis=1))]
        self.assertAlmostEqual(self.plate.surface(*outside), self.plate.surface(*nearest), places=8)

    def test_every_move_and_layer_follows_surface_without_moving_xy(self):
        height = 2.0
        path = slicer.wall(110, layers=3, layer_height_mm=height)
        placed = self.plate.place(path, ox=4, oy=3)
        points = np.asarray(path.deposit_points())
        pivot = (points[:, :2].min(0) + points[:, :2].max(0)) / 2
        bottom = cell_lc.lowest_weld_z(path)
        layers = set()
        originals = {move.line_no: move for move in path.moves}
        for move in placed.moves:
            original = originals[move.line_no]
            world = self.plate.origin + [move.x, move.y, move.z]
            expected_z = self.plate.datum(*world[:2]) + original.z - bottom
            self.assertAlmostEqual(world[2], expected_z, places=6)
            if original.deposit:
                layer = round((original.z - bottom) / height)
                layers.add(layer)
                self.assertAlmostEqual(world[2], self.plate.datum(*world[:2]) + layer * height, places=6)
        for original in path.moves:
            last = [move for move in placed.moves if move.line_no == original.line_no][-1]
            expected_xy = self.plate.basis[:2, :2] @ (
                np.array([original.x, original.y]) - pivot + [4, 3])
            np.testing.assert_allclose(last.target[:2], expected_xy, atol=1e-10)
        self.assertEqual(layers, {0, 1, 2})
        with self.assertRaisesRegex(ValueError, "edge margin"):
            self.plate.place(path, ox=70)

    def test_contact_body_and_shifted_geometry(self):
        x, y = self.plate.origin[:2] + [20, -10]
        datum = self.plate.datum(x, y)
        top = self.plate.surface(x, y)
        points = np.array([[x, y, datum], [x, y, datum - 1], [x, y, datum - 20]])
        np.testing.assert_allclose(self.plate.distance(points, contact=True), [0, -1, -20], atol=1e-10)
        self.assertGreater(self.plate.distance([[x, y, top + 1]])[0], 0)
        self.assertLess(self.plate.distance([[x, y, top - 1]])[0], 0)
        self.assertGreater(self.plate.distance([[x, y, top - self.plate.thickness - 1]])[0], 0)
        offset = np.array([CX - 30, CY + 20, 130])
        shifted = self.plate.shifted(offset)
        for contact in (False, True):
            np.testing.assert_allclose(shifted.distance(points - offset, contact=contact),
                                       self.plate.distance(points, contact=contact), atol=1e-10)
        self.assertAlmostEqual(shifted.datum(x - offset[0], y - offset[1]), datum - offset[2])
        np.testing.assert_allclose(shifted.mesh() + offset, self.plate.mesh(), atol=1e-10)

    def test_curved_mesh_and_setup_metadata(self):
        mesh = self.plate.mesh()
        self.assertEqual(mesh.shape[1:], (3, 3))
        self.assertGreater(len(mesh), 12)
        vertices = mesh.reshape(-1, 3)
        heights = vertices[:, 2] - self.plate.surface(vertices[:, 0], vertices[:, 1])
        self.assertTrue(np.all(np.isclose(heights, 0) | np.isclose(heights, -self.plate.thickness)))
        setup = self.plate.setup_metadata(150, [CX, CY])
        self.assertEqual(set(setup), set(self.corner_plate.setup_metadata(150, [CX, CY])))
        self.assertAlmostEqual(setup["center_height_above_platter_mm"], self.plate.origin[2] - 150)
        points = np.vstack((self.corner_plate.points,
                            [cell_lc.tcp_from_pulses(record) for record in self.records]))
        heights = self.plate.datum(points[:, 0], points[:, 1]) - 150
        np.testing.assert_allclose(setup["height_range_above_platter_mm"], [heights.min(), heights.max()], atol=.001)
        info = self.plate.metadata()
        self.assertTrue(set(self.corner_plate.metadata()).issubset(info))
        low, high = info["sensed"]["height_range_mm"]
        self.assertAlmostEqual(low, 160 + .015 * (CY - 60), delta=.025)
        self.assertAlmostEqual(high, 160 + .015 * (CY + 60) + 1.5, delta=.025)

    def test_rotated_skew_polygon_limits_plan_distance_and_mesh(self):
        local = np.array([[-100, -60], [100, -45], [75, 60], [-85, 45]], float)
        theta = np.radians(32)
        rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
        xy = local @ rotation.T + [CX, CY]
        top = 160 + .015 * (xy[:, 0] - CX) + .01 * (xy[:, 1] - CY)
        shift = np.array([0, 0, STICKOUT - self.raw["weld_ctwd_mm"]])
        corner_plate = replace(Substrate.fit(np.column_stack((xy, top)) - shift, 6.35),
                               physical_shift_robot=tuple(shift), gap_direction="torch",
                               tool0_tip_offset_mm=STICKOUT)
        plan = sense_plan(corner_plate, spacing_mm=25, max_points=25)
        expected = np.array([probe.expected for probe in plan])
        uv = ((expected - corner_plate.origin - shift) @ corner_plate.basis)[:, :2]
        self.assertGreaterEqual(corner_plate.edge_distances(uv).min(), 10 - 1e-8)
        plate = fit_sensed(corner_plate, pulse_records(expected))
        mesh = plate.mesh()
        vertices = mesh.reshape(-1, 3)
        uv = np.linalg.solve(plate.basis[:2, :2], (vertices[:, :2] - plate.origin[:2]).T).T
        self.assertGreaterEqual(plate.edge_distances(uv).min(), -1e-8)
        top_faces = mesh[np.all(np.isclose(mesh[:, :, 2], plate.surface(mesh[:, :, 0], mesh[:, :, 1])), axis=1)]
        edges_a, edges_b = top_faces[:, 1, :2] - top_faces[:, 0, :2], top_faces[:, 2, :2] - top_faces[:, 0, :2]
        mesh_area = np.abs(edges_a[:, 0] * edges_b[:, 1] - edges_a[:, 1] * edges_b[:, 0]).sum() / 2
        polygon_area = abs(np.dot(xy[:, 0], np.roll(xy[:, 1], -1))
                           - np.dot(xy[:, 1], np.roll(xy[:, 0], -1))) / 2
        self.assertAlmostEqual(mesh_area, polygon_area, places=6)
        midpoint = (xy[0] + xy[1]) / 2
        edge = xy[1] - xy[0]
        outside = midpoint + 2 * np.array([edge[1], -edge[0]]) / np.linalg.norm(edge)
        self.assertGreater(plate.distance([[*outside, plate.datum(*outside)]], contact=True)[0], 1)

    def test_height_range_includes_unsampled_interior_minimum(self):
        corners_xy = np.array([[-100, -60], [100, -60], [100, 60], [-100, 60]]) + [CX, CY]
        grid_xy = np.array([[x, y] for y in np.linspace(CY - 45, CY + 45, 4)
                            for x in np.linspace(CX - 80, CX + 80, 5)])

        def bowl(xy):
            return np.column_stack((xy, 160 + 1.5e-4 * (xy[:, 0] - CX) ** 2
                                    + 1e-4 * (xy[:, 1] - CY) ** 2))

        corners = from_request(json.dumps(registration(bowl(corners_xy))), 6.35)
        plate = fit_sensed(corners, pulse_records(bowl(grid_xy)))
        x, y = plate.origin[:2]
        f = plate.surface
        gradient = np.array([(f(x + 1, y) - f(x - 1, y)) / 2,
                             (f(x, y + 1) - f(x, y - 1)) / 2])
        cross = (f(x + 1, y + 1) - f(x + 1, y - 1) - f(x - 1, y + 1) + f(x - 1, y - 1)) / 4
        hessian = np.array([[f(x + 1, y) - 2 * f(x, y) + f(x - 1, y), cross],
                            [cross, f(x, y + 1) - 2 * f(x, y) + f(x, y - 1)]])
        minimum_xy = plate.origin[:2] - np.linalg.solve(hessian, gradient)
        low, high = plate.metadata()["sensed"]["height_range_mm"]
        self.assertAlmostEqual(low, f(*minimum_xy), places=10)
        polygon = plate.corners @ plate.basis[:2, :2].T + plate.origin[:2]
        self.assertAlmostEqual(high, np.max(f(polygon[:, 0], polygon[:, 1])), places=10)
        self.assertLess(low, np.min(f(grid_xy[:, 0], grid_xy[:, 1])) - .01)

    def test_clearance_accepts_seated_walls_and_closed_contours(self):
        height = 2.0
        wall = slicer.wall(100, layers=3, layer_height_mm=height)
        torch = TorchModel.yaskawa_accufast_45()
        for shape, path in (("wall", wall), ("contour", closed_contour())):
            for surface, plate in (("sag", self.plate), ("crown", self.crown)):
                with self.subTest(shape=shape, surface=surface):
                    placed = plate.place(path, ox=4, oy=3)
                    scene = Scene(substrates=[plate.shifted(plate.origin)])
                    hits = blocking(check(placed, torch, scene, contact_ok=CONTACT_OK,
                                          bead_height_mm=height))
                    self.assertFalse(hits, hits)

                    unframed = blocking(check(unseated(placed), torch, scene, contact_ok=CONTACT_OK,
                                              bead_height_mm=height))
                    self.assertTrue(any(hit.obstacle.startswith("bead(") for hit in unframed), unframed)
                    lowered = Toolpath([replace(move, z=move.z - .5) for move in placed.moves])
                    hits = blocking(check(lowered, torch, scene, contact_ok=CONTACT_OK,
                                          bead_height_mm=height))
                    self.assertIn("bead_test_coupon", {hit.obstacle for hit in hits})

    def test_surface_frame_preserves_four_micron_plough_refusals(self):
        torch = TorchModel.yaskawa_accufast_45()
        for label, plate in (("sag", self.plate), ("crown", self.crown)):
            with self.subTest(surface=label):
                placed = plate.place(slicer.wall(100, 3, 2.0), ox=4, oy=3)
                scene = Scene(substrates=[plate.shifted(plate.origin)])
                sunk = Toolpath([replace(move, z=move.z - (.004 if move.layer >= 1 else 0))
                                 for move in placed.moves])
                hits = [hit for hit in blocking(check(sunk, torch, scene, contact_ok=CONTACT_OK,
                                                      bead_height_mm=2.0))
                        if hit.obstacle.startswith("bead(")]
                self.assertTrue(hits)
                self.assertAlmostEqual(min(hit.clearance_mm for hit in hits), -.004, delta=1e-6)
                last = placed.moves[-1]
                down = Toolpath(placed.moves + [replace(last, z=last.z - .004, deposit=False,
                                                       line_no=last.line_no + 1)])
                hits = blocking(check(down, torch, scene, contact_ok=CONTACT_OK, bead_height_mm=2.0))
                self.assertTrue(any(hit.obstacle.startswith("bead(") for hit in hits), hits)

    def test_chords_stay_above_crown_and_sag_vertices_need_no_lift(self):
        for label, plate in (("sag", self.plate), ("crown", self.crown)):
            with self.subTest(surface=label):
                placed = plate.place(slicer.wall(100, 3, 2.0), ox=4, oy=3)
                datum = plate.shifted(plate.origin).datum
                vertices = np.array([move.target for move in placed.moves if move.layer == 0])
                lifts = vertices[:, 2] - datum(vertices[:, 0], vertices[:, 1])
                np.testing.assert_allclose(lifts, lifts[0], atol=1e-10)
                if label == "sag":
                    np.testing.assert_allclose(lifts, 0, atol=1e-10)
                else:
                    self.assertGreater(lifts[0], 0)
                for prev, move in zip(placed.moves, placed.moves[1:]):
                    if move.layer != 0:
                        continue
                    a, b = np.asarray(prev.target), np.asarray(move.target)
                    n = int(np.ceil(np.linalg.norm(b - a))) + 1
                    samples = a + np.linspace(0, 1, n)[:, None] * (b - a)
                    gap = samples[:, 2] - datum(samples[:, 0], samples[:, 1])
                    self.assertGreaterEqual(gap.min(), -1e-9)
                    self.assertLessEqual(gap.max(), CHORD_TOL_MM + 1e-9)

    def test_split_moves_preserve_ramps_attributes_and_arc_events(self):
        path = Toolpath([Move(-75, -10, 5, None, False, 0, 10, "start"),
                         Move(75, -10, 8, 123, True, 0, 20, "ramp"),
                         Move(-75, 10, 11, 456, False, 1, 30, "travel"),
                         Move(75, 10, 11, 321, True, 1, 40, "top"),
                         Move(75, 10, 15, 789, False, 1, 50, "lift")], [(99, "M999")])
        for label, plate in (("sag", self.plate), ("crown", self.crown)):
            with self.subTest(surface=label):
                placed = plate.place(path, ox=4, oy=3, gap=.3)
                self.assertEqual(placed.unhandled, path.unhandled)
                self.assertEqual([kind for kind, _ in placed.arc_events()],
                                 [kind for kind, _ in path.arc_events()])
                points = np.asarray(path.deposit_points())
                pivot = (points[:, :2].min(0) + points[:, :2].max(0)) / 2
                bottom = cell_lc.lowest_weld_z(path)
                inverse = np.linalg.inv(plate.basis[:2, :2])
                lift = None
                for i, original in enumerate(path.moves):
                    pieces = [move for move in placed.moves if move.line_no == original.line_no]
                    if i in (0, 4):
                        self.assertEqual(len(pieces), 1)
                    else:
                        self.assertGreater(len(pieces), 1)
                    for j, move in enumerate(pieces, 1):
                        self.assertEqual((move.deposit, move.layer, move.line_no, move.feed, move.region),
                                         (original.deposit, original.layer, original.line_no,
                                          original.feed, original.region))
                        t = j / len(pieces)
                        a = np.array(path.moves[max(i - 1, 0)].target)
                        b = np.array(original.target)
                        expected = a + t * (b - a)
                        local = inverse @ np.array(move.target[:2]) + pivot - [4, 3]
                        np.testing.assert_allclose(local, expected[:2], atol=1e-10)
                        world = plate.origin + np.array(move.target)
                        residual = world[2] - plate.datum(*world[:2]) - (expected[2] - bottom + .3)
                        if lift is None:
                            lift = residual
                        self.assertAlmostEqual(residual, lift, places=10)

    def test_plane_fit_does_not_split_moves(self):
        corners, records, _ = drooping_plate(curvature=0)
        plate = fit_sensed(corners, [records[i] for i in (0, 4, 7, 15, 19)])
        path = slicer.wall(150, 3, 2.0)
        placed = plate.place(path, ox=4, oy=3)
        self.assertEqual(len(placed.moves), len(path.moves))
        self.assertEqual(placed.arc_events(), path.arc_events())

    def test_seats_recover_the_slicer_frame_under_every_split_and_lift(self):


        path = Toolpath([Move(-75, -10, 5, None, False, 0, 10),
                         Move(75, -10, 8, 123, True, 0, 20),
                         Move(75, 10, 11, 321, True, 1, 30)])
        for label, plate in (("sag", self.plate), ("crown", self.crown),
                             ("planar", self.corner_plate)):
            with self.subTest(surface=label):
                bottom = cell_lc.lowest_weld_z(path)
                placed = plate.place(path, ox=4, oy=3, bottom=bottom, gap=.3)
                for i, original in enumerate(path.moves):
                    pieces = [move for move in placed.moves if move.line_no == original.line_no]
                    a = np.array(path.moves[max(i - 1, 0)].target)
                    b = np.array(original.target)
                    for j, move in enumerate(pieces, 1):
                        t = j / len(pieces)
                        source_z = (a + t * (b - a))[2]
                        self.assertAlmostEqual(move.z - move.seat, source_z - bottom + .3, places=9)
                if label != "planar":
                    self.assertTrue(any(abs(move.seat) > 1e-3 for move in placed.moves))

    def test_planar_datum_matches_place_and_shifted_frame(self):
        path = closed_contour()
        plate = self.corner_plate
        bottom, gap = cell_lc.lowest_weld_z(path), .25
        placed = plate.place(path, ox=4, oy=3, bottom=bottom, gap=gap)
        shifted = plate.shifted(plate.origin)
        self.assertGreater(plate.tilt, .1)
        for original, move in zip(path.moves, placed.moves):
            world = plate.origin + np.array(move.target)
            expected = original.z - bottom + gap
            self.assertAlmostEqual(world[2] - plate.datum(*world[:2]), expected, places=10)
            self.assertAlmostEqual(move.z - shifted.datum(move.x, move.y), expected, places=10)
        x, y = np.array([[-10], [10]]), np.array([-20, 0, 20])
        self.assertEqual(shifted.datum(x, y).shape, (2, 3))

    def test_tier_two_surface_exclusion_matches_tier_one(self):
        placed = self.plate.place(slicer.wall(100, 3, 2.0), ox=4, oy=3)
        physical = np.array([move.target for move in placed.moves])
        flat = physical.copy()
        flat[:, 2] -= [move.seat for move in placed.moves]
        framed = bead_specs(placed, bead_height_mm=2.0)
        unframed = bead_specs(unseated(placed), bead_height_mm=2.0)
        differs = False
        for i, move in enumerate(placed.moves):
            if not move.deposit or move.layer != 1:
                continue
            prior = [bead for bead in framed if bead.move_index < i]
            prior_physical = [bead for bead in unframed if bead.move_index < i]
            slabs = [clearance.bead_slab(flat[bead.move_index - 1], flat[bead.move_index], 2.0)
                     for bead in prior]
            axes_a, axes_b = np.array([s[0] for s in slabs]), np.array([s[1] for s in slabs])
            top, lay = np.array([s[2] for s in slabs]), np.array([s[3] for s in slabs])
            for t in np.linspace(0, 1, 9):
                sample = flat[i - 1] + t * (flat[i] - flat[i - 1])
                mask = excluded_beads(sample, prior, 12.0, flat[i - 1, 2])
                near = clearance._point_segment_dist(sample[None, :], axes_a, axes_b) < 12.0
                expected = clearance.weld_zone(near, [sample[2]], flat[i - 1, 2],
                                               top, lay[:, 0], lay[:, 1])[0]
                np.testing.assert_array_equal(mask, expected)
                sample_physical = physical[i - 1] + t * (physical[i] - physical[i - 1])
                physical_mask = excluded_beads(sample_physical, prior_physical, 12.0, physical[i - 1, 2])
                layer_zero = np.array([placed.moves[bead.move_index].layer == 0 for bead in prior])
                differs |= bool(np.any(mask & ~physical_mask & layer_zero))
        self.assertTrue(differs, "Surface frame must forgive a lower bead the absolute-Z rule refuses")

    def test_version_three_replays_raw_touch_records(self):
        raw = dict(self.raw, version=3, sensed={"base": 40, "points": self.records})
        replay = from_request(json.dumps(raw), 6.35)
        self.assertIsInstance(replay, SensedPlate)
        self.assertEqual(replay.metadata(), self.plate.metadata())
        for base in (-1, 226, True, 40.5):
            bad = dict(raw, sensed={"base": base, "points": self.records})
            with self.subTest(base=base), self.assertRaises(ValueError):
                from_request(json.dumps(bad), 6.35)
        for sensed in ({}, {"base": 40}, {"base": 40, "points": []}, []):
            with self.subTest(sensed=sensed), self.assertRaises(ValueError):
                from_request(json.dumps(dict(raw, sensed=sensed)), 6.35)


if __name__ == "__main__":
    unittest.main()
