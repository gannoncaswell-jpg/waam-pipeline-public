import json
import math
import subprocess
import unittest

from tests.test_studio_controls import NODE, ROOT
from waam_pipeline.slicer import wall
from waam_pipeline.studio import build_state, render_studio


def run_js(scenario: str) -> None:
    source = (
        "const assert=require('node:assert/strict');\n"
        "const scale=require(" + json.dumps(str(ROOT / "src/waam_pipeline/studio_scale.js")) + ");\n"
        "const near=(a,b,tol=1e-9)=>assert.ok(Math.abs(a-b)<=tol,`${a} != ${b}`);\n" + scenario
    )
    result = subprocess.run([NODE], input=source, encoding="utf-8", capture_output=True, timeout=20)
    if result.returncode:
        raise AssertionError(result.stderr)


@unittest.skipUnless(NODE, "Node.js is required for the scale bar tests")
class TestScaleFrame(unittest.TestCase):
    def test_platter_rulers_start_at_the_faceplate_end_and_the_robot_side_edge(self):
        run_js(r"""
const S={scene:{boxes:[{name:"manpro_training_block",min:[0,0,0],max:[1,1,1]},
  {name:"charles_platter",min:[-250,-460,-6.35],max:[164.3,216,0]}]}};
const f=scale.frame(S);
assert.equal(f.kind,"platter");
assert.deepEqual(f.origin,[-250,-460,0]);          // (min X, min Y) on the top face
assert.deepEqual([f.a,f.b],[[0,1,0],[1,0,0]]);   // along robot +Y, across robot +X
near(f.lenA,676);near(f.lenB,414.3);
const e=scale.extents([[-100,-60,1],[20,-60,1]],f);
assert.deepEqual(e,{a:[400,400],b:[150,270]});
assert.equal(scale.summary(f,e),"Weld path on the platter: 400.0–400.0 mm along platter from "+
  "faceplate end; 150.0–270.0 mm across platter from robot-side edge.");
""")

    def test_substrate_rulers_measure_in_the_plate_plane_from_corner_one(self):


        yaw, tilt = math.radians(20), math.radians(3.5)
        a = (math.cos(yaw), math.sin(yaw), 0.0)
        b = (-math.sin(yaw) * math.cos(tilt), math.cos(yaw) * math.cos(tilt), math.sin(tilt))
        c1 = (1000.0, -200.0, 170.0)
        corner = lambda s, t: [c1[k] + a[k] * s + b[k] * t for k in range(3)]
        corners = [corner(0, 0), corner(160, 0), corner(160, 80), corner(0, 80)]
        part = [corner(30, 30), corner(90, 60)]
        origin = [990.0, -150.0, 160.0]
        shift = lambda p: [p[k] - origin[k] for k in range(3)]
        state = {"meta": {"substrate": {"points_robot_mm": corners, "origin_robot_mm": origin}},
                 "scene": {"boxes": [{"name": "charles_platter", "min": [0, 0, 0], "max": [1, 1, 1]}]}}
        reversed_state = {"meta": {"substrate": {"points_robot_mm": [corners[0], corners[1], corners[2], corner(0, 80)],
                                                 "origin_robot_mm": origin}}}
        run_js(f"""
const S={json.dumps(state)}, part={json.dumps([shift(p) for p in part])};
const f=scale.frame(S);
assert.equal(f.kind,"substrate","a measured plate wins over the platter under it");
near(f.lenA,160,1e-9);near(f.lenB,80,1e-9);
const e=scale.extents(part,f);
near(e.a[0],30,1e-9);near(e.a[1],90,1e-9);near(e.b[0],30,1e-9);near(e.b[1],60,1e-9);
assert.match(scale.summary(f,e),/30.0–90.0 mm along edge 1→2 from corner 1; 30.0–60.0 mm square off edge 1→2/);
// Corner 4 decides which way "off the edge" is, whichever way the plate was walked.
const g=scale.frame({json.dumps(reversed_state)});
near(scale.extents(part,g).b[0],30,1e-9);
""")

    def test_mirrored_corner_order_still_measures_toward_corner_four(self):
        run_js(r"""

const S={meta:{substrate:{origin_robot_mm:[0,0,0],
  points_robot_mm:[[0,0,0],[100,0,0],[100,-50,0],[0,-50,0]]}}};
const f=scale.frame(S);
near(f.lenB,50);assert.deepEqual(f.b.map(v=>Math.round(v)),[0,-1,0]);
assert.deepEqual(scale.extents([[10,-20,0]],f),{a:[10,10],b:[20,20]});
""")

    def test_no_surface_no_rulers(self):
        run_js(r"""
assert.equal(scale.frame({}),null);
assert.equal(scale.frame({scene:{boxes:[{name:"hc_table",min:[0,0,0],max:[1,1,1]}]}}),null,
  "the HC cell is planning-only and has no measured surface");
assert.equal(scale.frame({meta:{substrate:{origin_robot_mm:[0,0,0],
  points_robot_mm:[[0,0,0],[0,0,0],[1,1,0],[0,1,0]]}}}),null,"degenerate corners");
assert.equal(scale.extents([],{origin:[0,0,0],a:[1,0,0],b:[0,1,0]}),null);
assert.equal(scale.summary(null,null),"");
""")

    def test_tick_spacing_keeps_labels_apart_at_any_zoom(self):
        run_js(r"""
for(const px of [0.05,0.3,1,4,20]){
  const t=scale.ticks(px,42);
  if(!t)continue;
  assert.ok(t.major*px>=42,`labels ${t.major} mm apart at ${px} px/mm`);
  assert.ok(t.major%t.minor===0);
}
assert.deepEqual(scale.ticks(1,42),{major:50,minor:10});
assert.equal(scale.ticks(0,42),null);
""")

    def test_drawing_never_overlaps_two_labels(self):
        run_js(r"""
const S={scene:{boxes:[{name:"charles_platter",min:[-250,-460,-6.35],max:[164.3,216,0]}]}};
const f=scale.frame(S), e=scale.extents([[-100,-60,1],[20,-60,1]],f);
for(const pxPerMm of [0.2,0.6,2.5]){
  const labels=[];
  const ctx={save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},arc(){},fill(){},
    setLineDash(){},measureText:t=>({width:6*t.length}),
    fillRect(x,y,w,h){labels.push([x,y,x+w,y+h]);},fillText(){}};

  const proj=p=>[600+(p.x*.9-p.y*.4)*pxPerMm,400-(p.x*.3+p.y*.5+p.z)*pxPerMm,pxPerMm,0];
  scale.draw(ctx,proj,f,e,{dpr:1});
  assert.ok(labels.length>=2,"the two tape measurements are always labelled");
  for(let i=0;i<labels.length;i++)for(let j=i+1;j<labels.length;j++){
    const [p,q]=[labels[i],labels[j]];
    assert.ok(!(p[0]<q[2] && q[0]<p[2] && p[1]<q[3] && q[1]<p[3]),`labels ${i} and ${j} overlap at ${pxPerMm}`);
  }
}
""")


class TestScaleBarsInThePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = render_studio(build_state(wall(40, layers=2, layer_height_mm=3), workbench=True,
                                             workspace="scale-test", request_params={"cell_profile": "lc50s"}))

    def test_page_ships_the_module_toggle_and_readout(self):
        self.assertIn("<script data-scale>", self.page)
        self.assertIn("root.WaamScale=api", self.page)
        self.assertIn('id="showScale" checked', self.page)
        self.assertIn('id="placeTape"', self.page)
        self.assertEqual(self.page.count("drawScaleBars(placed);"), 2, "both the GPU and 2D paths draw them")

    def test_path_only_preference_hides_them_with_the_surface(self):
        self.assertIn('"showScene","showScale",', self.page)


if __name__ == "__main__":
    unittest.main()
