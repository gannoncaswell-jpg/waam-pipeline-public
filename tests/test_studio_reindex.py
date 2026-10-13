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
IDS = (
    "wbReindexProfileName", "wbReindexProfileList", "wbReindexLoadBtn", "wbReindexSaveBtn",
    "wbReindexDownloadBtn", "wbReindexUploadBtn", "wbReindexUploadFile", "wbReindexClear",
    "wbReindexSetupBtn", "wbReindexSetupFile", "reindexSetupReadout", "wbReindexTouchName",
    "wbReindexTouchBtn", "wbReindexRefBtn", "wbReindexRefFile", "wbReindexSourceBtn",
    "wbReindexSourceFile", "wbReindexNewBtn", "wbReindexNewFile", "reindexReport",
    "wbReindexJobName", "wbReindexEmitBtn", "reindexStatus", "wbReindexRefreshBtn",
    "reindexSourceReadout",
)


def shell():
    state = build_state(wall(100, 4, 2.0), workbench=True, violations=[])
    state["meta"].update(params={"cell_profile": "lc50s"})
    parser = _ShellNodes()
    parser.feed(render_studio(state).split("<script data-app>", 1)[0])
    return state, parser


class TestReindexLayout(unittest.TestCase):
    def test_controls_appear_once_inside_the_panel_and_export_step(self):
        _, parser = shell()
        for element_id in IDS:
            with self.subTest(element_id=element_id):
                matches = [n for n in parser.nodes if n["attrs"].get("id") == element_id]
                self.assertEqual(len(matches), 1)
                node, ancestors = matches[0], []
                while node["parent"] is not None:
                    node = parser.nodes[node["parent"]]
                    ancestors.append(node["attrs"].get("id"))
                self.assertIn("reindexPanel", ancestors)
                self.assertIn("step-export", ancestors)
        nodes = {n["attrs"].get("id"): n for n in parser.nodes}
        self.assertEqual(sum(n["attrs"].get("id") == "reindexPanel" for n in parser.nodes), 1)
        self.assertEqual(nodes["reindexPanel"]["tag"], "details")
        self.assertEqual(nodes["wbReindexProfileName"]["attrs"]["value"], "MANPRO")
        self.assertEqual(nodes["wbReindexTouchName"]["attrs"]["value"], "REIDX_TOUCH")
        clear = nodes["wbReindexClear"]["attrs"]
        self.assertEqual((clear["type"], clear["value"], clear["min"], clear["max"]),
                         ("number", "50", "20", "200"))
        for button, registers in (("wbReindexSetupBtn", ("P040", "P047")),
                                  ("wbReindexRefBtn", ("P044", "P051")),
                                  ("wbReindexNewBtn", ("P044", "P051"))):
            for register in registers:
                self.assertIn(register, nodes[button]["text"])
        for element_id, accept in (("wbReindexSetupFile", ".DAT,.dat,.txt"),
                                   ("wbReindexRefFile", ".DAT,.dat,.txt"),
                                   ("wbReindexNewFile", ".DAT,.dat,.txt"),
                                   ("wbReindexSourceFile", ".JBI,.jbi"),
                                   ("wbReindexUploadFile", ".json")):
            attrs = nodes[element_id]["attrs"]
            self.assertEqual(attrs["type"], "file")
            self.assertEqual(attrs["accept"], accept)
            self.assertIn("hidden", attrs)
            self.assertTrue(attrs.get("aria-label"))
        self.assertEqual(nodes["reindexStatus"]["attrs"]["role"], "status")
        self.assertEqual(nodes["reindexStatus"]["attrs"]["aria-live"], "polite")

    def test_the_page_carries_the_module_and_copy_review_instructions(self):
        state, _ = shell()
        page = render_studio(state)
        self.assertIn("WaamReindex", page.split("<script data-app>", 1)[1])
        self.assertIn("The class job is never edited: this writes a copy that follows the block.", page)
        self.assertIn("Two-person review before the class runs it.", page)
        self.assertIn("Teach T1–T4 on top", page)
        self.assertIn("Touch the side/end faces 2–4 mm below the top edge", page)
        self.assertIn("The eight contacts land in P044–P051.", page)


