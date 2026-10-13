from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, platter_map, var_dat
from waam_pipeline.robots import ar1440


def pulse_records(points):
    solve = cell_lc.pulse_solver(np.zeros(3))
    return [np.rint(np.asarray(solve(point)) * ar1440.PULSES_PER_DEG).astype(int).tolist()
            for point in points]


def var_text(points, base=44):
    fixture = ROOT / "docs/cell-data/var-exports/VAR_BOP_LINE_V10_2026-09-25.DAT"
    lines = fixture.read_text(encoding="latin-1").splitlines()
    start = lines.index("///P") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("///"))
    position_lines = ['"UNUSED"'] * (end - start)
    for index, record in enumerate(pulse_records(points), base):
        axes = ",".join(str(p) for p in record)
        position_lines[index] = f'"PULSE"0,0,0,000000000000000000000000,0,0,{axes},0,0'
    return "\r\n".join(lines[:start] + position_lines + lines[end:]) + "\r\n"


def square_corners(half_span=120.0):
    offsets = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float)
    return np.array([1014.0, -300.0, cell_lc.PLATTER_FACE_Z]) + half_span * offsets


def synthetic_records(plan, coefficients, offsets, drift):
    result = []
    for job, probes in enumerate(plan.jobs):
        points = []
        for index, probe in enumerate(probes):
            point = probe.expected.copy()
            u, v = ((point - plan.origin) @ plan.basis)[:2]
            terms = np.array([1, u, v, u * u, u * v, v * v])
            point[2] = terms @ coefficients + offsets[job] + drift * index
            points.append(point)
        result.append(var_dat.probe_records(var_text(points), 44, len(points)))
    return result


class PlatterGeometryTests(unittest.TestCase):
    def test_corner_export_decodes_robot_coordinates(self):
        expected = square_corners()
        records = var_dat.probe_records(var_text(expected, base=40), 40, 4)
        actual = platter_map.corners_from_records(records)
        np.testing.assert_allclose(actual, expected, atol=0.02)

    def test_corner_guards(self):
        corners = square_corners()
        steep = corners.copy()
        steep[:, 2] += 0.12 * (steep[:, 0] - 1014.0)
        warped = corners.copy()
        warped[:, 2] += [3, -3, 3, -3]
        line = np.array([[980 + 20 * i, -300, corners[0, 2]] for i in range(4)])
        for label, points in (("crossed", corners[[0, 2, 1, 3]]),
                              ("steep", steep), ("warped", warped), ("line", line),
                              ("duplicate", corners[[0, 1, 1, 3]])):
            with self.subTest(label=label), self.assertRaises(ValueError):
                platter_map.corners_from_records(pulse_records(points))
        with self.assertRaises(ValueError):
            platter_map.corners_from_records(pulse_records(corners[:3]))

    def test_circle_recovery_in_tilted_corner_plane(self):
        corners = square_corners()
        corners[:, 2] += (corners[:, 0] - 1014) * 0.025 - (corners[:, 1] + 300) * 0.018
        normal = np.array([-0.025, 0.018, 1.0])
        normal /= np.linalg.norm(normal)
        u = np.array([1.0, 0.0, 0.025])
        u /= np.linalg.norm(u)
        v = np.cross(normal, u)
        center = corners.mean(axis=0) + 23 * u - 17 * v
        for count in (3, 4):
            angle = np.arange(count) * (2 * np.pi / count)
            points = center + 18 * (np.cos(angle)[:, None] * u + np.sin(angle)[:, None] * v)
            records = var_dat.probe_records(var_text(points), 44, count)
            hole, = platter_map.holes_from_records(records, count, corners=corners)
            np.testing.assert_allclose(hole["center"], center, atol=0.025)
            self.assertAlmostEqual(hole["radius"], 18.0, delta=0.02)
            self.assertLess(hole["residual"], 0.02)

    def test_circle_groups_and_rejections(self):
        corners = square_corners()
        center = corners.mean(axis=0)
        directions = np.array([[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0]], float)
        points = np.vstack((center + directions * 12, center + [55, 30, 0] + directions * 20))
        holes = platter_map.holes_from_records(pulse_records(points), 4, corners=corners)
        self.assertEqual(len(holes), 2)
        np.testing.assert_allclose([h["radius"] for h in holes], [12, 20], atol=0.02)
        for radius in (1, 65):
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                platter_map.holes_from_records(pulse_records(center + radius * directions),
                                              4, corners=corners)
        missed = center + 12 * directions
        missed[0, 0] += 8
        with self.assertRaises(ValueError):
            platter_map.holes_from_records(pulse_records(missed), 4, corners=corners)
        with self.assertRaises(ValueError):
            platter_map.holes_from_records(pulse_records(points[:5]), 4, corners=corners)

    def test_clustered_rim_touches_do_not_span_the_hole(self):
        corners = square_corners()
        center = corners.mean(axis=0)
        for angles in ((-15, 0, 15), (-15, -5, 5, 15)):
            angle = np.radians(angles)
            points = center + 18 * np.column_stack((np.cos(angle), np.sin(angle),
                                                    np.zeros(len(angle))))
            with self.subTest(angles=angles), self.assertRaisesRegex(
                    ValueError, r"spread the rim touches evenly around hole 1"):
                platter_map.holes_from_records(pulse_records(points), len(angle), corners=corners)


