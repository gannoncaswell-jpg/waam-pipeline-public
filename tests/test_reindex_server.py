from copy import deepcopy
import datetime as dt
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
from waam_pipeline import cell_lc, reindex, server
from waam_pipeline.backends import jbi_read
from tests._support import IsolatedServerCase
from tests.test_reindex import POINTS, SOURCE, records


class TestReindexServer(IsolatedServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_records = records(POINTS)
        cls.source = SOURCE.read_text(encoding="ascii")
        cls.source_job = jbi_read.read_jbi(cls.source)
        cls.var_lines = (ROOT / "docs/cell-data/var-exports/VAR_AR1440_D25.DAT").read_text(
            encoding="ascii").splitlines()

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(server, "_STATES", {}).start()
        patch.object(server, "_SHOW_CELL", True).start()
        patch.object(server, "_HOSTED_ASSETS", False).start()
        patch("waam_pipeline.backends.jbi._git_sha", return_value="test").start()
        self.profile = {
            "version": 1, "kind": "manpro-reindex", "setup": {"records": self.setup_records},
            "clear_mm": 50, "reference": None, "source_job": None, "notes": "Class datum",
        }

    def request(self, route, params=None, *, method="GET", body=None):
        query = "?" + urlencode(params) if params else ""
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + route + query, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read(), dict(response.headers)

    def var_dat(self, contacts=None):
        lines = self.var_lines.copy()
        start = lines.index("///P") + 1
        for index, record in enumerate(self.setup_records if contacts is None else contacts, 44):
            lines[start + index] = ('"PULSE"0,0,0,000000000000000000000000,0,0,'
                                    + ",".join(map(str, record)) + ",0,0")
        return "\r\n".join(lines) + "\r\n"

    def complete_profile(self):
        return {**self.profile, "reference": {"records": self.setup_records},
                "source_job": {"name": "MANPRO_REV_2", "text": self.source}}

    def test_profiles_save_list_and_load(self):
        expected = []
        for name, profile in (("Z class datum", self.profile), ("A reference", self.complete_profile())):
            code, body, _ = self.request("/api/reindex/profile", {"name": name},
                                         method="POST", body={"profile": profile})
            self.assertEqual(code, 200, body[:300])
            result = json.loads(body)
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], name)
            self.assertEqual(dt.datetime.fromisoformat(result["saved_at"]).utcoffset(), dt.timedelta(0))
            expected.append({"name": name, "saved_at": result["saved_at"],
                             "has_reference": profile["reference"] is not None,
                             "source": "MANPRO_REV_2" if profile["source_job"] else None})
            code, body, _ = self.request("/api/reindex/profile", {"name": name})
            self.assertEqual(code, 200, body[:300])
            loaded = json.loads(body)
            self.assertEqual(loaded, {"ok": True, "profile": {**profile, "saved_at": result["saved_at"]}})
            saved = server.SAVES_DIR / "local" / "_reindex" / f"{name}.json"
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), loaded["profile"])
        code, body, _ = self.request("/api/reindex/profiles")
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(json.loads(body), {"ok": True, "profiles": sorted(expected, key=lambda p: p["name"])})
        self.assertEqual({p.suffix for p in saved.parent.iterdir()}, {".json"})

    def test_profile_refusals(self):
        code, body, _ = self.request("/api/reindex/profile", {"name": "missing"})
        self.assertEqual(code, 404, body[:300])
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                code, body, _ = self.request("/api/reindex/profile", {"name": "../x"}, method=method,
                                             body={"profile": self.profile} if method == "POST" else None)
                self.assertEqual(code, 400, body[:300])
        bad = deepcopy(self.profile)
        for index in range(1, 4):
            bad["setup"]["records"][index] = bad["setup"]["records"][0].copy()
        for route in ("/api/reindex/profile", "/api/reindex/setup"):
            with self.subTest(route=route):
                code, body, _ = self.request(route, {"name": "bad"}, method="POST",
                                             body={"profile": bad})
                self.assertEqual(code, 400, body[:300])
                self.assertIn("collinear", json.loads(body)["error"])
        self.assertFalse((server.SAVES_DIR / "local" / "_reindex" / "bad.json").exists())

    def test_profile_save_requires_profile_wrapper(self):
        for payload in (self.profile, {}, {"profile": None}):
            with self.subTest(payload=payload):
                code, body, _ = self.request("/api/reindex/profile", {"name": "unwrapped"},
                                             method="POST", body=payload)
                self.assertEqual(code, 400, body[:300])
        self.assertFalse((server.SAVES_DIR / "local" / "_reindex" / "unwrapped.json").exists())

    def test_all_routes_refuse_guests(self):
        def guest_auth(handler, query):
            handler.waam_who = {"guest": True}
            return "guest-test", ""

        routes = (("GET", "profile"), ("GET", "profiles"),
                  *(("POST", name) for name in ("profile", "setup", "source", "touches",
                                                "touch-job", "preview", "emit")))
        with patch.object(server, "_auth", side_effect=guest_auth):
            for method, name in routes:
                route = "/api/reindex/" + name
                with self.subTest(method=method, route=route):
                    self.assertNotIn(route, server.GUEST_ALLOWED)
                    code, body, _ = self.request(route, {"name": "guest"}, method=method,
                                                 body=self.profile if method == "POST" else None)
                    self.assertEqual(code, 403, body[:300])
        self.assertFalse((server.SAVES_DIR / "guest-test").exists())

    def test_workspace_mismatch_refuses_persistent_routes(self):
        routes = (("GET", "profile"), ("GET", "profiles"),
                  ("POST", "profile"), ("POST", "touch-job"), ("POST", "emit"))
        with patch.object(server._Handler, "_archive") as archive:
            for method, name in routes:
                with self.subTest(method=method, route=name):
                    code, body, _ = self.request("/api/reindex/" + name,
                                                 {"name": "wrong", "ws": "other"}, method=method,
                                                 body=self.profile if method == "POST" else None)
                    self.assertEqual(code, 409, body[:300])
            archive.assert_not_called()
        self.assertFalse((server.SAVES_DIR / "local" / "_reindex" / "wrong.json").exists())

    def test_setup_source_and_reference_touches(self):
        code, body, _ = self.request("/api/reindex/setup", method="POST", body={"profile": self.profile})
        self.assertEqual(code, 200, body[:300])
        summary = json.loads(body)["summary"]
        self.assertEqual(len(summary["probes"]), 8)
        self.assertEqual([probe["role"] for probe in summary["probes"]], list(reindex.ROLES))
        np.testing.assert_allclose([probe["expected"] for probe in summary["probes"]], POINTS, atol=0.02)
        np.testing.assert_allclose(summary["depths_mm"], [3] * 4, atol=0.03)
        self.assertAlmostEqual(summary["face_angle_deg"], 90, delta=0.03)
        self.assertAlmostEqual(summary["top_tilt_deg"], 0, delta=0.03)
        for probe in summary["probes"]:
            self.assertEqual(probe["via"][2], summary["clear_z"])
            self.assertEqual(probe["via"][:2], probe["start"][:2])

        code, body, _ = self.request("/api/reindex/source", method="POST",
                                     body=b" " * 17000 + json.dumps({"text": self.source}).encode("ascii"))
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(json.loads(body), {"ok": True, "summary": {
            "name": "MANPRO_REV_2", "groups": ["RB1", "ST1"], "points": 15,
            "station_records": 15, "calls": [{"kind": "CALL", "job": "CLEAN_R1"}],
        }})
        code, body, _ = self.request("/api/reindex/touches", method="POST",
                                     body={"profile": self.profile, "var_dat": self.var_dat()})
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(json.loads(body), {"ok": True, "records": self.setup_records})
        off = POINTS.copy()
        off[0, 0] += 1
        code, body, _ = self.request("/api/reindex/touches", method="POST",
                                     body={"profile": self.profile, "var_dat": self.var_dat(records(off))})
        self.assertEqual(code, 400, body[:300])
        self.assertRegex(json.loads(body)["error"], "P044.*not from this touch job")

    def test_json_reader_refuses_malformed_nonobject_and_oversized_bodies(self):
        for name in ("profile", "setup", "source", "touches", "touch-job", "preview", "emit"):
            for payload in (b"{broken", b"[]", b" " * 600001):
                with self.subTest(route=name, payload=payload[:20], length=len(payload)):
                    code, body, _ = self.request("/api/reindex/" + name, {"name": "invalid"},
                                                 method="POST", body=payload)
                    self.assertEqual(code, 400, body[:300])
                    self.assertFalse(json.loads(body)["ok"])

    def test_source_reports_call_and_jump_targets_and_refuses_dynamic_jobs(self):
        source = self.source.replace("CALL JOB:CLEAN_R1", "CALL JOB:CLEAN_R1\nJUMP JOB:CLASS_NEXT")
        code, body, _ = self.request("/api/reindex/source", method="POST", body={"text": source})
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(json.loads(body)["summary"]["calls"],
                         [{"kind": "CALL", "job": "CLEAN_R1"}, {"kind": "JUMP", "job": "CLASS_NEXT"}])
        for instruction in ("CALL JOB:IG#(1)", "JUMP JOB:IG#(1)"):
            with self.subTest(instruction=instruction):
                source = self.source.replace("CALL JOB:CLEAN_R1", instruction)
                code, body, _ = self.request("/api/reindex/source", method="POST", body={"text": source})
                self.assertEqual(code, 400, body[:300])
                self.assertIn("static", json.loads(body)["error"])

    def test_touch_job_emits_eight_guarded_via_searches(self):
        with patch.object(server._Handler, "_archive", return_value="saved") as archive:
            code, body, headers = self.request("/api/reindex/touch-job", {"name": "class_touch", "folder": "WAAM"},
                                               method="POST", body={"profile": self.profile})
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        job = jbi_read.read_jbi(text)
        self.assertEqual(job.name, "CLASS_TOUCH")
        self.assertEqual(job.folder, "WAAM")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="CLASS_TOUCH.JBI"')
        self.assertEqual(headers["X-Waam-Archive"], "saved")
        self.assertEqual(headers["X-Waam-Probes"], "8")
        self.assertEqual(headers["X-Waam-Base"], "44")
        archive.assert_called_once_with("local", "jbi", body, name="CLASS_TOUCH")
        self.assertIn("CALL JOB:CLEAN_R1", text)
        self.assertEqual(text.count("SRCH RIN#(3)"), 16)
        self.assertEqual(text.count('SETUALM 8815 "UNEXPECTED TOUCH ON APPROACH"'), 8)
        self.assertEqual(re.findall(r"GETS PX(\d{3}) \$PX002", text), [f"{i:03d}" for i in range(44, 52)])
        self.assertEqual(re.findall(r"MOVL P(\d{3}) V=10.0 SRCH", text), [f"{i:03d}" for i in range(44, 52)])
        self.assertEqual(re.findall(r"MOVL P(\d{3}) V=100.0 SRCH", text), [f"{i:03d}" for i in range(44, 52)])
        self.assertIn("manpro reindex 8 pts P044-P051", text)
        self.assertIn("block = measured datum faces", text)
        _, plan = reindex.plan_from_profile(self.profile)
        motions = [line for line in job.instructions if re.match(r"MOV[JL] C\d{5}", line)]
        for i, probe in enumerate(plan):
            entries = motions[1 + 3 * i:4 + 3 * i]
            self.assertEqual([line.split()[0] for line in entries], ["MOVJ", "MOVL", "MOVL"])
            for line, expected in zip(entries, (probe.via, probe.start, probe.via)):
                pulses = [int(value) for value in job.positions[line.split()[1]].split(",")]
                np.testing.assert_allclose(cell_lc.tcp_from_pulses(pulses), expected, atol=0.03)

    def test_downloads_refuse_controller_names_and_invalid_names_or_folders(self):
        payload = {"profile": self.complete_profile(), "var_dat": self.var_dat()}
        with patch.object(server._Handler, "_archive") as archive:
            for route in ("/api/reindex/touch-job", "/api/reindex/emit"):
                for params, status in (({"name": "MANPRO_REV_2"}, 409),
                                       ({"name": "bad name"}, 400), ({"folder": "bad folder"}, 400)):
                    with self.subTest(route=route, params=params):
                        code, body, _ = self.request(route, params, method="POST", body=payload)
                        self.assertEqual(code, status, body[:300])
                        self.assertNotIn(b"/JOB", body)
                        if status == 409:
                            self.assertIn("already a job", json.loads(body)["error"])
            archive.assert_not_called()

    def test_identity_preview_and_emit_preserve_station_records(self):
        payload = {"profile": self.complete_profile(), "var_dat": self.var_dat()}
        with patch.object(server._Handler, "_archive", return_value="saved") as archive:
            code, body, headers = self.request("/api/reindex/preview", method="POST", body=payload)
            self.assertEqual(code, 200, body[:300])
            self.assertNotIn("Content-Disposition", headers)
            report = json.loads(body)["report"]
            self.assertEqual(report["name"], "MANPRO_REV_2_RX")
            self.assertEqual(report["source"], "MANPRO_REV_2")
            self.assertEqual(report["calls"], [{"kind": "CALL", "job": "CLEAN_R1"}])
            self.assertEqual(len(report["points"]), 15)
            np.testing.assert_allclose(report["shift"]["center_shift_mm"], [0, 0, 0], atol=1e-9)
            np.testing.assert_allclose(report["shift"]["rotation_deg"], [0, 0, 0], atol=1e-9)
            archive.assert_not_called()
            code, body, headers = self.request("/api/reindex/emit", {"folder": "REINDEX"},
                                               method="POST", body=payload)
            self.assertEqual(code, 200, body[:300])
            archive.assert_called_once_with("local", "jbi", body, name="MANPRO_REV_2_RX")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="MANPRO_REV_2_RX.JBI"')
        self.assertEqual(headers["X-Waam-Archive"], "saved")
        self.assertEqual(headers["X-Waam-Reindex-Shift-Mm"], "0.00")
        self.assertEqual(headers["X-Waam-Reindex-Rotation-Deg"], "0.000")
        text = body.decode("ascii")
        emitted = jbi_read.read_jbi(text)
        self.assertEqual(emitted.name, "MANPRO_REV_2_RX")
        self.assertEqual(emitted.folder, "REINDEX")
        expected_ec = {key: value for key, value in self.source_job.positions.items() if key.startswith("EC")}
        self.assertEqual({key: value for key, value in emitted.positions.items() if key.startswith("EC")}, expected_ec)
        for key, value in expected_ec.items():
            self.assertIn(f"{key}={value}\r\n", text)

    def test_default_name_preserves_suffix_for_32_character_source(self):
        source_name = "MANPRO_" + "A" * 25
        profile = self.complete_profile()
        profile["source_job"] = {"name": source_name,
                                 "text": self.source.replace("//NAME MANPRO_REV_2", "//NAME " + source_name)}
        payload = {"profile": profile, "var_dat": self.var_dat()}
        expected = source_name[:29] + "_RX"
        self.assertEqual(len(source_name), 32)
        self.assertNotEqual(source_name, expected)
        code, body, _ = self.request("/api/reindex/preview", method="POST", body=payload)
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(json.loads(body)["report"]["name"], expected)
        with patch.object(server._Handler, "_archive", return_value="saved") as archive:
            code, body, headers = self.request("/api/reindex/emit", method="POST", body=payload)
            self.assertEqual(code, 200, body[:300])
            archive.assert_called_once_with("local", "jbi", body, name=expected)
        self.assertEqual(headers["Content-Disposition"], f'attachment; filename="{expected}.JBI"')
        self.assertEqual(jbi_read.read_jbi(body.decode("ascii")).name, expected)


if __name__ == "__main__":
    unittest.main()
