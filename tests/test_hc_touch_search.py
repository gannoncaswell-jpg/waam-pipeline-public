import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_hc, cell_lc, touch
from waam_pipeline.backends.jbi import (HC_PARK_PULSES, PARK_PULSES,
                                        _render_hc_search_jbi, _render_search_jbi,
                                        emit_hc_search_jbi)
from waam_pipeline.robots import hc10

ORIGIN = np.zeros(3)


def _plan():
    return cell_hc.hc_probe_plan(cell_hc.TABLE_Z, solve=cell_hc.hc_solver(ORIGIN))


class TestHcSearchRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = _plan()

        cls.job = _render_hc_search_jbi(cls.plan, cell_hc.hc_solver(ORIGIN),
                                        name="HC-CALIB-TOUCH",
                                        date="2026/09/18 00:00", rin=3)

    def test_header_group_postype_and_terminator(self):
        self.assertTrue(self.job.startswith("/JOB\r\n//NAME HC-CALIB-TOUCH\r\n//POS\r\n"))
        self.assertIn("///POSTYPE PULSE", self.job)
        self.assertIn("///GROUP1 RB1", self.job)
        self.assertTrue(self.job.endswith("END\r\n"))

    def test_crlf_line_endings(self):

        self.assertIn("\r\n", self.job)
        self.assertNotIn("\r\r\n", self.job)

    def test_npos_equals_c_moves_equals_c_records(self):


        npos = int(re.search(r"///NPOS (\d+),", self.job).group(1))
        c_moves = re.findall(r"^MOV[JL] C\d{5}", self.job, re.MULTILINE)
        n_records = len(re.findall(r"^C\d{5}=", self.job, re.MULTILINE))
        self.assertEqual(npos, len(c_moves))
        self.assertEqual(npos, n_records)
        self.assertEqual(npos, 2 + 2 * len(self.plan))

    def test_position_records_are_robot_only_six_fields(self):


        recs = re.findall(r"^C\d{5}=(.+)$", self.job, re.MULTILINE)
        self.assertTrue(recs)
        for rec in recs:
            vals = rec.split(",")
            self.assertEqual(len(vals), 6, rec)
            for v in vals:
                int(v)

    def test_park_record_is_the_taught_hc_home(self):

        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", self.job, re.MULTILINE))
        body = self.job.split("//INST", 1)[1]
        moves = re.findall(r"MOV[JL] (C\d{5})", body)
        park = ",".join(str(p) for p in HC_PARK_PULSES)
        self.assertEqual(recs[moves[0]], park)
        self.assertEqual(recs[moves[-1]], park)

    def test_pulse_scale_is_hc10_not_ar1440(self):


        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", self.job, re.MULTILINE))
        q = [int(v) / r for v, r in zip(recs["C00001"].split(","), hc10.PULSES_PER_DEG)]
        self.assertTrue(hc10._within_limits(q), q)

    def test_rin3_wire_touch_io_and_search(self):

        self.assertIn("DOUT OT#(1891) ON", self.job)
        self.assertIn("DOUT OT#(1891) OFF", self.job)
        self.assertIn("SRCH RIN#(3)=ON", self.job)
        self.assertIn("WAIT IN#(1891)=OFF", self.job)
        self.assertNotIn("4037", self.job)

    def test_result_pvars_one_per_probe_from_base(self):
        pvars = re.findall(r"GETS (PX0\d\d) \$PX002", self.job)
        self.assertEqual(pvars, [f"PX{40 + i:03d}" for i in range(len(self.plan))])

    def test_unconfirmed_rin_flagged_in_comment(self):

        self.assertIn("HC RIN UNCONFIRMED-CHK PENDANT", self.job)
        self.assertIn("TOUCH-SENSE PROBE - REVIEW REQ", self.job)
        self.assertIn("NOT auto-runnable", self.job)

    def test_comment_lines_within_pendant_32_char_limit(self):


        job = _render_hc_search_jbi(self.plan, cell_hc.hc_solver(ORIGIN),
                                    name="HC-CALIB-TOUCH", date="2026/09/18 00:00",
                                    rin=3, provenance=("platter attested by test_operator",))
        comment_lines = [ln for ln in job.split("\r\n") if ln.startswith("'")]
        self.assertTrue(comment_lines)
        for ln in comment_lines:
            self.assertLessEqual(len(ln) - 1, 32,
                                 f"comment over the 32-char pendant limit: {ln!r}")

    def test_nested_lines_indent_with_tab_not_spaces(self):


        self.assertNotRegex(self.job, r"(?m)^ ")
        self.assertIn('\t SETUALM', self.job)


