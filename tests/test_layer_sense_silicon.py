from pathlib import Path
import json
import re
import sys
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_layer_sense import touches
from waam_pipeline import cell_lc, layer_sense, server, slicer, var_dat
from waam_pipeline.backends import jbi
from waam_pipeline.backends.jbi import emit_cycle_jbi, emit_search_jbi
from waam_pipeline.layer_sense import (FLAG_BASE, LEGACY_BASE, SENSE_BASE, apply_auto,
                                       auto_plan, fit_layer_top, layer_sense_plan)
from waam_pipeline.toolpath import Move, Toolpath

H = 2.0
REAL_VAR = ROOT / "docs" / "cell-data" / "var-exports" / "VAR_BOP_LINE_HOP_R1_2026-09-25.DAT"


def var_text(points, base, hits=None, flag_base=FLAG_BASE, template=REAL_VAR):

    lines = template.read_bytes().decode("latin-1").splitlines()
    b_at = lines.index("///B") + 1
    p_at = lines.index("///P") + 1
    flags = [int(v) for ln in lines[b_at:b_at + 25] for v in ln.split(",")]
    for i, point in enumerate(points):
        if point is not None:
            lines[p_at + base + i] = ('"PULSE"0,0,0,000000000000000000000000,0,0,'
                                      + ",".join(map(str, point)) + ",0,0")
        if hits is not None:
            flags[flag_base + i] = int(hits[i])
    lines[b_at:b_at + 25] = [",".join(map(str, flags[k:k + 10])) for k in range(0, 250, 10)]
    return "\r\n".join(lines) + "\r\n"


def pattern(n, misses):
    return [0 if i in misses else 1 for i in range(n)]


class TestTolerantJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 4, H)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)
        cls.solve = staticmethod(cell_lc.pulse_solver(cls.origin))
        cls.plan = layer_sense_plan(cls.path, 1, bead_height_mm=H)
        cls.text = emit_search_jbi(cls.plan, cls.solve, name="LT", date="2026/10/07 09:00",
                                   pvar_base=SENSE_BASE, flag_base=FLAG_BASE,
                                   clean_job="CLEAN_R1", hop_mm=5.0,
                                   overtravel_mm=float(cls.plan[0].expected[2] - cls.plan[0].target[2]),
                                   tolerant=True)
        cls.lines = cls.text.split("\r\n")

    def test_ten_mm_stations_land_in_p120_onward(self):
        self.assertEqual(len(self.plan), 9)
        self.assertEqual(re.findall(r"GETS PX(\d{3}) \$PX002", self.text), [str(120 + i) for i in range(9)])

    def test_each_station_stores_its_flag_and_counts_misses(self):
        n = len(self.plan)
        for i in range(n):
            k = self.lines.index(f"GETS B{FLAG_BASE + i:03d} $B002")
            self.assertEqual(self.lines[k - 1].split(" SRCH ")[0], f"MOVL P{120 + i:03d} V=10.0")
            self.assertEqual(self.lines[k + 1:k + 10], [
                f"IFTHENEXP B{FLAG_BASE + i:03d}=0",
                "\t INC LB000",
                "\t IFTHENEXP LB000>=3",
                '\t\t SETUALM 8814 "3 MISSES IN A ROW" 1 SMODE=0',
                "\t ENDIF",
                "ELSE",
                "\t SET LB000 0",
                f"\t GETS PX{120 + i:03d} $PX002",
                "ENDIF"])
        self.assertEqual(self.text.count("SETUALM 8814"), n)
        self.assertNotIn("MAX SEARCH DISTANCE EXCEEDED", self.text)
        self.assertNotIn("GETS B000", self.text)

        init = self.lines.index("SET LB000 0")
        self.assertEqual(self.lines[init + 1:init + n + 1],
                         [f"SET B{FLAG_BASE + i:03d} LB000" for i in range(n)])
        self.assertLess(init, self.lines.index("CALL JOB:CLEAN_R1"))
        self.assertLess(init + n, self.lines.index(f"GETS PX{120:03d} $PX000"))

    def test_local_counter_is_declared_after_the_group(self):
        k = self.lines.index("///GROUP1 RB1")
        self.assertEqual(self.lines[k + 1], "///LVARS 1,0,0,0,0,0,0,0")
        self.assertEqual(self.lines[k + 2], "NOP")

    def test_load_format_invariants(self):
        npos = int(re.search(r"///NPOS (\d+),", self.text).group(1))
        moves = [ln for ln in self.lines if re.match(r"\s*MOV[JL] C\d{5}", ln)]
        records = [ln for ln in self.lines if re.match(r"C\d{5}=", ln)]
        self.assertEqual(npos, len(moves))
        self.assertEqual(npos, len(records))
        self.assertEqual(len({re.search(r"C\d{5}", ln).group() for ln in moves}), len(moves))
        for ln in self.lines:
            if ln.lstrip().startswith("'"):
                self.assertLessEqual(len(ln.lstrip("\t ")), 33, ln)
            if ln[:1].isspace():
                self.assertTrue(ln.startswith("\t"), repr(ln))
        for text in re.findall(r'SETUALM \d+ "([^"]*)"', self.text):
            self.assertLessEqual(len(text), 32)

    def test_same_moves_as_the_strict_job(self):
        strict = emit_search_jbi(self.plan, self.solve, name="LT", date="2026/10/07 09:00",
                                 pvar_base=SENSE_BASE, clean_job="CLEAN_R1", hop_mm=5.0,
                                 overtravel_mm=float(self.plan[0].expected[2] - self.plan[0].target[2]))
        moves = [ln for ln in self.lines if ln.startswith("MOV") or re.match(r"C\d{5}=", ln)]
        self.assertEqual(moves, [ln for ln in strict.split("\r\n") if ln.startswith("MOV") or re.match(r"C\d{5}=", ln)])
        self.assertNotIn("LVARS", strict)
        self.assertIn('SETUALM 8814 "MAX SEARCH DISTANCE EXCEEDED"', strict)

    def test_auto_cycle_tail_is_tolerant(self):
        plan, hop = auto_plan(self.path, 0, 1, step_mm=H, spacing_mm=10)
        text = emit_cycle_jbi(Toolpath([m for m in self.path.moves if m.layer <= 1]), self.solve, name="AUTO_C01", date="2026/10/07 09:00", arc_file=12,
                              cycle={"cycle": 1, "first": 0, "last": 1, "step_mm": H},
                              tail={"plan": plan, "hop_mm": hop, "interpass_s": 20.0,
                                    "pvar_base": SENSE_BASE, "flag_base": FLAG_BASE})
        self.assertIn("///LVARS 1,0,0,0,0,0,0,0", text)
        self.assertIn(f"GETS B{FLAG_BASE + len(plan) - 1:03d} $B002", text)
        self.assertNotIn("MAX SEARCH DISTANCE EXCEEDED", text)
        self.assertIn(f"' TOUCH LAYER 1: {len(plan)} PTS P120-P{119 + len(plan):03d}", text)
        lines = text.split("\r\n")
        first_action = next(i for i, line in enumerate(lines)
                            if re.match(r"(?:MOV\w*|PAUSE|TIMER)\b", line))
        clears = [(i, line) for i, line in enumerate(lines)
                  if re.fullmatch(r"SET B\d{3} LB000", line)]
        self.assertEqual([line for _, line in clears],
                         [f"SET B{FLAG_BASE + i:03d} LB000" for i in range(len(plan))])
        self.assertTrue(all(i < first_action for i, _ in clears))
        self.assertEqual(sum(line == "SET LB000 0" for line in lines), 1)

    def test_long_auto_touch_comment_uses_the_comment_choke_point(self):
        plan = self.plan * 3
        full = f"TOUCH LAYER 10: {len(plan)} PTS P120-P{119 + len(plan):03d}"
        text = emit_cycle_jbi(
            Toolpath([m for m in self.path.moves if m.layer <= 1]), self.solve,
            name="AUTO_C10", date="2026/10/07 09:00", arc_file=12,
            cycle={"cycle": 10, "first": 9, "last": 10, "step_mm": H},
            tail={"plan": plan, "hop_mm": 5.0, "interpass_s": 20.0,
                  "pvar_base": SENSE_BASE, "flag_base": FLAG_BASE})
        self.assertGreaterEqual(len(plan), 19)
        self.assertEqual(len(jbi._comment(full)), 33)
        self.assertIn(jbi._comment(full), text.split("\r\n"))

    def test_tolerant_results_and_flags_must_fit_their_registers(self):
        with self.assertRaisesRegex(ValueError, "P120-P199"):
            emit_search_jbi(self.plan, self.solve, name="LT", date="2026/10/07 09:00", pvar_base=245,
                            flag_base=FLAG_BASE, hop_mm=5.0, overtravel_mm=1.5, tolerant=True)
        with self.assertRaisesRegex(ValueError, "B080-B159"):
            emit_search_jbi(self.plan, self.solve, name="LT", date="2026/10/07 09:00",
                            pvar_base=SENSE_BASE, flag_base=245, hop_mm=5.0,
                            overtravel_mm=1.5, tolerant=True)
        text = emit_search_jbi(self.plan, self.solve, name="LT", date="2026/10/07 09:00",
                               pvar_base=SENSE_BASE, hop_mm=5.0, overtravel_mm=1.5,
                               tolerant=True)
        self.assertIn(f"GETS B{FLAG_BASE:03d} $B002", text)
        with self.assertRaisesRegex(ValueError, "P120-P199"):
            emit_search_jbi(self.plan * 9, self.solve, name="LT", date="2026/10/07 09:00",
                            pvar_base=SENSE_BASE, flag_base=FLAG_BASE, hop_mm=5.0,
                            overtravel_mm=1.5, tolerant=True)


