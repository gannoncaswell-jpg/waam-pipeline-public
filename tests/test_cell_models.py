import csv
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hardware" / "cell_models"))

import cell_models as cm
import decimate_ar1440 as dec
import gen_measurement_sheet as sheet
from waam_pipeline import cell_lc
from waam_pipeline.slicer import load_stl


def size(triangles):
    lo, hi = cm.bbox(triangles)
    return hi - lo


class TestPrimitives(unittest.TestCase):
    def test_box_bounds_and_winding(self):
        b = cm.box_bounds((-1, -2, -3), (4, 5, 6))
        self.assertEqual(len(b), 12)
        np.testing.assert_allclose(size(b), [5, 7, 9])

        n = np.cross(b[:, 1] - b[:, 0], b[:, 2] - b[:, 0])
        np.testing.assert_allclose(n.sum(axis=0), np.zeros(3), atol=1e-9)

    def test_cylinder_axis_and_extent(self):
        c = cm.cylinder(50.0, 200.0, base=(10.0, 0.0, 5.0), segments=64, axis="x")
        lo, hi = cm.bbox(c)
        self.assertAlmostEqual(lo[0], 10.0, places=6)
        self.assertAlmostEqual(hi[0], 210.0, places=6)
        self.assertAlmostEqual(hi[1] - lo[1], 100.0, places=1)
        self.assertAlmostEqual((lo[2] + hi[2]) / 2, 5.0, places=6)

    def test_prism_caps_have_the_right_area(self):

        outer = cm.rounded_rect(400.0, 200.0, 40.0)
        holes = [cm.circle_loop(-60.0, 0.0, 15.0, 24, clockwise=True),
                 cm.circle_loop(60.0, 0.0, 15.0, 24, clockwise=True)]
        p = cm.prism(outer, holes, z0=0.0, z1=10.0)
        top = p[np.isclose(p[:, :, 2], 10.0).all(axis=1)]
        u, v = top[:, 1, :2] - top[:, 0, :2], top[:, 2, :2] - top[:, 0, :2]
        area = 0.5 * np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]).sum()
        expect = abs(cm._signed_area(outer)) - sum(abs(cm._signed_area(h)) for h in holes)
        self.assertAlmostEqual(area, expect, delta=1.0)

    def test_prism_raises_when_a_hole_is_not_inside_the_outline(self):
        outer = cm.rounded_rect(200.0, 100.0, 10.0)
        outside = cm.circle_loop(400.0, 0.0, 15.0, 16, clockwise=True)
        with self.assertRaises(ValueError):
            cm.prism(outer, [outside], z0=0.0, z1=5.0)


