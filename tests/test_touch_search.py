import re
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, touch
from waam_pipeline.backends.jbi import (PARK_PULSES, _render_search_jbi,
                                         emit_search_jbi)
from waam_pipeline.clearance import ClearanceError, Scene, TorchModel
from waam_pipeline.robots.ar1440 import PULSES_PER_DEG


def _plan():
    return touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)


def _solve():

    return cell_lc.pulse_solver(np.zeros(3))


class TestSearchExportEnabled(unittest.TestCase):


    def test_public_entry_delegates_to_the_renderer(self):
        plan, solve = _plan(), _solve()
        got = emit_search_jbi(plan, solve, name="R1_WT", date="2026/09/17 00:00")
        want = _render_search_jbi(plan, solve, name="R1_WT", date="2026/09/17 00:00")
        self.assertEqual(got, want)
        self.assertIn("TOUCH-SENSE PROBE - REVIEW REQ", got)
        self.assertIn("NOT auto-runnable", got)

    def test_public_entry_still_clearance_gates(self):
        torch = TorchModel.yaskawa_accufast_45()
        lid_z = cell_lc.PLATTER_FACE_Z + 60.0
        scene = Scene()
        scene.boxes.append(("lid", np.array([900.0, -450.0, lid_z]),
                            np.array([1130.0, -150.0, lid_z + 40.0])))
        with self.assertRaises(ClearanceError):
            emit_search_jbi(_plan(), _solve(), name="X", date="2026/09/17 00:00",
                            scene=scene, torch=torch)

    def test_cli_emits_new_path_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "probe.JBI"
            r = subprocess.run(
                [sys.executable, str(ROOT / "examples/gen_touch_job.py"), "--out", str(out)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(out.exists())
            self.assertIn(b"TOUCH-SENSE PROBE", out.read_bytes())
            out.write_bytes(b"reviewed job")
            r2 = subprocess.run(
                [sys.executable, str(ROOT / "examples/gen_touch_job.py"), "--out", str(out)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r2.returncode, 1, r2.stderr)
            self.assertIn("already exists", r2.stdout)
            self.assertEqual(out.read_bytes(), b"reviewed job")

    def test_denser_grid_gives_n_squared_probes(self):
        self.assertEqual(len(touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)), 4)
        self.assertEqual(len(touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z, grid=5)), 25)
        with self.assertRaises(ValueError):
            touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z, grid=1)