class TestFitWithMisses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(100, 4, H)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)
        cls.plan = layer_sense_plan(cls.path, 1, bead_height_mm=H)

    def records(self, error=lambda x: -0.3 + 0.004 * (x - 50.0)):
        return touches(self.plan, self.origin, error)

    def fit(self, hits, records=None, path=None, plan=None):
        return fit_layer_top(path or self.path, 1, plan or self.plan, records or self.records(),
                             origin=self.origin, bead_height_mm=H, hits=hits)

    def test_misses_are_left_out_and_the_seat_interpolates_across_them(self):
        records = self.records()
        missed = [2, 5]
        records_with_junk = [None if i in missed else r for i, r in enumerate(records)]
        with mock.patch.object(layer_sense, "RESEARCH_MODE", False):
            top = self.fit(pattern(9, missed), records_with_junk)
        self.assertEqual(top.missed, ("P122", "P125"))
        self.assertEqual(len(top.labels), 7)
        info = top.metadata()
        self.assertEqual((info["touched"], info["planned"]), (7, 9))
        self.assertAlmostEqual(info["hit_rate"], 7 / 9)

        for i in missed:
            x = self.plan[i].expected[0]
            e, _ = top.error_at(self.plan[i].expected[:2])
            self.assertAlmostEqual(e, -0.3 + 0.004 * (x - 50.0), delta=0.02)

    def test_a_miss_record_is_never_read_as_a_surface(self):
        records = self.records()
        records[4] = touches([self.plan[4]], self.origin, lambda _x: -4.9)[0]
        with mock.patch.object(layer_sense, "RESEARCH_MODE", False):
            top = self.fit(pattern(9, [4]), records)
        self.assertNotIn("P124", top.labels)
        self.assertLess(abs(top.error_at(self.plan[4].expected[:2])[0] + 0.3), 0.05)

    def test_production_refuses_the_rules_research_warns(self):
        cases = (([0, 1, 3, 4], "5 of 9 stations touched"),
                 ([2, 3, 4], "stopped after 3 misses in a row at P124"),
                 ([2, 3, 4], "P122-P128 missed, a gap of more than 3"))
        for missed, message in cases:
            with self.subTest(missed=missed):
                with mock.patch.object(layer_sense, "RESEARCH_MODE", False), \
                        self.assertRaisesRegex(ValueError, message):
                    self.fit(pattern(9, missed))
                with mock.patch.object(layer_sense, "RESEARCH_MODE", True):
                    top = self.fit(pattern(9, missed))
                self.assertTrue(any(re.search(message, w) for w in top.warnings), top.warnings)

    def test_the_job_stopped_after_three_in_a_row_so_later_flags_are_stale(self):
        with mock.patch.object(layer_sense, "RESEARCH_MODE", True):
            top = self.fit(pattern(9, [3, 4, 5]))
        self.assertEqual(top.missed, ("P123", "P124", "P125", "P126", "P127", "P128"))
        self.assertTrue(any("stopped after 3 misses in a row at P125" in w for w in top.warnings))

    def test_a_pass_with_no_hits_uses_the_nonnegative_nearest_correction(self):

        moves = [Move(0, 0, H, None, False, 0, 1), Move(100, 0, H, 200, True, 0, 2),
                 Move(100, 10, H, None, False, 0, 3), Move(0, 10, H, 200, True, 0, 4)]
        for k, z in ((1, 2 * H), (2, 3 * H)):
            moves += [Move(0, 0, z, None, False, k, 10 * k + 1), Move(100, 0, z, 200, True, k, 10 * k + 2),
                      Move(100, 10, z, None, False, k, 10 * k + 3), Move(0, 10, z, 200, True, k, 10 * k + 4)]
        path = Toolpath(moves)
        plan = layer_sense_plan(path, 1, bead_height_mm=H)
        n0 = sum(p.pass_index == 0 for p in plan)
        hits = [1 if p.pass_index == 0 else 0 for p in plan]
        records = touches(plan, self.origin, lambda _x: 0.4)
        with mock.patch.object(layer_sense, "RESEARCH_MODE", False), self.assertRaisesRegex(ValueError, "pass 2: 0 of"):
            fit_layer_top(path, 1, plan, records, origin=self.origin, bead_height_mm=H, hits=hits)
        with mock.patch.object(layer_sense, "RESEARCH_MODE", True):
            top = fit_layer_top(path, 1, plan, records, origin=self.origin, bead_height_mm=H, hits=hits)
        self.assertEqual(len(top.labels), n0)
        self.assertTrue(any("pass 2: 0 of" in w and "nearest touched station" in w for w in top.warnings))
        self.assertAlmostEqual(top.error_at((50, 0))[0], 0.4, delta=0.02)
        self.assertAlmostEqual(top.error_at((50, 10))[0], 0.4, delta=0.02)
        seated = top.apply(path)
        for move in seated.moves:
            if move.layer == 2 and move.deposit and move.y == 10:
                self.assertAlmostEqual(move.z, 3 * H + 0.4, delta=0.02)

        low = touches(plan, self.origin, lambda _x: -0.4)
        with mock.patch.object(layer_sense, "RESEARCH_MODE", True):
            top = fit_layer_top(path, 1, plan, low, origin=self.origin, bead_height_mm=H, hits=hits)
        self.assertEqual(top.error_at((50, 10))[0], 0.0)

    def test_no_hit_at_all_is_refused_in_both_modes(self):
        for mode in (False, True):
            with self.subTest(research=mode), mock.patch.object(layer_sense, "RESEARCH_MODE", mode), \
                    self.assertRaisesRegex(ValueError, "no station touched"):
                self.fit([0] * 9)

    def test_hit_flags_must_match_the_plan(self):
        for bad in ([1] * 8, [1] * 8 + [2], "111111111"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "hit flags"):
                self.fit(bad)

    def test_auto_growth_averages_only_the_hits(self):
        plan, _hop = auto_plan(self.path, 0, 1, step_mm=H, spacing_mm=10)
        records = touches(plan, self.origin, lambda _x: -0.4)
        records[3] = touches([plan[3]], self.origin, lambda _x: -4.9)[0]
        with mock.patch.object(layer_sense, "RESEARCH_MODE", False):
            top = fit_layer_top(self.path, 1, plan, records, origin=self.origin, bead_height_mm=H,
                                hits=pattern(len(plan), [3]))
            _seated, info = apply_auto(self.path, top, first=0, last=1, step_mm=H, bead_height_mm=H)
        self.assertAlmostEqual(info["mean_error_mm"], -0.4, delta=0.02)
        self.assertEqual(info["missed"], ["P123"])


