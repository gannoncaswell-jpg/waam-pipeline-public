import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace

from waam_pipeline import parse
from waam_pipeline.toolpath import Toolpath
from waam_pipeline.slicer import (
    _add_run_ins,
    _reduce_collinear,
    elbow_mesh,
    scale_stl_to_mm,
    slice_mesh,
    to_gcode,
    transform,
    wall,
)


def cylinder_mesh(radius=20.0, height=30.0, around=48):
    vs = np.linspace(0, 2 * np.pi, around, endpoint=False)
    ring0 = np.stack([radius * np.cos(vs), radius * np.sin(vs), np.zeros(around)], axis=1)
    ring1 = ring0 + [0, 0, height]
    tris = []
    for j in range(around):
        j2 = (j + 1) % around
        tris.append([ring0[j], ring0[j2], ring1[j]])
        tris.append([ring0[j2], ring1[j2], ring1[j]])
    return np.array(tris)


class TestWall(unittest.TestCase):
    def test_zigzag_stagger(self):
        tp = wall(100.0, layers=4, layer_height_mm=3.0)
        self.assertEqual(len(tp.moves), 8)
        self.assertEqual(len(tp.layers()), 4)
        deposits = [m for m in tp.moves if m.deposit]
        self.assertEqual(len(deposits), 4)

        self.assertEqual([d.x for d in deposits], [100.0, 0.0, 100.0, 0.0])
        self.assertEqual([round(d.z, 1) for d in deposits], [3.0, 6.0, 9.0, 12.0])


def banded_box(length=40.0, height=10.0, band_z=5.0, banded_faces=(0, 3), reverse=False):


    def quad(a, b, c, d):
        return [[a, b, c], [a, c, d]]
    corners = [(0, 0), (length, 0), (length, length), (0, length)]
    tris = []
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(corners, corners[1:] + corners[:1])):
        if i in banded_faces:
            tris += quad((x0, y0, 0), (x1, y1, 0), (x1, y1, band_z), (x0, y0, band_z))
            tris += quad((x0, y0, band_z), (x1, y1, band_z), (x1, y1, height), (x0, y0, height))
        else:
            tris += quad((x0, y0, 0), (x1, y1, 0), (x1, y1, height), (x0, y0, height))
    tris += quad((0, 0, 0), (0, length, 0), (length, length, 0), (length, 0, 0))
    tris += quad((0, 0, height), (length, 0, height), (length, length, height), (0, length, height))
    return np.array(tris[::-1] if reverse else tris, float)


class TestCutPlaneThroughVertices(unittest.TestCase):
    def test_a_vertex_row_on_a_cut_plane_keeps_the_whole_wall(self):


        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                tp = slice_mesh(banded_box(reverse=reverse), layer_height_mm=2.0)
                layers = tp.layers()
                self.assertEqual(len(layers), 5)
                for layer in layers:
                    weld = sum(np.hypot(b.x - a.x, b.y - a.y)
                               for a, b in zip(layer, layer[1:]) if b.deposit)
                    self.assertAlmostEqual(weld, 160.0, places=6)

    def test_a_cut_plane_on_the_top_face_gives_its_outline(self):
        from waam_pipeline.slicer import _chain_loops, _plane_segments
        loops = _chain_loops(_plane_segments(banded_box(), 10.0))
        self.assertEqual(len(loops), 1)
        self.assertAlmostEqual(float(np.linalg.norm(loops[0][0] - loops[0][-1])), 0.0)


class TestLayerLimit(unittest.TestCase):
    def test_a_part_past_the_layer_limit_is_refused_before_slicing(self):
        from waam_pipeline import ornl, slicer
        self.assertEqual(slicer.MAX_LAYERS, ornl.MAX_LAYERS)
        tall = banded_box(length=20.0, height=100.0, band_z=50.0)
        with patch.object(slicer, "_plane_segments", side_effect=AssertionError("sliced")):
            with self.assertRaisesRegex(ValueError, "1000 layers at 0.1 mm; the limit is 500"):
                slice_mesh(tall, layer_height_mm=0.1)
        self.assertEqual(len(slice_mesh(tall, layer_height_mm=0.2).layers()), 500)


