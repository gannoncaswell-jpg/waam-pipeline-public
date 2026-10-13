import hashlib
import math
import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests.test_interpass_control import JOB, SOLVE, continuous, timers
from waam_pipeline import cell_lc, emit, passes, weld_ctwd
from waam_pipeline.backends import jbi, ros2
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, PlatterState
from waam_pipeline.plan import MeasureStop, READING_HOLD_S, ROLES, Step, build_plan
from waam_pipeline.slicer import wall


def instructions(text):
    return [line for line in text.split("NOP\r\n", 1)[1].splitlines()
            if not line.startswith("'")]


def motion_names(lines):
    return [line.split(" ", 1)[0] if line.startswith(("MOVJ ", "MOVL ")) else line
            for line in lines]


class MeasurementCase(unittest.TestCase):
    def setUp(self):
        self.tp = wall(60.0, 4, 2.0)

        for module in (jbi, ros2):
            patcher = mock.patch.object(module, "_git_sha", return_value="unknown")
            patcher.start()
            self.addCleanup(patcher.stop)

    def render(self, **kwargs):
        return jbi.emit_jbi_pulse(self.tp, SOLVE, **JOB, **kwargs)

    def checked(self, *, cycle=False, ros=False, tp=None, **kwargs):
        tp = self.tp if tp is None else tp
        origin = cell_lc.snap_origin_to_platter(
            1014.0, -180.0, substrate_mm=6.35, first_deposit_z_mm=2.0)
        common = dict(name="MEASURE", scene=cell_lc.lc_scene(origin, training_block=False),
                      torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.0,
                      bead_width_mm=6.0, platter=PlatterState(clear=True, attested_by="t"))
        default = {"weld_ctwd_mm": weld_ctwd.default_mm(), "weld_ctwd_delta_mm": 0.0,
                   "weld_ctwd_notes": []}
        with mock.patch.object(emit, "_gate", return_value=(tp, [], default)):
            if ros:
                return emit.emit_ros2(tp, cell_lc.pulse_solver(origin), **common, **kwargs)
            common.update(arc_file=81, date=JOB["date"])
            if cycle:
                return emit.emit_cycle_job(
                    tp, cell_lc.pulse_solver(origin), cycle=1, layers=(0, 3),
                    step_mm=2.0, tail=None, layers_per_interpass=4, **common, **kwargs)
            return emit.emit_job(tp, cell_lc.pulse_solver(origin), **common, **kwargs)


class TestMeasureStopValidation(MeasurementCase):
    def test_defaults_and_immutable_configuration(self):
        measure = MeasureStop(1)
        self.assertEqual(measure.readings_s, (20.0, 40.0, 60.0))
        self.assertEqual((measure.pause, measure.clean, measure.pre_weld), (True, False, True))
        with self.assertRaises(FrozenInstanceError):
            measure.every = 2
        self.assertTrue({"measure_reading", "measure_pause", "clean", "pre_weld"} <= set(ROLES))

    def test_invalid_fields_have_clear_errors(self):
        for bad in (0, -1, True, 1.5, "2", None):
            with self.subTest(every=bad), self.assertRaisesRegex(ValueError, "every"):
                MeasureStop(bad)
        for bad in ((0,), (3601,), (float("nan"),), (float("inf"),), (True,),
                    ("20",), (20, 20), (40, 20), (10**400,), (1.006,), (1.0, 1.005), None, "20"):
            with self.subTest(readings_s=bad), self.assertRaisesRegex(ValueError, "readings_s"):
                MeasureStop(1, readings_s=bad)
        for field in ("pause", "clean", "pre_weld"):
            for bad in (0, 1, None, "yes"):
                with self.subTest(field=field, value=bad), self.assertRaisesRegex(ValueError, field):
                    MeasureStop(1, **{field: bad})
        with self.assertRaisesRegex(ValueError, "measure"):
            build_plan(self.tp, SOLVE, measure={"every": 1})

    def test_empty_and_endpoint_readings_are_valid(self):
        for readings in ((), (1.0, 3600.0)):
            self.assertEqual(MeasureStop(1, readings_s=readings).readings_s, readings)


