import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.studio import build_state, render_studio
from waam_pipeline.slicer import wall
from tests.test_studio_controls import _ShellNodes, _DOM

NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is needed to execute placement controls")
class TestPlacementControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = build_state(wall(60, layers=2, layer_height_mm=3), workbench=True, violations=[])
        state["meta"].update(params={"ox": "0", "oy": "0", "yaw": "0"},
                             placement={"origin": [1014, -180, 20], "bottom_z": 0,
                                        "pivot": [50, 10], "center_offsets": [20, 40]})
        parser = _ShellNodes()
        parser.feed(render_studio(state).split("<script data-app>", 1)[0])
        cls.setup = ("const SHELL=" + json.dumps(parser.nodes) + ";\nconst INITIAL="
                     + json.dumps(state) + ";\n" + _DOM + "\nconst P=require("
                     + json.dumps(str(ROOT / "src/waam_pipeline/studio_placement.js"))
                     + ").WaamPlacement;\n" + r"""
for(const e of elements)e.dataset=Object.fromEntries(Object.entries(e.attrs)
  .filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));
let busy=false,step='place',calls=0,clearances=0,restored=null;
let xAxis=[1,0],yAxis=[0,-1],pixelRatio=1;
canvas.width=1000;canvas.height=700;
canvas.getBoundingClientRect=()=>({left:25,top:40,width:1000/pixelRatio,height:700/pixelRatio});
canvas.setPointerCapture=id=>{};
const camera={center:[10,20,30],zoom:2,span:200,panX:7,panY:9,yaw:.6,pitch:.8};
const env={document,canvas,state:()=>S,step:()=>step,busy:()=>busy,
  project:p=>[500+(p.x-50)*xAxis[0]+(p.y-10)*yAxis[0],350+(p.x-50)*xAxis[1]+(p.y-10)*yAxis[1],1],
  dpr:()=>pixelRatio,draw:()=>draws++,pause:()=>playing=false,clearance:()=>clearances++,
  camera:()=>structuredClone(camera),restoreCamera:v=>restored=v,
  apply:()=>{calls++;return true;}};
const p=P.create(env);
const event=(x,y,extra={})=>({clientX:25+x/pixelRatio,clientY:40+y/pixelRatio,
  button:0,pointerId:1,shiftKey:false,altKey:false,...extra});
function accept(request,actual=request){
  const snapshot=p.begin();
  const prior=p.actual();S.meta.params={...S.meta.params,...actual};
  S.meta.placement.origin[0]+=Number(actual.ox)-prior.ox;
  S.meta.placement.origin[1]+=Number(actual.oy)-prior.oy;
  for(const [k,id] of Object.entries({ox:'wbOx',oy:'wbOy',yaw:'wbYaw',rotX:'wbRotX',rotY:'wbRotY',lay_flat:'wbLayFlat',tilt:'wbTilt'}))el(id).value=actual[k]||0;
  p.accepted(snapshot);
}
function near(a,b){assert.ok(Math.abs(a-b)<1e-7,`${a} != ${b}`);}
""")

    def run_js(self, body):
        result = subprocess.run([NODE], input=self.setup + "\n(async()=>{\n" + body
                                + "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inverse_projection_and_nearly_edge_on_refusal(self):
        self.run_js(r"""
for(const scale of [.1,1,3])for(const yaw of [0,.7,2.1])for(const pitch of [.3,-.6,Math.PI/2]){
  const x=[Math.cos(yaw)*scale,-Math.sin(yaw)*Math.sin(pitch)*scale];
  const y=[-Math.sin(yaw)*scale,-Math.cos(yaw)*Math.sin(pitch)*scale];
  const d=P.planeDelta(17*x[0]-9*y[0],17*x[1]-9*y[1],x,y);
  near(d[0],17);near(d[1],-9);
}
assert.equal(P.planeDelta(1,1,[1,0],[0,0]),null);
assert.equal(P.planeDelta(1,1,[1,0],[0,.005]),null);
assert.equal(P.planeDelta(1,1,[.707,.004],[-.707,.004]),null);
near(P.axisDelta(10,900,[2,0]),5);
""")

    def test_axis_drag_snaps_on_client_and_submits_once_on_release(self):
        self.run_js(r"""
pixelRatio=2;el('placeSnap').value='5';
const before=JSON.stringify(S);
assert.equal(p.down(event(570,350)),true);
for(const dx of [5,12,17])p.move(event(570+dx,400));
assert.equal(calls,0);assert.equal(el('wbOx').value,'15');assert.equal(el('wbOy').value,'0');
assert.equal(JSON.stringify(S),before);assert.equal(playing,false);
assert.equal(el('clearanceBadge').textContent,'Placement preview · not checked');
assert.equal(p.up(event(587,400)),true);p.up(event(587,400));assert.equal(calls,1);
""")

    def test_cancel_second_pointer_and_busy_never_submit(self):
        self.run_js(r"""
p.down(event(500,350));p.move(event(510,330));
p.down(event(520,340,{pointerId:2}));p.cancel(true,{pointerId:2});
assert.equal(p.hasGesture(),true);assert.equal(p.move(event(0,0,{pointerId:2})),false);
p.cancel();assert.equal(el('wbOx').value,'0');assert.equal(calls,0);
p.down(event(500,350));busy=true;p.move(event(520,320));p.up(event(520,320));
assert.equal(calls,0);assert.equal(p.hasGesture(),false);
busy=false;yAxis=[0,0];assert.equal(p.down(event(500,350)),true);
assert.equal(p.hasGesture(),false);assert.match(el('placementStatus').textContent,/Top view/);
""")

    def test_rotation_pivot_preview_and_alt_free_rotation(self):
        self.run_js(r"""
p.setMode('rotate');el('rotateSnap').value='15';
p.down(event(572,350));p.move(event(500,278));
assert.equal(el('wbYaw').value,'90');assert.equal(calls,0);
assert.deepEqual(P.transform({x:100,y:10,z:7},p.preview()),{x:50,y:60,z:7});
p.cancel();p.down(event(572,350));
const a=22*Math.PI/180;p.move(event(500+72*Math.cos(a),350-72*Math.sin(a),{altKey:true}));
near(Number(el('wbYaw').value),22);p.up(event(500,278));assert.equal(calls,1);
""")

    def test_clipped_acceptance_camera_and_undo_recheck(self):
        self.run_js(r"""
accept({ox:100,oy:0,yaw:0},{ox:30,oy:0,yaw:0});
assert.equal(el('wbOx').value,'30');assert.equal(el('placeUndo').disabled,false);
assert.deepEqual(restored,{...camera,center:[-20,20,30]});
env.apply=()=>{calls++;accept(p.values());return true;};
await p.history('undo');assert.equal(calls,1);assert.equal(p.actual().ox,0);
assert.equal(el('placeRedo').disabled,false);
await p.history('redo');assert.equal(calls,2);assert.equal(p.actual().ox,30);
env.apply=()=>{calls++;return false;};await p.history('undo');
assert.equal(p.actual().ox,30);assert.equal(el('wbOx').value,'30');
assert.equal(el('placeUndo').disabled,false);assert.equal(p.isApplying(),false);
env.apply=()=>{calls++;accept(p.values());return true;};await p.history('undo');
assert.equal(p.actual().ox,0);
p.reset();p.update();assert.equal(el('placeUndo').disabled,true);assert.equal(el('placeRedo').disabled,true);
""")

    def test_numeric_preview_invalid_values_and_center_orientation(self):
        self.run_js(r"""
el('wbOx').value='12';el('wbOx').dispatch('input');assert.equal(p.preview().dx,12);
assert.equal(playing,false);
el('wbOx').value='';el('wbOx').dispatch('input');assert.equal(p.preview(),null);
assert.equal(p.pending(),true);
assert.equal(el('placementPreview').hidden,false);assert.equal(el('play').disabled,true);
p.revert();assert.equal(el('wbOx').value,'0');
el('wbYaw').value='90';el('placeCenter').onclick();assert.equal(calls,0);
assert.match(el('placementStatus').textContent,/Apply the rotation/);
p.revert();el('placeCenter').onclick();assert.equal(calls,1);
assert.equal(el('wbOx').value,'20');assert.equal(el('wbOy').value,'40');
assert.equal(el('wbYaw').value,'0');
""")

    def test_fractional_server_values_survive_revert_and_click_without_drift(self):
        self.run_js(r"""
accept({ox:10.123456789,oy:-.23456789,yaw:33.333333});
el('wbOx').value='50';p.revert();assert.equal(p.preview(),null);
assert.equal(el('wbOx').value,'10.123456789');
assert.equal(el('play').disabled,false);
p.down(event(500,350));p.up(event(500,350));
assert.equal(p.preview(),null);assert.equal(calls,0);
""")

    def test_face_inputs_submit_once_and_undo_redo_keep_orientation(self):
        self.run_js(r"""
S.meta.stl={size_mm:[30,18,6]};p.update();
assert.equal(el('placeLayFlat').disabled,false);
el('wbRotX').value='70';el('wbRotX').dispatch('input');
assert.equal(calls,0);assert.equal(p.pending(),true);assert.equal(p.preview(),null);
assert.equal(el('play').disabled,true);assert.match(el('placementPreview').textContent,/snap to a face/);
el('placeCenter').onclick();assert.equal(calls,0);
env.apply=()=>{calls++;accept(p.values());return true;};
el('wbOz').value='8';el('wbRotX').dispatch('change');
assert.equal(calls,1);assert.equal(el('wbOz').value,'0');assert.equal(p.actual().rotX,70);
await p.history('undo');assert.equal(p.actual().rotX,0);assert.equal(el('wbRotX').value,'0');
await p.history('redo');assert.equal(p.actual().rotX,70);
await el('placeLayFlat').onclick();assert.equal(p.actual().lay_flat,1);
await el('placeResetOrientation').onclick();assert.equal(p.actual().lay_flat,0);assert.equal(p.actual().rotX,0);
await p.history('undo');assert.equal(p.actual().rotX,70);assert.equal(p.actual().lay_flat,1);
const prior=calls;el('wbRotY').value='361';el('wbRotY').dispatch('change');assert.equal(calls,prior);
p.revert();busy=true;await el('placeLayFlat').onclick();assert.equal(calls,prior);
""")

    def test_demo_has_no_faces_and_failed_orientation_retains_draft_until_revert(self):
        self.run_js(r"""
assert.equal(el('placeLayFlat').disabled,true);await el('placeLayFlat').onclick();assert.equal(calls,0);
S.meta.stl={size_mm:[30,18,6]};p.update();el('wbRotY').value='80';
const snapshot=p.begin();p.rejected();
assert.equal(el('wbRotY').value,'80');assert.equal(p.pending(),true);
assert.equal(el('placeUndo').disabled,true);assert.equal(el('play').disabled,true);
p.revert();assert.equal(el('wbRotY').value,'0');assert.equal(p.pending(),false);
assert.equal(el('play').disabled,false);
""")
