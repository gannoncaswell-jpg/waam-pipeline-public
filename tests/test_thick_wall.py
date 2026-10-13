import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams, demos, server
from waam_pipeline.slicer import wall
from tests._support import IsolatedServerCase
from tests.test_sense_server import registration

FIVE_EIGHTHS = 15.875
LO, HI = arcparams.STEPOVER_FRACTION_RANGE


class TestWallBeads(unittest.TestCase):
    def test_five_eighths_from_9_5_mm_beads_is_two_beads_edge_to_edge(self):

        b = arcparams.wall_beads(FIVE_EIGHTHS, 9.5, 3.2 / 1.5)
        self.assertEqual(b.count, 2)
        self.assertAlmostEqual(9.5 + (b.count - 1) * b.spacing_mm, FIVE_EIGHTHS)
        self.assertAlmostEqual(b.fraction, b.spacing_mm / 9.5)
        self.assertTrue(LO <= b.fraction <= HI)
        self.assertEqual(b.optimum, arcparams.optimal_stepover_fraction(9.5, 3.2))
        self.assertEqual(arcparams.wall_beads(FIVE_EIGHTHS, 9.5, 3.2).count, 2)

    def test_the_count_nearest_the_beads_own_optimum_wins(self):


        fractions = {n: (60.0 - 8.6) / (n - 1) / 8.6 for n in (9, 10)}
        self.assertTrue(all(LO <= f <= HI for f in fractions.values()))
        b = arcparams.wall_beads(60.0, 8.6, 2.9 / 1.5)
        self.assertEqual(b.count, min(fractions, key=lambda n: abs(fractions[n] - b.optimum)))
        self.assertEqual(b.count, 10)

    def test_a_wall_as_thick_as_the_bead_is_one_bead(self):
        self.assertEqual(arcparams.wall_beads(8.6, 8.6, 2.9).count, 1)

    def test_a_thickness_between_counts_takes_the_nearest_count_with_a_note(self):
        wide = arcparams.wall_beads(FIVE_EIGHTHS, 8.6, 2.9)
        self.assertEqual(wide.count, 2)
        self.assertAlmostEqual(wide.spacing_mm, FIVE_EIGHTHS - 8.6)
        self.assertGreater(wide.fraction, HI)
        self.assertIn("lack of fusion", wide.note)
        tight = arcparams.wall_beads(10.0, 8.6, 2.9)
        self.assertEqual(tight.count, 2)
        self.assertLess(tight.fraction, LO)
        self.assertIn("ridged", tight.note)

    def test_a_wall_thinner_than_one_bead_lays_one_bead_with_a_note(self):
        b = arcparams.wall_beads(5.0, 8.6, 2.9)
        self.assertEqual(b.count, 1)
        self.assertIn("thinner than one 8.6 mm bead", b.note)

    def test_only_crowded_and_bad_walls_are_refused(self):
        with self.assertRaisesRegex(ValueError, "more than 50"):
            arcparams.wall_beads(300.0, 2.4, 1.0)
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(thickness=bad), self.assertRaises(ValueError):
                arcparams.wall_beads(bad, 8.6, 2.9)


class TestBeadsOutsideTheModel(unittest.TestCase):

    def test_a_four_by_four_bead_is_warned_not_refused(self):
        b = arcparams.wall_beads(FIVE_EIGHTHS, 4.0, 4.0)
        floor = arcparams.optimal_stepover_fraction(arcparams.STEPOVER_WHR_RANGE[0], 1.0)
        self.assertEqual(b.optimum, floor)
        self.assertEqual(b.count, 5)
        self.assertAlmostEqual(b.spacing_mm, (FIVE_EIGHTHS - 4.0) / 4)
        self.assertTrue(LO <= b.fraction <= HI)
        for words in ("w/h 0.67", "6 mm crown", "outside the overlap model", "Mueller & Hensel"):
            self.assertIn(words, b.note)

    def test_beads_inside_the_model_carry_no_note(self):
        self.assertEqual(arcparams.wall_beads(FIVE_EIGHTHS, 9.5, 3.2 / 1.5).note, "")

    def test_a_very_flat_bead_aims_at_the_ceiling_without_the_fusion_warning(self):
        b = arcparams.wall_beads(30.0, 10.0, 0.3)
        self.assertEqual(b.optimum, arcparams.optimal_stepover_fraction(arcparams.STEPOVER_WHR_RANGE[1], 1.0))
        self.assertIn("outside the overlap model", b.note)
        self.assertNotIn("fuse", b.note)

    def test_a_thickness_no_count_fits_takes_the_nearest_overlapping_count(self):

        b = arcparams.wall_beads(8.0, 4.0, 4.0)
        self.assertEqual((b.count, b.spacing_mm), (3, 2.0))
        self.assertIn("50% of the bead width", b.note)
        self.assertIn("ridged", b.note)


