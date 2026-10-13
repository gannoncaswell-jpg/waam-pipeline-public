import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, server
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import ClearanceError, TorchModel
from waam_pipeline.sensed import CHORD_TOL_MM
from waam_pipeline.substrate import from_request
from waam_pipeline.toolpath import Toolpath
from tests._support import IsolatedServerCase


CX, CY = 1014.0, -180.0
STICKOUT = 8.92


def surface_points(xy):
    xy = np.asarray(xy, float)
    z = 160 + .015 * xy[:, 1] + 1.5e-4 * (xy[:, 0] - CX) ** 2
    return np.column_stack((xy, z))


def pulse_records(points):
    solve = cell_lc.pulse_solver(np.zeros(3))
    return [np.rint(np.asarray(solve(point)) * cell_lc.ar1440.PULSES_PER_DEG).astype(int).tolist()
            for point in points]


def registration(points=None):
    if points is None:
        xy = np.array([[-100, -60], [100, -60], [100, 60], [-100, 60]]) + [CX, CY]
        points = surface_points(xy)
    return {"version": 2, "frame": "lc50s_tool0_pulse",
            "weld_ctwd_mm": TorchModel.yaskawa_accufast_45().stackup.ctwd_mm,
            "gap_direction": "torch", "tool0_tip_offset_mm": STICKOUT,
            "corners": [{"pulses": record, "tip_distance_mm": STICKOUT}
                        for record in pulse_records(points)]}


