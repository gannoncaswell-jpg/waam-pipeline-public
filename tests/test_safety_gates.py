import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline import plan
from waam_pipeline.backends import jbi
from waam_pipeline.clearance import (
    ClearanceError,
    Scene,
    TorchModel,
    blocking,
    check_point,
)
from waam_pipeline.frames import CellConfig
from waam_pipeline.gcode import ParseError, parse
from waam_pipeline.slicer import wall

ORIGIN = np.array([1014.0, -300.0, cell_lc.platter_work_surface_z(6.35)])


def write_json(data: dict) -> Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, fh)
    fh.close()
    return Path(fh.name)


GOOD_CELL = {
    "name": "test", "robot": "ar1440", "tool_number": 1, "user_frame": 2,
    "arc_file": 12, "between_layer_job": "COOL", "ctwd_mm": 15.0, "safe_z_mm": 50.0,
}


class TestCartesianEmitStaysBlocked(unittest.TestCase):


    def test_emit_jbi_refuses(self):
        tp = wall(50.0, 1, 2.0)
        with self.assertRaises(Exception) as ctx:
            jbi.emit_jbi(tp, CellConfig(**GOOD_CELL))
        self.assertNotIsInstance(
            ctx.exception, AssertionError,
            "the Cartesian block must be a real raise, not an assert",
        )


class TestArcFileIsRequired(unittest.TestCase):


    def test_missing_arc_file_is_a_type_error(self):
        tp = wall(50.0, 1, 2.0)
        with self.assertRaises(TypeError):
            jbi.emit_jbi_pulse(
                tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00"
            )

    def test_out_of_range_arc_file_refused(self):
        tp = wall(50.0, 1, 2.0)
        for bad in (0, -1, 1001, 12.5, True, "12"):
            with self.subTest(arc_file=bad), self.assertRaises(ValueError):
                jbi.emit_jbi_pulse(
                    tp, cell_lc.pulse_solver(ORIGIN), name="X",
                    date="2026/01/01 00:00", arc_file=bad,
                )

    def test_non_positive_hover_refused(self):

        tp = wall(50.0, 1, 2.0)
        for bad in (0.0, -50.0):
            with self.subTest(hover=bad), self.assertRaises(ValueError):
                jbi.emit_jbi_pulse(
                    tp, cell_lc.pulse_solver(ORIGIN), name="X",
                    date="2026/01/01 00:00", arc_file=12, hover_mm=bad,
                )


class TestEmitterChecksItsOwnSynthesisedMotion(unittest.TestCase):


    def test_obstacle_at_hover_height_blocks_emission(self):
        tp = wall(50.0, 2, 2.0)
        scene = Scene()

        scene.boxes.append(
            (
                "obstruction",
                np.array([-500.0, -500.0, 30.0]),
                np.array([500.0, 500.0, 300.0]),
            )
        )
        with self.assertRaises(ClearanceError):
            jbi.emit_jbi_pulse(
                tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
                arc_file=12, scene=scene, torch=TorchModel.yaskawa_accufast_45(),
            )

    def test_provenance_records_when_the_check_was_skipped(self):
        tp = wall(50.0, 1, 2.0)
        text = jbi.emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
            arc_file=12,
        )
        self.assertIn("clearance NO - UNCHECKED", text)

    def test_provenance_records_the_arc_file_and_a_passed_check(self):
        tp = wall(50.0, 1, 2.0)
        scene = cell_lc.lc_scene(ORIGIN, training_block=False)
        text = jbi.emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
            arc_file=7, scene=scene, torch=TorchModel.yaskawa_accufast_45(),
        )
        self.assertIn("' ASF#/AEF# 7", text)


        n = len(scene.boxes) + len(scene.capsules)
        self.assertGreater(n, 0)
        self.assertIn(f"clearance yes: {n} obstacles", text)

    def test_an_empty_scene_is_stamped_unchecked_not_checked(self):


        text = jbi.emit_jbi_pulse(
            wall(50.0, 1, 2.0), cell_lc.pulse_solver(ORIGIN),
            name="X", date="2026/01/01 00:00", arc_file=7,
            scene=Scene(), torch=TorchModel.yaskawa_accufast_45(),
        )
        self.assertIn("clearance NO - UNCHECKED", text)
        self.assertNotIn("clearance yes", text)

    def test_the_stamped_line_fits_the_pendant(self):


        for line in jbi._provenance_lines(
            7, cell_lc.lc_scene(ORIGIN, training_block=False),
            TorchModel.yaskawa_accufast_45(), 5.0, (),
        ):
            with self.subTest(line=line):
                self.assertLessEqual(len(line), 32)


