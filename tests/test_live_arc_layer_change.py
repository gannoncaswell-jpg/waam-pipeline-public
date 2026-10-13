import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.backends import jbi
from waam_pipeline.emit import EmitRefused, _interpass_gate
from waam_pipeline.plan import build_plan
from waam_pipeline.toolpath import Move, Toolpath


def solve(xyz):
    return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def arc_window(steps):
    on = next(i for i, s in enumerate(steps) if s.kind == "arc_on")
    off = next(i for i, s in enumerate(steps) if s.kind == "arc_off")
    return steps[on:off + 1]


class TestLiveArcLayerChange(unittest.TestCase):
    def test_continuous_deposit_across_layers_never_hovers_with_arc_lit(self):
        tp = Toolpath([
            Move(0, 0, 0, None, False, 0, 1),
            Move(10, 0, 0, 300.0, True, 0, 2),
            Move(10, 0, 2, 300.0, True, 1, 3),
            Move(0, 0, 2, 300.0, True, 1, 4),
            Move(0, 0, 12, None, False, 1, 5),
        ])
        window = arc_window(build_plan(tp, solve, interpass_timer_s=30.0))
        self.assertEqual([s.role for s in window if s.kind != "arc_on" and s.kind != "arc_off"],
                         ["weld", "weld", "weld"])
        self.assertFalse(any(s.kind == "dwell" for s in window))

    def test_travel_between_layers_still_hovers_and_dwells(self):
        tp = Toolpath([
            Move(0, 0, 0, None, False, 0, 1),
            Move(10, 0, 0, 300.0, True, 0, 2),
            Move(10, 0, 2, None, False, 1, 3),
            Move(0, 0, 2, 300.0, True, 1, 4),
        ])
        steps = build_plan(tp, solve, interpass_timer_s=30.0)
        roles = [(s.kind, s.role) for s in steps]
        self.assertIn(("linear", "hover"), roles)
        self.assertIn(("dwell", "interpass"), roles)
        for i, s in enumerate(steps):
            if s.role == "hover" and i > 1:
                lit = sum(1 for t in steps[:i] if t.kind == "arc_on") > \
                    sum(1 for t in steps[:i] if t.kind == "arc_off")
                self.assertFalse(lit, "hover with the arc lit")


class TestInterpassGateFollowsThePlan(unittest.TestCase):
    def continuous(self):
        return Toolpath([
            Move(0, 0, 0, None, False, 0, 1),
            Move(10, 0, 0, 300.0, True, 0, 2),
            Move(10, 0, 2, 300.0, True, 1, 3),
            Move(0, 0, 2, 300.0, True, 1, 4),
        ])

    def test_lit_layer_change_is_refused_with_a_timer_set(self):
        with self.assertRaises(EmitRefused) as ctx:
            _interpass_gate(self.continuous(), 30.0, False)
        self.assertEqual(ctx.exception.reason, "interpass")
        self.assertIn("arc lit (move 2)", str(ctx.exception))

    def test_opt_out_still_allows_it(self):
        _interpass_gate(self.continuous(), 30.0, True)

    def test_travel_between_layers_passes(self):
        tp = Toolpath([
            Move(0, 0, 0, None, False, 0, 1),
            Move(10, 0, 0, 300.0, True, 0, 2),
            Move(10, 0, 2, None, False, 1, 3),
            Move(0, 0, 2, 300.0, True, 1, 4),
        ])
        _interpass_gate(tp, 30.0, False)


class TestVTokenDecimal(unittest.TestCase):
    def test_whole_speeds_keep_one_decimal(self):
        self.assertEqual(jbi.v_token(300.0, 60.0), "5.0")
        self.assertEqual(jbi.v_token(1200.0, 60.0), "20.0")
        self.assertEqual(jbi.v_token(325.0, 60.0), "5.4")


if __name__ == "__main__":
    unittest.main()