class TestMeasureStopSequence(MeasurementCase):
    def test_default_output_is_byte_identical_with_explicit_none(self):
        self.assertEqual(self.render(), self.render(measure=None))
        self.assertEqual(hashlib.sha256(self.render().encode("ascii")).hexdigest(),
                         "356fafe59fc0d2ce696980de4add2da52812f598840b67054598c53e1d682a58")
        self.assertEqual(self.render(temp_check_every=2, interpass_timer_s=1000.0),
                         self.render(temp_check_every=2, interpass_timer_s=1000.0, measure=None))
        legacy_checks = self.render(temp_check_every=2, interpass_timer_s=1000.0)
        self.assertEqual(hashlib.sha256(legacy_checks.encode("ascii")).hexdigest(),
                         "a6ded09c5328df92a47102334dcdc9a8c53b475b5c913bfcbe33531ca8cf71bb")
        self.assertEqual(build_plan(self.tp, SOLVE), build_plan(self.tp, SOLVE, measure=None))
        self.assertNotIn("MEASURE STOP", self.render())

    def test_exact_parked_sequence_every_one_and_two_layers(self):
        expected = ["MOVL", "MOVJ", "TIMER T=20.00", 'MSG "TEMP 20 S AFTER WELD"',
                    "TIMER T=20.00", 'MSG "TEMP 40 S AFTER WELD"',
                    "TIMER T=20.00", 'MSG "TEMP 60 S AFTER WELD"', "TIMER T=5.00", 'MSG ""',
                    'MSG "MEASURE BEAD THEN PRESS START"', "PAUSE", 'MSG ""',
                    "TIMER T=35.00", 'MSG "READ PRE WELD TEMP THEN START"', "PAUSE", 'MSG ""',
                    "MOVJ", "MOVL", "MOVL"]
        for every, layers in ((1, [1, 2, 3]), (2, [2])):
            with self.subTest(every=every):
                measure = MeasureStop(every)
                text = self.render(measure=measure, interpass_timer_s=100.0, temp_check_every=1)
                lines = instructions(text)
                starts = [i - 2 for i, line in enumerate(lines)
                          if line == "TIMER T=20.00" and lines[i + 1] == 'MSG "TEMP 20 S AFTER WELD"']
                self.assertEqual(len(starts), len(layers) + 1)
                for start in starts[:-1]:
                    self.assertEqual(motion_names(lines[start:start + len(expected)]), expected)
                self.assertEqual(motion_names(lines[starts[-1]:]), expected[:13] + ["END"])
                steps = build_plan(self.tp, SOLVE, measure=measure, interpass_timer_s=100.0,
                                   temp_check_every=1)
                self.assertEqual([s.layer for s in steps if s.role == "measure_pause"], layers + [3])
                self.assertEqual([s.layer for s in steps if s.role == "temp_check"],
                                 [layer for layer in (1, 2, 3) if layer not in layers])
                self.assertEqual(text.count("TIMER T=100.00"), 3 - len(layers))

    def test_reading_deltas_and_remaining_dwell(self):
        measure = MeasureStop(1, readings_s=(10.0, 25.0, 60.0), pause=False, pre_weld=False)
        for interpass, remaining in ((100.0, [35.0]), (65.0, []), (60.0, []), (30.0, []), (0.0, [])):
            with self.subTest(interpass=interpass):
                text = self.render(measure=measure, interpass_timer_s=interpass)
                readings = [10.0, 15.0, 35.0, READING_HOLD_S]
                self.assertEqual(timers(text), [2.0] + (readings + remaining) * 3 + readings)
                self.assertNotIn("PAUSE", text)
                self.assertEqual(text.count('MSG "TEMP 60 S AFTER WELD"\r\nTIMER T=5.00\r\nMSG ""'), 4)

    def test_empty_readings_and_long_waits(self):
        text = self.render(measure=MeasureStop(1, readings_s=(), pause=False, pre_weld=False),
                           interpass_timer_s=1000.0)
        self.assertEqual(timers(text), [2.0] + [500.0, 500.0] * 3)
        self.assertNotIn("TEMP", text)
        text = self.render(measure=MeasureStop(1, readings_s=(1000.0, 3600.0),
                                               pause=False, pre_weld=False),
                           interpass_timer_s=3600.0)
        self.assertEqual(timers(text),
                         [2.0] + [500.0, 500.0, 650.0, 650.0, 650.0, 650.0, READING_HOLD_S] * 4)

    def test_cadence_is_independent_of_interpass_dwell_cadence(self):
        text = self.render(measure=MeasureStop(1, readings_s=(10.0,), pause=False, pre_weld=False),
                           interpass_timer_s=100.0, layers_per_interpass=2)
        self.assertEqual(timers(text), [2.0] + [10.0, READING_HOLD_S, 85.0] * 3
                         + [10.0, READING_HOLD_S])

    def test_clean_returns_to_a_fresh_park_record(self):
        plain = self.render(measure=MeasureStop(1), interpass_timer_s=100.0)
        text = self.render(measure=MeasureStop(1, clean=True), interpass_timer_s=100.0)
        lines = instructions(text)
        calls = [i for i, line in enumerate(lines) if line == "CALL JOB:CLEAN_R1"]
        self.assertEqual(len(calls), 4)
        records = dict(re.findall(r"^(C\d{5})=(.*)$", text, re.MULTILINE))
        references = re.findall(r"^MOV[JL] (C\d{5})", text, re.MULTILINE)
        npos = int(re.search(r"///NPOS (\d+),", text).group(1))
        self.assertEqual(npos, len(references))
        self.assertEqual(npos, len(records))
        self.assertEqual(len(set(references)), len(references))
        self.assertEqual(len(references), len(re.findall(r"^MOV[JL] ", plain, re.MULTILINE)) + 4)
        for i in calls:
            self.assertRegex(lines[i + 1], r"^MOVJ C\d{5} VJ=50\.00$")
            park = lines[i + 1].split()[1]
            self.assertEqual(records[park].strip(), ",".join(str(p) for p in jbi.PARK_PULSES))
            if i == calls[-1]:
                self.assertEqual(lines[i + 2:], ["END"])
            else:
                self.assertEqual(lines[i + 2:i + 4],
                                 ["TIMER T=35.00", 'MSG "READ PRE WELD TEMP THEN START"'])

    def test_final_stop_has_only_readings_measure_pause_and_clean_after_park(self):
        for readings in ((), (20.0, 40.0)):
            for pause in (False, True):
                for clean in (False, True):
                    with self.subTest(readings=readings, pause=pause, clean=clean):
                        measure = MeasureStop(2, readings_s=readings, pause=pause, clean=clean)
                        steps = build_plan(self.tp, SOLVE, measure=measure, interpass_timer_s=1000)
                        final = next(i for i, step in enumerate(steps) if step.measurement_final)
                        self.assertEqual((steps[final].role, steps[final].layer), ("retreat", 3))
                        self.assertEqual(steps[final + 1].role, "park")
                        controls = steps[final + 2:]
                        self.assertFalse(any(step.role in ("interpass", "pre_weld", "approach",
                                                          "hover", "descend") for step in controls))
                        self.assertEqual([step.seconds for step in controls if step.kind == "dwell"],
                                         [20.0, 20.0, READING_HOLD_S] if readings else [])
                        self.assertEqual(sum(step.kind == "pause" for step in controls), int(pause))
                        self.assertEqual(sum(step.kind == "call" for step in controls), int(clean))
                        self.assertEqual(sum(step.is_motion for step in controls), int(clean))
                        if clean:
                            self.assertEqual([step.role for step in controls[-2:]], ["clean", "park"])

    def test_terminal_cadence_for_single_layer_and_continuous_weld(self):
        for tp, every, expected in ((wall(60.0, 1, 2.0), 1, [0]),
                                    (wall(60.0, 1, 2.0), 2, []),
                                    (self.tp, 3, []),
                                    (continuous(layers=2), 2, [1])):
            with self.subTest(every=every, layers=len(tp.layers())):
                steps = build_plan(tp, SOLVE, measure=MeasureStop(every))
                self.assertEqual([step.layer for step in steps if step.measurement_final], expected)
                if expected:
                    final = next(i for i, step in enumerate(steps) if step.measurement_final)
                    self.assertTrue(any(step.kind == "arc_off" for step in steps[:final]))

    def test_comment_and_message_limits_include_fractional_readings(self):
        for measure in (MeasureStop(1), MeasureStop(123456789012345678901234567890),
                        MeasureStop(1, readings_s=(1.25, 20.5, 3600.0))):
            with self.subTest(measure=measure):
                text = self.render(measure=measure)
                for line in text.splitlines():
                    if line.startswith("'"):
                        self.assertLessEqual(len(line), 32)
                for message in re.findall(r'^MSG "([^"]*)"', text, re.MULTILINE):
                    self.assertRegex(message, r"^[A-Z0-9 ]{0,32}$")
        self.assertIn("' MEASURE STOP EVERY 1 LAYERS\r\n", self.render(measure=MeasureStop(1)))


