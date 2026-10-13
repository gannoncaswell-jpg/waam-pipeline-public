import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.arcs import segment_pass
from waam_pipeline.backends.jbi import emit_jbi_pulse
from waam_pipeline.demos import demo_bead
from waam_pipeline.plan import build_plan
from waam_pipeline.sensed import fit_sensed
from waam_pipeline.slicer import wall
from waam_pipeline.toolpath import Move, Toolpath
from tests.test_sensed_surface import drooping_plate


MOVC = re.compile(r"^MOVC C\d{5} V=\d+(\.\d+)?( FPT)?$")
MOVL = re.compile(r"^MOVL C\d{5} V=\d+(\.\d+)?( PL=1)?$")
BACKUP = ROOT / "docs/cell-data/controller-backup"


def circle(radius=25.0, start=0.0, stop=2 * np.pi, chords=80):
    angles = np.linspace(start, stop, chords + 1)
    return np.column_stack((radius * np.cos(angles), radius * np.sin(angles),
                            np.zeros(len(angles))))


def separated_arcs():
    first = circle(stop=np.pi / 2, chords=20)
    second = circle(stop=np.pi / 2, chords=20) + [100, 100, 0]
    return np.vstack((first, second))


def d_shape():
    x = np.linspace(-60, 0, 31)
    before = np.column_stack((x, np.full(len(x), -25), np.zeros(len(x))))
    curved = circle(start=-np.pi / 2, stop=np.pi / 2, chords=40)
    after = np.column_stack((x[::-1], np.full(len(x), 25), np.zeros(len(x))))
    return np.vstack((before, curved[1:], after[1:]))


def path_from_points(points, layers=1):
    moves = []
    for layer in range(layers):
        for j, point in enumerate(points):
            x, y, z = point + [0, 0, 2 * (layer + 1)]
            moves.append(Move(x, y, z, 325.0, j > 0, layer, len(moves) + 1))
    return Toolpath(moves)


def solve(xyz):
    return tuple(np.asarray(xyz) / 10) + (0.0, 0.0, 0.0)


def emit(path, **kwargs):
    with mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test"):
        return emit_jbi_pulse(path, solve, name="SMOOTH", date="2026/10/02 12:00",
                              arc_file=81, **kwargs)


def through_three(points):
    origin, middle, end = points
    a, b = middle - origin, end - origin
    normal = np.cross(a, b)
    center = origin + (np.dot(a, a) * np.cross(b, normal)
                       + np.dot(b, b) * np.cross(normal, a)) / (2 * np.dot(normal, normal))
    return center, np.linalg.norm(origin - center), normal / np.linalg.norm(normal)


