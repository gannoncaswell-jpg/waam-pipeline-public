import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "hardware/cell_models")]
import cell_models as cm
from waam_pipeline import cell_lc as lc
from waam_pipeline.studio import build_state, render_studio
from waam_pipeline.toolpath import Move, Toolpath


class TestSupportGeometry(unittest.TestCase):
    def test_eight_anchor_pads_preserve_the_drawing_hole_pattern(self):
        pads = [v for k, v in cm.cell_base_parts().items() if k.startswith("anchor_")]
        self.assertEqual(len(pads), 8)
        for pad in pads:
            lo, hi = cm.bbox(pad)
            center = (lo[:2] + hi[:2]) / 2
            bottom = np.unique(pad.reshape(-1, 3)[np.isclose(pad.reshape(-1, 3)[:, 2], 0)], axis=0)
            for sign in (-1, 1):
                hole_center = center + [0, sign * 115.0 / 2]
                on_bore = np.isclose(np.linalg.norm(bottom[:, :2] - hole_center, axis=1), 12.6 / 2)
                self.assertEqual(on_bore.sum(), 12)

    def test_station_flange_is_supported_without_changing_machine_heights(self):
        base = cm.cell_base_parts(lc.FRAME_CENTER, lc.FACEPLATE_X)
        flange = cm.mh185_headstock_parts()["stand_base_plate"]
        self.assertAlmostEqual(cm.bbox(base["station_crossmember"])[1][2], cm.bbox(flange)[0][2])
        self.assertAlmostEqual(cm.bbox(cm.robot_pedestal())[1][2], 860.4)
        face = cm.mh185_headstock_parts()["faceplate"]
        self.assertAlmostEqual(sum(b[2] for b in cm.bbox(face)) / 2, 923.0)

        point = np.array([450., -300., 50.])
        self.assertFalse(any(np.all(point >= cm.bbox(v)[0]) and np.all(point <= cm.bbox(v)[1])
                             for v in base.values()))

    def test_new_static_structure_is_covered_and_robot_fixed(self):
        from waam_pipeline.clearance import _part_fixed_names
        scene = lc.lc_scene(np.zeros(3), training_block=False)
        fixed_to_part = _part_fixed_names(scene)
        boxes = {n: (lo, hi) for n, lo, hi in scene.boxes}
        for name, mesh in cm.cell_base_parts(lc.FRAME_CENTER, lc.FACEPLATE_X).items():
            name = "cell_base_" + name
            self.assertNotIn(name, fixed_to_part)
            points = mesh.reshape(-1, 3) + [0, 0, lc.FLOOR_Z]
            lo, hi = boxes[name]
            self.assertTrue(np.all(points >= lo - 1e-7) and np.all(points <= hi + 1e-7))
        station = [v for k, v in cm.mh185_headstock_parts().items() if k.startswith("stand_")]
        points = np.concatenate(station).reshape(-1, 3)
        rot, pos = lc.lc_scene_placements()["mh185_headstock"]
        points = points @ rot.T + pos
        lo, hi = boxes["positioner_stand"]
        self.assertTrue(np.all(points >= lo - 1e-7) and np.all(points <= hi + 1e-7))

    def test_housing_and_support_are_on_s_link_only(self):
        meshes = lc._decimated_link_meshes()
        expected = {"spool", "spool_housing", "spool_hub", "spool_support"}
        for name, entry in meshes.items():
            kinds = {g["kind"] for g in entry.get("groups", [])}
            if name == "link_1_s":
                self.assertLessEqual(expected, kinds)
            else:
                self.assertFalse(expected & kinds)

        bounds = [cm.bbox(v) for v in cm.spool_support_parts(lc.SPOOL_S_BASE_TRANSLATION).values()]
        reached = {0}
        for _ in bounds:
            reached |= {j for j, (lo, hi) in enumerate(bounds) if any(
                np.all(hi >= bounds[i][0] - 1e-7) and np.all(bounds[i][1] >= lo - 1e-7)
                for i in reached)}
        self.assertEqual(len(reached), len(bounds))

    def test_conduit_ends_follow_their_own_machine_links(self):
        q = np.zeros(6)
        base = lc.wire_conduit_points(q)
        tilted = lc.wire_conduit_points([0, 20, -35, 0, 0, 0])
        np.testing.assert_allclose(tilted[0], base[0])
        self.assertGreater(np.linalg.norm(tilted[-1] - base[-1]), 100)
        spun = lc.wire_conduit_points([90, 0, 0, 0, 0, 0])
        np.testing.assert_allclose(spun[:, 0], -base[:, 1], atol=1e-7)
        np.testing.assert_allclose(spun[:, 1], base[:, 0], atol=1e-7)
        np.testing.assert_allclose(spun[:, 2], base[:, 2], atol=1e-7)

    def test_feeder_lower_holes_meet_the_rear_arm_pads(self):


        meshes = lc._decimated_link_meshes()
        for name, entry in meshes.items():
            if name != "link_3_u":
                self.assertFalse(any(g["kind"] in ("bracket", "feeder")
                                     for g in entry.get("groups", [])))
        entry = meshes["link_3_u"]
        tris = np.asarray(entry["tris"]).reshape(-1, 3, 3) + entry["offset"]
        group = next(g for g in entry["groups"] if g["kind"] == "bracket")
        bracket = np.unique(tris[group["start"]:group["end"]].reshape(-1, 3), axis=0)
        self.assertAlmostEqual(bracket[:, 0].max(), -75.0, delta=0.1)
        rim = bracket[np.isclose(bracket[:, 0], -75.0, atol=0.01)]
        for z in (-39.0, -24.0, 24.0, 39.0):
            radius = np.linalg.norm(rim[:, 1:] - [-28.5, z], axis=1)
            self.assertGreaterEqual(np.count_nonzero(np.abs(radius - 4.407) < 0.25), 6)


        self.assertAlmostEqual(bracket[:, 2].max(), 82.79, delta=0.1)
        arm = tris[:min(g["start"] for g in entry["groups"])].reshape(-1, 3)
        self.assertGreaterEqual(arm[:, 0].min(), bracket[:, 0].max() - 0.1)

    def test_conduit_meets_rendered_housing_and_feeder(self):


        meshes = lc._decimated_link_meshes()
        spec = lc.wire_conduit_specs()[0]
        s = meshes["link_1_s"]
        points = np.asarray(s["tris"]).reshape(-1, 3, 3) + s["offset"]
        housings = [points[g["start"]:g["end"]] for g in s["groups"]
                    if g["kind"] == "spool_housing"]
        housing_points = np.concatenate(housings).reshape(-1, 3)
        outlet = housing_points[np.isclose(housing_points[:, 2], housing_points[:, 2].max())]
        np.testing.assert_allclose((outlet.min(axis=0) + outlet.max(axis=0)) / 2,
                                   spec["controls"][0]["point"], atol=.1)
        support = np.concatenate([points[g["start"]:g["end"]] for g in s["groups"]
                                  if g["kind"] == "spool_support"])
        expected = np.concatenate(list(cm.spool_support_parts(lc.SPOOL_S_BASE_TRANSLATION).values()))
        np.testing.assert_allclose(support, expected, atol=.1)
        end = np.array(spec["controls"][-1]["point"])


        u = meshes["link_3_u"]
        points = np.asarray(u["tris"]).reshape(-1, 3, 3) + u["offset"]
        part_ids = {g["array_key"] for g in cm.k3560_1_metadata()["groups"] if g["solid"] == 20}
        inlet_points = np.unique(np.concatenate([
            points[g["start"]:g["end"]] for g in u["groups"]
            if g.get("part") in part_ids]).reshape(-1, 3), axis=0)
        ring = inlet_points[np.abs(inlet_points[:, 0] - end[0]) < .0001]
        radii = np.linalg.norm(ring[:, 1:] - end[1:], axis=1)
        self.assertGreaterEqual(np.count_nonzero(np.abs(radii - 5.9563) < .001), 10)
        approach = np.array(spec["controls"][-2]["point"])
        np.testing.assert_allclose(approach - end, [-180., 0., 0.], atol=1e-7)

    def test_rendered_feeder_keeps_every_cad_face_and_color(self):
        entry = lc._decimated_link_meshes()["link_3_u"]
        payload = np.asarray(entry["tris"], np.float32).reshape(-1, 3, 3)
        parts = cm.autodrive_4r100_parts()
        appearances = cm.autodrive_4r100_appearances()
        feeder_groups = [g for g in entry["groups"] if g["kind"] == "feeder"]
        self.assertEqual(len(feeder_groups), 43)
        self.assertGreater(len({g["appearance"]["color"] for g in feeder_groups}), 10)
        for group in feeder_groups:
            actual = payload[group["start"]:group["end"]].astype(float)
            source = lc.feeder_points_in_u(parts[group["part"]])
            self.assertEqual(len(actual), len(source))
            self.assertEqual(group["appearance"], appearances[group["part"]])
            np.testing.assert_allclose(actual, source, atol=4e-5, rtol=0)
            rendered_normals = np.cross(actual[:, 1] - actual[:, 0], actual[:, 2] - actual[:, 0])
            source_normals = np.cross(source[:, 1] - source[:, 0], source[:, 2] - source[:, 0])
            self.assertTrue(np.isfinite(actual).all())
            self.assertGreater(np.linalg.norm(rendered_normals, axis=1).min() / 2, 1e-6)
            self.assertTrue(np.all(np.einsum('ij,ij->i', rendered_normals, source_normals) > 0))

    @unittest.skipUnless(shutil.which("node"), "Node needed for browser geometry")
    def test_browser_conduit_matches_drawing_at_multiple_poses(self):
        page = render_studio(build_state(Toolpath([Move(0, 0, 0, None, False, 0, 0)])))
        funcs = [re.search(r"function " + name + r"\([^\n]*\)\{.*?\n\}", page, re.S).group()
                 for name in ("rotAxis", "matMul", "fkFrames", "conduitCurve")]
        poses = [[0, 0, 0, 0, 0, 0], [90, 0, 0, 0, 0, 0], [-35, 20, -45, 15, 5, 0]]
        chain = [{"origin": list(j.origin), "axis": list(j.axis)} for j in lc.AR1440_JOINTS]
        script = "const ROB=" + json.dumps({"chain": chain}) + ";\n" + "\n".join(funcs)
        script += "\nconst spec=" + json.dumps(lc.wire_conduit_specs()[0]) + ";"
        script += "\nprocess.stdout.write(JSON.stringify(" + json.dumps(poses)
        script += ".map(q=>conduitCurve(spec,fkFrames(q)))));"
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        np.testing.assert_allclose(json.loads(result.stdout), [lc.wire_conduit_points(q) for q in poses], atol=1e-7)
