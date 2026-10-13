import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.gcode import parse
from waam_pipeline.slicer import wall

SUBSTRATE = 6.35


def platter_top_in_robot_frame(origin) -> float:

    scene = cell_lc.lc_scene(part_origin_robot=origin)
    box = next(b for b in scene.boxes if b[0] == "charles_platter")
    return float(box[2][2]) + float(origin[2])


class TestSnapSeatsTheFirstWeld(unittest.TestCase):
    def test_lowest_weld_tcp_lands_on_the_work_surface(self):
        tp = wall(150.0, 6, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=SUBSTRATE)
        lowest_tcp_robot_z = origin[2] + min(p[2] for p in tp.deposit_points())
        self.assertAlmostEqual(
            lowest_tcp_robot_z,
            cell_lc.platter_work_surface_z(SUBSTRATE),
            places=9,
        )

    def test_first_layer_runs_one_layer_lower_than_bead_bottom_seating_did(self):


        layer_h = 2.0
        tp = wall(150.0, 6, layer_h)
        snapped = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=SUBSTRATE)


        self.assertAlmostEqual(float(snapped[2]) + layer_h,
                               cell_lc.platter_work_surface_z(SUBSTRATE), places=9)

    def test_works_for_a_toolpath_whose_first_layer_is_not_at_layer_height(self):

        tp = parse("G90\nG0 X0 Y0 Z7.5\nG1 X10 E1\nG0 Z12\nG1 X0 E2\n")
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=SUBSTRATE)
        lowest = origin[2] + min(p[2] for p in tp.deposit_points())
        self.assertAlmostEqual(lowest, cell_lc.platter_work_surface_z(SUBSTRATE), places=9)

    def test_gap_lifts_the_first_layer(self):
        tp = wall(150.0, 2, 2.0)
        flush = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=SUBSTRATE)
        lifted = cell_lc.snap_toolpath_to_platter(
            tp, 1014.0, -180.0, substrate_mm=SUBSTRATE, gap_mm=1.5
        )
        lowest = min(p[2] for p in tp.deposit_points())
        self.assertAlmostEqual(flush[2] + lowest, cell_lc.platter_work_surface_z(SUBSTRATE), places=9)
        self.assertAlmostEqual(lifted[2] + lowest, cell_lc.platter_work_surface_z(SUBSTRATE) + 1.5, places=9)

    def test_xy_passes_straight_through(self):
        tp = wall(150.0, 2, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0)
        self.assertAlmostEqual(float(origin[0]), 1014.0)
        self.assertAlmostEqual(float(origin[1]), -180.0)

    def test_toolpath_with_no_deposit_moves_is_refused(self):
        tp = parse("G90\nG0 X0 Y0 Z5\nG0 X10\n")
        with self.assertRaises(ValueError):
            cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0)

    def test_negative_substrate_is_refused(self):
        with self.assertRaises(ValueError):
            cell_lc.platter_work_surface_z(-1.0)


class TestSeatingHeightGuards(unittest.TestCase):
    def seats(self):
        tp = wall(30.0, 2, 2.0)
        surface = mock.Mock()
        return ((cell_lc.snap_toolpath_to_platter, (tp, 1014.0, -180.0)),
                (cell_lc.snap_toolpath_to_surface, (tp, 1014.0, -180.0, surface)),
                (cell_lc.snap_toolpath_to_platter_pose, (tp, 1014.0, -180.0)),
                (cell_lc.seat_job, (tp, 1014.0, -180.0)))

    def test_bead_height_is_not_a_seating_input(self):


        for seat, args in self.seats():
            with self.subTest(seat=seat.__name__), self.assertRaisesRegex(TypeError, "bead_height_mm"):
                seat(*args, bead_height_mm=2.0)

    def test_negative_and_nonfinite_gaps_are_refused(self):
        for seat, args in self.seats():
            for gap in (-1.0, np.nan, np.inf, -np.inf):
                with self.subTest(seat=seat.__name__, gap=gap), \
                     mock.patch.object(cell_lc, "load_fixture", return_value=mock.Mock()), \
                     self.assertRaisesRegex(ValueError, "gap_mm"):
                    seat(*args, gap_mm=gap)


