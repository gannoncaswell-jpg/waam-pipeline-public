from dataclasses import replace
from pathlib import Path
import re
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, slicer
from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking, check
from waam_pipeline.backends.jbi import emit_search_jbi
from waam_pipeline.emit import EmitRefused, PlatterState, emit_job
from waam_pipeline.layer_sense import (MAX_POINTS, auto_plan, fit_layer_top, layer_sense_plan,
                                       sense_motions)
from waam_pipeline.toolpath import Move, Toolpath
from waam_pipeline import layer_sense as _layer_sense


def setUpModule():

    from unittest import mock
    patcher = mock.patch.object(_layer_sense, "RESEARCH_MODE", False)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)

H = 2.0
PLATTER = PlatterState(clear=True, training_block_on=False, attested_by="test")


def touches(plan, origin, error):

    solve = cell_lc.pulse_solver(origin)
    out = []
    for probe in plan:
        point = probe.expected + [0.0, 0.0, error(probe.expected[0])]
        out.append(np.rint(np.asarray(solve(tuple(point))) * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist())
    return out


def ramp(x):
    return -0.3 + 0.004 * (x - 50.0)


class TestLayerSense(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 4, H)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)
        cls.scene = cell_lc.lc_scene(cls.origin, training_block=False)
        cls.torch = TorchModel.yaskawa_accufast_45()
        cls.plan = layer_sense_plan(cls.path, 1, bead_height_mm=H, spacing_mm=20)

    def fit(self, path=None, plan=None, records=None, layer=1, **kwargs):
        path = self.path if path is None else path
        plan = self.plan if plan is None else plan
        records = touches(plan, self.origin, ramp) if records is None else records
        return fit_layer_top(path, layer, plan, records, origin=self.origin, bead_height_mm=H, **kwargs)

    def test_plan_touches_the_predicted_top_along_the_layer_path(self):

        np.testing.assert_allclose([p.expected for p in self.plan],
                                   [[x, 0, 4 + H] for x in (90, 70, 50, 30, 10)], atol=1e-9)
        for probe in self.plan:
            np.testing.assert_allclose(probe.start - probe.expected, [0, 0, 25])
            np.testing.assert_allclose(probe.target - probe.expected, [0, 0, -0.75 * H])
            np.testing.assert_allclose(probe.direction, [0, 0, -1])
        self.assertEqual([p.name for p in self.plan], [f"layer1_{i:02d}" for i in range(5)])
        dense = layer_sense_plan(self.path, 1, bead_height_mm=H, spacing_mm=1)
        self.assertLessEqual(len(dense), MAX_POINTS)
        self.assertTrue(all(10 - 1e-9 <= p.arc_mm <= 90 + 1e-9 for p in dense))

    def test_plan_refusals(self):
        for kwargs, message in (({"overtravel_mm": H}, "under one bead height"),
                                ({"max_points": 81}, "P120-P199"),
                                ({"spacing_mm": 0}, "positive")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                layer_sense_plan(self.path, 1, bead_height_mm=H, **kwargs)
        with self.assertRaisesRegex(ValueError, "no weld moves"):
            layer_sense_plan(self.path, 9, bead_height_mm=H)
        with self.assertRaisesRegex(ValueError, "last layer"):
            layer_sense_plan(self.path, 3, bead_height_mm=H)
        with self.assertRaisesRegex(ValueError, "long enough"):
            layer_sense_plan(slicer.wall(15, 3, H), 0, bead_height_mm=H)
        many = []
        for k in range(81):
            many += [Move(k * 30.0, 0, H, None, False, 0, 2 * k + 1), Move(k * 30.0 + 25, 0, H, 200, True, 0, 2 * k + 2)]
        many += [Move(0, 0, 2 * H, None, False, 1, 99), Move(25, 0, 2 * H, 200, True, 1, 100)]
        with self.assertRaisesRegex(ValueError, "81 weld passes"):
            layer_sense_plan(Toolpath(many), 0, bead_height_mm=H)

    def test_measured_top_reseats_every_later_layer(self):
        top = self.fit()
        np.testing.assert_allclose(top.errors, [ramp(p.expected[0]) for p in self.plan], atol=0.02)
        seated = top.apply(self.path)
        below = [m for m in seated.moves if m.layer <= 1]
        self.assertEqual(below, [m for m in self.path.moves if m.layer <= 1])
        layer2 = [m for m in seated.moves if m.layer == 2 and m.deposit]

        station_x = [p.expected[0] for p in self.plan]
        for x, measured in zip(station_x, top.measured):
            vertex = next(m for m in layer2 if abs(m.x - x) < 1e-9)
            self.assertAlmostEqual(vertex.z, measured, places=9)
        for move in seated.moves:
            if move.layer >= 2:
                source = 2 * (move.layer + 1) if move.layer < 4 else None
                self.assertAlmostEqual(move.z - move.seat, source, places=9)
        self.assertEqual([k for k, _ in seated.arc_events()], [k for k, _ in self.path.arc_events()])

    def test_reseated_part_passes_clearance_and_the_seat_carries_it(self):
        seated = self.fit().apply(self.path)
        self.assertFalse(blocking(check(seated, self.torch, self.scene, contact_ok=CONTACT_OK, bead_height_mm=H)))

        unseated = Toolpath([replace(m, seat=0.0) for m in seated.moves])
        hits = [v for v in blocking(check(unseated, self.torch, self.scene, contact_ok=CONTACT_OK, bead_height_mm=H))
                if v.obstacle.startswith("bead(")]
        self.assertTrue(hits)

    def test_a_second_sense_composes_on_the_first(self):
        once = self.fit().apply(self.path)
        plan2 = layer_sense_plan(once, 2, bead_height_mm=H, spacing_mm=20)

        def bump(x):
            return 0.2 - 0.002 * (x - 50.0)

        twice = fit_layer_top(once, 2, plan2, touches(plan2, self.origin, bump),
                              origin=self.origin, bead_height_mm=H).apply(once)
        layer3 = [m for m in twice.moves if m.layer == 3 and m.deposit]
        for probe in plan2:
            x = probe.expected[0]
            vertex = min(layer3, key=lambda m: abs(m.x - x))
            self.assertAlmostEqual(vertex.x, x, places=9)
            self.assertAlmostEqual(vertex.z, 8.0 + ramp(x) + bump(x), delta=0.03)

    def test_fit_refuses_records_that_are_not_this_plans(self):
        good = touches(self.plan, self.origin, ramp)
        with self.assertRaisesRegex(ValueError, "expected 5 touch records"):
            self.fit(records=good[:4])
        solve = cell_lc.pulse_solver(self.origin)

        def record(point):
            return np.rint(np.asarray(solve(tuple(point))) * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist()

        off = list(good)
        off[1] = record(self.plan[1].expected + [2.0, 0, 0])
        with self.assertRaisesRegex(ValueError, "P121 touched 2.0 mm from its planned station"):
            self.fit(records=off)
        low = list(good)
        low[2] = record(self.plan[2].target - [0, 0, 1.0])
        with self.assertRaisesRegex(ValueError, "P122 is outside its search window"):
            self.fit(records=low)
        high = list(good)
        high[0] = record(self.plan[0].expected + [0, 0, 2.5])
        with self.assertRaisesRegex(ValueError, r"P120: the top of layer 1 is \+2\.50 mm off"):
            self.fit(records=high)
        step = list(good)
        step[2] = record(self.plan[2].expected + [0, 0, 1.9])
        with self.assertRaisesRegex(ValueError, "steps .* between P121 and P122"):
            self.fit(records=step)
        with self.assertRaisesRegex(ValueError, "P123: enter six whole PULSE values"):
            self.fit(records=good[:3] + [[1, 2, 3]] + good[4:])

    def test_welds_off_the_sensed_path_are_refused(self):
        top = self.fit()
        moved = Toolpath([replace(m, y=m.y + (8.0 if m.layer >= 2 else 0.0)) for m in self.path.moves])
        with self.assertRaisesRegex(ValueError, "sensed path"):
            top.apply(moved)

    def test_sense_motions_are_checked_against_the_printed_part(self):
        hopped, start = sense_motions(self.path, 1, self.plan, hop_mm=5.0)
        motions = [v for v in blocking(check(hopped, self.torch, self.scene, contact_ok=CONTACT_OK, bead_height_mm=H))
                   if v.move_index >= start]
        self.assertFalse(motions, motions)

        self.assertEqual(len(hopped.moves) - start, 1 + 1 + 3 * len(self.plan) + 1)
        self.assertTrue(all(abs(m.z - (p.expected[2] + 5.0)) < 1e-9
                            for m, p in zip(hopped.moves[start + 2::3], self.plan)))
        path, begin = sense_motions(self.path, 1, self.plan)
        motions = [v for v in blocking(check(path, self.torch, self.scene, contact_ok=CONTACT_OK, bead_height_mm=H))
                   if v.move_index >= begin]
        self.assertFalse(motions, motions)
        self.assertTrue(all(m.layer <= 1 for m in path.moves[:begin]))

        deep = [replace(p, expected=p.expected - [0, 0, 3.0]) for p in self.plan]
        path, begin = sense_motions(self.path, 1, deep)
        motions = [v for v in blocking(check(path, self.torch, self.scene, contact_ok=CONTACT_OK, bead_height_mm=H))
                   if v.move_index >= begin]
        self.assertTrue(any(v.obstacle.startswith("bead(") for v in motions))

    def test_layer_range_emits_only_those_layers_but_gates_the_whole_part(self):
        seated = self.fit().apply(self.path)
        solve = cell_lc.pulse_solver(self.origin)
        options = dict(name="RESUME2", arc_file=12, scene=self.scene, torch=self.torch,
                       bead_height_mm=H, platter=PLATTER, date="2026/09/25 12:00")
        text, summary = emit_job(seated, solve, layers=(2, 3), **options)
        self.assertEqual(summary["layers"], [2, 3])
        self.assertEqual(sum(line.startswith("ARCON") for line in text.splitlines()), 2)
        self.assertIn("layers 2-3 of 0-3", text)
        full, _ = emit_job(seated, solve, **options)
        self.assertEqual(sum(line.startswith("ARCON") for line in full.splitlines()), 4)
        sunk = Toolpath([replace(m, z=m.z - (1.0 if m.layer == 1 else 0.0)) for m in seated.moves])
        with self.assertRaises(EmitRefused):
            emit_job(sunk, solve, layers=(2, 3), **options)
        for bad in ((3, 2), (0, 9), (True, 2)):
            with self.subTest(layers=bad), self.assertRaises(EmitRefused):
                emit_job(seated, solve, layers=bad, **options)

    def test_layer_range_opening_on_a_weld_gets_its_travel_start(self):
        path = Toolpath([Move(0, 0, 2, None, False, 0, 1), Move(50, 0, 2, 200, True, 0, 2),
                         Move(50, 0, 4, 200, True, 1, 3), Move(0, 0, 4, 200, True, 1, 4)])
        part = path.layer_range(1, 1)
        self.assertFalse(part.moves[0].deposit)
        self.assertEqual(part.moves[0].target, (50, 0, 2))
        self.assertEqual([m.line_no for m in part.moves[1:]], [3, 4])
        with self.assertRaisesRegex(ValueError, "No moves"):
            path.layer_range(5, 6)


class TestSearchWindowIsWholeTenths(unittest.TestCase):


    def test_the_window_rounds_down_and_stays_under_a_bead(self):
        for h, window in ((1.7, 1.2), (2.0, 1.5), (2.9, 2.1), (1.0, 0.7), (2.55, 1.9)):
            with self.subTest(h=h):
                path = slicer.wall(100, 4, h)
                for probe in layer_sense_plan(path, 1, bead_height_mm=h):
                    self.assertAlmostEqual(probe.expected[2] - probe.target[2], window, places=9)
                plan, _hop = auto_plan(path, 0, 1, step_mm=h, spacing_mm=20)
                self.assertAlmostEqual(plan[0].expected[2] - plan[0].target[2], window, places=9)

    def test_touch_jobs_write_one_decimal_and_refuse_finer(self):
        path = slicer.wall(100, 4, 1.7)
        origin = cell_lc.snap_toolpath_to_platter(path, 1014, -100)
        solve = cell_lc.pulse_solver(origin)
        plan = layer_sense_plan(path, 1, bead_height_mm=1.7)
        text = emit_search_jbi(plan, solve, name="LT", date="2026/10/03 01:00",
                               overtravel_mm=float(plan[0].expected[2] - plan[0].target[2]), hop_mm=5.0)
        self.assertEqual(set(re.findall(r"SRCH RIN#\(3\)=ON T=0\.10 DIS=(\S+)", text)), {"1.2"})
        for bad in (1.275, 0.05, 0.0, 6553.6, float("nan")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "whole 0.1 mm"):
                emit_search_jbi(plan, solve, name="LT", date="2026/10/03 01:00",
                                overtravel_mm=bad, hop_mm=5.0)


if __name__ == "__main__":
    unittest.main()
