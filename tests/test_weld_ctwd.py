import json
import re
import sys
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from waam_pipeline import cell_lc, demos, server, slicer, weld_ctwd
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import (CONTACT_OK, Scene, TorchModel, WireHangError, blocking,
                                     check, point_guard)
from waam_pipeline.emit import EmitRefused, PlatterState, emit_cycle_job, emit_job, emit_ros2
from waam_pipeline.layer_sense import apply_auto, auto_plan, fit_layer_top
from waam_pipeline.plan import build_plan
from waam_pipeline.toolpath import Move, Toolpath
from tests._support import IsolatedServerCase
from tests.test_layer_sense import touches

DEFAULT = weld_ctwd.default_mm()
PULSE = 19.05
PLATTER = PlatterState(clear=True, training_block_on=False, attested_by="unit test")
DATE = "2026/10/05 12:00"


def xyz_solve(p):
    return tuple(float(v) for v in p) + (0.0, 0.0, 0.0)


def demo_parts():
    yield "wall", demos.with_feed(slicer.wall(150, 6, 2.0), 200.0)
    yield "thick wall", demos.with_feed(demos.demo_wall(150, 4, 2.0, thickness_mm=16,
                                                        bead_width_mm=9.5), 200.0)
    yield "bead set", demos.demo_bead(count=3)
    yield "cylinder", demos.demo_cylinder(layers=3, segments=16)


def seated(tp):
    origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=6.35)
    return origin, cell_lc.lc_scene(origin, training_block=False)


def gate_kwargs(scene, h=2.0):
    return dict(torch=TorchModel.yaskawa_accufast_45(), scene=scene, bead_width_mm=9.0,
                bead_height_mm=h, margin_mm=5.0, contact_ok=CONTACT_OK)


