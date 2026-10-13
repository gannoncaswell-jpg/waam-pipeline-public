from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_hc, web_assets
from tests._support import isolated_server, serving


def hc_state():
    return {"meta": {"cell_profile": {"id": "hc10"},
                     "params": {"cell_profile": "hc10"}},
            "robot": {"meshes": {"private-upload": [123]}, "chain": ["hc-chain"]}}


class TestRobotAssetIdentity(unittest.TestCase):
    def setUp(self):

        cached = getattr(web_assets, "_hc_robot_asset", None)
        if cached is not None:
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)

    def test_hc_transport_uses_repository_hc_geometry_without_mutating_state(self):
        state = hc_state()
        transported = web_assets.transport_state(state)
        asset = web_assets.find(transported["robot"]["mesh_asset"])
        self.assertNotEqual(asset["key"], web_assets.assets()["robot"]["key"])
        expected = json.dumps(cell_hc._link_meshes(), separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(asset["body"]).hexdigest(), hashlib.sha256(expected).hexdigest())
        self.assertEqual(asset["key"], "robot-" + hashlib.sha256(asset["body"]).hexdigest())
        self.assertEqual(gzip.decompress(asset["gzip"]), asset["body"])
        self.assertEqual(asset["type"], "application/json")
        self.assertEqual(state["robot"]["meshes"], {"private-upload": [123]})
        self.assertIsNone(transported["robot"]["meshes"])
        self.assertIs(transported["robot"]["chain"], state["robot"]["chain"])

    def test_lc_and_robotless_paths_do_not_load_hc_geometry(self):
        with patch.object(cell_hc, "_link_meshes", side_effect=AssertionError("HC loaded for LC")):
            lc = web_assets.assets()["robot"]
            for state in ({"robot": {"meshes": None}},
                          {"meta": {"cell_profile": None, "params": {"cell_profile": "lc50s"}},
                           "robot": {"meshes": None}}):
                self.assertEqual(web_assets.transport_state(state)["robot"]["mesh_asset"], lc["key"])
            self.assertIs(web_assets.find(lc["key"]), lc)
            self.assertIsNone(web_assets.find("../../saves/private/part.stl"))
            state = {"meta": {"cell_profile": {"id": "hc10"}}, "robot": None}
            self.assertIs(web_assets.transport_state(state), state)

    def test_unknown_or_conflicting_identity_cannot_fall_back_to_lc(self):
        bad = [
            {"cell_profile": {"id": "future-robot"}},
            {"cell_profile": {}},
            {"cell_profile": "hc10"},
            {"params": {"cell_profile": "future-robot"}},
            {"cell_profile": {"id": "hc10"}, "params": {"cell_profile": "lc50s"}},
        ]
        with patch.object(web_assets, "assets", side_effect=AssertionError("LC fallback used")):
            for meta in bad:
                with self.subTest(meta=meta), self.assertRaises(ValueError):
                    web_assets.transport_state({"meta": meta, "robot": {"meshes": None}})

    def test_concurrent_hc_requests_build_one_fixed_asset(self):
        mesh = {"base_link": {"tris": [0, 0, 0, 1, 0, 0, 0, 1, 0], "offset": [0, 0, 0]}}
        with patch.object(cell_hc, "_link_meshes", return_value=mesh) as build:
            with ThreadPoolExecutor(max_workers=4) as pool:
                keys = list(pool.map(lambda _: web_assets.transport_state(hc_state())["robot"]["mesh_asset"], range(12)))
            self.assertEqual(len(set(keys)), 1)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(json.loads(web_assets.find(keys[0])["body"]), mesh)
            self.assertIsNone(web_assets.find("robot-" + "0" * 64))


class TestHcAssetHttp(unittest.TestCase):
    def test_hc_asset_uses_existing_auth_gate_and_no_store_compression(self):
        from waam_pipeline import server

        asset = web_assets.find(web_assets.transport_state(hc_state())["robot"]["mesh_asset"])
        with isolated_server(tokens={"test-token": "test-user"}, token_gate=True), \
                serving() as httpd:
            url = f"http://127.0.0.1:{httpd.server_port}/?visual=" + asset["key"]
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(url)
            with refused.exception as response:
                self.assertEqual(response.code, 401)
                self.assertIn("no-store", response.headers["Cache-Control"])
            for coding in ("gzip", "gzip;q=0, *;q=1"):
                request = urllib.request.Request(url + "&k=test-token", headers={"Accept-Encoding": coding})
                with urllib.request.urlopen(request) as response:
                    body = response.read()
                    self.assertIn("no-store", response.headers["Cache-Control"])
                    if coding == "gzip":
                        self.assertEqual(response.headers["Content-Encoding"], "gzip")
                        self.assertEqual(gzip.decompress(body), asset["body"])
                    else:
                        self.assertIsNone(response.headers.get("Content-Encoding"))
                        self.assertEqual(body, asset["body"])


@unittest.skipUnless(shutil.which("node"), "Node required for headless display tests")
class TestRobotBrowserCache(unittest.TestCase):
    def test_switching_hc_lc_hc_reuses_both_fixed_meshes(self):
        setup = "const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');\n"
        setup += "const source=" + json.dumps(str(ROOT / "src/waam_pipeline/studio_assets.js")) + ";\n"
        body = r"""
(async()=>{
const hcKey='robot-'+'a'.repeat(64),lcKey='robot-'+'b'.repeat(64);
const hc={base_link:{tris:[1,2,3]}},lc={base_link:{tris:[4,5,6]}};
const calls=[];
const w={location:{search:''},matchMedia:()=>({matches:false}),
  localStorage:{getItem:()=>null,setItem:()=>{}},
  fetch:async(url,options)=>{
    assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
    const key=new URLSearchParams(url.split('?')[1]).get('visual');calls.push(key);
    assert.ok([hcKey,lcKey].includes(key));return {ok:true,json:async()=>key===hcKey?hc:lc};
  }};
vm.runInNewContext(fs.readFileSync(source,'utf8'),{window:w,URLSearchParams});
const firstHc={robot:{mesh_asset:hcKey}},firstLc={robot:{mesh_asset:lcKey}},secondHc={robot:{mesh_asset:hcKey}};
await w.WaamAssets.hydrate(firstHc);await w.WaamAssets.hydrate(firstLc);await w.WaamAssets.hydrate(secondHc);
assert.deepEqual(calls,[hcKey,lcKey]);assert.equal(firstHc.robot.meshes,hc);assert.equal(firstLc.robot.meshes,lc);
assert.equal(secondHc.robot.meshes,firstHc.robot.meshes);
w.WaamAssets.setMode('light');await w.WaamAssets.hydrate(secondHc);assert.equal(secondHc.robot.meshes,null);
w.WaamAssets.setMode('full');await w.WaamAssets.hydrate(secondHc);
assert.deepEqual(calls,[hcKey,lcKey]);assert.equal(secondHc.robot.meshes,hc);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node")], input=setup + body, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