class TestCharlesPlatter(unittest.TestCase):
    def test_bare_plate_matches_the_taped_dimensions(self):
        plate = cm.charles_platter(include_gusset=False, include_fixture=False)
        np.testing.assert_allclose(size(plate), [676.0, 414.3, 6.35], atol=0.05)

    def test_work_face_at_z0_and_cantilever_from_faceplate(self):
        lo, hi = cm.bbox(cm.charles_platter())
        self.assertAlmostEqual(lo[0], 0.0, places=6)
        self.assertAlmostEqual(hi[0], cm.PLATTER_L, places=6)
        self.assertAlmostEqual(hi[2], cm.TRAINING_BLOCK[2], places=6)

    def test_gusset_hangs_below_the_plate(self):
        parts = cm.charles_platter_parts()
        lo, hi = cm.bbox(parts["gusset"])
        self.assertAlmostEqual(hi[2], -cm.PLATTER_T, places=6)
        self.assertAlmostEqual(lo[2], -cm.PLATTER_T - cm.GUSSET_DIA / 2, delta=0.6)
        self.assertAlmostEqual(hi[1] - lo[1], cm.GUSSET_DIA, delta=0.6)
        self.assertAlmostEqual(hi[0] - lo[0], cm.GUSSET_THK, places=6)

    def test_probe_datum_holes_are_cut(self):

        plate = cm.charles_platter(include_gusset=False, include_fixture=False)
        wall = plate[np.isclose(np.abs(plate[:, :, 1]).max(axis=1), 101.6 + 19.05 / 2,
                                atol=0.6)]
        self.assertGreater(len(wall), 0)
        solid = cm.charles_platter(include_gusset=False, include_fixture=False,
                                   include_holes=False)
        self.assertGreater(len(plate), len(solid))
        np.testing.assert_allclose(size(plate), size(solid), atol=1e-9)

    def test_mass_matches_the_capture_log(self):
        self.assertAlmostEqual(cm.platter_mass_kg(), 14.0, delta=0.5)

    def test_work_face_is_one_flat_plane(self):


        plate = cm.charles_platter(include_gusset=False, include_fixture=False)
        n = np.cross(plate[:, 1] - plate[:, 0], plate[:, 2] - plate[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True)
        up = n[:, 2] > np.cos(np.radians(5.0))
        self.assertGreater(int(up.sum()), 0)
        self.assertLess(np.abs(plate[up][:, :, 2]).max(), 1e-6)

        down = n[:, 2] < -np.cos(np.radians(5.0))
        self.assertGreater(int(down.sum()), 0)
        self.assertLess(np.abs(plate[down][:, :, 2] + cm.PLATTER_T).max(), 1e-6)

    def test_display_triangles_are_bounded_for_the_painter_sort(self):


        for name, tris in cm.charles_platter_parts().items():
            edges = np.stack([
                np.linalg.norm(tris[:, 1] - tris[:, 0], axis=1),
                np.linalg.norm(tris[:, 2] - tris[:, 1], axis=1),
                np.linalg.norm(tris[:, 0] - tris[:, 2], axis=1)])
            self.assertLessEqual(edges.max(), 110.0, name)


class TestMH185Headstock(unittest.TestCase):
    def test_faceplate_is_on_the_axis_at_the_stated_height(self):
        parts = cm.mh185_headstock_parts()
        lo, hi = cm.bbox(parts["faceplate"])
        self.assertAlmostEqual((lo[1] + hi[1]) / 2, 0.0, places=6)
        self.assertAlmostEqual((lo[2] + hi[2]) / 2, cm.AXIS_HEIGHT_ABOVE_BASE, places=6)
        self.assertAlmostEqual(hi[1] - lo[1], cm.FACEPLATE_DIA, delta=0.8)
        self.assertAlmostEqual(hi[2] - lo[2], cm.FACEPLATE_DIA, delta=0.8)
        self.assertAlmostEqual(hi[0], 0.0, places=6)
        self.assertAlmostEqual(lo[0], -cm.FACEPLATE_THK, places=6)

    def test_drive_assembly_uses_the_manual_273_outline(self):
        lo, _ = cm.bbox(cm.mh185_headstock(include_stand=False))
        self.assertAlmostEqual(lo[0], -cm.HEADSTOCK_LENGTH, places=6)

    def test_foot_and_stand_reach_the_cell_base(self):
        parts = cm.mh185_headstock_parts()
        foot = cm.bbox(parts["casting"])[0][2]
        self.assertAlmostEqual(foot, cm.AXIS_HEIGHT_ABOVE_BASE - cm.AXIS_ABOVE_FOOT,
                               places=6)
        self.assertLess(cm.bbox(parts["stand_base_plate"])[0][2], cm.STAND_DECK_Z)


        self.assertLessEqual(cm.bbox(parts["stand_top_plate"])[1][2],
                             cm.AXIS_HEIGHT_ABOVE_BASE - cm.AXIS_ABOVE_FOOT + 1e-6)

    def test_axis_height_is_parametric(self):


        a = cm.mh185_headstock(axis_height=cm.AXIS_HEIGHT_ABOVE_BASE)
        b = cm.mh185_headstock(axis_height=cm.AXIS_HEIGHT_ABOVE_BASE + 100.0)
        np.testing.assert_allclose(cm.bbox(b)[1][2] - cm.bbox(a)[1][2], 100.0, atol=1e-6)

    def test_faceplate_interface_matches_section_bb(self):
        holes = cm.faceplate_hole_pattern()
        self.assertEqual(len(holes["m12"]), 4)
        self.assertEqual(len(holes["dowel"]), 2)
        spread = max(x for x, _ in holes["m12"]) - min(x for x, _ in holes["m12"])
        self.assertAlmostEqual(spread, cm.FACEPLATE_BOLT_SQUARE)
        dowel = max(x for x, _ in holes["dowel"]) - min(x for x, _ in holes["dowel"])
        self.assertAlmostEqual(dowel, cm.FACEPLATE_DOWEL_SPACING)
        for x, y in holes["m12"] + holes["dowel"]:
            self.assertLess(np.hypot(x, y), cm.FACEPLATE_DIA / 2)


class TestStationaryFurniture(unittest.TestCase):
    def test_reamer_head_plate_is_independent_of_the_floor_foot(self):


        parts = cm.reamer_station_parts(base=(340., 300., 12.))
        np.testing.assert_allclose(size(parts["base_plate"]), [340., 300., 12.])
        np.testing.assert_allclose(size(parts["head_mount_plate"])[:2], [243.2, 178.2])
        self.assertAlmostEqual(cm.bbox(parts["head_mount_plate"])[0][2], 914.4)
        self.assertAlmostEqual(cm.bbox(parts["head"])[0][2],
                               cm.bbox(parts["head_mount_plate"])[1][2])
        self.assertAlmostEqual(cm.bbox(parts["head"])[1][2] - 914.4, 315.0)

    def test_reamer_station_stands_36_inches(self):
        parts = cm.reamer_station_parts()
        self.assertAlmostEqual(cm.bbox(parts["post"])[1][2], 914.4, places=3)
        lo, hi = cm.bbox(cm.reamer_station())
        self.assertAlmostEqual(lo[2], 0.0, places=6)
        self.assertAlmostEqual(hi[2], 914.4 + cm.REAMER_HEAD[2], places=3)

    def test_robot_pedestal_top_is_the_robot_base_plane(self):
        lo, hi = cm.bbox(cm.robot_pedestal())
        self.assertAlmostEqual(lo[2], 0.0, places=6)
        self.assertAlmostEqual(hi[2], cm.PEDESTAL_H, places=6)
        col = cm.bbox(cm.robot_pedestal_parts()["column"])
        np.testing.assert_allclose((col[1] - col[0])[:2],
                                   [cm.PEDESTAL_SECTION] * 2, atol=1e-6)

    def test_cell_frame_footprint_and_height(self):
        lo, hi = cm.bbox(cm.cell_frame())
        np.testing.assert_allclose(hi - lo,
                                   [cm.CELL_L, cm.CELL_W,
                                    cm.CELL_HEIGHT],
                                   atol=1e-6)
        self.assertAlmostEqual(lo[2], 0.0, places=6)

    def test_cell_frame_openings_stay_within_the_manual_limits(self):
        parts = cm.cell_frame_parts()
        posts = {k: cm.bbox(v) for k, v in parts.items() if k.startswith("post_")}
        y_front = min(lo[1] for lo, _ in posts.values())
        front = sorted((lo[0] + hi[0]) / 2 for lo, hi in posts.values()
                       if np.isclose(lo[1], y_front))
        self.assertGreaterEqual(len(front), 3)
        for a, b in zip(front, front[1:]):
            self.assertLessEqual(b - a - cm.FRAME_MEMBER, cm.OPENING_MAX_WIDTH + 1e-6)
        sill = cm.bbox(parts["rail_front_1"])[1][2]
        head = cm.bbox(parts["rail_front_2"])[0][2]
        self.assertAlmostEqual(head - sill, cm.OPENING_HEIGHT, places=6)


class TestVendorSpecComponents(unittest.TestCase):


    def test_autodrive_preserves_the_supplied_cad_assembly(self):


        metadata = cm.k3560_1_metadata()
        self.assertEqual(metadata["source_sha256"],
                         "7a37c771341028ed812f9d1247203e8a6e17bc86b117dec15fbb987f11d93f99")
        self.assertEqual(metadata["solid_count"], 21)
        self.assertEqual(len(metadata["groups"]), 43)
        self.assertTrue(all(s["brep_valid"] for s in metadata["solids"]))
        with np.load(cm._K3560_STEP_PARTS, allow_pickle=False) as source:
            self.assertEqual(set(source.files), {g["array_key"] for g in metadata["groups"]})
            native = np.concatenate([source[g["array_key"]] for g in metadata["groups"]])
        np.testing.assert_allclose(cm.bbox(native),
                                   [[-144.3992, -123.8632, -18.669],
                                    [113.5068, 127.470914, 210.28576]], atol=0.05)

    def test_autodrive_uses_the_exact_k3572_bracket_envelope(self):
        bracket = cm.autodrive_4r100_parts()["k3572_bracket"]
        np.testing.assert_allclose(
            size(bracket), cm.K3572_BRACKET_ENVELOPE, atol=1.5
        )
        lo, hi = cm.bbox(bracket)
        self.assertAlmostEqual(lo[2], -cm.K3572_BRACKET_DROP, delta=0.01)
        self.assertAlmostEqual(hi[2], cm.K3572_TOP_SHEET_THK, delta=0.01)

    def test_autodrive_sits_on_the_bracket(self):
        lo, hi = cm.bbox(cm.autodrive_4r100())
        self.assertAlmostEqual(lo[2], -cm.K3572_BRACKET_DROP, delta=0.01)
        self.assertAlmostEqual(hi[2], 210.28576 + cm.K3572_TOP_SHEET_THK, delta=.01)

        native_base = np.array([[0., 0., 0.], [100., 0., 0.], [0., 100., 0.]])
        placed_base = cm.k3560_points_in_feeder(native_base)
        np.testing.assert_allclose(placed_base[:, 2], cm.K3572_TOP_SHEET_THK, atol=1e-8)

    def test_wire_spool_is_the_s300_envelope_with_a_through_bore(self):
        spool = cm.wire_spool()
        lo, hi = cm.bbox(spool)
        self.assertAlmostEqual(hi[0] - lo[0], cm.SPOOL_WIDTH, places=6)
        self.assertAlmostEqual(lo[0], 0.0, places=6)

        radial = np.hypot(spool[:, :, 1], spool[:, :, 2]).max()
        self.assertAlmostEqual(radial, cm.SPOOL_DIA / 2, places=6)

        for name, part in cm.wire_spool_parts().items():
            radial = np.hypot(part[:, :, 1], part[:, :, 2]).min()
            self.assertGreater(radial, cm.SPOOL_BORE / 2 - 1.5, name)
        self.assertLess(len(cm.wire_spool()), 500)

    def test_wire_spool_can_be_emptied(self):
        full, empty = cm.wire_spool_parts(), cm.wire_spool_parts(full=False)
        self.assertIn("wire_pack", full)
        self.assertNotIn("wire_pack", empty)

    def test_fume_hood_cone_tapers_from_the_mouth_to_the_duct(self):
        parts = cm.fume_hood_parts()
        lo, hi = cm.bbox(parts["cone"])
        np.testing.assert_allclose((hi - lo)[:2], [cm.HOOD_MOUTH_DIA] * 2, atol=8.0)
        duct = cm.bbox(parts["duct"])
        np.testing.assert_allclose((duct[1] - duct[0])[:2],
                                   [cm.HOOD_DUCT_DIA] * 2, atol=4.0)
        whole = cm.bbox(cm.fume_hood())
        self.assertAlmostEqual(whole[0][2], 0.0, places=6)
        self.assertAlmostEqual(whole[1][2],
                               cm.HOOD_LIP + cm.HOOD_CONE_H + cm.HOOD_DUCT_LEN,
                               places=6)

    def test_frustum_refuses_a_degenerate_tip(self):
        with self.assertRaises(ValueError):
            cm.frustum(100.0, 0.0, 50.0)

    def test_fume_hood_has_an_open_airway_through_lip_cone_and_duct(self):


        for name, mesh in cm.fume_hood_parts().items():
            for triangle in mesh:
                xy = triangle[:, :2]
                edge = np.roll(xy, -1, axis=0) - xy
                cross = edge[:, 0] * -xy[:, 1] - edge[:, 1] * -xy[:, 0]
                area = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])[2]
                if abs(area) > 1e-8:
                    contains_axis = np.all(cross >= -1e-8) or np.all(cross <= 1e-8)
                    self.assertFalse(contains_axis, f"{name} blocks the intake")

    def test_yrc1000_cabinet_matches_the_datasheet_598_499_427(self):


        body = cm.yrc1000_cabinet_parts()["body"]
        np.testing.assert_allclose(size(body), list(cm.YRC1000_BODY), atol=1e-6)
        lo, hi = cm.bbox(cm.yrc1000_cabinet())
        self.assertAlmostEqual(lo[2], 0.0, delta=0.5)
        self.assertAlmostEqual(hi[2], 551.0, delta=2.0)
        self.assertAlmostEqual(hi[0], 269.7, delta=2.0)
        self.assertAlmostEqual(lo[0], -328.5, delta=2.0)
        self.assertGreaterEqual(hi[1] - lo[1], 598.0)

    def test_lincoln_r450_matches_the_datasheet_571_355_630(self):

        lo, hi = cm.bbox(cm.lincoln_r450())
        np.testing.assert_allclose(hi - lo,
                                   [cm.R450_CASE[0], cm.R450_CASE[1],
                                    cm.R450_SKID + cm.R450_CASE[2]], atol=1e-6)
        self.assertAlmostEqual(lo[2], 0.0, places=6)

    def test_components_stay_in_the_documented_display_budgets(self):


        budgets = {"yrc1000_cabinet": 1500, "autodrive_4r100": 200000,
                   "cell_frame_v2": 650}
        for name in ("autodrive_4r100", "wire_spool", "fume_hood",
                     "yrc1000_cabinet", "lincoln_r450", "cell_frame_v2"):
            n = len(cm.GENERATORS[name]())
            self.assertGreaterEqual(n, 30, name)
            self.assertLessEqual(n, budgets.get(name, 500), name)


