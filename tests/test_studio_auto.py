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
IDS = ("wbBuildMode", "wbBuildManual", "wbBuildAuto", "autoOptions", "wbAutoSpacing", "wbAutoMax",
       "wbAutoMaxUnit", "autoPanel", "autoNext", "autoWarning", "autoSteps", "wbAutoBefore", "wbAutoAfter",
       "wbAutoUnit", "wbAutoVarBtn", "wbAutoVarFile", "autoFit", "autoHold", "autoHoldMessage",
       "wbAutoReading", "wbAutoReadingUnit", "wbAutoReadingBtn", "autoStatus", "wbManualStages", "autoManualHint")


def shell():
    state = build_state(wall(100, 6, 4.0), workbench=True, violations=[])
    state["meta"].update(params={"cell_profile": "lc50s", "lh": "4"}, clearance_checked=True, cell="LC50S",
                         auto={"cycles_done": 0, "next_first": 0, "step_mm": 4, "top_layer": 5})
    parser = _ShellNodes()
    parser.feed(render_studio(state).split("<script data-app>", 1)[0])
    return state, parser


class TestAutoLayout(unittest.TestCase):
    def test_controls_appear_once_inside_export(self):
        _, parser = shell()
        for element_id in IDS:
            with self.subTest(element_id=element_id):
                nodes = [n for n in parser.nodes if n["attrs"].get("id") == element_id]
                self.assertEqual(len(nodes), 1)
                node, ancestors = nodes[0], []
                while node["parent"] is not None:
                    node = parser.nodes[node["parent"]]
                    ancestors.append(node["attrs"].get("id"))
                self.assertIn("step-export", ancestors)
        nodes = {n["attrs"].get("id"): n for n in parser.nodes}
        self.assertEqual(nodes["wbBuildMode"]["attrs"]["value"], "manual")
        self.assertEqual(nodes["wbBuildMode"]["attrs"]["aria-label"], "Build mode")
        self.assertEqual(nodes["wbAutoSpacing"]["attrs"]["value"], "10")
        self.assertNotIn("required", nodes["wbAutoMax"]["attrs"])
        for element_id in ("wbAutoSpacing", "wbAutoMax", "wbAutoBefore", "wbAutoAfter", "wbAutoReading"):
            self.assertIn("data-auto-input", nodes[element_id]["attrs"])
            self.assertTrue(nodes[element_id]["attrs"]["data-error"])

    def test_module_is_wired_and_optional_numbers_do_not_block_slice(self):
        state, _ = shell()
        script = render_studio(state).split("<script data-app>", 1)[1]
        self.assertIn("WaamAuto", script)
        self.assertIn('"/api/cycle-job?"', script)
        self.assertIn('"/api/cycle-import?"', script)
        self.assertIn('"/api/run/cycle-applied?"', script)
        self.assertIn('.filter(el=>el.getAttribute("data-auto-input")===null)', script)
        self.assertIn('const notes=r.headers.get("X-Waam-Notes");', script)
        self.assertIn('notes:r.headers.get("X-Waam-Notes")||""', script)
        self.assertIn('(el.id==="wbBeadDwell" && n>0 && n<0.01)', script)
        self.assertIn('"wbBuildMode","wbAutoSpacing","wbAutoMax","wbAutoMaxUnit"', script)

    def test_cooling_request_uses_run_without_a_cycle_count_gate(self):
        state, _ = shell()
        script = render_studio(state).split("async function coolingFit(){", 1)[1]
        before_request = script.split('await json("/api/run/cooling?"+query)', 1)[0]
        self.assertIn('if(!runId)', before_request)
        self.assertIn('if(!enabled()||env.guest())', before_request)
        self.assertIn('if(!setpoint)', before_request)
        for old_gate in ("paired", "cycles", "t_before", "t_after"):
            self.assertNotIn(old_gate, before_request)

    def test_cooling_labels_use_the_backend_basis(self):
        state, _ = shell()
        script = render_studio(state)
        self.assertIn('(data.basis||f.basis||p.basis)==="layer_decay"', script)
        self.assertIn('basis=sheet?"sheet decay fit":"cycle pairs"', script)
        self.assertIn('Cooling fit (${basis}, ${count})', script)

    def test_cooling_refusal_displays_the_backend_message(self):
        state, _ = shell()
        script = render_studio(state)
        self.assertIn('throw new Error(data?.error||', script)
        cooling = script.split("async function coolingFit(){", 1)[1].split("function temperature", 1)[0]
        self.assertIn('cooling="Cooling fit: "+e.message;', cooling)
        self.assertIn('proposal=null;', cooling)


