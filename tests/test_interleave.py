import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import (cell_lc, correction, demos, layer_sense, sensed,
                           server, weld_ctwd)
from waam_pipeline.backends import jbi
from waam_pipeline.clearance import Scene, TorchModel, Violation
from waam_pipeline.emit import PlatterState, emit_cycle_job, emit_job
from waam_pipeline.slicer import interleave, to_gcode, wall
from waam_pipeline.substrate import Substrate
from waam_pipeline.thermal_plan import DEFAULT, layer_passes, schedule
from waam_pipeline.toolpath import Move, Toolpath


class TestInterleave(unittest.TestCase):
    def setUp(self):
        self.part = wall(60.0, 3, 2.0, 300.0)

    def test_order_layers_offsets_and_lift(self):
        path = interleave([self.part, self.part], [(0, 0), (0, 30)],
                          lift_mm=5, bead_height_mm=2)
        deposits = [move for move in path.moves if move.deposit]
        self.assertEqual([(move.layer, move.copy) for move in deposits],
                         [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)])
        self.assertEqual([move.y for move in deposits], [0, 30, 0, 30, 0, 30])
        self.assertEqual(len(path.layers()), 3)
        first_end, lift, across, approach = path.moves[1:5]
        self.assertEqual((lift.x, lift.y, lift.z), (first_end.x, first_end.y, 9.0))
        self.assertEqual((across.x, across.y, across.z), (0.0, 30.0, 9.0))
        self.assertEqual((approach.x, approach.y, approach.z), (0.0, 30.0, 2.0))
        self.assertFalse(lift.deposit or across.deposit or approach.deposit)

    def test_layer_starting_with_deposit_keeps_every_weld_segment(self):
        part = Toolpath([
            Move(0, 0, 2, None, False, 0, 1),
            Move(60, 0, 2, 300, True, 0, 2),
            Move(0, 0, 4, 300, True, 1, 3),
            Move(60, 0, 4, 300, True, 1, 4),
        ])

        def segments(path, copy=0):
            return [(path.moves[index - 1].target, move.target)
                    for index, move in enumerate(path.moves)
                    if move.deposit and move.copy == copy]

        expected = segments(part)
        path = interleave([part, part], [(0, 0), (0, 30)],
                          lift_mm=5, bead_height_mm=2)
        self.assertEqual(segments(path, 0), expected)
        shifted = [((a[0], a[1] + 30, a[2]), (b[0], b[1] + 30, b[2]))
                   for a, b in expected]
        self.assertEqual(segments(path, 1), shifted)

    def test_lift_uses_both_weld_endpoints_and_bead_height(self):
        part = Toolpath([
            Move(0, 0, 6, None, False, 0, 1),
            Move(60, 0, 2, 300, True, 0, 2),
            Move(60, 0, 4, None, False, 1, 3),
            Move(0, 0, 4, 300, True, 1, 4),
        ])
        path = interleave([part, part], [(0, 0), (0, 30)],
                          lift_mm=5, bead_height_mm=3)
        self.assertEqual(path.moves[2].z, 14.0)
        self.assertEqual(path.moves[3].z, 14.0)

    def test_refuses_overlap_count_and_layer_mismatch(self):
        with self.assertRaisesRegex(ValueError, "10 mm clearance"):
            interleave([self.part, self.part], [(0, 0), (0, 9.9)],
                       lift_mm=5, bead_height_mm=2)
        with self.assertRaisesRegex(ValueError, "same layer count"):
            interleave([self.part, wall(60, 2, 2)], [(0, 0), (0, 30)],
                       lift_mm=5, bead_height_mm=2)
        for count in (1, 5):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "2 to 4"):
                interleave([self.part] * count, [(0, 30 * i) for i in range(count)],
                           lift_mm=5, bead_height_mm=2)

    def test_one_copy_server_slice_is_the_original_path(self):
        params = {"kind": "wall", "shape": "wall", "wall_length": "60",
                  "wall_layers": "3", "lh": "2", "bead": "8.6", "travel": "300"}
        with mock.patch.object(server, "_SHOW_CELL", False):
            server.run_slice(params, None, "interleave-single-a")
            server.run_slice({**params, "copies": "1", "copy_spacing": "38.6"},
                             None, "interleave-single-b")
        original = server._state("interleave-single-a")["toolpath"]
        explicit = server._state("interleave-single-b")["toolpath"]
        self.assertEqual(original, explicit)
        self.assertEqual(to_gcode(original), to_gcode(explicit))
        self.assertNotIn("copies", server._state("interleave-single-b")["request"]["params"])

    def test_server_refuses_physical_footprint_overlap(self):
        params = {"kind": "wall", "shape": "wall", "wall_length": "60",
                  "wall_layers": "3", "lh": "2", "bead": "8.6", "travel": "300",
                  "copies": "2", "copy_spacing": "18.5"}
        with mock.patch.object(server, "_SHOW_CELL", False), \
                self.assertRaisesRegex(ValueError, "leave 10 mm"):
            server.run_slice(params, None, "interleave-overlap")

    def test_bead_set_defaults_to_its_full_width_plus_30(self):
        params = {"kind": "wall", "shape": "bead", "bead_length": "60",
                  "bead_count": "3", "bead_pitch": "25", "lh": "2",
                  "bead": "8.6", "travel": "300", "copies": "2"}
        with mock.patch.object(server, "_SHOW_CELL", False):
            result = server.run_slice(params, None, "interleave-beads")
        state = server._state("interleave-beads")
        self.assertEqual(result["layers"], 3)
        self.assertEqual(state["request"]["params"]["copy_spacing"], "88.6")
        self.assertEqual([(move.layer, move.copy) for move in state["toolpath"].moves
                          if move.deposit],
                         [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)])