class TestBounds(unittest.TestCase):
    def test_default_comes_from_the_calibration_and_the_profile_agrees(self):
        self.assertEqual(DEFAULT, cell_lc.CALIBRATION["touch_off"]["weld_stickout_mm"])
        self.assertEqual(DEFAULT, TorchModel.yaskawa_accufast_45().stackup.ctwd_mm)
        self.assertEqual(weld_ctwd.resolve(None), (DEFAULT, 0.0, []))
        self.assertEqual(weld_ctwd.resolve(DEFAULT), (DEFAULT, 0.0, []))

    def test_below_default_and_above_40_are_refused(self):
        for bad in (DEFAULT - 0.01, 8.9, 5.0, 40.01, float("nan"), float("inf"), True, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                weld_ctwd.resolve(bad)
        self.assertAlmostEqual(weld_ctwd.resolve(40.0)[1], 40.0 - DEFAULT)

    def test_above_one_and_a_quarter_inch_is_a_note_not_a_refusal(self):
        self.assertEqual(weld_ctwd.resolve(31.75)[2], [])
        value, delta, notes = weld_ctwd.resolve(32.0)
        self.assertEqual((value, delta), (32.0, 32.0 - DEFAULT))
        self.assertIn("1.25 in", notes[0])

    def test_stamp_fits_a_pendant_comment(self):
        from waam_pipeline.backends.jbi import _comment
        for value in (DEFAULT, PULSE, 40.0):
            line = _comment(weld_ctwd.stamp(value))
            self.assertLessEqual(len(line), 33)
            self.assertEqual(line, "' " + weld_ctwd.stamp(value))
        self.assertEqual(weld_ctwd.stamp(PULSE), "WELD CTWD 19.05MM 0.750IN")


class TestApply(unittest.TestCase):
    def test_default_is_the_same_object(self):
        tp = slicer.wall(100, 3, 2.0)
        for value in (None, DEFAULT):
            self.assertIs(weld_ctwd.apply(tp, value)[0], tp)

    def test_everything_from_the_first_weld_on_rises(self):
        for name, tp in demo_parts():
            with self.subTest(part=name):
                raised, value, delta, _ = weld_ctwd.apply(tp, PULSE)
                self.assertAlmostEqual(delta, PULSE - DEFAULT)
                first = weld_ctwd.weld_vertices(tp).index(True)
                for i, (a, b) in enumerate(zip(tp.moves, raised.moves)):
                    if i >= first:
                        self.assertEqual(b, replace(a, z=a.z + delta, ctwd_lift=delta))
                    else:
                        self.assertEqual(a, b)
        way_in = Toolpath([Move(0, 0, 30, None, False, 0, 1), *slicer.wall(100, 2, 2.0).moves])
        raised = weld_ctwd.apply(way_in, PULSE)[0]
        self.assertEqual(raised.moves[0], way_in.moves[0])
        self.assertEqual(raised.moves[1].ctwd_lift, PULSE - DEFAULT)

    def test_never_applied_twice(self):
        raised = weld_ctwd.apply(slicer.wall(100, 3, 2.0), PULSE)[0]
        with self.assertRaisesRegex(ValueError, "already"):
            weld_ctwd.apply(raised, PULSE)
        with self.assertRaisesRegex(ValueError, "already"):
            weld_ctwd.apply(raised, None)


class TestPlan(unittest.TestCase):


    def test_after_the_first_weld_starts_everything_rises_but_the_approach(self):
        for name, tp in demo_parts():
            with self.subTest(part=name):
                raised, _, delta, _ = weld_ctwd.apply(tp, PULSE)
                a = build_plan(tp, xyz_solve, max_joint_step_deg=1e9)
                b = build_plan(raised, xyz_solve, max_joint_step_deg=1e9)
                self.assertEqual([(s.kind, s.role) for s in a], [(s.kind, s.role) for s in b])
                for sa, sb in zip(a, b):
                    if sa.xyz is None:
                        self.assertEqual(sa, sb)
                        continue
                    up = 0.0 if sa.role == "approach" else delta
                    np.testing.assert_allclose(sb.xyz, np.add(sa.xyz, (0, 0, up)), atol=1e-9)

    def test_every_invented_point_is_guarded_with_the_hanging_wire(self):
        seen = []
        raised = weld_ctwd.apply(demos.demo_bead(count=2), PULSE)[0]
        build_plan(raised, xyz_solve, max_joint_step_deg=1e9,
                   guard=lambda xyz, where, wire_drop_mm=0.0: seen.append(wire_drop_mm))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {PULSE - DEFAULT})

    def test_clean_first_guards_the_way_in_with_the_cut_wire(self):
        seen = []
        raised = weld_ctwd.apply(demos.demo_bead(count=2), PULSE)[0]
        build_plan(raised, xyz_solve, max_joint_step_deg=1e9, clean_first=True,
                   guard=lambda xyz, where, wire_drop_mm=0.0: seen.append((where, wire_drop_mm)))
        self.assertEqual(seen[0][1], 0.0)
        self.assertIn(PULSE - DEFAULT, {drop for _, drop in seen})

    def test_a_raise_that_reaches_the_hover_is_refused(self):
        raised = weld_ctwd.apply(slicer.wall(100, 2, 2.0), PULSE)[0]
        with self.assertRaisesRegex(ValueError, "hover"):
            build_plan(raised, xyz_solve, hover_mm=PULSE - DEFAULT, max_joint_step_deg=1e9)

    def test_a_layer_range_keeps_its_raised_start(self):
        raised = weld_ctwd.apply(slicer.wall(100, 4, 2.0), PULSE)[0]
        part = raised.layer_range(2, 3)
        steps = build_plan(part, xyz_solve, max_joint_step_deg=1e9)
        descend = next(s for s in steps if s.role == "descend" or s.role == "air")
        self.assertAlmostEqual(descend.xyz[2], 3 * 2.0 + PULSE - DEFAULT)
        hover = next(s for s in steps if s.role == "approach")
        self.assertAlmostEqual(hover.xyz[2], 3 * 2.0 + 50.0)