class TestCalibrationOffsetMovesEverythingTogether(unittest.TestCase):


    def test_scene_and_snap_derive_from_the_same_face_height(self):
        tp = wall(150.0, 6, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=SUBSTRATE)
        self.assertAlmostEqual(
            platter_top_in_robot_frame(origin), cell_lc.PLATTER_FACE_Z, places=6
        )

    def test_an_offset_shifts_the_obstacle_and_the_bead_by_the_same_amount(self):
        tp = wall(150.0, 6, 2.0)
        shift = 7.5

        def state(face_z):
            with mock.patch.object(cell_lc, "PLATTER_FACE_Z", face_z):
                origin = cell_lc.snap_toolpath_to_platter(
                    tp, 1014.0, -180.0, substrate_mm=SUBSTRATE
                )
                first_tcp = origin[2] + min(p[2] for p in tp.deposit_points())
                self.assertAlmostEqual(first_tcp, cell_lc.platter_work_surface_z(SUBSTRATE), places=9)
                return platter_top_in_robot_frame(origin), float(first_tcp)

        plate0, bead0 = state(cell_lc.PLATTER_FACE_Z_MODEL)
        plate1, bead1 = state(cell_lc.PLATTER_FACE_Z_MODEL + shift)

        self.assertAlmostEqual(plate0, cell_lc.PLATTER_FACE_Z_MODEL, places=6)
        self.assertAlmostEqual(plate1, cell_lc.PLATTER_FACE_Z_MODEL + shift, places=6)
        self.assertAlmostEqual(bead0, cell_lc.PLATTER_FACE_Z_MODEL + SUBSTRATE, places=6)
        self.assertAlmostEqual(bead1, cell_lc.PLATTER_FACE_Z_MODEL + shift + SUBSTRATE, places=6)

    def test_effective_face_is_model_plus_offset(self):
        self.assertAlmostEqual(
            cell_lc.PLATTER_FACE_Z,
            cell_lc.PLATTER_FACE_Z_MODEL + cell_lc.PLATTER_FACE_OFFSET_MM,
            places=9,
        )

    def test_an_absurd_offset_is_refused(self):

        bad = {"platter_face_offset_mm": 500.0}
        with mock.patch.object(cell_lc.Path, "exists", lambda self: True), \
             mock.patch.object(cell_lc.Path, "read_text", lambda self, encoding=None: __import__("json").dumps(bad)):
            with self.assertRaises(ValueError):
                cell_lc._load_calibration()

    def test_offset_still_matches_its_own_touch_off_evidence(self):


        touch = cell_lc.CALIBRATION.get("touch_off")
        if not touch:
            self.skipTest("no touch-off recorded yet")
        derived = cell_lc.offset_from_touch_off(
            touch["pulses_SLURBT"],
            substrate_mm=touch.get("substrate_mm", 0.0),
            touch_stickout_mm=touch.get("touch_stickout_mm"),
            weld_stickout_mm=touch.get("weld_stickout_mm")
            if touch.get("weld_stickout_mm") is not None
            else touch.get("touch_stickout_mm"),
        )
        self.assertAlmostEqual(
            derived, cell_lc.PLATTER_FACE_OFFSET_MM, places=1,
            msg=f"offset in use is {cell_lc.PLATTER_FACE_OFFSET_MM} mm but its "
                f"recorded touch-off implies {derived:.3f} mm",
        )

    def test_touch_off_decoder_round_trips_a_known_posture(self):

        import numpy as np

        from waam_pipeline.robots import ar1440
        from waam_pipeline.robots.ar1440 import PULSES_PER_DEG

        pulses = [-1691, 33350, -49764, 6597, 14034, -2639]
        q = [p / r for p, r in zip(pulses, PULSES_PER_DEG)]
        f = ar1440.fk(q)["flange"]
        expected = f[:3, 3] + f[:3, :3] @ cell_lc.TOOL_TCP
        np.testing.assert_allclose(cell_lc.tcp_from_pulses(pulses), expected, atol=1e-9)

    def test_calibration_summary_reports_the_offset_actually_used(self):
        lines = " ".join(cell_lc.calibration_summary())
        self.assertIn(f"{cell_lc.PLATTER_FACE_Z:.1f}", lines)
        self.assertIn(f"{cell_lc.PLATTER_FACE_OFFSET_MM:+.1f}", lines)


