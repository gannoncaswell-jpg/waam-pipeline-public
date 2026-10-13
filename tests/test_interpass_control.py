import math
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams, cell_lc, demos, passes, server
from waam_pipeline.backends import jbi
from waam_pipeline.backends.jbi import emit_jbi_pulse, timer_lines
from waam_pipeline.emit import EmitRefused, _interpass_gate
from waam_pipeline.interpass import cooling_pairs, fit_cooling, propose_dwell
from waam_pipeline.plan import bead_dwell_note, bead_gaps, build_plan
from waam_pipeline.toolpath import Move, Toolpath
from waam_pipeline.slicer import wall
from waam_pipeline.studio import _filler_options
from waam_pipeline.studio_layout import STUDIO_SHELL

SOLVE = cell_lc.pulse_solver(np.array(cell_lc.DEFAULT_PART_ORIGIN, float))
JOB = dict(name="T", date="2026/10/05 12:00", arc_file=81)


def setUpModule():
    provenance = mock.patch.object(jbi, "_git_sha", return_value="test")
    provenance.start()
    unittest.addModuleCleanup(provenance.stop)


def lateral_layer():


    m = [Move(0, 0, 2, None, False, 0, 1), Move(60, 0, 2, 300, True, 0, 2),
         Move(60, 7, 2, None, False, 0, 3), Move(0, 7, 2, 300, True, 0, 4),
         Move(0, 14, 5, None, False, 0, 5), Move(0, 14, 2, 300, True, 0, 6),
         Move(60, 14, 2, 300, True, 0, 7),
         Move(60, 14, 12, None, False, 0, 8), Move(60, 21, 12, None, False, 0, 9),
         Move(60, 21, 2, None, False, 0, 10), Move(0, 21, 2, 300, True, 0, 11)]
    return Toolpath(m)


def timers(text):
    return [float(v) for v in re.findall(r"^TIMER T=(\d+\.\d\d)\r?$", text, re.MULTILINE)]