class TestSenseServer(IsolatedServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = server
        cls.corners = registration()
        cls.raw = json.dumps(cls.corners)
        xy = np.array([[x, y] for y in np.linspace(CY - 45, CY + 45, 4)
                       for x in np.linspace(CX - 80, CX + 80, 5)])
        cls.sensed_raw = json.dumps(dict(cls.corners, version=3,
                                         sensed={"base": 44, "points": pulse_records(surface_points(xy))}))

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(self.server, "_STATES", {}).start()
        patch.object(self.server, "_SHOW_CELL", True).start()
        patch.object(self.server, "_HOSTED_ASSETS", False).start()
        patch("waam_pipeline.backends.jbi._git_sha", return_value="test").start()

    def request(self, route, params=None, *, method="GET", headers=None, body=None):
        query = "?" + urlencode(params) if params else ""
        req = urllib.request.Request(self.base + route + query, data=body, method=method,
                                     headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read(), dict(response.headers)

    def sense(self, **overrides):
        params = dict(substrate_registration=self.raw, fixture="0")
        params.update(overrides)
        return self.request("/api/sense-job", params)

    def slice(self, **overrides):
        params = dict(kind="wall", shape="wall", substrate_registration=self.sensed_raw,
                      lh="2", fixture="0")
        params.update(overrides)
        code, body, _ = self.request("/api/slice", params, method="POST", body=b"")
        self.assertEqual(code, 200, body[:300])
        return json.loads(body)

    def assert_refused(self, result, *, status=None, message=None):
        code, body, headers = result
        self.assertTrue(400 <= code < 500, body[:300])
        if status is not None:
            self.assertEqual(code, status, body[:300])
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        if message is not None:
            self.assertIn(message, payload["error"])
        self.assertNotIn(b"/JOB", body)
        self.assertNotIn("Content-Disposition", headers)

    def test_sense_job_is_parseable_crlf_and_uses_only_our_result_registers(self):
        with patch.object(server._Handler, "_archive", return_value="saved") as archive:
            code, body, headers = self.sense(name="sense_test")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(headers["Content-Type"], "text/plain; charset=us-ascii")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="SENSE_TEST.JBI"')
        self.assertEqual(headers["X-Waam-Archive"], "saved")
        archive.assert_called_once_with("local", "jbi", body, name="SENSE_TEST")
        self.assertIn(b"\r\n", body)
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))
        self.assertNotIn(b"\r\r\n", body)
        text = body.decode("ascii")
        self.assertIn("CALL JOB:CLEAN_R1", text)
        self.assertIn("SRCH RIN#(3)", text)
        self.assertNotIn("RIN#(4)", text)
        self.assertIn("TOUCH-SENSE PROBE - REVIEW REQ", text)
        self.assertIn("NOT auto-runnable", text)
        probes = int(headers["X-Waam-Probes"])
        self.assertGreaterEqual(probes, 2)
        self.assertLessEqual(probes, 21)
        self.assertEqual(headers["X-Waam-Base"], "44")
        results = [int(value) for value in re.findall(r"GETS PX(\d{3}) \$PX002", text)]
        self.assertEqual(results, list(range(44, 44 + probes)))
        registers = {int(value) for value in re.findall(r"(?<!\$)\bPX?(\d{3})\b", text)}
        self.assertEqual(registers, set(results))
        self.assertEqual(text.count("SRCH RIN#(3)"), probes)
        self.assertEqual(set(re.findall(r"DIS=(\S+)", text)), {"5"})
        self.assertIn(f"sense {probes} pts P044-P{43 + probes:03d}", text)
        self.assertIn("workspace local", text)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "SENSE_TEST.JBI"
            path.write_bytes(body)
            job = read_jbi(path)
        self.assertEqual(job.name, "SENSE_TEST")
        self.assertEqual(job.groups, ["RB1"])
        self.assertEqual(job.pos_type, "PULSE")
        positions = sum(key.startswith("C") for key in job.positions)
        self.assertEqual(job.npos[0], positions)

        self.assertEqual(positions, 4 + 2 * probes)
        self.assertIn("' hop 5 mm between touches", text)

    def test_sense_job_loads_into_the_requested_job_folder(self):
        code, body, _ = self.sense(name="folder_test", folder="WAAM")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(body.split(b"\r\n")[:3], [b"/JOB", b"//NAME FOLDER_TEST", b"///FOLDERNAME WAAM"])
        code, plain, _ = self.sense(name="folder_test")
        self.assertEqual(code, 200, plain[:300])
        self.assertNotIn(b"FOLDERNAME", plain)
        for bad in ("has space", "A" * 33):
            with self.subTest(folder=bad):
                self.assert_refused(self.sense(folder=bad), status=400, message="Controller job folder")

    def test_sense_uses_query_registration_without_a_slice_or_current_generation(self):
        pending = {"registration_pending": True, "gen": 987, "request": {"params": {}}}
        server._STATES["local"] = pending
        code, body, headers = self.sense(points="7", spacing="10", gen="1")
        self.assertEqual(code, 200, body[:300])
        self.assertLessEqual(int(headers["X-Waam-Probes"]), 7)
        self.assertIn(b"//NAME SENSE_PLATE\r\n", body)
        self.assertEqual(headers["X-Waam-Archive"], "not-requested")
        self.assertIs(server._STATES["local"], pending)
        self.assertEqual(pending, {"registration_pending": True, "gen": 987, "request": {"params": {}}})

    def test_path_sense_job_probes_current_wall_in_robot_frame(self):
        from waam_pipeline.sensed import sense_plan_along

        result = self.slice(substrate_registration=self.raw, wall_length="120", wall_layers="2",
                            ox="8", oy="6", yaw="15")
        self.assertTrue(result["ok"], result.get("error"))
        state = server._state("local")
        tp, em = state["toolpath"], state["emit"]
        plate = from_request(self.raw, 6.35)
        plan = sense_plan_along(plate, tp, em["origin"], spacing_mm=20)
        with patch.object(server._Handler, "_archive", return_value="saved") as archive:
            code, body, headers = self.sense(layout="path", gen=str(state["gen"]), name="wall_sense",
                                            folder="WAAM")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(int(headers["X-Waam-Probes"]), len(plan))
        self.assertEqual(headers["X-Waam-Base"], "44")
        archive.assert_called_once_with("local", "jbi", body, name="WALL_SENSE")
        job = read_jbi(body.decode("ascii"))
        self.assertEqual(job.folder, "WAAM")
        self.assertIn("CALL JOB:CLEAN_R1", job.instructions)
        self.assertIn("' hop 5 mm between touches", job.instructions)
        self.assertEqual(sum("SRCH RIN#(3)" in line for line in job.instructions), len(plan))
        targets = {}
        for line in job.instructions:
            match = re.fullmatch(r"SETE P(\d{3}) \(([1-6])\) (-?\d+)", line)
            if match:
                pvar, axis, pulse = map(int, match.groups())
                targets.setdefault(pvar, [None] * 6)[axis - 1] = pulse
        self.assertEqual(sorted(targets), list(range(44, 44 + len(plan))))
        actual = np.array([cell_lc.tcp_from_pulses(targets[key]) for key in sorted(targets)])
        np.testing.assert_allclose(actual, [probe.expected for probe in plan], atol=.03)
        weld = next(i for i, move in enumerate(tp.moves) if move.deposit)
        ends = np.asarray([tp.moves[weld - 1].target, tp.moves[weld].target]) + em["origin"]
        delta, line = actual[:, :2] - ends[0, :2], ends[1, :2] - ends[0, :2]
        length = np.linalg.norm(line)
        off = np.abs(delta[:, 0] * line[1] - delta[:, 1] * line[0]) / length
        self.assertLess(off.max(), .03)
        distances = delta @ (line / length)
        self.assertAlmostEqual(distances.min(), 0, delta=.03)
        self.assertAlmostEqual(distances.max(), length, delta=.03)
        np.testing.assert_allclose(actual[:, 2],
                                   plate.datum(actual[:, 0], actual[:, 1]) + plate.physical_shift_robot[2],
                                   atol=.03)

    def test_path_sense_requires_a_current_build_and_known_layout(self):
        self.assert_refused(self.sense(layout="bogus"), status=400, message="Sense layout")
        self.assert_refused(self.sense(layout="path", gen="1"), status=409, message="No build")
        result = self.slice(substrate_registration=self.raw, wall_layers="2")
        self.assertTrue(result["ok"], result.get("error"))
        state = server._state("local")
        for value in (None, "0", str(state["gen"] + 1)):
            with self.subTest(gen=value):
                params = {"layout": "path"}
                if value is not None:
                    params["gen"] = value
                reply = self.sense(**params)
                self.assert_refused(reply, status=409)
                self.assertEqual(json.loads(reply[1])["reason"], "stale")
        gen = str(state["gen"])
        for field, value, message in (("cell_profile", "hc10", "LC50S"),
                                       ("scene", None, "cell layer is off"),
                                       ("origin", None, "cell layer is off")):
            with self.subTest(field=field), patch.dict(state["emit"], {field: value}):
                self.assert_refused(self.sense(layout="path", gen=gen), status=409, message=message)
        with patch.dict(state, {"registration_pending": True}):
            self.assert_refused(self.sense(layout="path", gen=gen), status=409,
                                message="Substrate registration was not applied")
        with patch.dict(state, {"toolpath": Toolpath([move for move in state["toolpath"].moves if not move.deposit])}):
            self.assert_refused(self.sense(layout="path", gen=gen), status=409, message="no weld moves")

    def test_sense_refuses_another_workspace(self):
        self.assert_refused(self.sense(ws="another-workspace"), status=409)

    def test_guest_cannot_download_sense_jobs(self):
        self.assertNotIn("/api/sense-job", server.GUEST_ALLOWED)
        patch.object(server, "_TOKEN_GATE", True).start()
        cfg = server.oidc.AuthConfig("local", "", "", "http://127.0.0.1")
        patch.object(server, "_refresh_auth", return_value=cfg).start()
        gid = server._new_guest()
        self.addCleanup(server.forget_guests)
        headers = {"Cookie": f"{server.GUEST_COOKIE}={gid}"}
        code, body, _ = self.request("/api/whoami", headers=headers)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["guest"])
        result = self.request("/api/sense-job", {"substrate_registration": self.raw}, headers=headers)
        self.assert_refused(result, status=403)

    def test_registration_and_job_name_refusals(self):
        for raw in (json.dumps(dict(self.corners, version=1)), "{", "", "null"):
            with self.subTest(registration=raw[:60]):
                self.assert_refused(self.sense(substrate_registration=raw))
        self.assert_refused(self.sense(substrate_registration=self.sensed_raw),
                            message="Send the four-corner (version 2) record; sensed touches are replaced by a new sense run")
        for name in ("bad name", "../BAD", "A" * 33):
            with self.subTest(name=name):
                self.assert_refused(self.sense(name=name), status=400)

    def test_names_already_on_the_shared_controller_are_refused(self):
        from waam_pipeline.backends.jbi import _render_search_jbi, controller_job_names
        from waam_pipeline.sensed import sense_plan

        names = controller_job_names()

        self.assertTrue({"CLEAN_R1", "R1-TOUCH", "TEAM2V2", "WAAMPARAT"} <= names)
        self.assertFalse({"SENSE_PLATE", "WAAMJOB"} & names)
        for name in ("CLEAN_R1", "clean_r1", "team2v2"):
            with self.subTest(name=name):
                self.assert_refused(self.sense(name=name), status=409, message="already a job on the LC controller")
        self.slice()
        code, body, headers = self.request("/api/jbi", {"arc": "12", "name": "MANPRO_REV_2"})
        self.assert_refused((code, body, headers), status=409, message="already a job on the LC controller")

        plan = sense_plan(from_request(self.raw, 6.35))
        with self.assertRaisesRegex(ValueError, "would replace the CLEAN_R1"):
            _render_search_jbi(plan, cell_lc.pulse_solver(np.zeros(3)), name="CLEAN_R1",
                               date="2026/09/25 12:00", clean_job="CLEAN_R1")

    def test_invalid_sense_controls_are_plain_400_refusals(self):
        for key, values in (("points", ("22", "1", "2.5", "nan", "bad", "")),
                            ("spacing", ("5", "201", "nan", "bad", "")),
                            ("inset", ("4", "51", "inf", "bad", "")),
                            ("sub", ("-1", "101", "nan", "bad", "")),
                            ("fixture", ("2", "true", ""))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assert_refused(self.sense(**{key: value}), status=400)

    def test_emitter_refusals_remain_visible_and_unexpected_errors_are_logged(self):
        for error in (ClearanceError("probe approach fouls training_fixture"),
                      ValueError("probe is outside the robot reach")):
            with self.subTest(error=type(error).__name__), \
                    patch("waam_pipeline.backends.jbi.emit_search_jbi", side_effect=error):
                self.assert_refused(self.sense(), status=409, message=str(error))
        with patch("waam_pipeline.backends.jbi.emit_search_jbi", side_effect=RuntimeError("private details")), \
                patch.object(server, "_log_trace") as log:
            code, body, headers = self.sense()
        self.assertEqual(code, 500)
        self.assertFalse(json.loads(body)["ok"])
        self.assertIn("server logged", json.loads(body)["error"])
        self.assertNotIn(b"private details", body)
        self.assertNotIn(b"/JOB", body)
        self.assertNotIn("Content-Disposition", headers)
        log.assert_called_once_with("sense job")

    def assert_wall_seated(self, result):
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["unreachable"], 0)
        state = server._state("local")
        toolpath = state["toolpath"]
        plate = from_request(self.sensed_raw, 6.35)
        origin = np.asarray(state["emit"]["origin"])
        layer = min(move.layer for move in toolpath.moves if move.deposit)
        indices = {j for i, move in enumerate(toolpath.moves) if move.deposit and move.layer == layer
                   for j in (max(0, i - 1), i)}
        first = np.asarray([toolpath.moves[i].target for i in sorted(indices)]) + origin
        offset = first[:, 2] - plate.datum(first[:, 0], first[:, 1])
        self.assertGreaterEqual(offset.min(), -.05)
        self.assertLessEqual(offset.max(), .05 + CHORD_TOL_MM)
        self.assertGreater(np.ptp(first[:, 2]), .4)
        self.assertGreater(len(toolpath.layers()), 1)
        self.assertIn("sensed", result["state"]["meta"]["substrate"])
        self.assertIn("surface_setup", result["state"]["meta"]["substrate"])

    def test_sensed_wall_seats_through_slice_and_placement_requests(self):
        result = self.slice()
        self.assert_wall_seated(result)
        code, body, _ = self.request("/api/place", {"ox": "4", "oy": "3", "yaw": "12"},
                                     method="POST", body=b"")
        self.assertEqual(code, 200, body[:300])
        self.assert_wall_seated(json.loads(body))

    def test_interpolated_surface_seats_wall_through_slice(self):
        plate = from_request(self.raw, 6.35)
        xy = np.array([[CX - 60, CY], [CX, CY], [CX + 60, CY]])
        z = plate.datum(xy[:, 0], xy[:, 1]) + plate.physical_shift_robot[2] + [0, .8, 0]
        raw = json.dumps(dict(self.corners, version=3, sensed={
            "base": 44, "points": pulse_records(np.column_stack((xy, z))), "fit": "interpolate"}))
        result = self.slice(substrate_registration=raw, wall_length="120", wall_layers="2")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["unreachable"], 0)
        metadata = result["state"]["meta"]["substrate"]["sensed"]
        self.assertEqual(metadata["fit"], "interpolated")
        self.assertGreater(metadata["max_facet_slope_deg"], 0)
        self.assertLessEqual(metadata["max_facet_slope_deg"], 5)
        fitted = from_request(raw, 6.35)
        state = server._state("local")
        tp, origin = state["toolpath"], np.asarray(state["emit"]["origin"])
        for layer in (0, 1):
            indices = {j for i, move in enumerate(tp.moves) if move.deposit and move.layer == layer
                       for j in (i - 1, i)}
            vertices = np.array([tp.moves[i].target for i in sorted(indices)]) + origin
            np.testing.assert_allclose(vertices[:, 2] - fitted.datum(vertices[:, 0], vertices[:, 1]),
                                       2 * layer, atol=1e-8)
            self.assertGreater(np.ptp(vertices[:, 2]), .6)

    def test_four_corner_wall_seats_its_base_layer_on_the_corner_plane(self):
        result = self.slice(substrate_registration=self.raw, wall_layers="4", wall_length="120")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["unreachable"], 0)
        self.assertNotIn("sensed", result["state"]["meta"]["substrate"])
        state = server._state("local")
        toolpath, origin = state["toolpath"], np.asarray(state["emit"]["origin"])
        plate = from_request(self.raw, 6.35)
        self.assertEqual(len(toolpath.layers()), 4)
        self.assertAlmostEqual(toolpath.deposit_length(), 4 * 120.0, places=6)
        for layer in range(4):
            ends = np.asarray([toolpath.moves[j].target for i, move in enumerate(toolpath.moves)
                               if move.deposit and move.layer == layer for j in (i - 1, i)]) + origin
            np.testing.assert_allclose(ends[:, 2] - plate.datum(ends[:, 0], ends[:, 1]),
                                       2.0 * layer, atol=1e-6)

    def test_ruler_gap_registration_still_locates_one_bead_only(self):
        v1 = dict(self.corners, version=1)
        for key in ("gap_direction", "tool0_tip_offset_mm"):
            v1.pop(key)
        refused = self.slice(substrate_registration=json.dumps(v1))
        self.assertFalse(refused["ok"])
        self.assertIn("locates one bead only", refused["error"])

    def test_sensed_placement_keeps_cell_and_auto_correction_refusals(self):
        for params in ({"cell": "0"}, {"cell_profile": "hc10"}, {"correct": "1"}):
            with self.subTest(params=params):
                result = self.slice(**params)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_thermal_runs_on_flattened_sensed_wall_and_v2_still_refuses(self):
        from waam_pipeline.interpass import simulate

        with patch("waam_pipeline.interpass.simulate", wraps=simulate) as model:
            result = self.slice(thermal="illustrative", cooling="continuous")
        self.assert_wall_seated(result)
        self.assertIn(result["thermal"]["status"], ("complete", "stopped"))
        self.assertTrue(result["thermal"]["planning_only"])
        model.assert_called_once()
        flattened = model.call_args.args[0]
        actual = server._state("local")["toolpath"]
        np.testing.assert_allclose([move.target[:2] for move in flattened.moves],
                                   [move.target[:2] for move in actual.moves], atol=1e-10)
        for layer in {move.layer for move in flattened.moves if move.deposit}:
            heights = [move.z for move in flattened.moves if move.deposit and move.layer == layer]
            self.assertLess(np.ptp(heights), 1e-8)
        self.assertAlmostEqual(model.call_args.kwargs["substrate_z_mm"],
                               cell_lc.lowest_weld_z(flattened) - 2, places=8)
        refused = self.slice(shape="bead", substrate_registration=self.raw, thermal="illustrative")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "Turn interpass simulation off for the registered single-bead test")

    def test_stl_display_mesh_follows_registered_offsets_and_physical_datum(self):
        from examples.ornl_acceptance import block

        mesh = block(width=40, depth=24, height=6)
        body = bytearray(b"\0" * 80 + struct.pack("<I", len(mesh)))
        for tri in mesh:
            body += struct.pack("<12fH", 0, 0, 0, *tri.reshape(-1), 0)
        params = dict(kind="stl", units="mm", substrate_registration=self.sensed_raw,
                      lh="2", fixture="0", ox="15", oy="8", yaw="20")
        code, payload, _ = self.request("/api/slice", params, method="POST", body=bytes(body))
        self.assertEqual(code, 200, payload[:300])
        result = json.loads(payload)
        self.assertTrue(result["ok"], result.get("error"))
        shown = np.asarray(result["state"]["part"]["tris"]).reshape(-1, 3)
        plate = from_request(self.sensed_raw, 6.35)
        center = (shown[:, :2].min(0) + shown[:, :2].max(0)) / 2
        np.testing.assert_allclose(center, plate.basis[:2, :2] @ [15, 8], atol=.01)
        self.assertAlmostEqual(shown[:, 2].min(), plate.physical_shift_robot[2], delta=.01)
        self.assertAlmostEqual(np.ptp(shown[:, 2]), 6, delta=.01)


if __name__ == "__main__":
    unittest.main()
