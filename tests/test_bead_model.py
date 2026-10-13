import math
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams, ornl, server

FIVE_EIGHTHS = 15.875


def lone_wfs_ipm(lh, w, travel=175.0):
    return lh * w * travel / arcparams.wire_area_mm2(arcparams.WIRE_045_MM) / 25.4


class TestPerBeadFeed(unittest.TestCase):
    def test_single_bead_is_unchanged(self):
        default = arcparams.solve(layer_height_mm=2.9, bead_width_mm=8.6, travel_mm_min=175)
        explicit = arcparams.solve(layer_height_mm=2.9, bead_width_mm=8.6, travel_mm_min=175,
                                   fill_width_mm=8.6)
        self.assertEqual(default.wfs_ipm, explicit.wfs_ipm)
        self.assertAlmostEqual(default.wfs_ipm, lone_wfs_ipm(2.9, 8.6), places=9)
        self.assertAlmostEqual(default.bead_area_mm2, 2.9 * 8.6, places=9)
        self.assertEqual(arcparams.wall_bead_area_mm2(8.6, 2.9, 1), 2.9 * 8.6)

    def test_two_bead_five_eighths_wall_feeds_thickness_x_pitch_over_two(self):
        beads = arcparams.wall_beads(FIVE_EIGHTHS, 9.5, 3.2)
        self.assertEqual(beads.count, 2)
        area = arcparams.wall_bead_area_mm2(FIVE_EIGHTHS, 3.2, beads.count)
        self.assertAlmostEqual(area, 25.4, places=9)
        sol = arcparams.solve(layer_height_mm=3.2, bead_width_mm=9.5, travel_mm_min=175,
                              fill_width_mm=FIVE_EIGHTHS / beads.count)
        self.assertAlmostEqual(sol.bead_area_mm2, 25.4, places=9)
        self.assertAlmostEqual(sol.wfs_ipm, 170.55, places=2)
        self.assertAlmostEqual(lone_wfs_ipm(3.2, 9.5), 204.12, places=2)
        self.assertAlmostEqual(lone_wfs_ipm(3.2, 9.5) / sol.wfs_ipm, 2 * 9.5 / FIVE_EIGHTHS, places=9)
        self.assertEqual(sol.bead_width_mm, 9.5)

    def test_pitch_inverts_the_bead_area(self):
        for t, lh, n in ((FIVE_EIGHTHS, 3.2, 2), (60.0, 2.9, 10), (8.6, 2.9, 1)):
            area = arcparams.wall_bead_area_mm2(t, lh, n)
            self.assertAlmostEqual(arcparams.predicted_layer_pitch_mm(area, t, n), lh, places=12)

    def test_bad_inputs_refuse(self):
        for args in ((0, 3.2, 2), (15.0, -1, 2), (15.0, 3.2, 0), (15.0, 3.2, 1.5), (15.0, 3.2, True)):
            with self.assertRaises(ValueError):
                arcparams.wall_bead_area_mm2(*args)
        with self.assertRaises(ValueError):
            arcparams.solve(layer_height_mm=3, bead_width_mm=9, travel_mm_min=175, fill_width_mm=0)


class TestPitchNote(unittest.TestCase):
    def test_within_ten_percent_is_quiet(self):
        self.assertEqual(arcparams.pitch_note(3.2 * 1.099, 3.2), "")
        self.assertEqual(arcparams.pitch_note(3.2 * 0.901, 3.2), "")

    def test_low_and_high_say_which_way_ctwd_moves(self):
        low = arcparams.pitch_note(2.6, 3.2)
        self.assertIn("19% below", low)
        self.assertIn("CTWD grows by about 0.60 mm per layer", low)
        high = arcparams.pitch_note(3.7, 3.2)
        self.assertIn("16% above", high)
        self.assertIn("contact tip closes on the part", high)