class TestSliceMesh(unittest.TestCase):
    def test_auto_units_recognise_metre_export(self):
        raw = cylinder_mesh(radius=0.02, height=0.01)
        scaled, units, factor = scale_stl_to_mm(raw, "auto")
        self.assertEqual(units, "m")
        self.assertEqual(factor, 1000.0)
        self.assertAlmostEqual(float(np.ptp(scaled[..., 0])), 40.0)

    def test_explicit_units_override_auto(self):
        raw = cylinder_mesh(radius=2.0, height=1.0)
        scaled, units, factor = scale_stl_to_mm(raw, "mm")
        self.assertEqual((units, factor), ("mm", 1.0))
        self.assertAlmostEqual(float(np.ptp(scaled[..., 0])), 4.0)

    def test_cylinder(self):
        tp = slice_mesh(cylinder_mesh(), layer_height_mm=3.0)
        layers = tp.layers()
        self.assertEqual(len(layers), 10)
        for layer in layers:
            deposits = [m for m in layer if m.deposit]

            length = sum(
                np.linalg.norm(np.subtract(b.target, a.target))
                for a, b in zip(layer, layer[1:])
                if b.deposit
            )
            self.assertAlmostEqual(length, 2 * np.pi * 20, delta=0.02 * 2 * np.pi * 20)

            np.testing.assert_allclose(layer[0].x, deposits[-1].x, atol=1e-6)
            np.testing.assert_allclose(layer[0].y, deposits[-1].y, atol=1e-6)

    def test_spiralize_cylinder(self):
        radius, height, step = 20.0, 180.0, 3.0
        tri = cylinder_mesh(radius=radius, height=height)
        plain = slice_mesh(tri, step)
        tp = slice_mesh(tri, step, feed_mm_min=237.0, spiralize=True)
        self.assertEqual(tp.arc_events(), [("ARCON", 1), ("ARCOFF", len(tp.moves))])
        self.assertEqual(sum(not m.deposit for m in tp.moves), 1)
        self.assertEqual(tp.moves[0].z, 0.0)
        self.assertEqual(tp.bbox()[2][1], height)
        self.assertEqual(len(tp.layers()), 60)
        self.assertEqual([m.line_no for m in tp.moves], list(range(1, len(tp.moves) + 1)))
        self.assertTrue(all(m.feed == 237.0 for m in tp.moves if m.deposit))

        pairs = list(zip(tp.moves, tp.moves[1:]))
        self.assertTrue(all(b.z >= a.z for a, b in pairs))
        expected_length = 0.0
        xy_length = 0.0
        for k in range(60):
            turn = [(a, b) for a, b in pairs if b.layer == k and b.z > a.z]
            lengths = np.array([np.linalg.norm(np.subtract(b.target[:2], a.target[:2])) for a, b in turn])
            perimeter = lengths.sum()
            self.assertAlmostEqual(perimeter, 2 * np.pi * radius, delta=0.02 * 2 * np.pi * radius)
            self.assertEqual(turn[0][0].z, k * step)
            self.assertEqual(turn[-1][1].z, (k + 1) * step)
            np.testing.assert_allclose(
                [b.z for a, b in turn], k * step + step * np.cumsum(lengths) / perimeter,
            )
            expected_length += np.hypot(perimeter, step)
            xy_length += perimeter

        cap = [(a, b) for a, b in pairs if a.z == b.z == height]
        cap_length = sum(np.linalg.norm(np.subtract(b.target[:2], a.target[:2])) for a, b in cap)
        self.assertAlmostEqual(cap_length, 2 * np.pi * radius, delta=0.02 * 2 * np.pi * radius)
        np.testing.assert_allclose(cap[0][0].target, cap[-1][1].target)
        self.assertTrue(all(b.layer == 59 and b.deposit for a, b in cap))
        self.assertAlmostEqual(tp.deposit_length(), expected_length + cap_length, places=6)
        self.assertAlmostEqual(xy_length + cap_length, plain.deposit_length(), delta=0.02 * plain.deposit_length())
        self.assertGreater(tp.deposit_length(), plain.deposit_length())
        self.assertLess(tp.deposit_length(), 1.02 * plain.deposit_length())

    def test_spiralize_two_turns_include_extra_cap(self):
        tri = cylinder_mesh(height=6.0)
        plain = slice_mesh(tri, 3.0)
        tp = slice_mesh(tri, 3.0, spiralize=True)
        perimeter = plain.deposit_length() / 2
        self.assertEqual(sum(e[0] == "ARCON" for e in tp.arc_events()), 1)
        self.assertEqual(tp.moves[-1].z, 6.0)
        self.assertAlmostEqual(
            tp.deposit_length(), 2 * np.hypot(perimeter, 3.0) + perimeter,
            delta=0.02 * perimeter,
        )

    def test_spiralize_false_and_single_layer(self):
        for tri in (cylinder_mesh(), cylinder_mesh(height=3.0)):
            with self.subTest(height=tri[:, :, 2].max()):
                default = slice_mesh(tri, 3.0)
                explicit = slice_mesh(tri, 3.0, spiralize=False)
                self.assertEqual(default, explicit)
                self.assertEqual(to_gcode(default), to_gcode(explicit))
        self.assertEqual(
            slice_mesh(cylinder_mesh(height=3.0), 3.0),
            slice_mesh(cylinder_mesh(height=3.0), 3.0, spiralize=True),
        )

    def test_spiralize_elbow_keeps_ineligible_layers(self):
        tri = transform(elbow_mesh(60.0, 15.0, rings=40, around=24), rotate_deg=-45, axis="y")
        plain = slice_mesh(tri, 3.0)
        tp = slice_mesh(tri, 3.0, spiralize=True)
        self.assertGreater(sum(e[0] == "ARCON" for e in tp.arc_events()), 1)
        self.assertLess(len(tp.arc_events()), len(plain.arc_events()))
        actual = {layer[0].layer: layer for layer in tp.layers()}
        fallback_layers = 0
        for layer in plain.layers():
            if sum(not m.deposit for m in layer) != 1 or layer[0].target != layer[-1].target:
                fallback_layers += 1
                self.assertEqual(
                    [(m.target, m.feed, m.deposit, m.layer) for m in layer],
                    [(m.target, m.feed, m.deposit, m.layer) for m in actual[layer[0].layer]],
                )
        self.assertGreater(fallback_layers, 0)
        self.assertTrue(np.isfinite([m.target for m in tp.moves]).all())

    def test_spiralize_runs_split_at_open_and_multiple_contours(self):


        closed = np.array([[0., 0., 0.], [4., 0., 0.], [4., 3., 0.], [0., 3., 0.], [0., 0., 0.]])
        other = closed + [20, 0, 0]
        open_arc = closed[:-1]
        layers = [
            [open_arc], [closed], [open_arc], [closed], [closed[::-1]],
            [closed, other], [closed], [closed], [open_arc], [closed],
        ]
        with patch("waam_pipeline.slicer._chain_loops", side_effect=layers):
            plain = slice_mesh(cylinder_mesh(height=30.0), 3.0)
        with patch("waam_pipeline.slicer._chain_loops", side_effect=layers):
            tp = slice_mesh(cylinder_mesh(height=30.0), 3.0, spiralize=True)
        expected = {layer[0].layer: layer for layer in plain.layers()}
        actual = {layer[0].layer: layer for layer in tp.layers()}
        self.assertEqual(set(actual), set(range(10)))
        self.assertEqual(sum(e[0] == "ARCON" for e in tp.arc_events()), 9)
        for k in (0, 1, 2, 5, 8, 9):
            self.assertEqual(
                [(m.target, m.feed, m.deposit, m.layer) for m in actual[k]],
                [(m.target, m.feed, m.deposit, m.layer) for m in expected[k]],
            )
        for first, last in ((3, 4), (6, 7)):
            self.assertFalse(actual[first][0].deposit)
            self.assertEqual(actual[first][0].z, first * 3.0)
            self.assertEqual(actual[last][-1].z, (last + 1) * 3.0)
            self.assertEqual(sum(not m.deposit for k in (first, last) for m in actual[k]), 1)

    def test_spiralize_matches_seams_and_winding(self):
        first = np.array([[0., 0., 0.], [4., 0., 0.], [4., 3., 0.], [0., 3., 0.], [0., 0., 0.]])
        second = first[::-1] + [0.4, 0.2, 0]
        with patch("waam_pipeline.slicer._chain_loops", side_effect=[[first], [second]]):
            tp = slice_mesh(cylinder_mesh(height=6.0), 3.0, spiralize=True)
        self.assertEqual(sum(e[0] == "ARCON" for e in tp.arc_events()), 1)
        np.testing.assert_allclose(tp.layers()[1][0].target[:2], [4.0, 0.2])
        pairs = list(zip(tp.moves, tp.moves[1:]))
        areas = []
        for k in (0, 1):
            turn = [(a, b) for a, b in pairs if b.layer == k and b.z > a.z]
            lengths = np.array([np.linalg.norm(np.subtract(b.target[:2], a.target[:2])) for a, b in turn])
            np.testing.assert_allclose(
                [b.z for a, b in turn], k * 3.0 + 3.0 * np.cumsum(lengths) / lengths.sum(),
            )
            areas.append(sum(a.x * b.y - a.y * b.x for a, b in turn))
        self.assertGreater(areas[0] * areas[1], 0)
        np.testing.assert_allclose(tp.moves[-1].target, [4.0, 0.2, 6.0])

    def test_spiralize_run_has_no_zero_length_approach(self):
        loop = np.array([[0., 0., 0.], [4., 0., 0.], [4., 3., 0.], [0., 3., 0.], [0., 0., 0.]])
        with patch("waam_pipeline.slicer._chain_loops", side_effect=[[loop + [20, 0, 0], loop], [loop], [loop]]):
            tp = slice_mesh(cylinder_mesh(height=9.0), 3.0, seam_rotation_deg_per_layer=0, spiralize=True)
        self.assertEqual(sum(e[0] == "ARCON" for e in tp.arc_events()), 3)
        for a, b in zip(tp.moves, tp.moves[1:]):
            if not b.deposit:
                self.assertNotEqual(a.target, b.target)

    def test_seam_rotates(self):
        tp = slice_mesh(cylinder_mesh(), layer_height_mm=3.0, seam_rotation_deg_per_layer=30)
        starts = [layer[0] for layer in tp.layers()]
        self.assertNotAlmostEqual(starts[0].x, starts[1].x, places=1)

    def test_tilted_elbow_slices(self):
        tri = transform(elbow_mesh(60.0, 15.0, rings=40, around=24), rotate_deg=-45, axis="y")
        tp = slice_mesh(tri, layer_height_mm=3.0)
        self.assertGreater(len(tp.layers()), 10)
        self.assertGreater(sum(1 for m in tp.moves if m.deposit), 100)
        (x0, x1), _, (z0, z1) = tp.bbox()
        self.assertGreaterEqual(z0, 0.0)

    def test_point_reduction(self):
        line_pts = np.array([[x, 0.0, 0.0] for x in np.linspace(0, 10, 30)])
        square = np.vstack([line_pts, [[10, 10, 0]], [[0, 10, 0]]])
        reduced = _reduce_collinear(square)
        self.assertLess(len(reduced), 6)