class TestArcIsNeverCommandedAwayFromTheWeld(unittest.TestCase):
    def test_first_move_depositing_is_refused(self):


        tp = parse("G90\nG1 X10 E1\nG1 X20 E2\n")
        self.assertTrue(tp.moves[0].deposit, "fixture must start with a deposit move")
        with self.assertRaises(ValueError) as ctx:
            jbi.emit_jbi_pulse(
                tp, cell_lc.pulse_solver(ORIGIN), name="X",
                date="2026/01/01 00:00", arc_file=12,
            )
        self.assertIn("first move deposits", str(ctx.exception))

    def test_arc_is_struck_only_after_arriving_at_the_weld_start(self):


        tp = parse("G90\nG0 X0 Y0 Z0\nG1 X10 E1\nG0 X0 Y5 Z2\nG1 X10 E2\n")
        text = jbi.emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
            arc_file=12,
        )
        body = [ln for ln in text.splitlines()
                if ln[:5] in ("MOVJ ", "MOVL ", "ARCON", "ARCOF")]
        arcons = [i for i, ln in enumerate(body) if ln.startswith("ARCON")]
        self.assertTrue(arcons, "fixture should produce at least one arc start")
        for i in arcons:
            self.assertTrue(
                body[i - 1].startswith("MOVL"),
                f"ARCON preceded by {body[i - 1]!r}; the arc is struck away "
                "from the weld start",
            )

    def test_every_arcon_is_closed(self):
        tp = parse("G90\nG0 X0 Y0 Z0\nG1 X10 E1\nG0 X20\nG1 X30 E2\n")
        text = jbi.emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
            arc_file=12,
        )
        self.assertEqual(
            text.count("ARCON"), text.count("ARCOF"),
            "an unmatched ARCON leaves the arc live",
        )

    def test_no_movj_while_the_arc_is_on(self):

        tp = parse("G90\nG0 X0 Y0 Z0\nG1 X10 E1\nG0 X20\nG1 X30 E2\n")
        text = jbi.emit_jbi_pulse(
            tp, cell_lc.pulse_solver(ORIGIN), name="X", date="2026/01/01 00:00",
            arc_file=12,
        )
        live = 0
        for ln in text.splitlines():
            if ln.startswith("ARCON"):
                live += 1
            elif ln.startswith("ARCOF"):
                live -= 1
            elif ln.startswith("MOVJ"):
                self.assertEqual(live, 0, "MOVJ emitted while the arc is on")


class TestIkBranchContinuityGuard(unittest.TestCase):


    @staticmethod
    def _flipping_solver(jump_deg):
        postures = [
            [0.0, 20.0, -30.0, 0.0, 40.0, 0.0],
            [0.0, 20.0 + jump_deg, -30.0, 0.0, 40.0, 0.0],
        ]
        state = {"n": 0}

        def solve(_xyz):
            q = postures[state["n"] % 2]
            state["n"] += 1
            return list(q)

        return solve

    def _emit(self, jump_deg):
        return jbi.emit_jbi_pulse(
            wall(50.0, 2, 2.0), self._flipping_solver(jump_deg),
            name="X", date="2026/01/01 00:00", arc_file=12,
        )

    def test_large_branch_flip_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._emit(120.0)
        self.assertIn("discontinuity", str(ctx.exception))

    def test_guard_threshold_is_not_slack(self):


        with self.assertRaises(ValueError):
            self._emit(plan.MAX_JOINT_STEP_DEG + 1.0)

    def test_threshold_constant_is_a_branch_detector_not_a_comfort_limit(self):


        self.assertLessEqual(
            plan.MAX_JOINT_STEP_DEG, 45.0,
            "this is a branch-flip detector, not a comfort limit; above ~45 deg "
            "a straight-line weld can reorient through space the clearance "
            "check never sampled",
        )

    def test_small_steps_still_emit(self):
        self.assertIn("MOVL", self._emit(1.0))


