import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import server
from waam_pipeline.emit import PlatterState
from waam_pipeline import cell_lc, emit
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import CONTACT_OK, blocking, check
from waam_pipeline.server import _state, run_slice
from waam_pipeline.toolpath import Toolpath
from tests._support import IsolatedServerCase

FIXED_DATE = "2026/09/04 09:00"


_P0 = PlatterState(clear=True, training_block_on=False, attested_by="t")

CLEAN = {"kind": "wall", "shape": "wall", "lh": "2.0", "name": "endpoint test"}


TALL = {"kind": "wall", "shape": "wall", "lh": "3.0", "name": "tall wall"}


class _Serving(IsolatedServerCase):


    def get(self, path):


        if path.startswith(("/api/jbi?", "/api/ros2?")) and "gen=" not in path:
            path += f"&gen={_state('local').get('gen')}"
        try:
            r = urllib.request.urlopen(self.base + path)
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)


class TestEndpointMatchesTheScript(_Serving):
    def test_browser_job_is_byte_identical_to_a_direct_emit(self):


        run_slice(dict(CLEAN), None, "local")
        st = _state("local")
        em = st["emit"]

        with mock.patch.object(emit, "_now", return_value=FIXED_DATE):
            expected, summary = emit.emit_job(
                st["toolpath"],
                cell_lc.pulse_solver(em["origin"]),
                bead_height_mm=em["bead_height"], name="BYTETEST", platter=_P0,
                arc_file=12,
                scene=em["scene"],
                torch=em["torch"],
                bead_width_mm=em["bead"],
                margin_mm=max(em["margin"], 5.0),
                comments=(
                    "waam-pipeline workbench",
                    f"part {str(em['part'])[:24]}",
                    "check ASF#12 + dry run",
                ),
                provenance=cell_lc.calibration_summary() + (
                    f"origin {em['origin'][0]:.0f},{em['origin'][1]:.0f}",
                    "workspace local",
                ),
            )
            code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=BYTETEST")

        self.assertEqual(code, 200, body[:300])
        self.assertEqual(body.decode("ascii"), expected)
        self.assertGreater(summary["positions"], 2)

    def test_the_job_folder_adds_one_header_line_and_nothing_else(self):
        run_slice(dict(CLEAN), None, "local")
        base = "/api/jbi?clear=1&block=0&by=t&arc=12&name=FOLDERED"
        with mock.patch.object(emit, "_now", return_value=FIXED_DATE):
            _, plain, _ = self.get(base)
            code, body, _ = self.get(base + "&folder=WAAM")
            unfiled = [self.get(base + "&folder=" + folder)[1] for folder in ("", "NONE")]
        self.assertEqual(code, 200, body[:300])
        lines = plain.decode("ascii").split("\r\n")
        self.assertEqual(body.decode("ascii").split("\r\n"), lines[:2] + ["///FOLDERNAME WAAM"] + lines[2:])
        self.assertEqual(unfiled, [plain, plain])
        for bad in ("WAAM%20FOLDER", "A" * 33, "..%2Fx"):
            with self.subTest(folder=bad):
                code, body, _ = self.get(base + "&folder=" + bad)
                self.assertEqual(code, 400, body[:300])
                self.assertIn("Controller job folder", json.loads(body)["error"])

    def test_the_served_job_parses_back(self):
        run_slice(dict(CLEAN), None, "local")
        code, body, hdrs = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=PARSEME")
        self.assertEqual(code, 200, body[:300])
        self.assertIn("attachment", hdrs.get("Content-Disposition", ""))

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "PARSEME.JBI"
            p.write_bytes(body)
            job = read_jbi(p)
        self.assertEqual(job.name, "PARSEME")
        self.assertEqual(job.groups, ["RB1"])
        self.assertEqual(job.pos_type, "PULSE")
        n_c = sum(1 for k in job.positions if k.startswith("C"))
        self.assertEqual(job.npos[0], n_c)
        on = sum(1 for ln in job.instructions if ln.startswith("ARCON"))
        off = sum(1 for ln in job.instructions if ln.startswith("ARCOF"))
        self.assertEqual(on, off)
        self.assertGreater(on, 0)

        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))

    def test_smooth_is_opt_in_for_cached_pages_and_a_straight_wall_keeps_its_moves(self):
        run_slice(dict(CLEAN), None, "local")
        base = "/api/jbi?clear=1&block=0&by=t&arc=12&name=SMOOTH"
        with mock.patch.object(emit, "_now", return_value=FIXED_DATE):
            responses = [self.get(base + query) for query in ("", "&smooth=0", "&smooth=1")]
        for code, body, headers in responses:
            self.assertEqual(code, 200, body[:300])
            self.assertEqual(headers["X-Waam-Smooth"], "0,0")
        plain, disabled, enabled = [body for _, body, _ in responses]
        self.assertEqual(plain, disabled)
        self.assertNotIn(b"' smooth:", plain)
        self.assertIn(b"' smooth: 0 arcs, 0 PL=1", enabled)
        self.assertEqual(b"\r\n".join(line for line in enabled.split(b"\r\n")
                                     if not line.startswith(b"' smooth:")), plain)

    def test_smooth_header_counts_arcs_and_blends_from_the_emitted_lines(self):
        run_slice(dict(CLEAN), None, "local")
        text = "\r\n".join((
            "' smooth: 99 arcs, 99 PL=1",
            "MOVL C00000 V=10.0 PL=1", "MOVL C00001 V=10.0 PL=1", "MOVL C00002 V=10.0",
            "MOVC C00003 V=10.0", "MOVC C00004 V=10.0", "MOVC C00005 V=10.0 FPT",
            "MOVC C00006 V=10.0", "MOVC C00007 V=10.0", "MOVL C00008 V=10.0",
            "MOVC C00009 V=10.0", "MOVC C00010 V=10.0", "MOVC C00011 V=10.0", "",
        ))
        base = "/api/jbi?clear=1&block=0&by=t&arc=12&name=COUNTS"
        for query, enabled in (("", False), ("&smooth=0", False), ("&smooth=1", True)):
            with self.subTest(query=query), mock.patch.object(
                    emit, "emit_job", return_value=(text, {"positions": 12, "passes": 1})) as exporter:
                code, body, headers = self.get(base + query)
                self.assertEqual(code, 200, body[:300])
                self.assertIs(exporter.call_args.kwargs["smooth"], enabled)
                self.assertEqual(headers["X-Waam-Smooth"], "3,2")
                self.assertEqual(body, text.encode("ascii"))

    def test_a_wall_taller_than_the_weld_zone_exclusion_emits(self):


        result = run_slice(dict(TALL), None, "local")
        self.assertEqual(result["blocking"], 0, "tall wall refused by the check")
        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=TALLWALL")
        self.assertEqual(code, 200, body[:300])
        self.assertIn("//NAME TALLWALL", body.decode("ascii"))

    def test_the_job_names_the_arc_file_and_the_check_in_its_provenance(self):
        run_slice(dict(CLEAN), None, "local")
        _, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=PROV")
        text = body.decode("ascii")
        self.assertIn("ASF#/AEF# 12", text)
        self.assertIn("clearance yes", text)
        self.assertNotIn("UNCHECKED", text)


