from dataclasses import replace
import hashlib
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, slicer
from waam_pipeline.backends import jbi
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, emit_cycle_job
from waam_pipeline.layer_sense import apply_auto, auto_plan, fit_layer_top, layer_sense_plan
from waam_pipeline.toolpath import Toolpath
from tests.test_layer_sense import PLATTER, touches
from waam_pipeline import layer_sense as _layer_sense


def setUpModule():

    patcher = patch.object(_layer_sense, "RESEARCH_MODE", False)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TestAutoGrowth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 6, 2)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)

    def test_measured_growth_reseats_and_resteps_with_preserved_clearance_frame(self):
        for ratio in (0.5, 0.75, 1.0, 1.3, 1.5):
            with self.subTest(ratio=ratio):
                plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
                records = touches(plan, self.origin, lambda x, ratio=ratio: 2 * (ratio - 1))
                top = fit_layer_top(self.path, 0, plan, records,
                                    origin=self.origin, bead_height_mm=2)
                seated, info = apply_auto(self.path, top, first=0, last=0,
                                          step_mm=2, bead_height_mm=2)
                self.assertAlmostEqual(info["growth_raw_mm"], 2 * ratio, delta=0.01)
                self.assertAlmostEqual(info["growth_mm"], 2 * np.clip(ratio, 0.6, 1.4), delta=0.01)
                self.assertEqual(info["clamped"], ratio < 0.6 or ratio > 1.4)
                for probe, measured in zip(plan, top.measured):
                    for layer in (1, 2):
                        vertex = next(m for m in seated.moves if m.layer == layer and m.deposit
                                      and abs(m.x - probe.expected[0]) < 1e-9)
                        self.assertAlmostEqual(vertex.z, measured + (layer - 1) * info["growth_mm"])
                self.assertEqual([m for m in seated.moves if m.layer == 0],
                                 [m for m in self.path.moves if m.layer == 0])
                for move in seated.moves:
                    self.assertAlmostEqual(move.z - move.seat, 2 * (move.layer + 1))
                    if move.deposit:
                        self.assertEqual(move.feed, self.path.moves[1].feed)

    def test_growth_outside_inspection_bounds_is_refused(self):
        plan, _ = auto_plan(self.path, 0, 0, step_mm=2, spacing_mm=20)
        for ratio in (0.3, 1.7):
            with self.subTest(ratio=ratio):
                records = touches(plan, self.origin, lambda x, ratio=ratio: 2 * (ratio - 1))
                top = fit_layer_top(self.path, 0, plan, records,
                                    origin=self.origin, bead_height_mm=2)
                with self.assertRaisesRegex(ValueError, "the part grew .*stop and inspect the part"):
                    apply_auto(self.path, top, first=0, last=0, step_mm=2, bead_height_mm=2)

    def test_cycle_growth_averages_error_over_every_printed_layer(self):
        plan, _ = auto_plan(self.path, 0, 2, step_mm=2, spacing_mm=20)
        top = fit_layer_top(self.path, 2, plan, touches(plan, self.origin, lambda x: -1.5),
                            origin=self.origin, bead_height_mm=2)
        _, info = apply_auto(self.path, top, first=0, last=2, step_mm=2, bead_height_mm=2)
        self.assertAlmostEqual(info["growth_raw_mm"], 1.5, delta=0.01)
        self.assertEqual(info["n"], 3)
        self.assertEqual(info["layers"], [0, 2])

    def test_auto_plan_grows_hop_and_approach_but_preserves_window(self):
        path = slicer.wall(100, 42, 2)
        for last, want_hop, want_approach in ((1, 5, 25), (19, 20, 30), (39, 40, 50)):
            with self.subTest(last=last):
                plan, hop = auto_plan(path, 0, last, step_mm=2, spacing_mm=20)
                self.assertEqual(hop, want_hop)
                manual = layer_sense_plan(path, last, bead_height_mm=2, spacing_mm=20,
                                          approach_mm=want_approach)
                for probe, expected in zip(plan, manual):
                    np.testing.assert_array_equal(probe.expected, expected.expected)
                    np.testing.assert_array_equal(probe.start, expected.start)
                    self.assertAlmostEqual(probe.expected[2] - probe.target[2], 1.5)
                    self.assertLess(hop, probe.start[2] - probe.expected[2])

    def test_invalid_cycle_range_and_step_are_refused(self):
        for first, last, step in ((True, 1, 2), (2, 1, 2), (-1, 1, 2), (0, 1, 0), (0, 1, float("nan"))):
            with self.subTest(values=(first, last, step)), self.assertRaises(ValueError):
                auto_plan(self.path, first, last, step_mm=step, spacing_mm=20)