@unittest.skipUnless(NODE, "Node executes the re-index controls")
class TestReindexControls(unittest.TestCase):
    def run_js(self, body):
        state, parser = shell()
        source = ROOT / "src/waam_pipeline/studio_reindex.js"
        setup = "const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL=" + json.dumps(state) + ";\n" + _DOM
        setup += "\nconst R=require(" + json.dumps(str(source)) + ").WaamReindex;\n"
        setup += r"""
let busy=false,guest=false,blocked=null,posts=[],gets=[],downloads=[],imports=[],failPath='',changed=0;
const clone=value=>JSON.parse(JSON.stringify(value));
const profileOptions=[],reportChildren=[],localDownloads=[],revoked=[],blobs=new Map();
const profileList=el('wbReindexProfileList');
Object.defineProperty(profileList,'textContent',{
  get:()=>profileOptions.map(option=>option.textContent).join(''),
  set:()=>{profileOptions.length=0;},configurable:true});
profileList.appendChild=option=>{profileOptions.push(option);return option;};
let reportText='';
const reportElement=el('reindexReport');
Object.defineProperty(reportElement,'textContent',{
  get:()=>reportText+reportChildren.map(child=>child.textContent).join(''),
  set:value=>{reportText=String(value);reportChildren.length=0;},configurable:true});
reportElement.appendChild=child=>{reportChildren.push(child);return child;};
const createElement=document.createElement?.bind(document);
document.createElement=tag=>{
  if(tag==='option')return {value:'',textContent:''};
  if(tag==='a')return {click(){localDownloads.push({name:this.download,blob:blobs.get(this.href)});},remove(){}};
  if(['table','caption','thead','tbody','tr','th','td'].includes(tag)){
    let text='';
    return {tagName:tag.toUpperCase(),children:[],
      get textContent(){return text+this.children.map(child=>child.textContent).join('');},
      set textContent(value){text=String(value);this.children.length=0;},
      appendChild(child){this.children.push(child);return child;}};
  }
  return createElement(tag);
};
document.body=document.body||{};document.body.appendChild=element=>element;
URL.createObjectURL=blob=>{const url='blob:profile-'+blobs.size;blobs.set(url,blob);return url;};
URL.revokeObjectURL=url=>{revoked.push(url);};
const records=Array.from({length:8},(_,i)=>Array.from({length:6},(_,j)=>i*10+j));
const reference=records.map(row=>row.map(value=>value+100));
const profiles=new Map();
const sourceSummary={name:'MANPRO_REV_2',groups:['RB1','ST1'],points:8,
  station_records:2,calls:[{kind:'CALL',job:'CLEAN_R1'},{kind:'JUMP',job:'CLASS_NEXT'}]};
const setupSummary={clear_z:150,depths_mm:[3,3,3,3],face_angle_deg:90,top_tilt_deg:0,probes:[]};
const preview={source:'MANPRO_REV_2',name:'MANPRO_REV_2_RX',calls:[{kind:'CALL',job:'CLEAN_R1'},{kind:'JUMP',job:'CLASS_NEXT'}],
  shift:{center_shift_mm:[3,4,0],rotation_deg:[.1,.2,.2],
    residuals_mm:[.01,-.03,.02,0,0,.01,0,-.12],displacements_mm:[1,1,1,1,2,3,1,1],
    labels:['T1','T2','T3','T4','S1','S2','E1','E2']},
  points:[{record:'C00000',station_deg:0,moved_mm:2,max_joint_change_deg:.1},
    {record:'C00001',station_deg:90,moved_mm:5.25,max_joint_change_deg:.6}]};
const env={document,busy:()=>busy,guest:()=>guest,blocked:()=>blocked,
  changed:()=>{changed++;},jobFolder:()=>'/CLASS',
  importVarDat:async(text,base,count)=>{imports.push({text,base,count});return clone(records);},
  post:async(path,obj)=>{
    posts.push({path,obj:clone(obj)});
    if(path.split('?')[0]===failPath)throw Error('Touch E2 is outside its search window.');
    if(path==='/api/reindex/setup')return {ok:true,summary:clone(setupSummary)};
    if(path==='/api/reindex/source')return {ok:true,summary:clone(sourceSummary)};
    if(path==='/api/reindex/touches')return {ok:true,records:clone(reference)};
    if(path==='/api/reindex/preview')return {ok:true,report:clone(preview)};
    if(path.startsWith('/api/reindex/profile?')){
      const name=new URLSearchParams(path.split('?')[1]).get('name');
      profiles.set(name,clone(obj.profile));return {ok:true,name,saved_at:'2026-09-28'};
    }
    throw Error('Unexpected POST '+path);
  },
  get:async path=>{
    gets.push(path);
    if(path.split('?')[0]===failPath)throw Error('Touch E2 is outside its search window.');
    if(path==='/api/reindex/profiles')return {ok:true,profiles:[...profiles].map(([name,p])=>
      ({name,saved_at:'2026-09-28',has_reference:!!p.reference,source:p.source_job?.name||null}))};
    if(path.startsWith('/api/reindex/profile?')){
      const name=new URLSearchParams(path.split('?')[1]).get('name');
      return {ok:true,profile:clone(profiles.get(name))};
    }
    throw Error('Unexpected GET '+path);
  },
  download:async(path,obj,fallbackName)=>{
    downloads.push({path,obj:clone(obj),fallbackName});
    return {probes:8,count:8,base:44,archive:'saved',shift:5,rotation:.3};
  }};
const control=R.create(env);
async function importFile(id,text,name='VAR.DAT'){
  const file=el(id);file.value='selected';file.files=[{name,text:async()=>text}];
  await file.onchange();assert.equal(file.value,'',id+' resets after import');
}
async function setupProfile(){await importFile('wbReindexSetupFile','setup registers');}
async function readyProfile(){
  await setupProfile();await importFile('wbReindexRefFile','reference registers');
  await importFile('wbReindexSourceFile','class job text','MANPRO_REV_2.JBI');
}
async function savedProfile(){
  await el('wbReindexSaveBtn').onclick();
  return posts.filter(call=>call.path.startsWith('/api/reindex/profile?')).at(-1).obj.profile;
}
"""
        result = subprocess.run([NODE], input=setup + "\n(async()=>{\n" + body +
                                "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_setup_import_and_touch_job(self):
        self.run_js(r"""
for(const id of ['wbReindexTouchBtn','wbReindexRefBtn','wbReindexNewBtn','wbReindexEmitBtn'])
  assert.equal(el(id).disabled,true,id);
await setupProfile();
assert.deepEqual(imports,[{text:'setup registers',base:40,count:8}]);
const call=posts.find(call=>call.path==='/api/reindex/setup');
assert.deepEqual(call.obj.profile.setup.records,records);
assert.equal(call.obj.profile.version,1);assert.equal(call.obj.profile.kind,'manpro-reindex');
assert.equal(call.obj.profile.clear_mm,50);
assert.match(el('reindexSetupReadout').textContent,/90/);
assert.match(el('reindexSetupReadout').textContent,/150/);
assert.match(el('reindexSetupReadout').textContent,/depth/i);
el('wbReindexTouchName').value='REIDX_TOUCH';await el('wbReindexTouchBtn').onclick();
assert.equal(downloads.length,1);
const job=downloads[0],params=new URLSearchParams(job.path.split('?')[1]);
assert.equal(job.path.split('?')[0],'/api/reindex/touch-job');
assert.equal(params.get('name'),'REIDX_TOUCH');assert.equal(params.get('folder'),'/CLASS');
assert.deepEqual(Object.keys(job.obj),['profile']);assert.deepEqual(job.obj.profile.setup.records,records);
const status=el('reindexStatus').textContent;
for(const pattern of [/S1/,/0\.0/,/CLEAN_R1/,/two-person/i,/P044/,/P051/])assert.match(status,pattern);
assert.equal(el('wbReindexRefBtn').disabled,false);
assert.equal(el('wbReindexNewBtn').disabled,true);
""")

    def test_reference_source_and_clear_height_invalidation(self):
        self.run_js(r"""
await readyProfile();
const ref=posts.find(call=>call.path==='/api/reindex/touches');
assert.equal(ref.obj.var_dat,'reference registers');assert.deepEqual(ref.obj.profile.setup.records,records);
const source=posts.find(call=>call.path==='/api/reindex/source');
assert.deepEqual(source.obj,{text:'class job text'});
assert.equal(el('wbReindexJobName').value,'MANPRO_REV_2_RX');
const profile=await savedProfile();
assert.deepEqual(profile.reference,{records:reference});
assert.deepEqual(profile.source_job,{name:'MANPRO_REV_2',text:'class job text'});
const readout=el('reindexSourceReadout').textContent;
for(const pattern of [/MANPRO_REV_2/,/8/,/2/,/CALL CLEAN_R1/,/JUMP CLASS_NEXT/])assert.match(readout,pattern);
assert.doesNotMatch(readout,/\[object Object\]/);
await importFile('wbReindexNewFile','new registers');
el('wbReindexClear').value='70';await el('wbReindexClear').onchange();
assert.match(el('reindexStatus').textContent,/reference/i);
const changedProfile=await savedProfile();
assert.equal(changedProfile.clear_mm,70);assert.equal(changedProfile.reference,null);
assert.deepEqual(changedProfile.source_job,profile.source_job);
assert.equal(el('wbReindexNewBtn').disabled,true);assert.equal(el('wbReindexEmitBtn').disabled,true);
""")

    def test_preview_and_emit_use_the_accepted_profile_and_new_touches(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','after swap');
const call=posts.find(call=>call.path==='/api/reindex/preview');
assert.equal(call.obj.var_dat,'after swap');assert.deepEqual(call.obj.profile.reference.records,reference);
const report=el('reindexReport').textContent;
for(const pattern of [/5\.00/,/0\.300/,/0\.12/,/E2/,/5\.25/,/0\.6/])assert.match(report,pattern);
assert.equal(el('wbReindexEmitBtn').disabled,false);
el('wbReindexJobName').value='MANPRO_CLASS_RX';await el('wbReindexEmitBtn').onclick();
const job=downloads.at(-1),params=new URLSearchParams(job.path.split('?')[1]);
assert.equal(job.path.split('?')[0],'/api/reindex/emit');
assert.equal(params.get('name'),'MANPRO_CLASS_RX');assert.equal(params.get('folder'),'/CLASS');
assert.deepEqual(job.obj,{profile:call.obj.profile,var_dat:'after swap'});
assert.match(el('reindexStatus').textContent,
  /Load it BESIDE the class job; two-person review and an arc-off dry run before the class welds with it\./);
""")

    def test_guest_and_busy_disable_every_action(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','after swap');await savedProfile();
await control.refresh();el('wbReindexProfileList').value='MANPRO';control.paint();
const ids=['wbReindexProfileName','wbReindexProfileList','wbReindexLoadBtn','wbReindexSaveBtn',
  'wbReindexDownloadBtn','wbReindexUploadBtn','wbReindexUploadFile','wbReindexClear',
  'wbReindexSetupBtn','wbReindexSetupFile','wbReindexTouchName','wbReindexTouchBtn',
  'wbReindexRefBtn','wbReindexRefFile','wbReindexSourceBtn','wbReindexSourceFile',
  'wbReindexNewBtn','wbReindexNewFile','wbReindexJobName','wbReindexEmitBtn','wbReindexRefreshBtn'];
for(const id of ids)assert.equal(el(id).disabled,false,id+' enabled with prerequisites');
for(const state of ['guest','busy']){
  if(state==='guest')guest=true;else busy=true;
  control.paint();for(const id of ids)assert.equal(el(id).disabled,true,state+' '+id);
  const count=posts.length+gets.length+downloads.length;
  await el('wbReindexSaveBtn').onclick();await el('wbReindexTouchBtn').onclick();
  await el('wbReindexEmitBtn').onclick();await control.refresh();
  assert.equal(posts.length+gets.length+downloads.length,count,state+' blocks direct handlers');
  guest=false;busy=false;
}
control.paint();assert.equal(el('wbReindexEmitBtn').disabled,false);
""")

    def test_desktop_block_refuses_write_handlers_but_keeps_refresh_read_only(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','after swap');
blocked='Desktop review is still open.';control.paint();
for(const id of ['wbReindexSaveBtn','wbReindexDownloadBtn','wbReindexUploadBtn',
  'wbReindexSetupBtn','wbReindexSetupFile','wbReindexTouchBtn','wbReindexRefBtn',
  'wbReindexSourceBtn','wbReindexNewBtn','wbReindexEmitBtn'])assert.equal(el(id).disabled,true,id);
assert.equal(el('wbReindexRefreshBtn').disabled,false);
const before={posts:posts.length,downloads:downloads.length,imports:imports.length,local:localDownloads.length};
el('wbReindexSetupFile').value='selected';
el('wbReindexSetupFile').files=[{name:'VAR.DAT',text:async()=>'blocked setup'}];
await el('wbReindexSetupFile').onchange();
await el('wbReindexSaveBtn').onclick();await el('wbReindexDownloadBtn').onclick();
await el('wbReindexTouchBtn').onclick();await el('wbReindexEmitBtn').onclick();
assert.deepEqual({posts:posts.length,downloads:downloads.length,imports:imports.length,local:localDownloads.length},before);
assert.equal(el('wbReindexSetupFile').value,'');assert.equal(el('reindexStatus').textContent,blocked);
const getCount=gets.length;await control.refresh();assert.equal(gets.length,getCount+1);
blocked=null;control.paint();assert.equal(el('wbReindexSetupBtn').disabled,false);
assert.equal(el('wbReindexEmitBtn').disabled,false);
""")

    def test_save_list_and_load_round_trip(self):
        self.run_js(r"""
await readyProfile();el('wbReindexProfileName').value='Class block';
const original=await savedProfile();
const save=posts.filter(call=>call.path.startsWith('/api/reindex/profile?')).at(-1);
assert.deepEqual(save.obj,{profile:original});
assert.equal(profiles.size,1);
const savedName=[...profiles.keys()][0];assert.equal(savedName,'Class block');
await control.refresh();assert.equal(gets.at(-1),'/api/reindex/profiles');
assert.deepEqual(profileOptions.map(option=>option.value),['Class block']);
assert.match(el('wbReindexProfileList').textContent,/Class block/);
el('wbReindexClear').value='80';await el('wbReindexClear').onchange();
el('wbReindexProfileList').value=savedName;control.paint();
await el('wbReindexLoadBtn').onclick();
const load=gets.find(path=>path.startsWith('/api/reindex/profile?'));
assert.equal(new URLSearchParams(load.split('?')[1]).get('name'),savedName);
assert.equal(el('wbReindexClear').value,'50');
assert.deepEqual(await savedProfile(),original);
assert.equal(el('wbReindexJobName').value,'MANPRO_REV_2_RX');
assert.equal(el('wbReindexNewBtn').disabled,false);assert.equal(el('wbReindexEmitBtn').disabled,true);
assert.ok(posts.filter(call=>call.path==='/api/reindex/setup').length>=3);
""")

    def test_failed_preview_clears_previous_export_and_reports_server_error(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','accepted swap');
assert.equal(el('wbReindexEmitBtn').disabled,false);
failPath='/api/reindex/preview';await importFile('wbReindexNewFile','bad swap');
assert.match(el('reindexStatus').textContent,/Touch E2 is outside its search window\./);
assert.equal(el('wbReindexEmitBtn').disabled,true);
const count=downloads.length;await el('wbReindexEmitBtn').onclick();assert.equal(downloads.length,count);
assert.ok(changed>0);
""")

    def test_setup_validation_is_transactional_and_replacement_clears_reference_and_source(self):
        self.run_js(r"""
await readyProfile();const original=await savedProfile();
records[0][0]=999;failPath='/api/reindex/setup';
await importFile('wbReindexSetupFile','invalid setup');
assert.match(el('reindexStatus').textContent,/Touch E2 is outside its search window\./);
failPath='';assert.deepEqual(await savedProfile(),original);
await importFile('wbReindexSetupFile','replacement setup');
assert.match(el('reindexStatus').textContent,/reference/i);
assert.match(el('reindexStatus').textContent,/Upload the class job exported with the block in THIS position\./);
assert.equal(el('reindexSourceReadout').textContent,'No class job uploaded.');
assert.equal(el('wbReindexJobName').value,'');
const replacement=await savedProfile();
assert.equal(replacement.setup.records[0][0],999);assert.equal(replacement.reference,null);
assert.equal(replacement.source_job,null);
assert.equal(el('wbReindexNewBtn').disabled,true);
""")

    def test_reference_replacement_clears_source_and_class_upload_keeps_reference(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','after swap');
assert.equal(el('wbReindexEmitBtn').disabled,false);
reference[0][0]=999;
await importFile('wbReindexRefFile','replacement reference');
assert.match(el('reindexStatus').textContent,/Upload the class job exported with the block in THIS position\./);
assert.equal(el('reindexSourceReadout').textContent,'No class job uploaded.');
assert.equal(el('wbReindexJobName').value,'');
assert.equal(el('wbReindexNewBtn').disabled,true);
assert.equal(el('wbReindexEmitBtn').disabled,true);
const replacement=await savedProfile();
assert.deepEqual(replacement.reference,{records:reference});assert.equal(replacement.source_job,null);
await importFile('wbReindexSourceFile','replacement class job','MANPRO_REV_2.JBI');
assert.match(el('reindexStatus').textContent,
  /The reference must have been recorded with the block where this job was taught\./);
const paired=await savedProfile();
assert.deepEqual(paired.reference,replacement.reference);
assert.deepEqual(paired.source_job,{name:'MANPRO_REV_2',text:'replacement class job'});
assert.equal(el('wbReindexNewBtn').disabled,false);
assert.equal(el('wbReindexEmitBtn').disabled,true);
""")

    def test_default_name_preserves_suffix_for_32_character_source(self):
        self.run_js(r"""
sourceSummary.name='MANPRO_'+'A'.repeat(25);
await readyProfile();
const expected=sourceSummary.name.slice(0,29)+'_RX';
assert.equal(sourceSummary.name.length,32);assert.notEqual(expected,sourceSummary.name);
assert.equal(el('wbReindexJobName').value,expected);
await savedProfile();
el('wbReindexProfileList').value='MANPRO';await el('wbReindexLoadBtn').onclick();
assert.equal(el('wbReindexJobName').value,expected);
await importFile('wbReindexNewFile','after swap');
el('wbReindexJobName').value='';await el('wbReindexEmitBtn').onclick();
assert.equal(new URLSearchParams(downloads.at(-1).path.split('?')[1]).get('name'),expected);
""")

    def test_local_profile_upload_is_validated_before_acceptance(self):
        self.run_js(r"""
const profile={version:1,kind:'manpro-reindex',setup:{records:clone(records)},clear_mm:65,
  reference:{records:clone(reference)},source_job:{name:'MANPRO_REV_2',text:'uploaded job'}};
await importFile('wbReindexUploadFile',JSON.stringify(profile),'class.reindex.json');
assert.deepEqual(posts.find(call=>call.path==='/api/reindex/setup').obj,{profile});
assert.deepEqual(await savedProfile(),profile);assert.equal(el('wbReindexClear').value,'65');
failPath='/api/reindex/setup';profile.clear_mm=80;
await importFile('wbReindexUploadFile',JSON.stringify(profile),'bad.reindex.json');
assert.match(el('reindexStatus').textContent,/Touch E2 is outside its search window\./);
failPath='';assert.equal((await savedProfile()).clear_mm,65);
""")

    def test_local_profile_download_contains_only_the_profile(self):
        self.run_js(r"""
await readyProfile();await importFile('wbReindexNewFile','after swap');
el('wbReindexProfileName').value='MANPRO';const profile=await savedProfile();
await el('wbReindexDownloadBtn').onclick();
assert.equal(localDownloads.length,1);assert.equal(localDownloads[0].name,'MANPRO.reindex.json');
assert.equal(localDownloads[0].blob.type,'application/json');
assert.deepEqual(JSON.parse(await localDownloads[0].blob.text()),profile);
assert.equal(revoked.length,1);
assert.equal(downloads.length,0);
""")


if __name__ == "__main__":
    unittest.main()
