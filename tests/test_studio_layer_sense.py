import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from waam_pipeline.slicer import wall
from waam_pipeline.studio import build_state, render_studio
from tests.test_studio_controls import _ShellNodes, _DOM

NODE = shutil.which("node")
IDS = ("wbPrintLayers", "wbSenseLayer", "wbLayerSpacing", "wbLayerSenseName", "wbLayerSenseJobBtn",
       "wbLayerVarDatBtn", "wbLayerVarDatFile", "wbLayerSenseClearBtn", "layerSenseStatus",
       "layerSenseReadout", "wbLayerSense")


def shell():
    state = build_state(wall(100, 4, 2.0), workbench=True, violations=[])
    state["meta"].update(params={"cell_profile": "lc50s"})
    parser = _ShellNodes()
    parser.feed(render_studio(state).split("<script data-app>", 1)[0])
    return state, parser


class TestLayerSenseLayout(unittest.TestCase):
    def test_controls_appear_once_inside_the_export_step(self):
        _, parser = shell()
        for element_id in IDS:
            with self.subTest(element_id=element_id):
                matches = [n for n in parser.nodes if n["attrs"].get("id") == element_id]
                self.assertEqual(len(matches), 1)
                node, ancestors = matches[0], []
                while node["parent"] is not None:
                    node = parser.nodes[node["parent"]]
                    ancestors.append(node["attrs"].get("id"))
                self.assertIn("layerSensePanel", ancestors)
                self.assertIn("step-export", ancestors)
        nodes = {n["attrs"].get("id"): n for n in parser.nodes}
        self.assertEqual(nodes["wbPrintLayers"]["attrs"]["type"], "text")
        self.assertEqual(nodes["wbLayerSense"]["attrs"]["type"], "hidden")
        self.assertEqual(nodes["wbLayerSpacing"]["attrs"]["value"], "10")
        self.assertIn("P120", nodes["wbLayerVarDatBtn"]["text"])

    def test_the_page_carries_the_module_and_sends_the_sets_with_every_slice(self):
        state, _ = shell()
        page = render_studio(state)
        script = page.split("<script data-app>", 1)[1]
        self.assertIn("WaamLayerSense", script)
        self.assertEqual(script.count("layer_sense:wbLayerSense.value"), 2)
        self.assertEqual(script.count("+printLayers()"), 2)


