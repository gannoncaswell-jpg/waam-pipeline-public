import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, server
from waam_pipeline.layer_sense import layer_sense_plan
from tests._support import IsolatedServerCase
from waam_pipeline import layer_sense as _layer_sense


def setUpModule():

    patcher = patch.object(_layer_sense, "RESEARCH_MODE", False)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def records(plan, origin, error):
    solve = cell_lc.pulse_solver(origin)
    return [np.rint(np.asarray(solve(tuple(p.expected + [0, 0, error(p.expected[0])])))
                    * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist() for p in plan]


class TestLayerSenseServer(IsolatedServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = server

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(self.server, "_STATES", {}).start()
        patch.object(self.server, "_SHOW_CELL", True).start()
        patch.object(self.server, "_HOSTED_ASSETS", False).start()
        patch("waam_pipeline.backends.jbi._git_sha", return_value="test").start()

    def request(self, route, params=None, *, method="GET", body=None):
        query = "?" + urlencode(params) if params else ""
        req = urllib.request.Request(self.base + route + query, data=body, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read(), dict(response.headers)

    def slice(self, **overrides):
        params = dict(kind="wall", shape="wall", lh="2", fixture="0")
        params.update(overrides)
        code, body, _ = self.request("/api/slice", params, method="POST", body=b"")
        self.assertEqual(code, 200, body[:300])
        return json.loads(body)

    def build(self):
        st = server._STATES["local"]
        return st["toolpath"], st["emit"]

    def touch_job(self, result, **overrides):
        params = dict(layer="1", spacing="20", gen=str(result["state"]["meta"]["gen"]))
        params.update(overrides)
        return self.request("/api/layer-sense-job", params)

    def test_layer_touch_job_probes_the_printed_top_and_is_part_checked(self):
        result = self.slice()
        code, body, _ = self.touch_job(result, plan="1")
        self.assertEqual(code, 200, body[:300])
        plan = json.loads(body)
        self.assertEqual((plan["base"], plan["flag_base"]), (120, 80))
        with patch.object(server._Handler, "_archive", return_value="saved"):
            code, body, headers = self.touch_job(result, name="wall_l1")
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="WALL_L1.JBI"')
        self.assertEqual(headers["X-Waam-Layer"], "1")
        self.assertEqual(headers["X-Waam-Base"], "120")
        self.assertEqual(headers["X-Waam-Flag-Base"], "80")
        n = int(headers["X-Waam-Probes"])
        tp, em = self.build()
        self.assertEqual(n, len(layer_sense_plan(tp, 1, bead_height_mm=em["bead_height"], spacing_mm=20)))
        self.assertIn("CALL JOB:CLEAN_R1", text)
        self.assertEqual(text.count("SRCH RIN#(3)"), n)
        results = [int(v) for v in re.findall(r"GETS PX(\d{3}) \$PX002", text)]
        self.assertEqual(results, list(range(120, 120 + n)))
        flags = [int(v) for v in re.findall(r"GETS B(\d{3}) \$B002", text)]
        self.assertEqual(flags, list(range(80, 80 + n)))
        self.assertIn(f"layer 1 top, {n} pts P120-P{119 + n:03d}", text)
        self.assertIn("' hop 5 mm between touches", text)
        self.assertEqual(int(re.search(r"///NPOS (\d+)", text).group(1)), 4 + 2 * n)
        self.assertNotIn("FOLDERNAME", text)
        code, body, _ = self.touch_job(result, name="wall_l1", folder="WAAM")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(body.split(b"\r\n")[:3], [b"/JOB", b"//NAME WALL_L1", b"///FOLDERNAME WAAM"])

    def test_manual_studio_import_and_saved_set_carry_flag_base(self):
        studio = re.sub(r"\s+", "", (ROOT / "src/waam_pipeline/studio.py").read_text(encoding="utf-8"))
        controls = re.sub(r"\s+", "", (ROOT / "src/waam_pipeline/studio_layer_sense.js").read_text(encoding="utf-8"))
        self.assertIn('flag_base:Number(r.headers.get("X-Waam-Flag-Base"))', studio)
        self.assertIn('flags:"1",flag_base:flagBase', studio)
        self.assertIn('env.importVarDat(awaitfileBody(chosen),plan.base,plan.count,"layer-touch",plan.flag_base)', controls)
        self.assertIn('base:plan.base,flag_base:plan.flag_base,hits:read.hits', controls)

    def test_a_bead_whose_window_is_not_whole_tenths_still_loads(self):

        result = self.slice(lh="1.7")
        with patch.object(server._Handler, "_archive", return_value="saved"):
            code, body, _ = self.touch_job(result, name="wall_l1")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(set(re.findall(r"\bDIS=(\S+)", body.decode("ascii"))), {"1.2"})

    def test_touch_job_refusals(self):
        result = self.slice()
        gen = str(result["state"]["meta"]["gen"])
        for params, status, message in (
                ({"layer": "99", "gen": gen}, 409, "no weld moves"),
                ({"layer": "1.5", "gen": gen}, 400, "Layer to touch"),
                ({"layer": "1", "spacing": "2", "gen": gen}, 400, "Layer touch spacing"),
                ({"layer": "1", "name": "CLEAN_R1", "gen": gen}, 409, "already a job"),
                ({"layer": "1", "folder": "has space", "gen": gen}, 400, "Controller job folder"),
                ({"layer": "1", "gen": "0"}, 409, None)):
            with self.subTest(params=params):
                code, body, _ = self.request("/api/layer-sense-job", params)
                self.assertEqual(code, status, body[:300])
                self.assertNotIn(b"/JOB", body)
                if message:
                    self.assertIn(message, json.loads(body)["error"])
        top = max(m.layer for m in self.build()[0].moves)
        code, body, _ = self.request("/api/layer-sense-job", {"layer": str(top), "gen": gen})
        self.assertEqual(code, 409)
        self.assertIn("last layer", json.loads(body)["error"])

    def test_imported_touches_reseat_the_rest_and_the_print_resumes_by_layer(self):
        self.slice()
        tp, em = self.build()
        plan = layer_sense_plan(tp, 1, bead_height_mm=em["bead_height"], spacing_mm=20)
        touches = records(plan, em["origin"], lambda x: -0.25)
        sets = json.dumps([{"layer": 1, "spacing": 20, "points": touches}])
        result = self.slice(layer_sense=sets)
        meta = result["state"]["meta"]["layer_sense"]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0]["layer"], 1)
        self.assertAlmostEqual(meta[0]["mean_error_mm"], -0.25, delta=0.02)
        seated, em = self.build()
        layer2 = [m for m in seated.moves if m.layer == 2 and m.deposit]
        for probe in plan:
            vertex = min(layer2, key=lambda m: abs(m.x - probe.expected[0]))
            self.assertAlmostEqual(vertex.x, probe.expected[0], places=6)
            self.assertAlmostEqual(vertex.z, probe.expected[2] - 0.25, delta=0.02)
        self.assertEqual(result["state"]["meta"]["params"]["layer_sense"], sets)
        gen = str(result["state"]["meta"]["gen"])
        jbi = dict(arc="12", name="RESUME", clear="1", block="0", gen=gen)
        code, body, headers = self.request("/api/jbi", dict(jbi, layers="2-3"))
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(sum(line.startswith("ARCON") for line in body.decode().splitlines()), 2)
        for bad in ("3-2", "all", "2-99"):
            with self.subTest(layers=bad):
                code, body, _ = self.request("/api/jbi", dict(jbi, layers=bad))
                self.assertIn(code, (400, 409), body[:300])
                self.assertNotIn(b"/JOB", body)

    def test_layer_touch_sets_keep_their_refusals(self):
        self.slice()
        tp, em = self.build()
        plan = layer_sense_plan(tp, 1, bead_height_mm=em["bead_height"], spacing_mm=20)
        good = records(plan, em["origin"], lambda x: 0.0)
        for sets, message in (
                ("not json", "not valid JSON"),
                (json.dumps({"layer": 1}), "list of 1 to 50"),
                (json.dumps([{"layer": 2, "points": good}, {"layer": 1, "points": good}]), "increasing"),
                (json.dumps([{"layer": 1, "spacing": 1, "points": good}]), "Layer touch spacing"),
                (json.dumps([{"layer": 1, "spacing": 20, "points": good[:-1]}]), "touch records"),
                (json.dumps([{"layer": 1, "spacing": 30, "points": good}]), "touch records|planned station")):
            with self.subTest(sets=sets[:40]):
                code, body, _ = self.request("/api/slice", dict(kind="wall", shape="wall", lh="2", fixture="0",
                                                                layer_sense=sets), method="POST", body=b"")
                payload = json.loads(body)
                self.assertFalse(payload["ok"])
                self.assertRegex(payload["error"], message)
        sets = json.dumps([{"layer": 1, "points": good}])
        for extra in ({"correct": "1"}, {"cell_profile": "hc10"}):
            with self.subTest(extra=extra):
                code, body, _ = self.request("/api/slice", dict(kind="wall", shape="wall", lh="2", fixture="0",
                                                                layer_sense=sets, **extra), method="POST", body=b"")
                self.assertFalse(json.loads(body)["ok"])

    def test_guests_cannot_download_layer_touch_jobs(self):
        self.assertNotIn("/api/layer-sense-job", server.GUEST_ALLOWED)


if __name__ == "__main__":
    unittest.main()