class TestTravelOrdering(unittest.TestCase):
    @staticmethod
    def deposited_edges(tp):
        return Counter(
            (b.layer, frozenset((tuple(round(v, 9) for v in a.target),
                                tuple(round(v, 9) for v in b.target))))
            for a, b in zip(tp.moves, tp.moves[1:]) if b.deposit
        )

    def assert_preserved(self, plain, optimized):
        self.assertAlmostEqual(optimized.deposit_length(), plain.deposit_length(), delta=1e-6)
        self.assertEqual(self.deposited_edges(optimized), self.deposited_edges(plain))
        self.assertEqual(
            sum(e[0] == "ARCON" for e in optimized.arc_events()),
            sum(e[0] == "ARCON" for e in plain.arc_events()),
        )
        self.assertEqual([m.line_no for m in optimized.moves], list(range(1, len(optimized.moves) + 1)))
        self.assertEqual([m.layer for m in optimized.moves], sorted(m.layer for m in optimized.moves))

    def test_two_tubes(self):
        tube = cylinder_mesh()
        tri = np.concatenate([tube, tube + [100, 0, 0]])
        plain = slice_mesh(tri, 3.0)
        optimized = slice_mesh(tri, 3.0, optimize_travel=True)
        self.assert_preserved(plain, optimized)
        self.assertLess(optimized.travel_length(), 0.5 * plain.travel_length())
        self.assertEqual(sum(e[0] == "ARCON" for e in optimized.arc_events()), 20)

    def test_single_tube(self):
        tri = cylinder_mesh()
        plain = slice_mesh(tri, 3.0)
        optimized = slice_mesh(tri, 3.0, optimize_travel=True)
        self.assert_preserved(plain, optimized)
        self.assertLessEqual(optimized.travel_length(), plain.travel_length())

    def test_elbow_open_arcs(self):
        tri = transform(elbow_mesh(60.0, 15.0, rings=40, around=24), rotate_deg=-45, axis="y")
        plain = slice_mesh(tri, 3.0)
        optimized = slice_mesh(tri, 3.0, optimize_travel=True)
        self.assert_preserved(plain, optimized)
        self.assertLessEqual(optimized.travel_length(), plain.travel_length())

    def test_disabled_equality(self):
        tube = cylinder_mesh()
        meshes = [tube, np.concatenate([tube, tube + [100, 0, 0]])]
        for tri in meshes:
            for spiralize in (False, True):
                with self.subTest(contours=len(tri) // len(tube), spiralize=spiralize):
                    options = {"spiralize": True} if spiralize else {}
                    plain = slice_mesh(tri, 3.0, **options)
                    disabled = slice_mesh(tri, 3.0, optimize_travel=False, **options)
                    explicit = slice_mesh(tri, 3.0, spiralize=spiralize, optimize_travel=False)
                    self.assertEqual(plain.moves, disabled.moves)
                    self.assertEqual(plain.moves, explicit.moves)
                    self.assertEqual(to_gcode(plain), to_gcode(disabled))
                    self.assertEqual(to_gcode(plain), to_gcode(explicit))

    def test_spiralized_tube_unchanged(self):
        tri = cylinder_mesh()
        plain = slice_mesh(tri, 3.0, spiralize=True)
        optimized = slice_mesh(tri, 3.0, spiralize=True, optimize_travel=True)
        self.assertEqual(plain.moves, optimized.moves)
        self.assertEqual(to_gcode(plain), to_gcode(optimized))

    def test_mixed_spiralized_and_ordinary_layers(self):
        square = np.array([[0., 0., 0.], [4., 0., 0.], [4., 3., 0.],
                           [0., 3., 0.], [0., 0., 0.]])
        arc = np.array([[9., 0., 0.], [8., 2., 0.], [7., 0., 0.]])


        loops = [[square], [square], [arc], [arc], [square, square + [20, 0, 0]],
                 [square], [square]]
        with patch("waam_pipeline.slicer._chain_loops", side_effect=loops):
            plain = slice_mesh(cylinder_mesh(height=21), 3.0, spiralize=True)
        with patch("waam_pipeline.slicer._chain_loops", side_effect=loops):
            optimized = slice_mesh(cylinder_mesh(height=21), 3.0,
                                   spiralize=True, optimize_travel=True)
        self.assert_preserved(plain, optimized)
        def spiral_moves(tp):
            return [(m.target, m.deposit, m.feed, m.layer) for m in tp.moves
                    if m.layer in (0, 1, 5, 6)]
        self.assertEqual(spiral_moves(plain), spiral_moves(optimized))
        self.assertNotEqual(plain.moves, optimized.moves)

    def test_greedy_vertices_and_open_endpoints(self):
        square = np.array([[0., 0., 0.], [2., 0., 0.], [2., 2., 0.],
                           [0., 2., 0.], [0., 0., 0.]])
        arc = np.array([[8., 4., 0.], [2., 1., 0.], [8., 0., 0.]])
        near = square + [4, 0, 0]
        far = square + [20, 0, 0]
        loops = [[square], [far, arc, near]]
        with patch("waam_pipeline.slicer._chain_loops", side_effect=loops):
            plain = slice_mesh(cylinder_mesh(height=6), 3.0)
        with patch("waam_pipeline.slicer._chain_loops", side_effect=loops):
            optimized = slice_mesh(cylinder_mesh(height=6), 3.0, optimize_travel=True)
        self.assert_preserved(plain, optimized)
        entries = [m.target for m in optimized.moves if not m.deposit]
        self.assertEqual(entries, [(0., 0., 3.), (4., 0., 6.), (8., 0., 6.), (20., 2., 6.)])
        arc_start = next(i for i, m in enumerate(optimized.moves) if m.target == (8., 0., 6.))
        self.assertEqual([m.target for m in optimized.moves[arc_start + 1:arc_start + 3]],
                         [(2., 1., 6.), (8., 4., 6.)])


class TestRunIn(unittest.TestCase):
    @staticmethod
    def starts(tp):
        return [i for event, i in tp.arc_events() if event == "ARCON"]

    def assert_upper_unchanged(self, plain, lead):
        self.assertEqual([m for m in plain.moves if m.layer > 0],
                         [m for m in lead.moves if m.layer > 0])

    def assert_lead(self, plain, lead, before, after, distance=8.0):
        start, next_point = plain.moves[before - 1], plain.moves[before]
        direction = np.subtract(next_point.target[:2], start.target[:2])
        direction /= np.linalg.norm(direction)
        approach, entry = lead.moves[after - 1], lead.moves[after]
        np.testing.assert_allclose(approach.target[:2],
                                   np.asarray(start.target[:2]) - distance * direction, atol=1e-12)
        np.testing.assert_array_equal(entry.target[:2], start.target[:2])
        self.assertEqual((approach.z, entry.z), (start.z, start.z))
        self.assertEqual((approach.layer, entry.layer), (0, 0))
        self.assertFalse(approach.deposit)
        self.assertTrue(entry.deposit)
        self.assertEqual(entry.feed, next_point.feed)
        self.assertEqual(entry.line_no, start.line_no)
        self.assertAlmostEqual(np.linalg.norm(np.subtract(entry.target, approach.target)),
                               distance, delta=1e-9)

    def test_single_tube(self):
        tri = cylinder_mesh()
        plain = slice_mesh(tri, 3.0, feed_mm_min=237.0)
        lead = slice_mesh(tri, 3.0, feed_mm_min=237.0, run_in_mm=8.0)
        self.assertEqual((lead.moves[0].z, lead.moves[1].z), (3.0, 3.0))
        self.assertEqual(len(self.starts(plain)), 10)
        self.assertEqual(len(self.starts(lead)), len(self.starts(plain)))
        self.assertAlmostEqual(lead.deposit_length() - plain.deposit_length(), 8.0, delta=1e-9)
        self.assert_lead(plain, lead, self.starts(plain)[0], self.starts(lead)[0])
        self.assertEqual(plain.moves[1:], lead.moves[2:])
        self.assert_upper_unchanged(plain, lead)

    def test_every_substrate_contour_after_travel_ordering(self):
        tube = cylinder_mesh()
        tri = np.concatenate([tube, tube + [100, 0, 0]])
        for optimize in (False, True):
            with self.subTest(optimize_travel=optimize):
                plain = slice_mesh(tri, 3.0, optimize_travel=optimize)
                lead = slice_mesh(tri, 3.0, optimize_travel=optimize, run_in_mm=8.0)
                before, after = self.starts(plain), self.starts(lead)
                self.assertEqual(len(before), 20)
                self.assertEqual(len(after), len(before))
                self.assertEqual(len(lead.moves), len(plain.moves) + 2)
                self.assertAlmostEqual(lead.deposit_length() - plain.deposit_length(), 16.0, delta=1e-9)
                for a, b in zip(before, after):
                    if plain.moves[a].layer == 0:
                        self.assert_lead(plain, lead, a, b)
                self.assert_upper_unchanged(plain, lead)

    def test_spiralized_tube(self):
        tri = cylinder_mesh(around=80)
        for optimize in (False, True):
            with self.subTest(optimize_travel=optimize):
                plain = slice_mesh(tri, 3.0, spiralize=True, optimize_travel=optimize)
                lead = slice_mesh(tri, 3.0, spiralize=True, optimize_travel=optimize, run_in_mm=8.0)
                self.assertEqual(len(self.starts(plain)), 1)
                self.assertEqual(len(self.starts(lead)), 1)
                descending = [(a.z, b.z) for a, b in zip(lead.moves, lead.moves[1:])
                              if b.deposit and b.z < a.z - 1e-6]
                self.assertEqual(descending, [])
                self.assertEqual((lead.moves[0].z, lead.moves[1].z), (0.0, 0.0))
                self.assertAlmostEqual(lead.deposit_length() - plain.deposit_length(), 8.0, delta=1e-9)
                self.assert_lead(plain, lead, self.starts(plain)[0], self.starts(lead)[0])
                self.assertEqual(plain.moves[1:], lead.moves[2:])
                self.assertEqual(max(m.z for m in plain.moves), max(m.z for m in lead.moves))
                self.assert_upper_unchanged(plain, lead)

    def test_disabled_equality(self):
        tube = cylinder_mesh()
        for tri in (tube, np.concatenate([tube, tube + [100, 0, 0]])):
            for spiralize in (False, True):
                for optimize in (False, True):
                    with self.subTest(contours=len(tri) // len(tube),
                                      spiralize=spiralize, optimize_travel=optimize):
                        options = {"spiralize": spiralize, "optimize_travel": optimize}
                        plain = slice_mesh(tri, 3.0, **options)
                        disabled = slice_mesh(tri, 3.0, run_in_mm=0.0, **options)
                        self.assertEqual(plain.moves, disabled.moves)
                        self.assertEqual(to_gcode(plain), to_gcode(disabled))
        plain = slice_mesh(tube, 3.0)
        for options in ({"run_in_mm": 0.0}, {"run_in_mm": 0.0, "spiralize": False,
                                           "optimize_travel": False}):
            disabled = slice_mesh(tube, 3.0, **options)
            self.assertEqual(plain.moves, disabled.moves)
            self.assertEqual(to_gcode(plain), to_gcode(disabled))

    def test_invalid_distance(self):
        tri = cylinder_mesh()
        for distance in (-1.0, float("nan"), float("inf"), -float("inf")):
            with self.subTest(run_in_mm=distance):
                with self.assertRaisesRegex(ValueError, "run_in_mm must be finite and >= 0"):
                    slice_mesh(tri, 3.0, run_in_mm=distance)

    def test_later_starts_without_substrate_are_unchanged(self):


        plain = slice_mesh(cylinder_mesh(height=9), 3.0)
        upper = Toolpath([replace(m, layer=m.layer + 1) for m in plain.moves])
        lead = Toolpath(list(upper.moves))
        _add_run_ins(lead, 8.0)
        self.assertTrue(upper.moves)
        self.assertTrue(all(m.layer > 0 for m in upper.moves))
        self.assertEqual(upper.moves, lead.moves)
        self.assertEqual(to_gcode(upper), to_gcode(lead))

    def test_degenerate_initial_edges_use_first_valid_direction(self):
        arc = np.array([[0., 0., 0.], [0., 0., 0.], [1e-10, 0., 0.],
                        [2., 2., 0.], [4., 3., 0.]])
        with patch("waam_pipeline.slicer._chain_loops", return_value=[arc]), \
             patch("waam_pipeline.slicer._reduce_collinear", side_effect=lambda loop, _: loop):
            plain = slice_mesh(cylinder_mesh(height=3), 3.0)
            lead = slice_mesh(cylinder_mesh(height=3), 3.0, run_in_mm=8.0)
        direction = arc[3, :2] - arc[2, :2]
        direction /= np.linalg.norm(direction)
        np.testing.assert_allclose(lead.moves[0].target[:2], -8.0 * direction, atol=1e-12, rtol=0)
        self.assertEqual(plain.moves[1:], lead.moves[2:])
        self.assertEqual(len(self.starts(lead)), len(self.starts(plain)))

    def test_degenerate_contour_skips_run_in(self):
        point = np.zeros((3, 3))
        with patch("waam_pipeline.slicer._chain_loops", return_value=[point]):
            plain = slice_mesh(cylinder_mesh(height=3), 3.0)
            lead = slice_mesh(cylinder_mesh(height=3), 3.0, run_in_mm=8.0)
        self.assertEqual(plain.moves, lead.moves)
        self.assertEqual(to_gcode(plain), to_gcode(lead))


class TestGcodeExport(unittest.TestCase):
    def test_round_trip(self):
        tp = wall(80.0, layers=5, layer_height_mm=3.0)
        reparsed = parse(to_gcode(tp))
        self.assertEqual(
            sum(m.deposit for m in reparsed.moves), sum(m.deposit for m in tp.moves)
        )
        self.assertEqual(len(reparsed.layers()), 5)


if __name__ == "__main__":
    unittest.main()


class TestMachiningAllowance(unittest.TestCase):
    def test_cylinder_inflates_radially(self):
        from waam_pipeline.slicer import inflate_mesh

        tri = cylinder_mesh(radius=20.0, height=30.0)
        fat = inflate_mesh(tri, 2.0)
        r_orig = np.hypot(tri[..., 0], tri[..., 1]).max()
        r_fat = np.hypot(fat[..., 0], fat[..., 1]).max()
        self.assertAlmostEqual(r_fat - r_orig, 2.0, delta=0.15)

    def test_inflated_slices_wider(self):
        from waam_pipeline.slicer import inflate_mesh, slice_mesh

        tri = cylinder_mesh(radius=20.0, height=12.0)
        tp0 = slice_mesh(tri, layer_height_mm=3.0)
        tp2 = slice_mesh(inflate_mesh(tri, 2.0), layer_height_mm=3.0)
        (x0a, x1a), _, _ = tp0.bbox()
        (x0b, x1b), _, _ = tp2.bbox()
        self.assertGreater((x1b - x0b) - (x1a - x0a), 3.0)

    def test_stl_round_trip(self):
        import tempfile

        from waam_pipeline.slicer import load_stl, write_stl

        tri = cylinder_mesh(radius=10.0, height=8.0, around=16)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stock.stl"
            write_stl(tri, p)
            back = load_stl(p)
            self.assertEqual(back.shape, tri.shape)
            np.testing.assert_allclose(back, tri, atol=1e-3)


class TestSliceMeshRefusals(unittest.TestCase):


    def test_a_mesh_not_dropped_to_z0_is_refused(self):
        tri = cylinder_mesh(height=12.0) + [0.0, 0.0, 40.0]
        with self.assertRaises(ValueError) as cm:
            slice_mesh(tri, 3.0)
        self.assertIn("Z=0", str(cm.exception))

        self.assertTrue(slice_mesh(transform(tri), 3.0).moves)

    def test_a_cut_plane_with_no_contour_is_refused(self):
        square = np.array([[0., 0., 0.], [4., 0., 0.], [4., 3., 0.],
                           [0., 3., 0.], [0., 0., 0.]])
        with patch("waam_pipeline.slicer._chain_loops",
                   side_effect=[[square], [], [square]]):
            with self.assertRaises(ValueError) as cm:
                slice_mesh(cylinder_mesh(height=9), 3.0)
        self.assertIn("z=4.5 mm", str(cm.exception))

    def test_an_empty_toolpath_raises_a_named_error_not_a_bare_one(self):
        from waam_pipeline.correction import footprint, rotate_part
        from waam_pipeline.toolpath import EmptyToolpath

        empty = Toolpath()
        with self.assertRaises(EmptyToolpath):
            empty.bbox()
        with self.assertRaises(EmptyToolpath):
            empty.summary()
        with self.assertRaises(EmptyToolpath):
            rotate_part(empty, None, 90.0)
        with self.assertRaises(EmptyToolpath):
            footprint(empty, None, 9.0)

        self.assertIsInstance(EmptyToolpath("x"), ValueError)