class TestInterleaveIntegration(unittest.TestCase):
    def test_two_copy_wall_emits_and_dwells_once_per_layer_change(self):
        params = {"kind": "wall", "shape": "wall", "wall_length": "60",
                  "wall_layers": "3", "lh": "2", "bead": "8.6", "travel": "300",
                  "copies": "2", "copy_spacing": "40", "fixture": "0",
                  "repair": "1", "thermal": "off"}
        with mock.patch.object(server, "_SHOW_CELL", True):
            result = server.run_slice(params, None, "interleave-emit")
        self.assertEqual(result["blocking"], 0)
        state = server._state("interleave-emit")
        path, emit = state["toolpath"], state["emit"]
        with mock.patch.object(jbi, "_git_sha", return_value="test"):
            text, summary = emit_job(
                path, cell_lc.pulse_solver(emit["origin"]), name="COPYWALL", arc_file=81,
                scene=emit["scene"], torch=emit["torch"], bead_height_mm=emit["bead_height"],
                bead_width_mm=emit["bead"],
                platter=PlatterState(clear=True, attested_by="test"),
                date="2026/10/07 12:00",
            )
        self.assertEqual(summary["passes"], 6)
        self.assertEqual(text.count("TIMER T=30.00"), 2)

    def test_auto_and_layer_touch_refuse_interleaved_slice(self):
        path = interleave([wall(60, 2, 2), wall(60, 2, 2)],
                          [(0, 0), (0, 30)], lift_mm=5, bead_height_mm=2)
        state = {"toolpath": path, "emit": {"cell_profile": "lc50s", "scene": object(),
                                              "origin": object(), "copies": 2}}
        handler = server._Handler.__new__(server._Handler)
        handler._json = mock.Mock()
        handler._stale_gen = mock.Mock(return_value=False)
        handler.waam_who = {"email": "operator@example.com"}
        with mock.patch.object(server, "_state", return_value=state):
            handler._api_layer_sense_job("test", {})
            layer_error = handler._json.call_args.args[1]["error"]
            handler._json.reset_mock()
            self.assertIsNone(handler._cycle_build("test", {}))
            auto_error = handler._json.call_args.args[1]["error"]
        self.assertIn("interleaved copies", layer_error)
        self.assertIn("interleaved copies", auto_error)

    def test_single_part_library_entry_points_refuse_interleaved_paths(self):
        path = interleave([wall(60, 2, 2), wall(60, 2, 2)],
                          [(0, 0), (0, 30)], lift_mm=5, bead_height_mm=2)
        calls = (
            lambda: layer_sense.layer_sense_plan(path, 0, bead_height_mm=2),
            lambda: layer_sense.auto_plan(path, 0, 0, step_mm=2, spacing_mm=20),
            lambda: layer_sense.apply_auto(path, object(), first=0, last=0,
                                            step_mm=2, bead_height_mm=2),
            lambda: emit_cycle_job(
                path, lambda point: point, name="COPYAUTO", arc_file=12,
                cycle=1, layers=(0, 0), step_mm=2, tail=None,
                scene=object(), torch=object(), bead_height_mm=2,
            ),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, "interleaved copies"):
                call()

    def test_copy_identity_survives_toolpath_rebuilds(self):
        path = interleave([wall(60, 2, 2), wall(60, 2, 2)],
                          [(0, 0), (0, 30)], lift_mm=5, bead_height_mm=2)

        def unchanged(before, after):
            self.assertEqual(after.copies, 2)
            self.assertEqual([move.copy for move in after.moves],
                             [move.copy for move in before.moves])

        fed = demos.with_feed(path, 250)
        unchanged(path, fed)
        rotated, _ = correction.rotate_part(fed, None, 5)
        unchanged(fed, rotated)

        corners = np.array([[-100, -100, 0], [100, -100, 0],
                            [100, 100, 0], [-100, 100, 0]], dtype=float)
        plate = Substrate.fit(corners, 6.35)
        placed = plate.place(rotated)
        unchanged(rotated, placed)
        sensed_plate = sensed.SensedPlate(**plate.__dict__)
        followed = sensed_plate.place(rotated)
        unchanged(rotated, followed)

        runs = layer_sense._passes(path, 0)
        station_xy = np.array([[run[0][0].x, run[0][0].y] for run in runs])
        top = layer_sense.LayerTop(
            layer=0, stations=tuple((index, 0.0) for index in range(len(runs))),
            station_xy=station_xy, predicted=np.zeros(len(runs)),
            measured=np.full(len(runs), 0.1),
            labels=tuple(f"P{index}" for index in range(len(runs))),
            passes=tuple(layer_sense._chords(run) for run in runs), reach_mm=100.0,
        )
        reseated = top.apply(path)
        unchanged(path, reseated)

        probe = layer_sense.LayerProbe(
            name="copy", expected=np.array([0.0, 0.0, 4.0]),
            start=np.array([0.0, 0.0, 29.0]), target=np.array([0.0, 0.0, 2.5]),
        )
        sensed_moves, begin = layer_sense.sense_motions(path, 0, [probe])
        self.assertEqual(sensed_moves.copies, 2)
        self.assertEqual([move.copy for move in sensed_moves.moves[:begin]],
                         [move.copy for move in path.moves[:begin]])
        self.assertTrue(all(move.copy in (0, 1) for move in sensed_moves.moves))

        raised = weld_ctwd.apply(reseated, weld_ctwd.default_mm() + 1)[0]
        unchanged(reseated, raised)
        with mock.patch("waam_pipeline.clearance.check", return_value=[]) as checked:
            checked_raised = weld_ctwd.checked(
                reseated, weld_ctwd.default_mm() + 1,
                torch=TorchModel(), scene=Scene(), bead_height_mm=2,
            )[0]
        unchanged(reseated, checked_raised)
        unchanged(reseated, checked.call_args.args[0])

        travel = next(index for index, move in enumerate(path.moves)
                      if index and not move.deposit and path.moves[index - 1].deposit)
        hit = Violation(travel, path.moves[travel].line_no,
                        path.moves[travel].target, "fixture", 0.0)
        with mock.patch.object(correction, "check", side_effect=[[hit], []]):
            repaired = correction.repair_travels(
                path, TorchModel(), Scene(), bead_height_mm=2
            ).toolpath
        self.assertEqual(repaired.copies, 2)
        self.assertEqual([move.copy for move in repaired.moves if move.deposit],
                         [move.copy for move in path.moves if move.deposit])
        self.assertTrue(all(move.copy in (0, 1) for move in repaired.moves))

    def test_other_copy_work_reduces_required_layer_dwell(self):
        single = wall(60, 3, 2, 300)
        multiple = interleave([single, single], [(0, -20), (0, 20)],
                              lift_mm=5, bead_height_mm=2)
        timing = layer_passes(multiple, 300)
        self.assertTrue(all(layer["tail_gap_s"] > 0 for layer in timing["layers"][:-1]))
        one = schedule(DEFAULT, layer_passes(single, 300), start_c=30,
                       target_preweld_c=100)
        two = schedule(DEFAULT, timing, start_c=30, target_preweld_c=100)
        self.assertLess(two["layer_dwell_s"], one["layer_dwell_s"])
        self.assertEqual(two["copies"], 2)
        self.assertEqual({row["copy"] for row in two["rows"]}, {0, 1})


if __name__ == "__main__":
    unittest.main()
