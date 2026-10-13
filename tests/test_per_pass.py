import csv
import io
import json
import math
import re
import statistics
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from waam_pipeline import arcmon, cell_lc, passes, runs
from waam_pipeline.arcparams import deposited_area_mm2
from waam_pipeline.backends import jbi
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, PlatterState, emit_cycle_job, emit_job, emit_ros2
from waam_pipeline.slicer import wall
from waam_pipeline import server
from waam_pipeline.server import run_slice
from tests._support import IsolatedServerCase
from tests.test_emit_endpoint import CLEAN, _Serving

ATTESTED = PlatterState(clear=True, training_block_on=False, attested_by="test")
TORCH = TorchModel.yaskawa_accufast_45()
SAMPLE = ROOT / "docs/cell-data/arcmon/1-20251103-181340.CSV"
TEAM2V2 = ROOT / "docs/cell-data/controller-backup/industrial-text-export/TEAM2V2.JBI"
GAJ = ROOT / "docs/cell-data/jobs/BASIC-W-ARC-GAJ.JBI"


def setup(layers=3, feed=200.0):
    tp = wall(60.0, layers, 3.0, feed_mm_min=feed)
    origin = cell_lc.snap_toolpath_to_platter(tp, 1014.0, -180.0, substrate_mm=6.35)
    return tp, origin, cell_lc.lc_scene(part_origin_robot=origin, training_block=False)


def emit(tp, origin, scene, **kw):
    kw.setdefault("arc_file", 12)
    with patch.object(jbi, "_git_sha", return_value="pinned"):
        return emit_job(tp, cell_lc.pulse_solver(origin), bead_height_mm=3.0, name="PTEST",
                        scene=scene, torch=TORCH, date="2026/10/05 00:00", platter=ATTESTED, **kw)


def lines(text):
    return text.split("\r\n")


class TestArcSchedule(unittest.TestCase):
    def test_syntax(self):
        self.assertEqual(passes.parse_arc_schedule("0:81;1-:83"), ((0, 0, 81), (1, None, 83)))
        self.assertEqual(passes.parse_arc_schedule(" 2- : 85 ; 0-1:81 ;"), ((0, 1, 81), (2, None, 85)))

    def test_refusals(self):
        for text in ("", "  ", "0:81;0:83", "0-:81;1:83", "0-2:81;2-:83", "-1:81", "2-1:81", "0:0",
                     "0:1001", "x", "0:81:2", "0,1:81", "0:81;" + "1:81;" * 100):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                passes.parse_arc_schedule(text)

    def test_every_layer_exactly_once(self):
        tp = wall(60.0, 3, 3.0)
        resolve = lambda s: passes.resolve_arc_schedule(passes.parse_arc_schedule(s), tp)
        self.assertEqual(resolve("0:81;1-:83"), {0: 81, 1: 83, 2: 83})
        self.assertEqual(resolve("0-2:81"), {0: 81, 1: 81, 2: 81})
        with self.assertRaisesRegex(ValueError, "layer 2 without.*layer 0-2"):
            resolve("0-1:81")
        with self.assertRaisesRegex(ValueError, "layer 0 without"):
            resolve("1-:83")
        with self.assertRaisesRegex(ValueError, "names layer 5.*layers 0-2"):
            resolve("0-5:81")
        with self.assertRaisesRegex(ValueError, "3-:83 starts above the part's top layer 2"):
            resolve("0-2:81;3-:83")
        with self.assertRaisesRegex(ValueError, "starts above"):
            resolve("0-2:81;7:83")

    def test_the_schedule_counts_layers_as_the_workbench_does(self):


        tp, origin, scene = setup()
        schedule = passes.resolve_arc_schedule(passes.parse_arc_schedule("0:81;1-:83"), tp)
        _, summary = emit(tp, origin, scene, arc_file=schedule, asf_attested=True)
        self.assertEqual([(r["layer"], r["asf"]) for r in summary["pass_table"]], [(0, 81), (1, 83), (2, 83)])


