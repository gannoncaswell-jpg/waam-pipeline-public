import copy
from dataclasses import asdict, replace
import json
import shutil
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline import server
from waam_pipeline.interpass import illustrative_profile
from waam_pipeline.studio import render_studio
from waam_pipeline.slicer import wall
from tests.test_studio import _block_after
from tests.test_thermal import profile as goldak_test_profile

NODE = shutil.which("node")


class TestThermalIntegration(unittest.TestCase):
    def setUp(self):
        self.workspace = "interpass-tests"
        self.params = dict(kind="wall", shape="wall", cell="0", thermal="illustrative",
                           cooling="adaptive", lh="2.9", bead="8.6", travel="175")
        self.addCleanup(server._STATES.pop, self.workspace, None)

    def run_slice(self, params=None):
        return server.run_slice(params or self.params, None, self.workspace)

    def test_opt_in_report_does_not_change_clearance_motion_or_emit_arguments(self):
        before = self.run_slice(dict(self.params, thermal="off"))
        before_tp = copy.deepcopy(server._state(self.workspace)["toolpath"])
        before_emit = dict(server._state(self.workspace)["emit"])
        after = self.run_slice()
        self.assertEqual(after["state"]["thermal"], after["thermal"])
        self.assertTrue(after["thermal"]["planning_only"])
        self.assertEqual(after["state"]["violations"], before["state"]["violations"])
        self.assertEqual(server._state(self.workspace)["toolpath"], before_tp)
        self.assertEqual(set(server._state(self.workspace)["emit"]), set(before_emit))
        self.assertNotIn("allow_no_interpass", server._state(self.workspace)["emit"])
        self.assertNotIn("thermal", server._state(self.workspace)["emit"])
        self.assertEqual(after["state"]["schedule"], before["state"]["schedule"])

    def test_supplied_profile_matches_process_and_survives_request_replay(self):
        profile = replace(illustrative_profile(), evidence="supplied", name="Test inputs")
        params = dict(self.params, thermal="profile", thermal_profile=json.dumps(asdict(profile)))
        a = self.run_slice(params)
        saved = copy.deepcopy(server._state(self.workspace)["request"])
        b = self.run_slice(saved["params"])
        self.assertEqual(a["thermal"], b["thermal"])
        for key, value in (("travel", "180"), ("bead", "9"), ("lh", "3"),
                           ("wire", "1.2"), ("filler", "er308l"), ("sub", "8")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "does not match"):
                self.run_slice(dict(params, **{key: value}))

    def test_invalid_profile_cannot_replace_previous_checked_state(self):
        good = self.run_slice()
        original = server._state(self.workspace)["html"]
        for value in ("[]", "{}", "null", "not-json", " "*12001):
            with self.subTest(value=value[:10]), self.assertRaises(ValueError):
                self.run_slice(dict(self.params, thermal="profile", thermal_profile=value))
            self.assertEqual(server._state(self.workspace)["html"], original)
        self.assertTrue(good["ok"])

    def test_model_refusal_is_visible_and_does_not_discard_clearance(self):
        with patch("waam_pipeline.interpass.simulate", side_effect=ValueError("outside model domain")):
            result = self.run_slice()
        self.assertTrue(result["ok"])
        self.assertEqual(result["thermal"]["status"], "unavailable")
        self.assertEqual(result["thermal"]["error"], "outside model domain")
        self.assertIn("violations", result["state"])

    def test_placement_recomputes_thermal_report(self):
        self.run_slice()
        with patch("waam_pipeline.interpass.simulate", return_value={"status": "test recomputed"}) as model:
            result = server.run_placement({"yaw": "10"}, self.workspace)
        self.assertTrue(model.called)
        self.assertEqual(result["thermal"]["status"], "test recomputed")

    def test_uploaded_goldak_reaches_solver_and_replays_without_affecting_emit(self):
        p = goldak_test_profile()
        params = dict(self.params, thermal="profile", thermal_profile=json.dumps(asdict(p)),
                      bead="4", lh="2", travel="600", sub="2", cooling="continuous")
        with patch("waam_pipeline.server.demos.build", return_value=wall(4, 2, 2)):
            off = self.run_slice(dict(params, thermal="off"))
            r = self.run_slice(params)
            saved = copy.deepcopy(server._state(self.workspace)["request"]["params"])
            replay = self.run_slice(saved)
        self.assertEqual(r["thermal"]["heat"]["model"], "goldak_finite_volume")
        self.assertEqual(r["thermal"], replay["thermal"])
        self.assertEqual(r["state"]["violations"], off["state"]["violations"])
        self.assertEqual(r["state"]["schedule"], off["state"]["schedule"])
        self.assertNotIn("thermal", server._state(self.workspace)["emit"])
        self.assertNotIn("allow_no_interpass", server._state(self.workspace)["emit"])

        refused = self.run_slice(params)
        self.assertEqual(refused["thermal"]["status"], "unavailable")
        self.assertIn("footprint", refused["thermal"]["error"])

    @unittest.skipUnless(NODE, "Node needed for actual thermal renderer")
    def test_renderer_uses_text_for_profile_and_shows_pause_and_stop_reasons(self):
        state = self.run_slice()["state"]
        t = state["thermal"]
        t["profile"]["provenance"] = '<img src=x onerror="throw 1">'
        t["passes"][-1]["status"] = "stop"
        t["passes"][-1]["trigger"]["reasons"] = ["height_error"]
        script = render_studio(state).split("<script data-app>", 1)[1].split("</script>", 1)[0]
        renderer = "function renderThermal()" + _block_after(script, r"function renderThermal\(\)")
        setup = r"""
const assert=require('assert');let cursor=0,N=100,slider={},drawn=false;
function setPlaying(){}function draw(){drawn=true;}
class Element {
 constructor(tag){this.tag=tag;this.children=[];this._text='';}
 set innerHTML(v){throw new Error('Unsafe HTML in thermal panel');}
 set textContent(v){this._text=v;this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join(' ');}
 appendChild(c){this.children.push(c);}
}
const body=new Element('div');
const document={getElementById:id=>body,createElement:tag=>new Element(tag)};
"""
        checks = r"""
renderThermal();
assert.ok(body.textContent.includes('Not calibrated'));
assert.ok(body.textContent.includes('<img src=x'));
assert.ok(body.textContent.includes('cool '));
assert.ok(body.textContent.includes('accumulated height error'));
const detail=body.children.find(c=>c.tag==='details' && c.children.some(child=>child.tag==='button'));
detail.children.find(c=>c.tag==='button').onclick();assert.ok(drawn);
S.thermal={status:'unavailable',error:'missing response'};renderThermal();
assert.ok(body.textContent.includes('missing response'));
S.thermal=null;renderThermal();assert.ok(body.textContent.includes('Simulation is off'));
"""
        result = subprocess.run([NODE, "-e", "const S="+json.dumps(state)+";"+setup+renderer+checks],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