def scene_json(scene: Scene) -> Path:


    return write_json({
        "boxes": [{"name": n, "min": list(map(float, mn)), "max": list(map(float, mx))}
                  for n, mn, mx in scene.boxes],
        "capsules": [{"name": n, "p0": list(map(float, p0)), "p1": list(map(float, p1)),
                      "radius": float(r)}
                     for n, p0, p1, r in scene.capsules],
        "part_fixed": list(scene.part_fixed) if scene.part_fixed is not None else None,
    })


class TestCliAsksTheSameQuestionAsTheGate(unittest.TestCase):


    def _run(self, argv):
        import waam_pipeline.__main__ as cli

        old = sys.argv
        sys.argv = argv
        try:
            return cli.main()
        finally:
            sys.argv = old

    def setUp(self):
        from waam_pipeline.slicer import to_gcode

        tp = wall(150.0, 6, 2.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=6.35)
        self.origin = origin
        self.scene_path = scene_json(cell_lc.lc_scene(origin, training_block=False))
        fh = tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False, encoding="utf-8")
        fh.write(to_gcode(tp))
        fh.close()
        self.gcode_path = Path(fh.name)

    def test_a_seated_job_the_emitter_accepts_is_not_refused_by_the_cli(self):
        code = self._run(["waam_pipeline", str(self.gcode_path),
                          "--check", "--bead-height", "2.0", "--scene", str(self.scene_path)])
        self.assertEqual(code, 0, "the CLI refused a job emit_job emits clean")

    def test_the_same_job_carries_no_platter_blocker(self):


        from waam_pipeline.clearance import CONTACT_OK, check

        tp = parse(self.gcode_path.read_text(encoding="utf-8"))
        violations = check(tp, TorchModel.yaskawa_accufast_45(),
                           Scene.load(self.scene_path),
                           bead_height_mm=2.0, contact_ok=CONTACT_OK)
        blockers = blocking(violations)
        self.assertEqual(
            [v.obstacle for v in blockers], [],
            "the gate itself blocks this job; the CLI's exit 0 would be wrong",
        )

    def test_a_real_penetration_still_exits_1(self):


        sunk = cell_lc.lc_scene(self.origin - np.array([0.0, 0.0, 40.0]),
                                training_block=False)
        code = self._run(["waam_pipeline", str(self.gcode_path),
                          "--check", "--bead-height", "2.0", "--scene", str(scene_json(sunk))])
        self.assertEqual(code, 1, "a 40 mm penetration of the platter was forgiven")


class TestClearanceGateSemantics(unittest.TestCase):
    def test_penetration_of_a_contact_ok_obstacle_still_blocks(self):

        from waam_pipeline.clearance import Violation

        near = Violation(0, 0, (0, 0, 0), "charles_platter", 1.2)
        deep = Violation(0, 0, (0, 0, 0), "charles_platter", -20.0)
        self.assertEqual(blocking([near]), [])
        self.assertEqual(blocking([deep]), [deep])

    def test_unlisted_obstacle_blocks_on_mere_proximity(self):
        from waam_pipeline.clearance import Violation

        v = Violation(0, 0, (0, 0, 0), "positioner_stand", 2.0)
        self.assertEqual(blocking([v]), [v])

    def test_worst_obstacle_is_not_masked_by_declaration_order(self):

        scene = Scene()
        scene.boxes.append(("benign_declared_first",
                            np.array([-200.0, -200.0, 40.0]), np.array([200.0, 200.0, 45.0])))
        scene.boxes.append(("deep_collision",
                            np.array([-5.0, -5.0, -50.0]), np.array([5.0, 5.0, 50.0])))
        hits = check_point((0.0, 0.0, 0.0), TorchModel.yaskawa_accufast_45(), scene, 5.0)
        self.assertIn("deep_collision", {v.obstacle for v in hits},
                      "a deep collision was hidden behind an earlier declaration")


class TestCellConfigValidatesValues(unittest.TestCase):
    def test_good_config_loads(self):
        self.assertEqual(CellConfig.load(write_json(GOOD_CELL)).arc_file, 12)

    def test_out_of_range_values_refused(self):
        for key, bad in [
            ("tool_number", 999), ("user_frame", -1), ("arc_file", 0),
            ("ctwd_mm", -5.0), ("safe_z_mm", 0.0), ("between_layer_job", "  "),
        ]:
            with self.subTest(key=key):
                data = dict(GOOD_CELL, **{key: bad})
                with self.assertRaises(ValueError):
                    CellConfig.load(write_json(data))


