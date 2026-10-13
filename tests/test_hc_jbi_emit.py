import re
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_hc
from waam_pipeline.backends.jbi import (HC_PARK_PULSES, _render_hc_jbi,
                                        emit_hc_jbi)
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.robots import hc10
from waam_pipeline.toolpath import Move, Toolpath

ORIGIN = cell_hc.DEFAULT_PART_ORIGIN


def _toolpath() -> Toolpath:


    return Toolpath(moves=[
        Move(x=0.0, y=0.0, z=2.9, feed=200.0, deposit=False, layer=0, line_no=1),
        Move(x=30.0, y=15.0, z=2.9, feed=200.0, deposit=True, layer=0, line_no=2),
    ])


class TestRenderHcJbi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):

        cls.job = _render_hc_jbi(
            _toolpath(), cell_hc.hc_solver(ORIGIN),
            name="HC-WALL-TEST", date="2026/02/20 18:14", arc_file=13,
            scene=None, torch=None,
        )

    def test_header_group_and_terminator(self):
        self.assertTrue(self.job.startswith("/JOB\r\n//NAME HC-WALL-TEST\r\n//POS\r\n"))
        self.assertIn("///POSTYPE PULSE", self.job)
        self.assertIn("///GROUP1 RB1", self.job)
        self.assertTrue(self.job.endswith("END\r\n"))

    def test_crlf_line_endings(self):

        self.assertIn("\r\n", self.job)
        self.assertNotIn("\r\r\n", self.job)
        self.assertNotIn("\n\n", self.job.replace("\r\n", "\n"))

    def test_npos_equals_c_move_count(self):


        npos = int(re.search(r"///NPOS (\d+),", self.job).group(1))
        c_moves = re.findall(r"^MOV[JL] C\d{5}", self.job, re.MULTILINE)
        n_records = len(re.findall(r"^C\d{5}=", self.job, re.MULTILINE))
        self.assertEqual(npos, len(c_moves))
        self.assertEqual(npos, n_records)

        job = read_jbi(self.job.replace("\r\n", "\n"))
        self.assertEqual(job.groups, ["RB1"])
        self.assertEqual(job.npos[0], sum(1 for k in job.positions if k.startswith("C")))

    def test_position_records_are_robot_only_six_fields(self):


        recs = re.findall(r"^C\d{5}=(.+)$", self.job, re.MULTILINE)
        self.assertTrue(recs)
        for rec in recs:
            vals = rec.split(",")
            self.assertEqual(len(vals), 6, rec)
            for v in vals:
                int(v)

    def test_park_record_is_the_taught_r1_home(self):


        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", self.job, re.MULTILINE))
        body = self.job.split("//INST", 1)[1]
        moves = re.findall(r"MOV[JL] (C\d{5})", body)
        park = ",".join(str(p) for p in HC_PARK_PULSES)
        self.assertEqual(recs[moves[0]], park)
        self.assertEqual(recs[moves[-1]], park)

    def test_pulse_scale_is_hc10_not_ar1440(self):


        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", self.job, re.MULTILINE))
        body = self.job.split("//INST", 1)[1]
        ref = re.search(r"ARCON[^\r\n]*\r\nMOVL (C\d{5})", body).group(1)
        q = [int(v) / r for v, r in zip(recs[ref].split(","), hc10.PULSES_PER_DEG)]
        self.assertTrue(hc10._within_limits(q), q)
        f = hc10.fk(q)["flange"]
        tcp = f[:3, 3] + f[:3, :3] @ cell_hc.HC_TOOL0_TCP_FLANGE_MM + cell_hc.ROBOT_BASE
        np.testing.assert_allclose(tcp - ORIGIN, [30.0, 15.0, 2.9], atol=0.5)

    def test_comment_lines_within_pendant_32_char_limit(self):


        job = _render_hc_jbi(
            _toolpath(), cell_hc.hc_solver(ORIGIN),
            name="HC-WALL-TEST", date="2026/02/20 18:14", arc_file=13,
            provenance=("platter attested by test_operator",),
            comments=("x" * 80,),
        )
        comment_lines = [ln for ln in job.split("\r\n") if ln.startswith("'")]
        self.assertTrue(comment_lines)
        for ln in comment_lines:
            self.assertLessEqual(len(ln) - 1, 32,
                                 f"comment over the 32-char pendant limit: {ln!r}")


class TestHcEmitGate(unittest.TestCase):
    def test_public_emit_refuses(self):


        with self.assertRaisesRegex(ValueError, "HC10 pulse emit is disabled"):
            emit_hc_jbi(_toolpath(), cell_hc.hc_solver(ORIGIN),
                        name="HC-WALL-TEST", date="2026/02/20 18:14", arc_file=13)

    def test_bad_name_and_bad_arc_file_refused_by_renderer(self):


        for bad in ("bad name", "", "x" * 33, "job#1"):
            with self.assertRaises(ValueError):
                _render_hc_jbi(_toolpath(), cell_hc.hc_solver(ORIGIN),
                               name=bad, date="2026/02/20 18:14", arc_file=13)
        for bad_arc in (0, 1001, "12", 12.0, True):
            with self.assertRaises(ValueError):
                _render_hc_jbi(_toolpath(), cell_hc.hc_solver(ORIGIN),
                               name="HC-WALL-TEST", date="2026/02/20 18:14",
                               arc_file=bad_arc)


if __name__ == "__main__":
    unittest.main()