class TestHcProbePlan(unittest.TestCase):
    def test_plan_non_empty_and_reachable(self):
        plan = _plan()
        self.assertTrue(plan)
        solve = cell_hc.hc_solver(ORIGIN)
        for pr in plan:
            solve(pr.start)
            solve(pr.expected)

    def test_points_over_the_table_within_bounds(self):
        for pr in _plan():
            self.assertTrue(cell_hc.TABLE_XY_MIN[0] < pr.expected[0] < cell_hc.TABLE_XY_MAX[0])
            self.assertTrue(cell_hc.TABLE_XY_MIN[1] < pr.expected[1] < cell_hc.TABLE_XY_MAX[1])

            self.assertGreater(pr.start[2], pr.expected[2])
            self.assertLess(pr.target[2], pr.expected[2])


class TestHcSearchGate(unittest.TestCase):
    def test_public_emit_refuses(self):


        with self.assertRaisesRegex(ValueError, "HC10 touch-search export is disabled"):
            emit_hc_search_jbi(_plan(), cell_hc.hc_solver(ORIGIN),
                               name="HC-CALIB-TOUCH", date="2026/09/18 00:00")

    def test_empty_plan_refused(self):
        with self.assertRaises(ValueError):
            _render_hc_search_jbi([], cell_hc.hc_solver(ORIGIN), name="X",
                                  date="2026/09/18 00:00")

    def _run_cli(self, out: Path, *extra: str) -> int:
        import importlib.util
        old = sys.argv
        sys.argv = ["gen_hc_touch_job", "--out", str(out),
                    "--date", "2026/09/18 00:00", *extra]
        try:
            spec = importlib.util.spec_from_file_location(
                "gen_hc_touch_job", ROOT / "examples" / "gen_hc_touch_job.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with contextlib.redirect_stdout(io.StringIO()):
                return mod.main()
        finally:
            sys.argv = old

    def test_the_unchecked_render_needs_experimental_and_is_marked(self):


        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "HC-CALIB-TOUCH.JBI"
            self.assertEqual(self._run_cli(out), 1)
            self.assertFalse(out.exists())

            self.assertEqual(self._run_cli(out, "--experimental"), 0)
            self.assertFalse(out.exists(), "unchecked job landed at the plain name")
            marked = out.with_name("HC-CALIB-TOUCH-EXPERIMENTAL.JBI")
            text = marked.read_text(encoding="utf-8")
            self.assertIn("//NAME HC-CALIB-TOUCH-EXPERIMENTAL", text)
            self.assertIn("clearance NO - UNCHECKED", text)

    def test_cli_writes_bytes_not_text_mode(self):


        src = (ROOT / "examples" / "gen_hc_touch_job.py").read_text(encoding="utf-8")
        self.assertIn("write_job(", src)
        self.assertNotIn("write_text(job", src)


class TestLcPathUnshifted(unittest.TestCase):


    def test_lc_platter_plan_still_ar1440_park_and_rin3(self):
        plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z)
        job = _render_search_jbi(plan, cell_lc.pulse_solver(np.zeros(3)),
                                 name="R1_WT", date="2026/09/18 00:00", rin=3)
        recs = dict(re.findall(r"^(C\d{5})=([^\r\n]+)", job, re.MULTILINE))
        body = job.split("//INST", 1)[1]
        moves = re.findall(r"MOV[JL] (C\d{5})", body)
        self.assertEqual(recs[moves[0]], ",".join(str(p) for p in PARK_PULSES))
        self.assertNotEqual(recs[moves[0]], ",".join(str(p) for p in HC_PARK_PULSES))
        self.assertIn("SRCH RIN#(3)=ON", job)
        self.assertIn("DOUT OT#(1891) ON", job)


if __name__ == "__main__":
    unittest.main()
