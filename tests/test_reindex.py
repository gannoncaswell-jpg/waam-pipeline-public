from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, clearance, reindex, station
from waam_pipeline.backends import jbi, jbi_read
from waam_pipeline.robots import ar1440
from waam_pipeline.touch import Probe

DATE = "2026/09/28 12:00"
SOURCE = ROOT / "docs/cell-data/controller-backup/industrial-text-export/MANPRO_REV_2.JBI"
POINTS = np.array([[1000, -400, 175], [1200, -400, 175], [1200, -200, 175], [1000, -200, 175],
                   [990, -410, 172], [1210, -410, 172],
                   [980, -405, 172], [980, -200, 172]], float)
CENTER = np.array([1100.0, -300.0, 175.0])


def records(points):
    solve = cell_lc.pulse_solver(np.zeros(3))
    return [np.rint(np.asarray(solve(p)) * ar1440.PULSES_PER_DEG).astype(int).tolist()
            for p in points]


def transform(translation=(0, 0, 0), rotation_deg=(0, 0, 0), center=None):
    vector = np.radians(rotation_deg)
    angle = np.linalg.norm(vector)
    rotation = np.eye(3)
    if angle:
        x, y, z = vector / angle
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        rotation += np.sin(angle) * skew + (1 - np.cos(angle)) * (skew @ skew)
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = translation
    if center is not None:
        result[:3, 3] += center - rotation @ center
    return result


def intersections(plan, reference, move):
    result = []
    for probe, record in zip(plan, reference):
        point = cell_lc.tcp_from_pulses(record)
        normal = move[:3, :3] @ probe.normal
        moved = move[:3, :3] @ point + move[:3, 3]
        distance = np.dot(moved - point, normal) / np.dot(probe.direction, normal)
        result.append(point + distance * probe.direction)
    return np.array(result)


def shift(move=None, rotation_deg=(0, 0, 0)):
    move = np.eye(4) if move is None else move
    return reindex.BlockShift(transform=move, translation_mm=move[:3, 3],
                              rotation_deg=np.asarray(rotation_deg, float),
                              displacements_mm=np.zeros(8), residuals_mm=np.zeros(8),
                              labels=tuple(f"P{i:03d}" for i in range(44, 52)))


def pulse_values(value):
    return [int(v) for v in value.split(",")]