class TestClearance(unittest.TestCase):
    def test_zero_raise_is_unchanged(self):
        for name, tp in demo_parts():
            with self.subTest(part=name):
                _, scene = seated(tp)
                kw = gate_kwargs(scene)
                self.assertEqual(check(tp, **kw), weld_ctwd.checked(tp, None, **kw)[3])
                self.assertEqual(check(tp, **kw), weld_ctwd.checked(tp, DEFAULT, **kw)[3])

    def test_a_raise_adds_no_blocking_violation_to_the_standard_parts(self):
        for name, tp in demo_parts():
            _, scene = seated(tp)
            kw = gate_kwargs(scene)
            before = blocking(check(tp, **kw))
            for value in (PULSE, 40.0):
                with self.subTest(part=name, ctwd=value):
                    after = blocking(weld_ctwd.checked(tp, value, **kw)[3])
                    self.assertLessEqual({v.obstacle for v in after}, {v.obstacle for v in before})
                    self.assertLessEqual(len(after), len(before))

    def test_metal_is_judged_where_it_lands(self):
        tp = slicer.wall(150, 4, 2.0)
        _, scene = seated(tp)
        kw = gate_kwargs(scene)
        default = [v for v in check(tp, **kw) if v.obstacle.startswith(("bead(", "charles"))]
        raised = weld_ctwd.checked(tp, PULSE, **kw)[3]


        plate = lambda vs: sorted(round(v.clearance_mm, 9) for v in vs if v.obstacle == "charles_platter")
        self.assertEqual(plate(default), plate(raised))

    def test_entry_wire_state_never_rewrites_move_geometry(self):
        path = slicer.wall(100, 2, 2.0)
        for clean_first, entry_drop in ((False, PULSE - DEFAULT), (True, 0.0)):
            with self.subTest(clean_first=clean_first), \
                    mock.patch("waam_pipeline.clearance.check", return_value=[]) as judge:
                raised = weld_ctwd.checked(path, PULSE, clean_first=clean_first)[0]
            judged = judge.call_args.args[0]
            self.assertEqual(judged.moves, raised.moves)
            self.assertEqual(judge.call_args.kwargs["entry_wire_drop_mm"], entry_drop)
            self.assertEqual(judge.call_args.kwargs["entry_wire_through_move"],
                             weld_ctwd.weld_vertices(raised).index(True))

    def test_a_ploughing_weld_is_still_caught(self):
        tp = slicer.wall(150, 4, 2.0)
        sunk = Toolpath([replace(m, z=m.z - (3.0 if m.layer == 3 else 0.0)) for m in tp.moves])
        _, scene = seated(tp)
        kw = gate_kwargs(scene)
        self.assertTrue(any(v.obstacle.startswith("bead(") for v in blocking(check(sunk, **kw))))
        for value in (PULSE, 40.0):
            with self.subTest(ctwd=value):
                hits = blocking(weld_ctwd.checked(sunk, value, **kw)[3])
                self.assertTrue(any(v.obstacle.startswith("bead(") and v.move_index > 5 for v in hits))

    def test_a_seated_and_raised_path_judges_like_the_flat_one(self):
        tp = slicer.wall(150, 4, 2.0)
        _, scene = seated(tp)
        kw = gate_kwargs(scene)
        ramp = Toolpath([replace(m, z=m.z + 0.01 * m.x, seat=0.01 * m.x) for m in tp.moves])
        flat = {v.obstacle for v in blocking(weld_ctwd.checked(tp, PULSE, **kw)[3])}
        self.assertEqual({v.obstacle for v in blocking(weld_ctwd.checked(ramp, PULSE, **kw)[3])
                          if v.obstacle.startswith("bead(")},
                         {o for o in flat if o.startswith("bead(")})

    def test_bead_sets_and_two_bead_walls_lift_the_hanging_wire_clear(self):
        parts = (("bead set", demos.demo_bead(count=3)),
                 ("2-bead wall", demos.with_feed(demos.demo_wall(150, 4, 2.0, thickness_mm=16,
                                                                 bead_width_mm=9.5), 200.0)))
        for name, tp in parts:
            _, scene = seated(tp)
            kw = gate_kwargs(scene)
            for value in (PULSE, 31.75):
                with self.subTest(part=name, ctwd=value):
                    raised, _, delta, violations, hanging, _ = weld_ctwd.checked(tp, value, **kw)
                    self.assertEqual(hanging, [])


                    first = next(i for i, m in enumerate(tp.moves) if m.deposit)
                    for a, b in zip(tp.moves[first:], raised.moves[first:]):
                        self.assertAlmostEqual(b.z - b.ctwd_lift, a.z)
                    self.assertEqual(sorted((v.obstacle, round(v.clearance_mm, 6)) for v in blocking(violations)),
                                     sorted((v.obstacle, round(v.clearance_mm, 6)) for v in blocking(check(tp, **kw))))

    def test_a_hanging_wire_that_still_meets_the_part_is_refused(self):


        tp = demos.demo_bead(count=2)
        dragged = Toolpath([m for m in tp.moves if not (not m.deposit and m.line_no % 3 == 0)])
        _, scene = seated(dragged)
        kw = gate_kwargs(scene)
        hanging = weld_ctwd.checked(dragged, PULSE, **kw)[4]
        self.assertTrue(hanging)
        self.assertIn("Add travel lift", weld_ctwd.hang_refusal(hanging, PULSE - DEFAULT))
        origin, _ = seated(dragged)
        with mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test"), \
                self.assertRaises(EmitRefused) as refused:
            emit_job(dragged, cell_lc.pulse_solver(origin), name="DRAG", arc_file=81, platter=PLATTER,
                     date=DATE, bead_height_mm=2.0, weld_ctwd_mm=PULSE, **{
                         k: v for k, v in kw.items() if k in ("torch", "scene")})
        self.assertEqual(refused.exception.reason, "ctwd")