class TestPerPassAsf(unittest.TestCase):
    def setUp(self):
        self.tp, self.origin, self.scene = setup()

    def test_a_schedule_naming_one_number_is_the_single_arc_job(self):
        single, _ = emit(self.tp, self.origin, self.scene)
        same, summary = emit(self.tp, self.origin, self.scene, arc_file={0: 12, 1: 12, 2: 12})
        self.assertEqual(single, same)
        self.assertEqual(summary["asf"], [12])

    def test_a_staged_job_counts_only_the_layers_it_prints(self):
        schedule = {0: 81, 1: 83, 2: 83}
        text, summary = emit(self.tp, self.origin, self.scene, arc_file=schedule, layers=(1, 2))
        self.assertEqual(summary["asf"], [83])
        self.assertEqual(set(re.findall(r"ARCON ASF#\((\d+)\)", text)), {"83"})
        self.assertNotIn("' ASF/AEF list attested", lines(text))
        with self.assertRaises(EmitRefused) as cm:
            emit(self.tp, self.origin, self.scene, arc_file=schedule, layers=(0, 1))
        self.assertEqual(cm.exception.reason, "attestation")

    def test_two_numbers_need_the_pendant_attestation(self):
        with self.assertRaises(EmitRefused) as cm:
            emit(self.tp, self.origin, self.scene, arc_file={0: 81, 1: 83, 2: 83})
        self.assertEqual(cm.exception.reason, "attestation")
        self.assertIn("81,83", cm.exception.detail)
        text, summary = emit(self.tp, self.origin, self.scene, arc_file={0: 81, 1: 83, 2: 83},
                             asf_attested=True)
        self.assertEqual([int(n) for n in re.findall(r"^ARCON ASF#\((\d+)\)\r?$", text, re.M)], [81, 83, 83])
        self.assertEqual([int(n) for n in re.findall(r"^ARCOF AEF#\((\d+)\)\r?$", text, re.M)], [81, 83, 83])
        self.assertIn("' ASF#/AEF# 81,83", lines(text))
        self.assertIn("' ASF/AEF list attested", lines(text))
        self.assertEqual(summary["asf"], [81, 83])
        self.assertEqual([r["asf"] for r in summary["pass_table"]], [81, 83, 83])
        self.assertEqual([r["aef"] for r in summary["pass_table"]], [81, 83, 83])

    def test_a_long_asf_list_spans_comment_lines_inside_the_limit(self):
        tp, origin, scene = setup(layers=12)
        schedule = {n: 989 + n for n in range(12)}
        text, _ = emit(tp, origin, scene, arc_file=schedule, asf_attested=True)
        stamped = [ln for ln in lines(text) if ln.startswith("' ASF#/AEF# ")]
        self.assertGreater(len(stamped), 1)
        self.assertTrue(all(len(ln) <= 33 for ln in stamped))
        listed = {int(n) for ln in stamped for n in ln[len("' ASF#/AEF# "):].split(",")}
        self.assertEqual(listed, set(schedule.values()))

    def test_a_live_arc_cannot_carry_a_pass_into_another_asf(self):
        from waam_pipeline.toolpath import Move, Toolpath
        z = self.tp.moves[0].z
        spiral = Toolpath([Move(0, 0, z, None, False, 0, 1), Move(60, 0, z, None, True, 0, 2),
                           Move(60, 0, z + 3, None, True, 1, 3), Move(0, 0, z + 3, None, True, 1, 4)])
        with self.assertRaisesRegex(ValueError, "only change at an ARCON"):
            jbi.emit_jbi_pulse(spiral, cell_lc.pulse_solver(self.origin), name="SPIRAL",
                               date="2026/10/05 00:00", arc_file={0: 81, 1: 83})
        jbi.emit_jbi_pulse(spiral, cell_lc.pulse_solver(self.origin), name="SPIRAL",
                           date="2026/10/05 00:00", arc_file={0: 81, 1: 81})

    def test_bad_numbers_are_refused(self):
        for schedule in ({0: 81, 1: 1001, 2: 81}, {0: 81, 1: True, 2: 83}, {0: 81, 1: 83}):
            with self.subTest(schedule=schedule), self.assertRaises(EmitRefused) as cm:
                emit(self.tp, self.origin, self.scene, arc_file=schedule, asf_attested=True)
            self.assertEqual(cm.exception.reason, "argument")