class TestThickWallToolpath(unittest.TestCase):
    tp = demos.demo_wall(100.0, 3, 3.2, 175.0, thickness_mm=FIVE_EIGHTHS, bead_width_mm=9.5)

    def test_thickness_zero_or_one_bead_is_the_single_bead_wall(self):
        single = wall(90.0, 4, 3.2, 325.0).moves
        self.assertEqual(demos.demo_wall(90.0, 4, 3.2, 325.0).moves, single)
        self.assertEqual(demos.demo_wall(90.0, 4, 3.2, 325.0, thickness_mm=9.5, bead_width_mm=9.5).moves,
                         single)

    def test_every_layer_lays_both_beads_with_the_outer_edges_on_the_thickness(self):
        self.assertEqual(len(self.tp.layers()), 3)
        self.assertEqual([e[0] for e in self.tp.arc_events()], ["ARCON", "ARCOFF"] * 6)
        self.assertAlmostEqual(self.tp.deposit_length(), 6 * 100.0)
        ys = sorted({m.y for m in self.tp.moves if m.deposit})
        self.assertAlmostEqual(ys[-1] - ys[0] + 9.5, FIVE_EIGHTHS)
        self.assertAlmostEqual(ys[0] + ys[-1], 0.0)
        self.assertEqual({m.feed for m in self.tp.moves if m.deposit}, {175.0})

    def test_beads_rise_out_of_each_crater_and_layers_start_above_the_last(self):
        moves = self.tp.moves
        for i, m in enumerate(moves[:-1]):
            nxt = moves[i + 1]
            if not m.deposit or nxt.deposit:
                continue
            with self.subTest(move=i):
                self.assertEqual((nxt.x, nxt.y), (m.x, m.y))
                if nxt.layer == m.layer:
                    self.assertAlmostEqual(nxt.z - m.z, demos.BEAD_LIFT_MM)
                else:
                    self.assertAlmostEqual(nxt.z - m.z, 3.2)

        welds = [(moves[i - 1].x, m.x) for i, m in enumerate(moves) if m.deposit]
        self.assertTrue(all(a[1] == b[0] for a, b in zip(welds, welds[1:])))

    def test_refusals_reach_the_caller(self):
        tp = demos.demo_wall(100.0, 3, 2.9, thickness_mm=FIVE_EIGHTHS, bead_width_mm=8.6)
        self.assertEqual(sum(m.deposit for m in tp.moves), 6)
        with self.assertRaisesRegex(ValueError, "bead width"):
            demos.demo_wall(100.0, 3, 2.9, thickness_mm=FIVE_EIGHTHS)


