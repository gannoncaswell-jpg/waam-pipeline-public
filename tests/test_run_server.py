import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests._support import IsolatedServerCase
from waam_pipeline import runs, server
from waam_pipeline.demos import demo_wall
from waam_pipeline.slicer import wall
from waam_pipeline.thermal_plan import DEFAULT, layer_passes


class TestRunServer(IsolatedServerCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "saves"
        self.patch(server, "SAVES_DIR", self.root)
        self.patch(server, "_STATES", {})
        self.patch(server, "_git_commit", lambda: "b" * 40)
        self.auth = self.patch(server, "_auth", self.account_auth)
        self.patch(server._Handler, "_archive", lambda *a, **kw: "saved")
        self.emitter = patch("waam_pipeline.emit.emit_job", return_value=(
            "/JOB\r\n//NAME WALL\r\n", {"positions": 2, "passes": 1}))
        self.emitter.start()
        self.addCleanup(self.emitter.stop)
        self.patch(server, "_STATES", {"local": {
            "gen": 1, "toolpath": Mock(layers=Mock(return_value=[0, 1])),
            "request": {"params": {"kind": "wall", "lh": "2", "token": "secret"}, "stl": None},
            "emit": {"scene": object(), "origin": [0, 0, 0], "torch": object(),
                     "part": "WALL", "fixture": False, "bead": 3, "bead_height": 2, "margin": 5},
        }})

    def patch(self, target, name, value):
        change = patch.object(target, name, value)
        change.start()
        self.addCleanup(change.stop)
        return change

    @staticmethod
    def account_auth(handler, _query):
        handler.waam_who = {"email": "operator@example.invalid", "outcome": "ok-sso"}
        return "local", None

    def request(self, route, params=None, *, method="GET", body=None, headers=None):
        query = "?" + urlencode(params) if params else ""
        req = urllib.request.Request(self.base + route + query, data=body, method=method,
                                     headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read(), dict(response.headers)

    def post(self, action, obj):
        code, raw, headers = self.request("/api/run/" + action, method="POST",
                                         body=json.dumps(obj).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        return code, json.loads(raw), headers

    def export(self, **overrides):
        query = dict(arc="12", gen="1", name="WALL", clear="1", block="0", by="operator")
        query.update(overrides)
        return self.request("/api/jbi", query)

    def thermal(self, params):
        return self.request("/api/thermal-plan", {"gen": str(server._state("local").get("gen")),
                                                   **params})

    def test_export_starts_run_and_records_sidecar_second_export_reuses_it(self):
        code, body, headers = self.export(run_name="wall 10", layers="1-1", interpass="20",
                                          folder="WAAM", clean_first="1")
        self.assertEqual(code, 200, body)
        active = runs.active(self.root, "local")
        folder = self.root / "local" / "_runs" / active["run"]
        name = headers["X-Waam-Run"]
        self.assertEqual(name, active["run"] + "_01_WELD-JOB_WALL.JBI")
        self.assertEqual((folder / name).read_bytes(), body)
        sidecar = json.loads((folder / name).with_suffix(".json").read_text())
        self.assertEqual(sidecar["params"], {"kind": "wall", "lh": "2"})
        self.assertEqual(sidecar["query"]["arc"], "12")
        self.assertEqual(sidecar["query"]["folder"], "WAAM")
        self.assertEqual(sidecar["query"]["clean_first"], "1")
        self.assertTrue(sidecar["query"]["platter"]["clear"])
        self.assertEqual(sidecar["summary"]["positions"], 2)
        self.assertEqual(sidecar["commit"], "b" * 40)
        code, _, second = self.export(run_name="ignored new name")
        self.assertEqual(code, 200)
        self.assertTrue(second["X-Waam-Run"].startswith(active["run"] + "_"))
        self.assertEqual(len(runs.active(self.root, "local")["files"]), 4)
        self.assertEqual(runs.active(self.root, "local")["name"], "wall 10")

    def test_part_is_preserved_once_per_distinct_hash(self):
        server._STATES["local"]["request"]["stl"] = b"original STL upload"
        self.assertEqual(self.export()[0], 200)
        self.assertEqual(self.export()[0], 200)
        files = runs.active(self.root, "local")["files"]
        self.assertEqual(sum(item["kind"] == "PART" for item in files), 1)
        server._STATES["local"]["request"]["stl"] = b"changed STL upload"
        self.assertEqual(self.export()[0], 200)
        files = runs.active(self.root, "local")["files"]
        self.assertEqual(sum(item["kind"] == "PART" for item in files), 2)

    def test_ros2_and_gcode_file_only_into_an_existing_run(self):
        class Job:
            @staticmethod
            def to_json():
                return '{"segments":[]}'

        with patch("waam_pipeline.emit.emit_ros2", return_value=(Job(), {"segments": 0, "points": 0})), \
                patch.object(server, "to_gcode", return_value="G21\n"):
            query = {"gen": "1", "clear": "1", "block": "0"}
            for route in ("ros2", "gcode"):
                code, body, headers = self.request("/api/" + route, query)
                self.assertEqual(code, 200, body)
                self.assertEqual(headers["X-Waam-Run"], "none")
                self.assertIsNone(runs.active(self.root, "local"))
            self.assertEqual(self.export()[0], 200)
            for route, kind in (("ros2", "ROS2-JOB"), ("gcode", "GCODE")):
                code, body, headers = self.request("/api/" + route, query)
                self.assertEqual(code, 200, body)
                self.assertIn(kind, headers["X-Waam-Run"])

            active = runs.active(self.root, "local")
            runs.finish(self.root, "local", outcome="not-run", by="operator@example.invalid")
            pointer = self.root / "local" / "_runs" / "active.json"
            pointer.write_text(json.dumps({"run": active["run"]}), encoding="utf-8")
            for route in ("ros2", "gcode"):
                code, body, headers = self.request("/api/" + route, query)
                self.assertEqual(code, 200, body)
                self.assertEqual(headers["X-Waam-Run"], "none")

    def test_var_dat_default_parse_only_and_purpose_preserves_raw_bytes(self):
        raw = b"//VAR\r\n\xff RAW controller bytes\r\n"
        with patch("waam_pipeline.var_dat.probe_records", return_value=[[1, 2, 3, 4, 5, 6]]) as parser:
            code, body, headers = self.request("/api/var-dat", method="POST", body=raw)
            self.assertEqual(code, 200, body)
            self.assertEqual(headers["X-Waam-Run"], "none")
            self.assertIsNone(runs.active(self.root, "local"))
            for purpose, kind in (("plate-corners", "PLATE-CORNERS-VAR"),
                                  ("plate-sense", "PLATE-SENSE-VAR"), ("layer-touch", "LAYER-TOUCH-VAR")):
                code, body, headers = self.request("/api/var-dat", {"purpose": purpose, "run_name": "plate one"},
                                                   method="POST", body=raw)
                self.assertEqual(code, 200, body)
                active = runs.active(self.root, "local")
                path = self.root / "local" / "_runs" / active["run"] / headers["X-Waam-Run"]
                self.assertEqual(path.read_bytes(), raw)
                self.assertIn(kind, path.name)
            parser.assert_called_with(raw.decode("latin-1"), 40, 4)
            self.assertEqual(active["part"], "PLATE-ONE")

    def test_every_var_dat_upload_is_kept_for_the_onedrive_copy(self):
        raw = b"//VAR\r\n raw\r\n"
        archive = Mock(return_value="saved")
        with patch("waam_pipeline.var_dat.probe_records", return_value=[[1, 2, 3, 4, 5, 6]]), \
                patch.object(server._Handler, "_archive", archive):
            for query in (None, {"purpose": "plate-corners"}, {"purpose": "layer-touch"}):
                code, body, _ = self.request("/api/var-dat", query, method="POST", body=raw)
                self.assertEqual(code, 200, body)
        self.assertEqual([(*call.args, call.kwargs["name"]) for call in archive.call_args_list],
                         [("local", "vardat", raw, name) for name in ("import", "plate-corners", "layer-touch")])
        archive.reset_mock()
        with patch("waam_pipeline.var_dat.probe_records", side_effect=ValueError("bad VAR")), \
                patch.object(server._Handler, "_archive", archive):
            self.assertEqual(self.request("/api/var-dat", method="POST", body=b"bad")[0], 400)
        archive.assert_not_called()

    def test_refused_export_and_failed_var_parse_file_nothing(self):
        self.assertEqual(self.export(arc="")[0], 400)
        self.assertEqual(self.export(run_name="../escape")[0], 400)
        with patch("waam_pipeline.var_dat.probe_records", side_effect=ValueError("bad VAR")):
            self.assertEqual(self.request("/api/var-dat", {"purpose": "plate-corners"},
                                          method="POST", body=b"bad")[0], 400)
        self.assertIsNone(runs.active(self.root, "local"))

    def test_filing_failure_never_breaks_download(self):
        for error in (OSError("disk full"), ValueError("limit")):
            with self.subTest(error=error), patch.object(runs, "add_file", side_effect=error):
                code, body, headers = self.export()
            self.assertEqual(code, 200)
            self.assertTrue(body.startswith(b"/JOB"))
            self.assertEqual(headers["X-Waam-Run"], "failed")

    def test_run_routes_require_an_account_and_guests_are_refused(self):
        routes = (("/api/run", "GET"), ("/api/run/cooling", "GET"), ("/run-sheet", "GET"),
                  ("/api/run/decay", "GET"), ("/api/thermal-plan", "GET"),
                  *(('/api/run/' + action, "POST")
                   for action in ("add", "temperature", "layer-row", "note", "finish")))
        self.auth.stop()
        self.patch(server, "_TOKEN_GATE", True)
        cfg = server.oidc.AuthConfig("local", "", "", "http://127.0.0.1")
        self.patch(server, "_refresh_auth", lambda: cfg)
        guest = server._new_guest()
        self.addCleanup(server.forget_guests)
        for route, method in routes:
            with self.subTest(route=route):
                self.assertNotIn(route, server.GUEST_ALLOWED)
                code, _, _ = self.request(route, method=method, body=b"{}" if method == "POST" else None,
                                           headers={"Cookie": f"{server.GUEST_COOKIE}={guest}"})
                self.assertEqual(code, 403)

    def test_cooling_fit_needs_two_paired_cycles_then_proposes_a_dwell(self):
        code, raw, _ = self.request("/api/run/cooling", {"setpoint": "150", "unit": "C"})
        self.assertEqual(code, 400)
        self.assertIn("at least 2", json.loads(raw)["error"])
        self.assertEqual(self.export()[0], 200)
        for cycle, before, after in ((1, 300.0, 200.0), (2, 350.0, 215.0)):
            runs.set_cycle(self.root, "local", {"cycle": cycle, "interpass_s": 90.0,
                                                "t_before": {"value_c": before}, "t_after": {"value_c": after}})
        code, raw, _ = self.request("/api/run/cooling", {"setpoint": "150", "unit": "C", "ambient": "20"})
        self.assertEqual(code, 200, raw)
        out = json.loads(raw)
        self.assertEqual(len(out["fit"]["used"]), 2)
        self.assertEqual(out["fit"]["ambient_c"], 20.0)
        self.assertEqual(out["proposal"]["start_c"], 350.0)
        self.assertGreater(out["proposal"]["seconds"], 90.0)
        self.assertEqual(self.request("/api/run/cooling", {"setpoint": "x", "unit": "C"})[0], 400)

    def test_token_without_email_cannot_file_or_use_run_routes(self):
        def token_auth(handler, _query):
            handler.waam_who = {"email": None}
            return "local", None

        self.patch(server, "_auth", token_auth)
        self.assertEqual(self.request("/api/run")[0], 403)
        self.assertEqual(self.request("/run-sheet")[0], 403)
        self.assertEqual(self.request("/api/run/decay")[0], 403)
        self.assertEqual(self.request("/api/thermal-plan", {"target_preweld": "150"})[0], 403)
        self.assertEqual(self.post("layer-row", {"layer": 1, "unit": "C"})[0], 403)
        self.assertEqual(self.export()[2]["X-Waam-Run"], "none")
        self.assertIsNone(runs.active(self.root, "local"))

    def test_thermal_plan_default_wall_fallback_and_bad_inputs(self):
        server._STATES["local"]["toolpath"] = wall(60.0, 3, 2.0)
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        query = {"target_preweld": "150", "max_peak": "400", "ambient": "30",
                 "unit": "C", "model": "default"}
        code, raw, headers = self.thermal(query)
        self.assertEqual(code, 200, raw)
        self.assertIn("no-store", headers["Cache-Control"])
        plan = json.loads(raw)
        self.assertEqual(plan["gen"], 1)
        self.assertTrue(plan["valid"])
        self.assertEqual(plan["reasons"], [])
        self.assertEqual(plan["model"]["C"], 661.0)
        self.assertEqual(plan["model"]["tau"], 430.0)
        self.assertEqual((plan["layer_dwell_s"], plan["bead_dwell_s"], plan["added_time_s"]),
                         (13.0, 0.0, 26.0))
        self.assertEqual(len(plan["rows"]), 3)
        self.assertAlmostEqual(plan["rows"][1]["start_c"], 92.10038291701088)
        fahrenheit = dict(query, target_preweld="302", max_peak="752", ambient="86", unit="F")
        self.assertEqual(json.loads(self.thermal(fahrenheit)[1])["layer_dwell_s"], 13.0)

        fallback = json.loads(self.thermal(dict(query, model="run"))[1])
        self.assertEqual(fallback["model"]["C"], 661.0)
        self.assertEqual(fallback["model"]["key"], "default")
        self.assertIn("no run is open", " ".join(fallback["model"]["notes"]))
        for bad in ({"target_preweld": "hot"}, {"target_preweld": "150", "unit": "K"},
                    {"target_preweld": "150", "max_peak": "nan"},
                    {"target_preweld": "150", "ambient": "inf"},
                    {"target_preweld": "150", "power_w": "0"},
                    {"target_preweld": "150", "model": "other"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.thermal(bad)[0], 400)

        code, raw, _ = self.request("/api/thermal-plan", dict(query, gen="0"))
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(raw)["reason"], "stale")
        self.assertEqual(self.request("/api/thermal-plan", query)[0], 409)

    def test_invalid_thermal_plan_is_returned_with_reasons(self):
        server._STATES["local"]["toolpath"] = wall(60.0, 2, 2.0)
        result = json.loads(self.thermal({
            "target_preweld": "20", "ambient": "30", "max_peak": "100",
        })[1])
        self.assertFalse(result["valid"])
        self.assertTrue(result["reasons"])
        self.assertIn("ambient", " ".join(result["reasons"]).lower())

    def test_thermal_plan_requires_a_slice(self):
        server._STATES["local"]["toolpath"] = None
        code, raw, _ = self.thermal({"target_preweld": "150"})
        self.assertEqual(code, 409)
        self.assertIn("Slice and check", json.loads(raw)["error"])

    def test_thermal_plan_fits_the_active_run(self):
        server._STATES["local"]["toolpath"] = wall(60.0, 3, 2.0)
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        self.assertEqual(self.export()[0], 200)
        readings = {str(seconds): 30 + 170 * math.exp(-seconds / 200)
                    for seconds in (20, 40, 60)}
        runs.set_layer_row(self.root, "local", {"layer": 0, "pass": None, "unit": "C",
                                                 "pre_weld": 50, "park_travel_s_est": 0,
                                                 "readings": readings})
        code, raw, _ = self.thermal({
            "target_preweld": "150", "ambient": "30", "model": "run"})
        self.assertEqual(code, 200, raw)
        model = json.loads(raw)["model"]
        self.assertEqual(model["key"], "run")
        self.assertAlmostEqual(model["tau"], 200.0, places=6)
        self.assertNotEqual(model["C"], 661.0)
        self.assertIn("Active run", model["source"])
        self.assertNotIn("using DEFAULT", " ".join(model["notes"]))

    def test_run_fit_uses_layer_heating_when_no_row_has_timed_decay(self):
        path = wall(60.0, 3, 2.0)
        server._STATES["local"]["toolpath"] = path
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        self.assertEqual(self.export()[0], 200)
        ambient, tau, capacity, power = 30.0, 240.0, 720.0, DEFAULT.power_w
        layers = layer_passes(path, travel_mm_min=175.0)
        for layer, start, seconds in zip(layers, (45.0, 80.0, 120.0), (8.0, 25.0, 60.0)):
            selected = layer["passes"]
            arc_s = math.fsum(row["arc_s"] for row in selected)
            wall_s = arc_s + math.fsum(row["gap_s"] for row in selected[:-1])
            decay = math.exp(-wall_s / tau)
            heated = (ambient + (start - ambient) * decay
                       + power * arc_s / wall_s * tau * (1.0 - decay) / capacity)
            reading = ambient + (heated - ambient) * math.exp(-seconds / tau)
            runs.set_layer_row(self.root, "local", {
                "layer": layer["layer"], "pass": None, "unit": "C", "pre_weld": start,
                "park_travel_s_est": 0, "readings": {str(seconds): reading},
            })

        code, raw, _ = self.thermal({
            "target_preweld": "150", "ambient": "30", "model": "run",
        })
        self.assertEqual(code, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["model"]["key"], "run")
        self.assertAlmostEqual(result["model"]["tau"], tau, places=5)
        self.assertAlmostEqual(result["model"]["C"], capacity, places=5)
        self.assertIn("fitted from layer heating only (no timed decay readings)",
                      result["model"]["notes"])
        self.assertTrue(result["valid"])

    def test_run_fit_uses_archived_bead_dwell_and_drops_unknown_timing(self):
        path = demo_wall(length_mm=60.0, layers=1, layer_height_mm=2.0,
                         feed_mm_min=120.0, thickness_mm=16.0, bead_width_mm=8.0)
        server._STATES["local"]["toolpath"] = path
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        code, _, headers = self.export(bead_dwell="300", interpass="30",
                                       interpass_every="1")
        self.assertEqual(code, 200)

        passes = layer_passes(path, travel_mm_min=175.0)[0]["passes"]
        arc_s = math.fsum(row["arc_s"] for row in passes)
        wall_s = math.fsum(row["arc_s"] + row["gap_s"] for row in passes) + 600.0
        tau, start = 200.0, 50.0
        decay = math.exp(-wall_s / tau)
        base = DEFAULT.ambient_c + (start - DEFAULT.ambient_c) * decay
        gain = DEFAULT.power_w * arc_s / wall_s * tau * (1.0 - decay)
        end = base + gain / DEFAULT.C_j_per_k
        readings = {str(seconds): DEFAULT.ambient_c + (end - DEFAULT.ambient_c)
                    * math.exp(-seconds / tau) for seconds in (20, 40, 60)}
        runs.set_layer_row(self.root, "local", {
            "layer": 0, "pass": None, "unit": "C", "pre_weld": start,
            "park_travel_s_est": 0, "readings": readings,
        })

        query = {"target_preweld": "150", "ambient": "30", "model": "run"}
        result = json.loads(self.thermal(query)[1])
        self.assertEqual(result["model"]["key"], "run")
        self.assertAlmostEqual(result["model"]["C"], DEFAULT.C_j_per_k, places=5)

        run = runs.active(self.root, "local")
        folder = self.root / "local" / "_runs" / run["run"]
        sidecar_path = (folder / headers["X-Waam-Run"]).with_suffix(".json")
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["query"]["bead_dwell"] = "unknown"
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
        fallback = json.loads(self.thermal(query)[1])
        self.assertEqual(fallback["model"]["key"], "default")
        self.assertIn("could not be reconstructed", " ".join(fallback["model"]["notes"]))

    def test_pass_one_fit_ends_at_the_first_pass_arc_off(self):
        path = demo_wall(length_mm=60.0, layers=1, layer_height_mm=2.0,
                         feed_mm_min=120.0, thickness_mm=16.0, bead_width_mm=8.0)
        server._STATES["local"]["toolpath"] = path
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        self.assertEqual(self.export(bead_dwell="180")[0], 200)
        first = layer_passes(path, travel_mm_min=175.0)[0]["passes"][0]
        self.assertGreater(first["gap_s"], 0)
        self.assertTrue(first["bead_dwell"])

        tau, start, arc_s = 200.0, 50.0, first["arc_s"]
        decay = math.exp(-arc_s / tau)
        base = DEFAULT.ambient_c + (start - DEFAULT.ambient_c) * decay
        gain = DEFAULT.power_w * tau * (1.0 - decay)
        end = base + gain / DEFAULT.C_j_per_k
        readings = {str(seconds): DEFAULT.ambient_c + (end - DEFAULT.ambient_c)
                    * math.exp(-seconds / tau) for seconds in (20, 40, 60)}
        runs.set_layer_row(self.root, "local", {
            "layer": 0, "pass": 1, "unit": "C", "pre_weld": start,
            "park_travel_s_est": 0, "readings": readings,
        })

        result = json.loads(self.thermal({
            "target_preweld": "150", "ambient": "30", "model": "run",
        })[1])
        self.assertEqual(result["model"]["key"], "run")
        self.assertAlmostEqual(result["model"]["C"], DEFAULT.C_j_per_k, places=5)

    def test_user_pass_numbers_select_the_requested_pass(self):
        for pass_rows, pass_number, expected in (
                ([{"arc_s": 11.0, "gap_s": 0.0, "bead_dwell": False}], 1, 11.0),
                ([{"arc_s": 11.0, "gap_s": 3.0, "bead_dwell": True},
                  {"arc_s": 22.0, "gap_s": 0.0, "bead_dwell": False}], 2, 22.0)):
            with self.subTest(pass_number=pass_number):
                run = {"layer_log": [{"layer": 0, "pass": pass_number,
                                       "unit": "C", "pre_weld": 50.0}]}
                decay = {"used": [{"layer": 0, "pass": pass_number, "t0_c": 100.0}]}
                timing = {(0, pass_number): {"bead_dwell_s": 30.0}}
                observed = server._thermal_observations(
                    run, [{"layer": 0, "passes": pass_rows}], decay, timing)
                self.assertEqual(len(observed), 1)
                self.assertEqual((observed[0]["arc_s"], observed[0]["wall_s"]),
                                 (expected, expected))

    def test_invalid_latest_export_does_not_fall_back_to_older_timing(self):
        server._STATES["local"]["toolpath"] = wall(60.0, 1, 2.0)
        server._STATES["local"]["request"]["params"]["travel"] = "175"
        self.assertEqual(self.export(bead_dwell="0")[0], 200)
        code, _, latest = self.export(bead_dwell="10")
        self.assertEqual(code, 200)
        run = runs.active(self.root, "local")
        folder = self.root / "local" / "_runs" / run["run"]
        sidecar_path = (folder / latest["X-Waam-Run"]).with_suffix(".json")
        sidecar = json.loads(sidecar_path.read_text())
        sidecar["query"]["bead_dwell"] = "invalid latest value"
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

        readings = {str(seconds): 30 + 170 * math.exp(-seconds / 200)
                    for seconds in (20, 40, 60)}
        runs.set_layer_row(self.root, "local", {
            "layer": 0, "pass": None, "unit": "C", "pre_weld": 50,
            "park_travel_s_est": 0, "readings": readings,
        })
        result = json.loads(self.thermal({
            "target_preweld": "150", "ambient": "30", "model": "run",
        })[1])
        self.assertEqual(result["model"]["key"], "default")
        notes = " ".join(result["model"]["notes"])
        self.assertIn("Layer 0 was left out", notes)
        self.assertIn("archived bead_dwell is not numeric", notes)

    def test_thermal_apply_trace_adds_one_comment_and_sidecar_fields(self):
        trace = {"thermal_target": "150", "thermal_peak": "400", "thermal_model": "default",
                 "thermal_c": "661", "thermal_tau": "430"}
        with patch("waam_pipeline.emit.emit_job", return_value=(
                "/JOB\r\n//NAME WALL\r\n", {"positions": 2, "passes": 1})) as emit_job:
            code, body, headers = self.export(**trace)
        self.assertEqual(code, 200, body)
        comments = emit_job.call_args.kwargs["comments"]
        thermal = [comment for comment in comments if comment.startswith("THERMAL PRE")]
        self.assertEqual(thermal, ["THERMAL PRE 150C PEAK 400C"])
        self.assertLessEqual(len(thermal[0]) + 2, 32)
        active = runs.active(self.root, "local")
        folder = self.root / "local" / "_runs" / active["run"]
        sidecar = json.loads((folder / headers["X-Waam-Run"]).with_suffix(".json").read_text())
        for key, value in trace.items():
            self.assertEqual(sidecar["query"][key], value)
        self.assertEqual(self.export(thermal_target="150")[0], 400)

    def test_attachments_require_open_run_and_arcmon_is_validated(self):
        query = {"kind": "attachment", "name": "photo.JPG"}
        code, body, _ = self.request("/api/run/add", query, method="POST", body=b"photo")
        self.assertEqual(code, 400)
        self.assertIn("No run is open.", json.loads(body)["error"])
        self.assertEqual(self.export()[0], 200)
        code, body, _ = self.request("/api/run/add", {"kind": "attachment", "name": "photo.final.JPG"},
                                     method="POST", body=b"photo")
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["name"].endswith("_ATTACH_photo.final.JPG"))
        for name, kind in (("script.exe", "attachment"), ("random.csv", "arcmon"), ("../bad.jpg", "attachment")):
            self.assertEqual(self.request("/api/run/add", {"kind": kind, "name": name},
                                          method="POST", body=b"nope")[0], 400)

    def test_finish_requires_arcmon_or_waiver_and_seals(self):
        self.assertEqual(self.export()[0], 200)
        self.assertEqual(self.post("temperature", {"value": 212, "unit": "F", "stage": "before-weld", "note": "IR"})[0], 200)
        self.assertEqual(self.post("note", {"text": "First bead"})[0], 200)
        self.assertEqual(self.post("finish", {"outcome": "completed"})[0], 400)
        active = runs.active(self.root, "local")
        code, result, _ = self.post("finish", {"outcome": "stopped", "arcmon_waiver": True,
                                              "height_mm": 4, "notes": "Stopped after two layers"})
        self.assertEqual(code, 200, result)
        self.assertIsNone(result["active"])
        finished = result["recent"][0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["data_class"], "preliminary")
        self.assertEqual(finished["arc_monitor"]["waiver"]["by"], "operator@example.invalid")
        folder = self.root / "local" / "_runs" / active["run"]
        self.assertTrue((folder / (active["run"] + "_SEALED.json")).is_file())
        self.assertEqual(self.post("note", {"text": "too late"})[0], 400)

    def test_arcmon_upload_allows_finish_without_waiver(self):
        self.assertEqual(self.export()[0], 200)
        raw = (ROOT / "docs/cell-data/arcmon/1-20251103-181340.CSV").read_bytes()
        code, body, _ = self.request("/api/run/add", {"kind": "arcmon", "name": "1-20251103-181340.CSV"},
                                     method="POST", body=raw)
        self.assertEqual(code, 200, body)
        active = runs.active(self.root, "local")
        self.assertEqual(active["arc_monitor"]["summaries"][0]["samples"], 300)
        self.assertEqual(self.post("finish", {"outcome": "completed", "data_class": "controlled"})[0], 200)

    def test_not_run_can_finish_without_arcmon(self):
        self.assertEqual(self.export()[0], 200)
        code, obj, _ = self.post("finish", {"outcome": "not-run"})
        self.assertEqual(code, 200, obj)
        self.assertIsNone(obj["recent"][0]["arc_monitor"]["waiver"])

    def test_json_validation_and_cross_site_posts(self):
        self.assertEqual(self.export()[0], 200)
        for action, obj in (("temperature", {"value": float("nan"), "unit": "F", "stage": "other"}),
                            ("temperature", {"value": 2600, "unit": "F", "stage": "other"}),
                            ("note", {"text": "x" * 2001}),
                            ("finish", {"outcome": "completed", "arcmon_waiver": "yes"}),
                            ("finish", {"outcome": "not-run", "data_class": "unknown"})):
            with self.subTest(action=action, obj=str(obj)[:60]):
                self.assertEqual(self.post(action, obj)[0], 400)
        self.assertEqual(self.request("/api/run/note", method="POST", body=b"x" * 16385)[0], 400)
        self.assertEqual(self.request("/api/run/note", method="POST", body=b"[]")[0], 400)
        self.assertEqual(self.request("/api/run/note", method="POST", body=b"{}",
                                      headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def sheet_export(self, stops, *, last=2):
        return runs.add_file(self.root, "local", kind="WELD-JOB", body=b"/JOB\r\n", detail="SHEET",
                             ext="JBI", source="workbench", start_if_none=True, operator="operator",
                             sidecar={"measure_stops": stops, "summary": {"layers": [0, last]}})

    def rewrite_layer_log(self, transform):
        record, folder = runs._open(self.root, "local")
        transform(record["layer_log"])
        (folder / f"{record['run']}_run.json").write_text(
            json.dumps(record), encoding="utf-8")

    def test_run_sheet_uses_latest_export_and_preserves_extra_measurements(self):
        self.sheet_export([{"after_layer": 1, "readings_s": [10], "park_travel_s_est": 3}])
        self.sheet_export([{"after_layer": 2, "readings_s": [20, 40, 60], "park_travel_s_est": 7.5}])
        runs.set_cycle(self.root, "local", {"cycle": 1, "layers": [0, 1], "status": "applied",
                                            "summary": {"growth_mm": 2.7}})
        runs.set_layer_row(self.root, "local", {"layer": 4, "pass": 2, "unit": "F", "note": "hand check",
                                                "readings": {"90": 200}, "height_mm": 3, "height_source": "manual"})
        record = runs.active(self.root, "local")
        rows, times = server._run_sheet_data("local", record)
        self.assertEqual([(row["layer"], row["pass"]) for row in rows],
                         [(0, None), (1, None), (2, None), (4, 2)])
        self.assertEqual(times, ["20", "40", "60", "90"])
        self.assertIsNone(rows[0]["park_travel_s_est"])
        self.assertIsNone(rows[1]["park_travel_s_est"])
        self.assertEqual(rows[2]["park_travel_s_est"], 7.5)
        self.assertEqual((rows[1]["height_mm"], rows[1]["height_source"]), (2.7, "sense"))
        self.assertTrue(rows[1]["height_unsaved"])
        self.assertEqual((rows[3]["height_mm"], rows[3]["height_source"]), (3, "manual"))
        code, body, headers = self.request("/run-sheet")
        self.assertEqual(code, 200, body)
        page = body.decode()
        for text in ("Run data sheet", "before this layer", "hand check", "7.5", "2.7", "sense", "90 s"):
            self.assertIn(text, page)
        self.assertIn("From sense, not saved", page)
        self.assertIn("Save sensed heights", page)
        self.assertNotIn("0 · Plate", page)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.request("/run-sheet", {"ws": "someone-else"})[0], 409)

    def test_sheet_before_and_after_readings_belong_to_the_same_zero_based_layer(self):
        self.sheet_export([{"after_layer": 0, "readings_s": [20, 40], "park_travel_s_est": 4},
                           {"after_layer": 1, "readings_s": [20, 40], "park_travel_s_est": 8}])
        for layer, pre_weld, after, park in ((0, 25, 200, 4), (1, 100, 250, 8)):
            row = {"layer": layer, "unit": "C", "pre_weld": pre_weld,
                   "readings": {"20": after, "40": after - 20}, "park_travel_s_est": park}
            self.assertEqual(self.post("layer-row", row)[0], 200)
            runs.set_cycle(self.root, "local", {"cycle": layer + 1, "layers": [layer, layer],
                                                "summary": {"growth_mm": 2.5 + layer}})
        record = json.loads(self.request("/api/run")[1])["active"]
        rows, times = server._run_sheet_data("local", record)
        self.assertEqual([row["layer"] for row in rows], [0, 1, 2])
        self.assertEqual(times, ["20", "40"])
        self.assertEqual([row["pre_weld"] for row in rows], [25, 100, None])
        self.assertEqual([row["readings"] for row in rows],
                         [{"20": 200, "40": 180}, {"20": 250, "40": 230}, {}])
        self.assertEqual([row["park_travel_s_est"] for row in rows], [4, 8, None])
        self.assertEqual([row["height_mm"] for row in rows], [2.5, 3.5, None])

    def test_sheet_seeds_unmeasured_layers_and_cycle_ranges_without_live_slice_state(self):
        self.sheet_export([], last=4)
        server._STATES.clear()
        rows, times = server._run_sheet_data("local", runs.active(self.root, "local"))
        self.assertEqual([row["layer"] for row in rows], list(range(5)))
        self.assertEqual(times, [])
        runs.add_file(self.root, "local", kind="CYCLE-JOB", body=b"/JOB\r\n", detail="C01-SHEET",
                      ext="JBI", source="workbench", sidecar={"cycle": {"first": 3, "last": 5},
                                                               "summary": {"layers": [3, 5]}})
        rows, _ = server._run_sheet_data("local", runs.active(self.root, "local"))
        self.assertEqual([row["layer"] for row in rows], list(range(6)))

    def test_layer_row_round_trip_decay_and_cooling_preference(self):
        self.sheet_export([{"after_layer": 1, "readings_s": [20, 40, 60]}])
        values = {str(t): 25 + 400 * math.exp(-(t + 12) / 180) for t in (20, 40, 60)}
        row = {"layer": 1, "pass": None, "unit": "C", "pre_weld": 70,
               "park_travel_s_est": 12, "readings": values, "height_mm": 2.5,
               "height_source": "sense", "width_mm": 7, "note": "decay"}
        code, result, headers = self.post("layer-row", row)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["active"]["layer_log"][0]["readings"], values)
        self.assertIn("no-store", headers["Cache-Control"])
        row["width_mm"] = 8
        self.assertEqual(self.post("layer-row", row)[0], 200)
        saved = json.loads(self.request("/api/run")[1])["active"]["layer_log"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["width_mm"], 8)
        code, raw, headers = self.request("/api/run/decay")
        self.assertEqual(code, 200, raw)
        fit = json.loads(raw)["fit"]
        self.assertAlmostEqual(fit["tau_s"], 180, places=7)
        self.assertEqual((fit["count"], fit["rows"][0]["layer"]), (1, 1))
        self.assertIn("no-store", headers["Cache-Control"])
        for cycle in (1, 2):
            runs.set_cycle(self.root, "local", {"cycle": cycle, "interpass_s": 60,
                                                "t_before": {"value_c": 400}, "t_after": {"value_c": 350}})
        code, raw, _ = self.request("/api/run/cooling", {"setpoint": "150"})
        self.assertEqual(code, 200, raw)
        proposal = json.loads(raw)
        self.assertEqual(proposal["basis"], "layer_decay")
        self.assertAlmostEqual(proposal["proposal"]["tau_used_s"], 180, places=7)
        self.assertTrue(any("Run data sheet" in note for note in proposal["proposal"]["notes"]))

    def test_sheet_refuses_bad_stale_and_cross_site_saves(self):
        self.sheet_export([])
        row = {"layer": 1, "unit": "C", "readings": {"20": float("nan")}}
        self.assertEqual(self.post("layer-row", row)[0], 400)
        for bad_pass in (0, -1, 1.5, True):
            with self.subTest(pass_number=bad_pass):
                self.assertEqual(self.post("layer-row", {"layer": 1, "pass": bad_pass,
                                                          "unit": "C"})[0], 400)
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://elsewhere.invalid"}):
            self.assertEqual(self.request("/api/run/layer-row", method="POST", body=b"{}", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/run/layer-row", {"ws": "someone-else"}, method="POST", body=b"{}")[0], 409)
        self.assertEqual(self.request("/api/run/layer-row", {"run": "old-run"}, method="POST", body=b"{}")[0], 409)
        self.assertEqual(self.request("/api/run/decay", {"run": "old-run"})[0], 409)
        self.assertEqual(self.request("/api/run/decay")[0], 400)
        self.assertEqual(runs.active(self.root, "local")["layer_log"], [])

    def test_sensed_height_is_marked_unsaved_until_posted(self):
        self.sheet_export([], last=1)
        runs.set_layer_row(self.root, "local", {"layer": 1, "pass": None, "unit": "C",
                                                 "pre_weld": 70.0})
        runs.set_cycle(self.root, "local", {"cycle": 1, "layers": [0, 1],
                                             "summary": {"growth_mm": 2.7}})
        rows, _ = server._run_sheet_data("local", runs.active(self.root, "local"))
        sensed = next(row for row in rows if row["layer"] == 1)
        self.assertTrue(sensed["height_unsaved"])
        self.assertIn("From sense, not saved", self.request("/run-sheet")[1].decode())

        payload = dict(sensed)
        payload.pop("at", None)
        payload.pop("height_unsaved")
        self.assertEqual(self.post("layer-row", payload)[0], 200)
        saved = next(row for row in runs.active(self.root, "local")["layer_log"]
                     if row["layer"] == 1 and row.get("pass") is None)
        self.assertEqual((saved["height_mm"], saved["height_source"]), (2.7, "sense"))
        self.assertIn("Saved", self.request("/run-sheet")[1].decode())

    def test_numbered_row_without_pass_base_is_refused_on_load(self):
        self.sheet_export([], last=0)
        readings = {"20": 180.0, "40": 150.0, "60": 130.0}
        runs.set_layer_row(self.root, "local", {
            "layer": 0, "pass": 1, "unit": "C", "readings": readings,
        })

        def remove_marker(rows):
            rows[0].pop("pass_base")

        self.rewrite_layer_log(remove_marker)
        with self.assertRaisesRegex(ValueError, "no pass_base: 1.*refusing"):
            runs.active(self.root, "local")
        code, raw, _ = self.request("/api/run")
        self.assertEqual(code, 400, raw)
        self.assertRegex(json.loads(raw)["error"], "no pass_base: 1.*refusing")

    def test_sheet_empty_and_saved_manual_height(self):
        code, raw, headers = self.request("/run-sheet")
        self.assertEqual(code, 200, raw)
        self.assertIn(b"No open run", raw)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.post("layer-row", {"layer": 0, "unit": "F", "pre_weld": 77})[0], 400)
        self.sheet_export([{"after_layer": 1, "readings_s": [60]}])
        runs.set_cycle(self.root, "local", {"cycle": 1, "layers": [0, 1], "summary": {"growth_mm": 2.7}})
        runs.set_layer_row(self.root, "local", {"layer": 1, "unit": "C", "height_mm": 3.1,
                                                "height_source": "manual"})
        rows, _ = server._run_sheet_data("local", runs.active(self.root, "local"))
        self.assertEqual((rows[1]["height_mm"], rows[1]["height_source"]), (3.1, "manual"))
        runs.finish(self.root, "local", outcome="not-run")
        self.assertEqual(self.post("layer-row", {"layer": 2, "unit": "C"})[0], 400)


class TestRunBodyBounds(unittest.TestCase):
    def test_length_is_checked_before_read_and_short_body_refused(self):
        handler = object.__new__(server._Handler)
        for length in ("bad", "0", "-1", str(25 * 1024 * 1024 + 1)):
            handler.headers = {"Content-Length": length}
            handler.rfile = io.BytesIO(b"must not read")
            with self.assertRaisesRegex(ValueError, "limit"):
                handler._read_run_body(25 * 1024 * 1024)
            self.assertEqual(handler.rfile.tell(), 0)
        handler.headers = {"Content-Length": "10"}
        handler.rfile = io.BytesIO(b"short")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            handler._read_run_body(16384)

    def test_an_unnamed_attestation_records_the_signed_in_account(self):
        query = {"clear": ["1"], "block": ["0"]}
        platter, error = server._platter_from_query(query, {"fixture": False}, "local",
                                                    "operator@example.invalid")
        self.assertIsNone(error)
        self.assertEqual(platter.attested_by, "operator@example.invalid")
        platter, _ = server._platter_from_query(query, {"fixture": False}, "local")
        self.assertEqual(platter.attested_by, "local", "a token link has only its workspace")

    def test_filing_uses_effective_request_and_actual_platter_attestation(self):
        handler = object.__new__(server._Handler)
        handler.waam_who = {"email": "operator@example.invalid"}
        handler.log_error = lambda *args: None
        query = {"arc": ["12"], "clear": ["1"], "block": ["0"], "by": ["reviewer"],
                 "run_name": ["typed name"], "k": ["secret-token"], "smooth": ["1"]}
        platter, error = server._platter_from_query(query, {"fixture": False}, "local")
        self.assertIsNone(error)
        state = {"local": {"request": {"params": {"lh": "2"}, "stl": b"part"}}}
        with patch.object(server, "_STATES", state), patch.object(server, "_git_commit", return_value="test"), \
                patch.object(runs, "add_file", return_value="stored.JBI") as add:
            result = handler._file_run("local", "WELD-JOB", b"job", name="WALL", ext=".JBI",
                                       query=query, start_if_none=True, platter=platter)
        self.assertEqual(result, "stored.JBI")
        self.assertEqual(add.call_count, 2)
        first, part = [call.kwargs for call in add.call_args_list]
        self.assertEqual(first["name"], "typed name")
        self.assertEqual(first["sidecar"]["params"], {"lh": "2"})
        self.assertEqual(first["sidecar"]["query"]["smooth"], "1")
        self.assertEqual(first["sidecar"]["query"]["platter"]["attested_by"], "reviewer")
        self.assertNotIn("secret-token", json.dumps(first["sidecar"]))
        self.assertEqual(part["kind"], "PART")
        self.assertEqual(part["body"], b"part")
        self.assertFalse(part["start_if_none"])

    def test_guest_and_unidentified_export_never_call_store(self):
        handler = object.__new__(server._Handler)
        with patch.object(runs, "add_file") as add:
            for who in ({"guest": True, "email": "x"}, {"email": None}):
                handler.waam_who = who
                self.assertEqual(handler._file_run("local", "WELD-JOB", b"job", name="WALL", ext=".JBI",
                                                   query={}, start_if_none=True), "none")
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