class TestTimerSplit(unittest.TestCase):
    def test_split_is_exact_and_each_piece_is_a_legal_timer(self):
        cases = {2: [2.0], 655.35: [655.35], 655.36: [327.68, 327.68], 700: [350.0, 350.0],
                 1310.7: [655.35, 655.35], 1310.71: [436.91, 436.9, 436.9], 3600: [600.0] * 6,
                 1234.567: [617.29, 617.28]}
        for seconds, pieces in cases.items():
            with self.subTest(seconds=seconds):
                lines = timer_lines(seconds)
                self.assertEqual([float(line.split("=")[1]) for line in lines], pieces)
                self.assertTrue(all(re.fullmatch(r"TIMER T=\d+\.\d\d", line) for line in lines))
                self.assertTrue(all(0.01 <= p <= 655.35 for p in pieces))
                self.assertEqual(round(sum(p * 100 for p in pieces)), round(seconds * 100))

    def test_out_of_range_dwells_are_refused(self):
        for seconds in (0, 0.009, 3600.01, float("nan"), float("inf"), True, "30"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                timer_lines(seconds)

    def test_long_interpass_renders_as_consecutive_timers(self):
        tp = wall(60.0, 3, 2.0)
        text = emit_jbi_pulse(tp, SOLVE, interpass_timer_s=1000.0, **JOB)
        self.assertEqual(timers(text), [2.0] + [500.0, 500.0] * 2)
        self.assertEqual(text.count("TIMER T=500.00\r\nTIMER T=500.00\r\n"), 2)

    def test_short_dwell_output_is_unchanged(self):
        tp = wall(60.0, 3, 2.0)
        self.assertEqual(emit_jbi_pulse(tp, SOLVE, interpass_timer_s=30.0, **JOB),
                         emit_jbi_pulse(tp, SOLVE, interpass_timer_s=30.0, temp_check_every=0,
                                        bead_dwell_s=0.0, **JOB))
        self.assertEqual(timers(emit_jbi_pulse(tp, SOLVE, **JOB)), [2.0, 30.0, 30.0])

    def test_gate_caps_one_dwell_at_an_hour(self):
        tp = wall(60.0, 3, 2.0)
        _interpass_gate(tp, 3600.0, False, 1)
        with self.assertRaisesRegex(EmitRefused, "an hour"):
            _interpass_gate(tp, 3600.5, False, 1)

    def test_cycle_tail_splits_its_wait(self):
        from waam_pipeline.layer_sense import auto_plan

        path = wall(100, 4, 2)
        origin = cell_lc.snap_toolpath_to_platter(path, 1014, -100)
        plan, hop = auto_plan(path, 0, 1, step_mm=2, spacing_mm=20)
        text = jbi.emit_cycle_jbi(
            path.layer_range(0, 1), cell_lc.pulse_solver(origin), name="CYCLE_C01",
            date="2026/10/02 12:00", arc_file=12,
            cycle={"cycle": 1, "first": 0, "last": 1, "step_mm": 2},
            tail={"plan": plan, "hop_mm": hop, "interpass_s": 900.0})
        self.assertIn('MSG "READ TEMP 1 THEN PRESS START"\r\nPAUSE\r\nMSG ""\r\n'
                      "TIMER T=450.00\r\nTIMER T=450.00\r\n"
                      'MSG "READ TEMP 2 THEN PRESS START"', text)


class TestTemperatureChecks(unittest.TestCase):
    PAUSE = 'MSG "READ TEMP THEN PRESS START"\r\nPAUSE\r\nMSG ""\r\n'

    def test_off_by_default(self):
        self.assertNotIn("PAUSE", emit_jbi_pulse(wall(60.0, 6, 2.0), SOLVE, **JOB))

    def test_every_n_layers_after_the_dwell_at_the_hover(self):
        tp = wall(60.0, 6, 2.0)
        steps = build_plan(tp, SOLVE, temp_check_every=2)
        checks = [i for i, s in enumerate(steps) if s.kind == "pause"]
        self.assertEqual([steps[i].layer for i in checks], [2, 4])
        for i in checks:
            self.assertEqual((steps[i - 2].role, steps[i - 1].role), ("hover", "interpass"))
        text = emit_jbi_pulse(tp, SOLVE, temp_check_every=2, **JOB)
        self.assertEqual(text.count(self.PAUSE), 2)
        self.assertEqual(text.count("TIMER T=30.00\r\n" + self.PAUSE), 2)
        for message in re.findall(r'^MSG "([^"]*)"', text, re.MULTILINE):
            self.assertRegex(message, r"^[A-Z0-9 ]{0,32}$")

    def test_bad_cadence_is_refused(self):
        for bad in (-1, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_plan(wall(60.0, 2, 2.0), SOLVE, temp_check_every=bad)


class TestBetweenBeadDwell(unittest.TestCase):
    def setUp(self):
        self.tp = demos.demo_wall(80.0, 2, 2.9, thickness_mm=15.875, bead_width_mm=8.6)

    def test_dwell_at_the_lift_between_beads_of_one_layer_only(self):
        beads = arcparams.wall_beads(15.875, 8.6, 2.9).count
        steps = build_plan(self.tp, SOLVE, bead_dwell_s=45.0)
        found = [i for i, s in enumerate(steps) if s.role == "bead_dwell"]
        self.assertEqual(len(found), 2 * (beads - 1))
        for i in found:
            lift = steps[i - 1]
            move = self.tp.moves[lift.move_index]
            self.assertEqual((lift.role, steps[i - 2].kind), ("air", "arc_off"))
            self.assertFalse(move.deposit)
            self.assertAlmostEqual(move.z - self.tp.moves[lift.move_index - 1].z, demos.BEAD_LIFT_MM)
        text = emit_jbi_pulse(self.tp, SOLVE, bead_dwell_s=700.0, **JOB)
        self.assertEqual(timers(text), [2.0] + [350.0, 350.0] * (beads - 1) + [30.0]
                         + [350.0, 350.0] * (beads - 1))

    def test_wall_lift_gets_the_dwell_and_no_note(self):
        lifted, skipped = bead_gaps(self.tp)
        self.assertEqual((len(lifted), skipped), (2 * (arcparams.wall_beads(15.875, 8.6, 2.9).count - 1), 0))
        self.assertIsNone(bead_dwell_note(self.tp, 45.0))
        self.assertNotIn("BEAD DWELL SKIP", emit_jbi_pulse(self.tp, SOLVE, bead_dwell_s=45.0, **JOB))

    def test_lateral_travel_gets_no_dwell_and_one_note(self):
        tp = lateral_layer()
        self.assertEqual(bead_gaps(tp), ({7}, 2))
        steps = build_plan(tp, SOLVE, bead_dwell_s=15.0)
        dwell = [i for i, s in enumerate(steps) if s.role == "bead_dwell"]
        self.assertEqual(len(dwell), 1)
        self.assertEqual(steps[dwell[0] - 1].move_index, 7)
        note = bead_dwell_note(tp, 15.0)
        self.assertEqual(note, "between-bead dwell skipped at 2 gaps: the travel after the pass "
                               "does not lift 5 mm")
        self.assertIsNone(bead_dwell_note(tp, 0.0))
        text = emit_jbi_pulse(tp, SOLVE, bead_dwell_s=15.0, **JOB)
        self.assertEqual(text.count("' BEAD DWELL SKIP 2: NO LIFT\r\n"), 1)
        self.assertEqual(timers(text), [2.0, 15.0])
        self.assertNotIn("BEAD DWELL SKIP", emit_jbi_pulse(tp, SOLVE, **JOB))

    def test_emit_summary_carries_the_note(self):
        from unittest import mock

        from waam_pipeline import emit, weld_ctwd
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState, emit_job

        tp = lateral_layer()
        origin = cell_lc.snap_origin_to_platter(1014.0, -180.0, substrate_mm=6.35, first_deposit_z_mm=2.0)
        kwargs = dict(name="LATERAL", arc_file=81, scene=cell_lc.lc_scene(origin, training_block=False),
                      torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.0, bead_width_mm=6.0,
                      platter=PlatterState(clear=True, attested_by="t"), date="2026/10/05 12:00")


        default = {"weld_ctwd_mm": weld_ctwd.default_mm(), "weld_ctwd_delta_mm": 0.0, "weld_ctwd_notes": []}
        with mock.patch.object(emit, "_gate", return_value=(tp, [], default)):
            _, summary = emit_job(tp, cell_lc.pulse_solver(origin), bead_dwell_s=15.0, **kwargs)
            self.assertEqual(summary["notes"], [bead_dwell_note(tp, 15.0)])
            _, summary = emit_job(tp, cell_lc.pulse_solver(origin), **kwargs)
            self.assertNotIn("notes", summary)

    def test_default_off_and_single_bead_walls_get_none(self):
        self.assertFalse(any(s.role == "bead_dwell" for s in build_plan(self.tp, SOLVE)))
        self.assertFalse(any(s.role == "bead_dwell"
                             for s in build_plan(wall(60.0, 3, 2.0), SOLVE, bead_dwell_s=30.0)))
        bead_set = demos.demo_bead(60.0, 2.0, 325.0, count=3, pitch_mm=25.0)
        self.assertFalse(any(s.role == "bead_dwell" for s in build_plan(bead_set, SOLVE, bead_dwell_s=30.0)))

    def test_bad_values_are_refused(self):
        for bad in (-1.0, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_plan(self.tp, SOLVE, bead_dwell_s=bad)
        with self.assertRaises(ValueError):
            emit_jbi_pulse(self.tp, SOLVE, bead_dwell_s=3601.0, **JOB)


def arc_windows(steps):

    lit, bad = False, []
    for s in steps:
        if s.kind in ("arc_on", "arc_off"):
            lit = s.kind == "arc_on"
        elif lit and s.kind in ("dwell", "pause"):
            bad.append(s)
    return bad


class TestPassTable(unittest.TestCase):
    def test_dwell_before_and_steps_with_every_interpass_feature(self):
        tp = demos.demo_wall(80.0, 2, 2.9, thickness_mm=15.875, bead_width_mm=8.6)
        beads = arcparams.wall_beads(15.875, 8.6, 2.9).count
        text = emit_jbi_pulse(tp, SOLVE, interpass_timer_s=1000.0, temp_check_every=1,
                              bead_dwell_s=700.0, **JOB)
        rows = passes.pass_table(tp, text=text, v_unit_mm_per_min=60.0)
        self.assertEqual(len(rows), 2 * beads)
        dwell = [2.0] + [700.0] * (beads - 1) + [1000.0] + [700.0] * (beads - 1)
        self.assertEqual([r["dwell_s"] for r in rows], dwell)
        self.assertEqual([r["pauses_before"] for r in rows],
                         [0] * beads + [1] + [0] * (beads - 1))

        body = text.split("NOP\r\n", 1)[1].splitlines()
        moves = [i for i, ln in enumerate(body) if ln.split(" ", 1)[0] in ("MOVJ", "MOVL")]
        for r in rows:
            self.assertTrue(body[moves[r["first_step"] - 1] - 1].startswith("ARCON ASF#("))
            self.assertTrue(body[moves[r["last_step"] - 1] + 1].startswith("ARCOF AEF#("))


class TestArcOffOnly(unittest.TestCase):


    def plans(self):
        thick = demos.demo_wall(80.0, 3, 2.9, thickness_mm=15.875, bead_width_mm=8.6)
        kw = dict(temp_check_every=1, bead_dwell_s=700.0, interpass_timer_s=1000.0)
        yield "wall", wall(60.0, 4, 2.0), kw
        yield "thick wall", thick, kw
        yield "thick wall smooth", thick, dict(kw, smooth=True)
        yield "bead set", demos.demo_bead(60.0, 2.0, 325.0, count=3, pitch_mm=25.0), kw
        yield "lateral layer", lateral_layer(), kw
        yield "cylinder", demos.demo_cylinder(radius_mm=30.0, layers=3), dict(kw, smooth=True)

    def test_no_dwell_or_pause_under_a_lit_arc(self):
        for label, tp, kw in self.plans():
            with self.subTest(plan=label):
                steps = build_plan(tp, SOLVE, **kw)
                self.assertEqual(any(s.kind == "pause" for s in steps), len(tp.layers()) > 1)
                self.assertEqual(arc_windows(steps), [])
                text = emit_jbi_pulse(tp, SOLVE, **JOB, **kw)
                lit = False
                for line in text.split("\r\n"):
                    if line.startswith(("ARCON", "ARCOF")):
                        lit = line.startswith("ARCON")
                    elif lit:
                        self.assertFalse(line.startswith(("TIMER", "MSG", "PAUSE")), (label, line))

    def test_a_temperature_check_never_pauses_a_lit_arc(self):

        tp = Toolpath([Move(0, 0, 2, None, False, 0, 1), Move(60, 0, 2, 300, True, 0, 2),
                       Move(60, 0, 4, 300, True, 1, 3), Move(0, 0, 4, 300, True, 1, 4)])
        try:
            steps = build_plan(tp, SOLVE, temp_check_every=1)
        except ValueError as exc:
            self.assertIn("arc lit", str(exc))
        else:
            self.assertFalse([s for s in arc_windows(steps) if s.kind == "pause"])

    def test_ros2_refuses_a_step_it_cannot_render(self):
        from unittest import mock

        from waam_pipeline.backends import ros2
        from waam_pipeline.plan import Step

        tp = wall(60.0, 2, 2.0)
        real = ros2.build_plan

        def with_pause(*args, **kwargs):
            steps = real(*args, **kwargs)
            return steps[:2] + [Step("pause", "temp_check", layer=1)] + steps[2:]

        with mock.patch.object(ros2, "build_plan", with_pause), \
                self.assertRaisesRegex(ros2.Ros2Refused, "'pause'.*no ROS 2 rendering"):
            ros2.build_ros2_job(tp, SOLVE, name="P")
        self.assertIsNotNone(ros2.build_ros2_job(tp, SOLVE, name="P"))


def continuous(layers=3, length=60.0, h=2.0, travel_after=None):


    m, x, line = [Move(0, 0, h, None, False, 0, 1)], 0.0, 2
    for k in range(layers):
        z = h * (k + 1)
        if k == travel_after:
            m += [Move(x, 0, z + 10, None, False, k, line), Move(x, 0, z, None, False, k, line + 1)]
            line += 2
        elif k:
            m.append(Move(x, 0, z, 300.0, True, k, line))
            line += 1
        x = length if x == 0.0 else 0.0
        m.append(Move(x, 0, z, 300.0, True, k, line))
        line += 1
    m.append(Move(x, 0, z + 10, None, False, layers - 1, line))
    return Toolpath(m)


class TestLitLayerChanges(unittest.TestCase):


    def emit(self, tp, **kw):
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState, emit_job

        origin = cell_lc.snap_origin_to_platter(1014.0, -180.0, substrate_mm=6.35, first_deposit_z_mm=2.0)
        return emit_job(tp, cell_lc.pulse_solver(origin), name="CONT", arc_file=81,
                        scene=cell_lc.lc_scene(origin, training_block=False),
                        torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.0, bead_width_mm=6.0,
                        platter=PlatterState(clear=True, attested_by="t"), date="2026/10/05 12:00", **kw)

    def test_continuous_part_without_checks_emits_with_nothing_in_the_arc(self):
        text, _ = self.emit(continuous(), allow_no_interpass=True)
        self.assertEqual(text.count("ARCON"), 1)
        inside = text.split("ARCON", 1)[1].split("ARCOF", 1)[0]
        for token in ("TIMER", "MSG", "PAUSE"):
            self.assertNotIn(token, inside)

    def test_continuous_part_with_a_check_is_refused(self):
        with self.assertRaises(EmitRefused) as ctx:
            self.emit(continuous(), allow_no_interpass=True, temp_check_every=1)
        self.assertEqual(ctx.exception.reason, "argument")
        self.assertIn("temperature check due there cannot pause", str(ctx.exception))
        with self.assertRaises(EmitRefused) as ctx:
            self.emit(continuous())
        self.assertEqual(ctx.exception.reason, "interpass")

    def test_check_cadence_counts_every_layer_change(self):
        tp = continuous(layers=3, travel_after=2)
        steps = build_plan(tp, SOLVE, temp_check_every=2)
        self.assertEqual([s.layer for s in steps if s.kind == "pause"], [2])
        self.assertEqual(arc_windows(steps), [])
        with self.assertRaisesRegex(ValueError, "layer 0 runs into layer 1 with the arc lit"):
            build_plan(tp, SOLVE, temp_check_every=1)
        self.assertFalse(any(s.kind == "pause" for s in build_plan(tp, SOLVE)))


class TestFillerSetpoints(unittest.TestCase):
    def test_literature_defaults(self):
        expected = {"er70s6": 150.0, "er308l": 100.0, "er316l": 100.0}
        for key, filler in arcparams.FILLERS.items():
            with self.subTest(key=key):
                self.assertEqual(filler.interpass_max_c, expected.get(key))
        options = _filler_options()
        self.assertIn('value="er70s6" selected data-interpass-c="150"', options)
        self.assertIn('value="er316l" data-interpass-c="100"', options)
        self.assertNotIn('value="er5356" data-interpass-c', options)


def pairs_for(tau, rows, ambient=25.0):
    return [{"before_c": b, "after_c": ambient + (b - ambient) * math.exp(-t / tau), "seconds": t}
            for b, t in rows]


class TestCoolingFit(unittest.TestCase):
    def test_recovers_tau_from_exact_readings(self):
        fit = fit_cooling(pairs_for(200.0, [(300, 60), (350, 90), (400, 120)]))
        self.assertAlmostEqual(fit["tau_s"], 200.0, places=6)
        self.assertLess(fit["rms_c"], 1e-9)
        self.assertTrue(fit["valid"])
        self.assertEqual(fit["notes"], [])

    def test_least_squares_in_log_space(self):
        pairs = [{"before_c": 300, "after_c": 200, "seconds": 60},
                 {"before_c": 320, "after_c": 180, "seconds": 120}]
        fit = fit_cooling(pairs, ambient_c=20.0)
        t = np.array([60, 120.0])
        y = -np.log(np.array([180.0, 160.0]) / np.array([280.0, 300.0]))
        self.assertAlmostEqual(fit["tau_s"], float((t @ t) / (t @ y)))
        for p in fit["used"]:
            self.assertAlmostEqual(p["residual_c"], p["after_c"] - p["predicted_after_c"])

    def test_refuses_until_two_usable_pairs(self):
        good = pairs_for(200.0, [(300, 60)])
        bad = [{"before_c": 200, "after_c": 210, "seconds": 60},
               {"before_c": 200, "after_c": 28, "seconds": 600},
               {"before_c": 200, "after_c": 150, "seconds": 0},
               {"before_c": float("nan"), "after_c": 150, "seconds": 60}]
        with self.assertRaisesRegex(ValueError, "at least 2.*1 usable"):
            fit_cooling(good + bad)
        fit = fit_cooling(good + bad + pairs_for(200.0, [(350, 90)]))
        self.assertEqual(len(fit["used"]), 2)
        self.assertEqual([p["reason"] for p in fit["skipped"]],
                         ["did not cool", "T-after within 5 C of ambient", "no dwell", "not finite numbers"])
        with self.assertRaises(ValueError):
            fit_cooling(pairs_for(200.0, [(300, 60), (350, 90)]), ambient_c=200.0)

    def test_inconsistent_readings_are_flagged_not_refused(self):
        fit = fit_cooling(pairs_for(100.0, [(300, 60)]) + pairs_for(400.0, [(300, 60)]))
        self.assertFalse(fit["valid"])
        self.assertTrue(any("tau varies" in note for note in fit["notes"]))

    def test_proposal_is_the_conservative_clamped_dwell(self):
        fit = fit_cooling(pairs_for(200.0, [(300, 60), (350, 90)]))
        p = propose_dwell(fit, start_c=400.0, setpoint_c=150.0, after_c=200.0)
        self.assertEqual(p["seconds"], math.ceil(200.0 * math.log(375 / 125)))
        self.assertEqual(p["hold_s"], math.ceil(200.0 * math.log(175 / 125)))
        self.assertEqual(propose_dwell(fit, start_c=140.0, setpoint_c=150.0)["seconds"], 1.0)
        long = propose_dwell(dict(fit, tau_s=1000.0), start_c=1400.0, setpoint_c=31.0)
        self.assertEqual(long["seconds"], 3600.0)
        self.assertTrue(any("clamped" in note for note in long["notes"]))
        with self.assertRaisesRegex(ValueError, "above ambient"):
            propose_dwell(fit, start_c=400.0, setpoint_c=27.0)
        slower = dict(fit, tau_last_s=300.0)
        self.assertEqual(propose_dwell(slower, start_c=400.0, setpoint_c=150.0)["tau_used_s"], 300.0)

    def test_pairs_come_from_the_run_cycles(self):
        run = {"cycles": [
            {"cycle": 1, "interpass_s": 60.0, "t_before": {"value_c": 300.0}, "t_after": {"value_c": 200.0}},
            {"cycle": 2, "interpass_s": 90.0},
            {"cycle": 3, "interpass_s": 30.0, "last_cycle": True}],
            "temperatures": [
                {"cycle": 2, "stage": "before-interpass", "value_c": 320.0},
                {"cycle": 2, "stage": "after-interpass", "value_c": 190.0}]}
        self.assertEqual(cooling_pairs(run), [
            {"cycle": 1, "before_c": 300.0, "after_c": 200.0, "seconds": 60.0},
            {"cycle": 2, "before_c": 320.0, "after_c": 190.0, "seconds": 90.0}])

    def test_server_proposal_converts_units(self):
        run = {"cycles": [dict(cycle=i + 1, interpass_s=p["seconds"], t_before={"value_c": p["before_c"]},
                               t_after={"value_c": p["after_c"]})
                          for i, p in enumerate(pairs_for(200.0, [(300, 60), (350, 90)]))]}
        out = server._cooling_proposal(run, {"setpoint": ["302"], "unit": ["F"], "ambient": ["77"]})
        self.assertAlmostEqual(out["fit"]["tau_s"], 200.0, places=6)
        self.assertAlmostEqual(out["proposal"]["setpoint_c"], 150.0)
        with self.assertRaisesRegex(ValueError, "at least 2"):
            server._cooling_proposal(None, {"setpoint": ["150"], "unit": ["C"]})
        for blank in ({}, {"setpoint": [" "]}):
            with self.assertRaisesRegex(ValueError, "Enter a max interpass temperature"):
                server._cooling_proposal(run, dict(blank, unit=["C"]))
        with self.assertRaisesRegex(ValueError, "Max interpass setpoint"):
            server._cooling_proposal(run, {"setpoint": ["hot"], "unit": ["C"]})
        self.assertEqual(server._dwell_extras_from_query({"bead_dwell": ["0.01"]})[0]["bead_dwell_s"], 0.01)


class TestQueryParsing(unittest.TestCase):
    def test_dwell_extras(self):
        self.assertEqual(server._dwell_extras_from_query({}), ({"temp_check_every": 0, "bead_dwell_s": 0.0}, None))
        self.assertEqual(server._dwell_extras_from_query({"temp_check_every": ["3"], "bead_dwell": ["700"]}),
                         ({"temp_check_every": 3, "bead_dwell_s": 700.0}, None))
        for query in ({"temp_check_every": ["1.5"]}, {"temp_check_every": ["201"]},
                      {"bead_dwell": ["3601"]}, {"bead_dwell": ["-1"]}, {"bead_dwell": ["x"]},
                      {"bead_dwell": ["0.004"]}, {"bead_dwell": ["0.009"]}):
            with self.subTest(query=query):
                self.assertIsNone(server._dwell_extras_from_query(query)[0])

    def test_interpass_accepts_an_hour(self):
        self.assertEqual(server._interpass_from_query({"interpass": ["3600"]}), ((3600.0, 1), None))
        self.assertIsNone(server._interpass_from_query({"interpass": ["3600.5"]})[0])


class TestMeasureQueryParsing(unittest.TestCase):
    def test_disabled_and_default_options(self):
        for query in ({}, {"measure_every": ["0"]}, {"measure_every": [""]}):
            with self.subTest(query=query):
                self.assertEqual(server._measure_from_query(query), (None, None))
        measure, error = server._measure_from_query({"measure_every": ["1"]})
        self.assertIsNone(error)
        self.assertEqual((measure.every, measure.readings_s, measure.pause, measure.clean, measure.pre_weld),
                         (1, (20, 40, 60), True, False, True))

    def test_custom_times_flags_and_upper_cadence(self):
        query = {"measure_every": ["1000"], "measure_times": ["1, 20.5 3600"],
                 "measure_pause": ["0"], "measure_clean": ["1"], "measure_pre_weld": ["0"]}
        measure, error = server._measure_from_query(query)
        self.assertIsNone(error)
        self.assertEqual((measure.every, measure.readings_s, measure.pause, measure.clean, measure.pre_weld),
                         (1000, (1, 20.5, 3600), False, True, False))
        measure, error = server._measure_from_query({"measure_every": ["1"], "measure_times": [""]})
        self.assertIsNone(error)
        self.assertEqual(measure.readings_s, ())
        measure, error = server._measure_from_query(
            {"measure_every": ["1"], "measure_times": ["1 2 3 4 5 6 7 8 9 10"]})
        self.assertIsNone(error)
        self.assertEqual(measure.readings_s, tuple(range(1, 11)))

    def test_bad_cadence_times_and_flags_are_refused(self):
        bad = [{"measure_every": [value]} for value in ("-1", "1001", "1.5", "nan", "x")]
        bad += [{"measure_times": [value]} for value in
                ("0", "1.005", "3600.1", "20 20", "40,20", "nan", "inf", "20 seconds", "1 2 3 4 5 6 7 8 9 10 11")]
        bad += [{key: [value]} for key in ("measure_pause", "measure_clean", "measure_pre_weld")
                for value in ("", "2", "true", "false")]
        for query in bad:
            with self.subTest(query=query):
                measure, error = server._measure_from_query({"measure_every": ["1"], **query})
                self.assertIsNone(measure)
                self.assertIsInstance(error, str)
                self.assertTrue(error.strip())
        measure, error = server._measure_from_query({"measure_every": ["0"], "measure_times": ["40 20"]})
        self.assertIsNone(measure)
        self.assertTrue(error)


class TestMeasureExports(unittest.TestCase):
    def setUp(self):
        from waam_pipeline import weld_ctwd
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState

        self.tp = wall(60.0, 3, 2.0)
        origin = cell_lc.snap_origin_to_platter(1014.0, -180.0, substrate_mm=6.35, first_deposit_z_mm=2.0)
        self.em = {"origin": origin, "scene": cell_lc.lc_scene(origin, training_block=False),
                   "torch": TorchModel.yaskawa_accufast_45(), "bead": 6.0, "bead_height": 2.0,
                   "margin": 5.0, "part": "wall"}
        self.st = {"toolpath": self.tp, "emit": self.em}
        self.handler = server._Handler.__new__(server._Handler)
        for name in ("_json", "_send", "_archive", "_file_run", "_stale_gen"):
            setattr(self.handler, name, mock.Mock())
        self.handler._stale_gen.return_value = False
        self.handler._archive.return_value = "saved"
        self.handler._file_run.return_value = "MEASURE.JBI"
        self.ctwd = weld_ctwd.default_mm()
        self.platter = PlatterState(clear=True, attested_by="test")
        for name, value in (("_state", self.st), ("_controller_job_taken", False),
                            ("_platter_from_query", (self.platter, None)),
                            ("_ctwd_from_query", (self.ctwd, None)), ("_deposit_model", None)):
            patcher = mock.patch.object(server, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.query = {"name": ["MEASURE"], "arc": ["81"], "measure_every": ["1"]}

    def test_jbi_prompts_and_clean_only_when_requested(self):
        for clean in ("0", "1"):
            with self.subTest(clean=clean):
                self.handler._send.reset_mock()
                self.handler._json.reset_mock()
                self.handler._api_jbi("test", dict(self.query, measure_clean=[clean]))
                self.handler._json.assert_not_called()
                self.handler._send.assert_called_once()
                status, body = self.handler._send.call_args.args[:2]
                self.assertEqual(status, 200)
                text = body.decode("ascii")
                self.assertEqual("CALL JOB:CLEAN_R1" in text, clean == "1")
                for seconds in (20, 40, 60):
                    self.assertRegex(text, rf'(?m)^MSG "[^"\r\n]*\b{seconds}\b[^"\r\n]*"\r?$')
                self.assertIn("PAUSE\r\n", text)
                self.assertTrue(self.handler._file_run.call_args.kwargs["summary"]["measure_stops"])

    def test_single_layer_records_its_terminal_measurement(self):
        self.st["toolpath"] = wall(60.0, 1, 2.0)
        self.handler._api_jbi("test", dict(self.query, measure_clean=["1"]))
        self.handler._json.assert_not_called()
        self.assertEqual(self.handler._send.call_args.args[0], 200)
        text = self.handler._send.call_args.args[1].decode("ascii")
        self.assertIn("CALL JOB:CLEAN_R1", text)
        self.assertIn('MSG "TEMP 60 S AFTER WELD"', text)
        stops = self.handler._file_run.call_args.kwargs["summary"]["measure_stops"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["after_layer"], 0)
        self.assertTrue(stops[0]["final"])
        self.assertFalse(stops[0]["pre_weld"])

    def test_bad_measurements_return_400_before_emission(self):
        from waam_pipeline import emit

        for invalid in ({"measure_every": ["1001"]}, {"measure_times": ["20,10"]}, {"measure_times": ["1.005"]},
                        {"measure_clean": ["yes"]}):
            with self.subTest(invalid=invalid), mock.patch.object(emit, "emit_job") as emit_job:
                self.handler._json.reset_mock()
                self.handler._api_jbi("test", {**self.query, **invalid})
                self.assertEqual(self.handler._json.call_args.args[0], 400)
                self.assertTrue(self.handler._json.call_args.args[1]["error"])
                if invalid.get("measure_times") == ["1.005"]:
                    self.assertIn("0.01 s", self.handler._json.call_args.args[1]["error"])
                emit_job.assert_not_called()
                self.handler._send.assert_not_called()

    def test_get_preserves_blank_times_and_refuses_blank_flags(self):
        from waam_pipeline import emit

        for name in ("_guest_gate", "_admin_gate", "_cross_site_post", "_wrong_workspace",
                     "_registration_pending"):
            setattr(self.handler, name, mock.Mock(return_value=False))
        self.handler._valid_run_name = mock.Mock(return_value=True)
        self.handler.path = "/api/jbi?name=MEASURE&arc=81&measure_every=1&measure_times="
        with mock.patch.object(server, "_auth", return_value=("test", None)), \
                mock.patch.object(server, "_log_auth"), \
                mock.patch.object(server, "_same_token", return_value=False), \
                mock.patch.object(emit, "emit_job", wraps=emit.emit_job) as emit_job:
            self.handler.do_GET()
            self.handler._json.assert_not_called()
            self.assertEqual(self.handler._send.call_args.args[0], 200)
            self.assertEqual(emit_job.call_args.kwargs["measure"].readings_s, ())
            emit_job.reset_mock()
            self.handler._send.reset_mock()
            self.handler.path += "&measure_clean="
            self.handler.do_GET()
            self.assertEqual(self.handler._json.call_args.args[0], 400)
            emit_job.assert_not_called()
            self.handler._send.assert_not_called()

    def test_cycle_forwards_measurement_and_records_stops(self):
        from waam_pipeline import emit

        stops = [{"layer": 1, "readings_s": [20, 40, 60]}]
        summary = {"positions": 0, "passes": 0, "weld_ctwd_mm": self.ctwd, "measure_stops": stops}
        self.handler._cycle_build = mock.Mock(return_value=(
            self.st, self.tp, self.em, [], {"next_first": 0, "top_layer": 2, "step_mm": 2.0}))
        with mock.patch.object(emit, "emit_cycle_job", return_value=("NOP\r\nEND\r\n", summary)) as emit_cycle, \
                mock.patch.object(server.runs, "active", return_value=None), \
                mock.patch.object(server.runs, "set_cycle") as set_cycle:
            self.handler._api_cycle_job("test", dict(
                self.query, interpass_every=["3"], measure_clean=["1"], clean_first=["1"]))
        self.handler._json.assert_not_called()
        self.assertEqual(self.handler._send.call_args.args[0], 200)
        measure = emit_cycle.call_args.kwargs["measure"]
        self.assertEqual((measure.every, measure.clean, measure.readings_s), (1, True, (20, 40, 60)))
        self.assertTrue(emit_cycle.call_args.kwargs["clean_first"])
        self.assertEqual(self.handler._file_run.call_args.kwargs["summary"]["measure_stops"], stops)
        self.assertEqual(set_cycle.call_args.args[2]["measure_stops"], stops)

    def test_cycle_refuses_bad_measurement_before_emission(self):
        from waam_pipeline import emit

        self.handler._cycle_build = mock.Mock(return_value=(
            self.st, self.tp, self.em, [], {"next_first": 0, "top_layer": 2, "step_mm": 2.0}))
        for times in ("60,20", "1.005"):
            with self.subTest(times=times), mock.patch.object(emit, "emit_cycle_job") as emit_cycle:
                self.handler._json.reset_mock()
                self.handler._api_cycle_job("test", dict(self.query, measure_times=[times]))
                self.assertEqual(self.handler._json.call_args.args[0], 400)
                self.assertTrue(self.handler._json.call_args.args[1]["error"])
                if times == "1.005":
                    self.assertIn("0.01 s", self.handler._json.call_args.args[1]["error"])
                emit_cycle.assert_not_called()
                self.handler._send.assert_not_called()

    def test_sidecar_retains_measurement_options_and_summary(self):
        self.handler.waam_who = {"email": "test@example.com"}
        stops = [{"layer": 1, "readings_s": [20, 40, 60]}]
        summary = {"measure_stops": stops, "notes": ["export note"]}
        query = dict(self.query, measure_times=[""], measure_pause=["0"], clean_first=["1"],
                     measure_clean=["1"], measure_pre_weld=["0"])
        with mock.patch.object(server, "_git_commit", return_value="test"), \
                mock.patch.object(server.runs, "add_file", return_value="MEASURE.JBI") as add_file:
            filed = server._Handler._file_run(self.handler, "test", "WELD-JOB", b"NOP\r\nEND\r\n",
                                             name="MEASURE", ext=".JBI", query=query,
                                             start_if_none=True, summary=summary)
        self.assertEqual(filed, "MEASURE.JBI")
        sidecar = add_file.call_args.kwargs["sidecar"]
        for key in ("clean_first", "measure_every", "measure_times", "measure_pause",
                    "measure_clean", "measure_pre_weld"):
            self.assertEqual(sidecar["query"][key], query[key][0])
        self.assertEqual(sidecar["measure_stops"], stops)
        self.assertEqual(sidecar["summary"]["measure_stops"], stops)
        self.assertEqual(summary["measure_stops"], stops)

    def test_ros_route_forwards_interpass_controls(self):
        from waam_pipeline import emit

        class Job:
            @staticmethod
            def to_json():
                return '{"steps":[]}'

        query = {"name": ["MEASURE"], "temp_check_every": ["2"],
                 "bead_dwell": ["3.5"], "measure_every": ["1"],
                 "measure_clean": ["1"]}
        with mock.patch.object(emit, "emit_ros2",
                               return_value=(Job(), {"segments": 0, "points": 0})) as emit_ros2:
            self.handler._api_ros2("test", query)
        self.handler._json.assert_not_called()
        self.assertEqual(self.handler._send.call_args.args[0], 200)
        kwargs = emit_ros2.call_args.kwargs
        self.assertEqual((kwargs["temp_check_every"], kwargs["bead_dwell_s"]), (2, 3.5))
        self.assertEqual((kwargs["measure"].every, kwargs["measure"].clean), (1, True))


class TestThermalPlanStudioSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "src/waam_pipeline/studio.py").read_text(encoding="utf-8")

    def test_controls_and_prediction_table_exist(self):
        for control in ("wbThermalTarget", "wbThermalUnit", "wbThermalPeak", "wbThermalAmbient",
                        "wbThermalModel", "wbThermalCompute", "wbThermalApply", "thermalPlanRows"):
            self.assertIn(f'id="{control}"', STUDIO_SHELL)
        self.assertIn("Default run-001 fit", STUDIO_SHELL)
        self.assertIn("Fit this run", STUDIO_SHELL)
        self.assertIn("always verify with the pyrometer", STUDIO_SHELL)

    def test_apply_fills_dwell_fields_sets_every_layer_and_dispatches_edits(self):
        self.assertIn("wbInterpass.value=String(thermalPlan.layer_dwell_s)", self.source)
        self.assertIn('wbInterpassEvery.value="1"', self.source)
        self.assertIn("wbBeadDwell.value=String(thermalPlan.bead_dwell_s)", self.source)
        self.assertIn('new Event("input",{bubbles:true})', self.source)
        self.assertIn('new Event("change",{bubbles:true})', self.source)
        self.assertIn("with interpass set to every layer", self.source)

    def test_trace_query_exists_only_while_applied_values_are_unchanged(self):
        for key in ("thermal_target", "thermal_peak", "thermal_model", "thermal_c", "thermal_tau"):
            self.assertIn(key, self.source)
        self.assertIn("wbInterpass.value!==thermalApplied.interpass", self.source)
        self.assertIn("wbInterpassEvery.value!==thermalApplied.every", self.source)
        self.assertIn("wbBeadDwell.value!==thermalApplied.bead", self.source)
        self.assertIn("[wbInterpass,wbInterpassEvery,wbBeadDwell]", self.source)
        self.assertRegex(self.source, r'el\.addEventListener\("input",\(\)=>\{thermalApplied=null;')
        self.assertIn("interpassQuery()+thermalTraceQuery()+ctwdQuery()", self.source)

    def test_trace_records_the_model_used_by_the_accepted_reply(self):
        self.assertIn("model:result.model.key", self.source)

    def test_invalid_fit_remains_applicable_with_a_clear_warning(self):
        self.assertIn('"Fit not valid: "', self.source)
        self.assertIn('result.valid===false?"Apply anyway":"Apply"', self.source)


if __name__ == "__main__":
    unittest.main()
