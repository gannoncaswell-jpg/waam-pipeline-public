import json
import subprocess
import unittest

from tests.test_studio_controls import NODE, ROOT, _DOM, _ShellNodes
from waam_pipeline.slicer import wall
from waam_pipeline.studio import build_state, render_studio


_SETUP = r"""
for(const node of elements){
  node.isConnected=true;
  node.dataset=Object.fromEntries(Object.entries(node.attrs)
    .filter(([key])=>key.startsWith('data-'))
    .map(([key,value])=>[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),value]));
}
document.body=elements.find(node=>node.tag==='body');
document.documentElement=elements.find(node=>node.tag==='html');
Element.prototype.showModal=function(){this.open=true;};
Element.prototype.close=function(){this.open=false;this.dispatch('close');};
Element.prototype.scrollIntoView=function(){this.scrolled=true;};
function fire(node,type,extra={}){
  const event={target:node,defaultPrevented:false,propagationStopped:false,
    preventDefault(){this.defaultPrevented=true;},
    stopPropagation(){this.propagationStopped=true;},...extra};
  node['on'+type]?.(event);
  for(const fn of node.listeners[type]||[])fn(event);
  return event;
}
const A='a'.repeat(64),B='b'.repeat(64);
const defaults={theme:'graphite',density:'comfortable',reduce_motion:false,
  default_frame:'cell',default_camera:'iso',path_only:false};
const prefs=extra=>({...defaults,...extra});
const who=(owner=A,extra={})=>({ok:true,sso:true,guest:false,
  library_owner:owner,email:owner===A?'alice@example.invalid':'bob@example.invalid',...extra});
const guest={ok:true,sso:false,guest:true};
const storageCalls=[],storageValues=new Map();
const storage={
  getItem(key){storageCalls.push({op:'get',key});return storageValues.get(key)??null;},
  setItem(key,value){storageCalls.push({op:'set',key,value});storageValues.set(key,String(value));},
  removeItem(key){storageCalls.push({op:'remove',key});storageValues.delete(key);}
};
let busy=false,blocked=null,pauses=0,resizes=0;
const previews=[];
const apiCalls=[];
const requestApi=(path,params={})=>{apiCalls.push({path,params});
  return path+(Object.keys(params).length?'?'+new URLSearchParams(params):'');};
let handle=async(url,options={})=>({ok:true,
  owner_id:new URL(url,'http://studio.test').searchParams.get('owner'),
  preferences:options.method==='POST'?JSON.parse(options.body):prefs()});
let releaseHandle=async()=>({available:false});
const releaseRequests=[],copied=[];
const requestFetch=async(url,options={})=>{
  const pathname=new URL(url,'http://studio.test').pathname;
  if(pathname==='/api/desktop-release'){
    releaseRequests.push({url,options});const value=await releaseHandle(url,options);
    return {ok:!value.status||value.status<400,status:value.status||200,json:async()=>value};
  }
  requests.push({url,options});const value=await handle(url,options);
  return {ok:!value.status||value.status<400,status:value.status||200,json:async()=>value};
};
const settings=settingsModule.create({document,api:requestApi,fetch:requestFetch,
  pause:()=>{pauses++;setPlaying(false);},busy:()=>busy,blocked:()=>blocked,storage,
  origin:'https://studio.test',clipboard:{writeText:async value=>copied.push(value)},
  applyPreview:(next,previous,options)=>previews.push({next:{...next},previous:{...previous},options}),
  resize:()=>resizes++});
const theme=value=>document.querySelector('[data-settings-theme="'+value+'"]');
function change(id,value){
  const node=el(id);
  if(node.type==='checkbox')node.checked=value;else node.value=value;
  fire(node,'change');
}
function draft(extra={}){
  const value=prefs(extra);
  fire(theme(value.theme),'click');
  change('settingsDensity',value.density);change('settingsReduceMotion',value.reduce_motion);
  change('settingsDefaultFrame',value.default_frame);change('settingsDefaultCamera',value.default_camera);
  change('settingsPathOnly',value.path_only);
  return value;
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const card=key=>document.querySelector('[data-desktop-platform="'+key+'"]');
const field=(key,name)=>card(key).querySelector('[data-desktop-field="'+name+'"]');
const copyButton=key=>card(key).querySelector('[data-desktop-copy]');
const release=(id,extra={})=>({id,url:'/download/desktop/'+id,version:'2.4.0',size:157286400,
  sha256:(id==='windows-x64'?'fedcba9876543210':'0123456789abcdef').repeat(4),
  platform:id==='windows-x64'?'Windows 10/11 (x64)':'macOS, Apple Silicon (arm64)',
  signed:id==='windows-x64'?'unsigned (development)':'ad-hoc (development)',notarized:false,
  published_at:'2026-10-11T14:30:00+00:00',notes:'',...extra});
"""