class TestMeasureStopSafety(MeasurementCase):
    def test_no_controls_between_arc_on_and_off(self):
        for smooth in (False, True):
            text = self.render(measure=MeasureStop(1, clean=True), smooth=smooth)
            lit = False
            for line in instructions(text):
                if line.startswith(("ARCON", "ARCOF")):
                    lit = line.startswith("ARCON")
                elif lit:
                    self.assertFalse(line.startswith(("TIMER", "MSG", "PAUSE", "CALL")), line)
            self.assertFalse(lit)

    def test_lit_layer_change_refuses_a_due_stop(self):
        with self.assertRaisesRegex(ValueError, "arc lit"):
            build_plan(continuous(), SOLVE, measure=MeasureStop(1))
        with self.assertRaisesRegex(ValueError, "arc lit"):
            build_plan(continuous(), SOLVE,
                       measure=MeasureStop(1, readings_s=(), pause=False, pre_weld=False))
        with self.assertRaises(EmitRefused) as ctx:
            self.checked(tp=continuous(), allow_no_interpass=True, measure=MeasureStop(1))
        self.assertEqual(ctx.exception.reason, "argument")
        self.assertIn("arc lit", str(ctx.exception))

    def test_cadence_counts_lit_changes_but_only_stops_at_real_boundaries(self):
        steps = build_plan(continuous(layers=3, travel_after=2), SOLVE, measure=MeasureStop(2))
        self.assertEqual([s.layer for s in steps if s.role == "measure_pause"], [2])
        with self.assertRaisesRegex(ValueError, "arc lit"):
            build_plan(continuous(layers=3, travel_after=2), SOLVE, measure=MeasureStop(1))

    def test_invented_retreats_and_returns_pass_the_guard(self):
        guard = mock.Mock()
        steps = build_plan(self.tp, SOLVE, measure=MeasureStop(1), guard=guard)
        guarded = [call.args[0] for call in guard.call_args_list]
        for step in steps:
            if step.is_motion and step.move_index is None and step.xyz is not None:
                self.assertIn(step.xyz, guarded, step.label)
        retreats = [s for s in steps if s.role == "retreat"]
        self.assertEqual(len(retreats), 4)
        forbidden = retreats[0].xyz

        def refuse(xyz, where):
            if xyz == forbidden:
                raise ValueError("measurement retreat blocked")

        with self.assertRaisesRegex(ValueError, "measurement retreat blocked"):
            build_plan(self.tp, SOLVE, measure=MeasureStop(1), guard=refuse)

    def test_ros_refuses_call_and_pause_in_a_plan(self):
        real = ros2.build_plan
        for kind, role in (("call", "clean"), ("pause", "measure_pause")):
            with self.subTest(kind=kind):
                steps = real(self.tp, SOLVE)
                steps.insert(2, Step(kind, role, label="CLEAN_R1" if kind == "call" else ""))
                with mock.patch.object(ros2, "build_plan", return_value=steps), \
                        self.assertRaisesRegex(ros2.Ros2Refused, rf"'{kind}'.*MotoROS2 cannot call jobs"):
                    ros2.build_ros2_job(self.tp, SOLVE, name="MEASURE")

    def test_ros_emit_forwards_measure_and_refuses_it(self):
        for measure in (MeasureStop(1), MeasureStop(1, readings_s=(), pause=False,
                                                  pre_weld=False, clean=True)):
            with self.subTest(measure=measure):
                with self.assertRaises(EmitRefused) as ctx:
                    self.checked(ros=True, measure=measure)
                self.assertIn("MotoROS2 cannot call jobs", str(ctx.exception))


