import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_hc, cell_lc as lc
from waam_pipeline.studio import build_state, render_studio
from waam_pipeline.toolpath import Move, Toolpath


class TestStudioGeometry(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute viewer geometry")
    def test_outward_faces_are_visible_from_their_front_side(self):
        page = render_studio(build_state(Toolpath([Move(0, 0, 0, None, False, 0, 0)])))
        functions = []
        for name in ("cameraScale", "cameraEyeDistance", "proj", "addFace", "wireFace"):
            functions.append(re.search(r"function " + name + r"\([^\n]*\)\{.*?\n\}",
                                       page, flags=re.S).group())
        setup = """
const canvas={width:800,height:600}, cx=0,cy=0,cz=0,span=10;
let yaw=0,pitch=0,zoom=1,panX=0,panY=0,perspective=false,dolly=0;
let lines=0;
const ctx={moveTo(){},lineTo(){lines++},closePath(){}};
"""
        check = """
const triangles=[
  [{x:0,y:0,z:0},{x:1,y:0,z:0},{x:0,y:1,z:0},[0,0,1]],
  [{x:0,y:0,z:0},{x:0,y:0,z:1},{x:1,y:0,z:0},[0,1,0]],
  [{x:0,y:0,z:0},{x:0,y:1,z:0},{x:0,y:0,z:1},[1,0,0]]
];
let checks=0;
for(const angles of [[-.85,.42],[.7,-.6],[2.1,.8]]){
  [yaw,pitch]=angles;
  const eye=[-Math.sin(yaw)*Math.cos(pitch),-Math.cos(yaw)*Math.cos(pitch),Math.sin(pitch)];
  for(const [p0,p1,p2,n] of triangles){
    for(const reverse of [false,true]){
      const visible=(n.reduce((s,v,i)=>s+v*eye[i],0)*(reverse?-1:1))>0;
      const a=reverse?p2:p1,b=reverse?p1:p2;
      const faces=[]; lines=0;
      addFace(faces,p0,a,b,[0,0,50,20,1],null,false);
      wireFace(p0,a,b,false);
      if(faces.length!==Number(visible) || lines!==2*Number(visible))
        throw new Error('Culling disagrees with outward normal at '+angles);
      const shell=[];
      addFace(shell,p0,a,b,[0,0,50,20,1],null,true);
      if(shell.length!==1) throw new Error('Two-sided shells must remain visible');
      checks++;
    }
  }
}
process.stdout.write(JSON.stringify({checks}));
"""
        result = subprocess.run([shutil.which("node"), "-e", setup + "\n".join(functions) + check],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["checks"], 18)


class TestLcCleanerGeometry(unittest.TestCase):
    def asset(self):
        try:
            mesh = cell_hc._furniture().get(lc.CLEANER_MODEL)
        except (OSError, ValueError) as exc:
            self.skipTest(f"HC cleaner asset unavailable: {exc}")
        if mesh is None or len(mesh) == 0:
            self.skipTest("HC cleaner asset unavailable")
        return mesh

    def test_cleaner_bore_maps_to_lc_working_point(self):
        native = self.asset()
        original = native.copy()
        mesh = lc.cleaner_mesh()

        self.assertEqual(len(mesh), len(native) - 132)
        self.assertTrue(np.isfinite(mesh).all())
        np.testing.assert_allclose(lc._place_cleaner(lc.CLEANER_SOURCE_BORE), lc.CLEANER_BORE_POINT)

        np.testing.assert_allclose(lc._place_cleaner(lc.CLEANER_SOURCE_BORE)[:2],
                                   [151.689, -620.076], atol=.05)
        np.testing.assert_allclose(lc._place_cleaner(lc.CLEANER_SOURCE_SPRAY)[:2],
                                   [72.811, -620.088], atol=.5)
        np.testing.assert_array_equal(native, original)

        expected = np.concatenate((native[:94], native[226:]))
        np.testing.assert_array_equal(mesh, np.round(lc._place_cleaner(expected), 1))

    def test_yaw_about_bore_and_part_origin_preserve_upright_axis(self):
        native = np.array([[[10., 20., 30.], [14., 26., 38.], [12., 23., 38.]],
                           [[12., 23., 37.], [14., 20., 30.], [10., 26., 30.]]])
        original = native.copy()
        native.setflags(write=False)
        with patch.object(lc, "_cleaner_head_native", return_value=native), \
             patch.object(lc, "CLEANER_SOURCE_BORE", np.array([11., 22., 35.])), \
             patch.object(lc, "CLEANER_BORE_POINT", np.array([100., -200., 300.])), \
             patch.object(lc, "CLEANER_YAW_DEG", 90.0):
            mesh = lc.cleaner_mesh([5., 7., 11.])
        np.testing.assert_array_equal(mesh[0], [[97., -208., 284.], [91., -204., 292.],
                                              [94., -206., 292.]])
        np.testing.assert_array_equal(mesh[1, 0], [94., -206., 291.])
        np.testing.assert_array_equal(native, original)

    def test_stand_meets_floor_mount_and_head_without_gaps(self):
        supports = lc.cleaner_support_meshes()
        bounds = {name: (tri.min((0, 1)), tri.max((0, 1))) for name, tri in supports.items()}
        ordered = ["lc_cleaner_" + name for name in ("base", "foot", "post", "mount")]
        self.assertEqual(bounds[ordered[0]][0][2], lc.FLOOR_Z)
        for below, above in zip(ordered, ordered[1:]):
            self.assertEqual(bounds[below][1][2], bounds[above][0][2])
        mount = lc._place_cleaner(lc.CLEANER_SOURCE_MOUNT)
        self.assertAlmostEqual(bounds[ordered[-1]][1][2], mount[2], delta=.05)
        self.assertAlmostEqual(mount[2] - bounds["lc_cleaner_foot"][0][2], 914.4, delta=.05)
        for lo, hi in bounds.values():
            self.assertTrue(np.all(hi > lo))
            np.testing.assert_allclose((lo[:2] + hi[:2]) / 2, mount[:2], atol=.05)

        head = lc._cleaner_head_native()
        self.assertGreater(np.count_nonzero(np.isclose(head[..., 2],
                                                       lc.CLEANER_SOURCE_MOUNT[2], atol=.02)), 30)

    def test_supports_follow_yaw_origin_and_clearance_in_both_tiers(self):
        from waam_pipeline.clearance import _part_fixed_names, TorchModel, blocking, check_point

        self.asset()
        origin = np.array([1014., -180., 150.201])
        for yaw in (0., 37., 90.):
            with self.subTest(yaw=yaw), patch.object(lc, "CLEANER_YAW_DEG", yaw):
                supports = lc.cleaner_support_meshes()
                scene = lc.lc_scene(origin, training_block=False)
                boxes = {name: (lo, hi) for name, lo, hi in scene.boxes}
                shifted = lc.cleaner_support_meshes(origin)
                for name, tri in supports.items():
                    obstacle = name.replace("lc_cleaner_", "reamer_")
                    np.testing.assert_allclose(shifted[name] + origin, tri, atol=.1)
                    lo, hi = tri.min((0, 1)), tri.max((0, 1))
                    np.testing.assert_allclose(boxes[obstacle][0] + origin, lo - 5.)
                    np.testing.assert_allclose(boxes[obstacle][1] + origin, hi + 5.)
                    self.assertNotIn(obstacle, _part_fixed_names(scene))
                    hits = blocking(check_point((lo + hi) / 2 - origin,
                                                 TorchModel.yaskawa_accufast_45(), scene))
                    self.assertIn(obstacle, [hit.obstacle for hit in hits])

    def test_incompatible_import_uses_complete_fallback(self):
        self.asset()
        with patch.object(lc, "CLEANER_SOURCE_SHA256", "different source"):
            self.assertIsNone(lc.cleaner_mesh())
            meshes = lc.lc_scene_meshes(np.zeros(3), training_block=False)
        self.assertIn("reamer_station", meshes)
        self.assertFalse(any(name.startswith("lc_cleaner") for name in meshes))

    def test_cleaner_reaches_studio_scene_meshes(self):
        self.asset()
        origin = lc.DEFAULT_PART_ORIGIN
        meshes = lc.lc_scene_meshes(origin, training_block=False)
        self.assertIn("lc_cleaner", meshes)
        for name, tri in lc.cleaner_support_meshes(origin).items():
            np.testing.assert_array_equal(np.asarray(meshes[name]).reshape(-1, 3, 3), tri)
        self.assertNotIn("reamer_station", meshes)
        np.testing.assert_array_equal(np.asarray(meshes["lc_cleaner"]).reshape(-1, 3, 3),
                                      lc.cleaner_mesh(origin))
        tp = Toolpath([Move(0, 0, 0, None, False, 0, 0)])
        state = build_state(tp, scene=lc.lc_scene(origin, training_block=False), scene_meshes=meshes)
        self.assertEqual(state["scene"]["meshes"]["lc_cleaner"], meshes["lc_cleaner"])
        self.assertIn("reamer_yair_185091", [b["name"] for b in state["scene"]["boxes"]])

    def test_missing_asset_keeps_legacy_display(self):
        for error in (FileNotFoundError("missing archive"), ValueError("incomplete archive")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(cell_hc, "_furniture", side_effect=error):
                self.assertIsNone(lc.cleaner_mesh())
                meshes = lc.lc_scene_meshes(np.zeros(3), training_block=False)
                self.assertNotIn("lc_cleaner", meshes)
                self.assertIn("reamer_station", meshes)
                self.assertIn("charles_platter", meshes)
        for meshes in ({}, {lc.CLEANER_MODEL: np.empty((0, 3, 3))}):
            with patch.object(cell_hc, "_furniture", return_value=meshes):
                self.assertIsNone(lc.cleaner_mesh())

    def test_clearance_bounds_follow_cleaner_placement_and_origin(self):
        from waam_pipeline.clearance import _part_fixed_names

        self.asset()
        for yaw in (0.0, 37.0):
            with self.subTest(yaw=yaw), patch.object(lc, "CLEANER_YAW_DEG", yaw):
                mesh = lc.cleaner_mesh()
                lo, hi = mesh.min(axis=(0, 1)), mesh.max(axis=(0, 1))
                for origin in (np.zeros(3), lc.DEFAULT_PART_ORIGIN):
                    scene = lc.lc_scene(origin, training_block=False)
                    boxes = [b for b in scene.boxes if b[0] == "reamer_yair_185091"]
                    self.assertEqual(len(boxes), 1)
                    np.testing.assert_allclose(boxes[0][1] + origin, lo - 5.0)
                    np.testing.assert_allclose(boxes[0][2] + origin, hi + 5.0)
                    self.assertNotIn("reamer_yair_185091", _part_fixed_names(scene))

    def test_missing_cleaner_keeps_legacy_collision_box(self):
        origin = np.array([1014., -180., 150.201])
        with patch.object(lc, "cleaner_mesh", return_value=None):
            scene = lc.lc_scene(origin, training_block=False)
        boxes = [b for b in scene.boxes if b[0] == "reamer_yair_185091"]
        self.assertEqual(len(boxes), 1)
        np.testing.assert_array_equal(boxes[0][1], np.array([-1., -787., lc.FLOOR_Z]) - origin)
        np.testing.assert_array_equal(boxes[0][2], np.array([281., -533., 500.]) - origin)

    def test_wall_clears_with_cleaner_present(self):
        from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking, check
        from waam_pipeline.slicer import wall

        self.asset()
        tp = wall(150., 6, 3.)
        origin = lc.snap_toolpath_to_platter(tp, 1014., -180., substrate_mm=6.35)
        scene = lc.lc_scene(origin, training_block=False)
        hits = check(tp, TorchModel.yaskawa_accufast_45(), scene, contact_ok=CONTACT_OK)
        self.assertEqual(blocking(hits), [])

    def test_cleaner_volume_blocks_torch(self):
        from waam_pipeline.clearance import TorchModel, blocking, check_point

        self.asset()
        origin = lc.DEFAULT_PART_ORIGIN
        scene = lc.lc_scene(origin, training_block=False)
        mesh = lc.cleaner_mesh()
        center = (mesh.min(axis=(0, 1)) + mesh.max(axis=(0, 1))) / 2
        hits = blocking(check_point(center - origin, TorchModel.yaskawa_accufast_45(), scene))
        self.assertIn("reamer_yair_185091", [hit.obstacle for hit in hits])

    def test_server_import_with_unavailable_cleaner_asset(self):
        code = """
import sys
from unittest.mock import patch
sys.path.insert(0, "src")
with patch("waam_pipeline.cell_hc._furniture", side_effect=FileNotFoundError("missing asset")):
    from waam_pipeline import server, cell_lc
    assert cell_lc.cleaner_mesh() is None
    meshes = cell_lc.lc_scene_meshes([0, 0, 0], training_block=False)
    assert "lc_cleaner" not in meshes
    assert "reamer_station" in meshes
    boxes = {name: (lo.tolist(), hi.tolist())
             for name, lo, hi in cell_lc.lc_scene([0, 0, 0], training_block=False).boxes}
    assert boxes["reamer_yair_185091"] == ([-1., -787., cell_lc.FLOOR_Z], [281., -533., 500.])
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestViewerClipGeometry(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute viewer geometry")
    def test_geometry_behind_the_eye_never_reaches_canvas(self):
        page = render_studio(build_state(Toolpath([Move(0, 0, 0, None, False, 0, 0)])))
        functions = "\n".join(re.search(r"function " + name + r"\([^\n]*\)\{.*?\n\}",
                                        page, re.S).group()
                              for name in ("cameraScale", "cameraEyeDistance", "proj", "drawSeg", "addFace", "wireFace"))
        script = """
const assert=require('node:assert/strict'),canvas={width:800,height:600};
let cx=0,cy=0,cz=0,span=10,yaw=0,pitch=0,zoom=1,panX=0,panY=0,perspective=true,dolly=0;
const ctx=new Proxy({}, {get(){return ()=>{throw new Error('Clipped geometry reached canvas');};}});
""" + functions + """
dolly=cameraEyeDistance();
const behind={x:0,y:-1,z:0},front={x:1,y:1,z:0},atEye={x:0,y:0,z:1};
assert.equal(proj(behind)[2],0);assert.equal(proj(atEye)[2],0);
assert.ok(proj(front)[2]>0);
drawSeg(behind,front,'red',1);drawSeg(front,atEye,'red',1);
const faces=[];addFace(faces,behind,front,atEye,[0,0,0,0,1],null,true);
wireFace(front,behind,atEye,true);assert.equal(faces.length,0);
"""
        result = subprocess.run([shutil.which("node")], input=script, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
