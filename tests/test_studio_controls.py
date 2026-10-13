import json
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.studio import _TEMPLATE, build_state, render_studio
from waam_pipeline.studio_layout import controls_script, placement_script
from waam_pipeline.slicer import wall
from tests.test_studio import _block_after

NODE = shutil.which("node")


class _ShellNodes(HTMLParser):


    def __init__(self):
        super().__init__()
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append({"tag": tag, "attrs": dict(attrs), "text": "",
                           "parent": self.stack[-1] if self.stack else None})
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[i]]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.stack and self.nodes[self.stack[-1]]["tag"] not in {"style", "script"}:
            self.nodes[self.stack[-1]]["text"] += data


_DOM = r"""
const assert=require('node:assert/strict');
class Events {
  constructor(){this.listeners={};}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  dispatch(type){
    const event={target:this};this['on'+type]?.(event);
    for(const fn of this.listeners[type]||[])fn(event);
  }
  dispatchEvent(event){
    if(!event.target)Object.defineProperty(event,'target',{value:this,configurable:true});
    this['on'+event.type]?.(event);
    for(const fn of this.listeners[event.type]||[])fn(event);
    return true;
  }
}
class CustomEvent {
  constructor(type,options={}){this.type=type;this.detail=options.detail;}
}
function matches(el,selector){
  if(selector.includes(','))return selector.split(',').some(s=>matches(el,s.trim()));
  const excluded=/:not\(#([\w-]+)\)$/.exec(selector);
  if(excluded)return el.id!==excluded[1] && matches(el,selector.slice(0,excluded.index));
  if(selector.endsWith(':not(:disabled)')){
    if(el.disabled)return false;
    selector=selector.slice(0,-':not(:disabled)'.length);
  }
  const gap=selector.lastIndexOf(' ');
  if(gap>=0){
    if(!matches(el,selector.slice(gap+1)))return false;
    for(let p=el.parentElement;p;p=p.parentElement)if(matches(p,selector.slice(0,gap)))return true;
    return false;
  }
  if(selector.startsWith('.'))return (el.attrs.class||'').split(/\s+/).includes(selector.slice(1));
  if(selector.startsWith('#'))return el.id===selector.slice(1);
  const attr=/^([a-z]+)?\[([^=\]]+)(?:="([^"]+)")?\]$/.exec(selector);
  if(attr)return (!attr[1]||el.tag===attr[1]) && Object.hasOwn(el.attrs,attr[2]) &&
    (attr[3]===undefined||el.attrs[attr[2]]===attr[3]);
  if(/^[a-z]+$/.test(selector))return el.tag===selector;
  throw new Error('Unsupported test selector: '+selector);
}
class Element extends Events {
  constructor(spec){
    super();Object.assign(this,spec);this.id=this.attrs.id||'';this.children=[];
    this.dataset=Object.fromEntries(Object.entries(this.attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5),v]));
    this.disabled=Object.hasOwn(this.attrs,'disabled');this.hidden=Object.hasOwn(this.attrs,'hidden');
    this.checked=Object.hasOwn(this.attrs,'checked');this.open=Object.hasOwn(this.attrs,'open');
    this.defaultChecked=this.checked;
    this._value=this.attrs.value||'';this.files=[];this.style={};
    for(const key of ['type','min','max','step','pattern'])this[key]=this.attrs[key]||'';
    this.validationMessage='';this.scrollTop=0;
    this.clientWidth=1000;this.clientHeight=700;
  }
  get value(){return this._value;}
  get valueAsNumber(){return this.value.trim()===''?NaN:Number(this.value);}
  set value(v){this._value=String(v);if(this.attrs.type==='file' && v==='')this.files=[];}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(v){this.text=String(v);this.children=[];}
  set innerHTML(v){this.text=String(v);this.children=[];}
  getAttribute(k){return this.attrs[k]??null;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  removeAttribute(k){delete this.attrs[k];}
  get href(){return this.attrs.href;}
  set href(v){this.attrs.href=v;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){return this.children.flatMap(c=>[...(matches(c,selector)?[c]:[]),...c.querySelectorAll(selector)]);}
  closest(selector){for(let p=this;p;p=p.parentElement)if(matches(p,selector))return p;return null;}
  contains(node){for(let p=node;p;p=p.parentElement)if(p===this)return true;return false;}
  appendChild(child){child.parentElement=this;this.children.push(child);return child;}
  after(child){
    child.parentElement=this.parentElement;
    this.parentElement.children.splice(this.parentElement.children.indexOf(this)+1,0,child);
    elements.push(child);
  }
  replaceChildren(){this.children=[];this.text='';}
  focus(){document.activeElement=this;}
  reportValidity(){this.reported=true;return this.checkValidity();}
  setCustomValidity(message){this.validationMessage=String(message);}
  checkValidity(){
    if(this.validationMessage)return false;
    if(this.attrs.type!=='number')return true;
    const n=Number(this.value),a=this.attrs;
    if(!this.value.trim())return !Object.hasOwn(a,'required');
    if(!Number.isFinite(n)||(a.min!==undefined&&n<Number(a.min))||(a.max!==undefined&&n>Number(a.max)))return false;

    if(a.step==='any')return true;
    const step=Number(a.step||1),base=Number(a.min??a.value??0),multiple=(n-base)/step;
    return Math.abs(multiple-Math.round(multiple))<1e-8;
  }
}
const elements=SHELL.map(spec=>new Element(spec));
for(const el of elements)if(el.parent!==null)elements[el.parent].appendChild(el);
for(const el of elements.filter(e=>e.tag==='select')){
  el.options=el.querySelectorAll('option');
  el.selectedIndex=Math.max(0,el.querySelectorAll('option').findIndex(e=>Object.hasOwn(e.attrs,'selected')));
  el.value=el.options[el.selectedIndex]?.value||'';
}
const document=new Events();
document.getElementById=id=>elements.find(e=>e.id===id)||null;
document.querySelectorAll=selector=>elements.filter(e=>matches(e,selector));
document.querySelector=selector=>document.querySelectorAll(selector)[0]||null;
document.createElement=tag=>new Element({tag,attrs:{},text:'',parent:null});
const window=new Events();window.devicePixelRatio=1;
window.scrollX=0;window.scrollY=0;window.scrollTo=(x,y)=>{window.scrollX=x;window.scrollY=y;};
const stored=new Map(),sessionStorage={getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,String(v))};
const location={hash:'',reload(){this.reloads=(this.reloads||0)+1;}};
const S=INITIAL;
const canvas=document.getElementById('c'),slider=document.getElementById('cursor');
const renderStyle=document.getElementById('renderStyle');
let gpu={updates:0,setState(state){this.state=state;this.updates++;},dispose(){this.disposed=true;}};
let M=S.toolpath.moves,N=M.length,totalDep=0,drag=null;
const cum=[0];
let cx=900,cy=-600,cz=200,span=3200;
const cellCenter=[cx,cy,cz];
let pcx=100,pcy=200,pcz=35,partZoom=3;
let yaw=-.8,pitch=.4,zoom=1.45,panX=0,panY=0,cursor=0,IS_GUEST=false;
let perspective=false,dolly=0;
let draws=0,playing=true;
function draw(){draws++;}
function setPlaying(value){playing=value;}
function updateBounds(){} function updateDisplay(){} function renderArcParams(){}
const api=url=>url,requests=[];
let response={ok:false,error:'Test response: preserve the current build'};
async function fetch(url,options){requests.push({url,options});return {ok:true,json:async()=>response};}
async function attestPlatter(){throw new Error('Unexpected attestation');}
for(const el of elements)if(el.id.startsWith('wb'))globalThis[el.id]=el;
const el=id=>{const found=document.getElementById(id);assert.ok(found,'Missing shell element: '+id);return found;};
async function sliceQuery(){
  const count=requests.length;await el('sliceBtn').onclick();
  assert.equal(requests.length,count+1,'The real slice handler should send a request');
  return new URL(requests.at(-1).url,'http://studio.test').searchParams;
}
"""