class GeometryAssertions(unittest.TestCase):
    def assert_geometry(self, points, motion):
        points = np.asarray(points, float)
        self.assertEqual(len(motion.interp), len(points))
        self.assertEqual(len(motion.fpt), len(points))
        self.assertEqual(len(motion.pl), len(points))
        self.assertEqual((motion.interp[0], motion.fpt[0], motion.pl[0]),
                         ("linear", False, 0))
        self.assertEqual(list(motion.separators), sorted(set(motion.separators)))
        rendered = set()
        arc_interior = set()
        distances = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
        for first, mid, last in motion.arcs:
            self.assertGreaterEqual(first, 1)
            self.assertLess(first, mid)
            self.assertLess(mid, last)
            rendered.update((first, mid, last))
            arc_interior.update(range(first, last + 1))
            self.assertGreaterEqual((distances[mid] - distances[first]) /
                                    (distances[last] - distances[first]), .25 - 1e-10)
            self.assertLessEqual((distances[mid] - distances[first]) /
                                 (distances[last] - distances[first]), .75 + 1e-10)
            center, radius, normal = through_three(points[[first, mid, last]])
            self.assertGreaterEqual(radius, 3 - 1e-8)
            self.assertLessEqual(radius, 2000 + 1e-8)
            vertices = points[first:last + 1]
            samples = np.vstack((vertices, (vertices[:-1] + vertices[1:]) / 2))
            offsets = samples - center
            heights = offsets @ normal
            radial = np.linalg.norm(offsets - heights[:, None] * normal, axis=1)
            error = np.hypot(radial - radius, heights)
            self.assertLessEqual(float(error.max()), .2 + 1e-8)
            u = (points[first] - center) / radius
            v = np.cross(normal, u)
            angles = np.unwrap(np.arctan2((vertices - center) @ v, (vertices - center) @ u))
            self.assertTrue(np.all(np.diff(angles) > 0) or np.all(np.diff(angles) < 0))
            self.assertLessEqual(abs(angles[-1] - angles[0]), np.pi + 1e-8)
            self.assertTrue(np.all(np.linalg.norm(np.diff(vertices, axis=0), axis=1) >= .01))
        self.assertEqual({i for i, kind in enumerate(motion.interp) if kind == "circular"},
                         rendered)
        self.assertEqual({i for i, kind in enumerate(motion.interp) if kind == "drop"},
                         arc_interior - rendered)
        for j, fpt in enumerate(motion.fpt):
            if fpt:
                self.assertEqual(motion.interp[j], "circular")
                self.assertTrue(any(last == j for _, _, last in motion.arcs))
                self.assertTrue(any(first == j for first, _, _ in motion.arcs))
        for j, pl in enumerate(motion.pl):
            self.assertIn(pl, (0, 1))
            if not pl:
                continue
            self.assertEqual(motion.interp[j], "linear")
            self.assertLess(j, len(points) - 1)
            nxt = next(k for k in range(j + 1, len(points)) if motion.interp[k] != "drop")
            self.assertEqual(motion.interp[nxt], "linear")
            self.assertFalse(any(j < sep <= nxt for sep in motion.separators))
            lo, hi = max(0, distances[j] - 25), min(distances[-1], distances[j] + 25)
            ends = np.array([[np.interp(s, distances, points[:, axis]) for axis in range(3)]
                             for s in (lo, hi)])
            window = points[(distances >= lo) & (distances <= hi)]
            direction = ends[1] - ends[0]
            errors = np.linalg.norm(np.cross(window - ends[0], direction), axis=1)
            self.assertLessEqual(float(errors.max()) / np.linalg.norm(direction), .1 + 1e-8)


