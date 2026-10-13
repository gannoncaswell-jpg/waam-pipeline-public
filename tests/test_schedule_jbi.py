import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.backends.jbi import emit_inform_preview
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.frames import CellConfig
from waam_pipeline.schedule import Schedule, WeldParams
from waam_pipeline.slicer import wall

CFG = CellConfig(
    name="test", robot="hc10", tool_number=0, user_frame=1, arc_file=69,
    between_layer_job="JOBWAAM", ctwd_mm=12.7, safe_z_mm=50.0,
)

INIT_ROS = Path.home() / "motoros2/robot_jobs/single_arm/INIT_ROS.JBI"


class TestSchedule(unittest.TestCase):
    def test_resolution_last_wins(self):
        s = Schedule([
            (0, 10, WeldParams(wfs=250, volts=18.0)),
            (5, 10, WeldParams(wfs=200, volts=17.0)),
        ])
        self.assertEqual(s.for_layer(2).wfs, 250)
        self.assertEqual(s.for_layer(7).wfs, 200)
        self.assertIsNone(s.for_layer(11))

    def test_ramp_interpolation(self):
        s = Schedule.ramp(0, 10, wfs=(250, 200), volts=18.0)
        self.assertAlmostEqual(s.for_layer(0).wfs, 250)
        self.assertAlmostEqual(s.for_layer(10).wfs, 200)
        self.assertAlmostEqual(s.for_layer(5).wfs, 225)
        self.assertAlmostEqual(s.for_layer(5).volts, 18.0)

    def test_arcset_dialects(self):
        self.assertEqual(
            WeldParams(wfs=250, volts=18.0).arcset_line(1),
            "ARCSET PROC#(1) WFS=250 V=18.0",
        )
        self.assertEqual(WeldParams(asf=70).arcset_line(1), "ARCSET ASF#(70)")
        self.assertIsNone(WeldParams(dwell_s=5).arcset_line(1))


class TestPreviewIntegration(unittest.TestCase):
    def test_arcset_emitted_only_on_change(self):
        tp = wall(80.0, layers=4, layer_height_mm=3.0)
        s = Schedule([
            (0, 1, WeldParams(wfs=250, volts=18.0)),
            (2, 3, WeldParams(wfs=210, volts=17.5, dwell_s=8.0)),
        ])
        text = emit_inform_preview(tp, CFG, s)


        proc = CFG.proc
        self.assertEqual(text.count("ARCSET"), 2)
        self.assertIn(f"ARCSET PROC#({proc}) WFS=250 V=18.0", text)
        self.assertIn(f"ARCSET PROC#({proc}) WFS=210 V=17.5", text)
        self.assertIn("TIMER T=8.00", text)

        lines = text.splitlines()
        i2 = lines.index(f"ARCSET PROC#({proc}) WFS=210 V=17.5")
        self.assertEqual(lines[i2 - 1], "TIMER T=8.00")
        self.assertEqual(lines[i2 - 2], "CALL JOB:JOBWAAM")

    def test_the_arc_off_token_is_arcof(self):


        text = emit_inform_preview(wall(80.0, layers=2, layer_height_mm=3.0), CFG)
        self.assertIn("ARCOF", text)
        self.assertNotIn("ARCOFF", text)

    def test_no_schedule_unchanged(self):
        tp = wall(80.0, layers=2, layer_height_mm=3.0)
        self.assertNotIn("ARCSET", emit_inform_preview(tp, CFG))


class TestJbiReader(unittest.TestCase):
    def test_reads_yaskawa_init_ros(self):
        if not INIT_ROS.exists():
            self.skipTest("motoros2 mirror not present")
        job = read_jbi(INIT_ROS)
        self.assertEqual(job.name, "INIT_ROS")
        self.assertEqual(job.npos, (0, 0, 0, 0, 0, 0))
        self.assertEqual(job.positions, {})
        self.assertEqual(job.instructions[0], "NOP")
        self.assertEqual(job.instructions[-1], "END")
        self.assertIn("DOUT OT#(890) OFF", job.instructions)
        self.assertIn("RB1", job.groups)
        self.assertFalse(job.uses_station_axis)

    def test_reads_synthetic_station_job(self):
        text = (
            "/JOB\n//NAME COORD1\n//POS\n///NPOS 2,0,0,0,0,0\n"
            "///POSTYPE PULSE\nC00000=0,1,2,3,4,5\nC00001=10,11,12,13,14,15\n"
            "//INST\n///DATE 2026/08/29\n///ATTR SC,RW\n///GROUP1 RB1,ST1\n"
            "NOP\nSMOVL C00000 V=80.0\nEND\n"
        )
        job = read_jbi(text)
        self.assertEqual(job.name, "COORD1")
        self.assertEqual(job.npos[0], 2)
        self.assertEqual(job.positions["C00001"], "10,11,12,13,14,15")
        self.assertEqual(job.pos_type, "PULSE")
        self.assertTrue(job.uses_station_axis)

    def test_missing_instructions_raises(self):
        with self.assertRaises(ValueError):
            read_jbi("/JOB\n//NAME BAD\n")


if __name__ == "__main__":
    unittest.main()