@unittest.skipUnless(NODE, "Node is needed to execute Studio workflow controls")
class TestStudioControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = build_state(wall(60, layers=50, layer_height_mm=3),
                            title="previous.stl", workbench=True, workspace="alice",
                            violations=[], shape="wall",
                            stl_info={"resolved_units": "mm", "size_mm": [60, 8, 150]})
        cls.initial = json.dumps(state)
        shell, script = render_studio(state).split("<script data-app>", 1)
        script = script.split("</script>", 1)[0]
        parser = _ShellNodes()
        parser.feed(shell)
        cls.shell = json.dumps(parser.nodes)


        workbench = _block_after(script, r'if\(S.meta.workbench\)')[1:-1]
        cls.handlers = workbench.split('  (async()=>{\n    let guest=false, identityKnown=false;', 1)[0]
        for button in ("loadBtn", "emitBtn", "emitRosBtn", "saveBtn"):
            anchor = 'document.getElementById("' + button + '").onclick=async()=>'
            cls.handlers += anchor + _block_after(script, re.escape(anchor)) + ";\n"
        anchor = 'for(const link of document.querySelectorAll(\'#wb a[download="part.gcode"]\'))link.onclick=async event=>'
        cls.handlers += anchor + _block_after(script, re.escape(anchor)) + ";\n"
        cls.ui = "const UI=" + _block_after(script, r"const UI=") + ";\n"
        for name in ("updateToolpath", "renderState", "adoptWb"):
            declaration = re.search(r"function " + name + r"\([^)]*\)", script).group()
            cls.handlers += declaration + _block_after(script, re.escape(declaration)) + "\n"
        declaration = "async function hydrateState(state)"
        cls.handlers += declaration + _block_after(script, re.escape(declaration)) + "\n"
        assignment = "window.onkeydown=e=>"
        cls.handlers += assignment + _block_after(script, re.escape(assignment)) + ";\n"
        declaration = "function markDraftReady()"
        cls.handlers += declaration + _block_after(script, re.escape(declaration)) + "\n"
        cls.projection = "\n".join(re.search(r"function " + name + r"\([^)]*\)\{.*?\n\}",
                                             _TEMPLATE, re.S).group()
                                   for name in ("cameraScale", "cameraEyeDistance", "proj"))
        cls.resize_binding = re.search(r"window\.onresize=[^;\n]+;", _TEMPLATE).group()

    def run_js(self, body, setup=""):
        script = ("const SHELL=" + self.shell + ";\nconst INITIAL=" + self.initial + ";\n"
                  + _DOM + "\n" + self.ui + self.projection
                  + "\n" + controls_script() + "\n" + setup + "\n" + self.handlers
                  + "\ninitializeStudioUI();\n" + self.resize_binding + "\n"
                  + "(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});")
        result = subprocess.run([NODE], input=script, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_edits_invalidate_clearance_and_block_save_and_both_exports(self):
        self.run_js(r"""
unlockAccountControls();const before=el('wbLh').value;
el('wbLh').value='3.5';el('wbLh').dispatch('input');
assert.equal(el('clearanceBadge').dataset.state,'pending');
assert.equal(el('reviewVerdict').dataset.state,'pending');
for(const id of ['emitBtn','emitRosBtn','saveBtn'])assert.equal(el(id).disabled,true,id);
const download=document.querySelector('#wb a[download="part.gcode"]');
assert.equal(download.getAttribute('href'),null);assert.equal(download.getAttribute('aria-disabled'),'true');
assert.equal(readyForRobotExport(),false);
await el('saveBtn').onclick();await el('emitBtn').onclick();await el('emitRosBtn').onclick();
assert.equal(requests.length,0,'No old build may be saved/exported behind a changed form');
assert.match(el('wbMsg').textContent,/Settings changed/);
el('wbLh').value=before;el('wbLh').dispatch('input');
assert.equal(el('clearanceBadge').dataset.state,'clear');assert.equal(el('emitBtn').disabled,false);
""")

    def test_placement_checks_changed_prepare_settings_together(self):
        self.run_js(r"""
el('wbLh').value='3.5';el('wbTravel').value='210';el('wbOx').value='12';
response={ok:true,state:structuredClone(S)};
response.state.meta.params={lh:'3.5',travel:'210',ox:'12'};
assert.equal(await applyPlacement(),true);
const qs=new URL(requests.at(-1).url,'http://studio.test');
assert.equal(qs.pathname,'/api/slice');assert.equal(qs.searchParams.get('lh'),'3.5');
assert.equal(qs.searchParams.get('travel'),'210');assert.equal(qs.searchParams.get('ox'),'12');
assert.equal(el('wbLh').value,'3.5');assert.equal(el('wbTravel').value,'210');
assert.equal(window.waamDraft.error(),'');assert.equal(UI.step,'place');
""")

    def test_placement_draft_survives_navigation_and_refusal(self):
        self.run_js(r"""
S.meta.params={ox:'0',oy:'0',yaw:'0'};S.meta.placement={origin:[0,0,0]};
switchStep('place');el('wbOx').value='12';el('wbOx').dispatch('input');
switchStep('review');switchStep('export');
assert.equal(el('wbOx').value,'12');assert.equal(readyForRobotExport(),false);
response={ok:false,error:'deliberate refusal'};await applyPlacement();
assert.equal(el('wbOx').value,'12');assert.match(window.waamDraft.error(),/Placement changed/);
""")

    def test_reload_restores_same_build_draft_and_blocks_old_export(self):
        self.run_js(r"""
assert.equal(el('wbLh').value,'3.5');assert.equal(el('wbOx').value,'12');
assert.match(window.waamDraft.error(),/Settings changed/);unlockAccountControls();
assert.equal(el('emitBtn').disabled,true);assert.equal(el('saveBtn').disabled,true);
assert.equal(readyForRobotExport(),false);
""", setup=r"""
sessionStorage.setItem(UI.fields,JSON.stringify({_draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),wbLh:'3.5',wbOx:'12'}));
""")

    def test_draft_from_another_build_never_overrides_loaded_settings(self):
        self.run_js(r"""
assert.equal(el('wbLh').value,'2.9');assert.equal(el('wbOx').value,'0');
assert.equal(window.waamDraft.error(),'');
""", setup="sessionStorage.setItem(UI.fields,JSON.stringify({_draftBase:'another build',wbLh:'3.5',wbOx:'12'}));")

    def test_source_draft_persists_removed_and_pending_upload(self):
        self.run_js(r"""
el('clearStl').onclick();
let saved=JSON.parse(sessionStorage.getItem(UI.fields));
assert.deepEqual(saved.source,{kind:'none'});
const file={name:'next.stl',size:42,lastModified:123,arrayBuffer:async()=>new ArrayBuffer(0)};
el('stlFile').files=[file];el('stlFile').dispatch('change');
saved=JSON.parse(sessionStorage.getItem(UI.fields));
assert.deepEqual(saved.source,{kind:'upload',name:'next.stl',pending:true});
assert.equal(saved.pendingFile,true);
""")

    def test_wire_feed_is_optional_validated_persisted_and_sent_to_slice(self):
        self.run_js(r"""
assert.match(el('wfsHelp').textContent,/predicts layer pitch only/i);
let qs=await sliceQuery();assert.equal(qs.get('wfs'),'');
el('wbWfs').value='350';el('wbWfs').dispatch('input');rememberWb();
let saved=JSON.parse(sessionStorage.getItem(UI.fields));assert.equal(saved.fields.wbWfs,'350');
qs=await sliceQuery();assert.equal(qs.get('wfs'),'350');
for(const value of ['9','2001']){
  el('wbWfs').value=value;el('wbWfs').dispatch('input');
  const before=requests.length;await el('sliceBtn').onclick();assert.equal(requests.length,before);
  assert.match(el('wbMsg').textContent,/Wire feed: enter 10.*2000/);
}
el('wbWfs').value='';el('wbWfs').dispatch('input');assert.equal(numberError(el('wbWfs')),'');
""")

    def test_desktop_draft_export_import_round_trip_and_source_semantics(self):
        self.run_js(r"""
assert.equal(window.waamDraft.version,1);assert.equal(Object.isFrozen(window.waamDraft.fields),true);
assert.ok(window.waamDraft.fields.includes('wbWfs'));
el('wbLh').value='4.25';el('wbSmooth').checked=false;
const fieldsDraft=window.waamDraft.export();
el('wbLh').value='9';el('wbSmooth').checked=true;
assert.deepEqual(window.waamDraft.import(fieldsDraft),{ok:true,missingFile:false});
assert.equal(el('wbLh').value,'4.25');assert.equal(el('wbSmooth').checked,false);

const library=window.waamDraft.export();library.source={kind:'library',id:'a'.repeat(64),units:'in'};
library.fields.wbUnits='in';
assert.deepEqual(window.waamDraft.import(library),{ok:true,missingFile:false});
assert.deepEqual(window.waamDraft.export().source,library.source);assert.equal(el('wbUnits').value,'in');

const removed=window.waamDraft.export();removed.source={kind:'none'};removed.pendingFile=false;
assert.deepEqual(window.waamDraft.import(removed),{ok:true,missingFile:false});
assert.deepEqual(window.waamDraft.export().source,{kind:'none'});assert.equal(loadedStl,null);

const pending=window.waamDraft.export();
pending.source={kind:'upload',name:'desktop.stl',pending:true};pending.pendingFile=true;
assert.deepEqual(window.waamDraft.import(pending),{ok:true,missingFile:true});
assert.equal(el('stlName').textContent,'desktop.stl');
assert.match(window.waamDraft.error(),/Reselect the pending STL/);
assert.equal(requests.filter(r=>/\/api\/(?:slice|place|save|jbi)/.test(r.url)).length,0);
""")

    def test_desktop_draft_rejects_another_base_without_changing_anything(self):
        self.run_js(r"""
const before=window.waamDraft.export(),bad=structuredClone(before);
bad.base='another build';bad.fields.wbLh='99';bad.source={kind:'none'};
assert.deepEqual(window.waamDraft.import(bad),{ok:false,reason:'base'});
assert.deepEqual(window.waamDraft.export(),before);
const invalid=structuredClone(before);invalid.version=2;
assert.deepEqual(window.waamDraft.import(invalid),{ok:false,reason:'invalid'});
assert.deepEqual(window.waamDraft.export(),before);assert.equal(requests.length,0);
""")

    def test_remember_uses_the_exact_desktop_export(self):
        self.run_js(r"""
el('wbWfs').value='412';el('wbSmooth').checked=false;rememberWb();
assert.deepEqual(JSON.parse(sessionStorage.getItem(UI.fields)),window.waamDraft.export());
""")

    def test_desktop_block_enumerates_main_write_and_job_handlers_then_unblocks(self):
        self.run_js(r"""
unlockAccountControls();switchStep('prepare');
assert.equal(window.waamDraft.setBlocked('Desktop review is still open.'),true);
const before=requests.length;
for(const id of ['saveBtn','loadBtn','emitBtn','emitRosBtn','wbThermalApply'])assert.equal(el(id).disabled,true,id);
for(const id of ['stlFile','thermalFile'])assert.equal(el(id).disabled,true,id);
assert.equal(wbThermalCompute.disabled,false,'Read-only thermal planning stays available');
cursor=0;el('showCompleted').onclick();assert.equal(cursor,N-1,'Viewing the checked build stays available');
el('wbJobName').value='Blocked build';el('wbJobs').value='saved-build';
const gcode=document.querySelector('#wb a[download="part.gcode"]');
const thermalBefore=[el('wbThermalProfile').value,el('wbThermal').value,sessionStorage.getItem(UI.fields)];
const writes=[
  ['Slice & check',()=>el('sliceBtn').onclick()],
  ['Apply placement',()=>el('placeBtn').onclick()],
  ['Find clear placement',()=>el('correctBtn').onclick()],
  ['Fixture placement',()=>el('wbFixture').onchange()],
  ['Snap placement',()=>el('snapBtn').onclick()],
  ['Cell-profile auto-slice',()=>el('wbCellProfile').onchange()],
  ['Cleaner auto-slice',()=>el('wbHcCleaner').onchange()],
  ['STL upload',()=>{el('stlFile').files=[{name:'blocked.stl'}];return el('stlFile').onchange();}],
  ['Simulation-profile upload',async()=>{
    const upload=el('thermalFile');
    upload.files=[{name:'blocked.json',size:24,text:async()=>'{"layers":[]}'}];
    await upload.onchange({target:upload});
    assert.equal(upload.files.length,0);
    assert.deepEqual(
      [el('wbThermalProfile').value,el('wbThermal').value,sessionStorage.getItem(UI.fields)],
      thermalBefore,
      'Blocked profile upload must not mutate or persist the draft',
    );
  }],
  ['Save',()=>el('saveBtn').onclick()],
  ['Load',()=>el('loadBtn').onclick()],
  ['G-code export',()=>gcode.onclick({preventDefault(){}})],
  ['JBI export',()=>el('emitBtn').onclick()],
  ['ROS export',()=>el('emitRosBtn').onclick()],
];
for(const [name,action] of writes){
  await action();assert.equal(requests.length,before,name+' must refuse before a request');
}
let prevented=false;
window.onkeydown({key:'Enter',code:'Enter',ctrlKey:true,metaKey:false,altKey:false,shiftKey:false,
  repeat:false,target:el('wbLh'),preventDefault(){prevented=true;}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(prevented,true);assert.equal(requests.length,before);
assert.match(el('wbMsg').textContent,/Desktop review is still open/);
assert.equal(el('stlFile').files.length,0);assert.equal(el('stlFile').disabled,true);
assert.equal(window.waamDraft.setBlocked(null),true);
for(const id of ['stlFile','thermalFile'])assert.equal(el(id).disabled,false,id);
await el('sliceBtn').onclick();assert.equal(requests.length,before+1);
""")

    def test_thermal_compute_stays_read_only_and_apply_cannot_mutate_while_blocked(self):
        self.run_js(r"""
unlockAccountControls();
response={ok:true,gen:S.meta.gen,valid:true,rows:[{layer:0,start_c:25,peak_c:210,next_preweld_c:120}],
  layer_dwell_s:44,bead_dwell_s:3,added_time_s:47,max_peak_c:220,
  model:{key:'default',C:1200,tau:80,source:'Default model',notes:[]},notes:[]};
window.waamDraft.setBlocked('Desktop review is still open.');
const before=requests.length;await wbThermalCompute.onclick();
assert.equal(requests.length,before+1,'Thermal planning is a read-only GET');
assert.match(requests.at(-1).url,/\/api\/thermal-plan\?/);
assert.equal(wbThermalCompute.disabled,false);assert.equal(wbThermalApply.disabled,true);
const prior=[wbInterpass.value,wbInterpassEvery.value,wbBeadDwell.value];
wbThermalApply.onclick();
assert.deepEqual([wbInterpass.value,wbInterpassEvery.value,wbBeadDwell.value],prior);
assert.equal(thermalApplied,null);assert.equal(el('thermalPlanNotes').textContent,'Desktop review is still open.');
window.waamDraft.setBlocked(null);assert.equal(wbThermalApply.disabled,false);
wbThermalApply.onclick();
assert.deepEqual([wbInterpass.value,wbInterpassEvery.value,wbBeadDwell.value],['44','1','3']);
""")

    def test_plate_and_reindex_var_dat_wiring_use_the_shared_write_guard(self):
        self.run_js(r"""
window.waamDraft.setBlocked('Desktop review is still open.');
for(const action of [
  ()=>globalThis.plateImport('plate',40,4,'plate-corners'),
  ()=>globalThis.reindexImport('reindex',40,8),
])await assert.rejects(action,/Desktop review is still open/);
assert.equal(requests.length,0);
window.waamDraft.setBlocked(null);response={ok:true,records:[[1,2,3,4,5,6]]};
await globalThis.plateImport('plate',40,1,'plate-corners');
assert.equal(requests.length,1);assert.equal(requests[0].options.method,'POST');
assert.match(requests[0].url,/\/api\/var-dat\?/);
""", setup=r"""
window.WaamSubstrate={create(env){globalThis.plateImport=env.importVarDat;
  return {paint(){},error(){return ''}};}};
window.WaamReindex={create(env){globalThis.reindexImport=env.importVarDat;
  return {paint(){},refresh(){}};}};
""")

    def test_desktop_ready_event_fires_once_after_initialisation(self):
        self.run_js(r"""
const events=[];document.addEventListener('waam:draft-ready',event=>events.push(event.detail));
assert.equal(window.waamDraft.ready,false);markDraftReady();markDraftReady();
assert.equal(window.waamDraft.ready,true);assert.deepEqual(events,[{version:1}]);
""")

    def test_reload_with_unchanged_upload_or_none_source_keeps_actions_ready(self):
        body = r"""
assert.equal(window.waamDraft.error(),'');
unlockAccountControls();
for(const id of ['saveBtn','emitBtn','emitRosBtn'])assert.equal(el(id).disabled,false,id);
assert.equal(readyForRobotExport(),true);
"""
        self.run_js(body, setup=r"""
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'upload',name:S.meta.title,size:42,lastModified:123},_pendingFile:false}));
""")
        self.run_js(body, setup=r"""
S.meta.stl=null;
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'none'},_pendingFile:false}));
""")

    def test_reload_restores_removed_source_instead_of_checked_stl(self):
        self.run_js(r"""
assert.equal(loadedStl,null);assert.equal(el('stlChip').style.display,'none');
assert.match(window.waamDraft.error(),/Settings changed/);
const qs=await sliceQuery();assert.equal(qs.get('kind'),'wall');assert.equal(qs.get('reuse'),'0');
""", setup=r"""
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'none'}}));
""")

    def test_reload_restores_pending_upload_without_reusing_checked_stl(self):
        self.run_js(r"""
assert.equal(loadedStl,null);assert.equal(el('stlName').textContent,'next.stl');
assert.equal(el('stlChip').style.display,'');assert.match(window.waamDraft.error(),/Reselect the pending STL/);
const before=requests.length;await el('sliceBtn').onclick();assert.equal(requests.length,before);
""", setup=r"""
sessionStorage.setItem(UI.fields,JSON.stringify({
  _draftBase:JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]),
  _draftSource:{kind:'upload',name:'next.stl',size:42,lastModified:123},_pendingFile:true}));
""")

    def test_unavailable_engine_and_bad_save_name_are_explained_before_requests(self):
        self.run_js(r"""
S.meta.ornl={configured:false};renderSlicerControls();
assert.equal(document.querySelector('#wbSlicer option[value="ornl"]').disabled,true);
el('wbSlicer').value='ornl';await el('sliceBtn').onclick();assert.equal(requests.length,0);
assert.match(el('wbMsg').textContent,/ORNL is unavailable/);
el('wbSlicer').value='builtin';el('wbJobName').value='Trial (1)';el('wbJobName').dispatch('input');
unlockAccountControls();assert.equal(el('saveBtn').disabled,true);
await el('saveBtn').onclick();assert.equal(requests.length,0);assert.match(el('wbMsg').textContent,/Build name/);
el('wbJobName').value='Trial 1';el('wbJobName').dispatch('input');assert.equal(el('saveBtn').disabled,false);
assert.match(el('buildReadiness').textContent,/illustrative/);
assert.match(el('buildReadiness').textContent,/Whole-arm interference/);
""")

    def test_successful_slice_opens_place_and_preserves_operator_fields(self):
        self.run_js(r"""
switchStep('place');
el('wbArc').value='12';el('wbJbiName').value='WALL_1';el('wbJobName').value='Wall trial';
document.querySelector('aside').scrollTop=123;window.scrollTo(0,456);
response={ok:true,state:structuredClone(S)};
response.state.meta.title='new-slice.stl';
await sliceQuery();
assert.equal(location.reloads,undefined);
assert.equal(S.meta.title,'new-slice.stl');assert.equal(gpu.state,S);assert.equal(gpu.updates,1);
assert.equal(el('wbArc').value,'12');assert.equal(el('wbJbiName').value,'WALL_1');assert.equal(el('wbJobName').value,'Wall trial');
assert.equal(document.querySelector('aside').scrollTop,0);assert.equal(window.scrollY,456);
assert.equal(JSON.parse(sessionStorage.getItem(UI.storage)).step,'place');
window.dispatch('pagehide');
assert.equal(JSON.parse(sessionStorage.getItem(UI.storage)).step,'place',
  'Page teardown must not overwrite the step requested by a successful check');
assert.equal(UI.step,'place');
""")

    def test_completed_build_frames_the_final_checked_move(self):
        self.run_js(r"""
const before=JSON.stringify(S);el('showCompleted').onclick();
assert.equal(cursor,N-1);assert.equal(Number(slider.value),N-1);
assert.equal(playing,false);assert.equal(UI.view,'part');assert.equal(JSON.stringify(S),before);
cursor=0;el('wbLh').value='3.5';el('wbLh').dispatch('input');el('showCompleted').onclick();
assert.equal(cursor,0);assert.match(el('wbMsg').textContent,/Settings changed/);
""")

    def test_hidden_demo_sizes_and_export_fields_never_block_or_ride_a_slice(self):
        self.run_js(r"""
el('clearStl').onclick();
el('wbShape').value='bead';el('wbShape').onchange();el('wbBeadLength').value='';
el('wbShape').value='wall';el('wbShape').onchange();el('wbWallLayers').value='5';
let qs=await sliceQuery();
assert.equal(qs.has('bead_length'),false,'A hidden, empty bead length neither blocks nor rides a wall slice');
el('wbWallLayers').value='';el('wbBeadLength').value='101.6';
el('wbShape').value='bead';el('wbShape').onchange();
assert.equal(el('wallLayersRow').hidden,true);assert.equal(el('beadCountRow').hidden,false);
el('wbInterpass').value='0';el('wbBeadCount').value='4';el('wbBeadPitch').value='25';
qs=await sliceQuery();
assert.equal(qs.get('bead_length'),'101.6');
assert.equal(qs.get('shape'),'bead');assert.equal(qs.get('bead_count'),'4');assert.equal(qs.get('bead_pitch'),'25');
for(const key of ['wall_layers','wall_length','interpass','interpass_every'])assert.equal(qs.has(key),false,key);
el('wbShape').value='wall';el('wbShape').onchange();el('wbWallLayers').value='5';
qs=await sliceQuery();
assert.equal(qs.get('wall_layers'),'5');assert.equal(qs.has('bead_count'),false);
el('wbWallLayers').value='2.5';
const before=requests.length;await el('sliceBtn').onclick();
assert.equal(requests.length,before,'A visible fractional layer count must not slice');
""")

    def test_pending_registration_replaces_old_clearance_verdict_and_blocks_ready(self):
        self.run_js(r"""
S.meta.registration_pending=true;
renderClearance();
assert.equal(el('clearanceBadge').dataset.state,'pending');
assert.equal(el('clearanceBadge').textContent,'Substrate registration pending');
assert.equal(readyForRobotExport(),false);
assert.equal(UI.step,'place');
assert.match(el('wbMsg').textContent,/registration is pending/);
S.meta.registration_pending=false;renderClearance();
assert.equal(el('clearanceBadge').dataset.state,'clear');
""")

    def test_display_download_failure_adopts_successful_slice_and_placement(self):
        self.run_js(r"""
let light=false;
window.WaamAssets={setMode:()=>{light=true;},hydrate:async state=>{
  if(!light)throw new Error('network failed');state.robot.meshes=null;
}};
for(const id of ['sliceBtn','placeBtn']){
  light=false;const old={updates:0,setState(){this.updates++;},dispose(){this.disposed=true;}};gpu=old;
  response={ok:true,state:structuredClone(S)};response.state.meta.title='new-'+id;
  response.state.robot={mesh_asset:'robot-new',meshes:null};
  await el(id).onclick();
  assert.equal(S.meta.title,'new-'+id,'Successful server state must replace the prior preview');
  assert.equal(S.robot.meshes,null);assert.equal(gpu,null);assert.ok(old.disposed);
  assert.equal(UI.busy,false);assert.equal(UI.step,id==='sliceBtn'?'place':'review');
  assert.ok((el('wbMsg').textContent+el('placementStatus').textContent).includes('light preview for the updated build'));
}
""")

    def test_fractional_placement_and_process_values_validate_with_real_bounds(self):
        self.run_js(r"""
switchStep('place');
const values={wbOx:'12.37',wbOy:'-4.91',wbYaw:'7.25',wbOz:'0.02',wbSub:'6.35',
  wbLh:'2.92',wbBead:'8.63',wbTravel:'175.5',wbWire:'1.143',wbTilt:'12.25',wbSeam:'25.25',wbMargin:'5.05'};
for(const [id,value] of Object.entries(values))el(id).value=value;
assert.equal(validateFields(Object.keys(values).map(el),el('wbMsg')),true,
  'Dragging, corrected placement, and saved process values may have fractional units');
const qs=await sliceQuery();
assert.equal(qs.get('ox'),'12.37');assert.equal(qs.get('oy'),'-4.91');
assert.equal(qs.get('yaw'),'7.25');assert.equal(qs.get('lh'),'2.92');
el('wbOz').value='-0.01';
const requestCount=requests.length;await el('sliceBtn').onclick();
assert.equal(requests.length,requestCount,'Invalid input must not make a request');
assert.equal(document.activeElement,el('wbOz'));assert.equal(el('wbOz').reported,true);
el('wbTilt').value='360.01';assert.equal(validateFields([el('wbTilt')],el('wbMsg')),false,'Keep shared maximum bounds');
el('wbArc').value='1.5';assert.equal(validateFields([el('wbArc')],el('wbMsg')),false,'ASF remains an integer');
""")

    def test_validation_reopens_the_hidden_inspector_before_focusing(self):
        self.run_js(r"""
let shown=0;window.waamWindow={showInspector:()=>shown++};
switchStep('review');const field=el('wbOz');revealField(field);
assert.equal(shown,1);assert.equal(UI.step,'place');assert.equal(document.activeElement,field);
assert.equal(field.reported,true);
""")

    def test_real_placement_handler_keeps_place_and_recovers_failed_undo(self):
        self.run_js(placement_script() + r"""
for(const e of elements)e.dataset=Object.fromEntries(Object.entries(e.attrs)
 .filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));
S.meta.params={ox:'0',oy:'0',yaw:'0'};
S.meta.placement={origin:[1014,-180,20],bottom_z:0,pivot:[50,10],center_offsets:[20,40]};
window.waamPlacement=module.exports.WaamPlacement.create({document,canvas,state:()=>S,step:()=>UI.step,
 validate:numberError,
 busy:()=>UI.busy,project:proj,dpr:()=>1,draw,pause:()=>setPlaying(false),clearance:renderClearance,
 apply:()=>el('placeBtn').onclick()});
switchStep('place');el('wbOx').value='100';
response={ok:true,state:structuredClone(S)};
response.state.meta.params.ox='10.123456789';response.state.meta.placement.origin[0]+=10.123456789;
assert.equal(await el('placeBtn').onclick(),true);
assert.equal(UI.step,'place');assert.equal(el('wbOx').value,'10.123456789');
assert.equal(el('placeUndo').disabled,false);assert.equal(window.waamPlacement.preview(),null);
el('wbSub').value='-1';const prior=requests.length;
await window.waamPlacement.history('undo');assert.equal(requests.length,prior);
assert.equal(el('wbOx').value,'10.123456789');
el('wbSub').value='6.35';el('wbOx').value='20';
response={ok:true,state:structuredClone(S)};response.state.meta.params.ox='20';
assert.equal(await el('placeBtn').onclick(),true);
assert.equal(el('placeUndo').disabled,false);assert.equal(el('placeRedo').disabled,true);
response={ok:false,error:'deliberate refusal'};el('wbOx').value='25';
assert.equal(await el('placeBtn').onclick(),false);assert.equal(el('wbOx').value,'25');
assert.equal(UI.busy,false);assert.equal(UI.step,'place');
assert.equal(el('placeUndo').disabled,true);assert.equal(window.waamPlacement.isApplying(),false);
el('wbOx').value='';el('wbOx').dispatch('input');assert.equal(el('wbOx').checkValidity(),false);
window.waamPlacement.revert();assert.equal(el('wbOx').value,'20');
assert.equal(el('wbOx').checkValidity(),true);assert.equal(el('wbOx').getAttribute('aria-invalid'),'false');
""")

    def test_desktop_block_refuses_placement_toolbar_mutations_then_reenables_them(self):
        self.run_js(placement_script() + r"""
for(const e of elements)e.dataset=Object.fromEntries(Object.entries(e.attrs)
 .filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));
S.meta.params={ox:'0',oy:'0',yaw:'0',rotX:'0',rotY:'0',lay_flat:'0',tilt:'0'};
S.meta.placement={origin:[1014,-180,20],bottom_z:0,pivot:[50,10],center_offsets:[20,40]};
window.waamPlacement=module.exports.WaamPlacement.create({document,canvas,state:()=>S,step:()=>UI.step,
 validate:numberError,busy:()=>UI.busy,blocked:()=>window.waamDraft.blocked(),
 project:proj,dpr:()=>1,draw,pause:()=>setPlaying(false),clearance:renderClearance,
 apply:()=>el('placeBtn').onclick()});
switchStep('place');window.waamDraft.setBlocked('Desktop review is still open.');
const nudge=document.querySelector('[data-place-nudge]'),before=el('wbOx').value,count=requests.length;
assert.equal(nudge.disabled,true);assert.equal(el('placeCenter').disabled,true);
nudge.onclick();el('placeCenter').onclick();el('placeLayFlat').onclick();el('placeResetOrientation').onclick();
assert.equal(el('wbOx').value,before);assert.equal(requests.length,count);
assert.equal(el('placementStatus').textContent,'Desktop review is still open.');
window.waamDraft.setBlocked(null);assert.equal(nudge.disabled,false);
response={ok:true,state:structuredClone(S)};response.state.meta.params.ox='-1';
nudge.onclick();await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.length,count+1);
""")

    def test_slice_and_place_submit_face_settings_and_reset_absent_defaults(self):
        self.run_js(r"""
el('wbRotX').value='70';el('wbRotY').value='25';el('wbLayFlat').value='1';
let qs=await sliceQuery();
assert.equal(qs.get('rotX'),'70');assert.equal(qs.get('rotY'),'25');assert.equal(qs.get('lay_flat'),'1');
await el('placeBtn').onclick();qs=new URL(requests.at(-1).url,'http://studio.test').searchParams;
assert.equal(qs.get('rotX'),'70');assert.equal(qs.get('rotY'),'25');assert.equal(qs.get('lay_flat'),'1');
const prior=requests.length;el('wbRotY').value='361';await el('placeBtn').onclick();assert.equal(requests.length,prior);
el('wbRotY').value='25';response={ok:true,state:structuredClone(S)};
response.state.meta.params={rotX:'70.0',rotY:'25.0',lay_flat:'1'};
await el('placeBtn').onclick();assert.equal(el('wbRotX').value,'70.0');
response={ok:true,state:structuredClone(S)};response.state.meta.params={};
await el('placeBtn').onclick();assert.equal(el('wbRotX').value,'0');assert.equal(el('wbRotY').value,'0');
assert.equal(el('wbLayFlat').value,'0');
""")

    def test_camera_drag_owns_pointer_before_placement_can_start(self):

        bindings = _TEMPLATE.split('canvas.onpointerdown=e=>{', 1)[1].split('canvas.onwheel=', 1)[0]
        self.run_js('canvas.onpointerdown=e=>{' + bindings + r"""
let placementStarts=0;window.waamPlacement={down:()=>{placementStarts++;return false;},
 move:()=>false,up:()=>false,cancel(){}};
canvas.setPointerCapture=()=>{};
canvas.onpointerdown({button:2,pointerId:1,clientX:100,clientY:100,preventDefault(){}});
assert.equal(placementStarts,1);
canvas.onpointerdown({button:0,pointerId:2,clientX:200,clientY:100});
assert.equal(placementStarts,1,'A camera gesture must exclude other pointer gestures');
canvas.onpointerup({pointerId:2});assert.ok(drag);
canvas.onpointerup({pointerId:1});assert.equal(drag,null);
""")

    def test_space_cannot_play_a_valid_or_invalid_placement_draft(self):
        playback = 'function setPlaying(p)' + _block_after(_TEMPLATE, r'function setPlaying\(p\)')
        playback += 'function togglePlayback()' + _block_after(_TEMPLATE, r'function togglePlayback\(\)')
        keyboard = 'window.onkeydown=e=>' + _block_after(_TEMPLATE, r'window.onkeydown=e=>') + ';'
        self.run_js(playback + keyboard + r"""
const playBtn=el('play');let selectedCollision=null;
let pending=true;window.waamPlacement={pending:()=>pending};
playing=false;
window.onkeydown({code:'Space',target:{tagName:'CANVAS'},preventDefault(){}});
assert.equal(playing,false,'Both valid and invalid drafts use the pending guard');
pending=false;UI.busy=true;
window.onkeydown({code:'Space',target:{tagName:'CANVAS'},preventDefault(){}});
assert.equal(playing,false,'A busy check also owns playback');
UI.busy=false;
window.onkeydown({code:'Space',target:{tagName:'CANVAS'},preventDefault(){}});
assert.equal(playing,true);
""")

    def test_settings_modal_owns_keyboard_and_reduced_motion_steps_layers(self):
        playback = 'function setPlaying(p)' + _block_after(_TEMPLATE, r'function setPlaying\(p\)')
        playback += 'function togglePlayback()' + _block_after(_TEMPLATE, r'function togglePlayback\(\)')
        keyboard = 'window.onkeydown=e=>' + _block_after(_TEMPLATE, r'window.onkeydown=e=>') + ';'
        self.run_js(playback + keyboard + r"""
const playBtn=el('play');let selectedCollision=null,settingsOpen=true,pending=false;
window.waamSettings={isOpen:()=>settingsOpen,reducedMotion:()=>true};
window.waamPlacement={pending:()=>pending};
M=[{l:0},{l:0},{l:1},{l:1},{l:2}];N=M.length;cursor=0;playing=false;
const space=()=>window.onkeydown({code:'Space',target:{tagName:'CANVAS'},preventDefault(){}});
space();assert.equal(cursor,0,'Settings must own keyboard shortcuts');
settingsOpen=false;space();assert.equal(cursor,2);assert.equal(playing,false);
assert.equal(playBtn.getAttribute('aria-label'),'Next layer');
space();assert.equal(cursor,4);space();assert.equal(cursor,0);
pending=true;space();assert.equal(cursor,0,'A placement draft still owns playback');
""")

    def test_enter_slices_from_prepare_and_backslash_toggles_the_panel(self):
        keyboard = 'window.onkeydown=e=>' + _block_after(_TEMPLATE, r'window.onkeydown=e=>') + ';'
        self.run_js(keyboard + r"""
let slices=0,toggles=0;el('sliceBtn').onclick=()=>slices++;
window.waamWindow={toggleInspector:()=>toggles++};
const key=(extra)=>{const e={key:'',code:'',target:{tagName:'CANVAS'},prevented:false,preventDefault(){this.prevented=true;},...extra};window.onkeydown(e);return e;};
UI.step='prepare';S.meta.workbench=true;
const field=el('wbLh');field.tagName='INPUT';el('wbSlicer').tagName='SELECT';
assert.ok(key({key:'Enter',target:field}).prevented);assert.equal(slices,1,'Enter in a Prepare number field slices');
key({key:'Enter',ctrlKey:true});assert.equal(slices,2,'Ctrl+Enter slices from the preview');
key({key:'Enter',target:field,repeat:true});assert.equal(slices,2,'A held key does not queue slices');
key({key:'Enter',target:el('wbSlicer')});assert.equal(slices,2,'Enter on a select keeps its own meaning');
el('sliceBtn').disabled=true;key({key:'Enter',ctrlKey:true});assert.equal(slices,2,'A disabled button stays disabled');
el('sliceBtn').disabled=false;UI.step='review';key({key:'Enter',ctrlKey:true});assert.equal(slices,2,'Only Prepare slices');
key({code:'Backslash'});assert.equal(toggles,1);
key({code:'Backslash',target:field});assert.equal(toggles,1,'Typing in a field never hides the panel');
""")

    def preference_hooks(self):
        return (_TEMPLATE.split('let studioInitialView=null,studioPathLayers=null;', 1)[1]
                .split('initializeStudioUI();', 1)[0])

    def test_appearance_preserves_build_view_and_path_only_restores_layers(self):
        self.run_js('let studioInitialView=null,studioPathLayers=null,studioResumesView=false;' +
                    self.preference_hooks() + r"""
switchStep('review');cursor=6;yaw=.4;panCamera(25,30);
const prefs={theme:'graphite',density:'comfortable',reduce_motion:false,
  default_frame:'cell',default_camera:'iso',path_only:false};
const state=JSON.stringify(S),view=studioViewSignature();
applyStudioPreferences({...prefs,theme:'drafting',density:'compact'},prefs,{});
assert.equal(studioViewSignature(),view);assert.equal(JSON.stringify(S),state);
el('showWalls').checked=true;el('showTorch').checked=false;
const layers=[...document.querySelectorAll('.layers-body input')].map(el=>el.checked);
applyStudioPreferences({...prefs,path_only:true},prefs,{});
assert.equal(el('showPart').checked,false);assert.equal(el('showScene').checked,false);
applyStudioPreferences(prefs,{...prefs,path_only:true},{});
assert.deepEqual([...document.querySelectorAll('.layers-body input')].map(el=>el.checked),layers);
assert.equal(JSON.stringify(S),state,'Presentation must never change engineering state');
""")

    def test_personal_defaults_do_not_override_restored_or_already_changed_view(self):
        self.run_js('let studioInitialView=null,studioPathLayers=null,studioResumesView=true;' +
                    self.preference_hooks() + r"""
const prefs={theme:'graphite',density:'comfortable',reduce_motion:false,
  default_frame:'part',default_camera:'top',path_only:true};
studioInitialView=studioViewSignature();
applyStudioPreferences(prefs,prefs,{initial:true});
assert.equal(studioViewSignature(),studioInitialView,'Restored tabs take priority');
studioResumesView=false;panCamera(20,30);
const navigated=studioViewSignature();
UI.preferencesPending=true;
applyStudioPreferences(prefs,prefs,{initial:true});
assert.equal(studioViewSignature(),navigated,'Slow preferences must not overwrite navigation');
assert.equal(JSON.parse(sessionStorage.getItem(UI.storage)).preferencesPending,false,
  'A successful in-page Retry clears the pending marker without another identity request');
studioInitialView=studioViewSignature();
applyStudioPreferences(prefs,prefs,{initial:true});
assert.equal(UI.view,'part');assert.equal(pitch,Math.PI/2);assert.equal(el('showPart').checked,false);
""")

    def test_shared_workspace_view_cache_belongs_to_the_verified_account(self):
        self.run_js('let studioInitialView=null,studioPathLayers=null,studioResumesView=true,studioStoredViewOwner="alice";' +
                    self.preference_hooks() + r"""
let identities=[],ready=true;window.waamSettings={setIdentity:async j=>{identities.push(j);return ready;}};
el('showPart').checked=false;studioInitialView=studioViewSignature();
await setStudioIdentity({ok:true,sso:true,library_owner:'alice'});
assert.equal(studioResumesView,true);assert.equal(el('showPart').checked,false);
ready=false;await setStudioIdentity({ok:true,sso:true,library_owner:'bob'});
assert.equal(studioResumesView,false);assert.equal(el('showPart').checked,true);
const pendingCache=JSON.parse(sessionStorage.getItem(UI.storage));
assert.equal(pendingCache.owner,'bob');assert.equal(pendingCache.preferencesPending,true,
  'A failed load must not mark the previous account camera as a completed personal view');
ready=true;await setStudioIdentity({ok:true,sso:true,library_owner:'bob'});
assert.equal(JSON.parse(sessionStorage.getItem(UI.storage)).preferencesPending,false);
studioResumesView=true;location.hash='#m=6';el('showPart').checked=false;
studioInitialView=studioViewSignature();
await setStudioIdentity({ok:true,sso:true,library_owner:'carol'});
assert.equal(studioResumesView,true);assert.equal(el('showPart').checked,false,'Explicit view links win');
location.hash='';studioInitialView=studioViewSignature();panCamera(30,30);
await setStudioIdentity({ok:true,sso:true,library_owner:'dan'});
assert.equal(studioResumesView,true,'Do not overwrite navigation performed during identity loading');
""")

    def test_camera_world_pan_stays_the_orbit_center_and_survives_resize(self):
        bindings = _TEMPLATE.split('canvas.onpointerdown=e=>{', 1)[1].split('window.onkeydown=', 1)[0]
        self.run_js('canvas.onpointerdown=e=>{' + bindings + r"""
canvas.setPointerCapture=()=>{};
for(const mode of [false,true])for(const dpr of [1,2,3]){
  perspective=mode;dolly=0;frameView('part');window.devicePixelRatio=dpr;
  resizeStudioView();
  const old=camera(),p={x:cx,y:cy,z:cz},before=proj(p);
  canvas.onpointerdown({button:mode?1:0,shiftKey:!mode,pointerId:1,clientX:10,clientY:20,preventDefault(){}});
  canvas.onpointermove({pointerId:1,clientX:70,clientY:45});
  canvas.onpointerup({pointerId:1});
  const after=proj(p),target=[cx,cy,cz];
  assert.ok(Math.abs(after[0]-before[0]-60*dpr)<1e-6);
  assert.ok(Math.abs(after[1]-before[1]-25*dpr)<1e-6);
  assert.equal(panX,0);assert.equal(panY,0);
  assert.notDeepEqual(target,old.center);
  canvas.onpointerdown({button:0,pointerId:2,clientX:10,clientY:20,preventDefault(){}});
  canvas.onpointermove({pointerId:2,clientX:30,clientY:40});
  canvas.onpointerup({pointerId:2});
  assert.deepEqual([cx,cy,cz],target,'Orbit must keep the translated target');
  assert.notEqual(yaw,old.yaw);
  const snapshot=camera(),eye=cameraEyeDistance();el('view').clientWidth=500;resizeStudioView();
  assert.deepEqual(camera(),snapshot,'Resize must not undo free navigation');
  assert.equal(cameraEyeDistance(),eye,'Aspect changes must not move the eye');
}
""")

    def test_perspective_wheel_crosses_target_and_fine_travel_is_reversible(self):
        binding = 'canvas.onwheel=e=>' + _block_after(_TEMPLATE, r'canvas.onwheel=e=>') + ';'
        self.run_js(binding + r"""
const wheel=(deltaY,shiftKey=false,deltaMode=0)=>canvas.onwheel({deltaY,shiftKey,deltaMode,preventDefault(){}});
const initialZoom=zoom;wheel(-100);assert.equal(zoom,initialZoom*1.12);
assert.equal(dolly,0);assert.equal(perspective,false);
el('projectionToggle').onclick();
assert.equal(perspective,true);assert.equal(el('projectionToggle').textContent,'Persp');
assert.equal(el('projectionToggle').getAttribute('aria-pressed'),'true');
const eye=cameraEyeDistance(),target=[cx,cy,cz],focalZoom=zoom;
wheel(-100);const fullStep=dolly;wheel(100);assert.ok(Math.abs(dolly)<1e-9);
wheel(-100,true);assert.ok(Math.abs(dolly-fullStep/5)<1e-9);
wheel(100,true);wheel(-1,false,1);assert.ok(Math.abs(dolly-eye*.016)<1e-9);
dolly=0;for(let i=0;i<12;i++)wheel(-100);
assert.ok(dolly>eye,'Travel must pass through the target');
assert.equal(zoom,focalZoom);assert.deepEqual([cx,cy,cz],target);
assert.equal(proj({x:cx,y:cy,z:cz})[2],0,'Points behind the eye are hidden');
const saved=JSON.parse(sessionStorage.getItem(UI.storage));
assert.equal(saved.dolly,dolly);assert.equal(saved.perspective,true);
UI.busy=true;const before=camera();wheel(-100);el('projectionToggle').onclick();assert.deepEqual(camera(),before);
UI.busy=false;el('fitView').onclick();assert.equal(dolly,0);assert.equal(perspective,true);
assert.ok(proj({x:cx,y:cy,z:cz})[2]>0);
""")

    def test_camera_round_trip_storage_toggle_and_legacy_pan(self):
        self.run_js(r"""
frameView('part');el('projectionToggle').onclick();panCamera(73,-49);dolly=cameraEyeDistance()*1.2;
const expected=camera();rememberView();
perspective=false;dolly=0;[cx,cy,cz]=cellCenter;
initializeStudioUI();assert.deepEqual(camera(),expected);
assert.equal(el('projectionToggle').getAttribute('aria-pressed'),'true');
el('projectionToggle').onclick();assert.equal(perspective,false);
restoreCamera(expected);assert.deepEqual(camera(),expected);
assert.equal(el('projectionToggle').textContent,'Persp');
frameView('cell');assert.deepEqual([cx,cy,cz],cellCenter);assert.equal(dolly,0);

sessionStorage.setItem(UI.storage,JSON.stringify({title:S.meta.title,view:'cell',yaw,pitch,zoom,
  panX:.1,panY:-.2}));
initializeStudioUI();assert.equal(perspective,false);
const p=proj({x:cellCenter[0],y:cellCenter[1],z:cellCenter[2]});
assert.ok(Math.abs(p[0]-canvas.width*.6)<1e-6);
assert.ok(Math.abs(p[1]-canvas.height*.3)<1e-6);
assert.equal(panX,0);assert.equal(panY,0);
""")

    def test_demo_selection_and_stl_removal_do_not_reuse_previous_mesh(self):
        self.run_js(r"""
let qs=await sliceQuery();
assert.equal(qs.get('kind'),'stl');assert.equal(qs.get('reuse'),'1');
el('wbShape').dispatch('change');
assert.equal(loadedStl,null);assert.equal(el('stlChip').style.display,'none');
qs=await sliceQuery();
assert.equal(qs.get('kind'),'wall');assert.equal(qs.get('reuse'),'0');
assert.equal(qs.get('name'),wbShape.options[wbShape.selectedIndex].text);assert.equal(requests.at(-1).options.body,null);
const bytes=new Uint8Array([1,2,3]).buffer;
el('stlFile').files=[{name:'replacement.stl',arrayBuffer:async()=>bytes}];
el('stlFile').dispatch('change');
assert.equal(el('stlChip').style.display,'');assert.equal(el('stlName').textContent,'replacement.stl');
qs=await sliceQuery();
assert.equal(qs.get('kind'),'stl');assert.equal(qs.get('reuse'),'0');
assert.equal(qs.get('name'),'replacement.stl');assert.equal(requests.at(-1).options.body,bytes);
el('clearStl').onclick();
assert.equal(el('stlFile').files.length,0);assert.equal(el('stlUnitsRow').hidden,true);
qs=await sliceQuery();
assert.equal(qs.get('kind'),'wall');assert.equal(qs.get('reuse'),'0');
assert.equal(requests.at(-1).options.body,null);
assert.match(el('sourceInfo').textContent,/Demo selected/);
""")

    def test_busy_failure_recovery_keeps_guest_controls_locked(self):
        self.run_js(r"""
const account=['saveBtn','loadBtn','emitBtn','emitRosBtn'];
IS_GUEST=true;
lockGuestControls();beginWb();
assert.equal(playing,false);assert.equal(el('sliceBtn').disabled,true);
endWb();
for(const id of account)assert.equal(el(id).disabled,true,'Initially locked '+id);
assert.equal(el('sliceBtn').disabled,false);assert.equal(UI.busy,false);

IS_GUEST=false;
unlockAccountControls();
S.violations=[{blocking:true,obstacle:'bead(line 7)',move:1,clearance:-5}];
updateCorrectionControls();beginWb();
IS_GUEST=true;
lockGuestControls();endWb();
for(const id of account)assert.equal(el(id).disabled,true,'Late guest lock '+id);
assert.equal(el('correctBtn').disabled,true,'Do not unlock a previously disabled action');
assert.equal(el('sliceBtn').disabled,false);assert.equal(el('busy').hidden,true);
assert.equal(el('wb').attrs['aria-busy'],'false');assert.deepEqual(wbDisabled,[]);
""")

    def test_inactive_ornl_numbers_cannot_silently_block_builtin_slice(self):
        self.run_js(r"""
el('wbSlicer').value='builtin';el('wbContours').value='0';el('wbStepover').value='';
const qs=await sliceQuery();assert.equal(qs.get('slicer'),'builtin');
S.meta.ornl={configured:true};
el('wbSlicer').value='ornl';const before=requests.length;
await el('sliceBtn').onclick();assert.equal(requests.length,before);
assert.ok(el('sliceStatus').textContent.length>0,'Validation must be visible near Slice');
assert.equal(el('sliceStatus').getAttribute('role'),'alert');
el('wbContours').value='2';el('wbStepover').value='75';el('wbSeam').value='';
const ornl=await sliceQuery();assert.equal(ornl.get('slicer'),'ornl');
""")

    def test_load_rejects_overlapping_slice_and_placement_and_releases_on_failure(self):
        self.run_js(r"""
unlockAccountControls();el('wbJobs').value='saved-build';
let release;
fetch=(url,options)=>{
  requests.push({url,options});
  return new Promise(resolve=>{release=()=>resolve({ok:false,status:403,json:async()=>({error:'Load refused'})});});
};
const loading=el('loadBtn').onclick();
assert.equal(UI.busy,true);assert.equal(wbBusy,true);
await el('sliceBtn').onclick();await applyPlacement();
assert.equal(requests.length,1,'A load must own the same mutation guard');
assert.equal(el('stlFile').disabled,true);assert.equal(el('emitBtn').disabled,true);
release();await loading;
assert.equal(UI.busy,false);assert.equal(wbBusy,false);assert.equal(el('sliceBtn').disabled,false);
assert.equal(el('emitBtn').disabled,false);assert.match(el('wbMsg').textContent,/Load refused/);
""")

    def test_successful_load_retires_schedule_without_copying_params_or_pagehide_resave(self):
        self.run_js(r"""
unlockAccountControls();el('wbJobs').value='other-build';
el('wbLh').value='4.2';el('wbArcSchedule').value='0:81;1-:83';rememberWb();
response={ok:true,params:{lh:'9.9',arc_schedule:'0:99'}};
await el('loadBtn').onclick();assert.equal(location.reloads,1);
assert.equal(el('wbArcSchedule').value,'');
let saved=JSON.parse(sessionStorage.getItem(UI.fields));
assert.equal(saved.fields.wbArcSchedule,'');assert.equal(saved.fields.wbLh,'4.2');
window.dispatch('pagehide');saved=JSON.parse(sessionStorage.getItem(UI.fields));
assert.equal(saved.fields.wbArcSchedule,'','The departing part must not restore its schedule');
assert.equal(saved.fields.wbLh,'4.2','Loaded params come from the reloaded state, not session copying');
""")

    def test_placement_checks_the_selected_source_instead_of_reverting_it(self):
        self.run_js(r"""
el('clearStl').onclick();
response={ok:true,state:structuredClone(S)};
response.state.meta.stl=null;
await applyPlacement();
assert.match(requests.at(-1).url,/\/api\/slice\?/);
assert.equal(loadedStl,null);assert.equal(el('stlChip').style.display,'none');
assert.match(el('sourceInfo').textContent,/Demo selected/);
const file={name:'pending.stl',arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer};
el('stlFile').files=[file];el('stlFile').dispatch('change');
response={ok:false,error:'source failed'};await applyPlacement();
assert.equal(el('stlFile').files[0],file);assert.equal(loadedStl,null);
assert.match(el('sourceInfo').textContent,/pending.stl.*ready to slice/);
response={ok:true,state:structuredClone(S)};response.state.meta.title=file.name;
response.state.meta.stl={resolved_units:'mm',size_mm:[10,10,10]};
await sliceQuery();
assert.equal(el('stlFile').files.length,0);assert.equal(loadedStl,'pending.stl');
""")

    def test_late_thermal_replies_cannot_replace_the_current_build_plan(self):
        self.run_js(r"""
S.meta.gen=1;
Object.assign(el('wbThermalTarget'),{value:'300'});Object.assign(el('wbThermalPeak'),{value:'600'});
Object.assign(el('wbThermalAmbient'),{value:'20'});el('wbThermalUnit').value='C';el('wbThermalModel').value='first-model';
const pending=[];
fetch=url=>new Promise(resolve=>{requests.push({url});pending.push({url,resolve});});
const result=(gen,dwell,key)=>({ok:true,gen,rows:[],layer_dwell_s:dwell,bead_dwell_s:2,added_time_s:4,
  max_peak_c:650,valid:true,model:{key,C:12,tau:34,source:'test',notes:[]},notes:[]});
const first=el('wbThermalCompute').onclick();
S.meta.gen=2;el('wbThermalTarget').value='350';el('wbThermalPeak').value='650';el('wbThermalModel').value='captured-model';
const second=el('wbThermalCompute').onclick();
assert.equal(new URL(pending[0].url,'http://studio.test').searchParams.get('gen'),'1');
assert.equal(new URL(pending[1].url,'http://studio.test').searchParams.get('gen'),'2');
pending[1].resolve({ok:true,status:200,json:async()=>result(2,22,'server-model')});await second;
assert.equal(thermalPlan.layer_dwell_s,22);assert.equal(thermalPlanInputs.target,350);
assert.equal(thermalPlanInputs.peak,650);assert.equal(thermalPlanInputs.model,'server-model');
pending[0].resolve({ok:true,status:200,json:async()=>result(1,11,'old-model')});await first;
assert.equal(thermalPlan.layer_dwell_s,22,'The late plan must not replace the accepted reply');
el('wbThermalApply').onclick();const trace=new URLSearchParams(thermalTraceQuery().slice(1));
assert.equal(trace.get('thermal_target'),'350');assert.equal(trace.get('thermal_peak'),'650');
assert.equal(trace.get('thermal_model'),'server-model');
const mismatched=el('wbThermalCompute').onclick();
pending[2].resolve({ok:true,status:200,json:async()=>result(1,99,'stale-build')});await mismatched;
assert.equal(thermalPlan,null);assert.match(el('thermalPlanSummary').textContent,/checked build changed/);
""")

    def test_layer_touch_request_rechecks_the_draft_after_click(self):
        module = ROOT / "src/waam_pipeline/studio_layer_sense.js"
        self.run_js(r"""
assert.equal(el('wbLayerSenseJobBtn').disabled,false);
const name=el('wbLayerSenseName');let value=name.value;
Object.defineProperty(name,'value',{configurable:true,get(){el('wbLh').value='3.5';return value;},set(next){value=String(next);}});
const before=requests.length;await el('wbLayerSenseJobBtn').onclick();
assert.equal(requests.length,before);assert.match(el('layerSenseStatus').textContent,/Settings changed/);
assert.equal(wbBusy,false);
""", setup=("S.meta.params={cell_profile:'lc50s'};"
                    "window.WaamLayerSense=require(" + json.dumps(str(module)) + ").WaamLayerSense;"))

    def test_emit_reports_all_known_objections_before_attestation(self):
        self.run_js(r"""
S.meta.clearance_checked=false;
el('wbJbiName').value='bad.name';el('wbArc').value='';
await el('emitBtn').onclick();
const message=el('wbMsg').textContent;
for(const reason of ['Controller job name','ASF#','Enable the cell view','Clearance has not been checked'])assert.ok(message.includes(reason),reason);
assert.equal(requests.length,0);assert.equal(UI.step,'review');
el('wbJbiName').value='wall_1';
await el('emitRosBtn').onclick();
assert.equal(el('wbJbiName').value,'WALL_1');
assert.match(el('wbMsg').textContent,/Enable the cell view/);
assert.doesNotMatch(el('wbMsg').textContent,/ASF#/);
assert.equal(requests.length,0);
""")

    def test_an_asf_schedule_replaces_the_single_number(self):
        self.run_js(r"""
el('wbArc').value='';el('wbArcSchedule').value='0:81; 1-:083';
await el('emitBtn').onclick();
assert.doesNotMatch(el('wbMsg').textContent,/ASF#/);
assert.deepEqual(scheduleAsfs(arcSchedule()),['81','83']);
assert.equal(arcQuery(),'arc_schedule='+encodeURIComponent('0:81; 1-:083'));
el('wbArc').value='12';
await el('emitBtn').onclick();
assert.match(el('wbMsg').textContent,/either one ASF# or an ASF# by layer schedule, not both/);
el('wbArcSchedule').value='';
assert.equal(arcQuery(),'arc=12');
const headers=new Map([['X-Waam-Pass-Count','3'],['X-Waam-Asf','81,83'],['X-Waam-Travel','198-402']]);
assert.equal(passSummary({headers:{get:k=>headers.get(k)??null}}),' 3 passes · ASF#81,83 · travel 198–402 mm/min.');
assert.equal(requests.length,0);
el('wbArcSchedule').value='0:81;1-:83';el('wbShape').onchange();
assert.equal(el('wbArcSchedule').value,'','a new preset clears the layer schedule');
el('wbArcSchedule').value='0:81;1-:83';el('stlFile').onchange();
assert.equal(el('wbArcSchedule').value,'','a new STL clears the layer schedule');
""")

    def test_changed_part_stays_centered_when_viewport_resizes(self):
        self.run_js(r"""
function assertCentered(){
  const p=proj({x:pcx,y:pcy,z:pcz});
  assert.ok(Math.abs(p[0]-canvas.width/2)<1e-8,'Part center must be at the viewport horizontal center');
  assert.ok(Math.abs(p[1]-canvas.height/2)<1e-8,'Part center must be at the viewport vertical center');
}
el('partView').onclick();
assertCentered();
cursor=61;rememberView();

S.meta.title='small-cube.stl';pcx=25;pcy=-40;pcz=12;partZoom=26.5;
initializeStudioUI();
assert.equal(UI.view,'part');assert.equal(cursor,0);assert.equal(Number(slider.value),0);
assert.equal(zoom,partZoom);assertCentered();
assert.equal(JSON.parse(sessionStorage.getItem(UI.storage)).title,'small-cube.stl');
for(const [width,height,dpr] of [[390,470,3],[700,360,2],[1200,800,1]]){
  el('view').clientWidth=width;el('view').clientHeight=height;window.devicePixelRatio=dpr;
  const before=draws;window.onresize();
  assert.equal(canvas.width,width*dpr);assert.equal(canvas.height,height*dpr);
  assert.equal(UI.view,'part');assert.equal(zoom,partZoom);assertCentered();
  assert.equal(draws,before+1,'Redraw after the backing dimensions and camera have been updated');
}
""")


class TestStudioControlSource(unittest.TestCase):


    def test_shared_workbench_guard_drives_the_workflow_busy_overlay(self):
        begin = _block_after(_TEMPLATE, r"function beginWb\(")
        end = _block_after(_TEMPLATE, r"function endWb\(")
        self.assertIn('setBusy("Updating build…");', begin)
        self.assertIn('setBusy(null);', end)
        self.assertIn('updateCorrectionControls();', end)
        self.assertNotIn('UI.disabled', controls_script())
        self.assertNotIn('placementBusy', _TEMPLATE)

    def test_only_workbench_source_selection_controls_the_chip_and_upload(self):
        source = _block_after(controls_script(), r"function sourceSelection\(")
        self.assertNotIn('S.part', source)
        self.assertNotIn('stlFile', source)
        self.assertIn('sourceSelection(name,!!f);', _TEMPLATE)
        init = _block_after(controls_script(), r"function initializeStudioUI\(")
        self.assertNotIn('stlFile', init)
        self.assertNotIn('useDemo', init)

    def test_node_harness_projects_the_served_shell_and_current_handlers(self):
        TestStudioControls.setUpClass()
        self.assertIn('min', TestStudioControls.shell)
        self.assertIn('data-error', TestStudioControls.shell)
        self.assertNotIn('%%wb', TestStudioControls.shell)
        for button in ('sliceBtn', 'loadBtn', 'emitBtn', 'emitRosBtn'):
            self.assertIn('document.getElementById("' + button + '").onclick',
                          TestStudioControls.handlers)
        self.assertIn('renderWorkbench(j.state);', TestStudioControls.handlers)
        self.assertIn('function renderState(next)', TestStudioControls.handlers)


if __name__ == "__main__":
    unittest.main()