class TestSegmentPass(GeometryAssertions):
    def test_full_circle_splits_at_180_and_chains_three_point_arcs(self):
        points = circle()
        motion = segment_pass(points)
        self.assertEqual(len(motion.arcs), 2)
        self.assertEqual(sum(motion.fpt), 1)
        self.assertEqual(motion.arcs[0][2], motion.arcs[1][0])
        self.assertEqual(sum(kind == "circular" for kind in motion.interp), 5)
        self.assertFalse(motion.separators)
        self.assertFalse(any(motion.pl))
        self.assert_geometry(points, motion)
        with self.assertRaises(FrozenInstanceError):
            motion.interp = []

    def test_half_millimetre_outlier_breaks_the_circle(self):
        points = circle()
        points[20] *= 25.5 / 25
        motion = segment_pass(points)
        self.assertGreater(len(motion.arcs), 2)
        self.assertNotEqual(motion.interp[20], "drop")
        self.assert_geometry(points, motion)

    def test_straight_pass_blends_every_interior_target(self):
        points = np.column_stack((np.arange(0, 101, 2), np.zeros((51, 2))))
        motion = segment_pass(points)
        self.assertFalse(motion.arcs)
        self.assertEqual(list(motion.pl), [0] + [1] * 49 + [0])
        self.assert_geometry(points, motion)

    def test_d_shape_combines_lines_and_arcs(self):
        points = d_shape()
        motion = segment_pass(points)
        self.assertTrue(motion.arcs)
        self.assertTrue(any(motion.pl))
        self.assert_geometry(points, motion)

    def test_one_chord_between_arcs_gets_exactly_one_separator(self):
        points = separated_arcs()
        motion = segment_pass(points)
        self.assertEqual(len(motion.arcs), 2)
        self.assertEqual(list(motion.separators), [21])
        self.assertFalse(any(motion.fpt))
        self.assert_geometry(points, motion)

    def test_ninety_degree_arc_junction_is_not_fpt(self):
        first = circle(stop=np.pi / 2, chords=20)
        second = circle(stop=np.pi / 2, chords=20) + [-25, 25, 0]
        points = np.vstack((first, second[1:]))
        motion = segment_pass(points)
        self.assertGreaterEqual(len(motion.arcs), 2)
        self.assertFalse(motion.fpt[20])
        self.assert_geometry(points, motion)

    def test_tangent_different_radius_arcs_can_chain(self):
        first = circle(stop=np.pi / 2, chords=20)
        second = circle(radius=10, start=np.pi / 2, stop=np.pi, chords=15) + [0, 15, 0]
        points = np.vstack((first, second[1:]))
        motion = segment_pass(points)
        self.assertGreaterEqual(len(motion.arcs), 2)
        self.assertTrue(any(motion.fpt))
        self.assert_geometry(points, motion)

    def test_circle_on_three_degree_plate_still_fits(self):
        angle = np.radians(3)
        rotation = np.array([[1, 0, 0], [0, np.cos(angle), -np.sin(angle)],
                             [0, np.sin(angle), np.cos(angle)]])
        points = circle() @ rotation.T
        motion = segment_pass(points)
        self.assertEqual(len(motion.arcs), 2)
        self.assert_geometry(points, motion)

    def test_nonplanar_pass_gets_arcs_only_within_tolerance(self):


        for rise in (1.0, 20.0):
            points = circle()
            points[:, 2] = np.linspace(0, rise, len(points))
            motion = segment_pass(points)
            self.assertTrue(motion.arcs)
            self.assert_geometry(points, motion)
        wobble = circle()
        wobble[:, 2] = 0.4 * np.sin(np.linspace(0, 2 * np.pi, len(wobble)))
        self.assert_geometry(wobble, segment_pass(wobble))

    def test_out_of_plane_noise_stays_linear(self):
        points = circle()
        points[1::2, 2] += 0.5
        motion = segment_pass(points)
        self.assertFalse(motion.arcs)
        self.assertFalse(any(motion.fpt))
        self.assert_geometry(points, motion)

    def test_radius_above_limit_stays_linear_and_blends(self):
        points = circle(radius=5000, start=-.01, stop=.01, chords=50)
        motion = segment_pass(points)
        self.assertFalse(motion.arcs)
        self.assertTrue(all(motion.pl[1:-1]))
        self.assert_geometry(points, motion)

    def test_radius_below_limit_stays_linear(self):
        self.assertFalse(segment_pass(circle(radius=2, chords=40)).arcs)

    def test_radius_bounds_are_inclusive(self):
        for points in (circle(radius=3), circle(radius=2000, stop=.05, chords=50)):
            with self.subTest(radius=np.linalg.norm(points[0])):
                self.assertTrue(segment_pass(points).arcs)

    def test_sharp_straight_corner_does_not_blend_inside_window(self):
        x = np.arange(0, 61, 2)
        first = np.column_stack((x, np.zeros((len(x), 2))))
        second = np.column_stack((np.full(len(x), 60), x, np.zeros(len(x))))
        points = np.vstack((first, second[1:]))
        motion = segment_pass(points)
        self.assertTrue(motion.pl[1])
        self.assertFalse(any(motion.pl[29:32]))
        self.assert_geometry(points, motion)

    def test_only_midpoint_outside_quarter_split_refuses_arc(self):
        angles = np.array([-.08, 0, .001, .12])
        points = np.column_stack((25 * np.cos(angles), 25 * np.sin(angles), np.zeros(4)))
        self.assertFalse(segment_pass(points).arcs)

    def test_short_chord_cannot_be_hidden_inside_an_arc(self):
        points = circle(stop=np.pi / 2, chords=20)
        points[10] = points[9] + (points[10] - points[9]) * .001
        motion = segment_pass(points)
        self.assertFalse(any(first < 10 <= last for first, _, last in motion.arcs))
        self.assert_geometry(points, motion)

    def test_coarse_chords_fail_circle_sag_bound(self):
        points = circle(chords=8)
        self.assertFalse(segment_pass(points).arcs)

    def test_reversal_is_never_hidden_inside_an_arc(self):
        points = circle(stop=np.pi / 2, chords=20)
        points[[10, 11]] = points[[11, 10]]
        motion = segment_pass(points)
        self.assertFalse(any(first <= 9 and last >= 12 for first, _, last in motion.arcs))
        self.assert_geometry(points, motion)


