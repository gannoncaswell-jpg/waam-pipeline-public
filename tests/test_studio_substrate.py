import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.studio import build_state, render_studio
from waam_pipeline.demos import demo_bead
from tests.test_studio_controls import _ShellNodes, _DOM

NODE = shutil.which("node")


class TestSenseSurfaceLayout(unittest.TestCase):
    def test_sense_controls_appear_once_inside_a_closed_substrate_section(self):
        state = build_state(demo_bead(), workbench=True, shape="bead", violations=[])
        parser = _ShellNodes()
        parser.feed(render_studio(state).split("<script data-app>", 1)[0])
        ids = ("wbSenseLayout", "wbSenseFit", "wbSenseSpacing", "wbSenseInset", "wbSensePoints", "wbSenseName",
               "wbSenseJobBtn", "wbSenseVarDatBtn", "wbSenseVarDatFile",
               "wbSenseClearBtn", "senseStatus", "senseReadout")
        nodes = {}
        for element_id in ids:
            with self.subTest(element_id=element_id):
                matches = [node for node in parser.nodes if node["attrs"].get("id") == element_id]
                self.assertEqual(len(matches), 1)
                node = nodes[element_id] = matches[0]
                ancestors = []
                while node["parent"] is not None:
                    node = parser.nodes[node["parent"]]
                    ancestors.append(node)
                self.assertTrue(any(node["attrs"].get("id") == "substratePanel" for node in ancestors))
                section = next(node for node in ancestors if node["tag"] == "details")
                self.assertNotEqual(section["attrs"].get("id"), "substratePanel")
                self.assertIn("accordion", section["attrs"].get("class", "").split())
                self.assertNotIn("open", section["attrs"])
        for element_id, value in (("wbSenseSpacing", "40"), ("wbSenseInset", "10"),
                                  ("wbSensePoints", "21"), ("wbSenseName", "SENSE_PLATE")):
            self.assertEqual(nodes[element_id]["attrs"].get("value"), value)
        self.assertEqual(nodes["wbSensePoints"]["attrs"].get("min"), "2")
        self.assertEqual(nodes["wbSensePoints"]["attrs"].get("max"), "21")
        self.assertEqual(nodes["senseStatus"]["attrs"].get("role"), "status")
        self.assertEqual(nodes["wbSenseVarDatFile"]["attrs"].get("type"), "file")
        self.assertIn("hidden", nodes["wbSenseVarDatFile"]["attrs"])