class TestGcodeIsStrict(unittest.TestCase):


    def test_frame_and_motion_commands_raise(self):
        for src in (
            "G90\nG28\nG1 X5 E1\n",
            "G90\nG54\nG1 X5 E1\n",
            "G90\nG51 P2\nG1 X5 E1\n",
            "G90\nG2 X10 Y10 I5 J0\n",
            "G90\nM999\nG1 X5 E1\n",
        ):
            with self.subTest(src=src.splitlines()[1]), self.assertRaises(ParseError):
                parse(src)

    def test_mistyped_digit_inside_a_coordinate_raises(self):

        with self.assertRaises(ParseError):
            parse("G90\nG1 X1O.5 E1\n")
        self.assertAlmostEqual(parse("G90\nG1 X10.5 E1\n").moves[-1].x, 10.5)

    def test_stray_characters_raise(self):
        with self.assertRaises(ParseError):
            parse("G90\nG1 X10 @@ E1\n")

    def test_inch_mode_scales_rather_than_silently_passing(self):
        self.assertAlmostEqual(parse("G20\nG90\nG1 X1 E1\n").moves[-1].x, 25.4)


if __name__ == "__main__":
    unittest.main()


class TestEmptySceneIsRefusedAtEmit(unittest.TestCase):


    def _emit(self, scene):
        from waam_pipeline.emit import PlatterState, emit_job

        tp = wall(60.0, layers=2, layer_height_mm=3.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -300.0, substrate_mm=6.35)
        return emit_job(
            tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="EMPTY", arc_file=12,
            scene=scene, torch=TorchModel.yaskawa_accufast_45(),
            date="2026/09/04 00:00",
            platter=PlatterState(clear=True, training_block_on=False, attested_by="test"),
        )

    def test_emit_job_refuses_a_scene_that_models_nothing(self):
        from waam_pipeline.emit import EmitRefused

        with self.assertRaises(EmitRefused) as cm:
            self._emit(Scene())
        self.assertEqual(cm.exception.reason, "clearance")
        self.assertIn("models nothing", cm.exception.detail)

    def test_the_refusal_names_the_way_out(self):


        from waam_pipeline.emit import EmitRefused

        with self.assertRaises(EmitRefused) as cm:
            self._emit(Scene())
        self.assertIn("lc_scene", cm.exception.detail)

    def test_there_is_no_allow_empty_escape_hatch(self):
        import inspect

        from waam_pipeline.emit import emit_job, emit_ros2

        for fn in (emit_job, emit_ros2):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("allow_empty", inspect.signature(fn).parameters)

    def test_a_real_scene_still_emits(self):

        tp = wall(60.0, layers=2, layer_height_mm=3.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -300.0, substrate_mm=6.35)
        text, _ = self._emit(cell_lc.lc_scene(origin, training_block=False))
        self.assertIn("NOP", text)


class TestNonAsciiIsRefusedNotCrashed(unittest.TestCase):


    DASH = "—"

    def _emit(self, **kw):
        from waam_pipeline.emit import PlatterState, emit_job

        tp = wall(60.0, layers=1, layer_height_mm=3.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=6.35)
        kw.setdefault("platter", PlatterState(clear=True, training_block_on=False,
                                              attested_by="test"))
        return emit_job(
            tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="ASCII", arc_file=12,
            scene=cell_lc.lc_scene(part_origin_robot=origin, training_block=False),
            torch=TorchModel.yaskawa_accufast_45(), date="2026/09/19 00:00", **kw)

    def test_a_non_ascii_comment_is_a_refusal(self):
        from waam_pipeline.emit import EmitRefused

        with self.assertRaises(EmitRefused) as cm:
            self._emit(comments=(f"layer 1 {self.DASH} first pass",))
        self.assertEqual(cm.exception.reason, "argument")
        self.assertIn("ASCII", cm.exception.detail)

    def test_a_non_ascii_attestation_is_a_refusal(self):
        from waam_pipeline.emit import EmitRefused, PlatterState

        with self.assertRaises(EmitRefused) as cm:
            self._emit(platter=PlatterState(clear=True, training_block_on=False,
                                            attested_by="André"))
        self.assertEqual(cm.exception.reason, "argument")

    def test_a_non_ascii_job_name_is_a_value_error(self):
        with self.assertRaises(ValueError):
            jbi._check_job_name("WALL±1")

    def test_the_ascii_case_still_emits(self):
        text, _ = self._emit(comments=("layer 1 - first pass",))
        text.encode("ascii")