class PlatterPlanTests(unittest.TestCase):
    def setUp(self):
        self.corners = square_corners()
        self.holes = [{"center": self.corners.mean(axis=0), "radius": 20.0, "residual": 0.0}]

    def test_grid_avoids_holes_and_edges_and_is_near_target(self):
        plan = platter_map.map_plan(self.corners, self.holes, target=100)
        self.assertGreaterEqual(len(plan.points), 90)
        self.assertLessEqual(len(plan.points), 110)
        self.assertEqual(len(np.unique(np.round(plan.points, 6), axis=0)), len(plan.points))
        for point in plan.points:
            self.assertGreaterEqual(np.linalg.norm(point[:2] - self.holes[0]["center"][:2]),
                                    29.0 - 1e-7)
            self.assertTrue(np.all(point[:2] >= self.corners[0, :2] + 10 - 1e-7))
            self.assertTrue(np.all(point[:2] <= self.corners[2, :2] - 10 + 1e-7))
        local = (plan.points - plan.origin) @ plan.basis
        rows = np.unique(np.round(local[:, 1], 6))
        spacings = np.diff(rows)
        self.assertTrue(np.allclose(spacings, spacings[0], atol=1e-5))
        directions = []
        for row in rows:
            points = local[np.isclose(local[:, 1], row, atol=1e-5), 0]
            if len(points) > 1:
                steps = np.diff(points)
                self.assertTrue(np.all(steps > 0) or np.all(steps < 0))
                directions.append(np.sign(steps[0]))
        self.assertTrue(all(a != b for a, b in zip(directions, directions[1:])))

    def test_jobs_reserve_repeat_ties_and_p_variables(self):
        plan = platter_map.map_plan(self.corners, self.holes, target=100)
        self.assertGreaterEqual(np.linalg.norm(plan.tie_points[0] - plan.tie_points[1]), 100)
        new_points = []
        for job_index, (probes, roles) in enumerate(zip(plan.jobs, plan.roles), 1):
            self.assertLessEqual(len(probes), 20)
            self.assertEqual(roles[:2], ["tie", "tie"])
            self.assertEqual(roles[-2:], ["tie", "tie"])
            np.testing.assert_allclose([p.expected for p in probes[:2]], plan.tie_points)
            np.testing.assert_allclose([p.expected for p in probes[-2:]], plan.tie_points)
            for index, (probe, role) in enumerate(zip(probes, roles)):
                self.assertEqual(probe.name, f"j{job_index}_{index:02d}")
                np.testing.assert_allclose(probe.start - probe.expected, [0, 0, 25])
                np.testing.assert_allclose(probe.target - probe.expected, [0, 0, -5])
                if role == "new":
                    new_points.append(probe.expected)
        surveyed = np.vstack((plan.tie_points, new_points))
        self.assertEqual(len(surveyed), len(plan.points))
        for point in plan.points:
            self.assertTrue(np.any(np.linalg.norm(surveyed - point, axis=1) < 1e-7))

    def test_argument_guards(self):
        for field, values in (("hop_mm", (1.9, 25, float("nan"))),
                              ("max_bow_mm", (0.4, 10.1, float("nan"))),
                              ("per_job", (3, 21, 12.5)), ("ties", (0, 1, 3))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    platter_map.map_plan(self.corners, [], **{field: value})
        for hop in (5.0, 20.0):
            self.assertEqual(platter_map.map_plan(self.corners, [], hop_mm=hop).hop_mm, hop)

    def test_surface_bound_controls_hop_and_survives_json(self):
        plan = platter_map.map_plan(self.corners, [])
        document = platter_map.plan_json(plan)
        self.assertEqual(plan.max_bow_mm, 2.0)
        self.assertEqual(document["max_bow_mm"], 2.0)
        self.assertEqual(platter_map.load_plan(document).max_bow_mm, 2.0)
        for bow, hop in ((2.0, 4.9), (6.0, 5.0), (10.0, 12.9)):
            with self.subTest(bow=bow, hop=hop), self.assertRaisesRegex(
                    ValueError, r"a hop must clear the worst surface departure from the corner plane plus 3 mm"):
                platter_map.map_plan(self.corners, [], max_bow_mm=bow, hop_mm=hop)
        for bow in (0.5, 10.0):
            with self.subTest(bow=bow):
                plan = platter_map.map_plan(self.corners, [], max_bow_mm=bow, hop_mm=bow + 3)
                self.assertEqual(plan.max_bow_mm, bow)
        for field, value in (("max_bow_mm", 0.4), ("max_bow_mm", 10.1),
                             ("max_bow_mm", 6.0), ("hop_mm", 25)):
            changed = json.loads(json.dumps(document))
            changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                platter_map.load_plan(changed)
        document.pop("max_bow_mm")
        with self.assertRaises(ValueError):
            platter_map.load_plan(document)

    def test_hole_exclusion_includes_fit_uncertainty(self):
        holes = [{**self.holes[0], "residual": 0.6}]
        plan = platter_map.map_plan(self.corners, holes)
        exclusion = 20 + 8 + 3 * 0.6 + 1
        document = platter_map.plan_json(plan)
        self.assertAlmostEqual(document["holes"][0]["exclusion_radius_mm"], exclusion)
        self.assertGreaterEqual(np.linalg.norm(plan.points[:, :2] - holes[0]["center"][:2],
                                              axis=1).min(), exclusion - 1e-7)
        self.assertEqual(platter_map.plan_json(platter_map.load_plan(document)), document)
        document["holes"][0]["exclusion_radius_mm"] -= 1
        with self.assertRaises(ValueError):
            platter_map.load_plan(document)

    def test_ties_use_100_mm_separation_when_available(self):
        plan = platter_map.map_plan(square_corners(80), [], target=25)
        distances = np.linalg.norm(plan.points[:, None] - plan.points[None, :], axis=2)
        self.assertGreater(float(distances.max()), 100)
        self.assertGreaterEqual(np.linalg.norm(plan.tie_points[0] - plan.tie_points[1]), 100)

    def test_ties_only_cannot_identify_the_surface(self):
        corners = self.corners.copy()
        corners[:, 1] = -300 + (corners[:, 1] + 300) / 2
        with self.assertRaises(ValueError):
            platter_map.map_plan(corners, [], target=2, per_job=4)
        with self.assertRaises(ValueError):
            platter_map.map_plan(self.corners, [], target=10, per_job=4)

    def test_quadratic_grid_requires_three_values_on_both_axes(self):
        for axis in (0, 1):
            corners = self.corners.copy()
            center = corners[:, axis].mean()
            corners[:, axis] = center + (corners[:, axis] - center) / 2
            with self.subTest(axis=axis), self.assertRaisesRegex(ValueError, r"(?i)(distinct|independent|underdetermined)"):
                platter_map.map_plan(corners, [], target=6)

    def test_ill_conditioned_planned_design_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"condition number .* exceeds 1e8"):
            platter_map.map_plan(square_corners(100000), [], target=25)

    def test_skew_quad_keeps_clear_of_each_edge(self):
        shape = ((-120, -100), (110, -115), (95, 110), (-80, 120))
        corners = np.column_stack((np.asarray(shape) + [1014, -300],
                                   np.full(4, cell_lc.PLATTER_FACE_Z)))
        plan = platter_map.map_plan(corners, [], target=100)
        self.assertGreaterEqual(len(plan.points), 90)
        self.assertLessEqual(len(plan.points), 110)
        for a, b in zip(corners, np.roll(corners, -1, axis=0)):
            segment = b - a
            fraction = np.clip((plan.points - a) @ segment / np.dot(segment, segment), 0, 1)
            nearest = a + fraction[:, None] * segment
            self.assertGreaterEqual(np.linalg.norm(plan.points - nearest, axis=1).min(),
                                    10.0 - 1e-7)

    def test_concave_quad_is_refused_for_straight_hops(self):
        shape = ((-120, -120), (120, -120), (-30, -20), (-120, 120))
        corners = np.column_stack((np.asarray(shape) + [1014, -300],
                                   np.full(4, cell_lc.PLATTER_FACE_Z)))
        message = r"the four corners must form a convex region: hops between stations travel straight"
        with self.assertRaisesRegex(ValueError, message):
            platter_map.map_plan(corners, [])
        with self.assertRaisesRegex(ValueError, message):
            platter_map.corners_from_records(pulse_records(corners))

    def test_json_roundtrip_preserves_replay_and_roles(self):
        plan = platter_map.map_plan(self.corners, self.holes, target=32, per_job=12)
        document = platter_map.plan_json(plan)
        restored = platter_map.load_plan(json.loads(json.dumps(document)))
        self.assertEqual(platter_map.plan_json(restored), document)
        self.assertEqual(len(document["probes"]), sum(map(len, plan.jobs)))
        for row in document["probes"]:
            self.assertEqual(row["pvar"], 44 + row["index"])
            self.assertIn(row["role"], ("tie", "new"))
            self.assertEqual(len(row["expected"]), 3)

    def test_json_rejects_edited_result_slots_and_search_windows(self):
        document = platter_map.plan_json(platter_map.map_plan(self.corners, [], target=25))
        for field, value in (("pvar", 64), ("role", "new"), ("expected", [0, 0, 0]),
                             ("target", [1014, -300, -100]), ("start", [1014, -300, 100])):
            changed = json.loads(json.dumps(document))
            changed["probes"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                platter_map.load_plan(changed)
        changed = json.loads(json.dumps(document))
        changed["probes"].pop()
        with self.assertRaises(ValueError):
            platter_map.load_plan(changed)

    def test_rendered_jobs_obey_controller_search_invariants(self):
        plan = platter_map.map_plan(square_corners(80), [], target=12, per_job=10)
        jobs = platter_map.render_jobs(plan, date="2026/09/28 12:00")
        self.assertEqual(list(jobs), [f"PLATMAP{i}" for i in range(1, len(plan.jobs) + 1)])
        for name, text in jobs.items():
            job = int(name.removeprefix("PLATMAP")) - 1
            self.assertIn("CALL JOB:CLEAN_R1", text)
            self.assertLess(text.index("CALL JOB:CLEAN_R1"), text.index("SRCH"))
            positions = re.findall(r"^C\d{5}=", text, re.MULTILINE)
            npos = re.search(r"^///NPOS (\d+),", text, re.MULTILINE)
            self.assertIsNotNone(npos)
            self.assertEqual(int(npos.group(1)), len(positions))
            moves = re.findall(r"^MOV[JL]\s+(C\d{5})\b", text, re.MULTILINE)
            self.assertEqual(len(moves), len(positions))
            self.assertEqual(len(set(moves)), len(moves))
            searches = [line for line in text.splitlines() if " SRCH " in line]
            self.assertEqual(len(searches), len(plan.jobs[job]))
            for line in searches:
                self.assertRegex(line, r"V=10(?:\.0+)?\b")
                self.assertIn("RIN#(3)", line)
                self.assertRegex(line, r"DIS=5(?:\.0+)?\b")
            captured = [int(value) for value in re.findall(r"GETS PX(\d+) \$PX002", text)]
            self.assertEqual(captured, list(range(44, 44 + len(searches))))
            targets = [int(re.search(r"MOVL P(\d+)", line).group(1)) for line in searches]
            self.assertEqual(targets, captured)
            for line in text.splitlines():
                if line.startswith("'"):
                    self.assertLessEqual(len(line), 32)
        with self.assertRaises(ValueError):
            platter_map.render_jobs(plan, name_prefix="P" * 32, date="2026/09/28 12:00")


class PlatterReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = platter_map.map_plan(square_corners(), [], target=100)
        cls.coefficients = np.array([cell_lc.PLATTER_FACE_Z + 0.15, 0.002, -0.003,
                                     0.00008, 0.000015, 0.00003])
        cls.offsets = 0.17 * np.sin(np.arange(len(cls.plan.jobs)))
        cls.drift = 0.004
        cls.records = synthetic_records(cls.plan, cls.coefficients, cls.offsets, cls.drift)

    def test_recovers_tilt_bow_job_offsets_and_drift(self):
        summary = platter_map.fit_map(self.plan, self.records)
        np.testing.assert_allclose(summary["poly_coefficients"], self.coefficients,
                                   atol=0.008, rtol=0.003)
        np.testing.assert_allclose(summary["poly_coefficients"][1:3], self.coefficients[1:3],
                                   atol=0.00003)
        np.testing.assert_allclose(summary["poly_coefficients"][3:], self.coefficients[3:],
                                   atol=0.0000005)
        offsets = [summary["job_offsets_mm"][str(job)]
                   for job in range(1, len(self.plan.jobs) + 1)]
        np.testing.assert_allclose(offsets, self.offsets, atol=0.012)
        self.assertAlmostEqual(summary["drift_mm_per_touch"], self.drift, delta=0.0005)
        tie_uv = (self.plan.tie_points - self.plan.origin) @ self.plan.basis[:, :2]
        u, v = tie_uv.T
        tie_z = np.column_stack((np.ones(2), u, v, u * u, u * v, v * v)) @ self.coefficients
        differences = summary["tie_differences_mm"]
        self.assertEqual(set(differences), {str(j) for j in range(1, len(self.plan.jobs) + 1)})
        np.testing.assert_allclose(list(differences.values()), tie_z[1] - tie_z[0], atol=0.02)
        self.assertLess(summary["rms_mm"], 0.01)
        self.assertLess(summary["max_abs_mm"], 0.025)
        self.assertLess(summary["tie_consistency_mm"], 0.5)
        self.assertEqual(len(summary["residuals"]), sum(map(len, self.plan.jobs)))
        local = (self.plan.points - self.plan.origin) @ self.plan.basis
        u, v = local[:, 0], local[:, 1]
        design = np.column_stack((np.ones(len(u)), u, v, u * u, u * v, v * v))
        z = design @ self.coefficients
        plane_design = np.column_stack((np.ones(len(z)), self.plan.points[:, :2]))
        plane, *_ = np.linalg.lstsq(plane_design, z, rcond=None)
        expected_pv = float(np.ptp(z - plane_design @ plane))
        expected_tilt = np.degrees(np.arctan(np.linalg.norm(plane[1:])))
        self.assertAlmostEqual(summary["flatness_pv_mm"], expected_pv, delta=0.02)
        self.assertAlmostEqual(summary["plane_tilt_deg"], expected_tilt, delta=0.01)
        expected_direction = np.degrees(np.arctan2(plane[2], plane[1])) % 360
        direction_error = (summary["tilt_direction_deg"] - expected_direction + 180) % 360 - 180
        self.assertLess(abs(direction_error), 1.0)

    def test_refuses_off_station_and_outside_search_window(self):
        self.assertEqual(self.plan.hop_mm, 5.0)
        for delta in ([1.5, 0, 0], [0, 0, -7], [0, 0, 10]):
            with self.subTest(delta=delta):
                records = [[list(record) for record in job] for job in self.records]
                point = self.plan.jobs[0][2].expected + delta
                records[0][2] = pulse_records([point])[0]
                with self.assertRaisesRegex(ValueError, r"(?i)job\s*1.*P046"):
                    platter_map.fit_map(self.plan, records)

    def test_refuses_tie_change_within_job(self):
        records = [[list(record) for record in job] for job in self.records]
        index = len(self.plan.jobs[0]) - 2
        point = cell_lc.tcp_from_pulses(records[0][index]) + [0, 0, 0.7]
        records[0][index] = pulse_records([point])[0]
        with self.assertRaisesRegex(ValueError, r"(?i)tie"):
            platter_map.fit_map(self.plan, records)

    def test_refuses_both_b_ties_shifted_in_one_job(self):
        records = [[list(record) for record in job] for job in self.records]
        job_index = 1
        last = len(self.plan.jobs[job_index]) - 1
        for index in (1, last):
            point = cell_lc.tcp_from_pulses(records[job_index][index]) + [0, 0, 2]
            records[job_index][index] = pulse_records([point])[0]
        with self.assertRaisesRegex(ValueError, r"(?i)job\s*2.*tie") as raised:
            platter_map.fit_map(self.plan, records)
        self.assertIn("P045", str(raised.exception))
        self.assertIn(f"P{44 + last:03d}", str(raised.exception))

    def test_measured_jitter_cannot_rescue_a_two_row_quadratic_plan(self):
        plan = platter_map.load_plan(platter_map.plan_json(self.plan))
        uv = np.round((plan.points - plan.origin) @ plan.basis[:, :2], 6)
        keep = (np.isin(uv[:, 0], np.unique(uv[:, 0])[:3])
                & np.isin(uv[:, 1], np.unique(uv[:, 1])[:2]))
        plan.points = plan.points[keep]
        self.assertEqual(len(plan.points), 6)
        plan.tie_points = plan.points[[0, -1]]
        expected = np.vstack((plan.tie_points, plan.points[1:-1], plan.tie_points))
        probe_type = type(plan.jobs[0][0])
        plan.jobs = [[probe_type(name=f"j1_{k:02d}", expected=p.copy(),
                                 start=p + [0, 0, 25], target=p - [0, 0, 5])
                      for k, p in enumerate(expected)]]
        plan.roles = [["tie", "tie"] + ["new"] * 4 + ["tie", "tie"]]
        plan.target = 6
        hits = expected.copy()
        index = np.arange(len(hits))
        hits[:, 0] += 0.1 * np.sin(index)
        hits[:, 1] += 0.1 * np.cos(index)
        with self.assertRaisesRegex(ValueError, r"(?i)(distinct|independent|underdetermined)"):
            platter_map.fit_map(plan, [pulse_records(hits)])

    def test_refuses_missing_job_and_record(self):
        with self.assertRaises(ValueError):
            platter_map.fit_map(self.plan, self.records[:-1])
        shortened = [list(job) for job in self.records]
        shortened[0] = shortened[0][:-1]
        with self.assertRaises(ValueError):
            platter_map.fit_map(self.plan, shortened)

    def test_degree_one_in_a_tilted_corner_frame(self):
        corners = square_corners()
        corners[:, 2] += 0.025 * (corners[:, 0] - 1014) - 0.018 * (corners[:, 1] + 300)
        plan = platter_map.map_plan(corners, [], target=49)
        coefficients = np.array([cell_lc.PLATTER_FACE_Z, 0.023, -0.014, 0, 0, 0])
        offsets = 0.12 * np.arange(len(plan.jobs))
        records = synthetic_records(plan, coefficients, offsets, 0.003)
        summary = platter_map.fit_map(plan, records, degree=1)
        np.testing.assert_allclose(summary["poly_coefficients"], coefficients[:3], atol=0.004)
        self.assertAlmostEqual(summary["drift_mm_per_touch"], 0.003, delta=0.0005)
        self.assertLess(summary["rms_mm"], 0.01)
        self.assertLess(summary["flatness_pv_mm"], 1e-7)


if __name__ == "__main__":
    unittest.main()
