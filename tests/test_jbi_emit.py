import re
import unittest

import numpy as np

from waam_pipeline import cell_lc
from waam_pipeline.backends.jbi import PARK_PULSES, emit_jbi_pulse
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.robots import ar1440
from waam_pipeline.slicer import wall

ORIGIN = np.array([1014.0, -300.0, cell_lc.platter_work_surface_z(6.35)])


def make_job(layers=2):
    tp = wall(120.0, layers, 2.0)
    text = emit_jbi_pulse(
        tp,
        cell_lc.pulse_solver(ORIGIN),
        name="TEST-WALL",
        date="2026/09/02 00:00",
        arc_file=12,
    )
    return tp, text


class TestEmitJbiPulse(unittest.TestCase):
    def test_header_and_roundtrip(self):
        _, text = make_job()
        self.assertTrue(text.startswith("/JOB\r\n//NAME TEST-WALL\r\n//POS\r\n"))
        self.assertIn("\r\n///TOOL 0\r\n///POSTYPE PULSE\r\n///PULSE\r\n", text)
        self.assertTrue(text.endswith("END\r\n"))
        job = read_jbi(text.replace("\r\n", "\n"))
        self.assertEqual(job.groups, ["RB1"])
        n_c = sum(1 for k in job.positions if k.startswith("C"))
        self.assertEqual(job.npos[0], n_c)

        for ln in job.instructions:
            if ln.startswith(("MOVJ C", "MOVL C")):
                self.assertIn(ln.split()[1], job.positions)

    def test_npos_equals_c_move_count(self):


        _, text = make_job(layers=3)
        npos = int(re.search(r"///NPOS (\d+),", text).group(1))
        c_moves = re.findall(r"^MOV[JL] C\d{5}", text, re.MULTILINE)
        n_records = len(re.findall(r"^C\d{5}=", text, re.MULTILINE))
        self.assertEqual(npos, len(c_moves))
        self.assertEqual(npos, n_records)

    def test_bad_job_name_refused(self):


        tp = wall(120.0, 2, 2.0)
        for bad in ("bad name", "", "x" * 33, "job#1", "a/b"):
            with self.assertRaises(ValueError):
                emit_jbi_pulse(tp, cell_lc.pulse_solver(ORIGIN),
                               name=bad, date="2026/09/02 00:00", arc_file=12)
        _, text = make_job()
        self.assertIn("//NAME TEST-WALL", text)

    def test_comment_lines_within_pendant_32_char_limit(self):


        tp = wall(120.0, 2, 2.0)
        text = emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN),
            name="TEST-WALL", date="2026/09/02 00:00", arc_file=12,
            provenance=("platter attested by test_operator",),
            comments=("x" * 80,),
        )
        comment_lines = [ln for ln in text.replace("\r\n", "\n").split("\n")
                         if ln.startswith("'")]
        self.assertTrue(comment_lines)
        for ln in comment_lines:
            self.assertLessEqual(len(ln) - 1, 32,
                                 f"comment over the 32-char pendant limit: {ln!r}")

    def test_park_record_matches_lab_home(self):
        _, text = make_job()
        job = read_jbi(text.replace("\r\n", "\n"))

        self.assertEqual(job.positions["C00000"], "0,-60000,-60000,0,-90000,0")
        self.assertEqual(
            list(PARK_PULSES),
            [int(v) for v in job.positions["C00000"].split(",")],
        )

    def test_arc_structure(self):
        tp, text = make_job(layers=3)
        job = read_jbi(text.replace("\r\n", "\n"))
        ins = job.instructions
        self.assertEqual(sum(1 for l in ins if l.startswith("ARCON")), 3)
        self.assertEqual(sum(1 for l in ins if l.startswith("ARCOF")), 3)


        for i, ln in enumerate(ins):
            if ln.startswith("ARCON"):
                self.assertEqual(ln, "ARCON ASF#(12)")
                self.assertTrue(ins[i + 1].startswith("MOVL C"))
                self.assertIn("V=13.9", ins[i + 1])
                self.assertEqual(ins[i + 2], "ARCOF AEF#(12)")

    def test_positions_reach_targets(self):
        tp, text = make_job()
        job = read_jbi(text.replace("\r\n", "\n"))


        weld_targets = [m for m in tp.moves if m.deposit]
        ins = job.instructions
        weld_refs = [
            ins[i + 1].split()[1]
            for i, ln in enumerate(ins)
            if ln.startswith("ARCON")
        ]
        self.assertEqual(len(weld_refs), len(weld_targets))
        for ref, m in zip(weld_refs, weld_targets):
            q = [int(v) / r for v, r in
                 zip(job.positions[ref].split(","), ar1440.PULSES_PER_DEG)]
            f = ar1440.fk(q)["flange"]
            tip = f[:3, 3] + f[:3, :3] @ cell_lc.TOOL_TCP
            expect = np.array([m.x, m.y, m.z]) + ORIGIN
            self.assertLess(float(np.linalg.norm(tip - expect)), 0.6, ref)

    def test_wrist_stays_near_neutral(self):
        _, text = make_job()
        job = read_jbi(text.replace("\r\n", "\n"))
        for key, rec in job.positions.items():
            if key == "C00000":
                continue
            q = [int(v) / r for v, r in zip(rec.split(","), ar1440.PULSES_PER_DEG)]
            self.assertLess(abs(q[3]), 30.0, f"{key} R axis contorted")
            self.assertLess(abs(q[5]), 30.0, f"{key} T axis contorted")

    def test_unreachable_raises(self):
        tp = wall(120.0, 1, 2.0)
        far = np.array([3000.0, 0.0, 0.0])
        with self.assertRaises(ValueError):
            emit_jbi_pulse(
                tp,
                cell_lc.pulse_solver(far),
                name="BAD",
                date="2026/09/02 00:00",
                arc_file=12,
            )


if __name__ == "__main__":
    unittest.main()
