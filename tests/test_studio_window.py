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
  const props={};
  node.style={setProperty(k,v){props[k]=String(v);},removeProperty(k){delete props[k];},
    getPropertyValue(k){return props[k]??'';}};
}
document.body=elements.find(node=>node.tag==='body');
const values=new Map();
const storage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))};
let resizes=0;
const make=(extra={})=>windowModule.create({document,storage,viewport:()=>1600,
  resize:()=>resizes++,...extra});
function fire(node,type,extra={}){
  const event={target:node,button:0,pointerId:1,defaultPrevented:false,
    preventDefault(){this.defaultPrevented=true;},...extra};
  node['on'+type]?.(event);return event;
}
const aside=document.querySelector('aside'),splitter=el('sideSplitter'),body=document.body;
aside.getBoundingClientRect=()=>({width:350});
"""


@unittest.skipUnless(NODE, "Node.js is required for the studio window tests")
class StudioWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = build_state(wall(40, layers=2, layer_height_mm=3),
                                workbench=True, workspace="window-test",
                                request_params={"cell_profile": "lc50s"})
        cls.page = render_studio(cls.state)
        parser = _ShellNodes()
        parser.feed(cls.page.split("<script data-app>", 1)[0])
        cls.shell = parser.nodes

    def run_js(self, scenario):
        source = (
            "const SHELL=" + json.dumps(self.shell) + ";\n"
            "const INITIAL=" + json.dumps(self.state) + ";\n" + _DOM + "\n"
            + "const windowModule=require("
            + json.dumps(str(ROOT / "src/waam_pipeline/studio_window.js")) + ");\n"
            + _SETUP + "\n" + scenario
        )
        result = subprocess.run([NODE], input=source, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_ships_the_module_and_its_controls(self):
        self.assertIn("<script data-window>", self.page)
        self.assertIn("root.WaamWindow=api", self.page)
        self.assertIn('window.waamWindow=window.WaamWindow.create(', self.page)
        for marker in ('id="sideSplitter"', 'role="separator"', 'id="layoutWidth"',
                       'id="layoutScale"', 'id="layoutReset"', 'id="inspectorToggle"',
                       'aria-controls="buildInspector"', 'id="buildInspector"'):
            self.assertIn(marker, self.page)

    def test_default_layout_leaves_each_look_in_charge(self):
        self.run_js(r"""
const w=make();
assert.deepEqual(w.state(),{width:null,scale:1,inspector:true});
assert.equal(body.dataset.sideWidth,undefined,'No width override until the user picks one');
assert.equal(body.dataset.wbScaled,undefined);
assert.equal(body.dataset.inspector,'shown');assert.equal(aside.hidden,false);
assert.equal(splitter.getAttribute('aria-valuenow'),'350');
assert.equal(el('layoutWidthValue').textContent,'Default');
assert.equal(values.size,0,'Loading must not write storage');
""")

    def test_width_clamps_persists_and_resets(self):
        self.run_js(r"""
let w=make();
w.setWidth(5000);
assert.equal(w.state().width,760);
assert.equal(body.style.getPropertyValue('--side-w'),'760px');
assert.equal(body.dataset.sideWidth,'custom');
assert.equal(splitter.getAttribute('aria-valuemax'),'760');
w.setWidth(10);assert.equal(w.state().width,280);
w.setWidth(Number.NaN);assert.equal(w.state().width,null,'A non-number restores the look default');
w.setWidth(512.4);
assert.equal(JSON.parse(values.get(windowModule.KEY)).width,512);
w=make();assert.equal(w.state().width,512,'Width survives a reload');
w.reset();
assert.deepEqual(w.state(),windowModule.DEFAULTS);
assert.equal(body.style.getPropertyValue('--side-w'),'');
assert.ok(resizes>0,'Fallback resize runs when the layout changes');
""")

    def test_splitter_drags_and_keys_resize_the_panel(self):
        self.run_js(r"""
const w=make();
fire(splitter,'pointerdown',{clientX:1000});
assert.equal(body.dataset.resizing,'true');
fire(splitter,'pointermove',{clientX:900});
assert.equal(w.state().width,450,'Dragging the edge left widens the panel');
assert.equal(values.size,0,'Mid-drag widths are not written to storage');
fire(splitter,'pointerup',{clientX:900});
assert.equal(body.dataset.resizing,undefined);
assert.equal(JSON.parse(values.get(windowModule.KEY)).width,450);
fire(splitter,'keydown',{key:'ArrowRight'});assert.equal(w.state().width,430);
fire(splitter,'keydown',{key:'ArrowLeft',shiftKey:true});assert.equal(w.state().width,510);
fire(splitter,'keydown',{key:'Home'});assert.equal(w.state().width,280);
fire(splitter,'keydown',{key:'End'});assert.equal(w.state().width,760);
const narrow=windowModule.create({document,storage,viewport:()=>900});
fire(splitter,'keydown',{key:'End'});
assert.equal(narrow.state().width,540,'A narrow window caps the panel at 60% of the viewport');
fire(splitter,'dblclick');assert.equal(narrow.state().width,null);
""")

    def test_hiding_and_text_size(self):
        self.run_js(r"""
const w=make();
fire(el('inspectorToggle'),'click');
assert.equal(w.state().inspector,false);
assert.equal(aside.hidden,true);assert.equal(splitter.hidden,true);
assert.equal(body.dataset.inspector,'hidden');
assert.equal(el('inspectorToggle').getAttribute('aria-pressed'),'true');
assert.equal(el('inspectorToggle').getAttribute('aria-label'),'Show build panel');
const textToggle=document.querySelector('[data-inspector-toggle="text"]');
assert.equal(textToggle.textContent,'Show build panel');
w.showInspector();assert.equal(aside.hidden,false);
el('layoutScale').value='1.3';fire(el('layoutScale'),'change');
assert.equal(w.state().scale,1.3);
assert.equal(body.style.getPropertyValue('--wb-scale'),'1.3');
assert.equal(body.dataset.wbScaled,'true');
w.setScale('2');assert.equal(w.state().scale,1,'Only the offered sizes apply');
""")

    def test_escape_closes_popover_menus_without_reaching_the_build(self):
        self.run_js(r"""
make();
for(const id of ['layoutMenu','layersMenu']){
  const menu=el(id);menu.open=true;
  const event={key:'Escape',stopped:false,preventDefault(){},stopPropagation(){this.stopped=true;}};
  for(const fn of menu.listeners.keydown)fn(event);
  assert.equal(menu.open,false,id+' closes on Escape');
  assert.equal(event.stopped,true,'Escape must not also revert a placement');
  assert.equal(document.activeElement,menu.querySelector('summary'));
}
""")

    def test_corrupt_or_unavailable_storage_falls_back_to_defaults(self):
        self.run_js(r"""
values.set(windowModule.KEY,'{not json');
let w=make();assert.deepEqual(w.state(),windowModule.DEFAULTS);
values.set(windowModule.KEY,JSON.stringify({width:'9999',scale:7,inspector:'no',extra:1}));
w=make();assert.deepEqual(w.state(),windowModule.DEFAULTS);
const broken={getItem(){throw new Error('denied');},setItem(){throw new Error('denied');}};
w=windowModule.create({document,storage:broken,viewport:()=>1600});
w.setWidth(400);assert.equal(w.state().width,400,'Applies for this visit without storage');
""")


if __name__ == "__main__":
    unittest.main()