class TestVarDatFlags(unittest.TestCase):
    def test_real_export_layout_reads_flags_and_drops_miss_records(self):
        points = [[-25489 + i, 24216, -58849, 42708, 18113, -13612] for i in range(5)]
        text = var_text(points, 120, hits=[1, 0, 1, 1, 0])
        self.assertEqual(text.splitlines()[3], "///B")
        records, flags = var_dat.probe_hits(text, 120, 5, flag_base=FLAG_BASE)
        self.assertEqual(flags, [1, 0, 1, 1, 0])
        self.assertEqual(records, [points[0], None, points[2], points[3], None])

        self.assertEqual(var_dat._byte_values(text)[0], 1)
        self.assertEqual(len(var_dat._byte_values(text)), 250)

    def test_a_miss_slot_may_be_unused_but_a_hit_may_not(self):
        text = var_text([None, [1, 2, 3, 4, 5, 6]], 120, hits=[0, 1])
        self.assertEqual(var_dat.probe_hits(text, 120, 2, flag_base=FLAG_BASE),
                         ([None, [1, 2, 3, 4, 5, 6]], [0, 1]))
        with self.assertRaisesRegex(ValueError, "P120 is flagged touched"):
            var_dat.probe_hits(var_text([None, None], 120, hits=[1, 1]), 120, 2,
                               flag_base=FLAG_BASE)

    def test_bad_flags_and_sections_are_refused(self):
        text = var_text([[1, 2, 3, 4, 5, 6]], 120, hits=[7])
        with self.assertRaisesRegex(ValueError, "B080 = 7"):
            var_dat.probe_hits(text, 120, 1, flag_base=FLAG_BASE)
        with self.assertRaisesRegex(ValueError, "beyond"):
            var_dat.probe_hits(text, 120, 10, flag_base=245)
        with self.assertRaisesRegex(ValueError, "no ///B"):
            var_dat.probe_hits("//VAR\r\n///P\r\n", 120, 1, flag_base=FLAG_BASE)


class TestReplayCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = slicer.wall(300, 4, H)
        cls.origin = cell_lc.snap_toolpath_to_platter(cls.path, 1014, -100)

    def test_old_p044_sets_replay_with_their_21_station_cap(self):


        old = layer_sense_plan(self.path, 1, bead_height_mm=H, spacing_mm=5, max_points=21)
        self.assertLessEqual(len(old), 21)
        self.assertGreater(len(layer_sense_plan(self.path, 1, bead_height_mm=H, spacing_mm=5)), 21)
        records = touches(old, self.origin, lambda _x: -0.2)
        sets = server._layer_senses(json.dumps([{"layer": 1, "spacing": 5, "points": records}]))
        self.assertEqual((sets[0].base, sets[0].hits, sets[0].flag_base),
                         (LEGACY_BASE, None, None))
        _tp, meta, _auto = server._replay_layer_senses(self.path, sets, origin=self.origin, bead_height_mm=H)
        self.assertEqual(meta[0]["labels"][0], "P044")
        self.assertEqual((meta[0]["touched"], meta[0]["planned"], meta[0]["missed"]), (len(old), len(old), []))
        self.assertAlmostEqual(meta[0]["mean_error_mm"], -0.2, delta=0.02)

    def test_new_sets_carry_base_and_flags(self):
        plan = layer_sense_plan(self.path, 1, bead_height_mm=H)
        hits = pattern(len(plan), [4, 9])
        records = [r if h else None for r, h in zip(touches(plan, self.origin, lambda _x: -0.2), hits)]
        sets = server._layer_senses(json.dumps([{"layer": 1, "spacing": 10, "points": records,
                                                 "base": SENSE_BASE, "flag_base": FLAG_BASE,
                                                 "hits": hits}]))
        self.assertEqual(sets[0].flag_base, FLAG_BASE)
        _tp, meta, _auto = server._replay_layer_senses(self.path, sets, origin=self.origin, bead_height_mm=H)
        self.assertEqual(meta[0]["missed"], ["P124", "P129"])
        self.assertEqual(meta[0]["planned"], len(plan))

    def test_base_and_flags_go_together(self):
        for entry in ({"base": SENSE_BASE, "hits": [1]},
                      {"base": SENSE_BASE, "flag_base": FLAG_BASE},
                      {"base": LEGACY_BASE, "flag_base": FLAG_BASE, "hits": [1]},
                      {"base": 60, "flag_base": FLAG_BASE, "hits": [1]},
                      {"base": SENSE_BASE, "flag_base": 120, "hits": [1]},
                      {"base": SENSE_BASE, "flag_base": FLAG_BASE, "hits": [1, True]},
                      {"base": SENSE_BASE, "flag_base": FLAG_BASE, "hits": [2]}):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "hit flag"):
                server._layer_senses(json.dumps([dict({"layer": 1, "points": [[0] * 6]}, **entry)]))


if __name__ == "__main__":
    unittest.main()