@unittest.skipUnless(NODE, "Node executes the layer touch controls")
class TestLayerSenseControls(unittest.TestCase):
    def run_js(self, body, **meta):
        state, parser = shell()
        state["meta"].update(meta)
        source = ROOT / "src/waam_pipeline/studio_layer_sense.js"
        setup = "const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL=" + json.dumps(state) + ";\n" + _DOM
        setup += "\nconst L=require(" + json.dumps(str(source)) + ").WaamLayerSense;\n"
        setup += r"""
let busy=false,guest=false,accept=true,draftError='',plans=[],jobs=[],imports=[],applied=[];
let touches=[[1,2,3,4,5,6],[7,8,9,10,11,12],[13,14,15,16,17,18]],hits=[1,0,1];
const env={document,state:()=>S,busy:()=>busy,guest:()=>guest,draftError:()=>draftError,pause:()=>{},changed:()=>{},
  layerSensePlan:async o=>{plans.push(o);return {ok:true,count:3,base:120,flag_base:80,layer:o.layer};},
  layerSenseJob:async o=>{jobs.push(o);return {count:3,base:120,flag_base:80,layer:o.layer,archive:'saved'};},
  importVarDat:async(text,base,count,purpose,flags)=>{imports.push({text,base,count,flags});
    return {records:touches.slice(0,count).map((t,i)=>hits[i]?t:null),hits:hits.slice(0,count)};},
  apply:async raw=>{applied.push(raw);if(accept)el('wbLayerSense').value=raw;return accept;}};
const control=L.create(env);
async function importFile(text='export'){
  el('wbLayerVarDatFile').files=[{name:'VAR.DAT',text:async()=>text}];
  await el('wbLayerVarDatFile').onchange();
}
"""
        result = subprocess.run([NODE], input=setup + "\n(async()=>{\n" + body +
                                "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_download_sends_the_layer_and_reports_the_registers(self):
        self.run_js(r"""
el('wbSenseLayer').value='2';el('wbLayerSpacing').value='15';el('wbLayerSenseName').value='wall_l2';
await el('wbLayerSenseJobBtn').onclick();
assert.deepEqual(jobs,[{layer:2,spacing:15,name:'wall_l2'}]);
assert.match(el('layerSenseStatus').textContent,/WALL_L2\.JBI .*3 touches on layer 2 \(P120–P122\)/);
assert.match(el('layerSenseStatus').textContent,/two-person dry-run/);
assert.equal(applied.length,0);
""")

    def test_draft_disables_and_refuses_the_touch_export(self):
        self.run_js(r"""
el('wbLayerSense').value=JSON.stringify([{layer:1,points:[]}]);
draftError='Desktop review is still open.';control.paint();
assert.equal(el('wbLayerSenseJobBtn').disabled,true);
assert.equal(el('wbLayerVarDatBtn').disabled,true);assert.equal(el('wbLayerSenseClearBtn').disabled,true);
await el('wbLayerSenseJobBtn').onclick();assert.deepEqual(jobs,[]);
await importFile();await el('wbLayerSenseClearBtn').onclick();
assert.deepEqual(plans,[]);assert.deepEqual(imports,[]);assert.deepEqual(applied,[]);
assert.equal(el('wbLayerVarDatFile').value,'');
assert.equal(el('layerSenseStatus').textContent,draftError);
draftError='';control.paint();assert.equal(el('wbLayerSenseJobBtn').disabled,false);
""")

    def test_import_appends_a_set_that_goes_up_the_part(self):
        self.run_js(r"""
el('wbLayerSense').value=JSON.stringify([{layer:1,spacing:20,points:[[0,0,0,0,0,0]]}]);
el('wbSenseLayer').value='3';el('wbLayerSpacing').value='25';
await importFile('both registers');
assert.deepEqual(plans,[{layer:3,spacing:25,name:'LAYER_TOUCH'}]);
assert.deepEqual(imports,[{text:'both registers',base:120,count:3,flags:80}]);
assert.equal(applied.length,1);
const sets=JSON.parse(applied[0]);
assert.deepEqual(sets.map(s=>s.layer),[1,3]);assert.deepEqual(sets[1].points,[touches[0],null,touches[2]]);
assert.deepEqual([sets[1].base,sets[1].flag_base,sets[1].hits],[120,80,[1,0,1]]);assert.equal(sets[0].hits,undefined);
assert.equal(sets[1].spacing,25);
assert.match(el('layerSenseStatus').textContent,/Layer 3 touches imported.*2 of 3 touched/);
el('wbSenseLayer').value='2';await importFile();
assert.equal(applied.length,1);
assert.match(el('layerSenseStatus').textContent,/go up the part: the last one is layer 3/);
accept=false;el('wbSenseLayer').value='4';await importFile();
assert.match(el('layerSenseStatus').textContent,/not applied/);
assert.deepEqual(JSON.parse(el('wbLayerSense').value).map(s=>s.layer),[1,3]);
assert.equal(el('wbLayerVarDatFile').value,'');
""")

    def test_remove_the_last_set(self):
        self.run_js(r"""
el('wbLayerSense').value=JSON.stringify([{layer:1,points:[]},{layer:2,points:[]}]);control.paint();
await el('wbLayerSenseClearBtn').onclick();
assert.deepEqual(JSON.parse(applied[0]).map(s=>s.layer),[1]);
await el('wbLayerSenseClearBtn').onclick();
assert.equal(applied[1],'');
""")

    def test_readout_and_disabled_states(self):
        self.run_js(r"""
const ids=['wbSenseLayer','wbLayerSpacing','wbLayerSenseName','wbLayerSenseJobBtn','wbLayerVarDatBtn'];
for(const id of ids)assert.equal(el(id).disabled,false,id);
assert.match(el('layerSenseReadout').textContent,/Layer 1: 5 of 5 touched, top -0\.25 mm on average \(-0\.40 mm to \+0\.10 mm\), largest at P046/);
guest=true;control.paint();for(const id of ids)assert.equal(el(id).disabled,true,id);guest=false;
busy=true;control.paint();for(const id of ids)assert.equal(el(id).disabled,true,id);busy=false;
S.meta.params.cell_profile='hc10';control.paint();for(const id of ids)assert.equal(el(id).disabled,true,id);
S.meta.params.cell_profile='lc50s';S.meta.clearance_checked=false;control.paint();
for(const id of ids)assert.equal(el(id).disabled,true,id);
""", layer_sense=[{"layer": 1, "points": 5, "mean_error_mm": -0.25, "error_range_mm": [-0.4, 0.1],
                   "worst": "P046", "worst_error_mm": -0.4}])

    def test_readout_says_how_many_touched(self):
        self.run_js(r"""
assert.match(el('layerSenseReadout').textContent,/^Layer 2: 7 of 9 touched \(missed P122, P125\), top -0\.10 mm/);
""", layer_sense=[{"layer": 2, "points": 7, "touched": 7, "planned": 9, "missed": ["P122", "P125"],
                   "mean_error_mm": -0.1, "error_range_mm": [-0.2, 0.0], "worst": "P121", "worst_error_mm": -0.2}])


if __name__ == "__main__":
    unittest.main()
