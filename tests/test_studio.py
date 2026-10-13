import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest import mock
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.clearance import Scene, check
from waam_pipeline.schedule import Schedule
from waam_pipeline.slicer import wall
from waam_pipeline.server import _ADMIN_PAGE, _state, run_slice
from waam_pipeline import studio
from waam_pipeline.studio import build_state, render_studio, write_studio


class _StudioElements(HTMLParser):


    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.workbench = False
        self.stack = []
        self.select = self.option = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "wb":
            self.workbench = True
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs.get("id")))
        element = {"tag": tag, "attrs": attrs, "workbench": self.workbench}
        self.elements.append(element)
        if tag == "select":
            self.select = element
            element["options"] = []
        elif tag == "option" and self.select is not None:
            self.option = element
            element["text"] = ""
            self.select["options"].append(element)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        self.workbench = any(element_id == "wb" for _, element_id in self.stack)
        if tag == "select":
            self.select = None
        elif tag == "option":
            self.option = None

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data


def run_studio_node(page, scenario, base_url):


    markup, script = page.split("<script data-app>", 1)
    script = script.split("</script>", 1)[0]
    fixture = {"elements": _StudioElements(markup).elements, "url": base_url + "/"}
    setup = r"""
const http=require('http');
globalThis.window=globalThis;
window.addEventListener=()=>{};
globalThis.location={href:fixture.url,search:'',reload(){throw new Error('unexpected reload');}};


function mockDOM(target,name){
  return new Proxy(target,{get(target,key,receiver){
    if(typeof key==='string' && !(key in target) && !key.startsWith('on') && key!=='toJSON'){
      const api=name+'.'+key;
      console.error('NODE_DOM_MOCK_MISSING:'+api);
      throw new TypeError('node DOM mock missing '+api);
    }
    return Reflect.get(target,key,receiver);
  }});
}
class Element{
  constructor(row){
    this.tagName=row.tag.toUpperCase(); this.attrs=row.attrs; this.workbench=row.workbench;
    this.id=this.attrs.id||''; this.disabled='disabled' in this.attrs; this.files=[];
    this.checked='checked' in this.attrs; this.parentElement=null; this.open=false;
    this.className=this.attrs.class||''; this.title=this.attrs.title||'';
    this.validationMessage=''; this.validity={valid:true,customError:false};
    for(const key of ['type','min','max','step','pattern']) this[key]=this.attrs[key]||'';
    this.style={}; this.dataset={}; this.scrollTop=0; this.textContent=row.text||'';
    for(const [key,value] of Object.entries(this.attrs)){
      if(key.startsWith('data-')) this.dataset[key.slice(5)]=value;
    }
    for(const entry of (this.attrs.style||'').split(';')){
      const [key,value]=entry.split(':'); if(value) this.style[key.trim()]=value.trim();
    }
    this.options=(row.options||[]).map(o=>new Element(o));
    const selected=this.options.find(o=>'selected' in o.attrs)||this.options[0];
    this._value=selected ? selected.value : (this.attrs.value||'');
    return mockDOM(this,'Element');
  }
  get value(){return this._value;}
  get valueAsNumber(){return this.value.trim()==='' ? NaN : Number(this.value);}
  set value(value){
    this._value=String(value);

    if(this.attrs.type==='file' && this._value==='') this.files=[];
  }
  get selectedIndex(){return this.options.findIndex(o=>o.value===this.value);}
  get text(){return this.textContent;}
  set innerHTML(value){this.options=[]; this.textContent=String(value);}
  get href(){return this.attrs.href;}
  set href(value){this.setAttribute('href',value);}
  getAttribute(key){return this.attrs[key]??null;}
  setAttribute(key,value){this.attrs[key]=String(value);}
  removeAttribute(key){delete this.attrs[key];}
  hasAttribute(key){return key in this.attrs;}
  addEventListener(){} removeEventListener(){}
  setCustomValidity(message){
    this.validationMessage=String(message);
    this.validity={valid:!this.validationMessage,customError:!!this.validationMessage};
  }
  checkValidity(){return this.validity.valid;}
  reportValidity(){return this.checkValidity();}
  focus(){document.activeElement=this;}
  closest(){return null;}
  after(child){child.parentElement=this.parentElement; attach(child);}
  appendChild(child){
    child.parentElement=this; attach(child);
    this.options.push(child); if(this.options.length===1) this.value=child.value;
    return child;
  }
  replaceChildren(){this.options=[];this.textContent='';}
  remove(){
    if(byId.get(this.id)===this) byId.delete(this.id);
    const index=elements.indexOf(this); if(index!==-1) elements.splice(index,1);
  }
  click(){if(!this.disabled) return this.dispatchEvent(new Event('click'));}
  showModal(){this.open=true;} close(){this.open=false;}
  dispatchEvent(event){event.target=this; return this['on'+event.type]?.(event);}
}
const elements=fixture.elements.map(row=>new Element(row));
const byId=new Map(elements.filter(el=>el.id).map(el=>[el.id,el]));
function attach(child){
  if(!elements.includes(child)) elements.push(child);
  if(child.id) byId.set(child.id,child);
}
for(const [id,el] of byId) globalThis[id]=el;
function matches(el,selector){
  if(selector==='header .title') return el.attrs.class==='title';
  if(/^#[\w-]+$/.test(selector)) return el.id===selector.slice(1);
  const excluded=selector.match(/:not\(#([\w-]+)\)$/);
  if(excluded) return el.id!==excluded[1] && matches(el,selector.slice(0,excluded.index));
  const m=selector.match(/^(#wb )?([a-z]+)?(?:\[([\w-]+)(?:="([^"]+)")?\])?(:not\(:disabled\))?$/);
  if(!m) throw new Error('unmocked selector: '+selector);
  return (!m[1]||el.workbench) && (!m[2]||el.tagName===m[2].toUpperCase()) &&
    (!m[3]||(el.hasAttribute(m[3]) && (m[4]===undefined||el.attrs[m[3]]===m[4]))) &&
    (!m[5]||!el.disabled);
}
globalThis.document=mockDOM({
  body:elements.find(el=>el.tagName==='BODY'),
  getElementById(id){return byId.get(id)||null;},
  querySelectorAll(selector){return elements.filter(el=>selector.split(',').some(s=>matches(el,s.trim())));},
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;},
  createElement(tag){return new Element({tag,attrs:{}});},
  addEventListener(){}, removeEventListener(){}
},'document');
for(const el of elements) if(el!==document.body) el.parentElement=document.body;
function storage(){
  const values=new Map();
  return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,String(value))};
}
globalThis.sessionStorage=storage(); globalThis.localStorage=storage();
globalThis.Event=class{constructor(type){this.type=type;} preventDefault(){}};
window.scrollX=0; window.scrollY=0;
window.scrollTo=(x,y)=>{window.scrollX=x; window.scrollY=y;};
URL.createObjectURL=()=>'blob:node-dom-mock'; URL.revokeObjectURL=()=>{};
class Response{
  constructor(body,{status=200,headers={}}={}){
    this.body=String(body); this.status=status; this.ok=status>=200&&status<300;
    this.headers={get:key=>headers[key.toLowerCase()]??null};
  }
  async json(){return JSON.parse(this.body);}
  async text(){return this.body;}
  async blob(){return this.body;}
}
const pending=[];
function realFetch(path,options={}){
  const request=new Promise((resolve,reject)=>{
    const body=options.body==null ? null : Buffer.from(options.body);
    const req=http.request(new URL(path,location.href),{
      method:options.method||'GET',headers:body ? {'Content-Length':body.length} : {}
    },res=>{
      const chunks=[];
      res.on('data',chunk=>chunks.push(chunk)); res.on('error',reject);
      res.on('end',()=>resolve(new Response(Buffer.concat(chunks),{status:res.statusCode,headers:res.headers})));
    });
    req.on('error',reject); req.end(body);
  });
  pending.push(request); return request;
}
globalThis.fetch=realFetch;
function expect(ok,message){if(!ok) throw new Error(message);}

function updateBounds(){} function updateDisplay(){} function renderClearance(){}
function updateCorrectionControls(){} function frameView(){}
function switchStep(step){UI.step=step;document.querySelector('aside').scrollTop=0;}
function renderArcParams(){} function draw(){} function setPlaying(){}
"""
    projected = []
    for declaration in (r"const S = .*?;\n", r"const WB_K=.*?;", r"let IS_GUEST=.*?;"):
        projected.append(re.search(declaration, script).group())
    projected.append("const UI=" + _block_after(script, r"const UI=") + ";")
    projected.append("""
let M=S.toolpath.moves,N=M.length,cursor=0,drag=null,totalDep=0,playing=false;
const cum=[0],slider=document.getElementById('cursor');
let gpu=null;
""")
    for name in ("api", "showIdentity", "showSignIn", "showAdminLink", "showOffer",
                 "updateToolpath", "renderState", "setBusy", "sourceSelection", "revealField", "hydrateState", "substrateRegistrationError", "buildDraftError"):
        declaration = re.search(r"(?:async )?function " + name + r"\([^)]*\)", script)
        if declaration:
            projected.append(declaration.group() + _block_after(script, re.escape(declaration.group())))
    projected.append("window.waamStudio=" + _block_after(script, r"window\.waamStudio=") + ";")
    projected.append("window.WaamSubstrate=require(" + json.dumps(str(ROOT / "src/waam_pipeline/studio_substrate.js")) + ").WaamSubstrate;")
    projected.append("if(S.meta.workbench)" + _block_after(script, r"if\(S.meta.workbench\)"))
    check = """
(async()=>{
  await Promise.all(pending);
  await new Promise(setImmediate); // Finish whoami/list-jobs continuations before interaction.
  const result=await (async()=>{SCENARIO})();
  await Promise.all(pending);
  process.stdout.write(JSON.stringify(result));
})().catch(error=>{console.error(error.stack);process.exitCode=1;})
  .finally(()=>http.globalAgent.destroy());
""".replace("SCENARIO", scenario)
    code = "const fixture=" + json.dumps(fixture) + ";\n" + setup + "\n".join(projected) + check

    def skip_missing_dom_api(stderr):


        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr = stderr or ""
        missing = re.search(r"^NODE_DOM_MOCK_MISSING:([\w.]+)\r?$", stderr, re.M)
        if not missing:
            missing = re.search(
                r"^ReferenceError: (window|document|navigator|HTMLElement|HTMLInputElement|"
                r"HTMLSelectElement|HTMLDialogElement|CustomEvent|Event|Node|DOMParser|"
                r"MutationObserver|ResizeObserver|getComputedStyle|requestAnimationFrame|"
                r"cancelAnimationFrame) is not defined\r?$", stderr, re.M)
        if missing:

            raise unittest.SkipTest("node DOM mock missing " + missing.group(1))


    try:
        result = subprocess.run([shutil.which("node"), "-e",
                                 "require('vm').runInThisContext(require('fs').readFileSync(0,'utf8'))"],
                                input=code, capture_output=True, text=True, encoding="utf-8", timeout=45)
    except subprocess.TimeoutExpired as error:
        skip_missing_dom_api(error.stderr)
        raise
    skip_missing_dom_api(result.stderr)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class TestStudio(unittest.TestCase):
    def _state(self):
        tp = wall(60.0, layers=5, layer_height_mm=3.0)
        sched = Schedule.ramp(0, 4, wfs=(250, 210), volts=18.0, dwell_s=(0, 6))
        return build_state(
            tp,
            title="test wall",
            schedule=sched,
            violations=check(tp),
            cell_name="hc10-test",
        )

    def test_schedule_resolved_per_layer(self):
        state = self._state()
        per_layer = state["schedule"]["per_layer"]
        self.assertEqual(len(per_layer), 5)
        self.assertAlmostEqual(per_layer[0]["wfs"], 250)
        self.assertAlmostEqual(per_layer[4]["wfs"], 210)
        self.assertAlmostEqual(per_layer[2]["dwell_s"], 3.0)

    def test_slots_present(self):
        state = self._state()
        for slot in ("meta", "toolpath", "schedule", "violations", "scene", "part", "torch", "robot", "live"):
            self.assertIn(slot, state)
        self.assertFalse(state["live"]["linked"])
        self.assertIsNone(state["robot"])

    def test_write_and_embedded_json_roundtrip(self):
        out = ROOT / "examples" / "studio_test.html"
        write_studio(self._state(), out)
        html = out.read_text(encoding="utf-8")
        self.assertIn("WAAM STUDIO", html)
        self.assertIn("waamStudio", html)
        data = json.loads(re.search(r"const S = (\{.*?\});\n", html).group(1))
        self.assertEqual(data["toolpath"]["layers"], 5)
        self.assertEqual(len(data["toolpath"]["moves"]), 10)
        out.unlink()

    def test_scene_serialization(self):
        import numpy as np

        scene = Scene()
        scene.capsules.append(("platter", np.array([0.0, 0.0, -25.0]), np.array([0.0, 0.0, 0.0]), 300.0))
        tp = wall(40.0, layers=2, layer_height_mm=3.0)
        state = build_state(tp, scene=scene)
        self.assertEqual(state["scene"]["capsules"][0]["name"], "platter")
        self.assertEqual(state["scene"]["capsules"][0]["r"], 300.0)

    def test_geometry_review_does_not_claim_clearance_was_checked(self):
        tp = wall(40.0, layers=2, layer_height_mm=3.0)
        self.assertFalse(build_state(tp)["meta"]["clearance_checked"])
        self.assertTrue(build_state(tp, violations=[])["meta"]["clearance_checked"])

    def test_part_title_cannot_break_the_document_or_embedded_state(self):
        state = self._state()
        state["meta"]["title"] = '</script><img src=x onerror="alert(1)">'
        html = render_studio(state)
        self.assertNotIn('<img src=x', html)
        data = json.loads(re.search(r"const S = (\{.*?\});\n", html).group(1))
        self.assertEqual(data["meta"]["title"], state["meta"]["title"])

    def test_part_mesh_is_embedded_for_shaded_solid_display(self):
        import numpy as np

        tri = np.array([[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0]]])
        tp = wall(40.0, layers=2, layer_height_mm=3.0)
        state = build_state(tp, part_mesh=tri)
        self.assertEqual(state["part"]["triangles"], 1)
        self.assertEqual(state["part"]["displayed"], 1)
        self.assertEqual(len(state["part"]["tris"]), 9)
        html = write_studio(state, ROOT / "examples" / "studio_solid_test.html").read_text(encoding="utf-8")
        self.assertIn('value="solid" selected', html)
        self.assertIn("function drawPartMesh(faces)", html)
        (ROOT / "examples" / "studio_solid_test.html").unlink()

    def test_workbench_shell_is_responsive_and_labelled(self):
        html = render_studio(self._state())
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            html,
        )
        self.assertIn("@media (max-width: 860px)", html)
        self.assertIn('<label for="wbLh">Bead height</label>', html)
        self.assertIn('role="status" aria-live="polite"', html)

    def test_settings_carries_the_desktop_download_tab_and_install_guidance(self):
        html = render_studio(self._state())
        for element_id in ("settings-tab-desktop", "settings-desktop",
                           "settingsDesktopRelease", "settingsDesktopOrigin"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('aria-controls="settings-desktop"', html)
        for platform in ("macos-arm64", "windows-x64"):
            self.assertEqual(html.count(f'data-desktop-platform="{platform}" hidden'), 1)
        self.assertIn('download>Download for Mac (Apple Silicon)</a>', html)
        self.assertIn('download>Download for Windows (x64)</a>', html)
        self.assertIn('value.url==="/download/desktop/"+value.id', html)
        self.assertIn('download.href=value.url', html)
        self.assertNotIn('env.api("/download/desktop', html)
        self.assertIn('env.api("/api/desktop-release")', html)
        self.assertIn('Privacy &amp; Security', html)
        self.assertIn('Never turn off Gatekeeper', html)
        self.assertIn('does not run on Intel Macs.', html)
        self.assertIn('without administrator rights', html)
        self.assertIn('SmartScreen may warn', html)

        self.assertIn('Get-FileHash "$HOME\\Downloads\\*WAAM-Studio-*-setup.exe"', html)
        self.assertEqual(html.count('<dt>Release signature</dt><dd data-desktop-field="signature">'), 2)
        self.assertEqual(html.count('WAAM Studio 0.2.0 and later update themselves'), 2)
        self.assertEqual(html.count('Help › Check for Updates…'), 2)
        self.assertEqual(html.count('must be installed by hand once'), 2)
        self.assertIn('"not signed — install manually":"present"', html)


if __name__ == "__main__":
    unittest.main()


def _strip_js(script: str) -> str:


    code = re.sub(r"/\*.*?\*/", " ", script, flags=re.S)
    code = re.sub(r"//[^\n]*", " ", code)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    code = re.sub(r"'(?:\\.|[^'\\])*'", "''", code)
    return re.sub(r"`(?:\\.|[^`\\])*`", "``", code)


def _tdz_violations(script: str):


    code = _strip_js(script)
    out = []
    for m in re.finditer(r"^(const|let)\s+([A-Za-z_$][\w$]*)", code, re.M):
        name, pos = m.group(2), m.start()
        earlier = re.search(r"\b" + re.escape(name) + r"\b", code[:pos])
        if earlier:
            out.append((name, code[:earlier.start()].count("\n") + 1,
                        code[:pos].count("\n") + 1))
    return out


class TestAdminPendingSelection(unittest.TestCase):
    def test_the_query_selects_only_a_matching_pending_row(self):
        self.assertIn('new URLSearchParams(window.location.search)', _ADMIN_PAGE)
        self.assertIn('.get("pending")', _ADMIN_PAGE)
        pending = _ADMIN_PAGE.split("function pendingSection(rows){", 1)[1]
        pending, active = pending.split("function activeSection(rows){", 1)
        self.assertIn('if(row.email === pendingEmail)', pending)
        self.assertIn('tr.className = "selected"', pending)
        self.assertIn('tr.tabIndex = -1', pending)
        self.assertIn('selectedRow = tr', pending)
        self.assertNotIn('selectedRow = tr', active)
        self.assertIn('e.textContent = row.email', pending)

        self.assertNotRegex(_ADMIN_PAGE, r'(?:innerHTML|querySelector)[^;]*pendingEmail')
        self.assertRegex(_ADMIN_PAGE, r'tr\.selected\s*\{[^}]*background:')

    def test_selection_scrolls_and_focuses_after_rows_are_attached(self):
        render = _ADMIN_PAGE.split("function render(state){", 1)[1]
        render = render.split("function load(){", 1)[0]
        self.assertIn('selectedRow = null', render)
        self.assertIn('if(selectedRow)', render)
        self.assertIn('selectedRow.scrollIntoView({block:"center"})', render)
        self.assertIn('selectedRow.focus({preventScroll:true})', render)
        self.assertLess(render.index('board.appendChild(activeSection'),
                        render.index('selectedRow.scrollIntoView'))


class TestNoTemporalDeadZone(unittest.TestCase):


    def test_the_detector_actually_detects(self):


        bad = "\n".join([
            "function api(p){ return p + WB_K; }",
            "fetch(api('/api/whoami'));",
            "const WB_K = 'k';",
        ])
        hits = _tdz_violations(bad)
        self.assertTrue(hits, "the detector missed a textbook TDZ violation")
        self.assertEqual(hits[0][0], "WB_K")

    def test_a_comment_mentioning_a_name_is_not_a_use(self):
        ok = "\n".join([
            "/* WB_K is declared below, on purpose. */",
            "const WB_K = 'k';",
            "function api(p){ return p + WB_K; }",
        ])
        self.assertEqual(_tdz_violations(ok), [])

    def test_the_rendered_page_has_none(self):
        run_slice({"kind": "wall", "name": "tdz guard"}, None, "local")
        html = _state("local")["html"]
        script = html.split("<script data-app>", 1)[1].rsplit("</script>", 1)[0]
        hits = _tdz_violations(script)
        self.assertEqual(
            hits, [],
            "a top-level const/let is referenced before it is declared: "
            + "; ".join(f"{n} used line {u}, declared line {d}"
                        for n, u, d in hits))


def _block_after(script, anchor):


    m = re.search(anchor, script)
    if not m:
        return None
    i = script.find("{", m.end())
    if i < 0:
        return None
    depth, j = 0, i
    in_str = None
    in_line = in_block = esc = False
    while j < len(script):
        c = script[j]
        nxt = script[j + 1:j + 2]
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                j += 1
        elif in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        elif c == "/" and nxt == "/":
            in_line = True
            j += 1
        elif c == "/" and nxt == "*":
            in_block = True
            j += 1
        elif c in "\"'`":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return script[i:j + 1]
        j += 1
    return None


class _Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.controls, self.labels = {}, set()
        self.in_label = 0
        self.feed(html.split("<script data-app>", 1)[0])

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self.in_label += 1
            self.labels.add(attrs.get("for"))
        if tag in ("input", "select"):
            attrs["wrapped_label"] = bool(self.in_label)
            self.controls[attrs["id"]] = attrs

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label -= 1


class TestInputRefusals(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.state = build_state(wall(60, layers=3, layer_height_mm=3),
                                title="test part", workbench=True)
        cls.html = render_studio(cls.state)
        cls.script = cls.html.split("<script data-app>", 1)[1].rsplit("</script>", 1)[0]
        cls.markup = _Controls(cls.html)

    def test_every_number_has_bounds_and_a_field_message(self):
        expected = {
            "wbLh": "Bead height", "wbTilt": "Tilt", "wbSeam": "Seam",
            "wbBead": "Bead width", "wbMargin": "Margin", "wbWire": "Wire",
            "wbTravel": "Travel", "wbWfs": "Wire feed", "wbOx": "X offset", "wbOy": "Y offset",
            "wbSub": "Substrate", "wbOz": "Lift-off gap", "wbYaw": "Z rotation",
            "wbBeadLength": "Bead length", "wbSubstrateMargin": "Substrate edge margin",
            "wbBeadCount": "Bead count", "wbBeadPitch": "Bead spacing",
            "wbWallLength": "Wall length", "wbWallLayers": "Wall layers",
            "wbWallThickness": "Wall thickness",
            "wbCopies": "Copies", "wbCopySpacing": "Copy spacing",
            "wbInterpass": "Interpass dwell", "wbInterpassEvery": "Layers per interpass",
            "wbTempCheckEvery": "Temperature check", "wbBeadDwell": "Between-bead dwell",
            "wbMeasureEvery": "Measure stop",
            "wbAutoAmbient": "Ambient",
            "wbRotX": "X tilt", "wbRotY": "Y tilt",
            "wbArc": "ASF#",
            "wbContours": "ORNL contours",
            "wbStepover": "ORNL bead spacing",
            "wbInfillAngle": "Infill angle",
            "wbInfillRotation": "Infill rotation",
            "wbTravelLift": "Travel lift",
            "wbSenseSpacing": "Touch spacing",
            "wbSenseInset": "Inset from edges",
            "wbSensePoints": "Maximum touches",
            "wbSenseLayer": "Layer to touch",
            "wbLayerSpacing": "Layer touch spacing",
            "wbRunTemperature": "Temperature",
            "wbAutoSpacing": "Layer touch spacing",
            "wbAutoMax": "Maximum interpass",
            "wbAutoBefore": "T-before",
            "wbAutoAfter": "T-after",
            "wbAutoReading": "Temperature",
            "wbReindexClear": "Clear height",
            "wbCtwd": "Weld CTWD",
        }
        numbers = {k: v for k, v in self.markup.controls.items()
                   if v.get("type") == "number"}
        self.assertEqual(numbers.keys(), expected.keys())
        for control, attrs in numbers.items():
            with self.subTest(control=control):
                self.assertIn("min", attrs)
                self.assertIn("max", attrs)
                self.assertLess(float(attrs["min"]), float(attrs["max"]))
                if control in ("wbAutoMax", "wbAutoAmbient", "wbWfs", "wbCtwd", "wbMeasureEvery"):
                    self.assertNotIn("required", attrs)
                else:
                    self.assertIn("required", attrs)
                self.assertIn(expected[control], attrs.get("data-error", ""))
                if "value" in attrs:
                    self.assertLessEqual(float(attrs["min"]), float(attrs["value"]))
                    self.assertLessEqual(float(attrs["value"]), float(attrs["max"]))
        for control in ("wbLh", "wbBead", "wbWire", "wbTravel", "wbWfs"):
            self.assertGreater(float(numbers[control].get("min", "0")), 0)
        self.assertEqual(numbers["wbArc"]["step"], "1")
        for control in ("wbBeadCount", "wbCopies", "wbWallLayers", "wbInterpassEvery", "wbTempCheckEvery", "wbMeasureEvery"):
            self.assertEqual(numbers[control]["step"], "1", control)
        self.assertEqual(float(numbers["wbRunTemperature"]["min"]), -40)
        self.assertEqual(float(numbers["wbRunTemperature"]["max"]), 2500)
        for control in ("wbRunHeight", "wbRunWidth"):
            self.assertEqual(self.markup.controls[control]["inputmode"], "decimal")
            self.assertNotIn("required", self.markup.controls[control])

    def test_live_numeric_validation_and_submission_use_the_bounds(self):
        body = _block_after(self.script, r"function\s+numberError\s*\(")
        self.assertIsNotNone(body)
        for expected in ("valueAsNumber", "Number.isFinite", "el.min", "el.max",
                      "Number.isInteger", "el.dataset.error"):
            self.assertIn(expected, body)
        feedback = _block_after(self.script, r"function\s+fieldFeedback\s*\(")
        self.assertIsNotNone(feedback)
        self.assertIn("setCustomValidity", feedback)
        self.assertIn('"aria-invalid"', feedback)
        self.assertIn("textContent", feedback)
        self.assertIn('el.addEventListener("input"', self.script)
        for anchor, request in ((r'getElementById\("sliceBtn"\)\.onclick', "/api/slice"),
                                (r"function\s+applyPlacement\s*\(", "/api/place")):
            handler = _block_after(self.script, anchor)
            self.assertIsNotNone(handler)
            self.assertLess(handler.index("validateFields("), handler.index(request))

    def test_measure_stop_controls_defaults_and_export_query(self):
        links = [element for element in _StudioElements(self.html).elements
                 if element["tag"] == "a" and "data-run-sheet" in element["attrs"]]
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(link["attrs"]["href"], "/run-sheet")
            self.assertEqual(link["attrs"]["target"], "_blank")
            self.assertEqual(link["attrs"]["rel"], "noopener")
            self.assertTrue(link["workbench"])
        self.assertIn('document.querySelectorAll("a[data-run-sheet]")', self.script)
        self.assertIn('link.href=api("/run-sheet")', self.script)
        every = self.markup.controls["wbMeasureEvery"]
        self.assertEqual((every["value"], every["min"], every["max"], every["step"]),
                         ("0", "0", "1000", "1"))
        self.assertNotIn("required", every)
        times = self.markup.controls["wbMeasureTimes"]
        self.assertEqual(times["type"], "text")
        self.assertEqual(times["value"], "20, 40, 60")
        self.assertNotIn("required", times)
        for control, checked in (("wbMeasurePause", True), ("wbMeasureClean", False),
                                 ("wbMeasurePreWeld", True)):
            with self.subTest(control=control):
                field = self.markup.controls[control]
                self.assertEqual(field["type"], "checkbox")
                self.assertEqual("checked" in field, checked)
        query = _block_after(self.script, r"function\s+interpassQuery\s*\(")
        fields = re.search(r"const WB_FIELDS=\[(.*?)\];", self.script, re.S).group(1)
        for key, control in (("measure_every", "wbMeasureEvery"),
                             ("measure_times", "wbMeasureTimes"),
                             ("measure_pause", "wbMeasurePause"),
                             ("measure_clean", "wbMeasureClean"),
                             ("measure_pre_weld", "wbMeasurePreWeld")):
            with self.subTest(key=key):
                self.assertIn(f"&{key}=", query)
                self.assertIn(control, query)
                self.assertIn(f'"{control}"', fields)
        for endpoint in ("/api/jbi?", "/api/cycle-job?", "/api/ros2?"):
            request = next(line for line in self.script.splitlines() if endpoint in line)
            self.assertIn("interpassQuery()", request)

    def test_clean_first_control_is_persisted_and_sent_with_every_export(self):
        field = self.markup.controls["wbCleanFirst"]
        self.assertEqual(field["type"], "checkbox")
        self.assertNotIn("checked", field)
        self.assertIn("Clean wire (CLEAN_R1) before the first weld", self.html)
        fields = re.search(r"const WB_FIELDS=\[(.*?)\];", self.script, re.S).group(1)
        self.assertIn('"wbCleanFirst"', fields)
        query = _block_after(self.script, r"function\s+ctwdQuery\s*\(")
        self.assertIn('"&clean_first="', query)
        self.assertIn("wbCleanFirst.checked", query)
        for endpoint in ("/api/jbi?", "/api/cycle-job?", "/api/ros2?"):
            request = next(line for line in self.script.splitlines() if endpoint in line)
            self.assertIn("ctwdQuery()", request)

    def test_measure_stop_export_validates_reading_times(self):
        validate = _block_after(self.script, r"function\s+validateEmit\s*\(")
        self.assertIn("wbMeasureEvery", validate)
        self.assertIn("measureTimesError(", validate)
        times = _block_after(self.script, r"function\s+measureTimesError\s*\(")
        self.assertIsNotNone(times)
        for expected in ("wbMeasureTimes", "fieldFeedback", "Number.isFinite",
                         "3600", "10", "strictly increasing"):
            self.assertIn(expected, times)
        self.assertIn('wbMeasureTimes.addEventListener("input",', self.script)

    def test_surface_sense_choices_have_safe_defaults_and_labels(self):
        elements = _StudioElements(self.html).elements
        for control, choices in (
            ("wbSenseLayout", [("grid", "Grid over the plate"),
                               ("path", "Along the part's base layer")]),
            ("wbSenseFit", [("smooth", "Smooth fit (refuses outliers)"),
                            ("interpolate", "Follow every touch")]),
        ):
            with self.subTest(control=control):
                fields = [element for element in elements
                          if element["attrs"].get("id") == control]
                self.assertEqual(len(fields), 1)
                field = fields[0]
                self.assertEqual(field["tag"], "select")
                self.assertIn("disabled", field["attrs"])
                self.assertIn(control, self.markup.labels)
                self.assertEqual([(option["attrs"]["value"], option["text"])
                                  for option in field["options"]], choices)
                selected = next((option for option in field["options"]
                                 if "selected" in option["attrs"]), field["options"][0])
                self.assertEqual(selected["attrs"]["value"], choices[0][0])

    def test_path_sense_import_can_preserve_the_measured_placement(self):
        body = _block_after(self.script, r"apply:async\(raw,options\)=>")
        self.assertIsNotNone(body)
        self.assertIn("const preservePlacement=!!options?.preservePlacement", body)
        reset = _block_after(body, r"if\(!preservePlacement\)")
        self.assertIsNotNone(reset)
        for field in ("wbOx", "wbOy", "wbYaw", "wbOz"):
            self.assertIn(f'{field}.value="0"', reset)
        self.assertIn("else if(!preservePlacement)window.waamPlacement?.reset()", body)
        self.assertIn("applyPlacement(false,true)", body)
        self.assertIn('if(options.layout==="path")qs.set("gen",String(S.meta.gen))', self.script)

    def test_validation_helpers_are_declared_before_use_and_tdz_clean(self):
        code = _strip_js(self.script)
        for name in ("fieldFeedback", "numberError", "measureTimesError", "jobNameError", "validateFields", "validateEmit"):
            with self.subTest(helper=name):
                definition = re.search(r"\bfunction\s+(" + name + r")\s*\(", code)
                self.assertIsNotNone(definition)
                self.assertEqual(re.search(r"\b" + name + r"\b", code).start(), definition.start(1))
                body = _block_after(self.script, r"function\s+" + name + r"\s*\(")
                self.assertEqual(_tdz_violations(dedent(body[1:-1])), [])

    def test_bad_file_is_refused_before_read_or_fetch(self):
        body = _block_after(self.script, r'getElementById\("sliceBtn"\)\.onclick')
        message = "Choose a binary or ASCII STL file."
        self.assertIn(message, body)
        self.assertIn(r"/\.stl$/i.test(f.name)", body)
        self.assertLess(body.index(message), body.index("f.arrayBuffer()"))

    def test_arc_card_survives_an_unavailable_estimate(self):
        for state in (self.state, dict(self.state, arc_warnings=["Choose a filler"])):
            html = render_studio(state)
            panel = re.search(r'<section\b[^>]*id="arcPanel"[^>]*>', html).group()
            self.assertNotIn("display:none", panel)
            self.assertIn("Arc estimate unavailable", html)
            self.assertIn('getElementById("arcWarn").textContent', html)

    def test_job_labels_and_live_inform_rule(self):
        for control, label in (("wbJobName", "Save this setup as"),
                               ("wbJbiName", "Controller job name (INFORM)")):
            self.assertIn(f'<label for="{control}">{label}</label>', self.html)
        field = self.markup.controls["wbJbiName"]
        self.assertEqual(field["pattern"], "[A-Z0-9][A-Z0-9_\\-]{0,31}")
        self.assertEqual(field["maxlength"], "32")
        self.assertIn("required", field)

        from waam_pipeline.server import _JBI_NAME_RE
        for name in ("A", "9_JOB-1", "A" * 32, "bad.name", "has space", "-A",
                     "A" * 33, "", "O'NEIL"):
            with self.subTest(name=name):
                self.assertEqual(bool(re.fullmatch(field["pattern"], name)),
                                 bool(_JBI_NAME_RE.fullmatch(name)))
        live = _block_after(self.script, r"function\s+jobNameError\s*\(")
        self.assertIsNotNone(live)
        self.assertIn("el.value=el.value.toUpperCase()", live.replace(" ", ""))
        self.assertIn("el.pattern", live)

    def test_job_folder_defaults_to_waam_and_rides_with_every_job_export(self):
        self.assertIn('<label for="wbJobFolder" class="field-label">Controller job folder</label>', self.html)
        field = self.markup.controls["wbJobFolder"]
        self.assertEqual(field["value"], "WAAM")
        self.assertEqual(field["maxlength"], "32")
        self.assertNotIn("required", field)
        from waam_pipeline.backends.jbi import _JOB_NAME_RE
        for folder in ("WAAM", "GABEJ-YRC1000", "FOLDER_2", "A" * 32, "A" * 33, "has space", "WA.AM", ""):
            with self.subTest(folder=folder):
                self.assertEqual(bool(re.fullmatch(field["pattern"], folder)),
                                 bool(_JOB_NAME_RE.fullmatch(folder)))
        fields = re.search(r"const WB_FIELDS=\[(.*?)\];", self.script, re.S).group(1)
        self.assertIn('"wbJobFolder"', fields)
        params = re.search(r"const WB_PARAM_FIELD=\{(.*?)\};", self.script, re.S).group(1)
        self.assertNotIn("wbJobFolder", params)
        robot = _block_after(self.script, r'getElementById\("emitBtn"\)\.onclick')
        self.assertIn('"&folder="+encodeURIComponent(folder)', robot)
        self.assertIn("substrate_registration:raw,folder:jobFolder()", self.script)
        self.assertIn("gen:String(S.meta.gen),folder:jobFolder()", self.script)
        validate = _block_after(self.script, r"function\s+validateEmit\s*\(")
        self.assertIn("jobFolderError", _block_after(validate, r"\bif\s*\(needsArc\)"))

    def test_emit_collects_every_field_before_returning(self):
        validate = _block_after(self.script, r"function\s+validateEmit\s*\(")
        self.assertIsNotNone(validate)
        for expected in ("jobNameError", "numberError", "S.meta.placement", "S.violations", "errors.push"):
            self.assertIn(expected, validate)


        self.assertIsNone(self.state["meta"]["placement"])
        cell_off = _block_after(validate, r"\bif\s*\(!S\.meta\.placement\)")
        self.assertIsNotNone(cell_off)
        self.assertRegex(cell_off, r'errors\.push\("Enable the cell view[^"\n]*slice and check')
        arc_only = _block_after(validate, r"\bif\s*\(needsArc\)")
        self.assertIsNotNone(arc_only)
        self.assertNotIn("S.meta.placement", arc_only)

        self.assertEqual(len(re.findall(r"\breturn\b", _strip_js(validate))), 1)
        for bid, mode in (("emitBtn", "true"), ("emitRosBtn", "false")):
            body = _block_after(self.script, r'getElementById\("' + bid + r'"\)\.onclick')
            call = f"validateEmit({mode})"
            self.assertIn(call, body)
            self.assertLess(body.index(call), body.index("attestPlatter("))
            self.assertLess(body.index(call), body.index("fetch("))
            self.assertIn('errors.join("\\n")', body)
            if mode == "true":


                self.assertIn('String(document.getElementById("wbArc").valueAsNumber)', body)

    def test_attestation_collects_errors_instead_of_serial_alerts(self):
        body = _block_after(self.script, r"form\.onsubmit=ev=>")
        self.assertIsNotNone(body)
        self.assertNotIn("alert(", body)
        self.assertIn("errors.push", body)
        self.assertIn('errors.join("\\n")', body)
        self.assertLess(body.index("pdClear"), body.index("if(errors.length)"))
        self.assertLess(body.index("who.value.trim()"), body.index("if(errors.length)"))

    def test_all_inputs_and_selects_have_accessible_labels(self):
        for control, attrs in self.markup.controls.items():
            with self.subTest(control=control):
                self.assertTrue(attrs.get("aria-label") or attrs["wrapped_label"]
                                or control in self.markup.labels,
                                f"{control} has no accessible label")
        for control in ("play", "offerX"):
            tag = re.search(r'<button\b[^>]*id="' + control + r'"[^>]*>', self.html).group()
            self.assertIn('aria-label="', tag)

    def test_part_heading_and_keyboard_canvas(self):
        self.assertRegex(self.html, r'<h1\b[^>]*>test part</h1>')
        wrapper = re.search(r'<div\b[^>]*id="view"[^>]*>', self.html).group()
        self.assertIn('tabindex="0"', wrapper)
        self.assertIn('aria-label="', wrapper)
        self.assertIn("#view:focus-visible", self.html)


class TestGuestControls(unittest.TestCase):


    def _html(self):
        tp = wall(60.0, layers=3, layer_height_mm=3.0)
        state = build_state(tp, title="guest wb", workbench=True, workspace="alice")
        return render_studio(state)

    def _script(self):
        return self._html().split("<script data-app>", 1)[1].rsplit("</script>", 1)[0]

    def _whoami(self):
        body = _block_after(self._script(), r"\(async\s*\(\)\s*=>")
        self.assertIsNotNone(body, "could not find the whoami IIFE")
        self.assertIn('fetch(api("/api/whoami"))', body)
        return body

    def test_account_buttons_start_disabled(self):
        markup = self._html().split("<script data-app>", 1)[0]
        for bid in ("saveBtn", "loadBtn", "emitBtn", "emitRosBtn"):
            with self.subTest(control=bid):
                tag = re.search(r'<button\b[^>]*\bid="' + bid + r'"[^>]*>', markup)
                self.assertIsNotNone(tag, f"{bid} is missing from the markup")
                self.assertRegex(tag.group(), r"\sdisabled(?:\s|=|>)",
                                 f"{bid} must be disabled before whoami answers")

    def test_gcode_download_starts_disabled_without_href(self):
        markup = self._html().split("<script data-app>", 1)[0]
        tags = re.findall(r'<a\b[^>]*\bdownload="part\.gcode"[^>]*>', markup)
        self.assertEqual(len(tags), 1)
        self.assertNotRegex(tags[0], r"\shref\s*=",
                            "a pending identity must not have a download URL")
        self.assertIn('aria-disabled="true"', tags[0])
        self.assertIn("opacity:.5", tags[0])
        self.assertIn("pointer-events:none", tags[0])

    def test_guest_flag_is_top_level_and_tdz_clean(self):
        script = self._script()
        code = _strip_js(script)
        self.assertEqual(
            len(re.findall(r"\b(?:const|let|var)\s+IS_GUEST\b", code)), 1,
            "IS_GUEST must be declared exactly once, at top level")
        self.assertRegex(code, r"(?m)^let IS_GUEST\s*=\s*false;")


        self.assertEqual(_tdz_violations(script), [])

    def test_unlock_identifiers_are_declared_once_before_use(self):
        code = _strip_js(self._script())
        definitions = list(re.finditer(r"\bfunction\s+(unlockAccountControls)\s*\(", code))
        self.assertEqual(len(definitions), 1)
        self.assertEqual(re.search(r"\bunlockAccountControls\b", code).start(),
                         definitions[0].start(1))

        scope = dedent(self._whoami()[1:-1])
        self.assertEqual(len(re.findall(r"\b(?:const|let|var)\s+guest\b",
                                        _strip_js(scope))), 1)
        self.assertEqual(_tdz_violations(scope), [])

    def test_whoami_confirmed_guest_flags_and_stays_locked(self):
        body = self._whoami()
        ok = _block_after(body, r"\bif\s*\(j\.ok\)")
        self.assertIsNotNone(ok)
        self.assertRegex(_strip_js(ok), r"\bguest\s*=\s*!!j\.guest;")
        block = _block_after(ok, r"\bif\s*\(j\.guest\)")
        self.assertIsNotNone(block, "could not find the confirmed guest branch")
        self.assertIn("IS_GUEST=true", block.replace(" ", ""),
                      "the whoami guest path must set IS_GUEST")
        self.assertIn("lockGuestControls();", block,
                      "the whoami guest path must lock the guest-forbidden controls")
        self.assertEqual(len(re.findall(r"\bguest\s*=(?!=)", _strip_js(body))), 2,
                         "only the initial default and j.ok may set the guest flag")
        final = _block_after(body, r"\bfinally\b")
        self.assertIsNotNone(final)
        self.assertRegex(_strip_js(final),
                         r"\bif\s*\(!guest\)\s*unlockAccountControls\(\);")

    def test_whoami_non_guest_unlocks_in_finally(self):
        body = self._whoami()
        ok = _block_after(body, r"\bif\s*\(j\.ok\)")
        self.assertIsNotNone(ok)
        self.assertRegex(_strip_js(ok), r"\bguest\s*=\s*!!j\.guest;")
        final = _block_after(body, r"\bfinally\b")
        self.assertIsNotNone(final, "non-guests must regain the account controls")
        self.assertRegex(_strip_js(final),
                         r"\bif\s*\(!guest\)\s*unlockAccountControls\(\);")
        self.assertEqual(len(re.findall(r"\bunlockAccountControls\s*\(",
                                        _strip_js(self._script()))), 2,
                         "one definition and one guarded call; no unconditional unlock")

    def test_whoami_failure_unlocks_without_guest_confirmation(self):
        body = self._whoami()
        self.assertRegex(_strip_js(body), r"^\{\s*let guest\s*=\s*false\b")
        attempt = _block_after(body, r"\btry\b")
        ok = _block_after(attempt, r"\bif\s*\(j\.ok\)")
        self.assertIsNotNone(ok)

        self.assertNotRegex(_strip_js(attempt.replace(ok, "{}", 1)), r"\bguest\b")
        caught = _block_after(body, r"\bcatch\s*\(e\)")
        self.assertIsNotNone(caught)
        self.assertRegex(_strip_js(caught), r"^\{\s*\}$",
                         "a network/JSON error must not turn the visitor into a guest")

        self.assertRegex(_strip_js(body), r"\}\s*catch\(e\)\s*\{\s*\}\s*finally\s*\{")
        final = _block_after(body, r"\bfinally\b")
        self.assertIsNotNone(final)
        self.assertRegex(_strip_js(final),
                         r"\bif\s*\(!guest\)\s*unlockAccountControls\(\);")

    def test_lock_disables_the_action_controls_and_download(self):
        body = _block_after(self._script(), r"function\s+lockGuestControls\s*\(")
        self.assertIsNotNone(body, "lockGuestControls is not defined")
        for bid in ("saveBtn", "loadBtn", "emitBtn", "emitRosBtn"):
            self.assertIn(bid, body, f"{bid} must be disabled for a guest")
        self.assertIn("disabled=true", body.replace(" ", ""))
        self.assertIn("part.gcode", body,
                      "the G-code download link must be disabled for a guest")

    def test_unlock_restores_the_action_controls_and_download(self):
        script = self._script()
        body = _block_after(script, r"function\s+unlockAccountControls\s*\(")
        self.assertIsNotNone(body, "unlockAccountControls is not defined")
        for bid in ("saveBtn", "loadBtn", "emitBtn", "emitRosBtn"):
            self.assertIn(bid, body, f"{bid} must be re-enabled for an account")
        buttons = _block_after(body, r"\bfor\s*\(const\s+id\b")
        self.assertIsNotNone(buttons)
        self.assertIn("b.disabled=false", buttons.replace(" ", ""))
        self.assertIn('b.title=""', buttons.replace(" ", ""))
        self.assertIn("document.querySelectorAll('#wb a[download=\"part.gcode\"]')", body,
                      "the link must be found while it has no href")
        link = _block_after(body, r"\bfor\s*\(const\s+a\b")
        self.assertIsNotNone(link)
        for statement in ('a.href=api("/api/gcode")', 'a.title=""',
                          'a.style.opacity=""', 'a.style.pointerEvents=""',
                          'a.removeAttribute("aria-disabled")'):
            self.assertIn(statement, link.replace(" ", ""))
        self.assertEqual(len(re.findall(r'a\.href\s*=\s*api\("/api/gcode"\)', script)), 1,
                         "the download URL/token must only be set by unlock")

    def test_placement_failure_preserves_pending_account_controls(self):
        body = _block_after(self._script(), r"function\s+applyPlacement\s*\(")
        self.assertIsNotNone(body)
        self.assertIn("if(!beginWb()) return false;", body)
        begin = _block_after(self._script(), r"function\s+beginWb\s*\(")
        self.assertIn("#wb button:not(:disabled)", begin,
                      "only controls that started enabled may be restored")
        final = _block_after(body, r"\bfinally\b")
        self.assertIsNotNone(final)
        self.assertIn("endWb();", final)
        end = _block_after(self._script(), r"function\s+endWb\s*\(")
        self.assertIn("for(const el of wbDisabled) el.disabled=false;", end)
        self.assertLess(end.index("disabled=false"), end.index("applyAccountControls();"),
                        "whoami may have confirmed a guest during placement")
        self.assertIn("wbBusy=false", end,
                      "successful no-reload placement must allow the next placement")

    def test_every_guest_forbidden_fetch_checks_response_ok(self):
        script = self._script()
        sites = {
            "wbListJobs (/api/jobs)": r"function\s+wbListJobs\s*\(",
            "attestPlatter (/api/fixtures)": r"function\s+attestPlatter\s*\(",
            "save (/api/save)": r'getElementById\("saveBtn"\)\.onclick',
            "load (/api/load)": r'getElementById\("loadBtn"\)\.onclick',
            "emit (/api/jbi)": r'getElementById\("emitBtn"\)\.onclick',
            "emitRos (/api/ros2)": r'getElementById\("emitRosBtn"\)\.onclick',
        }
        for label, anchor in sites.items():
            body = _block_after(script, anchor)
            self.assertIsNotNone(body, f"could not locate the {label} handler")
            self.assertIn(
                "r.ok", body,
                f"{label} must check response.ok before assuming a body shape")

    def test_guest_handlers_short_circuit_before_fetching(self):


        script = self._script()
        for bid in ("saveBtn", "loadBtn", "emitBtn", "emitRosBtn"):
            body = _block_after(script, r'getElementById\("' + bid + r'"\)\.onclick')
            self.assertIsNotNone(body, f"could not locate the {bid} handler")
            self.assertIn("IS_GUEST", body,
                          f"the {bid} handler must short-circuit for a guest")


class TestSliceLoop(unittest.TestCase):


    def setUp(self):
        state = build_state(wall(60, layers=3, layer_height_mm=3),
                            title="part.stl", workbench=True, workspace="alice",
                            stl_info={"resolved_units": "mm", "size_mm": [60, 8, 9]},
                            request_params={"kind": "stl"})
        self.html = render_studio(state)
        self.script = self.html.split("<script data-app>", 1)[1].rsplit("</script>", 1)[0]

    def block(self, anchor):
        body = _block_after(self.script, anchor)
        self.assertIsNotNone(body, f"missing handler: {anchor}")
        return body

    def test_loaded_and_selected_stl_have_a_named_removable_chip(self):
        markup = self.html.split("<script data-app>", 1)[0]
        self.assertIn('id="stlChip"', markup)
        self.assertIn('id="stlName"', markup)
        self.assertRegex(markup, r'<button[^>]*id="clearStl"[^>]*aria-label="Remove STL"')
        self.assertIn('let loadedStl=S.meta.stl ? S.meta.title : null;', self.script)
        chip = self.block(r"function renderStlChip\(")
        self.assertIn('files[0]', chip)
        self.assertIn('f ? f.name : selectedName', chip)
        self.assertIn('selectedSource.name||loadedStl', chip)
        self.assertIn('.textContent=name', chip)
        self.assertIn('name!==null ? "" : "none"', chip)
        self.assertNotIn('innerHTML', chip)
        changed = self.block(r'getElementById\("stlFile"\)\.onchange')
        self.assertIn('renderStlChip();', changed)
        self.assertIn('window.waamLibrary?.fileChanged();', changed)
        self.assertIn('getElementById("clearStl").onclick=clearStl;', self.script)
        self.assertRegex(self.script, r'fileChanged\(\);\s*\};\s*renderStlChip\(\);')

    def test_demo_change_and_remove_clear_both_sources_of_stl_reuse(self):
        self.assertIn('getElementById("wbShape").onchange=clearStl;', self.script)
        clear = self.block(r"function clearStl\(")
        self.assertIn('loadedStl=null;', clear)
        self.assertIn('getElementById("stlFile").value="";', clear)
        self.assertIn('renderStlChip();', clear)
        sliced = self.block(r'getElementById\("sliceBtn"\)\.onclick')
        self.assertIn('const priorStl=!f && loadedStl!==null;', sliced)
        self.assertIn('kind:(f||priorStl)?"stl":"wall"', sliced)
        self.assertIn('reuse:priorStl?"1":"0"', sliced)
        self.assertNotIn('S.part', sliced)

    def test_slice_and_placement_render_only_successful_responses_without_reload(self):
        for anchor in (r'getElementById\("sliceBtn"\)\.onclick',
                       r'function applyPlacement\('):
            with self.subTest(handler=anchor):
                body = self.block(anchor)
                self.assertNotIn('location.reload', body)
                self.assertIn('if(!r.ok || !j.ok) throw new Error', body)
                self.assertIn('renderWorkbench(j.state);', body)
                self.assertLess(body.index('if(!r.ok || !j.ok)'),
                                body.index('renderWorkbench(j.state);'))

                caught = _block_after(body, r'catch\(e\)')
                self.assertNotIn('clearStl', caught)
                self.assertNotIn('loadedStl=', caught)

    def test_render_preserves_the_next_source_and_only_a_slice_consumes_an_upload(self):
        render = self.block(r'function renderWorkbench\(')
        self.assertNotRegex(render, r'loadedStl\s*=|stlFile.*\.value\s*=')
        self.assertIn('if(key==="shape" || key==="units") continue;', render)
        self.assertIn('renderStlChip();', render)
        sliced = self.block(r'getElementById\("sliceBtn"\)\.onclick')
        self.assertIn('loadedStl=j.state.meta.stl ? j.state.meta.title : null;', sliced)
        self.assertIn('getElementById("stlFile").value="";', sliced)
        self.assertLess(sliced.index('if(!r.ok || !j.ok)'), sliced.index('loadedStl='))
        self.assertLess(sliced.index('loadedStl='), sliced.index('renderWorkbench(j.state);'))
        placed = self.block(r'function applyPlacement\(')
        self.assertNotRegex(placed, r'loadedStl\s*=|stlFile.*\.value\s*=')

    def test_slice_placement_and_load_share_a_busy_guard_and_always_release_it(self):
        begin = self.block(r'function beginWb\(')
        self.assertIn('if(wbBusy) return false;', begin)
        self.assertLess(begin.index('wbBusy=true;'), begin.index('disabled=true'))
        for selector in ('button:not(:disabled)', 'input:not(:disabled)', 'select:not(:disabled)'):
            self.assertIn(selector, begin)
        self.assertIn('a.removeAttribute("href")', begin)
        for anchor in (r'getElementById\("sliceBtn"\)\.onclick', r'function applyPlacement\(',
                       r'getElementById\("loadBtn"\)\.onclick'):
            with self.subTest(handler=anchor):
                body = self.block(anchor)
                self.assertRegex(body, r'if\(!beginWb\(\)\) return(?: false)?;')


                self.assertLess(body.index('if(!beginWb())'), body.index('await fetch('))
                final = _block_after(body, r'finally')
                self.assertIsNotNone(final)
                self.assertIn('endWb();', final)

    def test_busy_cleanup_respects_whoami_settling_during_a_request(self):
        unlock = self.block(r'function unlockAccountControls\(')
        self.assertIn('if(wbBusy) return;', unlock)
        self.assertLess(unlock.index('applyAccountControls=unlockAccountControls;'),
                        unlock.index('if(wbBusy) return;'))
        self.assertLess(unlock.index('if(wbBusy) return;'), unlock.index('b.disabled=false'))
        end = self.block(r'function endWb\(')
        self.assertLess(end.index('wbBusy=false;'), end.index('applyAccountControls();'))
        self.assertLess(end.index('disabled=false'), end.index('applyAccountControls();'))

    def test_render_preserves_operator_fields_in_place_and_in_storage(self):
        fields = re.search(r'const WB_FIELDS=(\[.*?\]);', self.script, re.S).group(1)
        mapping = self.block(r'const WB_PARAM_FIELD=')
        render = self.block(r'function renderWorkbench\(')
        for name in ('wbArc', 'wbJbiName', 'wbJobName'):
            self.assertIn('"' + name + '"', fields)
            self.assertNotIn(name, mapping)
        self.assertIn('Object.entries(WB_PARAM_FIELD)', render)
        self.assertIn('rememberWb();', render)
        self.assertNotRegex(render, r'innerHTML|outerHTML|replaceWith|\.reset\(')
        remember = self.block(r'function rememberWb\(')
        self.assertIn('window.waamDraft.export()', remember)
        self.assertIn('sessionStorage.setItem(UI.fields, JSON.stringify(', remember)
        self.assertIn('fields:"waamWb:"+(S.meta.workspace||"offline")', self.script)

    def test_desktop_draft_contract_is_versioned_and_signals_after_initialisation(self):
        contract = self.block(r'window\.waamDraft=')
        self.assertIn('version:1', contract)
        self.assertIn('fields:Object.freeze(WB_FIELDS.slice())', contract)
        self.assertIn('export:exportDraft', contract)
        self.assertIn('import:importDraft', contract)
        self.assertIn('setBlocked:setDraftBlocked', contract)
        ready = self.block(r'function markDraftReady\(')
        self.assertIn('window.waamDraft.ready=true', ready)
        self.assertIn('new CustomEvent("waam:draft-ready",{detail:{version:1}})', ready)
        self.assertLess(self.script.index('initializeStudioUI();'), self.script.index('markDraftReady();'))
        self.assertLess(self.script.index('window.waamPlacement=window.WaamPlacement.create'),
                        self.script.index('markDraftReady();'))

    def test_render_restores_panel_and_window_scroll_after_updates(self):
        render = self.block(r'function renderWorkbench\(')
        for capture in ('panel.scrollTop', 'window.scrollX', 'window.scrollY'):
            self.assertLess(render.index(capture), render.index('renderState(next);'))
        self.assertIn('panel.scrollTop=scrollTop;', render)
        self.assertIn('window.scrollTo(scrollX,scrollY);', render)
        self.assertLess(render.index('applyAccountControls();'),
                        render.index('panel.scrollTop=scrollTop;'))

    def test_response_rebuilds_viewer_and_panels_from_the_new_state(self):
        render = self.block(r'function renderState\(')
        self.assertIn('Object.assign(S,next);', render)
        for call in ('updateToolpath();', 'updateBounds();', 'updateDisplay();',
                     'renderClearance();', 'renderArcParams();', 'frameView(UI.view);'):
            self.assertIn(call, render)
            self.assertLess(render.index('Object.assign(S,next);'), render.index(call))
        path = self.block(r'function updateToolpath\(')
        for statement in ('M=S.toolpath.moves; N=M.length;', 'cum.length=1; totalDep=0;',
                          'slider.max=N-1;'):
            self.assertIn(statement, path)
        self.assertIn('ROB=S.robot && S.robot.skeleton ? S.robot : null;',
                      self.block(r'function updateDisplay\('))
        self.assertIn('cursor=0; slider.value=0; setPlaying(false);', render)
        self.assertIn('if(gpu) gpu.setState(S);', render)

    def test_render_reapplies_the_live_guest_account_or_pending_policy(self):
        self.assertIn('let applyAccountControls=()=>{};', self.script)
        for name in ('lockGuestControls', 'unlockAccountControls'):
            policy = self.block(r'function ' + name + r'\(')
            self.assertIn('applyAccountControls=' + name + ';', policy)
        render = self.block(r'function renderWorkbench\(')
        self.assertIn('applyAccountControls();', render)
        self.assertLess(render.index('renderState(next);'),
                        render.index('applyAccountControls();'))

        self.assertNotRegex(render, r'IS_GUEST\s*=|whoami|unlockAccountControls\(')
        self.assertNotIn('showIdentity', self.block(r'function renderState\('))

    def test_new_workbench_bindings_are_declared_before_use(self):
        body = self.block(r'if\(S.meta.workbench\)')
        self.assertEqual(_tdz_violations(dedent(body[1:-1])), [])
        for name in ('renderWorkbench', 'renderState', 'renderStlChip'):
            code = _strip_js(self.script)
            declaration = re.search(r'function (' + name + r')\(', code)
            self.assertIsNotNone(declaration)
            self.assertEqual(re.search(r'\b' + name + r'\b', code).start(),
                             declaration.start(1))


class TestPageTemplating(unittest.TestCase):


    def page(self, title="part.stl", *, hosted=False):
        state = build_state(wall(60, layers=3, layer_height_mm=3),
                            title=title, workbench=True)
        return render_studio(state, hosted=hosted)

    def test_the_app_script_is_tagged_and_no_bare_script_remains(self):


        page = self.page()
        self.assertEqual(page.count("<script>"), 0)
        self.assertEqual(page.count("<script data-app>"), 1)
        for tag in ("data-renderer", "data-library", "data-help", "data-passkeys"):
            self.assertEqual(page.count("<script " + tag + ">"), 1, tag)

    def test_hosted_wrap_survives_a_line_added_after_draw(self):


        extended = studio._TEMPLATE.replace(
            "draw();%%WRAPEND%%", "draw();\nresizeStudioView();%%WRAPEND%%")
        self.assertIn("resizeStudioView();%%WRAPEND%%", extended)
        with mock.patch.object(studio, "_TEMPLATE", extended):
            hosted = self.page(hosted=True)
            offline = self.page()
        self.assertEqual(hosted.count("(async function(){"), 1)
        self.assertEqual(hosted.count("})().catch(error=>{"), 1)
        self.assertLess(hosted.index("(async function(){"),
                        hosted.index("})().catch(error=>{"))
        self.assertNotIn("(async function(){", offline)
        self.assertNotIn("%%WRAP", hosted + offline)

    def test_hosted_init_failure_reaches_the_console_not_only_the_status_line(self):
        hosted = self.page(hosted=True)
        catch = hosted.split("})().catch(error=>{", 1)[1].split("});", 1)[0]
        self.assertIn("Preview failed: ", catch)
        self.assertIn("console.error(error)", catch)
        self.assertIn("throw error", catch)

    def test_a_filename_shaped_like_a_placeholder_is_not_substituted_again(self):


        page = self.page("%%DATA%%.stl")
        self.assertIn("<title>%%DATA%%.stl | WAAM Studio</title>", page)
        self.assertIn('<h1 class="title">%%DATA%%.stl</h1>', page)
        self.assertEqual(page.count('"toolpath":'), 1)
        options = self.page("%%FILLERS%%.stl")
        self.assertIn('<h1 class="title">%%FILLERS%%.stl</h1>', options)
        self.assertEqual(options.count("<option"), page.count("<option"))


class TestWorkspaceGenerationAndIdentity(unittest.TestCase):


    def setUp(self):
        state = build_state(wall(60.0, layers=3, layer_height_mm=3.0),
                            title="gen wb", workbench=True, workspace="alice",
                            email="owner@letu.edu")
        self.script = render_studio(state).split("<script data-app>", 1)[1].rsplit("</script>", 1)[0]

    def block(self, anchor):
        body = _block_after(self.script, anchor)
        self.assertIsNotNone(body, f"missing handler: {anchor}")
        return body

    def test_every_request_declares_the_build_it_describes(self):
        body = self.block(r"function\s+api\s*\(")
        self.assertIn('u.set("gen", S.meta.gen)', body)
        self.assertIn("S.meta.gen!=null", body,
                      "an offline/exported page has no generation to send")

    def test_the_attestation_reads_the_live_email(self):
        whoami = self.block(r"\(async\s*\(\)\s*=>")
        self.assertIn('fetch(api("/api/whoami"))', whoami)
        ok = _block_after(whoami, r"\bif\s*\(j\.ok\)")
        self.assertIsNotNone(ok, "could not find the whoami success branch")
        self.assertIn("S.meta.email=j.email||null;", ok)
        self.assertLess(ok.index("S.meta.email=j.email"), ok.index("showIdentity("),
                        "correct the identity before anything reads it")
        attest = self.block(r"async function attestPlatter\(")
        self.assertIn("S.meta.email", attest,
                      "the Checked by field still seeds from S.meta.email")