class TestPerMoveV(unittest.TestCase):
    def setUp(self):
        self.tp, self.origin, self.scene = setup()
        self.tp.moves = [replace(m, feed=400.0) if m.deposit and m.layer == 1 else m
                         for m in self.tp.moves]

    def test_each_weld_move_carries_its_own_feed(self):
        text, summary = emit(self.tp, self.origin, self.scene)
        weld = [ln.split("V=")[1].split()[0] for ln in lines(text)
                if ln.startswith(("MOVL", "MOVC")) and "V=333.3" not in ln]
        self.assertEqual(sorted(set(weld)), ["3.3", "6.7"])
        table = summary["pass_table"]
        self.assertEqual([(r["v_min_mm_s"], r["v_max_mm_s"]) for r in table], [(3.3, 3.3), (6.7, 6.7), (3.3, 3.3)])
        self.assertEqual([r["travel_mm_min"] for r in table], [198.0, 402.0, 198.0])

    def test_one_explicit_token_cannot_cover_several_feeds(self):
        with self.assertRaises(EmitRefused) as cm:
            emit(self.tp, self.origin, self.scene, weld_v="3.3")
        self.assertEqual(cm.exception.reason, "speed")
        self.assertIn("200..400 mm/min", cm.exception.detail)

    def test_impossible_feeds_are_still_refused(self):
        for feed in (0.0, -5.0, float("nan"), float("inf"), 1.0, 1e6):
            tp = replace(self.tp, moves=[replace(m, feed=feed) if m.deposit and m.layer == 2 else m
                                         for m in self.tp.moves])
            with self.subTest(feed=feed), self.assertRaises(EmitRefused) as cm:
                emit(tp, self.origin, self.scene)
            self.assertEqual(cm.exception.reason, "speed")

    def test_the_ros_job_runs_each_pass_at_its_jbi_speed(self):
        job, summary = emit_ros2(self.tp, cell_lc.pulse_solver(self.origin), bead_height_mm=3.0,
                                 name="PTEST", scene=self.scene, torch=TORCH, platter=ATTESTED)
        weld = [s for s in job.segments if s.welding]
        expected = sum(60.0 / (float(v) * 60.0) * 60.0 for v in ("3.3", "6.7", "3.3"))
        self.assertAlmostEqual(sum(s.duration_s for s in weld), expected, places=2)
        self.assertEqual([r["v_max_mm_s"] for r in summary["pass_table"]], [3.3, 6.7, 3.3])
        self.assertTrue(all(r["first_step"] is None for r in summary["pass_table"]))