class TestCrownFedStepover(unittest.TestCase):


    def test_target_uses_the_crown(self):
        target, note = arcparams.stepover_target(9.5, 3.2 / 1.5)
        self.assertAlmostEqual(target, arcparams.optimal_stepover_fraction(9.5, 3.2))
        self.assertEqual(note, "")
        self.assertEqual(arcparams.crown_from_pitch(2.0), 3.0)

    def test_out_of_domain_aims_at_the_edge_with_a_note(self):
        target, note = arcparams.stepover_target(9.5, 3.2)
        self.assertAlmostEqual(target, arcparams.optimal_stepover_fraction(2.4, 1.0))
        self.assertIn("4.8 mm crown (1.5 x pitch, parabolic assumption)", note)
        self.assertIn("Mueller & Hensel", note)

    def test_review_cases(self):

        cases = {(FIVE_EIGHTHS, 8.6, 2.9): (2, 7.275), (FIVE_EIGHTHS, 9.5, 3.2 / 1.5): (2, 6.375),
                 (FIVE_EIGHTHS, 9.5, 3.2): (2, 6.375), (20.0, 9.5, 3.2 / 1.5): (3, 5.25),
                 (25.4, 9.5, 3.2 / 1.5): (4, 5.3), (20.0, 9.5, 3.2): (3, 5.25),
                 (25.4, 9.5, 3.2): (3, 7.95)}
        for (t, w, pitch), (n, d) in cases.items():
            with self.subTest(t=t, w=w, pitch=pitch):
                b = arcparams.wall_beads(t, w, pitch)
                self.assertEqual(b.count, n)
                self.assertAlmostEqual(b.spacing_mm, d)
                self.assertLess(b.fraction, 1.0)

    def test_an_overlapping_count_beats_a_gapped_one(self):


        b = arcparams.wall_beads(20.0, 9.5, 3.2)
        self.assertEqual(b.count, 3)
        self.assertNotIn("do not overlap", b.note)

    def test_only_gapped_counts_still_lay_with_a_lack_of_fusion_note(self):
        with mock.patch.object(arcparams, "WALL_MAX_BEADS", 2):
            b = arcparams.wall_beads(25.0, 9.5, 3.2)
        self.assertEqual(b.count, 2)
        self.assertGreater(b.fraction, 1.0)
        self.assertIn("do not overlap", b.note)
        self.assertIn("lack of fusion", b.note)

    def test_ornl_note_uses_the_same_target(self):
        note = arcparams.ornl_spacing_note(6.0, 2.0, 75)
        target, model_note = arcparams.stepover_target(6.0, 2.0)
        self.assertIn(f"Hu et al. 2019 suggests {target:.0%}", note)
        self.assertIn("ORNL is spacing at 75%", note)
        self.assertIn(model_note, note)


class TestWorkbenchArcSummary(unittest.TestCase):
    PARAMS = dict(kind="wall", shape="wall", cell="0", repair="0", fixture="0",
                  lh="3.2", bead="9.5", travel="175", wall_length="60", wall_layers="2")

    def arc(self, workspace, **extra):
        result = server.run_slice(dict(self.PARAMS, **extra), None, workspace=workspace)
        return result["state"]["arc"], result["state"]["arc_warnings"]

    def test_single_bead_wall_feed_is_unchanged(self):
        arc, notes = self.arc("bead-model-single")
        self.assertAlmostEqual(arc["wfs_ipm"], lone_wfs_ipm(3.2, 9.5), places=9)
        self.assertEqual(arc["beads_across"], 1)
        self.assertNotIn("predicted_pitch_mm", arc)
        self.assertFalse(any("per bead" in n for n in notes))
        self.assertAlmostEqual(arc["free_crown_mm"], 1.5 * 3.2)
        self.assertAlmostEqual(arc["free_pitch_mm"], 3.2)

    def test_two_bead_wall_reports_the_corrected_per_bead_feed(self):
        arc, notes = self.arc("bead-model-thick", wall_thickness=str(FIVE_EIGHTHS))
        self.assertEqual(arc["beads_across"], 2)
        self.assertAlmostEqual(arc["bead_area_mm2"], 25.4, places=9)
        self.assertAlmostEqual(arc["wfs_ipm"], 170.55, places=2)
        self.assertAlmostEqual(arc["lone_bead_wfs_ipm"], 204.12, places=2)
        self.assertIn("Wire feed is per bead", notes[0])
        self.assertIn("171 ipm, not the 204 ipm", notes[0])
        self.assertAlmostEqual(arc["free_crown_mm"], 1.5 * 25.4 / 9.5)

    def test_pass_table_model_area_is_the_per_bead_share(self):
        from waam_pipeline.passes import pass_table
        from waam_pipeline.robots.ar1440 import V_TOKEN_MM_PER_MIN

        self.arc("bead-model-passes", wall_thickness=str(FIVE_EIGHTHS))
        st = server._state("bead-model-passes")
        rows = pass_table(st["toolpath"], v_unit_mm_per_min=V_TOKEN_MM_PER_MIN,
                          model=server._deposit_model(st))
        self.assertEqual(len(rows), 4)
        for row in rows:

            self.assertAlmostEqual(row["model_area_mm2"], 25.4 * 175 / row["travel_mm_min"], delta=0.01)

    def test_entered_feed_predicts_the_pitch_and_notes_a_mismatch(self):
        arc, notes = self.arc("bead-model-wfs", wall_thickness=str(FIVE_EIGHTHS), wfs="140")
        area = arcparams.deposited_area_mm2(arcparams.WIRE_045_MM, 140 * 0.0254, 175)
        self.assertAlmostEqual(arc["predicted_pitch_mm"], 2 * area / FIVE_EIGHTHS, places=9)
        self.assertLess(arc["predicted_pitch_mm"], 0.9 * 3.2)
        self.assertTrue(any("below the 3.2 mm Bead height" in n for n in notes))
        self.assertAlmostEqual(arc["wfs_ipm"], 170.55, places=2)
        arc, notes = self.arc("bead-model-wfs-ok", wall_thickness=str(FIVE_EIGHTHS), wfs="170")
        self.assertAlmostEqual(arc["predicted_pitch_mm"], 3.2, delta=0.02)
        self.assertFalse(any("Bead height the torch" in n for n in notes))

    def test_entered_feed_is_optional_and_validated(self):
        arc, _ = self.arc("bead-model-blank", wfs="")
        self.assertNotIn("entered_wfs_ipm", arc)
        with self.assertRaisesRegex(ValueError, "Wire feed"):
            self.arc("bead-model-bad", wfs="0")

    def test_stainless_wire_has_no_nominal_crown(self):
        arc, notes = self.arc("bead-model-035", wire=str(arcparams.WIRE_035_MM))
        self.assertNotIn("free_crown_mm", arc)
        self.assertTrue(any("No nominal bead shape" in n for n in notes))


