import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import server
from waam_pipeline.server import load_tokens
from tests._support import IsolatedServerCase


def binary_stl(triangles: np.ndarray) -> bytes:
    out = bytearray(b"\0" * 80)
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        out += struct.pack("<3f", 0, 0, 0)
        for v in tri:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    return bytes(out)


def cylinder(radius=15.0, height=12.0, around=24, capped=False):
    vs = np.linspace(0, 2 * np.pi, around, endpoint=False)
    r0 = np.stack([radius * np.cos(vs), radius * np.sin(vs), np.zeros(around)], axis=1)
    r1 = r0 + [0, 0, height]
    tris = []
    for j in range(around):
        j2 = (j + 1) % around
        tris.append([r0[j], r0[j2], r1[j]])
        tris.append([r0[j2], r1[j2], r1[j]])
        if capped:
            tris.append([[0, 0, 0], r0[j2], r0[j]])
            tris.append([[0, 0, height], r1[j], r1[j2]])
    return np.array(tris)


def http(url, method="GET", data=None, headers=None):

    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class _WorkspaceServerCase(IsolatedServerCase):


    PREFIX = "waam-saves-"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for ws in set(cls.TOKENS.values()):
            server._STATES.pop(ws, None)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for ws in set(cls.TOKENS.values()):
            server._STATES.pop(ws, None)


class TestWorkspaceIsolation(_WorkspaceServerCase):
    TOKENS = {"tok-alice-0001": "alice", "tok-bob-0002": "bob"}

    def test_wrong_token_denied(self):
        code, _, _ = http(f"{self.base}/?k=not-a-token")
        self.assertEqual(code, 401)
        code, _, _ = http(f"{self.base}/")
        self.assertEqual(code, 401)

    def test_two_tokens_have_isolated_state(self):

        code, _, _ = http(f"{self.base}/api/gcode?k=tok-bob-0002")
        self.assertEqual(code, 404)

        code, page_bob, _ = http(f"{self.base}/?k=tok-bob-0002")
        self.assertEqual(code, 200)
        self.assertIn(b"WAAM STUDIO", page_bob)
        self.assertIn(b'"workspace":"bob"', page_bob)
        _, gcode_bob_before, _ = http(f"{self.base}/api/gcode?k=tok-bob-0002&gen={server._STATES['bob']['gen']}")


        stl = binary_stl(cylinder())
        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-alice-0001&kind=stl&lh=4&cell=0&name=alice-part",
            method="POST", data=stl)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

        _, gcode_alice, _ = http(f"{self.base}/api/gcode?k=tok-alice-0001&gen={server._STATES['alice']['gen']}")
        _, gcode_bob_after, _ = http(f"{self.base}/api/gcode?k=tok-bob-0002&gen={server._STATES['bob']['gen']}")
        self.assertEqual(gcode_bob_before, gcode_bob_after)
        self.assertNotEqual(gcode_alice, gcode_bob_after)

        _, page_alice, _ = http(f"{self.base}/?k=tok-alice-0001")
        _, page_bob2, _ = http(f"{self.base}/?k=tok-bob-0002")
        self.assertIn(b"alice-part", page_alice)
        self.assertNotIn(b"alice-part", page_bob2)
        self.assertIn(b'"workspace":"alice"', page_alice)


class TestHostileRequests(_WorkspaceServerCase):


    TOKENS = {"tok-hostile-0005": "hostile"}

    def test_non_ascii_token_is_denied_not_a_crash(self):


        code, _, _ = http(f"{self.base}/?k=caf%C3%A9")
        self.assertEqual(code, 401)
        code, _, _ = http(f"{self.base}/api/jobs?k=%E4%B8%AD%E6%96%87")
        self.assertEqual(code, 401)

    def test_non_ascii_token_in_header_and_cookie(self):
        code, _, _ = http(f"{self.base}/", headers={"X-Waam-Token": "café"})
        self.assertEqual(code, 401)
        code, _, _ = http(f"{self.base}/", headers={"Cookie": "waam=café"})
        self.assertEqual(code, 401)

    def test_oversized_upload_is_refused_before_it_is_read(self):


        big = server.MAX_UPLOAD_BYTES + 1
        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-hostile-0005&kind=stl&cell=0",
            method="POST", data=b"x",
            headers={"Content-Length": str(big)})
        self.assertEqual(code, 413)
        self.assertFalse(json.loads(body)["ok"])

    def test_garbage_content_length_is_a_clean_400(self):
        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-hostile-0005&kind=wall&cell=0",
            method="POST", data=b"", headers={"Content-Length": "banana"})
        self.assertIn(code, (400, 411))