class TestEndpointRefusals(_Serving):
    def test_arc_file_is_required_and_has_no_default(self):

        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/jbi")
        self.assertEqual(code, 400)
        self.assertIn("ASF#: enter the number shown on the controller pendant", json.loads(body)["error"])

        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=notanumber")
        self.assertEqual(code, 400)


        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=99999")
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["reason"], "argument")

    def test_a_fouling_part_is_refused_and_the_obstacle_is_named(self):


        result = run_slice(dict(CLEAN, oy="-120", fixture="1"), None, "local")
        self.assertGreater(result["blocking"], 0, "expected the block to foul")

        code, body, _ = self.get("/api/jbi?clear=1&block=1&by=t&arc=12&name=FOULED")
        self.assertEqual(code, 409)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "clearance")
        self.assertIn("manpro_training_block", payload["error"])
        self.assertIn("mm", payload["error"])

    def test_cell_layer_off_refuses_rather_than_emitting_an_unchecked_job(self):

        run_slice(dict(CLEAN, cell="0"), None, "local")
        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=NOCELL")
        self.assertEqual(code, 409)
        self.assertIn("cell layer is off", json.loads(body)["error"])

    def test_job_name_must_be_pendant_legal(self):
        run_slice(dict(CLEAN), None, "local")
        for bad in ("has%20space", "has.dot", "-leading", "X" * 40, "a%2Fb"):
            code, _, _ = self.get(f"/api/jbi?clear=1&block=0&by=t&arc=12&name={bad}")
            self.assertEqual(code, 400, f"{bad!r} should be refused")

    def test_a_lower_case_name_is_normalised_not_refused(self):


        run_slice(dict(CLEAN), None, "local")
        code, body, hdrs = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=lowerjob")
        self.assertEqual(code, 200)
        self.assertIn("LOWERJOB.JBI", hdrs.get("Content-Disposition", ""))
        self.assertIn("//NAME LOWERJOB", body.decode("ascii"))


class TestJbiRouteIsBehindTheGate(_Serving):


    def setUp(self):
        self._saved = server._TOKENS
        server._TOKENS = {"tok-emit-test-0001": "local"}

    def tearDown(self):
        server._TOKENS = self._saved

    def test_no_token_is_denied(self):
        code, _, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=SNEAKY")
        self.assertEqual(code, 401)
        code, _, _ = self.get("/api/ros2?clear=1&block=0&by=t&name=SNEAKY")
        self.assertEqual(code, 401)

    def test_wrong_token_is_denied(self):
        code, _, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=SNEAKY&k=nope")
        self.assertEqual(code, 401)
        code, _, _ = self.get("/api/ros2?clear=1&block=0&by=t&name=SNEAKY&k=nope")
        self.assertEqual(code, 401)

    def test_the_right_token_gets_through_to_the_normal_gates(self):
        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=OK1&k=tok-emit-test-0001")
        self.assertEqual(code, 200, body[:200])


