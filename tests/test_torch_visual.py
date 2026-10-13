import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "hardware/cell_models")]

import cell_models as cm
from waam_pipeline import cell_lc as lc


class TestVendorTorchDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metadata = cm.yaskawa_torch_metadata()
        cls.variant = cls.metadata["variants"][cls.metadata["default_variant"]]
        cls.parts = cm.yaskawa_torch_parts()
        cls.meshes = lc._decimated_link_meshes()

    def test_eight_variants_keep_the_supplied_archive_identity(self):
        self.assertEqual(self.metadata["source_sha256"],
                         "a2082235c850d21e9cde8ce9edb11c3b3df7b6eb305623e4ca7ae257d9bd0f11")
        expected = {f"{angle}d_{mount}_mnt{brake}"
                    for angle in (22, 45) for mount in ("hard", "shock")
                    for brake in ("", "_brake")}
        self.assertEqual(set(self.metadata["variants"]), expected)
        self.assertEqual(self.metadata["default_variant"], "45d_shock_mnt")
        mesh_path = ROOT / "hardware/cell_models/meshes/yaskawa_torches.npz"
        self.assertEqual(hashlib.sha256(mesh_path.read_bytes()).hexdigest(),
                         self.metadata["mesh_sha256"])
        for variant in expected:
            with self.subTest(variant=variant):
                parts = cm.yaskawa_torch_parts(variant)
                self.assertEqual(set(parts), {g["array_key"] for g in
                                             self.metadata["variants"][variant]["groups"]})
                self.assertTrue(all(np.isfinite(p).all() for p in parts.values()))

    def test_nominal_torch_keeps_scale_flange_and_handedness(self):


        registration = self.variant["registration"]
        np.testing.assert_array_equal(registration["rotation"],
                                      [[0, 0, 1], [1, 0, 0], [0, 1, 0]])
        np.testing.assert_array_equal(registration["translation_mm"], [0, 0, 0])
        points = np.concatenate(list(self.parts.values())).reshape(-1, 3)
        self.assertEqual(len(points) // 3, 123576)
        np.testing.assert_allclose([points.min(0), points.max(0)],
                                  [[-4.5, -42.631172, -105.232285],
                                   [339.631927, 42.710659, 42.678856]], atol=.001, rtol=0)
        flange = points[np.abs(points[:, 0]) < .0001]
        self.assertGreater(len(flange), 20)
        self.assertGreater(np.ptp(flange[:, 1]), 70.)
        self.assertGreater(np.ptp(flange[:, 2]), 70.)
        np.testing.assert_array_equal(lc.TOOL_TCP, [351.972, -.324, -110.247])
        self.assertEqual(lc.TOOL_INTERFERE_CAPSULES[-1],
                         ((0., 0., 261.), (-90., 0., 351.), 20.))

    def test_actual_browser_payload_preserves_torch_shells_and_tcp(self):
        entry = self.meshes["link_6_t"]
        groups = [g for g in entry["groups"] if g["kind"] == "torch"]
        self.assertEqual({g["part"] for g in groups}, set(self.parts))
        self.assertEqual(len(groups), 17)
        for name, mesh in self.meshes.items():
            if name != "link_6_t":
                self.assertFalse(any(g["kind"] == "torch" for g in mesh.get("groups", [])))
        payload = np.asarray(entry["tris"], np.float32).reshape(-1, 3, 3).astype(float)
        for group in groups:
            actual = payload[group["start"]:group["end"]]
            source = self.parts[group["part"]]
            np.testing.assert_array_equal(actual, source)
            self.assertRegex(group["appearance"]["color"], r"^#[0-9a-fA-F]{6}$")
            a = np.cross(actual[:, 1] - actual[:, 0], actual[:, 2] - actual[:, 0])
            b = np.cross(source[:, 1] - source[:, 0], source[:, 2] - source[:, 0])
            self.assertTrue(np.isfinite(actual).all())
            self.assertTrue(np.all(np.linalg.norm(a, axis=1) / 2 > 1e-6))
            self.assertTrue(np.all(np.einsum("ij,ij->i", a, b) > 0))
        marker_group = next(g for g in entry["groups"] if g["kind"] == "tcp")
        marker = payload[marker_group["start"]:marker_group["end"]].reshape(-1, 3)
        np.testing.assert_allclose((marker.min(0) + marker.max(0)) / 2,
                                   [351.972, -.324, -110.247], atol=.0001, rtol=0)


if __name__ == "__main__":
    unittest.main()