class TestPassTable(unittest.TestCase):
    def test_columns_of_an_emitted_wall(self):
        tp, origin, scene = setup()
        text, summary = emit(tp, origin, scene, deposit_model=(1.2, 3.0))
        rows = summary["pass_table"]
        self.assertEqual(len(rows), summary["passes"])
        self.assertEqual([(r["pass"], r["layer"], r["bead"]) for r in rows], [(1, 0, 1), (2, 1, 1), (3, 2, 1)])
        self.assertEqual([r["length_mm"] for r in rows], [60.0, 60.0, 60.0])
        self.assertEqual([r["dwell_s"] for r in rows], [2.0, 30.0, 30.0])
        self.assertEqual(rows[0]["expected_s"], round(60.0 / (198.0 / 60.0), 2))
        self.assertEqual(rows[0]["model_area_mm2"], round(deposited_area_mm2(1.2, 3.0, 198.0), 3))
        body = lines(text)
        for r in rows:


            first = next(i for i, ln in enumerate(body) if ln.startswith(f"MOVL C{r['first_step'] - 1:05d} "))
            self.assertTrue(body[first - 1].startswith("ARCON ASF#("))
            last = next(i for i, ln in enumerate(body) if ln.startswith(f"MOVL C{r['last_step'] - 1:05d} "))
            self.assertTrue(body[last + 1].startswith("ARCOF AEF#("))
            self.assertEqual(r["weld_moves"], last - first + 1)

    def test_steps_of_real_pendant_jobs(self):

        team = passes.pass_table(None, text=TEAM2V2.read_text(encoding="latin-1"), v_unit_mm_per_min=60.0)
        self.assertEqual([(r["first_step"], r["last_step"], r["asf"]) for r in team],
                         [(6, 8, 1), (14, 15, 1), (22, 22, 1)])
        self.assertEqual(team[2]["travel_mm_min"], 3000.0)
        gaj = passes.pass_table(None, text=GAJ.read_text(encoding="latin-1"), v_unit_mm_per_min=60.0)
        self.assertEqual([(r["first_step"], r["last_step"], r["asf"], r["aef"]) for r in gaj],
                         [(4, 6, 12, 12), (10, 10, 12, 12), (13, 13, 13, 13)])

    def test_files(self):
        tp, origin, scene = setup()
        _, summary = emit(tp, origin, scene)
        rows = summary["pass_table"]
        parsed = list(csv.DictReader(io.StringIO(passes.to_csv(rows).decode())))
        self.assertEqual(list(parsed[0]), list(passes.FIELDS))
        self.assertEqual(len(parsed), 3)
        data = json.loads(passes.to_json(rows, job="PTEST"))
        self.assertEqual(data["passes"], rows)
        self.assertEqual(passes.digest(rows), {"count": 3, "asf": [12], "travel_mm_min": [198.0, 198.0]})

    def test_an_auto_cycle_job_carries_its_table(self):
        tp = wall(100, 3, 2)
        origin = cell_lc.snap_toolpath_to_platter(tp, 1014, -100)
        with patch.object(jbi, "_git_sha", return_value="pinned"):
            text, summary = emit_cycle_job(
                tp, cell_lc.pulse_solver(origin), name="CYCLE_C02", arc_file={0: 81, 1: 81, 2: 83},
                cycle=2, layers=(2, 2), step_mm=2, tail=None,
                scene=cell_lc.lc_scene(origin, training_block=False), torch=TORCH, bead_height_mm=2,
                platter=ATTESTED, date="2026/10/05 00:00", asf_attested=True)
        self.assertEqual([(r["layer"], r["asf"]) for r in summary["pass_table"]], [(2, 83)])
        self.assertIn("ARCON ASF#(83)", text)


def _csv(rows, *, steps=True):
    head = ["//ARC_MONDATA", "//DATE,:2026/10/05 12:00:00", "//JOB,:PTEST", "//SAMPLING,0.1sec",
            "CHANNEL,ATTRIBUTE,NAME,NO.,GRAPH,NUMBER,UNIT,MIN,MAX,OFFSET,BOLD LINE",
            "//CH1,AIN1,VOLTAGE,0,1,1,%/V,0.0,50.0,5.0,0", "//CH2,AIN2,CURRENT,0,1,1,A,0.0,500.0,0.0,0"]
    cols = ["CH1", "CH2"]
    if steps:
        head += ["//CH8,STEP,STEP NO.,0,0,1,,0,100,0.0,0", "//CH9,SPD,WELD SPEED,0,0,1,cm/min,0.0,3000.5,0.0,0",
                 "//CH10,FILE,WELD COND.NO,0,0,1,,0,100,0.0,0", "//CH11,LEN,WELD LENGTH,0,0,1,mm,0,100,0.0,0"]
        cols += ["CH8", "CH9", "CH10", "CH11"]
    body = [",".join(str(v) for v in row[:len(cols)]) for row in rows]
    return "\r\n".join(head + ["//DATA", ",".join(cols)] + body + ["//ARC_MONDATAEND", ""]).encode()


PLAN = [{"pass": 1, "layer": 0, "asf": 81, "first_step": 4, "last_step": 5, "travel_mm_min": 600.0, "expected_s": 1.0},
        {"pass": 2, "layer": 1, "asf": 83, "first_step": 9, "last_step": 9, "travel_mm_min": 600.0, "expected_s": 1.0}]