class TestMeasureStopSummary(MeasurementCase):
    def test_summary_lists_actual_layers_options_and_approximate_park_time(self):
        measure = MeasureStop(1, readings_s=(10.0, 20.0), pause=False, clean=True)
        text, summary = self.checked(measure=measure)
        entries = summary["measure_stops"]
        self.assertEqual(summary["layers"], [0, 3])
        self.assertEqual([entry["after_layer"] for entry in entries], [0, 1, 2, 3])
        for entry in entries:
            fields = {"after_layer", "readings_s", "pause", "clean", "pre_weld", "park_travel_s_est"}
            if entry is entries[-1]:
                fields.add("final")
                self.assertIs(entry["final"], True)
            self.assertEqual(set(entry), fields)
            self.assertEqual(entry["readings_s"], [10.0, 20.0])
            self.assertEqual((entry["pause"], entry["clean"], entry["pre_weld"]),
                             (False, True, entry is not entries[-1]))
            self.assertIsInstance(entry["park_travel_s_est"], float)
            self.assertTrue(math.isfinite(entry["park_travel_s_est"]))
            self.assertGreater(entry["park_travel_s_est"], 0.0)
        self.assertEqual(text.count("CALL JOB:CLEAN_R1"), 4)
        _, slower = self.checked(measure=measure, air_v="25.0")
        _, faster = self.checked(measure=measure, air_v="50.0")
        for slow, fast in zip(slower["measure_stops"], faster["measure_stops"]):
            self.assertAlmostEqual(slow["park_travel_s_est"], 2 * fast["park_travel_s_est"], places=4)

    def test_summary_cadence_and_cycle_forwarding(self):
        _, summary = self.checked(measure=MeasureStop(2))
        self.assertEqual([entry["after_layer"] for entry in summary["measure_stops"]], [1, 3])
        text, summary = self.checked(cycle=True, measure=MeasureStop(2))
        self.assertEqual([entry["after_layer"] for entry in summary["measure_stops"]], [1, 3])
        self.assertTrue(summary["measure_stops"][-1]["final"])
        self.assertEqual(text.count('MSG "MEASURE BEAD THEN PRESS START"'), 2)
        _, summary = self.checked()
        self.assertEqual(summary.get("measure_stops", []), [])

    def test_summary_full_range_single_layer_and_selected_range(self):
        _, summary = self.checked(measure=MeasureStop(3))
        self.assertEqual(summary["layers"], [0, 3])
        self.assertEqual([entry["after_layer"] for entry in summary["measure_stops"]], [2])
        self.assertNotIn("final", summary["measure_stops"][0])
        _, summary = self.checked(tp=wall(60.0, 1, 2.0), measure=MeasureStop(1))
        self.assertEqual(summary["layers"], [0, 0])
        self.assertEqual(len(summary["measure_stops"]), 1)
        self.assertEqual(summary["measure_stops"][0]["after_layer"], 0)
        self.assertTrue(summary["measure_stops"][0]["final"])
        _, summary = self.checked(layers=(2, 3), measure=MeasureStop(2))
        self.assertEqual(summary["layers"], [2, 3])
        self.assertEqual(len(summary["measure_stops"]), 1)
        self.assertEqual(summary["measure_stops"][0]["after_layer"], 3)
        self.assertTrue(summary["measure_stops"][0]["final"])

    def test_pass_table_counts_timers_pauses_and_only_moves_as_steps(self):
        text = self.render(measure=MeasureStop(1, clean=True), interpass_timer_s=1000.0)
        rows = passes.pass_table(self.tp, text=text, v_unit_mm_per_min=60.0)
        self.assertEqual([row["dwell_s"] for row in rows], [2.0, 1000.0, 1000.0, 1000.0])
        self.assertEqual([row["pauses_before"] for row in rows], [0, 2, 2, 2])
        body = instructions(text)
        moves = [i for i, line in enumerate(body) if line.startswith(("MOVJ ", "MOVL "))]
        for row in rows:
            self.assertTrue(body[moves[row["first_step"] - 1] - 1].startswith("ARCON ASF#("))
            self.assertTrue(body[moves[row["last_step"] - 1] + 1].startswith("ARCOF AEF#("))
        short = self.render(measure=MeasureStop(1), interpass_timer_s=30.0)
        short_rows = passes.pass_table(self.tp, text=short, v_unit_mm_per_min=60.0)
        self.assertEqual([row["dwell_s"] for row in short_rows], [2.0, 65.0, 65.0, 65.0])


if __name__ == "__main__":
    unittest.main()
