import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_hc, cell_lc, server
from tests._support import isolated_server, serving
from tests.test_studio import run_studio_node, _block_after


class TestHcGeometry(unittest.TestCase):
    def test_table_import_includes_large_cabinet_shell(self):
        audit = json.loads((cell_hc.MESH_PATH.parent / "aw_hc_import.json").read_text())
        self.assertEqual(hashlib.sha256(cell_hc.MESH_PATH.read_bytes()).hexdigest(), audit["mesh_sha256"])
        table = audit["assets"]["Table"]
        self.assertEqual(table["source_faces"], 669102)
        self.assertEqual(len(table["shells"]), 9)
        self.assertEqual(table["shells"][3]["display_triangles"], 21139,
                         "Preserve the perforated tabletop's native planar topology")
        self.assertTrue(any(s["vertices"] == 1557957 and s["source_faces"] == 519319 for s in table["shells"]))
        self.assertTrue(all(s["display_triangles"] > 0 for s in table["shells"]))
        for name, mesh in cell_hc._furniture().items():
            self.assertEqual(mesh.shape[1:], (3, 3), name)
            self.assertTrue(np.isfinite(mesh).all(), name)
            self.assertFalse(mesh.flags.writeable)
        tabletop = cell_hc._furniture()["Table"]
        tabletop = tabletop[np.all(np.abs(tabletop[:, :, 2] - cell_hc.TABLE_Z) < .003, axis=1)]
        area = np.linalg.norm(np.cross(tabletop[:, 1] - tabletop[:, 0], tabletop[:, 2] - tabletop[:, 0]), axis=1).sum() / 2
        self.assertAlmostEqual(float(area), 2736024.754547119, delta=1.0,
                               msg="Tabletop area must match its pinned source, including openings")

    def test_reference_pose_arm_links_are_connected(self):


        from waam_pipeline.robots import hc10
        frames = hc10.fk([0.0] * 6)
        link_names = ["base_link", "link_1_s", "link_2_l", "link_3_u",
                      "link_4_r", "link_5_b", "link_6_t"]
        frame_list = [np.eye(4)] + [frames[j.name] for j in hc10.HC10DTP_JOINTS]
        meshes = cell_hc._link_meshes()
        boxes = []
        for i, link in enumerate(link_names):
            tris = np.asarray(meshes[link]["tris"], float).reshape(-1, 3)
            offset = np.asarray(meshes[link]["offset"], float)
            R, p = frame_list[i][:3, :3], frame_list[i][:3, 3]
            placed = tris @ R.T + p + R @ offset
            boxes.append((placed.min(0), placed.max(0)))
        for i in range(1, len(boxes)):
            (lo_a, hi_a), (lo_b, hi_b) = boxes[i - 1], boxes[i]
            gap = float(np.maximum(0.0, np.maximum(lo_a - hi_b, lo_b - hi_a)).max())
            self.assertLessEqual(gap, 10.0,
                                 f"{link_names[i-1]}->{link_names[i]} disconnected by {gap:.0f} mm")

    def test_source_table_plane_and_robot_mount_agree(self):
        self.assertAlmostEqual(cell_hc.ROBOT_BASE[2], cell_hc.TABLE_Z_MODEL, delta=.01)
        bounds = np.array([[0, 0], [100, 40]], dtype=float)
        origin = cell_hc.place_origin(bounds, ox=10, oy=20, substrate=6, bottom=2, gap=3)
        np.testing.assert_allclose(origin[:2], cell_hc.DEFAULT_PART_ORIGIN[:2] + [10, 20])
        self.assertAlmostEqual(origin[2] + 2, cell_hc.TABLE_Z + 6 + 3)
        with self.assertRaisesRegex(ValueError, "does not fit"):
            cell_hc.place_origin([[0, 0], [4000, 4000]])
        with self.assertRaises(ValueError):
            cell_hc.place_origin(bounds, ox=float("nan"))

    def test_cleaner_options_are_alternatives(self):
        origin = cell_hc.DEFAULT_PART_ORIGIN
        base = cell_hc.scene_meshes(origin, cleaner="none")
        self.assertNotIn("hc_cleaner", base)
        for name in ("yaskawa", "tregaskiss", "binzel"):
            meshes = cell_hc.scene_meshes(origin, cleaner=name)
            self.assertEqual(set(meshes), set(base) | {"hc_cleaner"})
            np.testing.assert_allclose(np.asarray(meshes["hc_cleaner"]).reshape(-1, 3) + origin,
                                       cell_hc._furniture()[cell_hc.CLEANERS[name]].reshape(-1, 3), atol=.001)
        with self.assertRaises(ValueError):
            cell_hc.scene_meshes(origin, cleaner="unknown")

    def test_hosted_pose_defers_arm_meshes_but_builds_torch(self):


        with mock.patch.object(cell_hc, "_link_meshes", side_effect=AssertionError("eager arm meshes")):
            robot = cell_hc.reference_robot(cell_hc.DEFAULT_PART_ORIGIN, include_meshes=False)
        self.assertIsNone(robot["meshes"])
        self.assertIsNotNone(robot["torch_mesh"])
        self.assertIsNotNone(robot["tcp_marker"])
        self.assertEqual(len(robot["joints"]), 1)

    def test_torch_mounts_on_flange_with_vendor_nominal_tcp(self):
        manifest = json.loads((cell_hc.TORCH_MESH_PATH.parent / "aw_hc_torch.json").read_text())
        self.assertEqual(manifest["array_key"], cell_hc.TORCH_KEY)
        self.assertEqual(manifest["triangles"], 13953)
        self.assertEqual(hashlib.sha256(cell_hc.TORCH_MESH_PATH.read_bytes()).hexdigest(),
                         manifest["mesh_sha256"])

        mesh = cell_hc._torch_mesh()
        self.assertEqual(mesh.shape, (13953, 3, 3))
        self.assertTrue(np.isfinite(mesh).all())
        self.assertFalse(mesh.flags.writeable)

        origin = cell_hc.DEFAULT_PART_ORIGIN
        robot = cell_hc.reference_robot(origin)
        self.assertIsNotNone(robot["torch_mesh"])
        self.assertIsNotNone(robot["tcp_marker"])
        torch = np.asarray(robot["torch_mesh"])
        self.assertEqual(torch.shape, (13953, 3, 3))
        self.assertTrue(np.isfinite(torch).all())


        flange_world = np.asarray(robot["skeleton"][0][-1])
        tcp = np.asarray(robot["tcp_marker"])
        self.assertAlmostEqual(float(np.linalg.norm(tcp - flange_world)), 298.3, delta=1.0)


        offset = torch.reshape(-1, 3) - flange_world
        self.assertTrue(np.isfinite(offset).all())
        self.assertLess(float(np.abs(offset).max()), 400.0)

    def test_light_extents_match_selected_source_components(self):
        origin = cell_hc.DEFAULT_PART_ORIGIN
        for cleaner in ("none", "yaskawa", "binzel"):
            boxes = {b["name"]: b for b in cell_hc.reference_boxes(origin, cleaner=cleaner)}
            self.assertEqual("hc_cleaner" in boxes, cleaner != "none")
            selected = dict(cell_hc.FURNITURE)
            if cleaner != "none":
                selected["hc_cleaner"] = cell_hc.CLEANERS[cleaner]
            for name, source in selected.items():
                mesh = cell_hc._furniture()[source]
                np.testing.assert_allclose(np.asarray(boxes[name]["min"]) + origin, mesh.min(axis=(0, 1)), atol=1e-6)
                np.testing.assert_allclose(np.asarray(boxes[name]["max"]) + origin, mesh.max(axis=(0, 1)), atol=1e-6)

    def test_large_triangle_record_and_false_shell_signature(self):
        spec = importlib.util.spec_from_file_location("build_aw_hc", ROOT / "hardware/cell_models/build_aw_hc.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        count = 500001
        vertices = np.zeros((count, 3), dtype="<f4")
        vertices[1] = [1, 0, 0]
        vertices[2] = [0, 1, 0]
        fake = b"S\0" + b"\0" * 6 + struct.pack("<i", 3) + np.full((3, 3), 1e-40, dtype="<f4").tobytes() + b"!"
        record = b"S\0" + b"\0" * 6 + struct.pack("<i", count) + vertices.tobytes()
        faces = bytes([3, 0, 1, 2])
        record += b"\x01" + struct.pack("<I", 1 + len(faces)) + b"\x08" + faces
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.hsf"
            path.write_bytes(b"HSF fixture\n" + zlib.compress(fake + record, 9))
            shells = builder.extract_triangle_records(path)["shells"]
        self.assertEqual(len(shells), 1)
        self.assertEqual(len(shells[0]["vertices"]), count)
        np.testing.assert_array_equal(shells[0]["faces"][0], [0, 1, 2])

    def test_hc_robot_shadow_solves_and_reports_unreachable_without_skipping(self):
        from waam_pipeline.toolpath import Move, Toolpath

        reachable = [Move(x=float(i * 10), y=float(i * 5), z=2.9, feed=200.0,
                          deposit=True, layer=0, line_no=i) for i in range(4)]


        unreachable = Move(x=1.0e5, y=1.0e5, z=1.0e5, feed=200.0,
                           deposit=True, layer=0, line_no=99)
        moves = [*reachable[:2], unreachable, *reachable[2:]]
        tp = Toolpath(moves=moves)
        miss_index = 2

        shadow = cell_hc.hc_robot_shadow(tp, cell_hc.DEFAULT_PART_ORIGIN)
        self.assertTrue(shadow["ik_checked"])
        self.assertEqual(shadow["unreachable"], 1)


        self.assertEqual(len(shadow["skeleton"]), len(moves))
        self.assertEqual(len(shadow["joints"]), len(moves))
        self.assertIsNone(shadow["skeleton"][miss_index])
        self.assertIsNone(shadow["joints"][miss_index])
        for i in range(len(moves)):
            if i == miss_index:
                continue
            self.assertIsNotNone(shadow["skeleton"][i], f"move {i} unexpectedly unreachable")
            self.assertEqual(len(shadow["skeleton"][i]), 8,
                             "foot/base/L/U/elbow/wrist/flange/TCP")
            self.assertEqual(len(shadow["joints"][i]), 6)
        self.assertEqual(len(shadow["chain"]), 6)
        self.assertEqual(len(shadow["base_offset"]), 3)

    def test_hc_playback_keeps_torch_geometry_when_arm_meshes_are_hosted(self):
        from waam_pipeline.robots import hc10
        from waam_pipeline.toolpath import Move, Toolpath

        tp = Toolpath(moves=[Move(x=0, y=0, z=2.9, feed=200, deposit=False, layer=0, line_no=0)])
        with mock.patch.object(cell_hc, "hc_solver", return_value=lambda p: [0.0] * 6):
            shadow = cell_hc.hc_robot_shadow(tp, include_meshes=False)
        self.assertIsNone(shadow["meshes"])
        native = cell_hc._torch_mesh()
        mounted = np.asarray(shadow["torch_mesh_flange"])
        np.testing.assert_allclose(mounted, native @ cell_hc.TORCH_NATIVE_TO_FLANGE.T,
                                   atol=0.00051)
        np.testing.assert_array_equal(shadow["tcp_flange"], cell_hc.HC_TOOL0_TCP_FLANGE_MM)
        self.assertNotIn("torch_mesh", shadow)
        reference = cell_hc.reference_robot(cell_hc.DEFAULT_PART_ORIGIN, include_meshes=False)
        flange = hc10.fk([0.0] * 6)["flange"]
        world = mounted @ flange[:3, :3].T + flange[:3, 3] + shadow["base_offset"]
        np.testing.assert_allclose(world, reference["torch_mesh"], atol=0.0011)

    def test_hc_scene_box_provenance_matches_furniture_bounds(self):
        origin = cell_hc.DEFAULT_PART_ORIGIN
        scene = cell_hc.hc_scene(origin)

        self.assertEqual(scene.part_fixed, ())
        self.assertEqual(scene.capsules, [])
        boxes = {name: (np.asarray(mn), np.asarray(mx)) for name, mn, mx in scene.boxes}
        self.assertEqual(set(boxes), {"hc_table", "hc_back_panel", "hc_balancer", "hc_cleaner"})
        meshes = cell_hc._furniture()
        sources = {"hc_table": "Table", "hc_back_panel": "BackPanel", "hc_balancer": "Balancer",
                  "hc_cleaner": cell_hc.CLEANERS[cell_hc.DEFAULT_CLEANER]}
        for name, source in sources.items():
            mn, mx = boxes[name]
            np.testing.assert_allclose(mn + origin, meshes[source].min(axis=(0, 1)), atol=1e-6)
            np.testing.assert_allclose(mx + origin, meshes[source].max(axis=(0, 1)), atol=1e-6)

    def test_hc_tool0_tcp_and_capsules_match_the_controller_dump(self):


        np.testing.assert_allclose(cell_hc.HC_TOOL0_TCP_NATIVE_MM, [-2.758, -2.040, 304.681])
        self.assertEqual(cell_hc.HC_TOOL0_TCP_RY_DEG, -44.0)


        np.testing.assert_allclose(cell_hc.HC_TOOL0_TCP_FLANGE_MM, [304.681, -2.040, -2.758])

        self.assertEqual(len(cell_hc.HC_TOOL_INTERFERE_CAPSULES), 4)
        radii = [r for _p0, _p1, r in cell_hc.HC_TOOL_INTERFERE_CAPSULES]
        self.assertEqual(radii, [40.0, 35.0, 25.0, 15.0], "shrinking mount->tip radius chain")
        expected_points = (
            ((0.0, 0.0, 0.0), (0.0, 0.0, 25.0)),
            ((0.0, 5.0, 35.0), (150.0, 5.0, 110.0)),
            ((210.0, 0.0, -30.0), (100.0, 0.0, 200.0)),
            ((100.0, 0.0, 205.0), (15.0, 0.0, 290.0)),
        )
        for (p0, p1, _r), (exp0, exp1) in zip(cell_hc.HC_TOOL_INTERFERE_CAPSULES, expected_points):
            np.testing.assert_allclose(p0, exp0)
            np.testing.assert_allclose(p1, exp1)

    def test_hc_flange_pose_places_the_dump_sourced_tcp(self):


        target = np.array([100.0, -50.0, 300.0])
        for approach in ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.3, -0.6, -0.7)):
            T = cell_hc._hc_flange_pose(target, approach)
            placed_tcp = T[:3, :3] @ cell_hc.HC_TOOL0_TCP_FLANGE_MM + T[:3, 3]
            np.testing.assert_allclose(placed_tcp, target, atol=1e-6)

    def test_hc_torch_controller_tool0_envelope_reflects_all_four_capsules_and_ignore_pairs_stay_unwired(self):
        torch = cell_hc.hc_torch_controller_tool0()
        self.assertIsNotNone(torch.rings, "must be a real ring sweep, not the old single sphere")
        self.assertGreater(len(torch.rings), len(cell_hc.HC_TOOL_INTERFERE_CAPSULES),
                           "each capsule is sampled into multiple rings")


        ring_radii = {round(r, 6) for _axial, r, _lateral in torch.rings}
        dump_radii = {r for _p0, _p1, r in cell_hc.HC_TOOL_INTERFERE_CAPSULES}
        self.assertEqual(ring_radii, dump_radii)

        env = torch.envelope()
        self.assertEqual(env.shape[1], 3)
        self.assertTrue(np.isfinite(env).all())
        self.assertTrue((env[:, 1] > 0).all(), "every ring must carry a positive tube radius")

        spheres = torch.spheres()
        self.assertEqual(spheres.shape[1], 2)
        self.assertGreater(float(spheres[:, 1].max()), 0.0)

        self.assertIn(("base_link", "hc_table"), cell_hc.HC_PYBULLET_IGNORE_PAIRS)

        with mock.patch.object(cell_hc, "hc_torch_controller_tool0",
                               side_effect=AssertionError("torch envelope used")), \
             mock.patch.object(cell_hc, "hc_scene",
                               side_effect=AssertionError("hc_scene used")):
            result = server.run_slice({"cell_profile": "hc10"}, None)
        self.assertFalse(result["state"]["meta"]["clearance_checked"])
        self.assertEqual(result["state"]["scene"]["boxes"], [])