class TestSearchHops(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.plan = touch.line_probe_plan((1021.7, -232.5), (1019.4, -130.9), 179.0, stations=4)
        cls.job = _render_search_jbi(cls.plan, _solve(), name="HOP_TEST",
                                     date="2026/09/25 12:00", hop_mm=5.0)
        lines = cls.job.split("\r\n")
        cls.records = {line.split("=")[0]: [int(v) for v in line.split("=")[1].split(",")]
                       for line in lines if line.startswith("C0")}
        cls.body = lines[lines.index("NOP"):]
        cls.moves = [line for line in cls.body if line.startswith(("MOVJ", "MOVL"))]

    def tcp(self, record):
        from waam_pipeline.robots import ar1440
        pose = ar1440.fk(np.asarray(self.records[record], float) / PULSES_PER_DEG)["flange"]
        return pose[:3, 3] + pose[:3, :3] @ cell_lc.TOOL_TCP

    def test_one_approach_then_straight_hops(self):
        n = len(self.plan)
        joints = [m for m in self.moves if m.startswith("MOVJ")]
        self.assertEqual(len(joints), 3)
        searches = [m for m in self.moves if "SRCH" in m]
        self.assertEqual(len(searches), n)
        hops = [m for m in self.moves if m.startswith("MOVL") and "SRCH" not in m]
        self.assertEqual(len(hops), 2 * n + 1)
        self.assertTrue(all(m.endswith(" V=100.0") for m in hops))

        for k, line in enumerate(self.body):
            if "SRCH" in line:
                before = [m for m in self.body[:k] if m.startswith(("MOVJ", "MOVL"))][-1]
                self.assertTrue(before.startswith("MOVL C"), before)
        self.assertEqual(self.job.count('SETUALM 8812 "WIRE ALREADY TOUCHED"'), n)

    def test_hop_records_sit_hop_mm_above_each_expected_contact(self):
        hop_records = [m.split()[1] for m in self.moves if m.startswith("MOVL C")]
        probes = iter(self.plan)

        for k in range(len(self.plan)):
            probe = next(probes)
            for record in hop_records[2 * k:2 * k + 2]:
                np.testing.assert_allclose(self.tcp(record), probe.expected + [0, 0, 5.0], atol=0.05)
        np.testing.assert_allclose(self.tcp(hop_records[-1]), self.plan[-1].start, atol=0.05)
        npos = int(next(l for l in self.job.split("\r\n") if l.startswith("///NPOS")).split()[1].split(",")[0])
        self.assertEqual(npos, len(self.records))
        self.assertEqual(npos, 2 * len(self.plan) + 4)
        self.assertIn("' hop 5 mm between touches", self.job)

    def test_no_hop_is_the_original_job(self):
        plain = _render_search_jbi(self.plan, _solve(), name="HOP_TEST", date="2026/09/25 12:00")
        self.assertEqual(plain, _render_search_jbi(self.plan, _solve(), name="HOP_TEST",
                                                   date="2026/09/25 12:00", hop_mm=None))
        self.assertNotIn("hop", plain)
        self.assertFalse([l for l in plain.split("\r\n") if l.startswith("MOVL C")])

    def test_hop_refusals(self):
        for bad in (0, -1.0, float("nan"), True, "5"):
            with self.subTest(hop=bad), self.assertRaises(ValueError):
                _render_search_jbi(self.plan, _solve(), name="HOP_TEST", date="2026/09/25 12:00", hop_mm=bad)
        with self.assertRaisesRegex(ValueError, "below its approach start"):
            _render_search_jbi(self.plan, _solve(), name="HOP_TEST", date="2026/09/25 12:00", hop_mm=25.0)

    def test_every_hop_point_is_clearance_checked(self):


        target = self.plan[1].expected
        scene = Scene(boxes=[("hop_block", target + [-1, -1, 4.0], target + [1, 1, 6.0])])
        torch = TorchModel(samples=((0.0, 1.0),))
        _render_search_jbi(self.plan, _solve(), name="HOP_TEST", date="2026/09/25 12:00",
                           scene=Scene(boxes=[("far", np.array([0, 0, 0.0]), np.array([1, 1, 1.0]))]),
                           torch=torch, hop_mm=5.0)
        with self.assertRaisesRegex(ClearanceError, "line_s1 hop"):
            _render_search_jbi(self.plan, _solve(), name="HOP_TEST", date="2026/09/25 12:00",
                               scene=scene, torch=torch, hop_mm=5.0)


class TestSearchEmit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = _plan()


        cls.job = _render_search_jbi(cls.plan, _solve(), name="R1_WAAM_TOUCH",
                                  date="2026/09/17 00:00", rin=4)

    def test_srch_form_matches_the_manual(self):


        self.assertRegex(self.job, r"MOVL P0\d\d V=[\d.]+ SRCH RIN#\(4\)=ON T=0\.10 DIS=[\d.]+")
        self.assertNotRegex(self.job, r"MOVL C\d{5}[^\r\n]*SRCH")
        self.assertIn("GETS B000 $B002", self.job)
        self.assertRegex(self.job, r"GETS PX0\d\d \$PX000")
        self.assertRegex(self.job, r"SETE P0\d\d \(1\) -?\d+")
        self.assertRegex(self.job, r"GETS PX0\d\d \$PX002")

    def test_result_pvars_one_per_probe(self):
        pvars = re.findall(r"GETS (PX0\d\d) \$PX002", self.job)
        self.assertEqual(pvars, [f"PX{40 + i:03d}" for i in range(len(self.plan))])

    def test_default_search_speed_is_the_slow_one(self):


        speeds = set(re.findall(r"MOVL P0\d\d V=([\d.]+) SRCH", self.job))
        self.assertEqual(speeds, {"10.0"})

    def test_dis_is_millimetres_not_tenths(self):

        self.assertRegex(self.job, r"SRCH RIN#\(4\)=ON T=0\.10 DIS=5\b")
        self.assertNotRegex(self.job, r"DIS=50\b")

    def test_rb1_pulse_header_and_position_table(self):
        for tag in ("///POSTYPE PULSE", "///GROUP1 RB1", "///TOOL 0", "//NAME R1_WAAM_TOUCH"):
            self.assertIn(tag, self.job)
        cs = re.findall(r"^C(\d{5})=(.+)$", self.job, re.MULTILINE)


        self.assertEqual(len(cs), 2 + 2 * len(self.plan))
        for _idx, rec in cs:
            vals = rec.split(",")
            self.assertEqual(len(vals), len(PULSES_PER_DEG))
            for v in vals:
                int(v)

    def test_it_announces_itself_as_review_gated(self):
        self.assertIn("TOUCH-SENSE PROBE - REVIEW REQ", self.job)
        self.assertIn("NOT auto-runnable", self.job)

    def test_comment_lines_within_pendant_32_char_limit(self):


        job = _render_search_jbi(self.plan, _solve(), name="R1_WAAM_TOUCH",
                              date="2026/09/17 00:00",
                              provenance=("platter attested by test_operator",))
        comment_lines = [ln for ln in job.split("\r\n") if ln.startswith("'")]
        self.assertTrue(comment_lines)
        for ln in comment_lines:
            self.assertLessEqual(len(ln) - 1, 32,
                                 f"comment over the 32-char pendant limit: {ln!r}")

    def test_nested_lines_indent_with_tab_not_spaces(self):


        self.assertNotRegex(self.job, r"(?m)^ ")

        self.assertIn('\t SETUALM', self.job)

    def test_emitted_job_uses_crlf_line_endings(self):

        self.assertIn("\r\n", self.job)
        self.assertNotIn("\r\r\n", self.job)

    def test_gen_touch_job_cli_writes_bytes_not_text_mode(self):


        src = (ROOT / "examples" / "gen_touch_job.py").read_text(encoding="utf-8")
        self.assertIn("write_job(", src)
        self.assertNotIn("write_text(job", src)

    def test_unchecked_when_no_scene(self):
        self.assertIn("clearance NO - UNCHECKED", self.job)

    def test_accufast_io_is_enabled_then_cleared(self):

        self.assertIn("DOUT OT#(1892) ON", self.job)
        self.assertIn("DOUT OT#(4037) ON", self.job)
        self.assertIn("DOUT OT#(1892) OFF", self.job)
        self.assertIn("DOUT OT#(4037) OFF", self.job)
        self.assertLess(self.job.index("DOUT OT#(1892) ON"), self.job.index("SRCH"))
        self.assertLess(self.job.rindex("SRCH"), self.job.index("DOUT OT#(1892) OFF"))

    def test_starts_and_ends_at_park(self):


        park = ",".join(str(p) for p in PARK_PULSES)
        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", self.job, re.MULTILINE))
        body = self.job.split("//INST", 1)[1]
        moves = re.findall(r"MOV[JL] (C\d{5})", body)
        self.assertEqual(recs[moves[0]], park)
        self.assertEqual(recs[moves[-1]], park)

    def test_npos_equals_c_move_count(self):


        npos = int(re.search(r"///NPOS (\d+),", self.job).group(1))
        c_moves = re.findall(r"^MOV[JL] C\d{5}", self.job, re.MULTILINE)
        self.assertEqual(npos, len(c_moves))

    def test_the_default_sense_is_the_installed_one(self):


        job = _render_search_jbi(self.plan, _solve(), name="R1_WT",
                                 date="2026/09/17 00:00")
        self.assertIn("SRCH RIN#(3)=ON", job)
        self.assertNotIn("RIN#(4)", job)

    def test_wire_touch_mode_switches_inputs_and_alarm(self):
        job3 = _render_search_jbi(self.plan, _solve(), name="R1_WT", date="2026/09/17 00:00", rin=3)
        self.assertIn("SRCH RIN#(3)=ON", job3)
        self.assertIn("WAIT IN#(1891)=OFF", job3)
        self.assertIn("WIRE ALREADY TOUCHED", job3)
        self.assertNotIn("4037", job3)


class TestSearchRefusals(unittest.TestCase):
    def test_empty_plan_refused(self):
        with self.assertRaises(ValueError):
            _render_search_jbi([], _solve(), name="X", date="2026/09/17 00:00")

    def test_bad_rin_refused(self):
        with self.assertRaises(ValueError):
            _render_search_jbi(_plan(), _solve(), name="X", date="2026/09/17 00:00", rin=5)

    def test_date_only_refused(self):


        with self.assertRaises(ValueError):
            _render_search_jbi(_plan(), _solve(), name="X", date="2026/09/17")

    def test_unreachable_probe_raises_not_skips(self):
        far = touch.Probe(name="far",
                          start=np.array([9000.0, 0.0, 500.0]),
                          target=np.array([9000.0, 0.0, 400.0]),
                          expected=np.array([9000.0, 0.0, 450.0]))
        with self.assertRaises(ValueError):
            _render_search_jbi([far], _solve(), name="X", date="2026/09/17 00:00")

    def test_fouling_approach_is_refused(self):

        torch = TorchModel.yaskawa_accufast_45()
        plan = _plan()
        lid_z = cell_lc.PLATTER_FACE_Z + 60.0
        scene = Scene()
        scene.boxes.append(("lid", np.array([900.0, -450.0, lid_z]),
                            np.array([1130.0, -150.0, lid_z + 40.0])))
        with self.assertRaises(ClearanceError):
            _render_search_jbi(plan, _solve(), name="X", date="2026/09/17 00:00",
                            scene=scene, torch=torch)


def _pulses_for(point, solve):
    q = solve(np.asarray(point, float))
    return [int(round(a * r)) for a, r in zip(q, PULSES_PER_DEG)]


class TestReadBack(unittest.TestCase):


    def test_hits_from_points_pairs_in_plan_order(self):
        plan = _plan()
        pts = [pr.expected for pr in plan]
        hits = touch.hits_from_points(plan, pts)
        self.assertEqual([h.name for h in hits], [pr.name for pr in plan])
        np.testing.assert_allclose(hits[0].point, plan[0].expected)

    def test_wrong_record_count_raises(self):
        plan = _plan()
        with self.assertRaises(ValueError):
            touch.hits_from_points(plan, [pr.expected for pr in plan[:-1]])

    def test_perfect_search_round_trips_through_pulses(self):


        plan = _plan()
        solve = _solve()
        records = [_pulses_for(pr.expected, solve) for pr in plan]
        points = [cell_lc.tcp_from_pulses(r) for r in records]
        hits = touch.hits_from_points(plan, points)
        self.assertEqual(touch.check_hits(plan, hits), [])
        surface = cell_lc.fit_work_surface("unit_probe_face", records)
        self.assertLess(surface.tilt_deg, 1.0)
        self.assertLess(surface.residual_mm, 0.5)

    def test_a_missed_search_is_flagged_not_folded_in(self):
        plan = _plan()
        pts = [np.asarray(pr.expected, float) for pr in plan]
        pts[1] = pts[1] + np.array([0.0, 0.0, -40.0])
        hits = touch.hits_from_points(plan, pts)
        problems = touch.check_hits(plan, hits, tolerance_mm=15.0)
        self.assertTrue(any(plan[1].name in p for p in problems))


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
