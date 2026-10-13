import csv
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests import test_run_server as run_fixture
from tests._support import IsolatedServerCase
from tests.test_layer_sense import touches
from tests.test_layer_sense_silicon import var_text
from waam_pipeline import cell_lc, runs, server, slicer
from waam_pipeline.layer_sense import apply_auto, auto_plan, fit_layer_top


class TestAutoReplay(unittest.TestCase):
    def test_three_cycles_replay_the_same_plans_and_path(self):
        source = slicer.wall(80, 7, 2)
        origin = cell_lc.snap_toolpath_to_platter(source, 1014, -100)
        current, step, first, sets = source, 2, 0, []
        for layer, error in ((1, -0.2), (3, 0.15), (5, -0.1)):
            plan, hop = auto_plan(current, first, layer, step_mm=step, spacing_mm=20)
            self.assertEqual(hop, max(5, 0.5 * (layer - first + 1) * step))
            records = touches(plan, origin, lambda _x, e=error: e)
            top = fit_layer_top(current, layer, plan, records, origin=origin, bead_height_mm=step)
            current, info = apply_auto(current, top, first=first, last=layer, step_mm=step, bead_height_mm=2)
            sets.append({"layer": layer, "spacing": 20, "points": records, "step": True})
            first, step = layer + 1, info["growth_mm"]
        replay, meta, auto = server._replay_layer_senses(
            source, server._layer_senses(json.dumps(sets)), origin=origin, bead_height_mm=2)
        self.assertEqual(current.moves, replay.moves)
        self.assertEqual(auto, {"cycles_done": 3, "next_first": 6, "step_mm": step, "top_layer": 6})
        self.assertEqual([m["cycle"] for m in meta], [1, 2, 3])

    def test_step_is_boolean_and_manual_sets_cannot_mix(self):
        for value in (1, "true", None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "step must be"):
                server._layer_senses(json.dumps([{"layer": 1, "points": [], "step": value}]))
        with self.assertRaisesRegex(ValueError, "remove the manual"):
            server._layer_senses(json.dumps([{"layer": 1, "points": []}, {"layer": 3, "points": [], "step": True}]))