class TestSmoothPlan(unittest.TestCase):
    def test_disabled_plan_is_identical_including_default_fields(self):
        path = path_from_points(circle(), layers=3)
        self.assertEqual(build_plan(path, solve), build_plan(path, solve, smooth=False))
        for step in build_plan(path, solve):
            self.assertEqual((step.interp, step.fpt, step.pl), ("linear", False, 0))

    def test_separator_and_dropped_points_are_solved_in_execution_order(self):
        points = separated_arcs()
        path = path_from_points(points)
        calls = []

        def record(xyz):
            calls.append(tuple(xyz))
            return solve(xyz)

        steps = build_plan(path, record, smooth=True)
        self.assertEqual(calls, [step.xyz for step in steps if step.xyz is not None])
        separators = [step for step in steps if step.label.startswith("separator ")]
        self.assertEqual(len(separators), 1)
        separator = separators[0]
        np.testing.assert_allclose(separator.xyz, (points[20] + points[21]) / 2 + [0, 0, 2])
        self.assertEqual((separator.role, separator.layer, separator.feed_mm_min,
                          separator.interp, separator.fpt, separator.pl),
                         ("weld", 0, 325.0, "linear", False, 0))
        for point in points:
            self.assertIn(tuple(point + [0, 0, 2]), calls)

    def test_separator_clearance_guard_can_refuse(self):
        def guard(xyz, where):
            if where.startswith("separator "):
                raise ValueError("separator obstructed")

        with self.assertRaisesRegex(ValueError, "separator obstructed"):
            build_plan(path_from_points(separated_arcs()), solve, smooth=True, guard=guard)

    def test_dropped_vertex_still_checks_ik_branch_continuity(self):
        points = circle()
        motion = segment_pass(points)
        j = next(j for j, kind in enumerate(motion.interp) if kind == "drop")

        def discontinuous(xyz):
            if np.array_equal(xyz, points[j] + [0, 0, 2]):
                return (100.0, 0, 0, 0, 0, 0)
            return solve(xyz)

        with self.assertRaisesRegex(ValueError, "IK branch discontinuity"):
            build_plan(path_from_points(points), discontinuous, smooth=True)

    def test_separator_still_checks_ik_branch_continuity(self):
        points = separated_arcs()
        midpoint = (points[20] + points[21]) / 2 + [0, 0, 2]

        def discontinuous(xyz):
            if np.array_equal(xyz, midpoint):
                return (100.0, 0, 0, 0, 0, 0)
            return solve(xyz)

        with self.assertRaisesRegex(ValueError, "IK branch discontinuity"):
            build_plan(path_from_points(points), discontinuous, smooth=True)