@unittest.skipUnless(NODE, "Node executes the Auto cycle controls")
class TestAutoControls(unittest.TestCase):
    def run_js(self, body):
        state, parser = shell()
        source = ROOT / "src/waam_pipeline/studio_auto.js"
        setup = "const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL=" + json.dumps(state) + ";\n" + _DOM
        setup += "\nconst A=require(" + json.dumps(str(source)) + ").WaamAuto;\n"
        setup += r"""
let busy=false,guest=false,accept=true,calls=[],exports=[],applied=[],active=null,exportError='',flags={},blocked=null;
const records=[[1,2,3,4,5,6],[7,8,9,10,11,12]];
const summary={cycle:1,mean_error_mm:-.42,growth_mm:2.96,growth_raw_mm:2.96,clamped:false};
const reply=(ok,data,status=200)=>({ok,status,json:async()=>data});
const env={document,state:()=>S,busy:()=>busy,guest:()=>guest,blocked:()=>blocked,changed:()=>{},pause:()=>{},refreshRun:()=>{},
  query:fields=>new URLSearchParams(fields).toString(),
  exportCycle:async options=>{exports.push(options);if(exportError)throw new Error(exportError);
    return {cycle:1,firstLayer:0,lastLayer:1,last_cycle:false,job:'WALL_C01',archive:'saved',run:'RUN',
      passes:' 2 passes · ASF#81,83 · travel 198–402 mm/min.'};},
  request:async(path,options={})=>{calls.push({path,options});
    if(path==='/api/run')return reply(true,{ok:true,active});
    if(path.startsWith('/api/cycle-import?'))return reply(true,{ok:true,records,summary,...flags});
    return reply(true,{ok:true});},
  apply:async raw=>{applied.push(raw);if(accept){el('wbLayerSense').value=raw;
    S.meta.auto={cycles_done:1,next_first:2,step_mm:2.96,top_layer:5};}return accept;}};
el('wbInterpassEvery').value='2';
const control=A.create(env);
function auto(){el('wbBuildMode').value='auto';control.paint();}
async function importFile(){el('wbAutoBefore').value='650';el('wbAutoAfter').value='480';
  el('wbAutoVarFile').files=[{name:'VAR.DAT',text:async()=>'touch records'}];
  await el('wbAutoVarFile').onchange();}
"""
        result = subprocess.run([NODE], input=setup + "\n(async()=>{\n" + body +
                                "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manual_default_and_auto_cycle_warning(self):
        self.run_js(r"""
assert.equal(control.enabled(),false);assert.equal(el('wbBuildManual').checked,true);
assert.equal(el('autoPanel').hidden,true);assert.equal(el('wbManualStages').disabled,false);
auto();assert.equal(el('emitBtn').textContent,'Export cycle 1 · layers 0-1');
assert.equal(el('wbManualStages').disabled,true);assert.equal(el('wbPrintLayers').disabled,true);
assert.match(el('autoWarning').textContent,/Bead height \(4 mm\)/);
assert.match(el('autoWarning').textContent,/37.5 % less/);assert.match(el('autoWarning').textContent,/alarm 8814/);
assert.match(el('autoWarning').textContent,/50 % more/);
el('wbBuildManual').dispatch('change');assert.equal(control.enabled(),false);
assert.equal(el('emitBtn').textContent,'Export robot job · .JBI');
""")

    def test_the_cycle_message_carries_the_pass_summary(self):
        self.run_js(r"""
auto();await control.exportCycle();
assert.equal(el('autoStatus').textContent,'Downloaded WALL_C01.JBI. 2 passes · ASF#81,83 · travel 198–402 mm/min.');
""")

    def test_desktop_block_disables_auto_export_and_import(self):
        self.run_js(r"""
auto();await control.exportCycle();assert.equal(exports.length,1);
blocked='Desktop review is still open.';control.paint();
assert.equal(el('emitBtn').disabled,true);assert.equal(el('wbAutoVarBtn').disabled,true);
assert.equal(el('autoStatus').textContent,blocked);
const callCount=calls.length,appliedCount=applied.length;
await control.exportCycle();assert.equal(exports.length,1);
await importFile();assert.equal(calls.length,callCount);assert.equal(applied.length,appliedCount);
assert.equal(el('wbAutoVarFile').value,'');
el('wbAutoReading').value='500';await el('wbAutoReadingBtn').onclick();
assert.equal(calls.length,callCount);
blocked=null;control.paint();await control.exportCycle();assert.equal(exports.length,2);
""")

    def test_export_and_import_apply_then_acknowledge(self):
        self.run_js(r"""
auto();el('wbAutoMax').value='500';await control.exportCycle();
assert.deepEqual(exports,[{spacing:10,max_interpass:500,max_unit:'F'}]);
assert.match(el('autoSteps').textContent,/Run WALL_C01.*At each pause read the temperature, then press START/);
await importFile();const request=calls.find(r=>r.path.startsWith('/api/cycle-import?'));
const query=new URLSearchParams(request.path.split('?')[1]);
assert.equal(query.get('cycle'),'1');assert.equal(query.get('t_before'),'650');assert.equal(query.get('t_after'),'480');
assert.equal(query.get('unit'),'F');assert.equal(request.options.body,'touch records');
assert.deepEqual(JSON.parse(applied[0]),[{layer:1,spacing:10,points:records,step:true}]);
assert.equal(calls.at(-1).path,'/api/run/cycle-applied?cycle=1');
assert.equal(calls.at(-1).options.method,'POST');
assert.equal(el('emitBtn').textContent,'Export cycle 2 · layers 2-3');
assert.match(el('autoFit').textContent,/Cycle 1: top -0.42 mm on average; growth 2.96 mm per layer \(raw 2.96, not clamped\)/);
assert.equal(el('wbAutoVarFile').value,'');
""")

    def test_flagged_cycle_carries_its_hits_and_reports_misses(self):
        self.run_js(r"""
flags={base:120,flag_base:80,hits:[1,0]};
Object.assign(summary,{touched:1,planned:2,missed:['P121']});
auto();await control.exportCycle();await importFile();
assert.deepEqual(JSON.parse(applied[0]),[{layer:1,spacing:10,points:records,step:true,
  base:120,flag_base:80,hits:[1,0]}]);
assert.match(el('autoFit').textContent,/Cycle 1: 1 of 2 touched \(missed P121\); top -0.42 mm on average/);
assert.match(el('wbAutoVarBtn').textContent,/P120/);
""")

    def test_failed_apply_can_retry_without_importing_twice(self):
        self.run_js(r"""
auto();await control.exportCycle();accept=false;await importFile();
assert.equal(calls.filter(r=>r.path.startsWith('/api/run/cycle-applied')).length,0);
assert.equal(el('wbAutoVarBtn').textContent,'Apply imported cycle');assert.match(el('autoStatus').textContent,/not applied/);
accept=true;await el('wbAutoVarBtn').onclick();
assert.equal(calls.filter(r=>r.path.startsWith('/api/cycle-import')).length,1);
assert.equal(calls.filter(r=>r.path.startsWith('/api/run/cycle-applied')).length,1);
""")

    def test_temperature_hold_records_a_new_pre_weld_reading(self):
        self.run_js(r"""
auto();exportError='Hold: the last reading is above the maximum interpass.';await control.exportCycle();
assert.equal(el('autoHold').hidden,false);assert.match(el('autoHoldMessage').textContent,/^Hold:/);
el('wbAutoReading').value='450';await el('wbAutoReadingBtn').onclick();
const request=calls.find(r=>r.path==='/api/run/temperature');
assert.deepEqual(JSON.parse(request.options.body),{value:450,unit:'F',stage:'before-weld',note:''});
assert.equal(el('autoHold').hidden,true);assert.match(el('autoStatus').textContent,/Export again/);
""")

    def test_refresh_restores_imported_records_and_last_cycle_message(self):
        self.run_js(r"""
auto();active={run:'RUN',cycles:[{cycle:1,layers:[0,1],spacing_mm:20,job:'WALL_C01',
  status:'imported',last_cycle:false,records,sense_base:120,flag_base:80,hits:[1,0],summary}]};await control.refresh();
assert.equal(el('wbAutoVarBtn').textContent,'Apply imported cycle');
await el('wbAutoVarBtn').onclick();assert.equal(applied.length,1);
assert.deepEqual(JSON.parse(applied[0]),[{layer:1,spacing:20,points:records,step:true,
  base:120,flag_base:80,hits:[1,0]}]);
active.cycles.push({cycle:3,layers:[4,5],job:'WALL_C03',status:'exported',last_cycle:true});
await control.refresh();assert.equal(el('wbAutoVarBtn').disabled,true);
assert.equal(el('autoSteps').textContent,'Last cycle: no touches. When it has run, finish the run.');
""")

    def test_manual_sets_and_bad_temperatures_are_refused(self):
        self.run_js(r"""
auto();el('wbLayerSense').value=JSON.stringify([{layer:1,points:[]}]);await control.exportCycle();
assert.equal(exports.length,0);assert.match(el('autoStatus').textContent,/remove the manual layer touch sets/);
el('wbLayerSense').value='';el('wbAutoMax').value='3000';await control.exportCycle();assert.equal(exports.length,0);
el('wbAutoMax').value='';await control.exportCycle();
el('wbAutoBefore').value='';el('wbAutoAfter').value='300';
el('wbAutoVarFile').files=[{name:'VAR.DAT',text:async()=>'touch records'}];await el('wbAutoVarFile').onchange();
assert.equal(calls.filter(r=>r.path.startsWith('/api/cycle-import')).length,0);
""")

    def test_guests_hc_and_planning_only_disable_auto(self):
        self.run_js(r"""
auto();const ids=['wbBuildAuto','wbAutoSpacing','wbAutoMax','wbAutoBefore','wbAutoReadingBtn'];
guest=true;control.paint();for(const id of ids)assert.equal(el(id).disabled,true,id);
await control.exportCycle();assert.equal(exports.length,0);guest=false;
S.meta.params.cell_profile='hc10';control.paint();for(const id of ids)assert.equal(el(id).disabled,true,id);
S.meta.params.cell_profile='lc50s';S.meta.clearance_checked=false;control.paint();
for(const id of ids)assert.equal(el(id).disabled,true,id);
""")

    def test_cell_off_and_bead_sets_are_disabled_but_single_beads_are_allowed(self):
        self.run_js(r"""
auto();S.meta.shape='bead';S.meta.params.bead_count='1';control.paint();
assert.equal(el('wbBuildAuto').disabled,false);assert.equal(el('wbAutoSpacing').disabled,false);
S.meta.params.bead_count='2';control.paint();assert.equal(el('wbBuildAuto').disabled,true);
assert.equal(el('wbAutoSpacing').disabled,true);assert.equal(el('wbBuildManual').disabled,false);
el('wbBuildManual').dispatch('change');assert.equal(control.enabled(),false);
S.meta.shape='wall';S.meta.params.cell='0';control.paint();assert.equal(el('wbBuildAuto').disabled,true);
el('wbBuildAuto').dispatch('change');assert.equal(control.enabled(),false);
S.meta.params.cell='1';el('wbBuildAuto').dispatch('change');assert.equal(control.enabled(),true);
""")

    def test_correcting_cycle_length_restores_export(self):
        self.run_js(r"""
auto();env.changed=()=>{el('emitBtn').disabled=false;control.paint();};
el('wbInterpassEvery').value='';el('wbInterpassEvery').dispatch('input');
assert.equal(el('emitBtn').disabled,true);
el('wbInterpassEvery').value='3';el('wbInterpassEvery').dispatch('input');
assert.equal(el('emitBtn').disabled,false);
assert.equal(el('emitBtn').textContent,'Export cycle 1 · layers 0-2');
""")

    def test_no_cell_server_disables_auto_even_without_a_cell_query_parameter(self):
        self.run_js(r"""
auto();delete S.meta.params.cell;S.meta.cell='workbench';control.paint();
assert.equal(S.meta.clearance_checked,true);
assert.equal(el('wbBuildAuto').disabled,true);assert.equal(el('wbAutoSpacing').disabled,true);
await control.exportCycle();assert.equal(exports.length,0);
assert.equal(el('wbBuildManual').disabled,false);
""")

    def test_max_interpass_prefills_from_the_filler_until_edited(self):
        self.run_js(r"""
assert.equal(el('wbAutoMax').value,'302');assert.equal(el('wbAutoAmbient').value,'77');
el('wbAutoMaxUnit').value='C';el('wbAutoMaxUnit').dispatch('change');
assert.equal(el('wbAutoMax').value,'150');assert.equal(el('wbAutoAmbient').value,'25');
el('wbFiller').value='er316l';el('wbFiller').dispatch('change');assert.equal(el('wbAutoMax').value,'100');
el('wbFiller').value='er5356';el('wbFiller').dispatch('change');assert.equal(el('wbAutoMax').value,'');
el('wbFiller').value='er308l';el('wbFiller').dispatch('change');assert.equal(el('wbAutoMax').value,'100');
el('wbAutoMax').value='120';el('wbFiller').value='er70s6';el('wbFiller').dispatch('change');
assert.equal(el('wbAutoMax').value,'120');
""")

    def test_an_explicit_clear_survives_filler_unit_and_reload(self):
        self.run_js(r"""
const store=new Map(),storage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),
  removeItem:k=>store.delete(k)};
const env2={...env,storage};const first=A.create(env2);
el('wbAutoMax').value='';el('wbAutoMax').dispatch('input');assert.equal(store.get('waam.autoMaxCleared'),'1');
el('wbFiller').value='er316l';el('wbFiller').dispatch('change');assert.equal(el('wbAutoMax').value,'');
el('wbAutoMaxUnit').value='C';el('wbAutoMaxUnit').dispatch('change');assert.equal(el('wbAutoMax').value,'');
A.create(env2);assert.equal(el('wbAutoMax').value,'');               // a reload keeps the clear
el('wbAutoMax').value='90';el('wbAutoMax').dispatch('input');assert.equal(store.has('waam.autoMaxCleared'),false);
assert.equal(el('wbAutoMax').value,'90');void first;
""")

    def test_export_notes_reach_the_status(self):
        self.run_js(r"""
env.exportCycle=async options=>{exports.push(options);
  return {cycle:1,firstLayer:0,lastLayer:1,last_cycle:false,job:'WALL_C01',archive:'saved',run:'RUN',
          notes:'between-bead dwell skipped at 2 gaps'};};
auto();await control.exportCycle();
assert.match(el('autoStatus').textContent,/Downloaded WALL_C01\.JBI\. Note: between-bead dwell skipped at 2 gaps\./);
""")

    def test_cooling_proposal_is_applied_only_when_accepted(self):
        self.run_js(r"""
const fit={used:[{},{}],tau_s:200,rms_c:1.25,valid:true};
const proposal={seconds:220,start_c:400,setpoint_c:150,hold_s:68,notes:[]};
const plain=env.request;
env.request=async(path,options)=>path.startsWith('/api/run/cooling?')?
  (calls.push({path,options}),reply(true,{ok:true,basis:'cooling_pairs',fit,proposal})):plain(path,options);
el('wbInterpass').value='30';auto();
active={run:'RUN',cycles:[{cycle:1,t_before:{value_c:300},t_after:{value_c:200},status:'applied'},
  {cycle:2,t_before:{value_c:400},t_after:{value_c:200},status:'applied'}]};
await control.refresh();await new Promise(r=>setTimeout(r,0));
const query=new URLSearchParams(calls.find(r=>r.path.startsWith('/api/run/cooling')).path.split('?')[1]);
assert.equal(query.get('setpoint'),'302');assert.equal(query.get('unit'),'F');assert.equal(query.get('ambient'),'77');
assert.match(el('autoCooling').textContent,/Cooling fit \(cycle pairs, 2 cycles\)/);
assert.match(el('autoCooling').textContent,/tau 200 s, RMS 2.3 °F\. Proposed interpass dwell 220 s.*\(752 °F\) to cool to 302 °F\. The last T-after needs about 68 s more\./);
assert.match(el('autoCooling').textContent,/tau reads slightly short and the proposal slightly optimistic; the max interpass hold still checks/);
assert.equal(el('wbInterpass').value,'30');assert.equal(el('wbAutoUseDwell').hidden,false);
await el('wbAutoUseDwell').onclick();assert.equal(el('wbInterpass').value,'220');
assert.match(el('autoStatus').textContent,/Interpass dwell set to 220 s/);
""")

    def test_sheet_decay_proposal_without_any_cycle_pairs(self):
        self.run_js(r"""
const fit={count:1,used:[{layer:0}],tau_s:180,rms_c:0,valid:true};
const proposal={seconds:210,start_c:400,setpoint_c:150,hold_s:60,notes:[]};
const plain=env.request;
env.request=async(path,options)=>path.startsWith('/api/run/cooling?')?
  (calls.push({path,options}),reply(true,{ok:true,basis:'layer_decay',fit,proposal})):plain(path,options);
auto();active={run:'RUN',cycles:[],layer_log:[{layer:0,readings:{20:360,40:320}}]};
await control.refresh();await new Promise(r=>setTimeout(r,0));
assert.equal(calls.filter(r=>r.path.startsWith('/api/run/cooling?')).length,1);
assert.match(el('autoCooling').textContent,/sheet decay fit, 1 row.*tau 180 s/);
assert.match(el('autoCooling').textContent,/latest reading needs about 60 s more/);
assert.doesNotMatch(el('autoCooling').textContent,/T-before|T-after|tau reads slightly short/);
assert.equal(el('wbAutoUseDwell').hidden,false);
""")

    def test_insufficient_cooling_data_shows_the_backend_refusal(self):
        self.run_js(r"""
const refusal='No usable layer decay rows; Cooling fit needs at least two cycle pairs';
const plain=env.request;
env.request=async(path,options)=>path.startsWith('/api/run/cooling?')?
  (calls.push({path,options}),reply(false,{ok:false,error:refusal},400)):plain(path,options);
auto();active={run:'RUN',cycles:[]};
await control.refresh();await new Promise(r=>setTimeout(r,0));
assert.equal(calls.filter(r=>r.path.startsWith('/api/run/cooling?')).length,1);
assert.equal(el('autoCooling').textContent,'Cooling fit: '+refusal);
assert.equal(el('wbAutoUseDwell').hidden,true);
""")

    def test_no_active_run_does_not_request_cooling(self):
        self.run_js(r"""
auto();await control.refresh();await new Promise(r=>setTimeout(r,0));
assert.equal(calls.filter(r=>r.path.startsWith('/api/run/cooling?')).length,0);
assert.match(el('autoCooling').textContent,/start a run/);
assert.equal(el('wbAutoUseDwell').hidden,true);
""")

    def test_auto_names_keep_the_manual_first_character_rule(self):
        self.run_js(r"""
const matches=value=>new RegExp('^(?:'+el('wbJbiName').pattern+')$').test(value);
auto();assert.equal(el('wbJbiName').maxLength,28);
for(const value of ['WALL','W_-1','A'.repeat(28)])assert.equal(matches(value),true,value);
for(const value of ['', '_WALL','-WALL','A'.repeat(29),'WALL X'])assert.equal(matches(value),false,value);
assert.match(el('jobNameHint').textContent,/1–28 characters/);
el('wbBuildManual').dispatch('change');assert.equal(el('wbJbiName').maxLength,32);
assert.equal(matches('_WALL'),false);assert.equal(matches('-WALL'),false);
assert.equal(matches('A'.repeat(32)),true);assert.equal(matches('A'.repeat(33)),false);
assert.match(el('jobNameHint').textContent,/Start with a letter or digit/);
""")


if __name__ == "__main__":
    unittest.main()