class TestBlockDatum(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = records(POINTS)
        cls.datum = reindex.datum_from_setup(cls.setup)
        cls.plan = reindex.touch_plan(cls.datum)

    def test_setup_contacts_are_reachable_and_build_the_datum(self):
        datum = self.datum
        np.testing.assert_allclose(datum.contacts, POINTS, atol=0.02)
        np.testing.assert_allclose(datum.top_point, datum.contacts[:4].mean(axis=0))
        np.testing.assert_allclose(datum.top_normal, [0, 0, 1], atol=0.001)
        np.testing.assert_allclose(datum.side_in, [0, 1, 0], atol=0.001)
        np.testing.assert_allclose(datum.end_in, [1, 0, 0], atol=0.001)
        np.testing.assert_allclose(datum.depths_mm, [3] * 4, atol=0.03)
        top_z = datum.top_point[2] - ((datum.contacts[:, :2] - datum.top_point[:2])
                                     @ datum.top_normal[:2]) / datum.top_normal[2]
        self.assertAlmostEqual(datum.clear_z, max(top_z) + 50)

    def test_setup_refusals(self):
        collinear = deepcopy(self.setup)
        collinear[1:4] = [collinear[0].copy() for _ in range(3)]
        deep = POINTS.copy()
        deep[4, 2] = 167
        parallel = POINTS.copy()
        parallel[6:8] = [[1010, -410, 172], [1140, -410, 172]]
        for setup, message in ((collinear, "collinear"),
                               (records(deep), "P044.*(depth|below)|side/end faces"),
                               (records(parallel), "perpendicular|90|angle")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                reindex.datum_from_setup(setup)
        with self.assertRaisesRegex(ValueError, "clear|20"):
            reindex.datum_from_setup(self.setup, clear_mm=10)
        malformed = deepcopy(self.setup)
        malformed[4] = [1, 2, 3]
        with self.assertRaisesRegex(ValueError, "P044"):
            reindex.datum_from_setup(malformed)

    def test_plan_directions_vias_and_search_windows(self):
        self.assertEqual([p.name for p in self.plan],
                         ["top1", "top2", "top3", "top4", "side1", "side2", "end1", "end2"])
        self.assertEqual(tuple(p.role for p in self.plan), reindex.ROLES)
        for i, probe in enumerate(self.plan):
            expected_direction = ([0, 0, -1] if i < 4 else
                                  self.datum.side_in if i < 6 else self.datum.end_in)
            np.testing.assert_allclose(probe.direction, expected_direction, atol=1e-12)
            np.testing.assert_array_equal(probe.expected, self.datum.contacts[i])
            np.testing.assert_allclose(probe.start, probe.expected - 10 * probe.direction)
            np.testing.assert_allclose(probe.target, probe.expected + 5 * probe.direction)
            np.testing.assert_allclose(probe.via, [*probe.start[:2], self.datum.clear_z])
            np.testing.assert_allclose(probe.normal,
                                       self.datum.top_normal if i < 4 else
                                       (expected_direction - (expected_direction @ self.datum.top_normal)
                                        * self.datum.top_normal) / np.linalg.norm(
                                            expected_direction - (expected_direction @ self.datum.top_normal)
                                            * self.datum.top_normal))
        for kwargs in ({"overtravel_mm": 8.9}, {"standoff_mm": 4.9}, {"standoff_mm": 25.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                reindex.touch_plan(self.datum, **kwargs)

    def test_fit_recovers_a_known_rigid_move(self):
        move = transform((1.2, -0.8, 0.3), (0, 0, 0.6), CENTER)
        measured = records(intersections(self.plan, self.setup, move))
        fitted = reindex.fit_shift(self.plan, self.setup, measured)
        np.testing.assert_allclose(fitted.translation_mm, move[:3, 3], atol=0.03)
        np.testing.assert_allclose(fitted.rotation_deg, [0, 0, 0.6], atol=0.005)
        np.testing.assert_allclose(fitted.transform[:3, :3], move[:3, :3], atol=0.0001)
        centre = self.datum.contacts.mean(axis=0)
        np.testing.assert_allclose(fitted.center_shift_mm,
                                   move[:3, :3] @ centre + move[:3, 3] - centre, atol=0.02)
        self.assertLess(np.linalg.norm(fitted.center_shift_mm), 3.0)
        self.assertLess(max(abs(fitted.residuals_mm)), 0.02)
        self.assertEqual(tuple(fitted.labels), tuple(f"P{i:03d}" for i in range(44, 52)))
        json.dumps(fitted.metadata(), allow_nan=False)

    def test_fit_refuses_tilt_shift_and_contacts_from_another_job(self):
        for move, message in ((transform(rotation_deg=(0.5, 0, 0), center=CENTER),
                               "block not seated flat, or S1 not at 0"),
                              (transform((0, 0, 6)), "5|shift|moved")):
            measured = records(intersections(self.plan, self.setup, move))
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                reindex.fit_shift(self.plan, self.setup, measured)
        off = self.datum.contacts.copy()
        off[0, 0] += 1
        outside = self.datum.contacts.copy()
        outside[1] = self.plan[1].start - 1 * self.plan[1].direction
        for points, label in ((off, "P044"), (outside, "P045")):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label + ".*not from this touch job"):
                reindex.fit_shift(self.plan, self.setup, records(points))
        bad_reference = records(off)
        with self.assertRaisesRegex(ValueError, "P044.*not from this touch job"):
            reindex.fit_shift(self.plan, bad_reference, self.setup)

    def test_fit_refuses_a_single_inconsistent_touch(self):
        measured = self.datum.contacts.copy()
        measured[1, 2] += 1.0
        with self.assertRaisesRegex(ValueError, "P044-P047: the four top touches disagree"):
            reindex.fit_shift(self.plan, self.setup, records(measured))
        burr = self.datum.contacts.copy()
        burr[1, 2] += 1.2
        with self.assertRaisesRegex(ValueError, "P040-P043: the four top touches are not on one flat face"):
            reindex.datum_from_setup(records(burr))

    def test_touch_noise_on_one_top_touch_is_accepted(self):
        measured = self.datum.contacts.copy()
        measured[1, 2] += 0.3
        fitted = reindex.fit_shift(self.plan, self.setup, records(measured))
        self.assertLess(max(abs(fitted.residuals_mm)), reindex.MAX_RESIDUAL_MM)

    def test_vertical_limits_keep_the_contact_tip_above_the_block(self):
        for dz in (-1.6, 1.6):
            measured = records(intersections(self.plan, self.setup, transform((0, 0, dz))))
            with self.subTest(dz=dz), self.assertRaisesRegex(
                    ValueError, r"P044: the block top moved [+-]1\.\d{2} mm; .*re-seat it\."):
                reindex.fit_shift(self.plan, self.setup, measured)
        measured = records(intersections(self.plan, self.setup, transform((4.0, 0, 1.0))))
        fitted = reindex.fit_shift(self.plan, self.setup, measured)
        np.testing.assert_allclose(fitted.translation_mm, [4, 0, 1], atol=0.03)
        measured = records(intersections(self.plan, self.setup, transform((-5.2, 0, 0))))
        with self.assertRaisesRegex(ValueError, "limit is 5 mm"):
            reindex.fit_shift(self.plan, self.setup, measured)
        for depth in (1.9, 4.1):
            points = POINTS.copy()
            points[4:, 2] = 175 - depth
            with self.subTest(depth=depth), self.assertRaisesRegex(
                    ValueError, "touch the side/end faces 2-4 mm below the top edge"):
                reindex.datum_from_setup(records(points))

    def test_tilted_setup_recovers_a_vertical_move(self):
        tilted = transform(rotation_deg=(0, 2.5, 0), center=CENTER)
        setup = records(POINTS @ tilted[:3, :3].T + tilted[:3, 3])
        datum = reindex.datum_from_setup(setup)
        plan = reindex.touch_plan(datum)
        for probe in plan[4:]:
            self.assertAlmostEqual(probe.direction[2], 0)
            self.assertAlmostEqual(probe.normal @ datum.top_normal, 0)
        face_normals = {"top": [0, 0, 1], "side": [0, 1, 0], "end": [1, 0, 0]}
        physical_plan = [replace(probe, normal=tilted[:3, :3] @ face_normals[probe.role]) for probe in plan]
        measured = records(intersections(physical_plan, setup, transform((0, 0, 1.0))))
        fitted = reindex.fit_shift(plan, setup, measured)
        np.testing.assert_allclose(fitted.translation_mm, [0, 0, 1], atol=0.03)
        np.testing.assert_allclose(fitted.center_shift_mm, [0, 0, 1], atol=0.03)
        too_tilted = transform(rotation_deg=(0, 3.2, 0), center=CENTER)
        with self.assertRaisesRegex(ValueError, "top plane tilt.*exceeds 3 deg"):
            reindex.datum_from_setup(records(POINTS @ too_tilted[:3, :3].T + too_tilted[:3, 3]))

    def test_fit_refuses_combined_tilt_and_yaw_about_the_top_normal(self):
        move = transform(rotation_deg=(0.29, 0.29, 0), center=CENTER)
        with self.assertRaisesRegex(ValueError, "top normal tilt exceeds 0.3 deg"):
            reindex.fit_shift(self.plan, self.setup, records(intersections(self.plan, self.setup, move)))
        tilted = transform(rotation_deg=(0, 2.5, 0), center=CENTER)
        setup = records(POINTS @ tilted[:3, :3].T + tilted[:3, 3])
        datum = reindex.datum_from_setup(setup)
        plan = reindex.touch_plan(datum)
        move = transform(rotation_deg=datum.top_normal * 2.1, center=CENTER)
        with self.assertRaisesRegex(ValueError, "yaw about the top normal"):
            reindex.fit_shift(plan, setup, records(intersections(plan, setup, move)))

    def test_fit_refuses_a_rank_deficient_plan(self):
        plan = [replace(probe, normal=np.array([0, 0, 1]),
                        start=probe.expected + [0, 0, 10],
                        target=probe.expected - [0, 0, 5]) for probe in self.plan]
        with self.assertRaisesRegex(ValueError, "rank|layout|independent"):
            reindex.fit_shift(plan, self.setup, self.setup)


class TestReindexJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="ascii")
        cls.job = jbi_read.read_jbi(cls.source)
        patcher = patch.object(jbi, "_git_sha", return_value="test")
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    def render(self, source=None, correction=None, **kwargs):
        return reindex.reindex_job(self.source if source is None else source,
                                   shift() if correction is None else correction,
                                   name=kwargs.pop("name", "MANPRO_RX"), date=DATE, **kwargs)

    def test_identity_preserves_positions_station_and_instructions(self):
        text, report = self.render(folder="REINDEX")
        result = jbi_read.read_jbi(text)
        self.assertEqual(result.name, "MANPRO_RX")
        self.assertEqual(result.npos, self.job.npos)
        self.assertEqual(result.groups, ["RB1", "ST1"])
        self.assertEqual(result.folder, "REINDEX")
        self.assertEqual(text.splitlines()[1:3], ["//NAME MANPRO_RX", "///FOLDERNAME REINDEX"])
        for name, value in self.job.positions.items():
            if name.startswith("EC"):
                self.assertEqual(result.positions[name], value)
                self.assertIn(f"{name}={value}\r\n", text)
            else:
                self.assertEqual(result.positions[name], value)
        self.assertEqual(result.instructions[0], "NOP")
        self.assertEqual(result.instructions[6:], self.job.instructions[1:])
        self.assertTrue(result.instructions[1].startswith("' REINDEXED COPY - REVIEW REQ"))
        self.assertTrue(all(len(line) <= 33 for line in result.instructions if line.startswith("'")))
        self.assertEqual(result.date, DATE)
        self.assertTrue(text.endswith("\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        text.encode("ascii")
        self.assertEqual(report["source"], "MANPRO_REV_2")
        self.assertEqual(report["name"], "MANPRO_RX")
        self.assertEqual(report["calls"], [{"kind": "CALL", "job": "CLEAN_R1"}])
        self.assertEqual(len(report["points"]), 15)
        json.dumps(report, allow_nan=False)

    def test_shift_is_conjugated_at_each_original_station_angle(self):
        move = transform((0.25, -0.15, 0.1), (0, 0, 0.05), CENTER)
        text, report = self.render(correction=shift(move, (0, 0, 0.05)))
        result = jbi_read.read_jbi(text)
        for row in report["points"]:
            key = row["record"]
            theta = int(self.job.positions["E" + key]) / reindex.STATION_PULSES_PER_DEG
            station_pose = station.station_transform(theta)
            correction = station_pose @ move @ np.linalg.inv(station_pose)
            before = cell_lc.tcp_from_pulses(pulse_values(self.job.positions[key]))
            after = cell_lc.tcp_from_pulses(pulse_values(result.positions[key]))
            expected = correction[:3, :3] @ before + correction[:3, 3]
            np.testing.assert_allclose(after, expected, atol=0.05)
            np.testing.assert_allclose(row["tcp_before"], before)
            np.testing.assert_allclose(row["tcp_after"], after)
            self.assertAlmostEqual(row["station_deg"], theta)
            self.assertAlmostEqual(row["moved_mm"], np.linalg.norm(after - before))
            self.assertLessEqual(row["max_joint_change_deg"], reindex.MAX_JOINT_CHANGE_DEG)
        self.assertEqual({k: v for k, v in result.positions.items() if k.startswith("EC")},
                         {k: v for k, v in self.job.positions.items() if k.startswith("EC")})

    def test_refuses_unsupported_jobs_and_replacement_of_the_class_job(self):
        for old, new in (("MOVL C00000 EC00000", "MOVL P001 EC00000"),
                         ("MOVL C00000", "SMOVL C00000"),
                         ("///POSTYPE PULSE", "///POSTYPE RECTAN"),
                         ("MOVL C00001 EC00001", "MOVL C00000 EC00001"),
                         ("MOVL C00000 EC00000", "MOVL C00000"),
                         ("///NPOS 15,0,15,0,0,0", "///NPOS 14,0,15,0,0,0")):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.render(self.source.replace(old, new, 1))
        for instruction in ("SFTON P001", "SFTOF", "MSHIFT", "SREFP", "IMOV P001", "MOVL C00000 UF#(1)"):
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                self.render(self.source.replace("NOP\n", "NOP\n" + instruction + "\n", 1))
        with self.assertRaisesRegex(ValueError, "would replace the class job"):
            self.render(name=self.job.name)
        with self.assertRaises(ValueError):
            self.render(name="invalid name")

    def test_robot_only_job_and_source_header_are_preserved(self):
        source = self.source.replace("///NPOS 15,0,15,0,0,0", "///NPOS 15,0,0,0,0,0")
        source = source.replace("///GROUP1 RB1,ST1", "///GROUP1 RB1")
        source = re.sub(r"^EC\d{5}=.*\n", "", source, flags=re.MULTILINE)
        source = re.sub(r" EC\d{5}", "", source)
        source = source.replace("//NAME MANPRO_REV_2\n", "//NAME MANPRO_REV_2\n///FOLDERNAME OLD\n")
        text, report = self.render(source)
        result = jbi_read.read_jbi(text)
        self.assertEqual(result.groups, ["RB1"])
        self.assertFalse(result.folder)
        self.assertNotIn("///FOLDERNAME", text)
        self.assertTrue(all(row["station_deg"] == 0 for row in report["points"]))

    def test_parser_refuses_ambiguous_headers_and_embedded_motion(self):
        for old, new in (("///POSTYPE PULSE", "///POSTYPE PULSE\n///POSTYPE\tPULSE"),
                         ("C00000=-729,-59979,-60060,-74,-89734,186",
                          "C00000=-729,-59979,-60060,-74,-89734,186\nP0001=0,0,0,0,0,0"),
                         ("EC00000=1", "EC00000=1,2"),
                         ("MOVL C00000", "movl C00000"),
                         ("MOVL C00000 EC00000 V=833.3", "IF B000=0 MOVL C00000 EC00000 V=833.3")):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.render(self.source.replace(old, new, 1))
        for instruction in ("SET I000 P001", "MOVL C00000 EC00000 V=10 +MOVJ", "SRCH", "UNTIL IN#(1)=ON"):
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                self.render(self.source.replace("NOP\n", "NOP\n" + instruction + "\n", 1))

    def test_call_names_are_not_mistaken_for_position_variables(self):
        text, report = self.render(self.source.replace("CALL JOB:CLEAN_R1", "CALL JOB:CLEAN_R1\nCALL JOB:P001"))
        self.assertEqual(report["calls"], [{"kind": "CALL", "job": "CLEAN_R1"}, {"kind": "CALL", "job": "P001"}])
        self.assertIn("CALL JOB:P001", text)

    def test_call_and_jump_targets_are_reported_and_must_be_static(self):
        source = self.source.replace("CALL JOB:CLEAN_R1", "CALL JOB:CLEAN_R1\nJUMP JOB:P001\nCALL JOB:P001")
        text, report = self.render(source)
        expected = [{"kind": "CALL", "job": "CLEAN_R1"}, {"kind": "JUMP", "job": "P001"},
                    {"kind": "CALL", "job": "P001"}]
        self.assertEqual(report["calls"], expected)
        self.assertEqual(reindex.source_summary(source)["calls"], expected)
        self.assertIn("JUMP JOB:P001", text)
        for kind in ("CALL", "JUMP"):
            for target in ('"CLASS"', "CLASS[0]", "CLASS+1", "IG#(0)", "", "CLASS.JBI"):
                with self.subTest(kind=kind, target=target), self.assertRaisesRegex(ValueError, "plain static name"):
                    self.render(self.source.replace("CALL JOB:CLEAN_R1", f"{kind} JOB:{target}"))

    def test_station_must_stay_still_for_every_motion_while_welding(self):
        for key in ("EC00004", "EC00005"):
            source = self.source.replace(f"{key}=-1216", f"{key}=-1217")
            with self.subTest(key=key), self.assertRaisesRegex(
                    ValueError, f"S1 moves during a weld at MOVC C{key[2:]} {key}:? .*re-teach that weld"):
                self.render(source)
        source = self.source.replace("ARCON ASF#(2)", "' ARCON ASF#(2)")
        self.render(source.replace("EC00004=-1216", "EC00004=-1217"))
        self.render(self.source.replace("EC00006=-22347", "EC00006=-22348"))
        for instruction in ("CALL JOB:ARCOF", "JUMP JOB:ARCOF", "*ARCOF"):
            source = self.source.replace("ARCON ASF#(2)", f"ARCON ASF#(2)\n{instruction}", 1)
            with self.subTest(instruction=instruction), self.assertRaisesRegex(ValueError, "S1 moves during a weld"):
                self.render(source.replace("EC00004=-1216", "EC00004=-1217"))
        source = self.source.replace("ARCON ASF#(2)", "arcon ASF#(2)", 1)
        with self.assertRaisesRegex(ValueError, "S1 moves during a weld"):
            self.render(source.replace("EC00004=-1216", "EC00004=-1217"))

    def test_identity_keeps_singular_pulses_and_nonidentity_refuses_them(self):
        old = self.job.positions["C00000"]
        for b in (0.0, 0.9, -0.9):
            values = pulse_values(old)
            values[4] = round(b * ar1440.PULSES_PER_DEG[4])
            raw = ",".join(str(v) for v in values)
            source = self.source.replace(f"C00000={old}", f"C00000={raw}")
            with self.subTest(b=b), patch.object(ar1440, "ik", side_effect=AssertionError("identity ran IK")):
                text, report = self.render(source, correction=shift(transform((1e-10, 0, 0))))
            self.assertIn(f"C00000={raw}\r\n", text)
            self.assertTrue(all(p["moved_mm"] == 0 for p in report["points"]))
            with self.subTest(b=b), self.assertRaisesRegex(ValueError, r"C00000 is at a wrist singularity \(B ~ 0 deg\)"):
                self.render(source, correction=shift(transform((0.01, 0, 0))))


class TestReindexRenderer(unittest.TestCase):
    def setUp(self):
        self.plan = reindex.touch_plan(reindex.datum_from_setup(records(POINTS)))
        patcher = patch.object(jbi, "_git_sha", return_value="test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, plan=None, **kwargs):
        return jbi._render_search_jbi(self.plan if plan is None else plan,
                                      cell_lc.pulse_solver(np.zeros(3)), name="REIDX_TOUCH",
                                      date=DATE, pvar_base=44, clean_job="CLEAN_R1", **kwargs)

    def test_via_moves_have_distinct_records_and_guarded_descents(self):
        guard = Mock()
        with patch.object(jbi, "point_guard", return_value=guard) as factory:
            text = self.render(contact_ok=("datum",))
        self.assertEqual(factory.call_args.kwargs["contact_ok"], ("datum",))
        job = jbi_read.read_jbi(text)
        motions = [line for line in job.instructions if re.match(r"MOV[JL] C\d{5}", line)]
        keys = [line.split()[1] for line in motions]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(job.npos[0], len(keys))
        self.assertEqual(len(job.positions), 2 + 3 * len(self.plan))
        for i, probe in enumerate(self.plan):
            entries = motions[1 + 3 * i:4 + 3 * i]
            self.assertEqual([line.split()[0] for line in entries], ["MOVJ", "MOVL", "MOVL"])
            self.assertTrue(entries[0].endswith("VJ=25.00"))
            self.assertTrue(all(line.endswith("V=100.0") for line in entries[1:]))
            for line, expected in zip(entries, (probe.via, probe.start, probe.via)):
                np.testing.assert_allclose(cell_lc.tcp_from_pulses(pulse_values(job.positions[line.split()[1]])),
                                           expected, atol=0.03)
            labels = [call.args[1] for call in guard.call_args_list]
            self.assertIn(f"probe {probe.name} via", labels)
            self.assertIn(f"probe {probe.name} approach", labels)
            samples = [np.asarray(call.args[0]) for call in guard.call_args_list
                       if call.args[1] == f"probe {probe.name} descent"]
            self.assertTrue(samples)
            heights = sorted([probe.start[2], probe.via[2], *(p[2] for p in samples)])
            self.assertLessEqual(max(np.diff(heights)), 2.0 + 1e-9)
            for point in samples:
                np.testing.assert_allclose(point[:2], probe.start[:2])
        for pvar in range(44, 52):
            self.assertIn(f"GETS PX{pvar:03d} $PX002", text)
            self.assertIn(f"MOVL P{pvar:03d} V=10.0 SRCH RIN#(3)=ON T=0.10 DIS=5", text)

    def test_via_and_hop_are_incompatible_and_no_via_stays_unchanged(self):
        with self.assertRaisesRegex(ValueError, "via.*hop|hop.*via"):
            self.render(hop_mm=5)
        legacy = [Probe(p.name, p.start, p.target, p.expected) for p in self.plan]
        self.assertEqual(self.render(legacy), self.render([replace(p, via=None) for p in self.plan]))
        self.assertEqual(self.render(legacy), self.render(legacy, guard_v="42.0"))
        self.assertNotIn("UNEXPECTED TOUCH ON APPROACH", self.render(legacy))

    def test_via_guard_sequence_reseeds_the_measurement_target(self):
        solve = cell_lc.pulse_solver(np.zeros(3))
        for rin, io, alarm, message in ((3, 1891, 8812, "WIRE ALREADY TOUCHED"),
                                        (4, 1892, 8813, "ACCUFAST SIGNAL ALREADY ON")):
            text = self.render(rin=rin, guard_v="42.0")
            lines = text.splitlines()
            for i, probe in enumerate(self.plan):
                pvar = 44 + i
                check = [f"WAIT IN#({io})=OFF T=3.00", f"IFTHENEXP IN#({io})=ON",
                         f'\t SETUALM {alarm} "{message}" 1 SMODE=0', "ENDIF"]
                expected = check + [f"GETS PX{pvar:03d} $PX000"]
                expected += [f"SETE P{pvar:03d} ({k + 1}) {value}"
                             for k, value in enumerate(pulse_values(jbi._pulses(solve(probe.start))))]
                expected += [f"MOVL P{pvar:03d} V=42.0 SRCH RIN#({rin})=ON T=0.10 DIS=0.1",
                             "GETS B000 $B002", "IFTHENEXP B000=1",
                             '\t SETUALM 8815 "UNEXPECTED TOUCH ON APPROACH" 1 SMODE=0', "ENDIF"]
                expected += check + [f"GETS PX{pvar:03d} $PX000"]
                expected += [f"SETE P{pvar:03d} ({k + 1}) {value}"
                             for k, value in enumerate(pulse_values(jbi._pulses(solve(probe.expected))))]
                expected += [f"MOVL P{pvar:03d} V=10.0 SRCH RIN#({rin})=ON T=0.10 DIS=5"]
                begin = lines.index(f"' probe {probe.name}") + 1
                with self.subTest(rin=rin, probe=probe.name):
                    self.assertRegex(lines[begin], r"MOVJ C\d{5} VJ=25.00")
                    self.assertEqual(lines[begin + 1:begin + 1 + len(expected)], expected)

    def test_point_guard_uses_the_same_contact_allowance_for_both_checks(self):
        with patch.object(clearance, "check_point", return_value=[]) as check, \
                patch.object(clearance, "blocking", return_value=[]) as block:
            guard = clearance.point_guard(object(), object(), contact_ok=("datum",))
            guard(np.zeros(3), "datum via")
        self.assertEqual(check.call_args.kwargs["contact_ok"], ("datum",))
        self.assertEqual(block.call_args.kwargs["contact_ok"], ("datum",))


class TestReindexProfile(unittest.TestCase):
    def setUp(self):
        self.profile = {"version": 1, "kind": "manpro-reindex", "setup": {"records": records(POINTS)},
                        "clear_mm": 50, "reference": None, "source_job": None, "notes": "Class datum"}

    def test_profile_validation_and_plan(self):
        normalized = reindex.validate_profile(self.profile)
        json.dumps(normalized, allow_nan=False)
        datum, plan = reindex.plan_from_profile(normalized)
        self.assertEqual(len(plan), 8)
        np.testing.assert_allclose(datum.contacts, POINTS, atol=0.02)

    def test_bad_profiles_are_refused(self):
        mutations = ({"version": 2}, {"setup": {"records": self.profile["setup"]["records"][:6]}},
                     {"source_job": {"name": "CLASS", "text": "non-ASCII: \N{DEGREE SIGN}"}},
                     {"unexpected": True}, {"notes": "x" * 501})
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValueError):
                reindex.validate_profile({**self.profile, **change})

    def test_reindex_from_profile_requires_reference_and_source(self):
        setup = self.profile["setup"]["records"]
        with self.assertRaisesRegex(ValueError, "reference"):
            reindex.reindex_from_profile(self.profile, setup, name="MANPRO_RX", date=DATE)
        self.profile["reference"] = {"records": setup}
        with self.assertRaisesRegex(ValueError, "source|class job"):
            reindex.reindex_from_profile(self.profile, setup, name="MANPRO_RX", date=DATE)
        self.profile["source_job"] = {"name": "MANPRO_REV_2", "text": SOURCE.read_text(encoding="ascii")}
        with patch.object(jbi, "_git_sha", return_value="test"):
            text, report = reindex.reindex_from_profile(self.profile, setup, name="MANPRO_RX", date=DATE)
        self.assertEqual(jbi_read.read_jbi(text).name, "MANPRO_RX")
        self.assertEqual(report["calls"], [{"kind": "CALL", "job": "CLEAN_R1"}])


if __name__ == "__main__":
    unittest.main()