class TestServedThickWall(IsolatedServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.raw = json.dumps(registration())

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "_STATES", {}).start()
        mock.patch.object(server, "_SHOW_CELL", True).start()
        mock.patch.object(server, "_HOSTED_ASSETS", False).start()
        mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test").start()

    def request(self, route, params, method="GET"):
        req = urllib.request.Request(self.base + route + "?" + urlencode(params),
                                     data=b"" if method == "POST" else None, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read()

    def slice(self, **params):
        code, body = self.request("/api/slice", dict(dict(kind="wall", shape="wall", fixture="0", lh="3.2",
                                                          bead="9.5", travel="175"), **params), method="POST")
        self.assertEqual(code, 200, body[:300])
        return json.loads(body)

    def test_five_eighths_wall_checks_clean_and_emits_one_arc_per_bead(self):
        result = self.slice(wall_length="101.6", wall_layers="4", wall_thickness=str(FIVE_EIGHTHS),
                            substrate_registration=self.raw)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["blocking"], result["unreachable"]), (0, 0))
        meta = result["state"]["meta"]
        self.assertEqual(meta["params"]["wall_thickness"], str(FIVE_EIGHTHS))
        self.assertEqual(meta["slicing"]["wall"]["beads"], 2)
        self.assertAlmostEqual(server._state("local")["toolpath"].deposit_length(), 8 * 101.6, places=6)
        code, body = self.request("/api/jbi", dict(clear="1", block="0", by="t", arc="81", name="WALL58",
                                                   gen=server._state("local").get("gen")))
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(body.decode("ascii").count("ARCON ASF#(81)"), 8)

    def test_four_by_four_beads_slice_emit_and_carry_the_warning(self):
        result = self.slice(wall_length="101.6", wall_layers="3", wall_thickness=str(FIVE_EIGHTHS),
                            lh="4", bead="4", substrate_registration=self.raw)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual((result["blocking"], result["unreachable"]), (0, 0))
        wall_info = result["state"]["meta"]["slicing"]["wall"]
        self.assertEqual(wall_info["beads"], 5)
        self.assertIn("Mueller & Hensel", wall_info["note"])
        code, body = self.request("/api/jbi", dict(clear="1", block="0", by="t", arc="81", name="WALL44",
                                                   gen=server._state("local").get("gen")))
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(body.decode("ascii").count("ARCON ASF#(81)"), 15)

    def test_a_saved_thick_wall_reloads_with_its_thickness(self):
        self.slice(wall_length="120", wall_layers="3", wall_thickness=str(FIVE_EIGHTHS))
        code, body = self.request("/api/save", dict(name="wall58", gen=server._state("local").get("gen")),
                                  method="POST")
        self.assertEqual(code, 200, body[:300])
        saved = json.loads((server.SAVES_DIR / "local" / "wall58" / "request.json").read_text())
        self.assertEqual(saved["params"]["wall_thickness"], str(FIVE_EIGHTHS))
        self.slice(wall_thickness="0")
        code, body = self.request("/api/load", dict(name="wall58"), method="POST")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(len([m for m in server._state("local")["toolpath"].moves if m.deposit]), 6)

    def test_a_build_saved_before_the_speed_fix_loads_at_the_speed_it_ran(self):


        self.slice(wall_length="120", wall_layers="3", travel="133")
        code, body = self.request("/api/save", dict(name="old133", gen=server._state("local").get("gen")),
                                  method="POST")
        self.assertEqual(code, 200, body[:300])
        path = server.SAVES_DIR / "local" / "old133" / "request.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["v_unit"], "mm/s")
        reply = json.loads(self.request("/api/load", dict(name="old133"), method="POST")[1])
        self.assertEqual((reply["params"]["travel"], "note" in reply), ("133", False))
        del saved["v_unit"]
        path.write_text(json.dumps(saved))
        reply = json.loads(self.request("/api/load", dict(name="old133"), method="POST")[1])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["params"]["travel"], "798")
        self.assertIn("Travel of 133 mm/min ran at 798 mm/min", reply["note"])
        self.assertEqual({m.feed for m in server._state("local")["toolpath"].moves if m.deposit}, {798.0})
        self.assertIsNone(server._speed_fix_note({}, {"cell_profile": "hc10", "travel": "133"}))

    def test_a_thickness_outside_the_model_slices_with_its_note(self):
        result = self.slice(wall_thickness=str(FIVE_EIGHTHS), bead="8.6", lh="2.9")
        self.assertTrue(result["ok"], result.get("error"))
        wall_info = result["state"]["meta"]["slicing"]["wall"]
        self.assertEqual(wall_info["beads"], 2)
        self.assertIn("lack of fusion", wall_info["note"])

    def test_other_demos_ignore_it_and_the_thermal_model_refuses_it(self):
        result = self.slice(shape="bead", wall_thickness="oops")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertNotIn("wall_thickness", result["state"]["meta"]["params"])
        result = self.slice(wall_thickness=str(FIVE_EIGHTHS), thermal="illustrative")
        self.assertFalse(result["ok"])
        self.assertIn("2 beads per layer", result["error"])
        self.assertIn("overlap or remelting", result["error"])


if __name__ == "__main__":
    unittest.main()