def _two_passes():
    off = [(0, 0, 3, 0, 1081, 0)] * 20
    first = [(20, 200, 4 if i < 5 else 5, 60, 81, i) for i in range(10)]
    first[6] = (0, 0, 5, 60, 81, 6)
    crater = [(15, 60, 0, 0, 1081, 10)] * 3
    second = [(18, 100, 9, 60, 85, i) for i in range(10)]
    return off + first + crater + off + second + off


class TestArcmonPerPass(unittest.TestCase):
    def test_the_real_sample_joins_teamv2s_last_pass_by_step(self):
        table = passes.pass_table(None, text=TEAM2V2.read_text(encoding="latin-1"), v_unit_mm_per_min=60.0)
        result = arcmon.per_pass(SAMPLE.read_bytes(), table)
        self.assertEqual(result["mode"], "step")
        self.assertEqual(result["warnings"], [])
        one, two, three = result["rows"]
        self.assertEqual((one["flags"], two["flags"]), ("not recorded", "not recorded"))
        rows = list(csv.reader(io.StringIO(SAMPLE.read_text())))
        data = [list(map(float, r)) for r in rows[rows.index(["//DATA"]) + 2:] if len(r) == 11]
        weld = [r for r in data if r[1] > 10 and r[7] == 22]
        self.assertEqual(three["weld_s"], round(len(weld) * 0.1, 3))
        self.assertEqual(three["arc_on_s"], 3.6)
        self.assertAlmostEqual(three["current_mean_a"], statistics.fmean(r[1] for r in weld))
        self.assertAlmostEqual(three["voltage_mean_v"], statistics.fmean(r[0] for r in weld))
        self.assertEqual((three["cond_no"], three["weld_length_mm"], three["weld_speed_mm_min"]), (1, 44.0, 3005.0))
        self.assertAlmostEqual(three["energy_kj_mm"], round(three["voltage_mean_v"] * three["current_mean_a"]
                                                            / 50.0 / 1000.0, 4))
        self.assertEqual(three["flags"], "")

    def test_steps_place_each_stretch_and_flag_what_is_off(self):
        result = arcmon.per_pass(_csv(_two_passes()), PLAN)
        self.assertEqual((result["mode"], result["job"], result["segments"]), ("step", "PTEST", 2))
        first, second = result["rows"]
        self.assertEqual(first["dropouts"], 1)
        self.assertEqual(first["flags"], "dropout")
        self.assertEqual(first["arc_on_s"], 1.2)
        self.assertEqual(first["current_mean_a"], 200.0)
        self.assertAlmostEqual(first["energy_kj_mm"], 20 * 200 / 10.0 / 1000.0)
        self.assertEqual(second["flags"], "ASF mismatch;low current")
        self.assertEqual(second["cond_no"], 85)

    def test_without_steps_the_nth_stretch_is_the_nth_pass(self):
        rows = _two_passes() + [(20, 150, 0, 0, 0, 0)] * 5
        result = arcmon.per_pass(_csv(rows, steps=False), PLAN)
        self.assertEqual(result["mode"], "order")
        self.assertIn("3 arc-on stretch(es) for 2 passes", result["warnings"][0])
        self.assertEqual([r["matched_by"] for r in result["rows"]], ["order", "order"])
        self.assertEqual(result["rows"][1]["current_mean_a"], 100.0)

    def test_order_mode_reads_only_welding_samples(self):

        rows = [(v, a, cond) for v, a, _, _, cond, _ in _two_passes()]
        head = ["//ARC_MONDATA", "//DATE,:2026/10/05 12:00:00", "//SAMPLING,0.1sec",
                "//CH1,AIN1,VOLTAGE,0,1,1,%/V", "//CH2,AIN2,CURRENT,0,1,1,A",
                "//CH10,FILE,WELD COND.NO,0,0,1,", "//DATA", "CH1,CH2,CH10"]
        body = "\r\n".join(head + [",".join(map(str, r)) for r in rows] + ["//ARC_MONDATAEND", ""]).encode()
        plan = [dict(p, asf=asf) for p, asf in zip(PLAN, (81, 85))]
        first, second = arcmon.per_pass(body, plan)["rows"]
        self.assertEqual((first["current_mean_a"], first["cond_no"], first["aef_no"]), (200.0, 81, 81))
        self.assertEqual(first["arc_on_s"], 1.2)
        self.assertEqual(first["flags"], "dropout")
        self.assertEqual(second["flags"], "low current")

        table = passes.pass_table(None, text=TEAM2V2.read_text(encoding="latin-1"), v_unit_mm_per_min=60.0)
        last = [dict(table[2], first_step=None, last_step=None)]
        row, = arcmon.per_pass(SAMPLE.read_bytes(), last)["rows"]
        self.assertEqual(row["matched_by"], "order")
        self.assertAlmostEqual(row["current_mean_a"], 296.05)
        self.assertEqual((row["cond_no"], row["aef_no"], row["weld_s"], row["arc_on_s"]), (1, 1, 1.0, 3.6))
        self.assertEqual(row["flags"], "")

    def test_strike_and_crater_only_do_not_become_welding_statistics(self):
        rows = [(15, 60, 0, 0, 1081, distance) for distance in (0, 2, 4)]
        result = arcmon.per_pass(_csv(rows), PLAN[:1])
        row, = result["rows"]
        self.assertIn("no welding samples in this file (strike/crater only)", result["warnings"])
        self.assertEqual(row["weld_s"], 0.0)
        for field in ("current_mean_a", "current_min_a", "current_max_a",
                      "voltage_mean_v", "voltage_min_v", "voltage_max_v",
                      "cond_no", "weld_speed_mm_min", "energy_kj_mm"):
            self.assertIsNone(row[field], field)
        self.assertEqual((row["arc_on_s"], row["arc_current_mean_a"],
                          row["arc_voltage_mean_v"], row["aef_no"], row["weld_length_mm"]),
                         (0.3, 60.0, 15.0, 81, 4.0))
        exported = next(csv.DictReader(io.StringIO(arcmon.passes_csv([row]).decode())))
        self.assertEqual((exported["current_mean_a"], exported["arc_current_mean_a"]), ("", "60.0"))

    def test_the_join_is_linear(self):
        import time
        plan = [{"pass": k + 1, "asf": 81, "first_step": 3 * k + 1, "last_step": 3 * k + 2,
                 "travel_mm_min": 600.0} for k in range(2000)]
        rows = [(20, 200 if i % 50 < 45 else 0, 3 * (i // 50) + 1, 60, 81, 1) for i in range(100_000)]
        started = time.perf_counter()
        result = arcmon.per_pass(_csv(rows), plan)
        self.assertLess(time.perf_counter() - started, 30.0)
        self.assertEqual(result["mode"], "step")
        self.assertEqual(sum(bool(r["matched_by"]) for r in result["rows"]), 2000)

    def test_steps_from_another_job_fall_back_to_order_with_a_warning(self):
        plan = [dict(p, first_step=p["first_step"] + 50, last_step=p["last_step"] + 50) for p in PLAN]
        result = arcmon.per_pass(_csv(_two_passes()), plan)
        self.assertEqual(result["mode"], "order")
        self.assertIn("right job", result["warnings"][0])

    def test_a_recorded_pass_with_no_arc(self):
        rows = [(0, 0, 9, 60, 83, 0)] * 10 + [(20, 200, 4, 60, 81, i) for i in range(10)]
        result = arcmon.per_pass(_csv(rows), PLAN)
        self.assertEqual([r["flags"] for r in result["rows"]], ["", "no arc"])

    def test_not_an_export(self):
        with self.assertRaisesRegex(ValueError, "not an arc monitor export"):
            arcmon.per_pass(b"a,b\n1,2\n", PLAN)


class TestRunArchive(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"

    def add(self, **kw):
        values = dict(source="workbench", operator="op@example.invalid", start_if_none=True, part_hint="WALL")
        values.update(kw)
        return runs.add_file(self.root, "ws", **values)

    def test_job_tables_and_the_arcmon_join_are_filed_and_sealed(self):
        text = TEAM2V2.read_bytes()
        table = passes.pass_table(None, text=text.decode("latin-1"), v_unit_mm_per_min=60.0)
        job = self.add(kind="WELD-JOB", body=text, detail="TEAM2V2", ext=".JBI",
                       sidecar={"job": "TEAM2V2", "passes": passes.digest(table)}, passes=table)
        record = runs.active(self.root, "ws")
        folder = self.root / "ws" / "_runs" / record["run"]
        stem = job[:-len(".JBI")]
        self.assertEqual(list(csv.DictReader(io.StringIO((folder / f"{stem}.passes.csv").read_text())))[2]["first_step"],
                         "22")
        self.assertEqual(json.loads((folder / f"{stem}.passes.json").read_text())["passes"], table)
        sidecar = json.loads((folder / f"{stem}.json").read_text())
        self.assertEqual(sidecar["passes"]["count"], 3)
        self.assertEqual(sidecar["passes"]["csv"], f"{stem}.passes.csv")
        kinds = {item["name"]: item["kind"] for item in record["files"]}
        self.assertEqual(kinds[f"{stem}.passes.csv"], "PASSES")

        body = SAMPLE.read_bytes()
        name = self.add(kind="ARCMON", body=body, detail="1-20251103-181340", ext=".CSV", source="robot",
                        start_if_none=False, arcmon_summary=arcmon.parse(SAMPLE.name, body))
        joined_name = name[:-len(".CSV")] + ".arcmon-passes.csv"
        joined = list(csv.DictReader(io.StringIO((folder / joined_name).read_text())))
        self.assertEqual([r["flags"] for r in joined], ["not recorded", "not recorded", ""])
        summary = runs.active(self.root, "ws")["arc_monitor"]["summaries"][-1]["passes"]
        self.assertEqual((summary["mode"], summary["file"], summary["rows_total"]), ("step", joined_name, 3))
        sealed = runs.finish(self.root, "ws", outcome="completed", by="op@example.invalid")
        names = {item["name"] for item in json.loads(
            (folder / f"{sealed['run']}_SEALED.json").read_text())["files"]}
        self.assertTrue({f"{stem}.passes.csv", f"{stem}.passes.json", joined_name} <= names)

    def test_derived_names_belong_to_their_parent_kind(self):
        run = "2026-10-05_WALL_r01"
        for name, ok in ((f"{run}_01_WELD-JOB_WALL.passes.csv", True),
                         (f"{run}_01_CYCLE-JOB_C01-WALL_C01.passes.json", True),
                         (f"{run}_02_ROS2-JOB_WALL.passes.csv", True),
                         (f"{run}_03_ARCMON_trace.arcmon-passes.csv", True),
                         (f"{run}_01_ATTACH_notes.passes.csv", False),
                         (f"{run}_01_PLATE-SENSE-JOB_P.passes.csv", False),
                         (f"{run}_01_WELD-JOB_WALL.arcmon-passes.csv", False),
                         (f"{run}_00_WELD-JOB_WALL.passes.csv", False),
                         (f"2026-10-05_OTHER_r01_01_WELD-JOB_WALL.passes.csv", False)):
            with self.subTest(name=name):
                self.assertEqual(runs.valid_file_name(run, name), ok)

    def test_a_join_that_cannot_be_made_still_files_the_monitor(self):
        table = [{"pass": 1, "first_step": 1, "last_step": 1}]
        self.add(kind="WELD-JOB", body=b"/JOB\r\n", detail="W", ext=".JBI", sidecar={"job": "W"}, passes=table)
        with patch.object(arcmon, "per_pass", side_effect=ValueError("broken")):
            name = self.add(kind="ARCMON", body=b"x", detail="t", ext=".CSV", source="robot",
                            arcmon_summary={"kind": "realtime-csv"})
        self.assertTrue(name.endswith("_ARCMON_t.CSV"))
        self.assertEqual(runs.active(self.root, "ws")["arc_monitor"]["summaries"][-1]["passes"],
                         {"error": "broken"})


class TestEndpoint(_Serving):


    def test_schedule_attestation_and_pass_headers(self):
        run_slice(dict(CLEAN), None, "local")
        base = "/api/jbi?clear=1&block=0&by=t&name=SCHED&arc_schedule=" + quote("0:81;1-:83")
        code, body, _ = self.get(base)
        self.assertEqual(code, 409, body[:300])
        self.assertEqual(json.loads(body)["reason"], "attestation")
        code, body, hdrs = self.get(base + "&asf_attest=1")
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        self.assertIn("ARCON ASF#(81)", text)
        self.assertIn("ARCON ASF#(83)", text)
        self.assertIn("' check ASF list + dry run", text)
        self.assertEqual(hdrs["X-Waam-Asf"], "81,83")
        self.assertEqual(hdrs["X-Waam-Pass-Count"], str(text.count("\r\nARCON ")))
        self.assertRegex(hdrs["X-Waam-Travel"], r"^\d+(\.\d+)?-\d+(\.\d+)?$")
        both = base + "&asf_attest=1&arc=12"
        short = "/api/jbi?clear=1&block=0&by=t&name=SCHED&asf_attest=1&arc_schedule=" + quote("0:81")
        for query in (both, short):
            with self.subTest(query=query):
                self.assertEqual(self.get(query)[0], 400)


class TestExportPage(unittest.TestCase):
    def test_the_schedule_is_echoed_and_never_carried_to_another_build(self):
        from waam_pipeline.studio import build_state, render_studio
        page = render_studio(build_state(wall(60, 3, 3.0), workbench=True, violations=[]))
        self.assertIn('" (by layer: "+schedule+")"', page)
        self.assertIn('"each ASF# in the layer schedule "+schedule', page)
        self.assertIn('out.wbArcSchedule="";', page)
        self.assertIn('placeholder="0:81;1-:83"', page)


class TestRunFiling(IsolatedServerCase):


    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"
        table = passes.pass_table(None, text=TEAM2V2.read_text(encoding="latin-1"), v_unit_mm_per_min=60.0)
        for target, name, value in (
                (server, "SAVES_DIR", self.root), (server, "_git_commit", lambda: "b" * 40),
                (server, "_auth", self.account_auth), (server._Handler, "_archive", lambda *a, **kw: "saved"),
                (server, "_STATES", {"local": {
                    "gen": 1, "toolpath": object(), "request": {"params": {"kind": "wall"}, "stl": None},
                    "emit": {"scene": object(), "origin": [0, 0, 0], "torch": object(), "part": "WALL",
                             "fixture": False, "bead": 3, "bead_height": 2, "margin": 5}}})):
            change = patch.object(target, name, value)
            change.start()
            self.addCleanup(change.stop)
        emitter = patch("waam_pipeline.emit.emit_job", return_value=(
            "/JOB\r\n//NAME WALL\r\n", {"positions": 2, "passes": 3, "asf": [1], "pass_table": table}))
        emitter.start()
        self.addCleanup(emitter.stop)
        self.table = table

    @staticmethod
    def account_auth(handler, _query):
        handler.waam_who = {"email": "operator@example.invalid", "outcome": "ok-sso"}
        return "local", None

    def test_export_writes_passes_csv_and_json(self):
        import urllib.request
        query = "arc=1&gen=1&name=WALL&clear=1&block=0&by=operator"
        with urllib.request.urlopen(self.base + "/api/jbi?" + query, timeout=30) as response:
            headers = dict(response.headers)
        self.assertEqual((headers["X-Waam-Pass-Count"], headers["X-Waam-Asf"]), ("3", "1"))
        record = runs.active(self.root, "local")
        folder = self.root / "local" / "_runs" / record["run"]
        stem = headers["X-Waam-Run"][:-len(".JBI")]
        self.assertEqual(json.loads((folder / f"{stem}.passes.json").read_text())["passes"], self.table)
        sidecar = json.loads((folder / f"{stem}.json").read_text())
        self.assertNotIn("pass_table", sidecar["summary"])
        self.assertEqual(sidecar["summary"]["asf"], [1])
        self.assertEqual(sidecar["passes"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