class TestSceneLoadRefusesAFileItCannotRead(unittest.TestCase):


    def test_a_mistyped_key_raises(self):
        with self.assertRaises(ValueError) as cm:
            Scene.load(write_json({"box": [{"name": "clamp", "min": [0, 0, 0],
                                            "max": [1, 1, 1]}]}))


        self.assertIn("box", str(cm.exception))
        self.assertIn("boxes", str(cm.exception))

    def test_capsules_alone_are_a_valid_scene(self):

        sc = Scene.load(write_json({"capsules": [
            {"name": "cable", "p0": [0, 0, 0], "p1": [0, 0, 10], "radius": 2.0}]}))
        self.assertEqual(len(sc.capsules), 1)

    def test_a_box_with_its_corners_swapped_raises(self):


        with self.assertRaises(ValueError) as cm:
            Scene.load(write_json({"boxes": [{"name": "clamp", "min": [10, 10, 10],
                                              "max": [0, 0, 0]}]}))
        self.assertIn("clamp", str(cm.exception))

    def test_a_zero_volume_box_raises(self):
        with self.assertRaises(ValueError):
            Scene.load(write_json({"boxes": [{"name": "flat", "min": [0, 0, 5],
                                              "max": [10, 10, 5]}]}))

    def test_a_non_finite_corner_raises(self):
        with self.assertRaises(ValueError):
            Scene.load(write_json({"boxes": [{"name": "nan", "min": [0, 0, 0],
                                              "max": [1, 1, float("inf")]}]}))

    def test_a_zero_length_zero_radius_capsule_raises(self):
        with self.assertRaises(ValueError) as cm:
            Scene.load(write_json({"capsules": [{"name": "dot", "p0": [0, 0, 0],
                                                 "p1": [0, 0, 0], "radius": 0.0}]}))
        self.assertIn("dot", str(cm.exception))

    def test_a_zero_length_capsule_with_a_radius_is_a_sphere_and_loads(self):

        sc = Scene.load(write_json({"capsules": [{"name": "knob", "p0": [0, 0, 0],
                                                  "p1": [0, 0, 0], "radius": 4.0}]}))
        self.assertEqual(len(sc.capsules), 1)

    def test_a_real_scene_round_trips(self):
        sc = cell_lc.lc_scene(ORIGIN, training_block=False)
        back = Scene.load(scene_json(sc))
        self.assertEqual([n for n, *_ in back.boxes], [n for n, *_ in sc.boxes])


class TestInterpassGate(unittest.TestCase):


    def _emit(self, layers, **kw):
        from waam_pipeline import cell_lc
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState, emit_job
        from waam_pipeline.slicer import wall
        tp = wall(60.0, layers=layers, layer_height_mm=3.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -300.0, substrate_mm=6.35)
        return emit_job(
            tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="GATE", arc_file=12,


            scene=cell_lc.lc_scene(origin, training_block=False),
            torch=TorchModel.yaskawa_accufast_45(),
            date="2026/09/04 00:00",
            platter=PlatterState(clear=True, training_block_on=False, attested_by="test"),
            **kw)

    def test_multi_layer_without_dwell_is_refused(self):
        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused) as cm:
            self._emit(4, interpass_timer_s=0.0)
        self.assertEqual(cm.exception.reason, "interpass")
        self.assertIn("520", cm.exception.detail)

    def test_a_dwell_passes(self):
        text, summary = self._emit(4, interpass_timer_s=30.0)
        self.assertIn("NOP", text)
        self.assertEqual(summary["name"], "GATE")

    def test_single_layer_needs_no_interpass(self):

        text, _ = self._emit(1, interpass_timer_s=0.0)
        self.assertIn("NOP", text)

    def test_the_refusal_can_be_asked_past_deliberately(self):

        text, _ = self._emit(4, interpass_timer_s=0.0, allow_no_interpass=True)
        self.assertIn("NOP", text)