class TestSmoothRenderer(unittest.TestCase):
    def assert_rendering(self, path):
        text = emit(path, smooth=True)
        instructions = text.split("//INST\r\n", 1)[1].splitlines()
        moves = [line for line in instructions if line.startswith(("MOVJ ", "MOVL ", "MOVC "))]
        records = re.findall(r"^C\d{5}=", text, re.MULTILINE)
        npos = int(re.search(r"///NPOS (\d+),", text).group(1))
        self.assertEqual(npos, len(moves))
        self.assertEqual(npos, len(records))
        self.assertEqual(len({line.split()[1] for line in moves}), len(moves))
        plan = build_plan(path, solve, smooth=True)
        rendered = [step for step in plan if step.is_motion and step.interp != "drop"]
        self.assertEqual(len(moves), len(rendered))
        for line, step in zip(moves, rendered):
            if line.startswith("MOVC "):
                self.assertRegex(line, MOVC)
            elif line.startswith("MOVL "):
                self.assertRegex(line, MOVL)
            if step.role != "weld":
                self.assertFalse(line.startswith("MOVC "))
                self.assertNotIn(" PL=", line)
                self.assertNotIn(" FPT", line)
            else:
                self.assertEqual(line.startswith("MOVC "), step.interp == "circular")
                self.assertEqual(line.endswith(" FPT"), step.fpt)
                self.assertEqual(line.endswith(" PL=1"), step.pl == 1)
        previous_move = None
        run = []
        arc_on = False
        for i, line in enumerate(instructions):
            if line.startswith("ARCON "):
                arc_on = True
            if line.startswith("MOVC "):
                self.assertTrue(arc_on)
                if not run:
                    self.assertTrue(previous_move.startswith("MOVL "))
                run.append(line)
            elif run:
                self.assertGreaterEqual(len(run), 3)
                self.assertEqual(len(run) % 2, 1)
                for j, circular in enumerate(run):
                    self.assertEqual(circular.endswith(" FPT"), 2 <= j < len(run) - 1 and j % 2 == 0)
                run = []
            if line.startswith("MOVL ") and line.endswith(" PL=1"):
                self.assertTrue(arc_on)
                self.assertTrue(line.startswith("MOVL "))
                self.assertTrue(instructions[i + 1].startswith("MOVL "))
            if line.startswith("ARCOF "):
                self.assertTrue(instructions[i - 1].startswith(("MOVL ", "MOVC ")))
                self.assertNotIn(" PL=", instructions[i - 1])
                arc_on = False
            if line.startswith(("MOVJ ", "MOVL ", "MOVC ")):
                previous_move = line
        comments = [line for line in instructions if line.startswith("' smooth:")]
        self.assertEqual(len(comments), 1)
        self.assertLessEqual(len(comments[0]), 32)
        arcs = (sum(step.interp == "circular" for step in rendered)
                + sum(step.fpt for step in rendered)) // 3
        self.assertEqual(comments[0], f"' smooth: {arcs} arcs, {sum(step.pl for step in rendered)} PL=1")
        return text

    def test_three_layer_circular_wall(self):
        text = self.assert_rendering(path_from_points(circle(), layers=3))
        self.assertEqual(text.count("ARCON ASF#(81)"), 3)
        self.assertEqual(text.count(" FPT\r\n"), 3)
        self.assertIn("' smooth: 6 arcs, 0 PL=1", text)

    def test_mixed_lines_arcs_and_unchained_separators(self):
        self.assert_rendering(path_from_points(d_shape()))
        self.assert_rendering(path_from_points(separated_arcs()))

    def test_disabled_emission_is_identical(self):
        path = path_from_points(circle(), layers=3)
        self.assertEqual(emit(path), emit(path, smooth=False))

    def test_unseated_wall_and_bead_set_only_gain_provenance(self):
        for path in (wall(100, 3, 2), demo_bead(100, 2, 325, count=4, pitch_mm=25)):
            with self.subTest(passes=len(path.layers())):
                smooth = emit(path, smooth=True)
                self.assertIn("' smooth: 0 arcs, 0 PL=1\r\n", smooth)
                self.assertEqual(smooth.replace("' smooth: 0 arcs, 0 PL=1\r\n", ""),
                                 emit(path, smooth=False))

    def test_sensed_straight_wall_blends_its_split_points_without_movc(self):
        corners, records, _ = drooping_plate(curvature=8e-5)
        plate = fit_sensed(corners, records)
        path = plate.place(wall(150, 3, 2), ox=0, oy=0)
        self.assertGreater(sum(move.deposit for move in path.moves), 3)
        text = self.assert_rendering(path)
        self.assertNotIn("\r\nMOVC ", text)
        self.assertIn(" PL=1\r\n", text)
        self.assertTrue(any(move.seat != 0 for move in path.moves))
        blended = sum(line.startswith("MOVL ") and line.endswith(" PL=1")
                      for line in text.splitlines())
        self.assertEqual(blended, sum(move.deposit for move in path.moves) - 3)

    def test_real_controller_motion_grammar_matches_emitted_lines(self):
        paths = [BACKUP / "industrial-text-export" / name
                 for name in ("DEACON.JBI", "CA.JBI", "SIMPSTSQA.JBI")]
        paths.append(BACKUP / "cobot-hc10-dump-2026-09-15/---JEFFRESS-S-SHAPE.JBI")
        for path in paths:
            text = path.read_text(encoding="ascii")
            lines = [line for line in text.splitlines() if line.startswith(("MOVC ", "MOVL "))]
            self.assertTrue(lines, path)
            for line in lines:
                self.assertRegex(line, MOVC if line.startswith("MOVC ") else MOVL)
        self.assert_rendering(path_from_points(d_shape(), layers=3))

    def test_both_controller_backups_pin_positioning_zones(self):
        for folder in ("yaskawa1-binary-backup", "cobot-hc10-dump-2026-09-15"):
            text = (BACKUP / folder / "ALL.PRM").read_text(encoding="ascii", errors="replace")
            section = text.split("///S1C1G\n", 1)[1].split("///", 1)[0]
            values = [int(value) for value in re.findall(r"-?\d+", section)]
            self.assertEqual(values[33:41], [12500, 25000, 50000, 100000,
                                            200000, 300000, 400000, 500000])


if __name__ == "__main__":
    unittest.main()
