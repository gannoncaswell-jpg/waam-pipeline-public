import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import ParseError, parse


class TestSquareExample(unittest.TestCase):
    def setUp(self):
        self.tp = parse((ROOT / "examples" / "square.gcode").read_text(encoding="utf-8"))

    def test_layers_and_moves(self):
        self.assertEqual(len(self.tp.moves), 12)
        self.assertEqual(len(self.tp.layers()), 2)
        self.assertEqual(sum(1 for m in self.tp.moves if m.deposit), 8)

    def test_arc_events(self):
        starts = [i for kind, i in self.tp.arc_events() if kind == "ARCON"]
        stops = [i for kind, i in self.tp.arc_events() if kind == "ARCOFF"]
        self.assertEqual(starts, [1, 7])
        self.assertEqual(stops, [5, 11])

    def test_geometry(self):
        (x0, x1), (y0, y1), (z0, z1) = self.tp.bbox()
        self.assertEqual((x0, x1, y0, y1), (0.0, 50.0, 0.0, 50.0))
        self.assertEqual((z0, z1), (0.0, 13.0))
        self.assertAlmostEqual(self.tp.deposit_length(), 400.0)

    def test_feed_carried_and_converted(self):
        deposits = [m for m in self.tp.moves if m.deposit]
        self.assertTrue(all(m.feed == 480.0 for m in deposits))


class TestDialect(unittest.TestCase):
    def test_inches_converted(self):
        tp = parse("G20\nG90\nG0 X1 Y0 Z0\nG1 X2 E1 F10\n")
        self.assertAlmostEqual(tp.moves[0].x, 25.4)
        self.assertAlmostEqual(tp.moves[1].x, 50.8)
        self.assertAlmostEqual(tp.moves[1].feed, 254.0)
        self.assertTrue(tp.moves[1].deposit)

    def test_relative_extrusion_m83(self):
        tp = parse("G21\nG90\nM83\nG0 X0 Y0 Z0\nG1 X10 E1\nG1 X20 E1\n")
        self.assertEqual([m.deposit for m in tp.moves], [False, True, True])

    def test_g92_e_reset(self):
        tp = parse("G90\nG1 X10 E5\nG92 E0\nG1 X20 E5\n")
        self.assertEqual([m.deposit for m in tp.moves], [True, True])

    def test_g92_xyz_is_refused(self):


        for line in ("G92 X0", "G92 Y0", "G92 Z0", "G92 X0 Y0 Z0 E0"):
            with self.subTest(line=line):
                with self.assertRaises(ParseError):
                    parse("\n".join(["G90", "G1 X10 E5", line, "G1 X20 E5", ""]))

    def test_modal_motion(self):
        tp = parse("G90\nG1 X5 E1\nX10 E2\n")
        self.assertEqual(len(tp.moves), 2)
        self.assertTrue(tp.moves[1].deposit)
        self.assertAlmostEqual(tp.moves[1].x, 10.0)

    def test_arcs_rejected(self):
        with self.assertRaises(ParseError):
            parse("G90\nG2 X10 Y10 I5 J0\n")

    def test_comments_and_layer_markers(self):
        tp = parse("G90\n(setup) G1 X5 E1 ; first\n;LAYER:1\nG1 X10 E2\n")
        self.assertEqual([m.layer for m in tp.moves], [0, 1])

    def test_z_heuristic_without_layer_comments(self):
        tp = parse("G90\nG1 X10 E1\nG1 X0 Z2 E2\nG1 X10 Z4 E3\n")
        self.assertEqual([m.layer for m in tp.moves], [0, 1, 2])

    def test_explicit_arc_dialect(self):


        tp = parse(
            "G90\nG0 X0 Y0 Z0\nG82\nG1 X10\nG1 X20\nG83 [1]\nG0 X0 Z5\n",
            arc_on="G82",
            arc_off="G83",
        )
        self.assertEqual([m.deposit for m in tp.moves], [False, True, True, False])
        self.assertEqual(tp.arc_events(), [("ARCON", 1), ("ARCOFF", 3)])

    def test_unhandled_recorded_not_fatal(self):
        tp = parse("G90\nM104 S0\nG4 P100\nG1 X5 E1\n")
        self.assertEqual(len(tp.moves), 1)
        self.assertEqual(len(tp.unhandled), 2)


class TestNoSilentWords(unittest.TestCase):
    def test_number_running_into_e_is_refused(self):

        for line in ("G1 X20 Y2.5e2", "G1 X10 Y1e-05 E0.1", "G1 X10E0.5", "G1 X1.E-2"):
            with self.subTest(line=line), self.assertRaisesRegex(ParseError, "ambiguous number"):
                parse("G90\nG0 X0 Y0\n" + line + "\n")
        self.assertEqual(parse("G1 X10 E0.5\n").moves[0].x, 10.0)

    def test_tool_power_and_dwell_words_on_moves_are_refused(self):
        for line in ("T1", "S500", "P3", "G1 X10 E1 T1", "G1 X10 S200", "G0 X5 R2"):
            with self.subTest(line=line), self.assertRaises(ParseError):
                parse("G90\nG1 X0 Y0 E0\n" + line + "\nG1 X20 E2\n")

    def test_t0_is_the_one_tool_and_is_recorded(self):
        tp = parse("T0\nG90\nG1 X0 Y0 E0\nG1 X10 E1 T0\n")
        self.assertEqual([m.x for m in tp.moves], [0.0, 10.0])
        self.assertEqual(tp.unhandled, [(1, "T0"), (4, "T0")])


if __name__ == "__main__":
    unittest.main()