class TestEmit(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tp = demos.with_feed(slicer.wall(150, 6, 2.0), 200.0)
        self.origin, self.scene = seated(self.tp)
        self.kw = dict(name="CTWD", arc_file=81, scene=self.scene, platter=PLATTER,
                       torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.0, date=DATE)

    def emit(self, **kw):
        return emit_job(self.tp, cell_lc.pulse_solver(self.origin), **dict(self.kw, **kw))

    def test_default_job_is_byte_identical(self):
        text, summary = self.emit()
        for value in (None, DEFAULT):
            with self.subTest(ctwd=value):
                self.assertEqual(self.emit(weld_ctwd_mm=value)[0], text)
        self.assertEqual(self.emit(clean_first=False)[0], text)
        self.assertNotIn("WELD CTWD", text)
        self.assertEqual((summary["weld_ctwd_mm"], summary["weld_ctwd_delta_mm"]), (DEFAULT, 0.0))

    def test_everything_after_the_approach_rises_on_the_robot(self):
        a, _ = self.emit()
        b, summary = self.emit(weld_ctwd_mm=PULSE)
        self.assertIn("' WELD CTWD 19.05MM 0.750IN", b.splitlines())
        self.assertEqual(summary["weld_ctwd_mm"], PULSE)
        ja, jb = read_jbi(a), read_jbi(b)
        self.assertEqual(len(ja.positions), len(jb.positions))
        moves = lambda j: [line for line in j.instructions if line.startswith(("MOVL", "MOVJ"))]
        for la, lb in zip(moves(ja), moves(jb)):
            pa = cell_lc.tcp_from_pulses([int(v) for v in ja.positions[la.split()[1]].split(",")])
            pb = cell_lc.tcp_from_pulses([int(v) for v in jb.positions[lb.split()[1]].split(",")])
            self.assertEqual(la.split()[2:], lb.split()[2:])
            np.testing.assert_allclose(pb[:2], pa[:2], atol=0.03)

            self.assertAlmostEqual(pb[2] - pa[2], PULSE - DEFAULT if la.startswith("MOVL") else 0.0,
                                   delta=0.03)

    def test_a_raise_keeps_the_arc_schedule_and_pass_table(self):
        schedule = {n: 81 if n < 2 else 83 for n in range(6)}
        a, sa = self.emit(arc_file=schedule, asf_attested=True)
        b, sb = self.emit(arc_file=schedule, asf_attested=True, weld_ctwd_mm=PULSE)
        arc = r"^ARC(?:ON ASF|OF AEF)#\((\d+)\)\r?$"
        self.assertEqual(re.findall(arc, b, re.M), re.findall(arc, a, re.M))
        self.assertEqual(sb["asf"], sa["asf"])
        self.assertEqual(sb["pass_table"], sa["pass_table"])
        self.assertIn("' WELD CTWD 19.05MM 0.750IN", b.splitlines())

    def test_a_low_way_in_is_refused_without_clean_first_and_emits_with_it(self):


        path = Toolpath([Move(-60.0, 0.0, 1.0, None, False, 0, 1),
                         Move(0.0, 0.0, 0.0, None, False, 0, 2),
                         Move(100.0, 0.0, 0.0, 200.0, True, 0, 3)])
        origin, scene = seated(path)
        kw = dict(self.kw, scene=scene)
        solve = lambda: cell_lc.pulse_solver(origin)
        emit_job(path, solve(), **kw)
        with self.assertRaises(EmitRefused) as refused:
            emit_job(path, solve(), weld_ctwd_mm=PULSE, **kw)
        self.assertEqual(refused.exception.reason, "ctwd")
        self.assertIn('Enable "Clean wire before the first weld"', refused.exception.detail)
        hanging = weld_ctwd.checked(path, PULSE, **gate_kwargs(scene))[4]
        self.assertTrue(all(before for _, before in hanging))
        text, summary = emit_job(path, solve(), weld_ctwd_mm=PULSE, clean_first=True, **kw)
        self.assertTrue(summary["clean_first"])
        self.assertIn("' CLEAN WIRE BEFORE FIRST WELD", text.splitlines())
        self.assertLess(text.index("CALL JOB:CLEAN_R1"), text.index("ARCON"))
        parsed = read_jbi(text)
        moves = [line for line in parsed.instructions if line.startswith(("MOVJ", "MOVL", "MOVC"))]
        self.assertEqual(len(parsed.positions), len(moves))
        self.assertEqual(int(re.search(r"///NPOS (\d+),", text).group(1)), len(moves))
        self.assertEqual(weld_ctwd.checked(path, PULSE, clean_first=True,
                                           **gate_kwargs(scene))[4], [])
        self.assertEqual(weld_ctwd.apply(path, PULSE)[0].moves[0], path.moves[0])

    def test_clean_first_does_not_turn_a_colliding_first_bead_into_a_ramp(self):


        path = Toolpath([
            Move(-60.0, 0.0, 30.0, None, False, 0, 1),
            Move(0.0, 0.0, 0.0, None, False, 0, 2),
            Move(100.0, 0.0, 0.0, 200.0, True, 0, 3),
            Move(100.0, 0.0, 1.0, None, False, 1, 4),
            Move(0.0, 0.0, 1.0, 200.0, True, 1, 5),
        ])
        origin, scene = seated(path)
        kwargs = dict(self.kw, scene=scene)
        solve = lambda: cell_lc.pulse_solver(origin)
        exports = (
            lambda clean: emit_job(path, solve(), layers=(1, 1), clean_first=clean,
                                   weld_ctwd_mm=PULSE, **kwargs),
            lambda clean: emit_cycle_job(
                path, solve(), cycle=2, layers=(1, 1), step_mm=2.0, tail=None,
                clean_first=clean, weld_ctwd_mm=PULSE, **kwargs),
        )
        for export in exports:
            for clean in (False, True):
                with self.subTest(export=export.__code__.co_firstlineno, clean_first=clean), \
                        self.assertRaises(EmitRefused) as refused:
                    export(clean)
                self.assertEqual(refused.exception.reason, "clearance")
                self.assertIn("bead(line 3)", refused.exception.detail)

    def test_cut_entry_wire_does_not_extend_across_the_first_weld(self):


        path = Toolpath([
            Move(-60.0, 0.0, 30.0, None, False, 0, 1),
            Move(0.0, 0.0, 0.0, None, False, 0, 2),
            Move(100.0, 0.0, 0.0, 200.0, True, 0, 3),
        ])
        scene = Scene(boxes=[
            ("charles_platter", np.array([19.0, -10.0, -10.0]),
             np.array([21.0, 10.0, 1.0])),
        ])
        origin = cell_lc.snap_toolpath_to_platter(
            path, 1014.0, -180.0, substrate_mm=6.35)
        kwargs = dict(self.kw, scene=scene)
        for clean_first in (False, True):
            with self.subTest(clean_first=clean_first), \
                    self.assertRaises(EmitRefused) as refused:
                emit_job(path, cell_lc.pulse_solver(origin), weld_ctwd_mm=PULSE,
                         clean_first=clean_first, **kwargs)
            self.assertEqual(refused.exception.reason, "clearance")
            self.assertIn("charles_platter", refused.exception.detail)

    def test_invented_points_are_guarded_with_the_hanging_wire(self):
        guard = point_guard(TorchModel.yaskawa_accufast_45(), self.scene, 5.0)
        low = (0.0, 0.0, 4.0)
        guard(low, "low hover")
        with self.assertRaises(WireHangError) as refused:
            guard(low, "low hover", wire_drop_mm=PULSE - DEFAULT)
        self.assertIn("CLEAN_R1", str(refused.exception))

    def test_an_air_only_path_gets_no_raise(self):
        air = Toolpath([Move(0.0, 0.0, 30.0, None, False, 0, 1),
                        Move(50.0, 0.0, 30.0, None, False, 0, 2)])
        self.assertEqual(weld_ctwd.apply(air, PULSE), (air, DEFAULT, 0.0, []))
        text, summary = emit_job(air, cell_lc.pulse_solver(self.origin), weld_ctwd_mm=PULSE, **self.kw)
        self.assertNotIn("WELD CTWD", text)
        self.assertEqual((summary["weld_ctwd_mm"], summary["weld_ctwd_delta_mm"]), (DEFAULT, 0.0))
        self.assertEqual(text, emit_job(air, cell_lc.pulse_solver(self.origin), **self.kw)[0])

    def test_out_of_bounds_ctwd_is_refused(self):
        for value in (8.0, 41.0):
            with self.subTest(ctwd=value), self.assertRaises(EmitRefused) as refused:
                self.emit(weld_ctwd_mm=value)
            self.assertEqual(refused.exception.reason, "ctwd")

    def test_ros2_goes_through_the_same_raise(self):
        kw = dict(name="CTWD", scene=self.scene, platter=PLATTER,
                  torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.0)
        solve = lambda: cell_lc.pulse_solver(self.origin)
        default, _ = emit_ros2(self.tp, solve(), **kw)
        self.assertEqual(emit_ros2(self.tp, solve(), weld_ctwd_mm=DEFAULT, **kw)[0].to_json(),
                         default.to_json())
        job, summary = emit_ros2(self.tp, solve(), weld_ctwd_mm=PULSE, **kw)
        self.assertEqual(summary["weld_ctwd_mm"], PULSE)
        self.assertIn("WELD CTWD 19.05MM 0.750IN", job.to_json())
        self.assertNotEqual(job.to_json(), default.to_json())
        with self.assertRaises(EmitRefused):
            emit_ros2(self.tp, solve(), weld_ctwd_mm=9.0, **kw)
        with self.assertRaisesRegex(EmitRefused, "Clean wire .* before the first weld"):
            emit_ros2(self.tp, solve(), clean_first=True, **kw)


class TestReseat(unittest.TestCase):


    def test_auto_reseat_then_raise(self):
        path = slicer.wall(100, 6, 2)
        origin = cell_lc.snap_toolpath_to_platter(path, 1014, -100)
        plan, hop = auto_plan(path, 0, 1, step_mm=2, spacing_mm=20)
        records = touches(plan, origin, lambda x: -0.3)
        top = fit_layer_top(path, 1, plan, records, origin=origin, bead_height_mm=2)
        reseated, _ = apply_auto(path, top, first=0, last=1, step_mm=2, bead_height_mm=2)
        raised, _, delta, _ = weld_ctwd.apply(reseated, PULSE)
        for a, b, w in zip(reseated.moves, raised.moves, weld_ctwd.weld_vertices(reseated)):
            self.assertEqual(b.seat, a.seat)
            self.assertAlmostEqual(b.z - a.z, delta if w else 0.0)
        with mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test"):
            kwargs = dict(name="CYCLE_C02", arc_file=12, cycle=2, layers=(2, 3), step_mm=2,
                          scene=cell_lc.lc_scene(origin, training_block=False),
                          torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2, platter=PLATTER,
                          layers_per_interpass=2, date=DATE)
            plan2, hop2 = auto_plan(reseated, 2, 3, step_mm=2, spacing_mm=20)
            tail = {"plan": plan2, "hop_mm": hop2}
            a, _ = emit_cycle_job(reseated, cell_lc.pulse_solver(origin), tail=tail, **kwargs)
            b, summary = emit_cycle_job(reseated, cell_lc.pulse_solver(origin), tail=tail,
                                        weld_ctwd_mm=PULSE, **kwargs)
        self.assertEqual(summary["weld_ctwd_mm"], PULSE)
        self.assertIn("' WELD CTWD 19.05MM 0.750IN", b.splitlines())

        tail_of = lambda text: text.split("' INTERPASS: TEMP, WAIT, TEMP")[1]
        ja, jb = read_jbi(a), read_jbi(b)
        self.assertEqual(tail_of(a).split("CALL JOB")[1].splitlines()[1:],
                         tail_of(b).split("CALL JOB")[1].splitlines()[1:])
        searches = lambda j, t: [j.positions[ln.split()[1]] for ln in t.splitlines()
                                 if ln.startswith("MOVL C")]
        self.assertEqual(searches(ja, tail_of(a)), searches(jb, tail_of(b)))


class TestServed(IsolatedServerCase):
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
        code, body = self.request("/api/slice", dict(dict(kind="wall", shape="wall", fixture="0", lh="2",
                                                          bead="8.6", travel="175", wall_length="120",
                                                          wall_layers="3"), **params), method="POST")
        return code, json.loads(body)

    def jbi(self, name, **params):
        return self.request("/api/jbi", dict(dict(clear="1", block="0", by="t", arc="81", name=name,
                                                  gen=server._state("local").get("gen")), **params))

    def test_slice_export_save_and_load(self):
        code, result = self.slice(ctwd="19.05")
        self.assertTrue(result["ok"], result.get("error"))
        meta = result["state"]["meta"]
        self.assertEqual(meta["params"]["ctwd"], "19.05")
        self.assertEqual(meta["weld_ctwd"]["mm"], PULSE)
        self.assertAlmostEqual(meta["weld_ctwd"]["delta_mm"], PULSE - DEFAULT)
        self.assertEqual(result["blocking"], 0)
        code, body = self.jbi("CTWD34")
        self.assertEqual(code, 200, body[:300])
        self.assertIn(b"' WELD CTWD 19.05MM 0.750IN", body)
        code, body = self.jbi("CTWD34B", ctwd="12.7")
        self.assertEqual(code, 400, body[:300])
        self.assertIn(b"Slice & check again", body)
        code, body = self.request("/api/save", dict(name="ctwd34", gen=server._state("local").get("gen")),
                                  method="POST")
        self.assertEqual(code, 200, body[:300])
        path = server.SAVES_DIR / "local" / "ctwd34" / "request.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["params"]["ctwd"], "19.05")

        del saved["params"]["ctwd"]
        path.write_text(json.dumps(saved))
        reply = json.loads(self.request("/api/load", dict(name="ctwd34"), method="POST")[1])
        self.assertTrue(reply["ok"], reply)
        self.assertNotIn("ctwd", reply["params"])
        self.assertIsNone(server._state("local")["emit"]["weld_ctwd_mm"])
        code, body = self.jbi("CTWDDEF")
        self.assertEqual(code, 200, body[:300])
        self.assertNotIn(b"WELD CTWD", body)

    def test_a_blank_ctwd_on_export_means_the_default_on_every_route(self):
        code, result = self.slice(ctwd="19.05")
        self.assertTrue(result["ok"], result.get("error"))
        for route, extra in (("/api/jbi", dict(arc="81")), ("/api/ros2", {})):
            with self.subTest(route=route):
                code, body = self.request(route, dict(clear="1", block="0", by="t", name="BLANK",
                                                      ctwd="", gen=server._state("local").get("gen"),
                                                      **extra))
                self.assertEqual(code, 400, body[:300])
                self.assertEqual(json.loads(body)["reason"], "ctwd")
        self.assertEqual(server._ctwd_from_query({"ctwd": [""]}, {"weld_ctwd_mm": None}), (None, None))
        self.assertEqual(server._ctwd_from_query({"ctwd": [""]}, {"weld_ctwd_mm": PULSE})[0], None)
        self.assertEqual(server._ctwd_from_query({}, {"weld_ctwd_mm": PULSE}), (PULSE, None))

    def test_default_slice_emits_the_job_it_always_did(self):
        self.slice()
        code, plain = self.jbi("SAME")
        self.assertEqual(code, 200, plain[:300])
        self.slice(ctwd=str(DEFAULT))
        code, explicit = self.jbi("SAME", ctwd=str(DEFAULT))
        self.assertEqual(code, 200, explicit[:300])
        strip = lambda body: [ln for ln in body.decode("ascii").splitlines() if not ln.startswith("///DATE")]
        self.assertEqual(strip(plain), strip(explicit))

    def test_clean_first_query_emits_the_proven_call(self):
        self.slice(ctwd=str(PULSE))
        code, body = self.jbi("CLEANFIRST", clean_first="1")
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        self.assertLess(text.index("CALL JOB:CLEAN_R1"), text.index("ARCON"))
        self.assertIn("' CLEAN WIRE BEFORE FIRST WELD", text)

    def test_out_of_bounds_slice_is_refused(self):
        for value in ("9", "41"):
            with self.subTest(ctwd=value):
                code, result = self.slice(ctwd=value)
                self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
