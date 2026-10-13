import subprocess
import unittest

from tests import test_studio_controls as controls
from waam_pipeline.studio_layout import controls_script
from waam_pipeline.studio import _TEMPLATE
from tests.test_studio import _block_after

_DOM, NODE, ROOT = controls._DOM, controls.NODE, controls.ROOT

_SETUP = r"""
const A='a'.repeat(64),B='b'.repeat(64);
const info={requested_units:'auto',resolved_units:'m',estimated:true,size_mm:[43,24,10]};
const sample={id:'sample:wall',kind:'sample',name:'Starter wall',shape:'wall',units:'mm',
 owner:'alice@example.invalid',source_owner:'library-'+A};
const part={id:'1'.repeat(64),kind:'stl',name:'metres.stl',units:'auto',inspection:info,
 owner:'alice@example.invalid',source_owner:'library-'+A};
let inventory=[sample,part],serverOwner=A;
let handle=async(url,options)=>{
 const u=new URL(url,'http://studio.test'),path=u.pathname,q=u.searchParams;
 if(q.get('owner')!==serverOwner)return {status:409,ok:false,error:'The signed-in account changed. Refresh this page.'};
 if(path==='/api/library'||path==='/api/admin/library')return {ok:true,entries:inventory,owner_id:serverOwner};
 if(path==='/api/library/upload')return {ok:true,entry:{...part,name:q.get('name'),units:q.get('units')},owner_id:serverOwner};
 if(path==='/api/library/units')return {ok:true,entry:{...(inventory.find(entry=>entry.id===q.get('id'))||part),units:q.get('units'),inspection:
   q.get('units')==='mm'?{...info,estimated:false,resolved_units:'mm',size_mm:[.043,.024,.01]}:info},owner_id:serverOwner};
 if(path==='/api/admin/library/import')return {ok:true,entry:part,owner_id:serverOwner};
 if(path==='/api/slice')return {ok:true,state:structuredClone(S)};
 throw new Error('Unexpected request '+path);
};
fetch=async(url,options)=>{requests.push({url,options});const value=await handle(url,options);
 return {ok:value.status?value.status<400:true,status:value.status||200,json:async()=>value};};
const who=(hash=A,extra={})=>({ok:true,library_owner:hash,guest:false,admin:false,...extra});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function file(name='metres.stl'){
 const value={name,size:1024,arrayBuffer:async()=>new Uint8Array([4,3,2,1]).buffer};
 el('stlFile').files=[value];return value;
}
const library=window.waamLibrary;
"""