class TestSharedCookieJar(_WorkspaceServerCase):


    TOKENS = {"tok-ann-0006": "ann", "tok-ben-0007": "ben"}

    def test_stale_cookie_cannot_slice_into_another_workspace(self):


        http(f"{self.base}/?k=tok-ann-0006")
        http(f"{self.base}/?k=tok-ben-0007")
        code, body, _ = http(
            f"{self.base}/api/slice?kind=wall&cell=0&name=from-anns-tab&ws=ann",
            method="POST", data=b"", headers={"Cookie": "waam=tok-ben-0007"})
        self.assertEqual(code, 409)
        self.assertIn("ann", json.loads(body)["error"])

        _, page_ben, _ = http(f"{self.base}/?k=tok-ben-0007")
        self.assertNotIn(b"from-anns-tab", page_ben)

    def test_page_token_wins_over_a_stale_cookie(self):

        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-ann-0006&ws=ann&kind=wall&cell=0&name=ann-part",
            method="POST", data=b"", headers={"Cookie": "waam=tok-ben-0007"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        _, page_ann, _ = http(f"{self.base}/?k=tok-ann-0006")
        _, page_ben, _ = http(f"{self.base}/?k=tok-ben-0007")
        self.assertIn(b"ann-part", page_ann)
        self.assertNotIn(b"ann-part", page_ben)

    def test_matching_workspace_is_allowed(self):
        code, _, _ = http(f"{self.base}/api/jobs?k=tok-ben-0007&ws=ben")
        self.assertEqual(code, 200)


class TestSaveLoadRoundTrip(_WorkspaceServerCase):
    TOKENS = {"tok-rt-0003": "rt", "tok-empty-0004": "empty"}

    def test_save_load_reproduces_toolpath(self):
        stl = binary_stl(cylinder(capped=True))
        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-rt-0003&kind=stl&lh=3&tilt=15&cell=0&name=ring",
            method="POST", data=stl)
        self.assertTrue(json.loads(body)["ok"])
        summary0 = server._STATES["rt"]["toolpath"].summary()

        gen = server._STATES["rt"]["gen"]
        code, body, _ = http(
            f"{self.base}/api/save?k=tok-rt-0003&name=job%20one&gen={gen}",
            method="POST")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertTrue(j["ok"])
        self.assertEqual(j["job"], "job_one")

        d = server.SAVES_DIR / "rt" / "job_one"
        self.assertTrue((d / "request.json").exists())
        self.assertEqual((d / "part.stl").read_bytes(), stl)
        self.assertTrue((d / "part.gcode").exists())
        saved = json.loads((d / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["params"]["lh"], "3")
        self.assertEqual(saved["params"]["tilt"], "15")
        self.assertEqual(saved["params"]["name"], "ring")
        self.assertIn("saved_at", saved)
        self.assertNotIn("k", saved["params"])


        code, body, _ = http(f"{self.base}/api/jobs?k=tok-rt-0003")
        jobs = json.loads(body)["jobs"]
        self.assertIn("job_one", [j["name"] for j in jobs])

        code, body, _ = http(f"{self.base}/api/jobs?k=tok-empty-0004")
        self.assertEqual(json.loads(body)["jobs"], [])


        code, body, _ = http(
            f"{self.base}/api/slice?k=tok-rt-0003&kind=wall&lh=5&cell=0&name=other",
            method="POST")
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotEqual(server._STATES["rt"]["toolpath"].summary(), summary0)

        code, body, _ = http(f"{self.base}/api/load?k=tok-rt-0003&name=job_one",
                             method="POST")
        self.assertEqual(code, 200)
        loaded = json.loads(body)
        self.assertTrue(loaded["ok"])
        self.assertEqual(server._STATES["rt"]["toolpath"].summary(), summary0)


        self.assertEqual(loaded["params"]["lh"], "3")
        self.assertEqual(loaded["params"]["name"], "ring")
        self.assertNotIn("k", loaded["params"])

    def test_a_save_from_a_superseded_page_is_refused(self):


        http(f"{self.base}/api/slice?k=tok-rt-0003&kind=wall&lh=5&cell=0&name=one",
             method="POST", data=b"")
        stale = server._STATES["rt"]["gen"]
        http(f"{self.base}/api/slice?k=tok-rt-0003&kind=wall&lh=4&cell=0&name=two",
             method="POST", data=b"")
        code, body, _ = http(
            f"{self.base}/api/save?k=tok-rt-0003&name=stale&gen={stale}",
            method="POST")
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["reason"], "stale")
        self.assertFalse((server.SAVES_DIR / "rt" / "stale").exists())

        code, _, _ = http(f"{self.base}/api/save?k=tok-rt-0003&name=stale",
                          method="POST")
        self.assertEqual(code, 409)

    def test_save_refused_before_any_slice(self):
        code, body, _ = http(f"{self.base}/api/save?k=tok-empty-0004&name=nothing",
                             method="POST")
        self.assertEqual(code, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_load_of_missing_job_is_404(self):
        code, body, _ = http(f"{self.base}/api/load?k=tok-empty-0004&name=ghost",
                             method="POST")
        self.assertEqual(code, 404)
        self.assertFalse(json.loads(body)["ok"])


class TestJobNameValidation(_WorkspaceServerCase):
    TOKENS = {"tok-val-0005": "val"}

    BAD_NAMES = ["../x", "a/b", "a\\b", ".", "..", "", " leading", "-dash",
                 "x" * 60, "dot.dot"]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        code, body, _ = http(
            f"{cls.base}/api/slice?k=tok-val-0005&kind=wall&lh=5&cell=0&name=v",
            method="POST")
        assert json.loads(body)["ok"]

    def test_bad_names_rejected_and_nothing_written(self):
        for bad in self.BAD_NAMES:
            qs = urllib.parse.urlencode({"k": "tok-val-0005", "name": bad})
            for endpoint in ("save", "load"):
                code, body, _ = http(f"{self.base}/api/{endpoint}?{qs}",
                                     method="POST")
                self.assertEqual(code, 400, f"{endpoint} accepted {bad!r}")
                self.assertFalse(json.loads(body)["ok"])

        on_disk = list(server.SAVES_DIR.rglob("*")) if server.SAVES_DIR.exists() else []
        self.assertEqual(on_disk, [])

    def test_good_name_still_works(self):
        gen = server._STATES["val"]["gen"]
        code, body, _ = http(
            f"{self.base}/api/save?k=tok-val-0005&name=ok-name_1&gen={gen}",
            method="POST")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue((server.SAVES_DIR / "val" / "ok-name_1" / "request.json").exists())


class TestLegacySingleToken(_WorkspaceServerCase):
    TOKENS = {"legacy-tok-0006": "default"}

    def test_all_three_carriers_authenticate(self):

        code, page, headers = http(f"{self.base}/?k=legacy-tok-0006")
        self.assertEqual(code, 200)
        self.assertIn("legacy-tok-0006", headers.get("Set-Cookie", ""))


        for flag in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertIn(b'"workspace":"default"', page)

        code, _, _ = http(f"{self.base}/", headers={"Cookie": "waam=legacy-tok-0006"})
        self.assertEqual(code, 200)

        code, _, _ = http(f"{self.base}/", headers={"X-Waam-Token": "legacy-tok-0006"})
        self.assertEqual(code, 200)

        code, body, _ = http(
            f"{self.base}/api/slice?k=legacy-tok-0006&kind=wall&lh=5&cell=0&name=w",
            method="POST")
        self.assertTrue(json.loads(body)["ok"])
        code, gcode, _ = http(f"{self.base}/api/gcode?k=legacy-tok-0006&gen={server._STATES['default']['gen']}")
        self.assertEqual(code, 200)
        self.assertIn(b"G21", gcode)


class TestLoadTokens(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-tok-"))
        self._old_tf = server._token_file
        self._old_tsf = server._tokens_file
        server._token_file = lambda: self.tmp / ".waam-token"
        server._tokens_file = lambda: self.tmp / ".waam-tokens.json"
        self._old_env = os.environ.pop("WAAM_TOKEN", None)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        server._token_file = self._old_tf
        server._tokens_file = self._old_tsf
        if self._old_env is not None:
            os.environ["WAAM_TOKEN"] = self._old_env

    def test_no_roster_falls_back_to_single_default_token(self):
        tokens = load_tokens(None)
        self.assertEqual(list(tokens.values()), ["default"])
        (tok,) = tokens

        self.assertEqual((self.tmp / ".waam-token").read_text(encoding="utf-8").strip(),
                         tok)

        self.assertEqual(load_tokens(None), tokens)

    def test_roster_maps_each_token_to_a_sanitised_name(self):
        (self.tmp / ".waam-tokens.json").write_text(json.dumps({
            "tokens": {"aaa111": {"name": "Operator C!"}, "bbb222": {"name": "bob"}},
        }), encoding="utf-8")
        tokens = load_tokens(None)
        self.assertEqual(tokens, {"aaa111": "Operator_C", "bbb222": "bob"})

        tokens = load_tokens("cli-tok")
        self.assertEqual(tokens["cli-tok"], "default")
        self.assertEqual(tokens["aaa111"], "Operator_C")

    def test_roster_does_not_revoke_the_already_issued_link(self):


        (self.tmp / ".waam-token").write_text("delivered-tok", encoding="utf-8")
        (self.tmp / ".waam-tokens.json").write_text(json.dumps({
            "tokens": {"aaa111": {"name": "operator"}},
        }), encoding="utf-8")
        tokens = load_tokens(None)
        self.assertEqual(tokens["delivered-tok"], "default")
        self.assertEqual(tokens["aaa111"], "operator")

    def test_roster_without_a_persisted_token_mints_nothing(self):
        (self.tmp / ".waam-tokens.json").write_text(json.dumps({
            "tokens": {"aaa111": {"name": "operator"}},
        }), encoding="utf-8")
        tokens = load_tokens(None)
        self.assertEqual(tokens, {"aaa111": "operator"})
        self.assertFalse((self.tmp / ".waam-token").exists())

    def test_unreadable_roster_falls_back_whole(self):
        (self.tmp / ".waam-tokens.json").write_text("{not json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            tokens = load_tokens("explicit-tok")
        self.assertEqual(tokens, {"explicit-tok": "default"})


if __name__ == "__main__":
    unittest.main()