class TestCycleJbi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 4, 2)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)
        cls.plan, cls.hop = auto_plan(cls.path, 0, 1, step_mm=2, spacing_mm=20)

    def setUp(self):
        self.sha = patch.object(jbi, "_git_sha", return_value="pinned")
        self.sha.start()
        self.addCleanup(self.sha.stop)

    def render(self, *, tail=True, smooth=False):
        return jbi.emit_cycle_jbi(
            self.path.layer_range(0, 1), cell_lc.pulse_solver(self.origin),
            name="CYCLE_C01", date="2026/10/02 12:00", arc_file=12,
            cycle={"cycle": 1, "first": 0, "last": 1, "step_mm": 2}, smooth=smooth,
            tail={"plan": self.plan, "hop_mm": self.hop, "interpass_s": 30} if tail else None,
        )

    def test_existing_pulse_and_touch_jobs_remain_byte_identical(self):

        for smooth, digest in ((False, "b82024d9759ae92f7cffb88cb238f4618262cf03b3d1557dd38ee09ad2b67bc8"),
                               (True, "f62e19513b07a241905ceaa5c2b55e12e50d380780395372ed3ef2670e00dc58")):
            text = jbi.emit_jbi_pulse(self.path, cell_lc.pulse_solver(self.origin), name="PINNED",
                                      date="2026/10/02 12:00", arc_file=12, smooth=smooth)
            self.assertEqual(hashlib.sha256(text.encode("ascii")).hexdigest(), digest)
        for hop, digest in ((None, "6d6fefa148cf057e5a958829e64c3dd7deee23f6d2fdf17d40de97c5c1fc1dca"),
                            (5.0, "b3b656009112cf237f710ef3f60b4a476dd6f3808dfa5cde23631b125844061b")):
            text = jbi._render_search_jbi(self.plan, cell_lc.pulse_solver(self.origin), name="PINNED",
                                          date="2026/10/02 12:00", pvar_base=44, clean_job="CLEAN_R1",
                                          hop_mm=hop, overtravel_mm=1.5)
            self.assertEqual(hashlib.sha256(text.encode("ascii")).hexdigest(), digest)

    def test_weld_body_matches_standalone_and_tail_order_is_exact(self):
        for smooth in (False, True):
            with self.subTest(smooth=smooth):
                lines = self.render(smooth=smooth).splitlines()
                split = lines.index("' INTERPASS: TEMP, WAIT, TEMP")
                weld = jbi.emit_jbi_pulse(self.path.layer_range(0, 1), cell_lc.pulse_solver(self.origin),
                                           name="WELD", date="2026/10/02 12:00", arc_file=12,
                                           interpass_timer_s=0, smooth=smooth).splitlines()
                body = lines[lines.index("NOP") + 1:split]
                clear = ["SET LB000 0", *(f"SET B{80 + i:03d} LB000" for i in range(len(self.plan)))]
                self.assertEqual(body[:len(clear)], clear)
                actual = [ln for ln in body[len(clear):] if not ln.startswith("'")]
                expected = [ln for ln in weld[weld.index("NOP") + 1:-1] if not ln.startswith("'")]
                self.assertEqual(actual, expected)
                self.assertEqual(lines[split:split + 8], [
                    "' INTERPASS: TEMP, WAIT, TEMP", 'MSG "READ TEMP 1 THEN PRESS START"',
                    "PAUSE", 'MSG ""', "TIMER T=30.00", 'MSG "READ TEMP 2 THEN PRESS START"',
                    "PAUSE", 'MSG ""',
                ])
                search = jbi._render_search_jbi(
                    self.plan, cell_lc.pulse_solver(self.origin), name="TOUCH", date="2026/10/02 12:00",
                    clean_job="CLEAN_R1", pvar_base=120, hop_mm=self.hop, overtravel_mm=1.5,
                    tolerant=True).splitlines()
                def normalize(rows):
                    return [re.sub(r"C\d{5}", "CXXXXX", row) for row in rows]

                search_body = [line for line in search[search.index("NOP") + 1:-1]
                               if line != "SET LB000 0" and not re.fullmatch(r"SET B\d{3} LB000", line)]
                self.assertEqual(normalize(lines[split + 8:-1]), normalize(search_body))

    def test_one_header_unique_taught_moves_and_bounded_messages(self):
        text = self.render()
        self.assertEqual(text.count("/JOB\r\n"), 1)
        self.assertEqual(text.count("\r\nNOP\r\n"), 1)
        self.assertEqual(text.count("\r\nEND\r\n"), 1)
        count = int(re.search(r"///NPOS (\d+),", text)[1])
        references = re.findall(r"^MOV[JLC] (C\d{5})", text, re.MULTILINE)
        self.assertEqual(count, len(references))
        self.assertEqual(len(references), len(set(references)))
        self.assertEqual(count, len(re.findall(r"^C\d{5}=", text, re.MULTILINE)))
        self.assertEqual(len(re.findall(r"^MOVL P\d{3}.*SRCH RIN#\(3\)", text, re.MULTILINE)), len(self.plan))
        for message in re.findall(r'^MSG "([^"]*)"', text, re.MULTILINE):
            self.assertLessEqual(len(message), 32)
            self.assertRegex(message, r"^[A-Z0-9 ]*$")

    def test_last_cycle_has_no_pause_message_or_touch(self):
        text = self.render(tail=False)
        for token in ("PAUSE", "MSG ", "SRCH", "CLEAN_R1", "INTERPASS:"):
            self.assertNotIn(token, text)

    def test_message_and_timer_refusals(self):
        for value in ("X" * 33, "lowercase", "TOUCH!", "NONASCII \u00b0", "READ, START"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jbi._message(value)
        for seconds in (0, 0.009, 3600.01, float("nan"), True):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "TIMER"):
                jbi.emit_cycle_jbi(
                    self.path.layer_range(0, 1), cell_lc.pulse_solver(self.origin), name="CYCLE_C01",
                    date="2026/10/02 12:00", arc_file=12,
                    cycle={"cycle": 1, "first": 0, "last": 1, "step_mm": 2},
                    tail={"plan": self.plan, "hop_mm": self.hop, "interpass_s": seconds})
        with self.assertRaisesRegex(ValueError, "hop_mm"):
            jbi.emit_cycle_jbi(
                self.path.layer_range(0, 1), cell_lc.pulse_solver(self.origin), name="CYCLE_C01",
                date="2026/10/02 12:00", arc_file=12,
                cycle={"cycle": 1, "first": 0, "last": 1, "step_mm": 2},
                tail={"plan": self.plan, "hop_mm": None, "interpass_s": 30})

    def test_checked_cycle_gates_the_whole_part_and_touches(self):
        kwargs = dict(name="CYCLE_C01", arc_file=12, cycle=1, layers=(0, 1), step_mm=2,
                      scene=cell_lc.lc_scene(self.origin, training_block=False),
                      torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2, platter=PLATTER,
                      layers_per_interpass=2, date="2026/10/02 12:00")
        tail = {"plan": self.plan, "hop_mm": self.hop}
        text, summary = emit_cycle_job(self.path, cell_lc.pulse_solver(self.origin), tail=tail, **kwargs)
        self.assertEqual(summary["layers"], [0, 1])
        self.assertEqual(summary["probes"], len(self.plan))
        self.assertEqual(text.count("ARCON"), 2)
        sunk = Toolpath([replace(m, z=m.z - (1 if m.layer == 3 else 0)) for m in self.path.moves])
        with self.assertRaises(EmitRefused) as refused:
            emit_cycle_job(sunk, cell_lc.pulse_solver(self.origin), tail=tail, **kwargs)
        self.assertEqual(refused.exception.reason, "clearance")
        deep = [replace(p, expected=p.expected - [0, 0, 3]) for p in self.plan]
        with self.assertRaises(EmitRefused) as refused:
            emit_cycle_job(self.path, cell_lc.pulse_solver(self.origin),
                           tail={"plan": deep, "hop_mm": self.hop}, **kwargs)
        self.assertEqual(refused.exception.reason, "clearance")
        text, summary = emit_cycle_job(self.path, cell_lc.pulse_solver(self.origin), tail=None,
                                       **dict(kwargs, cycle=2, layers=(2, 3)))
        self.assertTrue(summary["last_cycle"])
        self.assertNotIn("PAUSE", text)

    def test_checked_cycle_range_and_tail_must_match_its_pause_gate(self):
        kwargs = dict(name="CYCLE_C01", arc_file=12, cycle=1, step_mm=2,
                      scene=cell_lc.lc_scene(self.origin, training_block=False),
                      torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2, platter=PLATTER,
                      layers_per_interpass=2, date="2026/10/02 12:00")
        tail = {"plan": self.plan, "hop_mm": self.hop}
        for layers, supplied_tail in (((0, 3), None), ((0, 0), tail),
                                      ((0, 1), None), ((2, 3), tail)):
            with self.subTest(layers=layers, tail=supplied_tail is not None):
                with self.assertRaises(EmitRefused) as refused:
                    emit_cycle_job(self.path, cell_lc.pulse_solver(self.origin),
                                   layers=layers, tail=supplied_tail, **kwargs)
                self.assertEqual(refused.exception.reason, "argument")
        with self.assertRaises(EmitRefused) as refused:
            emit_cycle_job(self.path, cell_lc.pulse_solver(self.origin), layers=(0, 3), tail=None,
                           **dict(kwargs, layers_per_interpass=4))
        self.assertEqual(refused.exception.reason, "interpass")
        text, summary = emit_cycle_job(slicer.wall(100, 3, 2), cell_lc.pulse_solver(self.origin),
                                       layers=(2, 2), tail=None, **dict(kwargs, cycle=2))
        self.assertEqual(summary["passes"], 1)
        self.assertTrue(summary["last_cycle"])
        self.assertNotIn("PAUSE", text)


if __name__ == "__main__":
    unittest.main()