if __name__ == "__main__":
    unittest.main()


class TestTouchOffWorkSurface(unittest.TestCase):


    @staticmethod
    def touches(points_xyz):

        from waam_pipeline.robots import ar1440
        from waam_pipeline.robots.ar1440 import PULSES_PER_DEG

        solve = cell_lc.pulse_solver(np.array([0.0, 0.0, 0.0]))
        return [
            [int(round(a * r)) for a, r in zip(solve(p), PULSES_PER_DEG)]
            for p in points_xyz
        ]

    SPREAD = [(1050.0, -250.0), (1180.0, -120.0), (1060.0, -60.0), (1160.0, -260.0)]

    def flat_at(self, z):
        return self.touches([(x, y, z) for x, y in self.SPREAD])

    def test_recovers_a_flat_surface_height(self):
        s = cell_lc.fit_work_surface("flat", self.flat_at(140.5))
        self.assertAlmostEqual(s.z_at(1112.5, -172.5), 140.5, delta=0.02)
        self.assertLess(s.tilt_deg, 0.05)
        self.assertLess(s.residual_mm, 0.02)

    def test_recovers_a_known_tilt(self):

        pts = [(x, y, 140.5 + 0.004 * (x - 1100.0)) for x, y in self.SPREAD]
        s = cell_lc.fit_work_surface("tilted", self.touches(pts))
        self.assertAlmostEqual(s.tilt_deg, np.degrees(np.arctan(0.004)), delta=0.02)

        self.assertAlmostEqual(s.z_at(1200.0, -150.0), 140.5 + 0.004 * 100.0, delta=0.05)

    def test_two_points_are_refused(self):
        with self.assertRaises(ValueError) as ctx:
            cell_lc.fit_work_surface("two", self.flat_at(140.5)[:2])
        self.assertIn("at least 3", str(ctx.exception))

    def test_collinear_touches_are_refused(self):

        line = [(1050.0 + 60 * k, -250.0 + 60 * k, 140.5) for k in range(3)]
        with self.assertRaises(ValueError) as ctx:
            cell_lc.fit_work_surface("line", self.touches(line))
        self.assertIn("collinear", str(ctx.exception))

    def test_absurd_tilt_is_refused(self):
        pts = [(1050.0, -250.0, 120.0), (1180.0, -120.0, 160.0), (1060.0, -60.0, 150.0)]
        with self.assertRaises(ValueError) as ctx:
            cell_lc.fit_work_surface("steep", self.touches(pts))
        self.assertIn("tilt", str(ctx.exception))

    def test_substrate_at_fit_time_recovers_the_fixture_under_it(self):

        s = cell_lc.fit_work_surface("under", self.flat_at(146.85), substrate_mm=6.35)
        self.assertAlmostEqual(s.z_at(1112.5, -172.5), 140.5, delta=0.02)

    def test_fit_removes_substrate_and_use_adds_it(self):


        s = cell_lc.fit_work_surface("both", self.flat_at(140.5))
        tp = wall(150.0, 6, 2.0)
        bare = cell_lc.snap_toolpath_to_surface(tp, 1100.0, -180.0, s)
        with_plate = cell_lc.snap_toolpath_to_surface(
            tp, 1100.0, -180.0, s, substrate_mm=6.35
        )
        lowest = min(p[2] for p in tp.deposit_points())
        self.assertAlmostEqual(bare[2] + lowest, 140.5, delta=0.02)
        self.assertAlmostEqual(with_plate[2] + lowest, 140.5 + 6.35, delta=0.02)

    def test_seats_the_first_weld_on_the_measured_plane(self):
        s = cell_lc.fit_work_surface("seat", self.flat_at(140.5))
        tp = wall(150.0, 6, 2.0)
        o = cell_lc.snap_toolpath_to_surface(tp, 1100.0, -180.0, s, substrate_mm=6.35)
        lowest = o[2] + min(p[2] for p in tp.deposit_points())
        self.assertAlmostEqual(lowest, 140.5 + 6.35, delta=0.02)

    def test_measured_plane_seats_the_start_of_a_rising_weld(self):
        s = cell_lc.fit_work_surface("rising", self.flat_at(140.5))
        tp = parse("G90\nG0 X0 Y0 Z-2\nG1 X10 Z4 E1\n")
        origin = cell_lc.snap_toolpath_to_surface(
            tp, 1100.0, -180.0, s, substrate_mm=6.35)

        self.assertAlmostEqual(origin[2] - 2.0, 140.5 + 6.35, delta=0.02)

    def test_a_tilt_the_flat_job_frame_cannot_follow_is_refused(self):


        pts = [(x, y, 140.5 + 0.02 * (x - 1100.0)) for x, y in self.SPREAD]
        s = cell_lc.fit_work_surface("slope", self.touches(pts))
        tp = wall(150.0, 6, 2.0)
        with self.assertRaises(ValueError) as ctx:
            cell_lc.snap_toolpath_to_surface(tp, 1050.0, -180.0, s, max_deviation_mm=0.5)
        self.assertIn("varies", str(ctx.exception))

        cell_lc.snap_toolpath_to_surface(tp, 1050.0, -180.0, s, max_deviation_mm=5.0)

    def test_fixture_and_scalar_paths_agree_on_a_flat_surface(self):

        s = cell_lc.fit_work_surface("agree", self.flat_at(cell_lc.PLATTER_FACE_Z))
        tp = wall(150.0, 6, 2.0)
        by_plane = cell_lc.snap_toolpath_to_surface(
            tp, 1014.0, -180.0, s, substrate_mm=SUBSTRATE
        )
        by_scalar = cell_lc.snap_toolpath_to_platter(
            tp, 1014.0, -180.0, substrate_mm=SUBSTRATE
        )
        self.assertAlmostEqual(float(by_plane[2]), float(by_scalar[2]), delta=0.02)