class TestProcessSlot(unittest.TestCase):


    def _cfg(self, **kw):
        from waam_pipeline.frames import CellConfig
        base = dict(name="t", robot="ar1440", tool_number=0, user_frame=1,
                    arc_file=12, ctwd_mm=15.0, safe_z_mm=50.0,
                    between_layer_job="COOL")
        base.update(kw)
        return CellConfig(**base)

    def test_default_is_the_chosen_stt_slot(self):
        self.assertEqual(self._cfg().proc, 4)

    def test_an_empty_slot_is_refused(self):
        for slot in (7, 10, 13):
            errs = self._cfg(proc=slot).validation_errors()
            self.assertTrue(any("EMPTY slot" in e for e in errs), slot)

    def test_populated_slots_pass(self):
        for slot in (1, 4, 6, 14, 16):
            self.assertEqual(self._cfg(proc=slot).validation_errors(), [], slot)

    def test_out_of_range_still_refused(self):
        self.assertTrue(self._cfg(proc=17).validation_errors())
        self.assertTrue(self._cfg(proc=0).validation_errors())


class TestTrainingBlockPresence(unittest.TestCase):


    def _names(self, **kw):
        from waam_pipeline import cell_lc
        return {b[0] for b in cell_lc.lc_scene(**kw).boxes}

    def test_default_follows_the_calibration_file(self):
        from waam_pipeline import cell_lc
        saved = cell_lc.TRAINING_BLOCK_ON_PLATTER
        try:
            cell_lc.TRAINING_BLOCK_ON_PLATTER = True
            self.assertIn("manpro_training_block", self._names())
            cell_lc.TRAINING_BLOCK_ON_PLATTER = False
            self.assertNotIn("manpro_training_block", self._names())
        finally:
            cell_lc.TRAINING_BLOCK_ON_PLATTER = saved

    def test_an_explicit_argument_still_wins(self):

        from waam_pipeline import cell_lc
        saved = cell_lc.TRAINING_BLOCK_ON_PLATTER
        try:
            cell_lc.TRAINING_BLOCK_ON_PLATTER = True
            self.assertNotIn("manpro_training_block", self._names(training_block=False))
            cell_lc.TRAINING_BLOCK_ON_PLATTER = False
            self.assertIn("manpro_training_block", self._names(training_block=True))
        finally:
            cell_lc.TRAINING_BLOCK_ON_PLATTER = saved

    def test_default_is_off_per_the_owner(self):


        from waam_pipeline import cell_lc
        self.assertFalse(cell_lc.TRAINING_BLOCK_ON_PLATTER)

    def test_only_a_real_boolean_is_accepted(self):

        from waam_pipeline import cell_lc
        saved = cell_lc.CALIBRATION
        try:
            for bad in ("false", "true", 0, 1, None):
                cell_lc.CALIBRATION = {"training_block": {"on_platter": bad}}
                with self.assertRaises(ValueError, msg=repr(bad)):
                    cell_lc._load_training_block()
            cell_lc.CALIBRATION = {"training_block": {"on_platter": False}}
            self.assertFalse(cell_lc._load_training_block()[2])
            cell_lc.CALIBRATION = {"training_block": {}}
            self.assertTrue(cell_lc._load_training_block()[2])
        finally:
            cell_lc.CALIBRATION = saved