@unittest.skipUnless(NODE, "Node is required for the served library controller")
class TestStudioLibrary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        controls.TestStudioControls.setUpClass()
        cls.base = controls.TestStudioControls
        cls.module = (ROOT / "src/waam_pipeline/studio_library.js").read_text(encoding="utf-8")

    def run_js(self, body, setup=""):
        base = self.base
        code = ("const SHELL=" + base.shell + ";const INITIAL=" + base.initial + ";\n"
                + _DOM + "\n" + base.ui + base.projection + "\n" + controls_script()
                + "\n" + self.module + "\n" + setup + "\n" + base.handlers + "\ninitializeStudioUI();\n"
                + "(async()=>{\n" + _SETUP + "\n" + body
                + "\n})().catch(e=>{console.error(e);process.exitCode=1;});")
        result = subprocess.run([NODE], input=code, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fresh_upload_resets_stale_units_shows_dimensions_and_slices_saved_source(self):
        self.run_js(r"""
await library.setIdentity(who());el('wbUnits').value='mm';file();
assert.equal(await library.fileChanged(),true);
assert.equal(el('wbUnits').value,'auto');assert.equal(el('stlFile').files.length,0);
assert.match(el('stlInspection').textContent,/Auto estimate: metres/);
assert.match(el('stlInspection').textContent,/43.00 × 24.00 × 10.00 mm/);
assert.equal(el('demoPicker').hidden,true);assert.equal(el('libraryPanel').hidden,false);
const qs=await sliceQuery();assert.equal(qs.get('library'),part.id);assert.equal(qs.get('units'),'auto');
assert.equal(qs.get('owner'),A);assert.equal(requests.at(-1).options.body,null);
""")

    def test_override_persists_dimensions_and_warns_when_part_is_smaller_than_layer(self):
        self.run_js(r"""
await library.setIdentity(who());file();await library.fileChanged();
el('wbUnits').value='mm';el('wbUnits').dispatch('change');await tick();
while(library.pending())await tick();
assert.equal(library.params().library,part.id);
assert.match(el('stlInspection').textContent,/below the/);
assert.equal(el('stlInspection').dataset.state,'warning');
const qs=await sliceQuery();assert.equal(qs.get('units'),'mm');
file('next.stl');await library.fileChanged();assert.equal(el('wbUnits').value,'auto');
""")

    def test_checked_tilt_bounds_do_not_replace_raw_unit_inspection(self):
        self.run_js(r"""
await library.setIdentity(who());file();await library.fileChanged();
const original=handle;
handle=async(url,options)=>{
 const result=await original(url,options);
 if(result.state)result.state.meta.stl={resolved_units:'m',size_mm:[50,60,80]};
 return result;
};
await sliceQuery();assert.deepEqual(S.meta.stl.size_mm,[50,60,80]);
assert.match(el('stlInspection').textContent,/43.00 × 24.00 × 10.00 mm/);
assert.match(el('stlInspection').textContent,/Auto estimate/);
""")

    def test_old_upload_response_cannot_adopt_after_source_was_cleared(self):
        self.run_js(r"""
await library.setIdentity(who());const original=handle;let release;
handle=async(url,options)=>url.startsWith('/api/library/upload')?
 new Promise(resolve=>{release=()=>resolve({ok:true,entry:part,owner_id:A});}):original(url,options);
file();const uploading=library.fileChanged();await tick();assert.ok(release);
el('clearStl').onclick();release();await uploading;
assert.equal(loadedStl,null);assert.equal(library.params().library,undefined);
assert.equal(el('stlFile').files.length,0);assert.equal(UI.busy,false);
""")

    def test_stale_identity_refusal_keeps_source_out_of_other_account(self):
        self.run_js(r"""
await library.setIdentity(who());serverOwner=B;file();
assert.equal(await library.fileChanged(),false);
assert.equal(library.params().library,undefined);assert.equal(loadedStl,null);
assert.equal(el('stlFile').files.length,0);
assert.match(el('sliceStatus').textContent,/account changed/);
assert.equal(el('sliceStatus').getAttribute('role'),'alert');
assert.equal(el('librarySelect').disabled,false);
""")

    def test_account_change_during_file_read_cannot_upload_to_new_account(self):
        self.run_js(r"""
await library.setIdentity(who());let release;
const chosen=file('alice-private.stl');chosen.arrayBuffer=()=>new Promise(resolve=>{release=resolve;});
const uploading=library.fileChanged();await tick();assert.ok(release);
serverOwner=B;inventory=[];const switched=library.setIdentity(who(B));
release(new Uint8Array([1,2]).buffer);await uploading;await switched;
assert.equal(requests.filter(r=>r.url.startsWith('/api/library/upload')).length,0);
assert.equal(loadedStl,null);assert.equal(library.params().library,undefined);
""")

    def test_upload_waits_for_identity_and_retains_local_file_on_save_failure(self):
        self.run_js(r"""
file();const uploading=library.fileChanged();await tick();assert.equal(requests.length,0);
handle=async(url)=>url.startsWith('/api/library/upload')?{status:400,ok:false,error:'Storage full'}:
 {ok:true,entries:inventory,owner_id:A};
await library.setIdentity(who());assert.equal(await uploading,false);
assert.equal(el('stlFile').files.length,1);assert.match(el('sliceStatus').textContent,/Storage full/);
assert.equal(await library.prepareSlice(),false);assert.equal(UI.busy,false);
""")

    def test_loaded_legacy_stl_and_demo_are_not_replaced_by_previous_library_selection(self):
        self.run_js(r"""
sessionStorage.setItem('waamLibrary:'+A,part.id);
await library.setIdentity(who());assert.equal(loadedStl,'previous.stl');
assert.equal(library.params().library,undefined);
let q=await sliceQuery();assert.equal(q.get('reuse'),'1');assert.equal(q.get('library'),null);
S.meta.stl=null;S.meta.shape='wall';S.meta.params={};loadedStl=null;
await library.setIdentity(who());assert.equal(library.params().library,'sample:wall');
q=await sliceQuery();assert.equal(q.get('kind'),'wall');assert.equal(q.get('library'),'sample:wall');
""")

    def test_guest_local_and_token_modes_keep_demos_without_durable_writes(self):
        self.run_js(r"""
for(const guest of [true,false]){
 await library.setIdentity({guest});assert.equal(el('demoPicker').hidden,false);
 assert.equal(el('libraryPanel').hidden,true);assert.deepEqual(library.params(),{});
}
handle=async(url)=>{assert.match(url,/^\/api\/slice\?/);return {ok:true,stl:info};};
file();assert.equal(await library.fileChanged(),true);
assert.equal(el('stlFile').files.length,1);
assert.match(el('stlInspection').textContent,/Auto estimate/);
""")

    def test_loaded_library_build_units_win_over_later_library_preference(self):
        self.run_js(r"""
S.meta.params={library:part.id,units:'m'};inventory=[sample,{...part,units:'mm'}];
await library.setIdentity(who());assert.equal(el('wbUnits').value,'m');
let qs=await sliceQuery();assert.equal(qs.get('units'),'m');assert.equal(qs.get('library'),part.id);

inventory=[sample,{...part,units:'mm'}];await el('libraryRefresh').onclick();
el('librarySelect').value=part.source_owner+'/'+part.id;el('librarySelect').onchange();
await tick();while(library.pending())await tick();assert.equal(el('wbUnits').value,'mm');
qs=await sliceQuery();assert.equal(qs.get('units'),'mm');
""")

    def test_same_filename_library_selection_changes_draft_identity(self):
        self.run_js(r"""
const other={...part,id:'2'.repeat(64),name:part.name};inventory=[sample,part,other];
await library.setIdentity(who());assert.equal(window.waamDraft.error(),'');
el('librarySelect').value=other.source_owner+'/'+other.id;el('librarySelect').onchange();
await tick();while(library.pending())await tick();
assert.equal(loadedStl,part.name);assert.equal(library.params().library,other.id);
assert.match(window.waamDraft.error(),/Settings changed/);
""", setup=r"""
S.meta.params={library:'1'.repeat(64),owner:'a'.repeat(64),units:'auto'};
""")

    def test_desktop_import_restores_library_identity_and_units_without_a_job_request(self):
        self.run_js(r"""
await library.setIdentity(who());requests.length=0;
const draft=window.waamDraft.export();
draft.source={kind:'library',id:part.id,units:'m'};draft.fields.wbUnits='m';
assert.deepEqual(window.waamDraft.import(draft),{ok:true,missingFile:false});
assert.equal(library.params().library,part.id);assert.equal(el('wbUnits').value,'m');
assert.equal(loadedStl,part.name);assert.equal(el('librarySelect').value,part.source_owner+'/'+part.id);
assert.equal(requests.filter(r=>/\/api\/(?:slice|place|save|jbi)/.test(r.url)).length,0);
""")

    def test_desktop_import_before_identity_restores_library_source_with_gets_only(self):
        self.run_js(r"""
const original=handle;let releaseLibrary;
handle=async(url,options)=>new URL(url,'http://studio.test').pathname==='/api/library'?
  new Promise(resolve=>{releaseLibrary=()=>resolve({ok:true,entries:inventory,owner_id:A});}):original(url,options);
const resolving=library.setIdentity(who());await tick();assert.ok(releaseLibrary);
markDraftReady();assert.equal(window.waamDraft.ready,true);
const draft=window.waamDraft.export();
draft.source={kind:'library',id:part.id,units:'m'};draft.fields.wbUnits='m';
const during=requests.length;
assert.deepEqual(window.waamDraft.import(draft),{ok:true,missingFile:false});
assert.equal(requests.length,during,'Import itself is purely local');
releaseLibrary();await resolving;await tick();
assert.equal(library.params().library,part.id);assert.equal(el('wbUnits').value,'m');
assert.equal(loadedStl,part.name);assert.equal(el('librarySelect').value,part.source_owner+'/'+part.id);
assert.ok(requests.length>0,'Identity resolution may refresh the library');
for(const request of requests){
  assert.ok(!request.options?.method||request.options.method==='GET',request.url+' must stay idempotent');
  assert.doesNotMatch(request.url,/\/api\/library\/units/);
}
await tick();assert.equal(requests.filter(request=>request.options?.method&&request.options.method!=='GET').length,0);
""")

    def test_desktop_block_refuses_library_writes_but_allows_refresh(self):
        self.run_js(r"""
await library.setIdentity(who());requests.length=0;
window.waamDraft.setBlocked('Desktop review is still open.');
assert.equal(el('librarySelect').disabled,true);assert.equal(el('stlFile').disabled,true);
el('librarySelect').value=part.source_owner+'/'+part.id;el('librarySelect').onchange();
el('wbUnits').value='m';el('wbUnits').dispatch('change');await tick();
assert.equal(requests.length,0);assert.match(el('libraryStatus').textContent,/Desktop review/);
await el('libraryRefresh').onclick();
assert.equal(requests.length,1);assert.equal(new URL(requests[0].url,'http://studio.test').pathname,'/api/library');
assert.ok(!requests[0].options?.method||requests[0].options.method==='GET');
window.waamDraft.setBlocked(null);assert.equal(el('librarySelect').disabled,false);
assert.equal(el('stlFile').disabled,false);
""")

    def test_reload_with_unchanged_library_source_keeps_actions_ready(self):
        self.run_js(r"""
await library.setIdentity(who());
assert.equal(window.waamDraft.error(),'');
unlockAccountControls();
for(const id of ['saveBtn','emitBtn','emitRosBtn'])assert.equal(el(id).disabled,false,id);
assert.equal(readyForRobotExport(),true);
""", setup=r"""
S.meta.params={library:'1'.repeat(64),units:'auto'};
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'library',id:'1'.repeat(64),owner:'a'.repeat(64),units:'auto'}}));
""")

    def test_legacy_pending_upload_without_source_remains_blocked(self):
        self.run_js(r"""
assert.match(window.waamDraft.error(),/Reselect the pending STL/);
unlockAccountControls();
for(const id of ['saveBtn','emitBtn','emitRosBtn'])assert.equal(el(id).disabled,true,id);
assert.equal(readyForRobotExport(),false);
const before=requests.length;await el('sliceBtn').onclick();assert.equal(requests.length,before);
""", setup=r"""
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),_pendingFile:true}));
""")

    def test_restored_library_source_and_units_win_over_checked_build(self):
        self.run_js(r"""
const other={...part,id:'2'.repeat(64),name:part.name};inventory=[sample,part,other];
await library.setIdentity(who());
assert.equal(library.params().library,other.id);assert.equal(loadedStl,other.name);
assert.equal(el('wbUnits').value,'mm');
assert.match(window.waamDraft.error(),/Settings changed/);
const qs=await sliceQuery();assert.equal(qs.get('units'),'mm');
""", setup=r"""
S.meta.params={library:'1'.repeat(64),owner:'a'.repeat(64),units:'auto'};
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'library',id:'2'.repeat(64),owner:'a'.repeat(64)},wbUnits:'mm'}));
""")

    def test_admin_can_select_other_owner_only_by_copying_to_own_library(self):
        self.run_js(r"""
const foreign={...part,source_owner:'library-'+B,owner:'bob@example.invalid'};
inventory=[foreign,sample];await library.setIdentity(who(A,{admin:true}));
assert.equal(el('libraryScope').hidden,false);el('libraryScope').value='all';
await el('libraryRefresh').onclick();
el('librarySelect').value=foreign.source_owner+'/'+foreign.id;
el('librarySelect').onchange();await tick();while(library.pending())await tick();
const imported=requests.find(r=>r.url.startsWith('/api/admin/library/import'));
assert.ok(imported);assert.equal(new URL(imported.url,'http://studio.test').searchParams.get('source_owner'),foreign.source_owner);
assert.equal(library.params().library,part.id);assert.equal(library.params().owner,A);
""")

    def test_download_link_targets_only_an_own_stl_original(self):
        self.run_js(r"""
await library.setIdentity(who());
assert.equal(el('libraryDownloadRow').hidden,true);
file();await library.fileChanged();
assert.equal(el('libraryDownloadRow').hidden,false);
const u=new URL(el('libraryDownload').href,'http://studio.test');
assert.equal(u.pathname,'/api/library/file');
assert.equal(u.searchParams.get('id'),part.id);assert.equal(u.searchParams.get('owner'),A);
el('clearStl').onclick();assert.equal(el('libraryDownloadRow').hidden,true);
inventory=[{...part,source_owner:'library-'+B},sample];await el('libraryRefresh').onclick();
el('librarySelect').value='library-'+A+'/sample:wall';el('librarySelect').onchange();
await tick();while(library.pending())await tick();
assert.equal(el('libraryDownloadRow').hidden,true);
""")

    def test_hostile_library_names_are_text_only(self):
        self.run_js(r"""
inventory=[{...part,name:'<img src=x onerror=attack()> .stl'}];await library.setIdentity(who());
const options=el('librarySelect').children;
assert.equal(options.at(-1).textContent,inventory[0].name);
assert.equal(options.at(-1).children.length,0);
""")

    def test_actual_identity_handshake_keeps_guest_locks_and_blocks_unconfirmed_library(self):
        handshake = '(async()=>'+_block_after(_TEMPLATE, r'\(async\s*\(\)\s*=>')+')()'
        for case in ('guest', 'account', 'refused', 'network'):
            with self.subTest(case=case):
                self.run_js(r"""
function showIdentity(){}function showSignIn(){}function showAdminLink(){}function showOffer(){}
const settingsIdentities=[];
function setStudioIdentity(identity){settingsIdentities.push(identity);}
const scenario='""" + case + r"""',previous=handle;
handle=async(url,options)=>{
 if(!url.startsWith('/api/whoami'))return previous(url,options);
 if(scenario==='network')throw new Error('Offline');
 if(scenario==='refused')return {ok:false,guest:true};
 return scenario==='guest'?{ok:true,guest:true}:who();
};
await """ + handshake + r""";
const guest=scenario==='guest';
assert.equal(IS_GUEST,guest);
for(const id of ['saveBtn','loadBtn','emitBtn','emitRosBtn'])assert.equal(el(id).disabled,guest,id);
assert.equal(el('libraryPanel').hidden,scenario!=='account');
if(scenario==='refused'||scenario==='network'){
 assert.deepEqual(settingsIdentities,[{unavailable:true}]);
 assert.equal(await library.prepareSlice(),false);
 assert.match(el('sliceStatus').textContent,/Could not confirm your sign-in/);
}else{
 assert.equal(settingsIdentities.length,1);
 assert.equal(!!settingsIdentities[0].guest,guest);
 if(scenario==='account')assert.equal(settingsIdentities[0].library_owner,A);
 assert.equal(await library.prepareSlice(),true);
}
""")


if __name__ == '__main__':
    unittest.main()