class TestHcWorkbench(unittest.TestCase):
    def setUp(self):
        self.before = dict(server._current)
        self.addCleanup(lambda: (server._current.clear(), server._current.update(self.before)))

    def test_preview_never_borrows_lc_clearance_or_solver(self):


        with mock.patch.object(server, "repair_travels", side_effect=AssertionError("LC clearance used")), \
             mock.patch.object(cell_lc, "pulse_solver", side_effect=AssertionError("LC solver used")):
            result = server.run_slice({"cell_profile": "hc10", "hc_cleaner": "yaskawa"}, None)
        state = result["state"]
        self.assertFalse(state["meta"]["clearance_checked"])
        self.assertTrue(state["meta"]["cell_profile"]["planning_only"])
        self.assertEqual(state["meta"]["cell_profile"]["robot_motion"], state["robot"]["mode"])
        self.assertIsNone(result["blocking"])


        self.assertEqual(result["unreachable"], 0)
        self.assertEqual(state["torch"], {"available": False})
        self.assertNotIn("charles_platter", state["scene"]["meshes"])
        self.assertTrue(state["robot"]["ik_checked"])
        self.assertEqual(state["robot"]["unreachable"], 0)
        self.assertEqual(len(state["robot"]["joints"]), result["moves"])
        self.assertEqual(len(state["robot"]["skeleton"]), result["moves"])
        self.assertTrue(state["scene"]["reference_boxes"])


        self.assertEqual(state["scene"]["boxes"], [])
        self.assertEqual(state["scene"]["capsules"], [])

    def test_light_renderer_draws_reference_extents_without_collision_boxes(self):
        if not shutil.which("node"):
            self.skipTest("Node required for headless display tests")
        result = server.run_slice({"cell_profile": "hc10"}, None)
        state = result["state"]
        script = "function drawSceneReferences()" + _block_after(
            server._current["html"], r"function drawSceneReferences\(\)")
        setup = "const S=" + json.dumps({"scene": {k: state["scene"][k] for k in
                                                 ("reference_boxes", "boxes", "capsules")}}) + ";\n" + r'''
let light=true,edges=0,clipped=false;const lightPreview=()=>light,showWalls={checked:false},showFumeHood={checked:true};
const ctx={beginPath(){},moveTo(){},lineTo(){edges++;},stroke(){}};
const proj=p=>clipped?[NaN,NaN,0,0]:[p.x,p.y,1,0];
'''
        scenario = r'''
drawSceneReferences();
const shown=S.scene.reference_boxes.filter(b=>!b.name.startsWith("hc_curtain_"));
if(edges!==shown.length*12 || edges===0) throw new Error("Light cell outline missing");
if(S.scene.boxes.length || S.scene.capsules.length) throw new Error("Display extents became clearance");
edges=0;showFumeHood.checked=false;drawSceneReferences();
if(edges!==(shown.length-1)*12) throw new Error("Hidden hood outline remains");
edges=0;showFumeHood.checked=true;drawSceneReferences();
if(edges!==shown.length*12) throw new Error("Hood outline was not restored");
edges=0;light=false;drawSceneReferences();
if(edges!==0) throw new Error("Full view drew bounding extents");
light=true;clipped=true;drawSceneReferences();
if(edges!==0) throw new Error("Clipped reference extents reached the canvas");
'''
        run = subprocess.run([shutil.which("node")], input=setup + script + scenario,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_replacement_and_placement_keep_cell_identity(self):
        first = server.run_slice({"cell_profile": "hc10", "hc_cleaner": "binzel"}, None)
        prepared = server._current["prepared"]["toolpath"]
        moved = server.run_placement({"ox": "40", "oy": "10", "sub": "8"})
        self.assertIs(server._current["prepared"]["toolpath"], prepared)
        self.assertEqual(moved["state"]["meta"]["params"]["hc_cleaner"], "binzel")
        self.assertEqual(moved["state"]["meta"]["params"]["cell_profile"], "hc10")
        self.assertAlmostEqual(moved["placement"]["origin"][0] - first["placement"]["origin"][0], 40)
        with self.assertRaisesRegex(ValueError, "not calibrated"):
            server.run_placement({"correct": "1"})
        lc = server.run_slice({"cell_profile": "lc50s", "fixture": "0"}, None)
        self.assertTrue(lc["state"]["meta"]["clearance_checked"])
        self.assertIn("charles_platter", lc["state"]["scene"]["meshes"])
        self.assertIsNone(lc["state"]["meta"]["cell_profile"])

    def test_center_and_rotation_use_the_source_pivot_on_the_hc_table(self):
        first = server.run_slice({"cell_profile": "hc10", "yaw": "37"}, None)
        prepared = server._current["prepared"]["toolpath"]
        points = np.asarray(prepared.deposit_points())
        expected_pivot = (points[:, :2].min(axis=0) + points[:, :2].max(axis=0)) / 2
        np.testing.assert_allclose(first["placement"]["pivot"], expected_pivot)
        center = first["placement"]["center_offsets"]
        placed = server.run_placement({"ox": str(center[0]), "oy": str(center[1])})
        footprint = np.asarray(placed["placement"]["footprint"])
        origin = np.asarray(placed["placement"]["origin"])
        np.testing.assert_allclose(footprint.mean(axis=0) + origin[:2],
                                   (cell_hc.TABLE_XY_MIN + cell_hc.TABLE_XY_MAX) / 2, atol=1e-8)
        turned = server.run_placement({"yaw": "81"})
        self.assertIs(server._current["prepared"]["toolpath"], prepared)
        np.testing.assert_allclose(turned["placement"]["pivot"], expected_pivot)
        self.assertFalse(turned["state"]["meta"]["clearance_checked"])

    def test_http_emit_handlers_refuse_hc_even_with_lc_query(self):
        server.run_slice({"cell_profile": "hc10"}, None)
        handler = object.__new__(server._Handler)
        handler._json = mock.Mock()
        for emit in (handler._api_jbi, handler._api_ros2):
            with mock.patch.object(cell_lc, "pulse_solver", side_effect=AssertionError("LC solver used")):
                emit("local", {"arc": ["12"], "cell_profile": ["lc50s"], "name": ["TEST"]})
            status, payload = handler._json.call_args.args
            self.assertEqual(status, 409)
            self.assertEqual(payload["reason"], "cell_profile")
            self.assertIn("HC10DTP", payload["error"])

    def test_saved_profiles_and_legacy_lc_job_on_hc_default_server(self):
        handler = object.__new__(server._Handler)
        handler._json = mock.Mock()
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(server, "SAVES_DIR", Path(temp)), \
             mock.patch.object(server, "_DEFAULT_CELL_PROFILE", "hc10"):
            legacy = Path(temp) / "local/legacy/request.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"params": {"kind": "wall", "fixture": "0"}}))
            handler._api_load("local", {"name": ["legacy"]})
            self.assertTrue(handler._json.call_args.args[1]["ok"])
            self.assertEqual(server._current["request"]["params"]["cell_profile"], "lc50s")
            self.assertIn("charles_platter", server._current["html"])

            server.run_slice({"cell_profile": "hc10", "hc_cleaner": "binzel"}, None)
            handler._api_save("local", {"name": ["cobot"],
                                        "gen": [str(server._state("local")["gen"])]})
            self.assertTrue(handler._json.call_args.args[1]["ok"])
            saved = json.loads((Path(temp) / "local/cobot/request.json").read_text())
            self.assertEqual(saved["params"]["cell_profile"], "hc10")
            self.assertEqual(saved["params"]["hc_cleaner"], "binzel")
            handler._api_load("local", {"name": ["legacy"]})
            handler._api_load("local", {"name": ["cobot"]})
            self.assertTrue(handler._json.call_args.args[1]["ok"])
            self.assertEqual(server._current["request"]["params"]["cell_profile"], "hc10")
            self.assertEqual(server._current["request"]["params"]["hc_cleaner"], "binzel")
            self.assertIn('"available":false', server._current["html"].replace(" ", ""))

    def test_invalid_profile_does_not_replace_checked_state(self):
        server.run_slice({"cell_profile": "hc10"}, None)
        before = dict(server._current)
        for params in ({"cell_profile": "unknown"}, {"cell_profile": "hc10", "hc_cleaner": "unknown"},
                       {"cell_profile": "hc10", "cell": "0"}):
            with self.assertRaises(ValueError):
                server.run_slice(params, None)
            for key, value in before.items():
                self.assertIs(server._current[key], value)

    def test_browser_selects_cleaner_and_switches_cells(self):
        if not shutil.which("node"):
            self.skipTest("Node required for headless browser tests")
        server.run_slice({"cell_profile": "hc10"}, None)
        page = server._current["html"]
        with isolated_server(), serving() as httpd:
            output = run_studio_node(page, r'''
await new Promise(r=>setTimeout(r,100));
if(wbCellProfile.value!=="hc10" || S.torch.available!==false) throw new Error("HC profile did not restore");
if(!emitBtn.disabled || !emitRosBtn.disabled) throw new Error("HC robot export was enabled");
wbHcCleaner.value="yaskawa";
await wbHcCleaner.onchange();
if(!S.scene.meshes.hc_cleaner || S.meta.params.hc_cleaner!=="yaskawa") throw new Error("Cleaner selection not applied");
if(S.meta.clearance_checked || !emitBtn.disabled || !emitRosBtn.disabled) throw new Error("Preview claimed robot readiness");
if(!wbFixture.disabled || !wbRepair.disabled) throw new Error("LC controls enabled after HC slice");
wbCellProfile.value="lc50s";
await wbCellProfile.onchange();
if(S.meta.params.cell_profile!=="lc50s" || !S.scene.meshes.charles_platter) throw new Error("Cell switch did not apply");
if(S.torch.available===false || !S.meta.clearance_checked) throw new Error("LC tooling/check was not restored");
if(wbFixture.disabled || wbRepair.disabled) throw new Error("LC controls were not restored");
wbCellProfile.value="hc10";
await wbCellProfile.onchange();
if(!wbFixture.disabled || !wbRepair.disabled || !emitBtn.disabled) throw new Error("LC controls leaked after profile switch");
return "HC_BROWSER_OK";
''', f"http://127.0.0.1:{httpd.server_port}")
            self.assertIn("HC_BROWSER_OK", output)
