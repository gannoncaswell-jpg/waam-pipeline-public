import contextlib
import io
import json
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from unittest.mock import patch
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from http.server import ThreadingHTTPServer

from waam_pipeline import server
from waam_pipeline.server import _Handler
from waam_pipeline.server import run_slice
from tests._support import IsolatedServerCase
from tests.test_studio import run_studio_node


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


class TestRunSlice(unittest.TestCase):
    def test_clipped_placement_explains_requested_and_accepted_offsets(self):
        result = run_slice({"kind": "wall", "ox": "999", "oy": "0", "repair": "0"}, None)
        placement = result["placement"]
        self.assertEqual(placement["requested_offsets"]["ox"], 999)
        self.assertNotEqual(placement["accepted_offsets"]["ox"], 999)
        self.assertEqual(placement["accepted_offsets"]["ox"], float(result["state"]["meta"]["params"]["ox"]))
        self.assertIn("kept inside the platter", placement["adjustment"])
        self.assertIn("requested X 999.000", placement["adjustment"])

    def test_wall_demo(self):
        result = run_slice({"kind": "wall", "name": "t"}, None)
        self.assertTrue(result["ok"])
        self.assertGreater(result["layers"], 2)
        self.assertIn("WAAM STUDIO", server._current["html"])
        self.assertIn('"workbench":true', server._current["html"])

    def test_an_unknown_demo_shape_is_refused_not_swapped_for_a_wall(self):
        run_slice({"kind": "wall", "name": "t"}, None)
        before = server._current["toolpath"]
        with self.assertRaisesRegex(ValueError, "unknown demo 'no-such-part'"):
            run_slice({"kind": "wall", "shape": "no-such-part", "name": "no-such-part"}, None)
        self.assertIs(server._current["toolpath"], before, "the old build stays")

    def test_stl_slicing_with_tilt(self):

        stl = binary_stl(cylinder(capped=True))
        result = run_slice({"kind": "stl", "lh": "3", "tilt": "20", "name": "cyl"}, stl)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["layers"], 3)
        self.assertIn('"part":{"tris":', server._current["html"])
        self.assertIn('id="renderStyle"', server._current["html"])
        self.assertAlmostEqual(result["stl"]["orientation"]["normal"][2], -1)

    def test_metre_export_auto_scales_and_slices(self):
        stl = binary_stl(cylinder(radius=0.02, height=0.01))
        result = run_slice({"kind": "stl", "lh": "2.9", "units": "auto"}, stl)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stl"]["resolved_units"], "m")
        self.assertAlmostEqual(result["stl"]["size_mm"][0], 40.0, places=2)
        self.assertGreater(result["moves"], 0)
        self.assertIn('id="wbUnits"', server._current["html"])

    def test_zero_tilt_still_drops_a_centred_mesh_to_z0(self):
        centred = cylinder() - np.array([0.0, 0.0, 6.0])
        result = run_slice(
            {"kind": "stl", "lh": "3", "tilt": "0", "units": "mm", "cell": "0"},
            binary_stl(centred),
        )
        self.assertEqual(result["layers"], 4)

    def test_empty_slice_has_actionable_error(self):
        stl = binary_stl(cylinder(radius=0.02, height=0.01))
        with self.assertRaisesRegex(ValueError, "check STL units"):
            run_slice({"kind": "stl", "lh": "2.9", "units": "mm"}, stl)

    def test_bad_file_has_a_friendly_refusal(self):
        with self.assertRaisesRegex(ValueError, r"^Choose a binary or ASCII STL file\.$"):
            run_slice({"kind": "stl"}, b"not an stl at all")


