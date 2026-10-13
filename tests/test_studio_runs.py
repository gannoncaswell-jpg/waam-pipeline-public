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
IDS = ("wbRunName", "runStatus", "wbRunAddBtn", "wbRunFiles", "wbRunTemperature", "wbRunUnit",
       "wbRunStage", "wbRunTemperatureBtn", "wbRunNote", "wbRunNoteBtn", "wbRunOutcome", "wbRunDataClass", "wbRunHeight",
       "wbRunWidth", "wbRunNotes", "wbRunArcmon", "wbRunWaiver", "wbRunFinishBtn", "runMessage",
       "runRecent", "runArcmonPasses")


def shell():
    state = build_state(wall(100, 4, 2.0), workbench=True, violations=[])
    state["meta"].update(params={"cell_profile": "lc50s"})
    parser = _ShellNodes()
    parser.feed(render_studio(state).split("<script data-app>", 1)[0])
    return state, parser


class TestRunLogLayout(unittest.TestCase):
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
                self.assertIn("runPanel", ancestors)
                self.assertIn("step-export", ancestors)

    def test_the_page_carries_the_module_and_names_every_export(self):
        state, _ = shell()
        script = render_studio(state).split("<script data-app>", 1)[1]
        self.assertIn("WaamRuns", script)
        self.assertGreaterEqual(script.count("run_name:runName()"), 5)