class TestOrnlStepover(unittest.TestCase):
    PARAMS = {"kind": "stl", "units": "mm", "slicer": "ornl", "lh": "2", "bead": "6",
              "travel": "180", "cell": "0", "repair": "0"}

    def slice(self, workspace, **extra):
        from tests.test_ornl import FIXTURE, block
        text = FIXTURE.read_text(encoding="utf-8")
        seen = []

        def engine(tri, options):
            seen.append(options)
            meta = {"engine": "ornl", "version": "2.1.0", "options": ornl.asdict(options),
                    "spacing_mm": options.spacing_mm, "region_length_mm": {},
                    "build_height_mm": 6, "input_height_mm": 6}
            return ornl.parse_output(text, ornl.Options(2, 6, 180)), meta

        with mock.patch.object(ornl, "slice_mesh", side_effect=engine):
            result = server.run_slice(dict(self.PARAMS, **extra), ornl.stl_bytes(block()), workspace=workspace)
        return seen[0], result["state"], server._state(workspace)["request"]["params"]

    def test_default_spacing_stays_75_and_hu_is_only_a_note(self):
        options, state, params = self.slice("ornl-default")
        self.assertEqual((options.stepover_percent, params["stepover"]), (75, "75.0"))
        target, _ = arcparams.stepover_target(6, 2)
        notes = state["arc_warnings"]
        self.assertTrue(any(f"Hu et al. 2019 suggests {target:.0%}" in n for n in notes))
        self.assertTrue(any("ORNL overlaps beads 4.50 mm apart" in n for n in notes))

    def test_entered_spacing_is_used_unchanged(self):
        options, state, params = self.slice("ornl-70", stepover="70")
        self.assertEqual((options.stepover_percent, params["stepover"]), (70, "70.0"))
        self.assertTrue(any("ORNL is spacing at 70%" in n for n in state["arc_warnings"]))

    def test_infill_pitch_is_area_over_spacing(self):
        _, state, _ = self.slice("ornl-wfs", wfs="150")
        area = arcparams.deposited_area_mm2(arcparams.WIRE_045_MM, 150 * 0.0254, 180)
        self.assertAlmostEqual(state["arc"]["predicted_pitch_mm"], area / 4.5)
        self.assertEqual(state["arc"]["predicted_pitch_basis"], "ORNL infill, area / spacing")


if __name__ == "__main__":
    unittest.main()
