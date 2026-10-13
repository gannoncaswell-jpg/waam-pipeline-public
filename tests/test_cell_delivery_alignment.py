import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "hardware" / "cell_models")]
import cell_models as cm
from waam_pipeline import cell_lc as lc


class TestCellDeliveryAlignment(unittest.TestCase):
    def test_overall_system_width_is_not_fence_width(self):

        self.assertEqual(cm.CELL_FLOOR_LAYOUT, (2000.0, 2000.0))
        np.testing.assert_allclose(np.ptp(cm.cell_frame_v2().reshape(-1, 3), axis=0),
                                   [2140.0, 2140.0, 2277.0])
        self.assertEqual(cm.CELL_SYSTEM_WIDTH, 3123.0)
        meshes = lc.lc_scene_meshes(np.zeros(3), training_block=False)
        fence = np.asarray(meshes["cell_frame_v2"]).reshape(-1, 3)
        rack = np.asarray(meshes["equipment_rack"]).reshape(-1, 3)

        self.assertLess(rack[:, 1].max(), fence[:, 1].min())
        self.assertAlmostEqual(fence[:, 1].max() - rack[:, 1].min(), 3123.0)

    def test_controller_and_welder_sit_on_separate_rack_decks(self):
        meshes = lc.lc_scene_meshes(np.zeros(3), training_block=False)
        poses = lc.lc_scene_placements()
        rack_z = poses["equipment_rack"][1][2]
        bounds = {name: cm.bbox(np.asarray(meshes[name]).reshape(-1, 3, 3))
                  for name in ("yrc1000_cabinet", "lincoln_r450")}
        yrc, welder = bounds["yrc1000_cabinet"], bounds["lincoln_r450"]
        self.assertAlmostEqual(yrc[0][2], rack_z + cm.RACK_CONTROLLER_DECK_Z, places=1)
        self.assertAlmostEqual(welder[0][2], rack_z + cm.RACK_LOWER_DECK_Z, places=1)
        self.assertGreater(yrc[0][2], welder[1][2])
        np.testing.assert_array_equal(poses["yrc1000_cabinet"][1][:2],
                                      poses["lincoln_r450"][1][:2])
        self.assertIn("power_disconnect", meshes)
        fence_min_y = np.asarray(meshes["cell_frame_v2"]).reshape(-1, 3)[:, 1].min()
        for name in ("equipment_rack", "yrc1000_cabinet", "lincoln_r450", "power_disconnect"):
            self.assertLess(np.asarray(meshes[name]).reshape(-1, 3)[:, 1].max(), fence_min_y)

    def test_fixture_preserves_source_dimensions_and_is_not_implicitly_installed(self):
        mesh = cm.waam_fixture()
        self.assertEqual(len(mesh), 3536)
        lo, hi = cm.bbox(mesh)
        np.testing.assert_allclose(lo, [-254., -127., 0.], atol=0.001)
        np.testing.assert_allclose(hi, [254., 127., 50.8], atol=0.001)


        top = mesh[np.all(np.isclose(mesh[:, :, 2], 50.8, atol=0.001), axis=1)]
        area = np.linalg.norm(np.cross(top[:, 1] - top[:, 0],
                                       top[:, 2] - top[:, 0]), axis=1).sum() / 2
        self.assertAlmostEqual(float(area), (16 + 6) * 1.75 * 25.4**2, delta=0.05)
        path = ROOT / "hardware/cell_models/meshes/waam_fixture.npy"
        meta = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), meta["mesh_sha256"])
        self.assertEqual(meta["source_sha256"],
                         "04d13819c58d83fdb035daec5c381a34213e2a7343c0c64cee9f888f0f5f9356")
        self.assertNotIn("waam_fixture", lc.lc_scene_meshes(training_block=False))

    def test_controller_torch_envelope_and_calibrated_marker(self):
        points = lc.torch_mesh().reshape(-1, 3)
        best = np.full(len(points), np.inf)
        for a, b, radius in lc.TOOL_INTERFERE_CAPSULES:
            a, b = lc.YASKAWA_FLANGE_TO_OURS @ np.asarray(a), lc.YASKAWA_FLANGE_TO_OURS @ np.asarray(b)
            ab = b - a
            t = np.clip((points - a) @ ab / (ab @ ab), 0, 1)
            best = np.minimum(best, np.linalg.norm(points - a - t[:, None] * ab, axis=1) - radius)

        self.assertLessEqual(best.max(), 1e-7)
        entry = lc._decimated_link_meshes()["link_6_t"]
        group = next(g for g in entry["groups"] if g["kind"] == "tcp")
        marker = np.asarray(entry["tris"]).reshape(-1, 3, 3)[group["start"]:group["end"]]
        lo, hi = cm.bbox(marker)
        np.testing.assert_allclose((lo + hi) / 2, lc.TOOL_TCP, atol=0.001)

    def test_pedestal_visual_vertices_are_covered_by_collision_boxes(self):
        points = cm.robot_pedestal().reshape(-1, 3) + [0, 0, lc.FLOOR_Z]
        covered = np.zeros(len(points), dtype=bool)
        for name, lo, hi in lc.lc_scene(np.zeros(3), training_block=False).boxes:
            if name.startswith("robot_pedestal"):
                covered |= ((points >= lo - 1e-7) & (points <= hi + 1e-7)).all(axis=1)
        self.assertTrue(covered.all())


if __name__ == "__main__":
    unittest.main()