@unittest.skipUnless(NODE, "Node executes the registration controls")
class TestSubstrateControls(unittest.TestCase):
    def run_js(self, body):
        state = build_state(demo_bead(), workbench=True, shape="bead", violations=[])
        state["meta"].update(params={"cell_profile": "lc50s"}, registration_ctwd_mm=9.525)
        parser = _ShellNodes()
        parser.feed(render_studio(state).split("<script data-app>", 1)[0])
        source = ROOT / "src/waam_pipeline/studio_substrate.js"
        setup = "const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL=" + json.dumps(state) + ";\n" + _DOM
        setup += "\nconst R=require(" + json.dumps(str(source)) + ").WaamSubstrate;\n"
        setup += r"""
let calls=[],applyOptions=[],busy=false,accept=true,guest=false,blocked=null,senseCalls=[],imports=[],senseError='',importError='';
let senseReply={count:3,base:44,archive:'sense-run'},touches=[[1,2,3,4,5,6],[7,8,9,10,11,12],[13,14,15,16,17,18]];
const env={document,state:()=>S,busy:()=>busy,guest:()=>guest,blocked:()=>blocked,
  storage:sessionStorage,storageKey:'draft',pause:()=>playing=false,
  senseParams:()=>({sub:'6.35',fixture:'0'}),
  senseJob:async(raw,options)=>{senseCalls.push({raw,options});if(senseError)throw Error(senseError);return senseReply;},
  importVarDat:async(text,base,count)=>{imports.push({text,base,count});if(importError)throw Error(importError);return touches;},
  apply:async(raw,options)=>{calls.push(raw);applyOptions.push(options);if(accept){S.meta.params.substrate_registration=raw;S.meta.registration_pending=false;
    if(!raw)S.meta.substrate=null;else if(S.meta.substrate){const data=JSON.parse(raw);
      if(data.sensed)S.meta.substrate.sensed={points:data.sensed.points.length,fit:data.sensed.fit==='interpolate'?'interpolated':'plane',max_facet_slope_deg:3.456,rms_residual_mm:.0123,max_residual_mm:.0456,worst:'P046',height_range_mm:[-1.2345,2.3456]};
      else delete S.meta.substrate.sensed;
    }}return accept;}};
let control=R.create(env);
function surface(sensed=false){
  const data=R.record(Array.from({length:4},(_,i)=>({pulses:[i+1,2,3,4,5,6].join(' '),distance:'11.25'})),9.525,
    {gap_direction:'normal',tool0_tip_offset_mm:13});
  S.meta.substrate={size_mm:[200,100],thickness_mm:6.35,tilt_deg:1,max_residual_mm:.12,gap_direction:'normal'};
  if(sensed){data.version=3;data.sensed={base:44,points:touches};
    S.meta.substrate.sensed={points:touches.length,fit:'quadratic',rms_residual_mm:.0123,max_residual_mm:.0456,worst:'P046',height_range_mm:[-1.2345,2.3456]};}
  S.meta.params.substrate_registration=JSON.stringify(data);control.paint();return data;
}
async function importTouches(text='touch records'){
  el('wbSenseVarDatFile').files=[{name:'VAR.DAT',text:async()=>text}];
  await el('wbSenseVarDatFile').onchange();
}
"""
        result = subprocess.run([NODE], input=setup + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});", encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_gap_is_sent_and_blank_or_malformed_inputs_do_not_submit(self):
        self.run_js(r"""
await control.apply();assert.equal(calls.length,0);
for(let i=1;i<=4;i++){el('wbCorner'+i).value='-10, 20, 30, 40, 50, 60';el('wbTipGap'+i).value=String(8+i);}
await control.apply();assert.equal(calls.length,0);assert.match(el('substrateStatus').textContent,/verified/);
el('wbTool0TipOffset').value='13';
await control.apply();assert.equal(calls.length,1);
const data=JSON.parse(calls[0]);assert.equal(data.weld_ctwd_mm,9.525);
assert.equal(data.version,2);assert.equal(data.gap_direction,'normal');assert.equal(data.tool0_tip_offset_mm,13);
assert.deepEqual(data.corners.map(c=>c.tip_distance_mm),[9,10,11,12]);
assert.deepEqual(data.corners[0].pulses,[-10,20,30,40,50,60]);
el('wbTipGap2').value='';await control.apply();assert.equal(calls.length,1);
el('wbTipGap2').value='10';el('wbCorner1').value='1, 2, 3';await control.apply();assert.equal(calls.length,1);
el('wbCorner1').value='1,2,3,4,5,6.5';await control.apply();assert.equal(calls.length,1);
busy=true;await control.apply(true);assert.equal(calls.length,1);
""")

    def test_desktop_block_disables_and_refuses_plate_var_dat_import_and_apply(self):
        self.run_js(r"""
blocked='Desktop review is still open.';control.paint();
assert.equal(el('registerSubstrate').disabled,true);
assert.equal(el('wbVarDatBtn').disabled,true);assert.equal(el('wbVarDatFile').disabled,true);
el('wbVarDatFile').value='selected';
el('wbVarDatFile').files=[{name:'VAR.DAT',text:async()=>'plate records'}];
await el('wbVarDatFile').onchange();
assert.equal(imports.length,0);assert.equal(el('wbVarDatFile').value,'');
assert.equal(await control.apply(),false);assert.equal(calls.length,0);
assert.equal(control.pending(),false);assert.equal(el('substrateStatus').textContent,blocked);
blocked=null;touches.push([19,20,21,22,23,24]);control.paint();
assert.equal(el('registerSubstrate').disabled,false);
assert.equal(el('wbVarDatBtn').disabled,false);assert.equal(el('wbVarDatFile').disabled,false);
el('wbVarDatFile').files=[{name:'VAR.DAT',text:async()=>'plate records'}];
await el('wbVarDatFile').onchange();assert.equal(imports.length,1);
""")

    def test_load_restores_raw_measurements_and_failed_fit_preserves_typed_values(self):
        self.run_js(r"""
const data=R.record(Array.from({length:4},()=>({pulses:'1 2 3 4 5 6',distance:'11.25'})),9.525);
S.meta.params.substrate_registration=JSON.stringify(data);
S.meta.substrate={size_mm:[200,100],thickness_mm:6.35,tilt_deg:1,max_residual_mm:.12};
control.paint();assert.equal(el('wbTipGap3').value,'11.25');assert.equal(el('clearSubstrate').disabled,false);
assert.equal(el('wbSubstrateGapDirection').value,'vertical');assert.equal(el('wbTool0TipOffset').value,'');
el('wbTool0TipOffset').value='13';
el('wbTipGap1').value='12';accept=false;await control.apply();
assert.equal(el('wbTipGap1').value,'12');assert.match(el('substrateStatus').textContent,/not applied/);
assert.equal(S.meta.params.substrate_registration,JSON.stringify(data));
await control.apply(true);assert.equal(calls.at(-1),'');
""")

    def test_draft_and_failed_status_persist_until_successful_register_or_clear(self):
        self.run_js(r"""
el('wbCorner1').value='1 2 3';el('wbCorner1').dispatch('input');
el('wbTool0TipOffset').value='-8.2';el('wbTool0TipOffset').dispatch('input');
el('wbSubstrateGapDirection').value='torch';el('wbSubstrateGapDirection').dispatch('input');
assert.equal(control.pending(),true);assert.equal(el('clearSubstrate').disabled,false);
control.paint();assert.match(el('substrateStatus').textContent,/blocked/);
control=R.create(env);
assert.equal(control.pending(),true);assert.equal(el('wbCorner1').value,'1 2 3');
assert.equal(el('wbTool0TipOffset').value,'-8.2');assert.equal(el('wbSubstrateGapDirection').value,'torch');
await control.apply();control.paint();assert.equal(control.pending(),true);
assert.match(el('substrateStatus').textContent,/six whole/);
accept=false;await control.apply(true);control.paint();assert.equal(control.pending(),true);
assert.match(el('substrateStatus').textContent,/not applied/);
accept=true;await control.apply(true);assert.equal(control.pending(),false);
assert.equal(el('wbCorner1').value,'');assert.equal(el('clearSubstrate').disabled,true);
""")

    def test_pending_server_state_blocks_after_reload_and_new_generation_discards_old_draft(self):
        self.run_js(r"""
el('wbCorner1').value='1 2 3';el('wbCorner1').dispatch('input');
S.meta.gen=2;S.meta.registration_pending=true;
control=R.create(env);
assert.equal(el('wbCorner1').value,'');assert.equal(control.pending(),true);
assert.equal(el('clearSubstrate').disabled,false);
await control.apply(true);assert.equal(control.pending(),false);
""")

    def test_missing_tool_distance_opens_setup_focuses_field_and_preserves_measurements(self):
        self.run_js(r"""
assert.equal(el('substrateToolSetup').open,false);
assert.match(el('substrateToolSetupStatus').textContent,/tool setup required/);
for(let i=1;i<=4;i++){el('wbCorner'+i).value='1 2 3 4 5 6';el('wbTipGap'+i).value='12';}
await control.apply();
assert.equal(calls.length,0);assert.equal(control.pending(),true);
assert.equal(el('substrateToolSetup').open,true);
assert.equal(document.activeElement,el('wbTool0TipOffset'));
assert.equal(el('wbTool0TipOffset').getAttribute('aria-invalid'),'true');
assert.equal(el('substrateToolOffsetError').hidden,false);
assert.match(el('substrateToolOffsetError').textContent,/calibration owner/);
assert.equal(el('wbCorner1').value,'1 2 3 4 5 6');assert.equal(el('wbTipGap4').value,'12');
assert.equal(el('substrateHeight').textContent,'—');
el('wbTool0TipOffset').value='13';el('wbTool0TipOffset').dispatch('input');
assert.equal(el('wbTool0TipOffset').getAttribute('aria-invalid'),null);
assert.equal(el('substrateToolOffsetError').hidden,true);
assert.equal(control.pending(),true);
await control.apply();assert.equal(calls.length,1);
assert.equal(JSON.parse(calls[0]).tool0_tip_offset_mm,13);
""")

    def test_accepted_surface_readouts_use_metadata_and_mark_edited_values_as_previous(self):
        self.run_js(r"""
const data=R.record(Array.from({length:4},()=>({pulses:'1 2 3 4 5 6',distance:'11.25'})),9.525,
  {gap_direction:'normal',tool0_tip_offset_mm:13});
S.meta.params.substrate_registration=JSON.stringify(data);
S.meta.substrate={size_mm:[201.2,120.5],thickness_mm:6.35,tilt_deg:2.03,max_residual_mm:.092,torch_tilt_deg:[2.2,2.02,1.66,1.85],
  surface_setup:{center_height_above_platter_mm:23.51,height_range_above_platter_mm:[21.1,25.92],
    bounds_xy_from_platter_center_mm:{min:[-110.2,-75.4],max:[90.3,45.6]}}};
control.paint();
assert.equal(el('substrateReadout').dataset.state,'ready');
assert.equal(el('substrateReadoutState').textContent,'Surface ready');
assert.equal(el('substrateHeight').textContent,'23.51 mm');
assert.equal(el('substrateHeightRange').textContent,'21.10 to 25.92 mm');
assert.equal(el('substrateCoverageX').textContent,'-110.20 to 90.30 mm');
assert.equal(el('substrateCoverageY').textContent,'-75.40 to 45.60 mm');
assert.equal(el('substrateFootprint').textContent,'201.2 × 120.5 mm');
assert.match(el('substrateFitDetails').textContent,/Tilt 2.03°/);
assert.match(el('substrateFitDetails').textContent,/0.092 mm/);
assert.match(el('substrateFitDetails').textContent,/1.66–2.20°/);
el('wbTipGap1').value='12';el('wbTipGap1').dispatch('input');
assert.equal(el('substrateReadout').dataset.state,'pending');
assert.match(el('substrateReadoutState').textContent,/Previous surface/);
assert.equal(el('substrateHeight').textContent,'23.51 mm');
assert.match(el('substrateFitDetails').textContent,/Previous surface/);
accept=false;await control.apply();control.paint();
assert.equal(el('substrateReadout').dataset.state,'pending');
assert.match(el('substrateReadoutState').textContent,/changes not applied/);
assert.equal(el('wbTipGap1').value,'12');
assert.equal(S.meta.params.substrate_registration,JSON.stringify(data));
accept=true;await control.apply(true);
assert.equal(el('substrateReadout').dataset.state,'empty');
assert.equal(el('substrateHeight').textContent,'—');assert.equal(el('substrateFootprint').textContent,'—');
""")

    def test_wire_tip_touching_uses_the_tool0_distance_along_the_wire_for_every_gap(self):
        self.run_js(r"""
for(let i=1;i<=4;i++)el('wbCorner'+i).value='-10, 20, 30, 40, 50, 60';
el('wbTouchCorners').checked=true;el('wbTouchCorners').dispatch('change');
assert.equal(el('wbSubstrateGapDirection').value,'torch');
assert.equal(el('wbTipGap1').readOnly,true);
assert.match(el('substrateGapHint').textContent,/touched each corner/);
await control.apply();assert.equal(calls.length,0);
assert.equal(el('substrateToolSetup').open,true);
assert.match(el('substrateToolOffsetError').textContent,/Wire tip touching/);
el('wbTool0TipOffset').value='8.92';el('wbTool0TipOffset').dispatch('input');
for(let i=1;i<=4;i++)assert.equal(el('wbTipGap'+i).value,'8.92');
await control.apply();assert.equal(calls.length,1);
const data=JSON.parse(calls[0]);
assert.equal(data.gap_direction,'torch');assert.equal(data.tool0_tip_offset_mm,8.92);
assert.deepEqual(data.corners.map(c=>c.tip_distance_mm),[8.92,8.92,8.92,8.92]);
""")

    def test_touch_mode_is_read_back_from_the_record_and_cleared_by_another_direction(self):
        self.run_js(r"""
const touched=R.record(Array.from({length:4},()=>({pulses:'1 2 3 4 5 6',distance:'8.92'})),9.525,
  {gap_direction:'torch',tool0_tip_offset_mm:'8.92'});
S.meta.params.substrate_registration=JSON.stringify(touched);control.paint();
assert.equal(el('wbTouchCorners').checked,true);assert.equal(el('wbTipGap2').readOnly,true);
const ruler=R.record(Array.from({length:4},(_,i)=>({pulses:'1 2 3 4 5 6',distance:String(8.92+i)})),9.525,
  {gap_direction:'torch',tool0_tip_offset_mm:'8.92'});
S.meta.params.substrate_registration=JSON.stringify(ruler);control.paint();
assert.equal(el('wbTouchCorners').checked,false);assert.equal(el('wbTipGap2').readOnly,false);
S.meta.params.substrate_registration=JSON.stringify(touched);control.paint();
el('wbSubstrateGapDirection').value='normal';el('wbSubstrateGapDirection').dispatch('input');
assert.equal(el('wbTouchCorners').checked,false);assert.equal(el('wbTipGap1').readOnly,false);
""")

    def test_touch_mode_survives_a_reload_as_a_draft(self):
        self.run_js(r"""
el('wbTouchCorners').checked=true;el('wbTouchCorners').dispatch('change');
el('wbTool0TipOffset').value='8.92';el('wbTool0TipOffset').dispatch('input');
control=R.create(env);
assert.equal(el('wbTouchCorners').checked,true);assert.equal(el('wbTipGap3').value,'8.92');
assert.equal(control.pending(),true);
""")

    def test_ruler_direction_instruction_follows_draft_and_legacy_restore(self):
        self.run_js(r"""
assert.equal(el('wbSubstrateGapDirection').value,'normal');
assert.match(el('substrateGapHint').textContent,/perpendicular/);
el('wbSubstrateGapDirection').value='torch';el('wbSubstrateGapDirection').dispatch('input');
assert.match(el('substrateGapHint').textContent,/wire axis/);
const data=R.record(Array.from({length:4},()=>({pulses:'1 2 3 4 5 6',distance:'11.25'})),9.525);
S.meta.params.substrate_registration=JSON.stringify(data);control.paint();
assert.equal(el('wbSubstrateGapDirection').value,'vertical');
assert.match(el('substrateGapHint').textContent,/vertically/);
assert.equal(el('wbTool0TipOffset').value,'');
assert.equal(el('substrateHeight').textContent,'—');
""")

    def test_sense_download_uses_the_accepted_v2_record_and_remembers_the_job(self):
        self.run_js(r"""
const previous=surface(true);
for(const [id,value] of Object.entries({wbSenseSpacing:'23.5',wbSenseInset:'8.5',wbSensePoints:'19',wbSenseName:'MY_PLATE'})){
  el(id).value=value;el(id).dispatch('input');
}
await el('wbSenseJobBtn').onclick();
assert.equal(senseCalls.length,1);assert.equal(calls.length,0);assert.equal(control.pending(),false);
const {raw,options}=senseCalls[0],sent=JSON.parse(raw),expected={...previous,version:2};delete expected.sensed;
assert.deepEqual(sent,expected);assert.equal(Object.hasOwn(sent,'sensed'),false);
assert.equal(Number(options.spacing),23.5);assert.equal(Number(options.inset),8.5);
assert.equal(Number(options.points),19);assert.equal(options.name,'MY_PLATE');
assert.equal(Number(options.sub),6.35);assert.equal(Number(options.fixture),0);
assert.equal(options.layout,'grid');
const saved=JSON.parse(sessionStorage.getItem('draft'));
assert.equal(saved.senseJob.count,3);assert.equal(saved.senseJob.base,44);
assert.equal(saved.senseJob.layout,'grid');
assert.equal(saved.senseValues.wbSenseName,'MY_PLATE');
assert.equal(S.meta.params.substrate_registration,JSON.stringify(previous));
""")

    def test_sense_download_refusal_is_local_to_sense_status(self):
        self.run_js(r"""
const previous=surface(),status=el('substrateStatus').textContent;
senseError='The probe grid crosses the clear area.';
await el('wbSenseJobBtn').onclick();control.paint();
assert.match(el('senseStatus').textContent,/The probe grid crosses the clear area\./);
assert.equal(el('substrateStatus').textContent,status);assert.equal(control.pending(),false);
assert.equal(S.meta.params.substrate_registration,JSON.stringify(previous));
assert.equal(el('wbSenseJobBtn').disabled,false);assert.equal(calls.length,0);
""")

    def test_sense_import_uses_downloaded_count_and_failed_apply_preserves_registration(self):
        self.run_js(r"""
const previous=surface();await el('wbSenseJobBtn').onclick();
el('wbSensePoints').value='21';await importTouches('export with corners and touches');
assert.deepEqual(imports,[{text:'export with corners and touches',base:44,count:3}]);
assert.equal(calls.length,1);
const applied=JSON.parse(calls[0]);
assert.deepEqual(applied,{...previous,version:3,sensed:{base:44,points:touches,fit:'smooth'}});
assert.deepEqual(applyOptions[0],{preservePlacement:false});
assert.deepEqual(applied.corners,previous.corners);assert.equal(control.pending(),false);
const accepted=S.meta.params.substrate_registration,status=el('substrateStatus').textContent;
accept=false;touches=touches.map(row=>row.map(value=>value+100));
await importTouches();control.paint();
assert.equal(S.meta.params.substrate_registration,accepted);assert.equal(control.pending(),false);
assert.match(el('senseStatus').textContent,/not applied/i);
assert.equal(el('substrateStatus').textContent,status);
assert.equal(el('wbSenseVarDatFile').value,'');assert.equal(el('wbSenseVarDatBtn').disabled,false);
""")

    def test_sense_import_without_a_download_reads_the_selected_count_and_reports_errors(self):
        self.run_js(r"""
const previous=surface();el('wbSensePoints').value='2';touches=touches.slice(0,2);
await importTouches();assert.deepEqual(imports,[{text:'touch records',base:44,count:2}]);
assert.deepEqual(JSON.parse(calls[0]),{...previous,version:3,sensed:{base:44,points:touches,fit:'smooth'}});
const accepted=S.meta.params.substrate_registration;
importError='P045 is not a whole-number pulse record.';
await importTouches();control.paint();
assert.match(el('senseStatus').textContent,/P045 is not a whole-number pulse record\./);
assert.equal(S.meta.params.substrate_registration,accepted);assert.equal(calls.length,1);
assert.equal(control.pending(),false);
""")

    def test_sense_clear_applies_the_original_v2_corners(self):
        self.run_js(r"""
const previous=surface(true),expected={...previous,version:2};delete expected.sensed;
await el('wbSenseClearBtn').onclick();
assert.equal(calls.length,1);assert.deepEqual(JSON.parse(calls[0]),expected);
assert.equal(Object.hasOwn(JSON.parse(calls[0]),'sensed'),false);
assert.equal(control.pending(),false);assert.match(el('senseReadout').textContent,/Not sensed/i);
""")

    def test_sensed_readout_and_seating_survive_selecting_another_part(self):
        self.run_js(r"""
const previous=surface(true);S.meta.shape='wall';control.paint();
const readout=el('senseReadout').textContent;
assert.match(readout,/3 points/i);assert.match(readout,/quadratic/i);
assert.match(readout,/0\.012 mm/);assert.match(readout,/0\.046 mm/);assert.match(readout,/P046/);
assert.ok(readout.includes((-1.2345).toFixed(3)));assert.ok(readout.includes((2.3456).toFixed(3)));
assert.match(readout+' '+el('senseStatus').textContent,/P044.*P046/);
assert.match(el('substrateIntro').textContent,/part.*seated.*sensed surface/i);
assert.match(el('substrateStatus').textContent,/part.*seated.*sensed surface/i);
assert.equal(el('registerSubstrate').disabled,false);assert.equal(el('wbSenseJobBtn').disabled,false);
assert.equal(el('wbSenseVarDatBtn').disabled,false);assert.equal(el('wbSenseClearBtn').disabled,false);
assert.equal(S.meta.params.substrate_registration,JSON.stringify(previous));assert.equal(control.pending(),false);
S.meta.params.cell_profile='hc10';control.paint();assert.equal(el('registerSubstrate').disabled,true);
assert.match(el('substrateIntro').textContent,/sensed surface/i);
""")

    def test_sense_controls_require_an_accepted_current_surface_and_an_account(self):
        self.run_js(r"""
const ids=['wbSenseLayout','wbSenseFit','wbSenseSpacing','wbSenseInset','wbSensePoints','wbSenseName','wbSenseJobBtn','wbSenseVarDatBtn','wbSenseVarDatFile','wbSenseClearBtn'];
function disabled(){for(const id of ids)assert.equal(el(id).disabled,true,id);}
disabled();assert.match(el('senseReadout').textContent,/Not sensed/i);
surface();assert.equal(el('wbSenseJobBtn').disabled,false);assert.equal(el('wbSenseClearBtn').disabled,true);
assert.equal(el('wbSenseLayout').disabled,false);assert.equal(el('wbSenseFit').disabled,false);
delete S.meta.substrate.gap_direction;control.paint();disabled();
surface(true);busy=true;control.paint();disabled();busy=false;
guest=true;control.paint();disabled();guest=false;
S.meta.registration_pending=true;control.paint();disabled();S.meta.registration_pending=false;control.paint();
el('wbTipGap1').value='12';el('wbTipGap1').dispatch('input');disabled();
assert.equal(control.pending(),true);
""")

    def test_sense_job_and_input_draft_restore_without_making_corners_pending(self):
        self.run_js(r"""
surface();el('wbSensePoints').value='11';el('wbSensePoints').dispatch('input');
el('wbSenseName').value='RESTORED_PLATE';el('wbSenseName').dispatch('input');
el('wbSenseLayout').value='path';el('wbSenseLayout').dispatch('input');
el('wbSenseFit').value='interpolate';el('wbSenseFit').dispatch('input');
await el('wbSenseJobBtn').onclick();
assert.equal(senseCalls[0].options.layout,'path');
el('wbSenseLayout').value='grid';el('wbSenseFit').value='smooth';
el('wbSensePoints').value='21';el('wbSenseName').value='SENSE_PLATE';control=R.create(env);
assert.equal(el('wbSensePoints').value,'11');assert.equal(el('wbSenseName').value,'RESTORED_PLATE');
assert.equal(el('wbSenseLayout').value,'path');assert.equal(el('wbSenseFit').value,'interpolate');
assert.equal(control.pending(),false);await importTouches();
assert.equal(imports[0].base,44);assert.equal(imports[0].count,3);
assert.equal(JSON.parse(calls[0]).sensed.fit,'interpolate');
assert.deepEqual(applyOptions[0],{preservePlacement:true});
""")

    def test_interpolated_readout_names_facet_slope_and_smooth_deviation(self):
        self.run_js(r"""
surface();el('wbSensePoints').value='3';el('wbSenseFit').value='interpolate';
await importTouches();
assert.equal(JSON.parse(calls[0]).sensed.fit,'interpolate');
assert.equal(el('wbSenseFit').value,'interpolate');
const readout=el('senseReadout').textContent;
assert.match(readout,/interpolated fit/);assert.match(readout,/Maximum facet slope 3\.46/);
assert.match(readout,/RMS deviation from smooth fit 0\.012 mm/);
assert.match(readout,/Largest deviation from smooth fit 0\.046 mm/);assert.match(readout,/P046/);
assert.doesNotMatch(readout,/residual/);
control=R.create({...env,storageKey:'fresh'});
assert.equal(el('wbSenseFit').value,'interpolate');
""")

    def test_path_import_preserves_the_downloaded_placement_when_layout_changes(self):
        self.run_js(r"""
surface();el('wbSenseLayout').value='path';await el('wbSenseJobBtn').onclick();
assert.equal(senseCalls[0].options.layout,'path');
assert.equal(JSON.parse(sessionStorage.getItem('draft')).senseJob.layout,'path');
el('wbSenseLayout').value='grid';await importTouches();
assert.deepEqual(applyOptions[0],{preservePlacement:true});
""")

    def test_path_import_without_download_preserves_placement(self):
        self.run_js(r"""
surface();el('wbSenseLayout').value='path';el('wbSensePoints').value='3';
await importTouches();assert.deepEqual(applyOptions[0],{preservePlacement:true});
""")

    def test_legacy_saved_sense_job_remains_a_grid_import(self):
        self.run_js(r"""
surface();await el('wbSenseJobBtn').onclick();
const saved=JSON.parse(sessionStorage.getItem('draft'));delete saved.senseJob.layout;
saved.senseValues.wbSenseLayout='path';sessionStorage.setItem('draft',JSON.stringify(saved));
control=R.create(env);assert.equal(el('wbSenseLayout').value,'path');
await importTouches();assert.deepEqual(applyOptions[0],{preservePlacement:false});
""")


if __name__ == "__main__":
    unittest.main()