class TestPlatterAttestation(unittest.TestCase):


    def _emit(self, platter, block_in_scene=False, **kw):
        from waam_pipeline import cell_lc
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import emit_job
        from waam_pipeline.slicer import wall
        tp = wall(60.0, layers=3, layer_height_mm=3.0)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -300.0, substrate_mm=6.35)
        return emit_job(
            tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="ATTEST", arc_file=12,
            scene=cell_lc.lc_scene(origin, training_block=block_in_scene),
            torch=TorchModel.yaskawa_accufast_45(),
            date="2026/09/04 00:00", platter=platter, **kw)

    def _ok(self, **kw):
        from waam_pipeline.emit import PlatterState
        base = dict(clear=True, training_block_on=False, attested_by="tester")
        base.update(kw)
        return PlatterState(**base)

    def test_missing_attestation_is_refused_with_a_reason(self):
        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused) as cm:
            self._emit(None)
        self.assertEqual(cm.exception.reason, "attestation")
        self.assertIn("no default", cm.exception.detail)

    def test_not_clear_is_refused(self):
        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused):
            self._emit(self._ok(clear=False))

    def test_nobody_signed_it_is_refused(self):
        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused) as cm:
            self._emit(self._ok(attested_by="  "))
        self.assertIn("who looked", cm.exception.detail)

    def test_block_mismatch_is_refused_both_ways(self):

        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused) as cm:
            self._emit(self._ok(training_block_on=True), block_in_scene=False)
        self.assertIn("attested", cm.exception.detail)
        with self.assertRaises(EmitRefused):
            self._emit(self._ok(training_block_on=False), block_in_scene=True)

    def test_unknown_fixture_is_refused(self):
        from waam_pipeline.emit import EmitRefused
        with self.assertRaises(EmitRefused) as cm:
            self._emit(self._ok(fixture="no-such-fixture"))
        self.assertIn("not registered", cm.exception.detail)

    def test_a_consistent_attestation_emits_and_is_stamped(self):
        text, summary = self._emit(self._ok())
        self.assertIn("NOP", text)
        self.assertIn("platter attested by tester", summary["platter"])
        self.assertIn("training block off", summary["platter"])
        self.assertIn("platter attested by tester", text)

    def test_a_calibration_file_newer_than_the_loaded_copy_refuses_emit(self):


        from waam_pipeline import cell_lc
        from waam_pipeline.emit import EmitRefused
        saved = cell_lc._CALIBRATION_MTIME_NS
        try:

            cell_lc._CALIBRATION_MTIME_NS = 0
            self.assertIsNotNone(cell_lc.calibration_stale_reason())
            with self.assertRaises(EmitRefused) as ctx:
                self._emit(self._ok())
            self.assertEqual(ctx.exception.reason, "calibration")
            self.assertIn("RESTART", ctx.exception.detail)
        finally:
            cell_lc._CALIBRATION_MTIME_NS = saved
        self.assertIsNone(cell_lc.calibration_stale_reason())
        self._emit(self._ok())

    def test_preflight_state_reads_the_block_off_the_scene(self):
        from waam_pipeline import cell_lc
        from waam_pipeline.emit import _preflight_platter_state
        on = _preflight_platter_state(cell_lc.lc_scene(training_block=True), attested_by="p")
        off = _preflight_platter_state(cell_lc.lc_scene(training_block=False), attested_by="p")
        self.assertTrue(on.training_block_on)
        self.assertFalse(off.training_block_on)

    def test_a_preflight_state_cannot_emit_a_job(self):


        from waam_pipeline import cell_lc
        from waam_pipeline.emit import EmitRefused, _preflight_platter_state
        p = _preflight_platter_state(cell_lc.lc_scene(training_block=False),
                                     attested_by="not an operator")
        self.assertIn("NOT ATTESTED (preflight", p.stamp())
        with self.assertRaises(EmitRefused) as ctx:
            self._emit(p)
        self.assertEqual(ctx.exception.reason, "attestation")
        self.assertIn("preflight", ctx.exception.detail)

    def test_cli_flags_build_an_attestation(self):
        from waam_pipeline.emit import platter_from_cli
        p = platter_from_cli(["--platter-clear", "--block", "off", "--by", "operator"])
        self.assertTrue(p.clear); self.assertFalse(p.training_block_on)
        self.assertEqual(p.attested_by, "operator")
        p = platter_from_cli(["--platter-clear", "--block", "on", "--fixture", "f1", "--by", "x"])
        self.assertTrue(p.training_block_on); self.assertEqual(p.fixture, "f1")

    def test_cli_without_flags_and_without_a_terminal_refuses(self):

        import io
        import sys
        from unittest import mock

        from waam_pipeline.emit import EmitRefused, platter_from_cli


        with mock.patch.object(sys, "stdin", io.StringIO("")):
            with self.assertRaises(EmitRefused) as cm:
                platter_from_cli([])
            self.assertIn("--platter-clear", cm.exception.detail)

    def test_a_lying_isatty_still_refuses(self):


        import io
        import sys
        from unittest import mock

        from waam_pipeline.emit import EmitRefused, platter_from_cli

        class LyingStdin(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(sys, "stdin", LyingStdin("")):
            self.assertTrue(sys.stdin.isatty())
            with self.assertRaises(EmitRefused) as cm:
                platter_from_cli([])
            self.assertIn("nobody attested", cm.exception.detail)