class TestCellFrameV2(unittest.TestCase):
    def test_footprint_and_height_match_the_print(self):
        lo, hi = cm.bbox(cm.cell_frame_v2())
        np.testing.assert_allclose(hi - lo,
                                   [cm.CELL_L, cm.CELL_W, cm.CELL_HEIGHT],
                                   atol=1e-6)
        self.assertAlmostEqual(lo[2], 0.0, places=6)
        self.assertAlmostEqual(cm.CELL_HEIGHT, 2277.0, places=6)

    def test_door_opening_is_the_manual_1779_by_1161(self):

        parts = cm.cell_frame_v2_parts()
        posts = {k: cm.bbox(v) for k, v in parts.items() if k.startswith("post_")}
        y_front = min(lo[1] for lo, _ in posts.values())
        inner = sorted(x for lo, hi in posts.values()
                       if np.isclose(lo[1], y_front) for x in (lo[0], hi[0]))

        gaps = [b - a for a, b in zip(inner[1:], inner[2:])]
        self.assertAlmostEqual(max(gaps), cm.OPENING_MAX_WIDTH, places=6)
        sill = cm.bbox(parts["rail_front_1"])[1][2]
        head = cm.bbox(parts["rail_front_2"])[0][2]
        self.assertAlmostEqual(sill, cm.OPENING_SILL, places=6)
        self.assertAlmostEqual(head - sill, cm.OPENING_HEIGHT, places=6)

    def test_panels_are_thin_slabs_and_the_door_bay_is_open_above_the_sill(self):
        parts = cm.cell_frame_v2_parts()
        panels = {k: v for k, v in parts.items() if k.startswith("panel_")}
        self.assertTrue(panels)
        for name, part in panels.items():
            self.assertAlmostEqual(min(size(part)), cm.PANEL_THK, places=6, msg=name)

        upper = [cm.bbox(v) for k, v in panels.items() if "front_upper" in k]
        for lo, hi in upper:
            self.assertGreater(abs((lo[0] + hi[0]) / 2), cm.OPENING_MAX_WIDTH / 2)

        lower = [cm.bbox(v) for k, v in panels.items() if "front_lower" in k]
        self.assertTrue(any(abs((lo[0] + hi[0]) / 2) < cm.OPENING_MAX_WIDTH / 2
                            for lo, hi in lower))

    def test_no_bay_exceeds_the_manual_maximum(self):
        parts = cm.cell_frame_v2_parts()
        posts = [cm.bbox(v) for k, v in parts.items() if k.startswith("post_")]
        for axis, edge in ((0, 1), (1, 0)):
            for face in (min, max):
                v = face(b[0][edge] for b in posts)
                line = sorted((lo[axis] + hi[axis]) / 2 for lo, hi in posts
                              if np.isclose(lo[edge], v))
                for a, b in zip(line, line[1:]):
                    self.assertLessEqual(b - a - cm.FRAME_MEMBER,
                                         cm.OPENING_MAX_WIDTH + 1e-6)