@unittest.skipUnless(NODE, "Node executes the run log controls")
class TestRunLogControls(unittest.TestCase):
    def run_js(self, body):
        state, parser = shell()
        source = ROOT / "src/waam_pipeline/studio_runs.js"
        setup = "const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL=" + json.dumps(state) + ";\n" + _DOM
        setup += "\nconst R=require(" + json.dumps(str(source)) + ").WaamRuns;\n"
        setup += r"""
let guest=false,busy=false,confirmed=true,calls=[],active=null,recent=[],failNext=null,blocked=null;
const reply=(ok,data,status=200)=>({ok,status,json:async()=>data});
const env={document,busy:()=>busy,guest:()=>guest,blocked:()=>blocked,confirm:()=>confirmed,changed:()=>{},
  query:fields=>new URLSearchParams(fields).toString(),begin:()=>true,end:()=>{},
  request:async(path,options={})=>{
    calls.push({path,options});
    if(failNext){const error=failNext;failNext=null;return reply(false,{ok:false,error},400);}
    if(path==="/api/run")return reply(true,{ok:true,active,recent});
    if(path.startsWith("/api/run/add")&&path.includes("kind=arcmon"))active.arc_monitor.files.push("A.CSV");
    return reply(true,{ok:true,active,recent});
  }};
const open=()=>({run:"2026-10-02_WALL10_r01",started_at:"2026-10-02T21:04:00-05:00",name:"Wall",
  files:[{name:"x"}],arc_monitor:{files:[],summaries:[],waiver:null}});
const control=R.create(env);
const posts=path=>calls.filter(r=>r.path===path&&r.options.method==="POST");
"""
        result = subprocess.run([NODE], input=setup + "\n(async()=>{\n" + body +
                                "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_open_run_disables_everything_but_the_run_name(self):
        self.run_js(r"""
assert.equal(el('runStatus').textContent,'Loading run log…');
await control.refresh();
assert.match(el('runStatus').textContent,/^No run open\./);
assert.equal(el('wbRunName').disabled,false);
for(const id of ['wbRunAddBtn','wbRunTemperatureBtn','wbRunNoteBtn','wbRunFinishBtn','wbRunArcmon','wbRunWaiver'])
  assert.equal(el(id).disabled,true,id);
assert.equal(el('runRecent').textContent,'No finished runs yet.');
""")

    def test_finish_needs_arc_monitor_data_a_waiver_or_not_run(self):
        self.run_js(r"""
active=open();await control.refresh();
assert.match(el('runStatus').textContent,/^Run 2026-10-02_WALL10_r01 · open since /);
assert.equal(el('wbRunName').disabled,true);
assert.equal(el('wbRunFinishBtn').disabled,true);
el('wbRunWaiver').checked=true;el('wbRunWaiver').dispatch('change');
assert.equal(el('wbRunFinishBtn').disabled,false);
el('wbRunWaiver').checked=false;el('wbRunWaiver').dispatch('change');
el('wbRunOutcome').value='not-run';el('wbRunOutcome').dispatch('change');
assert.equal(el('wbRunFinishBtn').disabled,false);
el('wbRunOutcome').value='completed';el('wbRunOutcome').dispatch('change');
el('wbRunArcmon').files=[{name:'1-20251103-181340.CSV',arrayBuffer:async()=>new ArrayBuffer(3)}];
await el('wbRunArcmon').onchange();
const add=calls.find(r=>r.path.startsWith('/api/run/add'));
assert.equal(new URLSearchParams(add.path.split('?')[1]).get('kind'),'arcmon');
assert.equal(new URLSearchParams(add.path.split('?')[1]).get('name'),'1-20251103-181340.CSV');
assert.ok(add.options.body instanceof ArrayBuffer);
assert.equal(el('wbRunFinishBtn').disabled,false);
""")

    def test_the_latest_arc_monitor_join_is_listed_per_pass(self):
        self.run_js(r"""
active=open();active.arc_monitor.files=['A.CSV'];
active.arc_monitor.summaries=[{kind:'realtime-csv',passes:{mode:'step',file:'X.arcmon-passes.csv',
  warnings:['1 arc-on stretch(es) carry no step of this job\'s passes'],rows_total:41,
  rows:Array.from({length:40},(_,i)=>({pass:i+1,asf:81,current_mean_a:296.05,voltage_mean_v:21.69,
    weld_s:1,energy_kj_mm:0.1284,arc_current_mean_a:260,arc_voltage_mean_v:20.5,
    arc_on_s:1.3,flags:i?'':'dropout'}))}}];
await control.refresh();
const lines=el('runArcmonPasses').children.map(c=>c.textContent);
assert.equal(lines[0],'Per pass (by step number) · X.arcmon-passes.csv');
assert.match(lines[1],/^Warning: 1 arc-on stretch/);
assert.equal(lines[2],'Pass 1 · ASF#81 · Welding: 296 A · 21.7 V · 1.0 s · 0.128 kJ/mm · Total arc: 260 A · 20.5 V · 1.3 s · dropout');
assert.equal(lines.at(-1),'1 more passes in X.arcmon-passes.csv.');
active.arc_monitor.summaries=[{kind:'realtime-csv',passes:{mode:'step',file:'Y.arcmon-passes.csv',
  warnings:['no welding samples in this file (strike/crater only)'],rows:[{pass:1,asf:81,
  current_mean_a:null,voltage_mean_v:null,weld_s:0,energy_kj_mm:null,
  arc_current_mean_a:60,arc_voltage_mean_v:15,arc_on_s:.3,flags:''}]}}];
await control.refresh();
assert.match(el('runArcmonPasses').textContent,/Welding: not available/);
assert.match(el('runArcmonPasses').textContent,/Total arc: 60 A · 15.0 V · 0.3 s/);
active.arc_monitor.summaries=[{kind:'realtime-csv',passes:{error:'broken'}}];
await control.refresh();
assert.equal(el('runArcmonPasses').textContent,'Arc monitor not joined to passes: broken');
active=null;await control.refresh();
assert.equal(el('runArcmonPasses').textContent,'');
""")

    def test_desktop_block_disables_run_actions_and_shows_the_reason(self):
        self.run_js(r"""
active=open();await control.refresh();
blocked='Desktop review is still open.';control.paint();
assert.equal(el('wbRunNoteBtn').disabled,true);assert.equal(el('wbRunFinishBtn').disabled,true);
assert.equal(el('runMessage').textContent,blocked);
const before=calls.length;el('wbRunNote').value='note';await el('wbRunNoteBtn').onclick();
assert.equal(calls.length,before);
blocked=null;control.paint();assert.equal(el('wbRunNoteBtn').disabled,false);
""")

    def test_finish_posts_the_form_only_after_confirmation(self):
        self.run_js(r"""
active=open();await control.refresh();
el('wbRunWaiver').checked=true;el('wbRunWaiver').dispatch('change');
el('wbRunOutcome').value='stopped';el('wbRunDataClass').value='controlled';
el('wbRunHeight').value='21.6';el('wbRunNotes').value=' deformed at layer 7 ';
confirmed=false;await el('wbRunFinishBtn').onclick();
assert.equal(posts('/api/run/finish').length,0);
confirmed=true;await el('wbRunFinishBtn').onclick();
const sent=JSON.parse(posts('/api/run/finish')[0].options.body);
assert.deepEqual(sent,{outcome:'stopped',data_class:'controlled',height_mm:21.6,width_mm:null,
  notes:'deformed at layer 7',arcmon_waiver:true});
""")

    def test_temperature_is_checked_before_it_is_sent(self):
        self.run_js(r"""
active=open();await control.refresh();
el('wbRunTemperature').value='3000';await el('wbRunTemperatureBtn').onclick();
assert.equal(posts('/api/run/temperature').length,0);
assert.match(el('runMessage').textContent,/-40 to 2500 °F/);
el('wbRunTemperature').value='730';el('wbRunStage').value='after-weld';
await el('wbRunTemperatureBtn').onclick();
const sent=JSON.parse(posts('/api/run/temperature')[0].options.body);
assert.deepEqual(sent,{value:730,unit:'F',stage:'after-weld',note:''});
assert.equal(el('wbRunTemperature').value,'');
""")

    def test_a_note_is_trimmed_posted_and_cleared(self):
        self.run_js(r"""
active=open();await control.refresh();
assert.equal(el('wbRunNote').disabled,false);
el('wbRunNote').value='   ';await el('wbRunNoteBtn').onclick();
assert.equal(posts('/api/run/note').length,0);
assert.match(el('runMessage').textContent,/Type a note/);
el('wbRunNote').value=' wire stubbed at layer 4 ';await el('wbRunNoteBtn').onclick();
assert.deepEqual(JSON.parse(posts('/api/run/note')[0].options.body),{text:'wire stubbed at layer 4'});
assert.equal(el('wbRunNote').value,'');
assert.equal(el('runMessage').textContent,'Note added to the run.');
""")

    def test_guests_see_a_disabled_panel(self):
        self.run_js(r"""
guest=true;await control.refresh();
assert.equal(el('runStatus').textContent,'Sign in to keep a run log.');
for(const id of ['wbRunName','wbRunAddBtn','wbRunTemperatureBtn','wbRunNoteBtn','wbRunFinishBtn'])
  assert.equal(el(id).disabled,true,id);
assert.equal(calls.length,0);
""")

    def test_a_failed_filing_is_reported_and_the_panel_refreshes(self):
        self.run_js(r"""
active=open();await control.refresh();
const before=calls.length;
control.response({headers:{get:name=>name==='X-Waam-Run'?'failed':null}});
assert.match(el('runMessage').textContent,/Run filing failed/);
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(calls.length,before+1);
assert.equal(calls.at(-1).path,'/api/run');
""")


if __name__ == "__main__":
    unittest.main()