class TestTheWorkspaceGeneration(_Serving):


    def test_an_export_from_a_superseded_page_is_refused(self):
        run_slice(dict(CLEAN), None, "local")
        stale = _state("local")["gen"]
        run_slice(dict(TALL), None, "local")
        for path in (f"/api/jbi?clear=1&block=0&by=t&arc=12&name=STALE&gen={stale}",
                     f"/api/ros2?clear=1&block=0&by=t&name=STALE&gen={stale}"):
            with self.subTest(path=path.split("?")[0]):
                code, body, _ = self.get(path)
                self.assertEqual(code, 409, body[:200])
                payload = json.loads(body)
                self.assertEqual(payload["reason"], "stale")
                self.assertIn("Reload", payload["error"])

    def test_omitting_the_generation_is_stale_too(self):

        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=12&name=NOGEN&gen=")
        self.assertEqual(code, 409, body[:200])
        self.assertEqual(json.loads(body)["reason"], "stale")

    def test_the_current_generation_exports(self):
        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get(
            f"/api/jbi?clear=1&block=0&by=t&arc=12&name=FRESH"
            f"&gen={_state('local')['gen']}")
        self.assertEqual(code, 200, body[:200])
        self.assertIn(b"//NAME FRESH", body)

    def test_a_bad_asf_is_still_answered_before_the_generation(self):


        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/jbi?clear=1&block=0&by=t&arc=notanumber&gen=0")
        self.assertEqual(code, 400)
        self.assertTrue(json.loads(body)["error"].startswith("ASF#"))


class TestRos2Endpoint(_Serving):


    def test_it_serves_what_emit_ros2_builds(self):
        run_slice(dict(CLEAN), None, "local")
        st = _state("local")
        em = st["emit"]
        expected, summary = emit.emit_ros2(
            st["toolpath"],
            cell_lc.pulse_solver(em["origin"]),
            bead_height_mm=em["bead_height"], name="ROSTEST", platter=_P0,
            scene=em["scene"],
            torch=em["torch"],
            bead_width_mm=em["bead"],
            margin_mm=max(em["margin"], 5.0),
        )
        code, body, hdrs = self.get("/api/ros2?clear=1&block=0&by=t&name=ROSTEST")
        self.assertEqual(code, 200, body[:300])
        self.assertIn("attachment", hdrs.get("Content-Disposition", ""))
        got = json.loads(body.decode("utf-8"))
        want = json.loads(expected.to_json())
        self.assertEqual(got["steps"], want["steps"])
        self.assertEqual(got["joint_names"], want["joint_names"])
        self.assertEqual(int(hdrs["X-Waam-Segments"]), summary["segments"])

    def test_the_browser_cannot_arm_the_arc(self):


        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/ros2?clear=1&block=0&by=t&name=NOARC&weld=1&arc=12")
        self.assertEqual(code, 200, body[:300])
        job = json.loads(body.decode("utf-8"))
        self.assertFalse(job["welding"])
        for step in job["steps"]:
            if step["kind"] == "io":
                self.assertEqual(step["writes"], [])

    def test_a_bad_job_name_is_refused(self):
        run_slice(dict(CLEAN), None, "local")
        code, body, _ = self.get("/api/ros2?clear=1&block=0&by=t&name=../etc/passwd")
        self.assertEqual(code, 400)
        self.assertIn("bad job name", json.loads(body)["error"])


class TestWorkbenchAndEmitterAgree(unittest.TestCase):


    def test_no_blocking_violations_on_a_job_the_script_emits_clean(self):
        result = run_slice(dict(CLEAN), None, "agree_ws")
        self.assertEqual(result["blocking"], 0, "workbench refuses a clean job")

        st = _state("agree_ws")
        em = st["emit"]
        direct = blocking(check(st["toolpath"], em["torch"], em["scene"],
                                bead_width_mm=em["bead"], bead_height_mm=em["bead_height"],
                                margin_mm=em["margin"], contact_ok=CONTACT_OK))
        self.assertEqual(len(direct), 0)

    def test_the_platter_is_forgiven_by_depth_not_by_name(self):

        run_slice(dict(CLEAN), None, "agree_ws")
        em = _state("agree_ws")["emit"]
        tp = _state("agree_ws")["toolpath"]


        sunk = Toolpath(moves=[replace(m, z=m.z - 10.0) for m in tp.moves])
        hits = blocking(check(sunk, em["torch"], em["scene"],
                              margin_mm=5.0, contact_ok=CONTACT_OK))
        self.assertTrue(any(v.obstacle == "charles_platter" for v in hits),
                        "a 40 mm plunge into the platter must still block")


if __name__ == "__main__":
    unittest.main()