class TestTheTrainingBlockIsTheSameBlockEverywhere(unittest.TestCase):


    def setUp(self):
        parts = cm.charles_platter_parts()
        self.block = parts["training_block"]
        self.calibration = json.loads(
            (ROOT / "docs" / "cell-calibration.json").read_text(encoding="utf-8")
        )["training_block"]

    def test_the_modelled_block_has_the_calibrated_dimensions(self):
        lo, hi = cm.bbox(self.block)
        want = [self.calibration["length_mm"], self.calibration["width_mm"],
                self.calibration["height_mm"]]
        np.testing.assert_allclose(
            hi - lo, want, atol=1e-6,
            err_msg="hardware/cell_models/cell_models.py and "
                    "docs/cell-calibration.json disagree about the ManPro "
                    "training block's size. Update BOTH: the JSON is what "
                    "cell_lc.lc_scene() gates emission against, the module is "
                    "what the drawing, the STL export and the studio shadow "
                    "draw.")

    def test_the_modelled_block_sits_where_the_calibration_puts_it(self):


        lo, hi = cm.bbox(self.block)
        centre_x = (lo[0] + hi[0]) / 2.0
        self.assertAlmostEqual(
            centre_x, self.calibration["along_plate_mm"], places=6,
            msg="hardware/cell_models/cell_models.py places the ManPro "
                "training block at x %.1f mm from the faceplate plane; "
                "docs/cell-calibration.json says along_plate_mm %.1f. Update "
                "BOTH -- the clearance gate reads the JSON and every drawn "
                "artefact reads the module." % (centre_x,
                                                self.calibration["along_plate_mm"]))

    def test_cell_lc_loaded_the_same_block_the_file_holds(self):


        self.assertEqual(
            list(cell_lc.TRAINING_BLOCK),
            [self.calibration["length_mm"], self.calibration["width_mm"],
             self.calibration["height_mm"]])
        self.assertAlmostEqual(cell_lc.TRAINING_BLOCK_ALONG_PLATE_MM,
                               self.calibration["along_plate_mm"], places=6)