class TestAutoCycleServer(IsolatedServerCase):
    request = run_fixture.TestRunServer.request
    post = run_fixture.TestRunServer.post
    patch = run_fixture.TestRunServer.patch
    account_auth = staticmethod(run_fixture.TestRunServer.account_auth)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"
        self.patch(server, "SAVES_DIR", self.root)
        self.patch(server, "_STATES", {})
        self.patch(server, "_git_commit", lambda: "c" * 40)
        self.patch(server._Handler, "_archive", lambda *a, **kw: "saved")
        self.patch(server, "_auth", self.account_auth)
        self.patch(server, "_SHOW_CELL", True)
        self.params = {"kind": "wall", "shape": "wall", "wall_length": "80", "wall_layers": "6",
                       "lh": "2", "bead": "3", "travel": "175", "fixture": "0", "repair": "0"}
        self.slice()

    def slice(self, sets=None):
        if sets is not None:
            self.params["layer_sense"] = json.dumps(sets)
        result = server.run_slice(self.params, None, email="operator@example.invalid")
        self.assertTrue(result["ok"])
        self.gen = result["state"]["meta"]["gen"]
        return result

    def export(self, **overrides):
        query = {"gen": self.gen, "name": "AUTO", "arc": 12, "clear": 1, "block": 0,
                 "interpass": 20, "interpass_every": 2, "spacing": 20, "smooth": 1}
        query.update(overrides)
        return self.request("/api/cycle-job", query)

    def import_cycle(self, cycle, *, error=-0.2, **overrides):
        st = server._state("local")
        record = runs.active(self.root, "local")["cycles"][-1]
        plan, _hop = ([], 0) if record["last_cycle"] else auto_plan(st["toolpath"], *record["layers"], step_mm=record["step_mm"], spacing_mm=record["spacing_mm"])
        points = touches(plan, st["emit"]["origin"], lambda _x: error)
        base = record.get("sense_base", 44)
        hits = overrides.pop("hits", None if base == 44 else [1] * len(points))
        raw = var_text(points, base, hits).encode("ascii")
        query = {"gen": self.gen, "cycle": cycle, "t_before": 700, "t_after": 650, "unit": "F"}
        query.update(overrides)
        code, body, headers = self.request("/api/cycle-import", query, method="POST", body=raw)
        return code, json.loads(body), headers

    def apply(self, cycle, imported, sets):
        record = runs.active(self.root, "local")["cycles"][-1]
        flags = ({} if imported.get("hits") is None else
                 {"base": imported["base"], "flag_base": imported["flag_base"], "hits": imported["hits"]})
        sets.append({"layer": record["layers"][1], "spacing": record["spacing_mm"],
                     "points": imported["records"], "step": True, **flags})
        self.slice(sets)
        code, body, _ = self.request("/api/run/cycle-applied", {"cycle": cycle, "gen": self.gen}, method="POST")
        self.assertEqual(code, 200, body)

    def test_full_three_cycle_flow_temperature_hold_and_seal(self):
        sets = []
        code, body, headers = self.export(run_name="cycle wall")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Layers"], "0-1")
        self.assertEqual(headers["X-Waam-Cycle"], "1")
        first = runs.active(self.root, "local")["cycles"][0]
        folder = self.root / "local" / "_runs" / runs.active(self.root, "local")["run"]
        sidecar = json.loads((folder / first["file"]).with_suffix(".json").read_text())
        plan, _ = auto_plan(server._state("local")["toolpath"], 0, 1, step_mm=2, spacing_mm=20)
        self.assertEqual(sidecar["stations"], server._cycle_stations(plan))
        code, imported, _ = self.import_cycle(1)
        self.assertEqual(code, 200, imported)
        self.assertEqual(self.export()[0], 409)
        self.assertEqual(self.import_cycle(1)[0], 409)
        self.apply(1, imported, sets)
        code, body, _ = self.export(max_interpass=500, max_unit="F")
        self.assertEqual(code, 409, body)
        self.assertIn("650 F", json.loads(body)["error"])
        self.assertEqual(self.post("temperature", {"value": 250, "unit": "C", "stage": "after-interpass"})[0], 200)
        code, body, headers = self.export(max_interpass=500, max_unit="F")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Layers"], "2-3")
        self.assertEqual(self.import_cycle(1)[0], 409)
        code, imported, _ = self.import_cycle(2, error=0.1, t_after=400)
        self.assertEqual(code, 200, imported)
        self.apply(2, imported, sets)
        code, body, headers = self.export(max_interpass=500, max_unit="F")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Layers"], "4-5")
        self.assertEqual(headers["X-Waam-Last"], "1")
        self.assertNotIn(b"PAUSE", body)
        self.assertEqual(self.import_cycle(2)[0], 409)
        run = runs.active(self.root, "local")
        self.assertEqual([r["status"] for r in run["cycles"]], ["applied", "applied", "exported"])
        self.assertEqual([t["cycle"] for t in run["temperatures"]], [1, 1, None, 2, 2])
        self.assertEqual(sum(f["kind"] == "CYCLE-VAR" for f in run["files"]), 2)
        self.assertEqual(self.post("finish", {"outcome": "completed", "arcmon_waiver": True})[0], 200)
        rows = list(csv.DictReader(io.StringIO((folder / (run["run"] + "_cycles.csv")).read_text())))
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(float(rows[0]["growth_mm"]), 1.9, delta=0.01)

    def test_a_wall_two_beads_thick_runs_auto_cycles_on_both_beads(self):

        self.params.update(wall_thickness="15.875", lh="3.2", bead="9.5", wall_layers="4")
        self.slice()
        code, body, headers = self.export()
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Layers"], "0-1")
        record = runs.active(self.root, "local")["cycles"][0]
        plan, _hop = auto_plan(server._state("local")["toolpath"], 0, 1, step_mm=3.2, spacing_mm=20)
        self.assertEqual({p.pass_index for p in plan}, {0, 1})
        self.assertEqual(int(headers["X-Waam-Probes"]), len(plan))
        self.assertEqual(body.count(b"ARCON"), 4)
        code, imported, _ = self.import_cycle(1, error=-0.3)
        self.assertEqual(code, 200, imported)
        sets = []
        self.apply(1, imported, sets)
        code, body, headers = self.export()
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Layers"], "2-3")
        self.assertEqual(headers["X-Waam-Last"], "1")
        growth = runs.active(self.root, "local")["cycles"][0]["growth_mm"]
        self.assertAlmostEqual(growth, 3.2 - 0.3 / 2, delta=0.01)
        self.assertEqual(record["touches"], len(plan))

    def test_a_missed_station_is_flagged_left_out_and_replayed(self):
        code, body, headers = self.export(spacing=10)
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["X-Waam-Base"], "120")
        self.assertIn(b"///LVARS 1,0,0,0,0,0,0,0", body)
        record = runs.active(self.root, "local")["cycles"][0]
        self.assertEqual((record["sense_base"], record["flag_base"]), (120, 80))
        n = record["touches"]
        hits = [0 if i == 2 else 1 for i in range(n)]
        code, imported, _ = self.import_cycle(1, hits=hits)
        self.assertEqual(code, 200, imported)
        self.assertEqual((imported["base"], imported["flag_base"], imported["hits"]), (120, 80, hits))
        self.assertIsNone(imported["records"][2])
        self.assertEqual(imported["summary"]["missed"], ["P122"])
        self.assertEqual((imported["summary"]["touched"], imported["summary"]["planned"]), (n - 1, n))
        sets = []
        self.apply(1, imported, sets)
        cycle = runs.active(self.root, "local")["cycles"][0]
        self.assertEqual((cycle["status"], cycle["hits"]), ("applied", hits))
        self.assertEqual(self.export(spacing=10)[0], 200)

    def test_aborted_cycle_cannot_reuse_previous_cycle_flags(self):
        sets = []
        self.assertEqual(self.export()[0], 200)
        code, imported, _ = self.import_cycle(1)
        self.assertEqual(code, 200, imported)
        self.apply(1, imported, sets)
        self.assertEqual(self.export()[0], 200)
        record = runs.active(self.root, "local")["cycles"][-1]
        code, refused, _ = self.import_cycle(2, hits=[0] * record["touches"])
        self.assertEqual(code, 400, refused)
        self.assertIn("no station touched", refused["error"].lower())

    def test_a_cycle_exported_before_hit_flags_still_imports(self):

        self.assertEqual(self.export()[0], 200)
        record = runs.active(self.root, "local")["cycles"][0]
        record.pop("sense_base")
        record.pop("flag_base")
        runs.set_cycle(self.root, "local", record)
        code, imported, _ = self.import_cycle(1)
        self.assertEqual(code, 200, imported)
        self.assertEqual((imported["base"], imported["hits"]), (44, None))
        self.assertEqual(imported["summary"]["labels"][0], "P044")
        sets = []
        self.apply(1, imported, sets)
        self.assertNotIn("hits", sets[0])
        self.assertEqual(self.export()[0], 200)

    def test_changed_build_and_invalid_temperatures_file_nothing(self):
        self.assertEqual(self.export()[0], 200)
        for options in ({"t_before": ""}, {"t_after": "nan"}, {"unit": "K"}, {"t_after": 2600}):
            self.assertEqual(self.import_cycle(1, **options)[0], 400)
        before = runs.active(self.root, "local")
        st = server._state("local")
        st["emit"]["origin"] = st["emit"]["origin"] + np.array([1, 0, 0])
        code, obj, _ = self.import_cycle(1)
        self.assertEqual(code, 409, obj)
        self.assertIn("build changed since cycle 1", obj["error"])
        self.assertEqual(runs.active(self.root, "local")["files"], before["files"])

    def test_changed_station_plan_refuses(self):
        self.assertEqual(self.export()[0], 200)
        st = server._state("local")
        st["toolpath"] = replace(st["toolpath"], moves=[replace(m, z=m.z + 0.1) for m in st["toolpath"].moves])
        self.assertEqual(self.import_cycle(1)[0], 409)

    def test_account_build_generation_and_export_arguments(self):
        self.assertEqual(self.export(gen=0)[0], 409)
        for options in ({"arc": ""}, {"arc": 0}, {"name": "X" * 29}, {"spacing": "nan"}, {"interpass": 3601}):
            self.assertEqual(self.export(**options)[0], 400)
        self.assertIsNone(runs.active(self.root, "local"))
        st = server._state("local")
        for key, value in (("cell_profile", "hc10"), ("scene", None), ("bead_set", True)):
            with patch.dict(st["emit"], {key: value}):
                self.assertEqual(self.export()[0], 409)
        for name in ("_WALL", "-WALL"):
            self.assertEqual(self.export(name=name)[0], 400)
        code, body, headers = self.export(name="W_-1")
        self.assertEqual(code, 200, body)
        self.assertIn('W_-1_C01.JBI', headers["Content-Disposition"])
        def unidentified(handler, query):
            handler.waam_who = {"email": None}
            return "local", None
        with patch.object(server, "_auth", unidentified):
            self.assertEqual(self.export()[0], 403)
        def guest(handler, query):
            handler.waam_who = {"guest": True}
            return "guest-auto", None
        with patch.object(server, "_auth", guest):
            for route, method in (("/api/cycle-job", "GET"), ("/api/cycle-import", "POST"), ("/api/run/cycle-applied", "POST")):
                self.assertEqual(self.request(route, method=method)[0], 403)


if __name__ == "__main__":
    unittest.main()