class TestHttpRoundTrip(IsolatedServerCase):


    def post_slice(self, params, body=b""):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/slice?{urlencode(params)}",
            data=body, method="POST")
        with urllib.request.urlopen(req) as response:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            return json.loads(response.read())

    def test_stale_page_cannot_slice_or_place_over_a_newer_build(self):
        first = self.post_slice(dict(kind="wall", cell="0", lh="2.9"))
        old_gen = first["state"]["meta"]["gen"]
        second = self.post_slice(dict(kind="wall", cell="0", lh="3.5"))
        for route in ("slice?kind=wall&cell=0&lh=2.9", "place?ox=12"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/{route}&gen={old_gen}", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read())["reason"], "stale")
            self.assertEqual(server._current["state"]["meta"]["gen"], second["state"]["meta"]["gen"])
            self.assertEqual(float(server._current["request"]["params"]["lh"]), 3.5)

    def test_face_orientation_round_trips_through_slice_and_place(self):
        from examples.ornl_acceptance import block
        params = dict(kind="stl", units="mm", lh="3", bead="3", seam="0", cell="0", repair="0")
        first = self.post_slice(params, binary_stl(block(width=30, depth=18, height=6)))
        self.assertTrue(first["ok"])
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/place?rotX=70&rotY=0&yaw=37&ox=10&oy=-12",
            data=b"", method="POST")
        with urllib.request.urlopen(request) as response:
            placed = json.loads(response.read())
        self.assertTrue(placed["ok"], placed)
        self.assertEqual(placed["stl"]["size_mm"], [30, 6, 18])
        for key, value in dict(rotX="70.0", rotY="0.0", yaw="37", ox="10", oy="-12").items():
            self.assertEqual(placed["state"]["meta"]["params"][key], value)
        before = server.to_gcode(server._current["toolpath"]).encode()
        replay = self.post_slice(dict(placed["state"]["meta"]["params"], reuse="1"))
        self.assertTrue(replay["ok"])
        self.assertEqual(before, server.to_gcode(server._current["toolpath"]).encode())
        self.assertEqual(placed["stl"], replay["stl"])

    def test_page_and_slice_and_gcode(self):
        base = f"http://127.0.0.1:{self.port}"
        html = urllib.request.urlopen(f"{base}/").read().decode()
        self.assertIn("Prepare build", html)
        stl = binary_stl(cylinder())
        req = urllib.request.Request(
            f"{base}/api/slice?kind=stl&lh=4&name=cyl", data=stl, method="POST"
        )
        result = json.loads(urllib.request.urlopen(req).read())
        self.assertTrue(result["ok"])
        with urllib.request.urlopen(f"{base}/api/gcode?gen={server._current['gen']}") as response:
            self.assertEqual(response.headers.get("Content-Disposition"),
                             'attachment; filename="cyl.gcode"')
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            gcode = response.read().decode()
        self.assertIn("G21", gcode)
        self.assertIn(";LAYER:", gcode)

    def test_reslice_reuses_the_uploaded_stl(self):
        base = f"http://127.0.0.1:{self.port}"
        stl = binary_stl(cylinder())
        first = urllib.request.Request(
            f"{base}/api/slice?kind=stl&lh=4&name=remember-me", data=stl, method="POST"
        )
        self.assertTrue(json.loads(urllib.request.urlopen(first).read())["ok"])
        second = urllib.request.Request(
            f"{base}/api/slice?kind=stl&lh=3&name=remember-me&reuse=1",
            data=b"", method="POST",
        )
        result = json.loads(urllib.request.urlopen(second).read())
        self.assertTrue(result["ok"])
        self.assertGreater(result["moves"], 0)
        self.assertIn('"part":{"tris":', server._current["html"])

    def test_bad_uploads_hide_parser_exceptions_and_keep_the_checked_part(self):
        run_slice({"name": "keep me", "cell": "0", "repair": "0"}, None)
        before = dict(server._current)
        bad_files = (
            b"not an stl at all", b"", b"\0" * 84,
            binary_stl(cylinder())[:-7],
            b"solid broken\nfacet normal 0 0 1\nvertex 0 0 0\nendsolid",
            binary_stl(np.full((1, 3, 3), np.nan)),
        )
        for data in bad_files:
            with self.subTest(data=data[:30]):
                result = self.post_slice({"kind": "stl", "name": "bad.stl"}, data)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"],
                                 "Choose a binary or ASCII STL file.")
                self.assertNotIn("unpack_from", result["error"])
                self.assertNotIn("Traceback", result["error"])
                for key, value in before.items():
                    self.assertIs(server._current[key], value)

    def test_stl_arithmetic_failures_have_friendly_geometry_guidance(self):
        run_slice({"name": "keep me", "cell": "0", "repair": "0"}, None)
        before = dict(server._current)
        cases = (
            ("scaling", {"units": "m"}, ((0, 0, 0), (1, 0, 1e308), (0, 1, 0))),
            ("transformation", {"units": "mm", "tilt": "45"},
             ((1.5e308, 0, 0), (0, 0, 1.5e308), (0, 1, 0))),
            ("slicing", {"units": "mm", "lh": "0.1"},
             ((0, 0, 0), (1, 0, 1e308), (0, 1, 0))),
        )
        for stage, params, triangle in cases:
            with self.subTest(stage=stage):
                vertices = "\n".join("vertex " + " ".join(map(str, v)) for v in triangle)
                data = ("solid bad\nfacet normal 0 0 1\nouter loop\n" + vertices
                        + "\nendloop\nendfacet\nendsolid bad").encode("ascii")
                result = self.post_slice(
                    dict(kind="stl", name="bad.stl", cell="0", repair="0", **params), data)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"],
                                 "STL geometry is outside the supported range. "
                                 "Check the units and export a binary or ASCII STL from your CAD")
                self.assertNotIn("cannot convert", result["error"])
                self.assertNotIn("Traceback", result["error"])
                for key, value in before.items():
                    self.assertIs(server._current[key], value)

    def test_stl_unit_and_contour_guidance_is_preserved(self):
        cases = (
            ({"units": "cm"}, cylinder(), "STL units must be auto, mm, in, or m"),
            ({"units": "mm"}, cylinder(radius=0.02, height=0.01),
             "check STL units, mesh closure, or reduce the layer height"),
        )
        for params, triangles, guidance in cases:
            with self.subTest(guidance=guidance):
                result = self.post_slice(
                    dict(kind="stl", cell="0", repair="0", **params), binary_stl(triangles))
                self.assertFalse(result["ok"])
                self.assertIn(guidance, result["error"])

    def test_ascii_stl_still_slices(self):
        facets = []
        for tri in cylinder(around=8):
            vertices = "\n".join("vertex " + " ".join(map(str, v)) for v in tri)
            facets.append(f"facet normal 0 0 0\nouter loop\n{vertices}\nendloop\nendfacet")
        data = ("solid cylinder\n" + "\n".join(facets) + "\nendsolid").encode("ascii")
        result = self.post_slice({"kind": "stl", "cell": "0", "repair": "0", "lh": "4"}, data)
        self.assertTrue(result["ok"], result)

    def test_bad_numbers_name_the_field_even_when_browser_validation_is_bypassed(self):
        fields = {
            "lh": ("Bead height", "0", "101"),
            "bead": ("Bead width", "0", "101"),
            "wire": ("Wire", "0", "11"),
            "travel": ("Travel", "0", "10001"),
            "margin": ("Margin", "-1", "101"),
            "tilt": ("Tilt", "-361", "361"),
            "seam": ("Seam", "-361", "361"),
            "ox": ("X offset", "-10001", "10001"),
            "oy": ("Y offset", "-10001", "10001"),
            "sub": ("Substrate", "-1", "101"),
            "oz": ("Lift-off gap", "-1", "101"),
            "yaw": ("Z rotation", "-361", "361"),
        }
        for key, (label, low, high) in fields.items():
            for value in (low, high, "", "oops", "nan", "inf", "1e999"):
                with self.subTest(field=key, value=value):
                    result = self.post_slice({key: value})
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["error"].startswith(label), result)
                    self.assertIn("enter", result["error"])
                    self.assertNotIn("could not convert", result["error"])
                    self.assertNotIn("Traceback", result["error"])

    def test_download_uses_the_part_name_and_sanitizes_header_characters(self):
        self.addCleanup(server._STATES.pop, "download-account", None)
        for title, filename in (("bracket.stl", "bracket.gcode"),
                                ('bracket"\r\n雪.stl', "bracket____.gcode")):
            with self.subTest(title=title):
                run_slice({"name": title, "cell": "0", "repair": "0"}, None, "download-account")
                with mock.patch.object(server, "_refresh_tokens", return_value={"account-token": "download-account"}), \
                        mock.patch.object(server, "_TOKEN_GATE", True):
                    gen = server._STATES["download-account"]["gen"]
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/gcode?k=account-token&gen={gen}") as response:
                        self.assertEqual(response.headers.get("Content-Disposition"),
                                         f'attachment; filename="{filename}"')
                        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                        self.assertIn(b"G21", response.read())

    def test_bad_asf_numbers_have_a_field_message_before_the_emit_gate(self):
        run_slice({"cell": "0", "repair": "0"}, None)
        for value in ("", "0", "1001", "1.5", "oops", "nan", "inf"):
            with self.subTest(value=value):
                url = f"http://127.0.0.1:{self.port}/api/jbi?{urlencode({'arc': value})}"
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url)
                with caught.exception as response:
                    self.assertEqual(response.code, 409 if value in ("0", "1001") else 400)
                    payload = json.loads(response.read())
                    if response.code == 409:
                        self.assertEqual(payload["reason"], "argument")
                    error = payload["error"]
                    self.assertTrue(error.startswith("ASF#"), error)
                    self.assertIn("pendant", error)
                    self.assertNotIn("arc_file", error)

    def test_unknown_filler_names_the_control(self):
        result = self.post_slice({"filler": "missing"})
        self.assertEqual(result, {"ok": False, "error": "Filler: choose a filler from the list"})

    def test_a_guest_with_a_slice_still_cannot_download_gcode(self):
        self.assertNotIn("/api/gcode", server.GUEST_ALLOWED)

        gate = mock.patch.object(server, "_TOKEN_GATE", True)
        gate.start()
        self.addCleanup(gate.stop)
        cfg = server.oidc.AuthConfig("local", "", "", "http://127.0.0.1")
        auth = mock.patch.object(server, "_refresh_auth", return_value=cfg)
        auth.start()
        self.addCleanup(auth.stop)
        gid = server._new_guest()
        self.addCleanup(server.forget_guests)
        run_slice({"name": "guest part", "cell": "0", "repair": "0"}, None,
                  server._guest_workspace(gid))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/gcode",
                                     headers={"Cookie": f"{server.GUEST_COOKIE}={gid}"})
        who = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/whoami",
                                     headers={"Cookie": f"{server.GUEST_COOKIE}={gid}"})
        with urllib.request.urlopen(who) as response:
            self.assertTrue(json.loads(response.read())["guest"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        with caught.exception as response:
            self.assertEqual(response.code, 403)
            self.assertIsNone(response.headers.get("Content-Disposition"))
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            self.assertFalse(json.loads(response.read())["ok"])

    def post_state(self, query, data=b""):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{query}",
                                     data=data, method="POST")
        with urllib.request.urlopen(req) as response:
            self.assertIn("no-store", response.headers["Cache-Control"].split(", "))
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("state", result, "the client needs the render state, not just counts")
        state = result["state"]
        embedded = json.loads(re.search(r"const S = (\{.*?\});\n",
                                        server._current["html"]).group(1))
        self.assertEqual(state, embedded)
        self.assertEqual(len(state["toolpath"]["moves"]), result["moves"])
        self.assertEqual(len(state["violations"]), result["violations"])
        self.assertEqual(state["meta"]["params"], server._current["request"]["params"])
        return state

    def test_slice_response_is_the_complete_render_state(self):
        state = self.post_state("slice?kind=stl&lh=4&units=mm&name=chip.stl&cell=0",
                                binary_stl(cylinder()))
        self.assertEqual(state["meta"]["title"], "chip.stl")
        self.assertEqual(state["meta"]["params"]["kind"], "stl")
        self.assertIsNotNone(state["meta"]["stl"])
        self.assertIsNotNone(state["part"])
        for key in ("schedule", "arc", "scene", "robot", "violations", "torch"):
            self.assertIn(key, state)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute workbench handlers")
    def test_demo_replaces_an_upload_and_cannot_reuse_it_afterward(self):


        run_slice({"kind": "stl", "shape": "square", "lh": "4", "name": "old.stl", "cell": "0"},
                  binary_stl(cylinder()))
        sent = run_studio_node(server._current["html"], """
let sent;
window.fetch=async(path,options)=>{
  if(path.startsWith('/api/slice?')){
    sent=Object.fromEntries(new URL(path,location.href).searchParams);
    const r=await realFetch(path,options), j=await r.json();
    expect(j.ok,j.error);


    return new Response(JSON.stringify({ok:false,error:'request captured'}));
  }
  return realFetch(path,options);
};
wbShape.value='wall'; wbShape.dispatchEvent(new Event('change'));
await sliceBtn.onclick();
return sent;
""", f"http://127.0.0.1:{self.port}")
        self.assertEqual(sent["kind"], "wall", "demo change left the uploaded STL selected")
        self.assertEqual(sent["reuse"], "0", "demo change still reuses the uploaded STL")
        self.assertEqual(sent["shape"], "wall")
        self.assertEqual(server._current["request"]["params"]["kind"], "wall")
        self.assertIsNone(server._current["request"]["stl"])
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/slice?kind=stl&reuse=1&cell=0",
            data=b"", method="POST")
        result = json.loads(urllib.request.urlopen(req).read())
        self.assertFalse(result["ok"])
        self.assertNotIn("state", result)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute workbench handlers")
    def test_overlapping_slice_attempt_is_blocked_then_later_slice_matches_all_targets(self):
        run_slice({"kind": "wall", "shape": "wall", "lh": "2", "fixture": "0"}, None)
        emitted = []

        def capture_emit(tp, *args, **kwargs):
            emitted.append(tp)
            return tp.summary(), {"positions": len(tp.moves), "passes": len(tp.layers())}

        with tempfile.TemporaryDirectory(prefix="waam-race-save-") as td, \
                patch.object(server, "SAVES_DIR", Path(td)), \
                patch.object(server, "_git_commit", return_value="test"), \
                patch("waam_pipeline.emit.emit_job", side_effect=capture_emit):
            result = run_studio_node(server._current["html"], """
let releaseA, committedA, calls=0;
const gate=new Promise(resolve=>{releaseA=resolve;});
const committed=new Promise(resolve=>{committedA=resolve;});
window.fetch=async(path,options)=>{
  if(path.startsWith('/api/slice?')){
    const n=++calls, r=await realFetch(path,options);
    if(n===1){committedA(); await gate;}
    return r;
  }
  return realFetch(path,options);
};
wbShape.value='wall'; wbShape.dispatchEvent(new Event('change')); wbLh.value='2';
const a=sliceBtn.onclick();
await committed;
const disabled=['sliceBtn','wbShape','stlFile','clearStl','placeBtn','wbFixture',
                'saveBtn','loadBtn','emitBtn','emitRosBtn'].every(id=>document.getElementById(id).disabled);
const downloadDisabled=!document.querySelector('#wb a[download="part.gcode"]').hasAttribute('href');
wbLh.value='3';
await sliceBtn.onclick();
releaseA(); await a;
expect(calls===1,'overlapping slice reached the server and can overtake A');
expect(disabled && downloadDisabled,'job controls were enabled while A was in flight');
expect(!sliceBtn.disabled && !stlFile.disabled && !wbShape.disabled,'slice controls stayed locked');

wbLh.value='3'; await sliceBtn.onclick();
expect(calls===2,'later slice did not run after A settled');
wbJobName.value='later'; await saveBtn.onclick();
const gcode=await (await realFetch(api('/api/gcode'))).text();
const job=await realFetch(api('/api/jbi?clear=1&block=0&by=test&arc=12&name=LATER'));
expect(job.ok,'emit endpoint refused the test probe');
return {preview:waamStudio.state.toolpath,params:waamStudio.state.meta.params,
        displayedMoves:M,gcode,job:await job.text()};
""", f"http://127.0.0.1:{self.port}")
            saved = json.loads((Path(td) / "local" / "later" / "request.json").read_text())
            saved_gcode = (Path(td) / "local" / "later" / "part.gcode").read_text()
        self.assertEqual(result["params"]["lh"], "3")
        self.assertEqual(saved["params"]["lh"], "3")
        self.assertEqual(server._current["request"]["params"]["lh"], "3")
        self.assertEqual(result["gcode"], saved_gcode)
        self.assertEqual(result["job"], result["preview"]["summary"])
        self.assertEqual(len(emitted), 1)
        self.assertIs(emitted[0], server._current["toolpath"])
        actual_moves = [{"x": round(m.x, 2), "y": round(m.y, 2), "z": round(m.z, 2),
                         "d": int(m.deposit), "l": m.layer} for m in emitted[0].moves]
        self.assertEqual(result["preview"]["moves"], actual_moves)
        self.assertEqual(result["displayedMoves"], actual_moves)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute workbench handlers")
    def test_placement_keeps_a_removed_stl_removed_before_the_next_slice(self):
        run_slice({"kind": "stl", "shape": "square", "lh": "4", "name": "old.stl", "cell": "0"},
                  binary_stl(cylinder()))
        result = run_studio_node(server._current["html"], """
wbShape.value='wall'; wbShape.dispatchEvent(new Event('change'));
wbOx.value='10'; wbFixture.value='0'; await wbFixture.onchange();
expect(Number(waamStudio.state.meta.params.ox)===Number(wbOx.value),'accepted placement response did not render');
expect(wbShape.value==='wall','placement reset the selected demo');
expect(stlChip.style.display==='none','placement resurrected the removed STL chip');
await sliceBtn.onclick();
return waamStudio.state.meta.params;
""", f"http://127.0.0.1:{self.port}")
        self.assertEqual(result["kind"], "wall")
        self.assertEqual(result["shape"], "wall")
        self.assertIsNone(server._current["request"]["stl"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute workbench handlers")
    def test_placement_checks_a_pending_upload_with_its_selected_units(self):
        run_slice({"kind": "stl", "lh": "4", "units": "mm", "name": "old.stl", "cell": "0"},
                  binary_stl(cylinder()))
        replacement = binary_stl(cylinder(radius=0.02, height=0.012))
        scenario = """
const upload={name:'replacement.stl',arrayBuffer:async()=>new Uint8Array(BYTES).buffer};
stlFile.files=[upload];
stlFile.dispatchEvent(new Event('change')); wbUnits.value='m';
wbOx.value='10';
await placeBtn.onclick();
expect(Number(waamStudio.state.meta.params.ox)===Number(wbOx.value),'accepted placement response did not render');
expect(stlFile.files.length===0,'successful setup and placement check did not consume the upload');
expect(waamStudio.state.meta.title==='replacement.stl','placement kept the earlier source');
expect(wbUnits.value==='m','placement reset units for the pending upload');
expect(stlName.textContent==='replacement.stl','placement changed the pending STL chip');
await sliceBtn.onclick();
expect(stlFile.files.length===0,'successful slice did not consume the pending upload');
expect(stlName.textContent==='replacement.stl','successful slice lost the loaded STL chip');
return waamStudio.state.meta;
""".replace("BYTES", json.dumps(list(replacement)))
        result = run_studio_node(server._current["html"], scenario, f"http://127.0.0.1:{self.port}")
        self.assertEqual(result["title"], "replacement.stl")
        self.assertEqual(result["params"]["units"], "m")
        self.assertEqual(server._current["request"]["stl"], replacement)

    def test_placement_response_uses_the_same_render_contract(self):
        before = self.post_state("slice?kind=wall&shape=wall&lh=6&fixture=0&name=placed")
        after = self.post_state("place?ox=10&oy=-20&yaw=90&fixture=0")
        self.assertNotEqual(before["toolpath"]["moves"], after["toolpath"]["moves"])
        self.assertNotEqual(before["meta"]["placement"], after["meta"]["placement"])
        self.assertEqual(after["meta"]["params"]["yaw"], "90")
        self.assertEqual(after["meta"]["title"], "placed")


class TestRegistrationExportState(IsolatedServerCase):
    def post(self, route, params):
        request = urllib.request.Request(f"{self.base}/api/{route}?{urlencode(params)}", data=b"", method="POST")
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def setUp(self):
        self.first = run_slice(dict(kind="wall", shape="bead", fixture="0", repair="0"), None,
                               registration_action="replace")

    def test_failed_registration_blocks_old_exports_and_save_until_explicit_clear(self):
        before = server._current["toolpath"]
        failed = self.post("place", {"substrate_registration": "{}", "registration_action": "apply"})
        self.assertFalse(failed["ok"])
        self.assertIs(server._current["toolpath"], before)
        self.assertTrue(server._current["registration_pending"])
        self.assertIn('"registration_pending":true', server._current["html"])
        for route in ("place", "slice"):
            generic = self.post(route, {"substrate_registration": "", "ox": "0"})
            self.assertFalse(generic["ok"], "An old tab must not clear the failed registration")
        gen = self.first["state"]["meta"]["gen"]
        for route in ("jbi", "ros2", "gcode", "save"):
            request = urllib.request.Request(
                f"{self.base}/api/{route}?gen={gen}&name=registration-test&arc=12",
                data=b"" if route == "save" else None)
            with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            with caught.exception as response:
                self.assertEqual(response.code, 409)
                self.assertEqual(json.loads(response.read())["reason"], "registration")
        cleared = self.post("place", {"substrate_registration": "", "registration_action": "clear"})
        self.assertTrue(cleared["ok"], cleared)
        self.assertEqual(cleared["state"]["meta"]["gen"], gen + 1)
        self.assertFalse(server._current["registration_pending"])
        with urllib.request.urlopen(f"{self.base}/api/gcode?gen={gen + 1}") as response:
            self.assertEqual(response.status, 200)

    def test_saved_registration_failure_is_blocked_and_successful_replay_recovers(self):
        directory = server._job_dir("local", "registration-replay")
        directory.mkdir(parents=True, exist_ok=True)
        params = dict(self.first["state"]["meta"]["params"], substrate_registration="{}")
        (directory / "request.json").write_text(json.dumps({"params": params}), encoding="utf-8")
        self.assertFalse(self.post("load", {"name": "registration-replay"})["ok"])
        self.assertTrue(server._current["registration_pending"])
        params["substrate_registration"] = ""
        (directory / "request.json").write_text(json.dumps({"params": params}), encoding="utf-8")
        self.assertTrue(self.post("load", {"name": "registration-replay"})["ok"])
        self.assertFalse(server._current["registration_pending"])

    @unittest.skipUnless(shutil.which("node"), "Node executes the registration workflow")
    def test_pending_and_failed_registration_block_browser_exports_and_preserve_preview(self):
        result = run_studio_node(server._current["html"], """
const before=JSON.stringify(waamStudio.state.toolpath);
wbCorner1.value='1 2 3';expect(!await waamSubstrate.apply(),'malformed draft was accepted');
expect(emitBtn.disabled && emitRosBtn.disabled && saveBtn.disabled,'draft did not disable exports');
expect(!document.querySelector('#wb a[download="part.gcode"]').hasAttribute('href'),'gcode stayed enabled');
expect(substrateRegistrationError().length>0,'old preview falsely reported ready');
expect(!clearSubstrate.disabled,'no recovery when no substrate was accepted');
waamSubstrate.paint();expect(emitBtn.disabled,'repaint unlocked draft');
for(let i=1;i<=4;i++){
  document.getElementById('wbCorner'+i).value='1 2 3 4 5 6';
  document.getElementById('wbTipGap'+i).value='12';
}
wbTool0TipOffset.value='13';
expect(!await waamSubstrate.apply(),'invalid corner geometry was accepted');
expect(emitBtn.disabled && saveBtn.disabled,'failed server registration unlocked old job');
expect(JSON.stringify(waamStudio.state.toolpath)===before,'failed registration discarded preview');
expect(wbCorner1.value==='1 2 3 4 5 6','failed registration discarded measurements');
await placeBtn.onclick();await sliceBtn.onclick();
expect(emitBtn.disabled,'old settings silently cleared the failed registration');
expect(await waamSubstrate.apply(true),'explicit clear did not recover');
expect(!emitBtn.disabled && !saveBtn.disabled,'successful clear left export locked');
return {pending:waamSubstrate.pending(),generation:waamStudio.state.meta.gen};
""", self.base)
        self.assertFalse(result["pending"])
        self.assertEqual(result["generation"], server._current["gen"])


class TestOneJobPerWorkspace(IsolatedServerCase):


    def slice(self, name, lh):
        req = urllib.request.Request(
            f"{self.base}/api/slice?kind=wall&shape=wall&cell=0&repair=0"
            f"&name={name}&lh={lh}", data=b"", method="POST")
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())

    def test_two_interleaved_requests_cannot_mix_one_record(self):
        order, seen = [], threading.Lock()
        real = server._run_slice

        def traced(params, stl, workspace="local", email=None, *, prepared=None):
            with seen:
                order.append(("in", params.get("name")))
            time.sleep(0.05)
            out = real(params, stl, workspace, email, prepared=prepared)
            with seen:
                order.append(("out", params.get("name")))
            return out

        before = server._state("local")["gen"]
        results = {}
        with mock.patch.object(server, "_run_slice", traced):
            threads = [threading.Thread(
                target=lambda n=n, h=h: results.__setitem__(n, self.slice(n, h)))
                for n, h in (("wsA", "6"), ("wsB", "4"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)

        self.assertEqual(len(order), 4, order)
        for i in (0, 2):
            self.assertEqual(order[i][0], "in", order)
            self.assertEqual(order[i + 1], ("out", order[i][1]), order)

        state = server._state("local")
        self.assertEqual(state["gen"], before + 2)

        last = order[-1][1]
        self.assertEqual(state["emit"]["part"], last)
        self.assertEqual(state["request"]["params"]["name"], last)
        self.assertIn(f'"title":"{last}"', state["html"])

        gens = sorted(r["state"]["meta"]["gen"] for r in results.values())
        self.assertEqual(gens, [before + 1, before + 2])


class TestHardeningHeaders(IsolatedServerCase):


    def headers_for(self, path, **extra):
        req = urllib.request.Request(self.base + path, headers=extra)
        try:
            with urllib.request.urlopen(req) as response:
                return response.headers
        except urllib.error.HTTPError as exc:
            with exc as response:
                return response.headers

    def test_every_response_carries_them(self):
        for path in ("/api/whoami", "/no-such-route"):
            with self.subTest(path=path):
                headers = self.headers_for(path)
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(headers.get("Referrer-Policy"), "same-origin")
                self.assertEqual(headers.get("Content-Security-Policy"),
                                 "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")

    def test_the_server_header_does_not_name_the_python_version(self):
        served = self.headers_for("/api/whoami").get("Server", "")
        self.assertEqual(served.strip(), "waam-workbench")
        self.assertNotIn("Python", served)

    def test_hsts_only_when_the_proxy_says_the_hop_was_tls(self):
        self.assertIsNone(self.headers_for("/api/whoami").get("Strict-Transport-Security"))
        secured = self.headers_for("/api/whoami", **{"X-Forwarded-Proto": "https"})
        self.assertEqual(secured.get("Strict-Transport-Security"), "max-age=31536000")

    def test_a_stalled_connection_is_dropped(self):


        self.assertEqual(_Handler.timeout, 30)

        class _Quick(_Handler):
            timeout = 0.3

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Quick)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        sock = socket.create_connection(httpd.server_address, timeout=10)
        self.addCleanup(sock.close)
        sock.settimeout(10)
        self.assertEqual(sock.recv(1024), b"",
                         "the stalled connection should have been closed")


class TestCrossSitePosts(IsolatedServerCase):


    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.host = f"127.0.0.1:{cls.port}"

    def post(self, path, **headers):
        req = urllib.request.Request(self.base + path, data=b"", method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc as response:
                return response.code, response.read()

    def test_a_cross_site_fetch_is_refused(self):
        for path in ("/no-such-route", "/auth/signin", "/auth/guest"):
            with self.subTest(path=path):
                code, body = self.post(path, **{"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(code, 403, body)
                self.assertIn(b"cross-site", body)

    def test_a_foreign_origin_is_refused_even_without_fetch_metadata(self):
        code, body = self.post("/no-such-route", Origin="https://evil.example")
        self.assertEqual(code, 403, body)
        code, body = self.post("/auth/signin", Origin="null")
        self.assertEqual(code, 403, body)

    def test_the_public_url_matches_the_way_a_browser_writes_it(self):


        handler = _Handler.__new__(_Handler)
        handler.headers = {"Host": "127.0.0.1:8017"}
        handler.client_address = ("127.0.0.1", 1)
        for configured, expected in (("https://cell.example.ts.net:443",
                                      "https://cell.example.ts.net"),
                                     ("https://cell.example.ts.net",
                                      "https://cell.example.ts.net"),
                                     ("https://cell.example.ts.net:8443/",
                                      "https://cell.example.ts.net:8443")):
            cfg = server.oidc.AuthConfig(provider="local", client_id="",
                                         client_secret="", public_url=configured)
            with mock.patch.object(server, "_refresh_auth", return_value=cfg):
                self.assertIn(expected, handler._own_origins(), configured)

    def test_a_cross_site_link_cannot_build_or_file_a_job(self):


        for path in ("/api/jbi", "/api/cycle-job", "/api/sense-job",
                     "/api/layer-sense-job", "/api/ros2", "/api/gcode"):
            with self.subTest(path=path):
                req = urllib.request.Request(self.base + path + "?gen=1",
                                             headers={"Sec-Fetch-Site": "cross-site"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req)
                with caught.exception as response:
                    self.assertEqual(response.code, 403)
                    self.assertIn(b"cross-site", response.read())
        req = urllib.request.Request(self.base + "/api/gcode",
                                     headers={"Sec-Fetch-Site": "same-origin"})
        try:
            code = urllib.request.urlopen(req).status
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
        self.assertNotEqual(code, 403, "the page's own download link must still work")

    def test_a_non_ascii_digit_gen_is_stale_not_a_dropped_connection(self):
        handler = _Handler.__new__(_Handler)
        sent = []
        handler._json = lambda code, body: sent.append(code)
        self.addCleanup(server._STATES.pop, "gen-digits", None)
        server._state("gen-digits")["gen"] = 2
        self.assertTrue(handler._stale_gen("gen-digits", {"gen": ["\u00b2"]}))
        self.assertEqual(sent, [409])
        self.assertFalse(handler._stale_gen("gen-digits", {"gen": ["2"]}))

    def test_our_own_page_still_posts(self):
        for headers in ({"Origin": self.base},
                        {"Sec-Fetch-Site": "same-origin", "Origin": self.base},
                        {"Sec-Fetch-Site": "none"},
                        {}):
            with self.subTest(headers=headers):
                code, body = self.post("/no-such-route", **headers)
                self.assertEqual(code, 404, body)


class TestErrorBodiesStayGeneric(IsolatedServerCase):


    def post(self, path):
        req = urllib.request.Request(self.base + path, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc as response:
                return exc.code, response.read()

    def test_an_unexpected_slice_failure_keeps_its_internals(self):
        boom = RuntimeError("unpack_from requires a buffer of at least 84 bytes")
        with mock.patch.object(server, "run_slice", side_effect=boom),                 contextlib.redirect_stderr(io.StringIO()) as err:
            code, body = self.post("/api/slice?kind=wall&shape=wall&lh=6&cell=0&name=x")
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertNotIn("unpack_from", payload["error"])
        self.assertNotIn("Traceback", payload["error"])
        self.assertIn("unpack_from", err.getvalue())
        self.assertIn("Traceback", err.getvalue())

    def test_a_pipeline_refusal_still_reaches_the_operator(self):


        code, body = self.post("/api/slice?kind=wall&shape=wall&filler=missing&cell=0")
        self.assertEqual(json.loads(body),
                         {"ok": False, "error": "Filler: choose a filler from the list"})


class TestSlicingIsBounded(IsolatedServerCase):


    def post(self, path):
        req = urllib.request.Request(self.base + path, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            with exc as response:
                return exc.code, response.read(), response.headers

    def test_a_slice_past_the_limit_is_refused_not_queued(self):
        self.assertEqual(server.MAX_CONCURRENT_SLICES, 3)
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(20)
            return {"ok": True}

        holder = threading.Thread(
            target=lambda: self.post("/api/slice?kind=wall&shape=wall&cell=0&name=slow"))
        with mock.patch.object(server, "_SLICE_SEM", threading.BoundedSemaphore(1)),                 mock.patch.object(server, "run_slice", side_effect=slow):
            holder.start()
            try:
                self.assertTrue(started.wait(20), "the first slice never started")
                code, body, headers = self.post("/api/slice?kind=wall&shape=wall&cell=0&name=b")
                place, _, _ = self.post("/api/place?ox=0&oy=0")
                load, _, _ = self.post("/api/load?name=anything")
            finally:
                release.set()
                holder.join(20)
        self.assertEqual(code, 429, body)
        self.assertEqual(headers.get("Retry-After"), "5")
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(place, 429, "placement shares the budget")
        self.assertEqual(load, 429, "replaying a saved build shares the budget")

    def _reader(self, *, guest, size):
        handler = _Handler.__new__(_Handler)
        handler.waam_who = {"guest": guest}
        handler.headers = {"Content-Length": str(size)}
        handler.rfile = io.BytesIO(b"x" * size)
        handler.replies = []
        handler._json = lambda code, obj, cookie=None: handler.replies.append((code, obj))
        return handler

    def test_a_guest_gets_the_smaller_upload_cap(self):
        self.assertEqual(server.MAX_UPLOAD_BYTES_GUEST, 8 * 1024 * 1024)
        self.assertLess(server.MAX_UPLOAD_BYTES_GUEST, server.MAX_UPLOAD_BYTES)
        with mock.patch.object(server, "MAX_UPLOAD_BYTES", 1000),                 mock.patch.object(server, "MAX_UPLOAD_BYTES_GUEST", 100):
            over = self._reader(guest=True, size=200)
            self.assertIsNone(over._read_stl_body())
            self.assertEqual(over.replies[0][0], 413)
            self.assertIn("Sign in", over.replies[0][1]["error"])

            signed_in = self._reader(guest=False, size=200)
            self.assertEqual(signed_in._read_stl_body(), b"x" * 200)
            under = self._reader(guest=True, size=90)
            self.assertEqual(under._read_stl_body(), b"x" * 90)


class TestForwardedForTrust(unittest.TestCase):


    class _Fake:
        def __init__(self, peer, xff=None):
            self.client_address = (peer, 51515)
            self.headers = {"X-Forwarded-For": xff} if xff else {}

    def test_the_last_hop_is_the_one_our_proxy_wrote(self):
        self.assertEqual(server._client(self._Fake("127.0.0.1", "203.0.113.9")),
                         ("203.0.113.9", "127.0.0.1"))

        self.assertEqual(server._client(
            self._Fake("127.0.0.1", "10.0.0.1, 8.8.8.8, 203.0.113.9")),
            ("203.0.113.9", "127.0.0.1"))

    def test_a_non_loopback_peer_cannot_forge_a_client(self):
        self.assertEqual(server._client(self._Fake("198.51.100.7", "203.0.113.9")),
                         ("198.51.100.7", None))

    def test_no_header_reports_the_peer(self):
        self.assertEqual(server._client(self._Fake("198.51.100.7")),
                         ("198.51.100.7", None))


if __name__ == "__main__":
    unittest.main()