class TestMeasurementSheet(unittest.TestCase):
    BASES = {"MANUAL", "MODEL", "SCALED", "LOG", "PHOTO", "WEB", "ASSUMED",

             "PENDANT"}

    def test_rows_are_present_unique_and_well_formed(self):
        self.assertGreater(len(cm.MEASUREMENTS), 20)
        ids = [m["id"] for m in cm.MEASUREMENTS]
        self.assertEqual(len(ids), len(set(ids)))
        for m in cm.MEASUREMENTS:
            self.assertIn(m["basis"], self.BASES, m["id"])
            self.assertIn(m["priority"], ("H", "M", "L"), m["id"])
            self.assertTrue(str(m["parameter"]).strip(), m["id"])

            how = str(m["how_to_measure"])
            self.assertGreater(len(how), 40, m["id"])
            self.assertTrue(how.rstrip().endswith("."), m["id"])

    def test_every_component_family_with_assumptions_is_covered(self):
        components = {m["component"] for m in cm.MEASUREMENTS}


        expect = set(cm.GENERATORS) - {"cell_frame"} | {"cell_lc"}
        self.assertEqual(expect - components, set())
        self.assertEqual(
            components - set(cm.GENERATORS) - {"cell_lc", "controller"}, set())

    def test_the_high_priority_open_questions_are_all_on_the_sheet(self):

        by_id = {m["id"]: m for m in cm.MEASUREMENTS}
        for rid in ("platter-face-row", "mh185-stand-splice-z",
                    "cell-frame-fore-aft", "cell-frame-lateral",
                    "reamer-stand-position",
                    "spool-mount-offset", "station-handedness"):
            self.assertIn(rid, by_id)
            self.assertEqual(by_id[rid]["priority"], "H", rid)
        counts = cm.measurements_by_priority()
        self.assertEqual(sum(counts.values()), len(cm.MEASUREMENTS))

    def test_csv_round_trips_every_row_with_blank_answer_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = sheet.write_sheet(Path(tmp) / "measurement-sheet.csv")
            with out.open(newline="", encoding="utf-8") as fh:
                got = list(csv.DictReader(fh))
        self.assertEqual(len(got), len(cm.MEASUREMENTS))
        self.assertEqual(list(got[0]), sheet.COLUMNS)
        by_id = {m["id"]: m for m in cm.MEASUREMENTS}
        for row in got:
            src = by_id[row["id"]]
            self.assertEqual(row["component"], src["component"])
            self.assertEqual(row["how_to_measure"], src["how_to_measure"])
            for blank in sheet.BLANKS:
                self.assertEqual(row[blank], "")

        order = [sheet.PRIORITY_ORDER[r["priority"]] for r in got]
        self.assertEqual(order, sorted(order))

    def test_generator_rejects_duplicate_ids_and_bad_priorities(self):
        dupe = [dict(cm.MEASUREMENTS[0]), dict(cm.MEASUREMENTS[0])]
        with self.assertRaises(ValueError):
            sheet.rows(dupe)
        bad = dict(cm.MEASUREMENTS[0])
        bad["priority"] = "URGENT"
        with self.assertRaises(ValueError):
            sheet.rows([bad])
        incomplete = {k: v for k, v in cm.MEASUREMENTS[0].items() if k != "basis"}
        with self.assertRaises(ValueError):
            sheet.rows([incomplete])

    def test_every_row_id_the_readme_cites_exists_on_the_sheet(self):


        readme = (ROOT / "hardware" / "cell_models" / "README.md").read_text(
            encoding="utf-8")
        with sheet.OUT.open(newline="", encoding="utf-8") as fh:
            ids = {r["id"] for r in csv.DictReader(fh)}
        cited = set(re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", readme))


        self.assertGreaterEqual(len(cited), 10, "the id net caught nothing -- "
                                "did the README stop citing rows in backticks?")
        missing = sorted(cited - ids)
        self.assertEqual(
            missing, [],
            f"hardware/cell_models/README.md cites measurement-sheet row "
            f"id(s) {missing} that {sheet.OUT.name} does not have")

    def test_the_committed_sheet_is_up_to_date(self):

        self.assertTrue(sheet.OUT.exists(), f"{sheet.OUT} missing — run gen_measurement_sheet.py")
        with sheet.OUT.open(newline="", encoding="utf-8") as fh:
            got = list(csv.DictReader(fh))
        self.assertEqual([r["id"] for r in got],
                         [r["id"] for r in sheet.rows(cm.MEASUREMENTS)])


class TestExport(unittest.TestCase):
    def test_write_all_round_trips_through_stl(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = cm.write_all(tmp)
            self.assertEqual(set(written), set(cm.GENERATORS))
            for name, path in written.items():
                self.assertTrue(path.exists())
                back = load_stl(path)
                ref = cm.GENERATORS[name]()
                self.assertEqual(len(back), len(ref))
                np.testing.assert_allclose(size(back), size(ref), atol=0.05)

    def test_parts_have_balanced_area_vectors_and_positive_volume(self):

        parts = {}
        for prefix, fn in (("platter", cm.charles_platter_parts),
                           ("mh185", cm.mh185_headstock_parts),
                           ("reamer", cm.reamer_station_parts),
                           ("pedestal", cm.robot_pedestal_parts),
                           ("frame", cm.cell_frame_parts),
                           ("frame2", cm.cell_frame_v2_parts),
                           ("spool", cm.wire_spool_parts),
                           ("hood", cm.fume_hood_parts),
                           ("yrc1000", cm.yrc1000_cabinet_parts),
                           ("r450", cm.lincoln_r450_parts)):
            parts.update({f"{prefix}.{k}": v for k, v in fn().items()})
        feeder = cm.autodrive_4r100_parts()
        parts["feeder.bracket"] = feeder["k3572_bracket"]
        groups = cm.k3560_1_metadata()["groups"]
        for solid in {g["solid"] for g in groups}:
            parts[f"feeder.solid_{solid}"] = np.concatenate([
                feeder[g["array_key"]] for g in groups if g["solid"] == solid])
        for name, t in parts.items():
            n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            scale = np.abs(n).sum()
            self.assertLess(np.abs(n.sum(axis=0)).max(), 1e-6 * scale, name)
            self.assertGreater(np.einsum("ij,ij->i", t[:, 0], n).sum(), 0.0, name)

    def test_every_generator_makes_clean_triangles(self):
        for name, gen in cm.GENERATORS.items():
            tris = gen()
            self.assertEqual(tris.shape[1:], (3, 3), name)
            self.assertTrue(np.isfinite(tris).all(), name)
            area = 0.5 * np.linalg.norm(
                np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
            self.assertGreater(area.min(), 1e-6, name)


class TestDecimatedMeshes(unittest.TestCase):


    def setUp(self):
        self.sources = sorted(dec.SRC.glob("*.stl"))
        self.assertTrue(self.sources)

    def test_every_link_has_a_decimated_mesh_within_budget(self):
        for path in self.sources:
            out = dec.OUT / f"{path.stem}_dec.stl"
            self.assertTrue(out.exists(), f"{out.name} missing — run decimate_ar1440.py")
            small = load_stl(out)
            self.assertLessEqual(len(small), dec.BUDGET, path.name)
            self.assertLess(len(small), len(load_stl(path)), path.name)

    def test_decimated_bboxes_track_the_collision_meshes(self):
        for path in self.sources:
            src = load_stl(path)
            small = load_stl(dec.OUT / f"{path.stem}_dec.stl")
            self.assertLessEqual(dec.bbox_error_mm(src, small), dec.BBOX_TOL_MM, path.name)

    def test_cluster_decimate_is_deterministic_and_shrinks(self):
        src = load_stl(self.sources[0])
        a = dec.cluster_decimate(src, 12.0)
        b = dec.cluster_decimate(src, 12.0)
        np.testing.assert_array_equal(a, b)
        self.assertLess(len(dec.cluster_decimate(src, 24.0)), len(a))


if __name__ == "__main__":
    unittest.main()