@unittest.skipUnless(NODE, "Node is required to exercise Studio settings")
class TestStudioSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = build_state(wall(40, layers=2, layer_height_mm=3),
                                workbench=True, workspace="settings-test",
                                request_params={"cell_profile": "lc50s"})
        parser = _ShellNodes()
        parser.feed(render_studio(cls.state).split("<script data-app>", 1)[0])
        cls.shell = parser.nodes

    def run_js(self, scenario):
        source = (
            "const SHELL=" + json.dumps(self.shell) + ";\n"
            "const INITIAL=" + json.dumps(self.state) + ";\n" + _DOM + "\n"
            + "const settingsModule=require("
            + json.dumps(str(ROOT / "src/waam_pipeline/studio_settings.js")) + ");\n"
            + _SETUP + "\n(async()=>{\n" + scenario
            + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        )
        result = subprocess.run([NODE], input=source, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unverified_identity_never_reads_browser_or_account_preferences(self):
        self.run_js(r"""
settings.open();
assert.equal(settings.isOpen(),true);
assert.equal(el('settingsApply').disabled,true);
assert.equal(el('settingsDensity').disabled,true);
assert.equal(requests.length,0);assert.deepEqual(storageCalls,[]);
await settings.setIdentity({unavailable:true});
assert.equal(el('settingsApply').disabled,true);
assert.equal(requests.length,0);assert.deepEqual(storageCalls,[]);
assert.equal(await settings.apply(),false);
assert.deepEqual(storageCalls,[]);assert.equal(requests.length,0);
""")

    def test_apply_is_a_transaction_and_sends_only_the_complete_preference_schema(self):
        self.run_js(r"""
await settings.setIdentity(who());settings.open();
const committed=settings.preferences(),look=document.body.dataset.look;
const before=previews.length;
const next=draft({theme:'drafting',density:'compact',reduce_motion:true,
  default_frame:'part',default_camera:'top',path_only:true});
assert.deepEqual(settings.preferences(),committed);
assert.equal(document.body.dataset.look,look);
assert.equal(previews.length,before,'Drafts must not alter the current 3D preview');
const pending=deferred();handle=()=>pending.promise;
const applying=settings.apply();await tick();
assert.equal(el('settingsApply').disabled,true);
assert.deepEqual(settings.preferences(),committed);
assert.equal(document.body.dataset.look,look);
const request=requests.at(-1),url=new URL(request.url,'http://studio.test');
assert.equal(url.pathname,'/api/preferences');assert.equal(url.searchParams.get('owner'),A);
assert.equal(request.options.method,'POST');assert.deepEqual(JSON.parse(request.options.body),next);
assert.equal(request.options.headers['Content-Type'],'application/json');
pending.resolve({ok:true,owner_id:A,preferences:next});
assert.equal(await applying,true);
assert.deepEqual(settings.preferences(),next);assert.equal(settings.reducedMotion(),true);
assert.equal(document.body.dataset.look,'drafting');assert.equal(document.body.dataset.density,'compact');
assert.equal(previews.length,before+1);assert.deepEqual(previews.at(-1).next,next);
assert.deepEqual(storageCalls,[],'Signed-in preferences must not leak into browser guest storage');
const copy=settings.preferences();copy.theme='operator';
assert.equal(settings.preferences().theme,'drafting','Callers cannot mutate the committed object');
""")

    def test_cancel_and_back_discard_the_draft_without_changing_the_build_or_view(self):
        self.run_js(r"""
await settings.setIdentity(who());
el('wbLh').value='4.25';el('wbArc').value='27';el('wbJobName').value='Keep this build';
cursor=7;yaw=.31;pitch=.42;zoom=3.2;panX=4;panY=8;perspective=true;dolly=10;
const state=JSON.stringify(S),fields=elements.filter(n=>n.id.startsWith('wb')).map(n=>n.value);
const view=()=>[cursor,yaw,pitch,zoom,panX,panY,perspective,dolly];
const camera=view(),committed=settings.preferences(),count=requests.length;
const applied=previews.length,size=resizes;
el('settingsBtn').focus();settings.open();draft({theme:'operator',default_camera:'top'});
settings.cancel();
assert.deepEqual(settings.preferences(),committed);assert.equal(el('settingsDefaultCamera').value,'iso');
draft({theme:'drafting',default_frame:'part'});fire(el('settingsBack'),'click');
assert.equal(settings.isOpen(),false);assert.equal(document.activeElement,el('settingsBtn'));
settings.open();assert.equal(theme(committed.theme).getAttribute('aria-pressed'),'true');
assert.equal(el('settingsDefaultFrame').value,committed.default_frame);settings.close();
assert.equal(JSON.stringify(S),state);assert.deepEqual(view(),camera);
assert.deepEqual(elements.filter(n=>n.id.startsWith('wb')).map(n=>n.value),fields);
assert.equal(requests.length,count);assert.equal(previews.length,applied);assert.equal(resizes,size);
assert.equal(playing,false);assert.ok(pauses>0);
""")

    def test_save_failure_preserves_the_draft_and_current_preferences_for_retry(self):
        self.run_js(r"""
await settings.setIdentity(who());settings.open();
const committed=settings.preferences(),before=previews.length;
const next=draft({theme:'operator',density:'compact',default_frame:'part'});
handle=async()=>({ok:false,status:503,error:'Preference storage is unavailable'});
assert.equal(await settings.apply(),false);
assert.deepEqual(settings.preferences(),committed);
assert.equal(document.body.dataset.look,committed.theme);assert.equal(previews.length,before);
assert.equal(theme('operator').getAttribute('aria-pressed'),'true');
assert.equal(el('settingsDensity').value,'compact');assert.equal(el('settingsDefaultFrame').value,'part');
assert.match(el('settingsStatus').textContent,/not saved|unavailable/i);
handle=async()=>({ok:true,owner_id:A,preferences:next});
assert.equal(el('settingsRetry').hidden,false);
fire(el('settingsRetry'),'click');await tick();
assert.equal(requests.at(-1).options.method,'POST','Retry must save the retained draft rather than reload defaults');
assert.deepEqual(JSON.parse(requests.at(-1).options.body),next);
assert.deepEqual(settings.preferences(),next);
""")

    def test_failed_load_keeps_controls_locked_and_does_not_fall_back_to_guest_storage(self):
        self.run_js(r"""
handle=async()=>({ok:false,status:503,error:'Preferences could not be loaded'});
assert.equal(await settings.setIdentity(who()),false);settings.open();
assert.equal(el('settingsApply').disabled,true);assert.equal(el('settingsDensity').disabled,true);
assert.deepEqual(storageCalls,[]);
assert.match(el('settingsStatus').textContent,/could not load|could not be loaded/i);
assert.equal(el('settingsRetry').hidden,false);
handle=async()=>({ok:true,owner_id:A,preferences:prefs({theme:'operator'})});
fire(el('settingsRetry'),'click');await tick();
assert.equal(settings.preferences().theme,'operator');assert.equal(el('settingsDensity').disabled,false);
assert.deepEqual(storageCalls,[]);
""")

    def test_account_change_ignores_stale_loads_and_resets_another_accounts_draft(self):
        self.run_js(r"""
const alice=deferred();
handle=async(url)=>new URL(url,'http://studio.test').searchParams.get('owner')===A?
  alice.promise:{ok:true,owner_id:B,preferences:prefs({theme:'drafting',default_camera:'top'})};
const loadingAlice=settings.setIdentity(who(A));await tick();
await settings.setIdentity(who(B));
const bob=settings.preferences(),count=previews.length;
alice.resolve({ok:true,owner_id:A,preferences:prefs({theme:'operator'})});await loadingAlice;
assert.deepEqual(settings.preferences(),bob);assert.equal(document.body.dataset.look,'drafting');
assert.equal(previews.length,count);assert.deepEqual(storageCalls,[]);
settings.open();draft({theme:'operator'});
await settings.setIdentity(guest);
assert.equal(settings.preferences().theme,'graphite');
assert.equal(theme('graphite').getAttribute('aria-pressed'),'true');
assert.doesNotMatch(el('settingsIdentity').textContent,/bob@example/);
assert.equal(storageCalls.some(call=>call.op==='set'),false,'Identity changes cannot save another account draft');
""")

    def test_owner_mismatch_is_rejected_on_load_and_save(self):
        self.run_js(r"""
handle=async()=>({ok:true,owner_id:B,preferences:prefs({theme:'operator'})});
assert.equal(await settings.setIdentity(who(A)),false);
assert.notEqual(settings.preferences().theme,'operator');assert.equal(el('settingsApply').disabled,true);
handle=async()=>({ok:true,owner_id:A,preferences:prefs()});
await settings.setIdentity(who(A));settings.open();
const committed=settings.preferences(),before=previews.length;draft({theme:'drafting'});
handle=async()=>({ok:true,owner_id:B,preferences:prefs({theme:'drafting'})});
assert.equal(await settings.apply(),false);
assert.deepEqual(settings.preferences(),committed);assert.equal(previews.length,before);
assert.equal(theme('drafting').getAttribute('aria-pressed'),'true');
assert.deepEqual(storageCalls,[]);
""")

    def test_stale_save_cannot_overwrite_the_new_account_or_its_draft(self):
        self.run_js(r"""
await settings.setIdentity(who(A));settings.open();const old=draft({theme:'operator'});
const save=deferred();
handle=async(url,options)=>options.method==='POST'?save.promise:
  {ok:true,owner_id:B,preferences:prefs({theme:'drafting'})};
const applying=settings.apply();await tick();
await settings.setIdentity(who(B));
const current=settings.preferences(),before=previews.length;
draft({theme:'graphite',density:'compact'});
save.resolve({ok:true,owner_id:A,preferences:old});await applying;
assert.deepEqual(settings.preferences(),current);assert.equal(document.body.dataset.look,'drafting');
assert.equal(theme('graphite').getAttribute('aria-pressed'),'true');
assert.equal(el('settingsDensity').value,'compact');assert.equal(previews.length,before);
assert.deepEqual(storageCalls,[]);
""")

    def test_confirmed_guest_and_offline_preferences_use_separate_local_storage(self):
        self.run_js(r"""
await settings.setIdentity(guest);settings.open();
assert.ok(storageCalls.some(call=>call.op==='get'));assert.equal(requests.length,0);
const guestPrefs=draft({theme:'drafting',density:'compact'});
assert.equal(await settings.apply(),true);
const saved=storageCalls.findLast(call=>call.op==='set');assert.ok(saved);
assert.deepEqual(JSON.parse(saved.value),guestPrefs);
await settings.setIdentity({offline:true});
assert.notEqual(storageCalls.at(-1).key,saved.key);
assert.equal(settings.preferences().theme,'graphite');
await settings.setIdentity(guest);assert.deepEqual(settings.preferences(),guestPrefs);
assert.equal(requests.length,0);
for(const call of storageCalls)assert.doesNotMatch(JSON.stringify(call),/alice@|bob@|library_owner/);
""")

    def test_browser_storage_failure_applies_for_this_visit_without_claiming_persistence(self):
        self.run_js(r"""
await settings.setIdentity(guest);settings.open();
const before=previews.length,next=draft({theme:'operator'});
storage.setItem=()=>{throw new Error('Storage disabled');};
assert.equal(await settings.apply(),true);
assert.deepEqual(settings.preferences(),next);assert.equal(previews.length,before+1);
assert.equal(document.body.dataset.look,next.theme);
assert.equal(theme('operator').getAttribute('aria-pressed'),'true');
assert.match(el('settingsStatus').textContent,/Applied for this visit.*browser storage is unavailable/i);
assert.doesNotMatch(el('settingsStatus').textContent,/saved/i);assert.equal(requests.length,0);
""")

    def test_keyboard_categories_and_native_escape_preserve_focus_and_preview(self):
        self.run_js(r"""
await settings.setIdentity(who());el('settingsBtn').focus();settings.open();
const before=previews.length,size=resizes;
let event=fire(el('settings-tab-appearance'),'keydown',{key:'ArrowDown'});
assert.equal(event.defaultPrevented,true);assert.equal(document.activeElement,el('settings-tab-preview'));
assert.equal(el('settings-preview').hidden,false);assert.equal(el('settings-appearance').hidden,true);
assert.equal(el('settings-tab-preview').getAttribute('aria-selected'),'true');
fire(el('settings-tab-preview'),'keydown',{key:'End'});
assert.equal(document.activeElement,el('settings-tab-desktop'));
fire(el('settings-tab-desktop'),'keydown',{key:'ArrowUp'});
assert.equal(document.activeElement,el('settings-tab-account'));
fire(el('settings-tab-account'),'keydown',{key:'Home'});
assert.equal(document.activeElement,el('settings-tab-appearance'));
draft({theme:'operator'});
event=fire(el('settingsDlg'),'keydown',{key:' ',code:'Space'});
assert.equal(event.propagationStopped,true,'Build shortcuts cannot run while settings owns focus');
event=fire(el('settingsDlg'),'cancel');
if(!event.defaultPrevented)el('settingsDlg').close();
assert.equal(settings.isOpen(),false);assert.equal(document.activeElement,el('settingsBtn'));
settings.open();assert.equal(theme('graphite').getAttribute('aria-pressed'),'true');
assert.equal(previews.length,before);assert.equal(resizes,size);
""")

    def test_desktop_tab_fetches_and_renders_available_absent_guest_and_error_states(self):
        self.run_js(r"""
const mac=release('macos-arm64',{notes:'Team preview'}),windows=release('windows-x64',{size:104857600});
releaseHandle=async()=>({available:true,releases:[mac,windows]});
await settings.setIdentity(who());settings.open();settings.chooseTab('desktop');await tick();
assert.equal(releaseRequests.length,1);
assert.equal(new URL(releaseRequests[0].url,'http://studio.test').pathname,'/api/desktop-release');
assert.equal(releaseRequests[0].options.credentials,'same-origin');
assert.equal(el('settings-desktop').hidden,false);assert.equal(el('settings-appearance').hidden,true);
assert.equal(el('settingsDesktopRelease').hidden,false);
assert.equal(card('macos-arm64').hidden,false);assert.equal(card('windows-x64').hidden,false);
assert.equal(field('macos-arm64','version').textContent,'2.4.0');
assert.equal(field('macos-arm64','platform').textContent,'macOS, Apple Silicon (arm64)');
assert.equal(field('macos-arm64','size').textContent,'150.0 MB');
assert.equal(field('macos-arm64','sha').textContent,mac.sha256);
assert.match(field('macos-arm64','trust').textContent,/ad-hoc.*not notarized/);
assert.equal(field('macos-arm64','notes').textContent,'Team preview');
assert.equal(field('macos-arm64','notes').hidden,false);
assert.equal(field('macos-arm64','download').href,'/download/desktop/macos-arm64');
assert.equal(field('windows-x64','platform').textContent,'Windows 10/11 (x64)');
assert.equal(field('windows-x64','size').textContent,'100.0 MB');
assert.equal(field('windows-x64','sha').textContent,windows.sha256);
assert.equal(field('windows-x64','trust').textContent,'unsigned (development)');
assert.equal(field('windows-x64','notes').hidden,true);
assert.equal(field('windows-x64','download').href,'/download/desktop/windows-x64');
assert.equal(apiCalls.some(call=>call.path.startsWith('/download/desktop')),false);
assert.equal(field('windows-x64','download').getAttribute('download'),'');
assert.equal(el('settingsDesktopOrigin').textContent,'https://studio.test');
await copyButton('windows-x64').onclick();assert.deepEqual(copied,[windows.sha256]);
assert.equal(copyButton('windows-x64').textContent,'Copied');
assert.equal(copyButton('macos-arm64').textContent,'Copy');

releaseHandle=async()=>({available:true,releases:[windows,release('linux-x64')]});
await settings.loadDesktop();
assert.equal(card('macos-arm64').hidden,true,'An unpublished platform is not offered');
assert.equal(card('windows-x64').hidden,false);
releaseHandle=async()=>({available:true,releases:[release('linux-x64')]});await settings.loadDesktop();
assert.equal(el('settingsDesktopRelease').hidden,true);
assert.equal(el('settingsDesktopState').textContent,'No desktop build has been published on this server yet.');
releaseHandle=async()=>({available:true,releases:[{...mac,url:'https://elsewhere.example/build.dmg'}]});
await settings.loadDesktop();
assert.match(el('settingsDesktopState').textContent,/invalid desktop build information/);
assert.equal(el('settingsDesktopRelease').hidden,true);
assert.equal(field('macos-arm64','download').getAttribute('href'),null);
assert.equal(copyButton('macos-arm64').disabled,true);
releaseHandle=async()=>({available:true,releases:[mac,windows]});await settings.loadDesktop();

const beforeGuest=releaseRequests.length;await settings.setIdentity(guest);await tick();
assert.equal(releaseRequests.length,beforeGuest);
assert.equal(el('settingsDesktopRelease').hidden,true);
assert.equal(el('settingsDesktopState').textContent,'Sign in to download the desktop app.');

releaseHandle=async()=>({available:false,releases:[]});await settings.setIdentity(who());await tick();
assert.equal(el('settingsDesktopState').textContent,'No desktop build has been published on this server yet.');
releaseHandle=async()=>({status:503,error:'down'});await settings.loadDesktop();
assert.match(el('settingsDesktopState').textContent,/unavailable/i);
assert.equal(el('settingsDesktopState').dataset.error,'true');
assert.equal(el('settingsDesktopRetry').hidden,false);
""")

    def test_desktop_cards_show_each_platforms_release_signature_state(self):
        self.run_js(r"""
const signature='AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+Pw==';
assert.equal(signature.length,88);
releaseHandle=async()=>({available:true,releases:[release('macos-arm64'),
  release('windows-x64',{version:'0.2.0',signature})]});
await settings.setIdentity(who());settings.open();settings.chooseTab('desktop');await tick();
assert.equal(field('windows-x64','signature').textContent,'present');
assert.equal(field('macos-arm64','signature').textContent,'not signed — install manually');
assert.equal(field('windows-x64','trust').textContent,'unsigned (development)',
  'The release signature is separate from operating-system code signing');
const markup=JSON.stringify(SHELL);
assert.match(markup,/Release signature/);
assert.match(markup,/0\.2\.0 and later update themselves/);
assert.match(markup,/Help › Check for Updates…/);
assert.match(markup,/installed by hand once/);

releaseHandle=async()=>({available:true,releases:[release('macos-arm64',{signature})]});
await settings.loadDesktop();
assert.equal(field('macos-arm64','signature').textContent,'present');
for(const bad of [signature.slice(0,-2),signature.replace(/\+/g,'-'),'<b>present</b>',64,null]){
  releaseHandle=async()=>({available:true,releases:[release('windows-x64',{signature:bad})]});
  await settings.loadDesktop();
  assert.match(el('settingsDesktopState').textContent,/invalid desktop build information/);
  assert.equal(el('settingsDesktopRelease').hidden,true);
}
""")

    def test_busy_build_and_overlapping_saves_cannot_apply_competing_changes(self):
        self.run_js(r"""
await settings.setIdentity(who());settings.open();const next=draft({theme:'drafting'});
const before=requests.length;busy=true;
assert.equal(await settings.apply(),false);assert.equal(requests.length,before);
assert.equal(theme('drafting').getAttribute('aria-pressed'),'true');
assert.match(el('settingsStatus').textContent,/Wait.*build update/);
busy=false;const pending=deferred();handle=()=>pending.promise;
const applying=settings.apply();await tick();
assert.equal(await settings.apply(),false);assert.equal(requests.length,before+1);
assert.equal(settings.close(),false);assert.equal(settings.isOpen(),true);
assert.equal(settings.cancel(),false);
fire(theme('operator'),'click');assert.equal(theme('drafting').getAttribute('aria-pressed'),'true');
pending.resolve({ok:true,owner_id:A,preferences:next});assert.equal(await applying,true);
assert.deepEqual(settings.preferences(),next);assert.equal(settings.close(),true);
""")

    def test_desktop_block_refuses_preference_writes_but_keeps_desktop_reads_available(self):
        self.run_js(r"""
await settings.setIdentity(who());settings.open();draft({theme:'drafting'});
const before=requests.length;blocked='Desktop review is still open.';settings.paint();
assert.equal(el('settingsApply').disabled,true);
assert.equal(await settings.apply(),false);assert.equal(requests.length,before);
assert.equal(settings.preferences().theme,'graphite');
assert.equal(el('settingsStatus').textContent,blocked);
releaseHandle=async()=>({available:true,releases:[release('macos-arm64',{size:1024,
  platform:'macOS',signed:'signed',notarized:true})]});
settings.chooseTab('desktop');await tick();
assert.equal(releaseRequests.length,1,'The desktop release lookup stays read-only and available');
assert.equal(field('macos-arm64','download').href,'/download/desktop/macos-arm64');
assert.equal(field('macos-arm64','trust').textContent,'signed; notarized');
blocked=null;settings.paint();assert.equal(el('settingsApply').disabled,false);
assert.equal(await settings.apply(),true);assert.equal(requests.length,before+1);
""")

    def test_signed_in_identity_without_owner_cannot_use_account_or_guest_storage(self):
        self.run_js(r"""
assert.equal(await settings.setIdentity(who(null)),false);settings.open();
assert.equal(requests.length,0);assert.deepEqual(storageCalls,[]);
assert.equal(el('settingsApply').disabled,true);assert.equal(el('settingsDensity').disabled,true);
assert.match(el('settingsStatus').textContent,/verify.*owner/i);
assert.equal(await settings.apply(),false);
""")


if __name__ == "__main__":
    unittest.main()