class TestStickoutIsOneNumberInTwoFiles(unittest.TestCase):


    CELL = ROOT / "examples" / "cell_lc50s.json"

    def test_the_two_files_state_the_same_stickout(self):
        import json

        weld = cell_lc.CALIBRATION["touch_off"]["weld_stickout_mm"]
        ctwd = json.loads(self.CELL.read_text(encoding="utf-8"))["ctwd_mm"]
        self.assertIsNotNone(weld, "weld_stickout_mm is unset; it gates the offset")
        self.assertAlmostEqual(float(weld), float(ctwd), places=3)

    def test_the_offset_in_use_is_the_one_that_stickout_implies(self):


        t = cell_lc.CALIBRATION["touch_off"]
        again = cell_lc.offset_from_touch_off(
            t["pulses_SLURBT"],
            substrate_mm=t["substrate_mm"],
            touch_stickout_mm=t["touch_stickout_mm"],
            weld_stickout_mm=t["weld_stickout_mm"],
        )
        self.assertAlmostEqual(again, cell_lc.PLATTER_FACE_OFFSET_MM, places=3)

    def test_welding_longer_than_the_touch_off_lifts_the_commanded_tcp(self):


        t = cell_lc.CALIBRATION["touch_off"]
        touch = t["touch_stickout_mm"]
        at_touch = cell_lc.offset_from_touch_off(
            t["pulses_SLURBT"], substrate_mm=t["substrate_mm"],
            touch_stickout_mm=touch, weld_stickout_mm=touch)
        longer = cell_lc.offset_from_touch_off(
            t["pulses_SLURBT"], substrate_mm=t["substrate_mm"],
            touch_stickout_mm=touch, weld_stickout_mm=touch + 6.35)
        self.assertGreater(longer, at_touch)
        self.assertAlmostEqual(longer - at_touch, 6.35, places=3)
