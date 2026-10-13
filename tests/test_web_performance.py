import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import server, web_assets
from tests._support import isolated_server, serving


class TestDisplayTransport(unittest.TestCase):
    def test_encoding_negotiation(self):
        for value in ("gzip", "br, GZip;q=0.5", "*;q=1", "gzip; q=1"):
            self.assertTrue(web_assets.accepts_gzip(value), value)
        for value in ("", "br", "gzip;q=0, *;q=1", "gzip;q=nan", "gzip;q=2", "gzip;q=bad"):
            self.assertFalse(web_assets.accepts_gzip(value), value)

    def test_hosted_page_drops_repeated_robot_data_and_keeps_offline_export(self):
        with patch.object(server, "_HOSTED_ASSETS", True):
            result = server.run_slice({"name": "transport test", "repair": "0"}, None, "transport-test")
        self.assertIsNone(result["state"]["robot"]["meshes"])
        self.assertTrue(result["state"]["robot"]["mesh_asset"].startswith("robot-"))
        page = server._STATES.pop("transport-test")["html"]
        self.assertLess(len(page.encode()), 2_000_000)
        self.assertIn('id="visualDetail"', page)
        self.assertNotIn("%%", page)
        self.assertIn("async function()", page)
        from waam_pipeline.studio import render_studio
        state = result["state"]
        state["robot"]["meshes"] = json.loads(web_assets.assets()["robot"]["body"])
        offline = render_studio(state)
        self.assertGreater(len(offline), 30_000_000)
        self.assertNotIn('id="visualDetail"', offline)

    def test_fixed_assets_auth_and_no_store_with_lossless_gzip(self):
        with isolated_server(tokens={"test-token": "test-user"}, token_gate=True), \
                serving() as httpd:
            base = f"http://127.0.0.1:{httpd.server_port}/?visual="
            asset = web_assets.assets()["renderer"]
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(base + asset["key"])
            self.assertEqual(refused.exception.code, 401)
            for coding in ("gzip", "gzip;q=0, *;q=1"):
                request = urllib.request.Request(base + asset["key"] + "&k=test-token",
                                                 headers={"Accept-Encoding": coding})
                with urllib.request.urlopen(request) as response:
                    body = response.read()
                    self.assertIn("no-store", response.headers["Cache-Control"])
                    if coding == "gzip":
                        self.assertEqual(response.headers["Content-Encoding"], "gzip")
                        self.assertEqual(gzip.decompress(body), asset["body"])
                    else:
                        self.assertIsNone(response.headers.get("Content-Encoding"))
                        self.assertEqual(body, asset["body"])
            for key in ("../../saves/test-user/request.json", "robot-" + "0" * 64):
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(base + key + "&k=test-token")
                self.assertEqual(missing.exception.code, 404)


