import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.studio_renderer import renderer_source
from waam_pipeline.studio import _TEMPLATE

NODE = shutil.which("node")
SOURCE = ROOT / "src" / "waam_pipeline"


@unittest.skipUnless(NODE, "Node is needed to execute the display geometry")
class TestRendererGeometry(unittest.TestCase):
    def run_js(self, body):
        setup = (
            "const assert=require('node:assert/strict');\n"
            f"require({json.dumps(str(SOURCE / 'studio-renderer.js'))});\n"
            f"const T=require({json.dumps(str(SOURCE / '_vendor' / 'three-r180.cjs'))});\n"
            "const {creaseNormals,cameraFrame,linkMatrix}=WaamRenderer.math;\n"
            "function near(a,b,e=1e-6){assert.ok(Math.abs(a-b)<e,`${a} != ${b}`);}\n"
        )
        result = subprocess.run([NODE], input=setup + body, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cylinder_is_smooth_at_seam_but_caps_remain_sharp(self):
        self.run_js("""
const positions=[], sides=[];
const point=(i,z)=>[Math.cos(i*Math.PI/8),Math.sin(i*Math.PI/8),z];
for(let i=0;i<16;i++){
  const a=point(i,0),b=point(i+1,0),c=point(i,1),d=point(i+1,1);
  for(const [triangle,kind] of [[[a,b,d],0],[[a,d,c],0],[[[0,0,0],b,a],-1],[[[0,0,1],c,d],1]]){
    positions.push(...triangle.flat()); sides.push(kind);
  }
}
const before=JSON.stringify(positions), normals=creaseNormals(positions,48);
assert.equal(JSON.stringify(positions),before,'Shading must not modify geometry');
for(let f=0;f<sides.length;f++)for(let corner=0;corner<3;corner++){
  const i=f*9+corner*3;
  if(sides[f]){near(normals[i],0);near(normals[i+1],0);near(normals[i+2],sides[f]);}
  else{near(normals[i],positions[i]);near(normals[i+1],positions[i+1]);near(normals[i+2],0);}
}
""")

    def test_material_boundary_and_sharp_fold_are_not_smoothed(self):
        self.run_js("""
const a=Math.PI/6, positions=[0,0,0,1,0,0,0,1,0, 0,0,0,1,0,0,0,Math.cos(a),Math.sin(a)];
const smooth=creaseNormals(positions,48), split=creaseNormals(positions,48,[{start:1,end:2}]);
assert.ok(smooth[1]<-.1 && smooth[2]<1,'Shared curved surface must be smoothed');
near(split[0],0);near(split[1],0);near(split[2],1);
near(split[9],0);near(split[10],-Math.sin(a));near(split[11],Math.cos(a));
const folded=creaseNormals([0,0,0,1,0,0,0,1,0, 0,0,0,1,0,0,0,0,1],48);
near(folded[1],0);near(folded[2],1);near(folded[10],-1);near(folded[11],0);
const degenerate=creaseNormals([0,0,0,0,0,0,0,0,0]);
assert.ok(Array.from(degenerate).every(Number.isFinite));
""")

    def test_webgl_camera_matches_the_existing_projection_with_pan(self):
        self.run_js("""
for(const [yaw,pitch] of [[-.85,.42],[.7,-.6],[2.1,.8],[0,0]]){
  const f={yaw,pitch,width:1440,height:900,zoom:1.7,span:3200,center:[200,-400,50],panX:87,panY:-31};
  const v=cameraFrame(f),c=new T.OrthographicCamera(-f.width/(2*v.scale),f.width/(2*v.scale),f.height/(2*v.scale),-f.height/(2*v.scale),.1,50000);
  c.position.set(...v.target).addScaledVector(new T.Vector3(...v.depth),-f.span*3);
  c.up.set(...v.up);c.lookAt(new T.Vector3(...v.target));c.updateMatrixWorld();
  for(const p of [[1200,-300,680],[-100,-900,-600],[0,0,0]]){
    const q=new T.Vector3(...p).project(c),dx=p[0]-200,dy=p[1]+400,dz=p[2]-50;
    const x=dx*Math.cos(yaw)-dy*Math.sin(yaw),y=dx*Math.sin(yaw)+dy*Math.cos(yaw);
    near((q.x+1)*f.width/2,f.width/2+f.panX+x*v.scale);
    near((1-q.y)*f.height/2,f.height/2+f.panY-(y*Math.sin(pitch)+dz*Math.cos(pitch))*v.scale);
  }
}
""")

    def test_gpu_link_matrix_keeps_parent_offset_and_part_origin(self):
        self.run_js("""
const angle=.71,c=Math.cos(angle),s=Math.sin(angle);
const frame={R:[c,0,s,0,1,0,-s,0,c],p:[148,614,-282]},off=[30.5,-6.2,91.8],base=[-517,250,-93];
const matrix=new T.Matrix4().set(...linkMatrix(frame,off,base));
for(const p of [[1,2,3],[-260,115,74],[0,0,0]]){
  const q=new T.Vector3(...p).applyMatrix4(matrix);
  const x=p[0]+off[0],y=p[1]+off[1],z=p[2]+off[2];
  near(q.x,frame.p[0]+base[0]+c*x+s*z);
  near(q.y,frame.p[1]+base[1]+y);
  near(q.z,frame.p[2]+base[2]-s*x+c*z);
}
""")

    def test_perspective_and_ortho_match_cpu_through_target_crossing(self):
        projection = "\n".join(re.search(r"function " + name + r"\([^)]*\)\{.*?\n\}",
                                        _TEMPLATE, re.S).group()
                              for name in ("cameraScale", "cameraEyeDistance", "proj"))
        self.run_js(projection + """
global.waamCollisionIndex=()=>({events:[],byMove:new Map(),segments:new Map()});
class Renderer extends WaamRenderer.StudioRenderer{
  makeEnvironment(){this.environment={dispose(){}};}
}
const backend={domElement:{dataset:{},addEventListener(){},removeEventListener(){}},
  shadowMap:{},setSize(){},render(){},dispose(){}};
const state={meta:{params:{}},scene:{meshes:{}},robot:null,
  toolpath:{moves:[{x:0,y:0,z:0,d:0}]},torch:{},violations:[]};
const r=new Renderer(T,backend,state),canvas={width:1000,height:700};
let cx=120,cy=-200,cz=30,span=3200,yaw=0,pitch=0,zoom=1.45,panX=0,panY=0,perspective=false,dolly=0;
for(const size of [[1400,800],[400,900]])for(const angles of [[-.85,.32],[0,Math.PI/2],[.7,-.6]]){
  [canvas.width,canvas.height]=size;[yaw,pitch]=angles;
  for(const mode of [false,true])for(const travel of [-1,0,.9,1,1.2,3]){
    perspective=mode;dolly=travel*cameraEyeDistance();
    const f={...canvas,center:[cx,cy,cz],span,yaw,pitch,zoom,panX,panY,perspective,dolly,flags:{}};
    r.render(f);r.camera.updateMatrixWorld();
    assert.equal(!!r.camera.isPerspectiveCamera,mode);
    const view=cameraFrame(f),direction=r.camera.getWorldDirection(new T.Vector3());
    view.depth.forEach((value,k)=>near(direction.getComponent(k),value));
    view.eye.forEach((value,k)=>near(r.camera.position.getComponent(k),value));
    const nearEye=view.eye.map((v,i)=>v+view.depth[i]*view.near/2);
    const points=[[250,430,120],[cx,cy,cz],nearEye];
    for(const distance of [-span,0,span*.01,span,span*3])
      points.push(view.eye.map((v,i)=>v+view.depth[i]*distance+view.right[i]*20+view.up[i]*10));
    for(const p of points){
      const projected=proj({x:p[0],y:p[1],z:p[2]}),q=new T.Vector3(...p).project(r.camera);
      const distance=p.reduce((sum,v,k)=>sum+(v-view.eye[k])*view.depth[k],0);
      if(mode && distance<view.near){assert.equal(projected[2],0);continue;}
      assert.ok(projected.every(Number.isFinite));
      near((q.x+1)*canvas.width/2,projected[0],1e-5);
      near((1-q.y)*canvas.height/2,projected[1],1e-5);
    }
    if(mode){
      near(r.camera.fov,50);assert.ok(r.camera.near<=span*.001);
      if(travel===1)near(r.camera.near,span*1e-6);
    }
  }
}
r.dispose();
""")

    def test_hc_torch_and_tcp_follow_the_flange_in_both_renderers(self):
        cpu = re.search(r"function robotFlangePoint\([^)]*\)\{.*?\n\}", _TEMPLATE, re.S).group()
        self.run_js(cpu + """
global.waamCollisionIndex=()=>({events:[],byMove:new Map(),segments:new Map()});
class Renderer extends WaamRenderer.StudioRenderer{
  makeEnvironment(){this.environment={dispose(){}};}
}
const backend={domElement:{dataset:{},addEventListener(){},removeEventListener(){}},
  shadowMap:{},setSize(){},render(){},dispose(){}};
const triangles=[[[0,0,0],[300,5,7],[100,-3,12]]],base=[120,-340,70];
const robot={torch_mesh_flange:triangles,tcp_flange:[304.681,-2.04,-2.758],base_offset:base,skeleton:[]};
const state={meta:{params:{}},scene:{meshes:{}},robot,
  toolpath:{moves:[{x:0,y:0,z:0,d:0}]},torch:{available:false},violations:[]};
const r=new Renderer(T,backend,state),geometry=r.torchMesh.geometry,original=JSON.stringify(state);
const frame={width:1000,height:700,center:[0,0,0],span:2000,yaw:0,pitch:1,zoom:1,
  panX:0,panY:0,cursor:0,flags:{robot:true}};
for(const flange of [{R:[1,0,0,0,1,0,0,0,1],p:[20,30,40]},
                     {R:[0,-1,0,1,0,0,0,0,1],p:[230,-180,740]}]){
  r.render({...frame,frames:Array(7).fill(flange)});
  assert.ok(r.torchMesh.visible);assert.ok(r.tcpMarker.visible);
  for(const p of triangles[0]){
    const q=new T.Vector3(...p).applyMatrix4(r.torchMesh.matrix);
    robotFlangePoint(p,flange,base).forEach((v,k)=>near(q.getComponent(k),v));
  }
  robotFlangePoint(robot.tcp_flange,flange,base).forEach((v,k)=>near(r.tcpMarker.position.getComponent(k),v));
}
r.render({...frame,frames:null});assert.equal(r.torchMesh.visible,false);assert.equal(r.tcpMarker.visible,false);
r.render({...frame,frames:Array(7).fill({R:[1,0,0,0,1,0,0,0,1],p:[0,0,0]}),flags:{robot:false}});
assert.equal(r.torchMesh.visible,false);assert.equal(r.tcpMarker.visible,false);
assert.equal(r.torchMesh.geometry,geometry);assert.equal(JSON.stringify(state),original);r.dispose();
""")

    def test_measured_substrate_keeps_torch_overlay_at_commanded_tcp(self):
        cpu = re.search(r"function drawTorch\([^)]*\)\{.*?\n\}", _TEMPLATE, re.S).group()
        self.run_js(cpu + """
global.waamCollisionIndex=()=>({events:[],byMove:new Map(),segments:new Map()});
class Renderer extends WaamRenderer.StudioRenderer{
  makeEnvironment(){this.environment={dispose(){}};}
}
const backend={domElement:{dataset:{},addEventListener(){},removeEventListener(){}},
  shadowMap:{},setSize(){},render(){},dispose(){}};
const state={meta:{params:{}},scene:{meshes:{}},robot:null,
  toolpath:{moves:[{x:0,y:0,z:13,d:0},{x:100,y:0,z:13,d:1}]},
  torch:{spheres:[[7,1]],axis:[0,0,1],command_from_physical_z_mm:-10},violations:[]};
const r=new Renderer(T,backend,state);
r.render({width:1000,height:700,center:[0,0,0],span:200,yaw:0,pitch:1,zoom:1,
  panX:0,panY:0,cursor:.5,flags:{torch:true}});
near(r.tip.position.z,13);near(r.torch.position.z,3);
near(r.tip.position.x,50);near(r.torch.position.x,50);
const S=state,collisionData={byMove:new Map()},cursor=0,seen=[];
const selectedEvent=()=>null,proj=p=>{seen.push(p);return [0,0,1];};
const ctx={save(){},restore(){},beginPath(){},arc(){},fill(){}};
drawTorch({x:50,y:0,z:13});near(seen[0].z,10);
assert.equal(state.toolpath.moves[0].z,13);r.dispose();
""")

    def test_embedded_dependency_and_renderer_execute_without_imports(self):
        source = "global.window=globalThis;\n" + renderer_source()
        source += "\nprocess.stdout.write(JSON.stringify({revision:WaamThree.REVISION,api:typeof WaamRenderer.create}));"
        result = subprocess.run([NODE], input=source, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"revision": "180", "api": "function"})

    def test_placement_moves_cached_geometry_and_restores_checked_colors(self):
        self.run_js("""

global.waamCollisionIndex=()=>({events:[],byMove:new Map(),segments:new Map([[1,{color:'#ff0000'}]])});
class Renderer extends WaamRenderer.StudioRenderer{
  makeEnvironment(){this.environment={dispose(){}};}
}
const backend={domElement:{dataset:{},addEventListener(){},removeEventListener(){}},
  shadowMap:{},setSize(){},render(){},dispose(){}};
const state={meta:{params:{}},scene:{meshes:{}},part:{tris:[50,10,0,100,10,0,50,20,0]},robot:null,
  toolpath:{moves:[{x:50,y:10,z:3,d:0},{x:100,y:10,z:3,d:1}]},torch:{},violations:[]};
const r=new Renderer(T,backend,state),original=JSON.stringify(state),geometry=r.part.geometry;
const frame={width:1000,height:700,center:[50,10,0],span:200,yaw:0,pitch:1,zoom:1,
  panX:0,panY:0,cursor:.5,flags:{ghost:true,torch:true}};
r.render(frame);const colors=r.deposits.instanceColor.array.slice();
r.render({...frame,placementPreview:{dx:12,dy:-5,angle:90,pivot:[50,10]}});
r.placementGroup.updateMatrixWorld(true);
const point=new T.Vector3(100,10,3).applyMatrix4(r.placementGroup.matrixWorld);
near(point.x,62);near(point.y,55);near(point.z,3);
assert.equal(r.part.geometry,geometry);assert.equal(JSON.stringify(state),original);
assert.notDeepEqual(r.deposits.instanceColor.array,colors);
assert.equal(r.partial.material.color.getHex(),0x3b82a8);
assert.equal(r.markers.visible,false);assert.equal(r.torch.visible,false);
r.render(frame);assert.deepEqual(r.deposits.instanceColor.array,colors);
assert.equal(r.partial.material.color.getHex(),0xff0000);
assert.equal(r.part.geometry,geometry);assert.equal(r.torch.visible,true);
r.dispose();
""")


    def test_tilted_substrate_preview_matches_cpu_and_resets(self):
        self.run_js("const P=require(" + json.dumps(str(SOURCE / 'studio_placement.js')) + ").WaamPlacement;\n" + """
global.waamCollisionIndex=()=>({events:[],byMove:new Map(),segments:new Map()});
class Renderer extends WaamRenderer.StudioRenderer{
  makeEnvironment(){this.environment={dispose(){}};}
}
const backend={domElement:{dataset:{},addEventListener(){},removeEventListener(){}},
  shadowMap:{},setSize(){},render(){},dispose(){}};
const state={meta:{params:{}},scene:{meshes:{}},robot:null,
  toolpath:{moves:[{x:0,y:0,z:3,d:0},{x:100,y:0,z:3,d:1}]},torch:{},violations:[]};
const r=new Renderer(T,backend,state);
const frame={width:1000,height:700,center:[0,0,0],span:200,yaw:0,pitch:1,zoom:1,
  panX:0,panY:0,cursor:.5,flags:{}};
const q=new T.Quaternion().setFromEuler(new T.Euler(.035,-.045,.63));
const basis=[[1,0,0],[0,1,0],[0,0,1]].map(a=>new T.Vector3(...a).applyQuaternion(q).toArray());
for(const angle of [-90,-15,0,45,180]){
  const preview={dx:12,dy:-5,angle,pivot:[50,10,3],plane_basis:basis};
  r.render({...frame,placementPreview:preview});r.placementGroup.updateMatrixWorld(true);
  for(const p of [{x:100,y:10,z:3},{x:-20,y:12,z:8},{x:50,y:10,z:3}]){
    const actual=new T.Vector3(p.x,p.y,p.z).applyMatrix4(r.placementGroup.matrixWorld);
    const expected=P.transform(p,preview);
    near(actual.x,expected.x);near(actual.y,expected.y);near(actual.z,expected.z);
  }
}
r.render(frame);r.placementGroup.updateMatrixWorld(true);
assert.ok(r.placementGroup.matrixWorld.equals(new T.Matrix4()));r.dispose();
""")


class TestVendoredRenderer(unittest.TestCase):
    def test_runtime_matches_the_pinned_public_build(self):
        expected = {
            "three-r180.cjs": "f9d60495587588349a2860d455e40dd395e42bd5edc27386bb79d9d7622a34a7",
            "THREE-LICENSE.txt": "bfe119ea4fd413f5f7ca3fcd63adb0c4a073ed39daa2fe7d3e6b769e21272601",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((SOURCE / "_vendor" / name).read_bytes()).hexdigest(), digest)