@unittest.skipUnless(shutil.which("node"), "Node required for headless display tests")
class TestBrowserAssets(unittest.TestCase):
    def run_js(self, body):
        setup = "const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');\n"
        setup += "const source=" + json.dumps(str(ROOT / "src/waam_pipeline/studio_assets.js")) + ";\n"
        result = subprocess.run([shutil.which("node")], input=setup + body, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_phone_skips_heavy_downloads_desktop_reuses_and_override_works(self):
        self.run_js("""
(async()=>{
let calls=0, scripts=0, fail=false;
const robot={base_link:{tris:[0,0,0,1,0,0,0,1,0]}}, saved={};
const w={location:{search:''}, matchMedia:()=>({matches:true}),
  localStorage:{getItem:k=>saved[k],setItem:(k,v)=>saved[k]=v},
  fetch:async()=>{calls++;return {ok:!fail,json:async()=>robot};},
  document:{createElement:()=>({remove(){}}),head:{appendChild:s=>{scripts++;w.WaamRenderer={};s.onload();}}}};
vm.runInNewContext(fs.readFileSync(source,'utf8'),{window:w,URLSearchParams});
const state=()=>({robot:{mesh_asset:'robot-'+'a'.repeat(64)}}), key='renderer-'+'b'.repeat(64);
await w.WaamAssets.prepare(state(),key);
assert.equal(calls,0);assert.equal(scripts,0);assert.ok(w.WaamAssets.light);
w.WaamAssets.setMode('full');const a=state(),b=state();
await Promise.all([w.WaamAssets.prepare(a,key),w.WaamAssets.prepare(b,key)]);
assert.equal(calls,1);assert.equal(scripts,1);assert.equal(a.robot.meshes,b.robot.meshes);
w.WaamAssets.setMode('light');await w.WaamAssets.hydrate(a);assert.equal(a.robot.meshes,null);
w.WaamAssets.setMode('auto');w.matchMedia=()=>({matches:false});w.WaamAssets.setMode('auto');
assert.equal(w.WaamAssets.light,false);await w.WaamAssets.prepare(a,key);assert.equal(calls,1);
fail=true;const bad={robot:{mesh_asset:'robot-'+'c'.repeat(64)}};
await assert.rejects(w.WaamAssets.hydrate(bad));fail=false;await w.WaamAssets.hydrate(bad);
assert.equal(calls,3);assert.equal(bad.robot.meshes,robot);
assert.equal(Object.keys(saved).length,1); // only the detail preference, never state
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_a_failed_download_does_not_persist_light_preview(self):

        self.run_js("""
(async()=>{
const saved={};
const w={location:{search:''},matchMedia:()=>({matches:false}),
localStorage:{getItem:k=>saved[k],setItem:(k,v)=>{saved[k]=v;}},
fetch:async()=>({ok:true,json:async()=>({base_link:{tris:[0,0,0,1,0,0,0,1,0]}})}),
document:{createElement:()=>({remove(){}}),head:{appendChild:s=>s.onerror()}}};
vm.runInNewContext(fs.readFileSync(source,'utf8'),{window:w,URLSearchParams});
const state={robot:{mesh_asset:'robot-'+'a'.repeat(64)}};
await assert.rejects(w.WaamAssets.prepare(state,'renderer-'+'b'.repeat(64)));
w.WaamAssets.setMode('light',false);            // the page's own fallback
assert.ok(w.WaamAssets.light);
assert.equal(Object.keys(saved).length,0);      // next visit starts at Automatic
w.WaamAssets.setMode('light');                  // the user picking it in Layers
assert.equal(saved.waamPreviewDetail,'light');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_late_mesh_response_cannot_restore_full_detail_after_fallback(self):
        self.run_js("""
(async()=>{
let finish;const w={location:{search:''},matchMedia:()=>({matches:false}),
localStorage:{getItem:()=>null,setItem:()=>{}},fetch:()=>new Promise(resolve=>{finish=resolve;}),
document:{createElement:()=>({remove(){}}),head:{appendChild:s=>s.onerror()}}};
vm.runInNewContext(fs.readFileSync(source,'utf8'),{window:w,URLSearchParams});
const state={robot:{mesh_asset:'robot-'+'a'.repeat(64)}};
await assert.rejects(w.WaamAssets.prepare(state,'renderer-'+'b'.repeat(64)));
w.WaamAssets.setMode('light');await w.WaamAssets.hydrate(state);
finish({ok:true,json:async()=>({base_link:{tris:[1,2,3]}})});
await new Promise(resolve=>setImmediate(resolve));
assert.ok(w.WaamAssets.light);assert.equal(state.robot.meshes,null);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_geometry_reuse_material_groups_and_release(self):
        self.run_js("""
const path=require('node:path');const dir=path.dirname(source);
require(path.join(dir,'studio-renderer.js'));const T=require(path.join(dir,'_vendor/three-r180.cjs'));
const r=Object.create(WaamRenderer.StudioRenderer.prototype);
Object.assign(r,{T,geometryCache:new Map(),cachedGeometries:new Set(),usedGeometries:new Set(),materials:new Map(),parts:new T.Group()});
const tris=[0,0,0,1,0,0,0,1,0], groups=[{start:0,end:1,kind:'torch'}];
const a=r.mesh(tris,'robot',groups).geometry;let disposed=0;a.addEventListener('dispose',()=>disposed++);
r.clearParts();r.usedGeometries.clear();
const b=r.mesh(tris,'robot',groups).geometry;r.pruneGeometry();
assert.equal(a,b);assert.equal(disposed,0);assert.equal(b.groups.length,1);
r.clearParts();r.usedGeometries.clear();r.mesh(tris.slice(),'part');r.pruneGeometry();
assert.equal(disposed,1);assert.equal(r.geometryCache.size,1);
r.clearParts();r.usedGeometries.clear();r.pruneGeometry();assert.equal(r.geometryCache.size,0);
""")
