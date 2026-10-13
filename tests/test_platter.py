import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hardware" / "platter"))

from platter import PlatterDesign, scene_capsule, write_dxf


class TestPlatter(unittest.TestCase):
    def setUp(self):
        self.d = PlatterDesign()

    def test_inertia_within_positioner_budget(self):
        self.assertLess(self.d.inertia_kgm2(), 47.0 * 0.05)

    def test_grid_respects_keepouts_and_rim(self):
        interface = list(self.d.mount_positions) + list(self.d.dowel_positions) + [(0, 0)]
        for x, y in self.d.tap_positions():
            r = np.hypot(x, y)
            self.assertLessEqual(r, self.d.diameter_mm / 2 - 20)
            for px, py in interface:
                self.assertGreater(
                    np.hypot(x - px, y - py),
                    self.d.mount_cbore_dia / 2 + self.d.tap_drill_dia / 2,
                )

    def test_dxf_layers(self):
        out = ROOT / "hardware" / "platter" / "platter_test.dxf"
        write_dxf(self.d, out)
        text = out.read_text()
        for layer in ("CUT", "MOUNT_CBORE", "DOWEL", "ACCESS", "TAP_DRILL", "GROUND_LUG", "PROBE_PIN"):
            self.assertIn(layer, text)
        self.assertEqual(text.count("CIRCLE"),
                         1 + 2 * len(self.d.mount_positions) + len(self.d.dowel_positions)
                         + 1 + len(self.d.tap_positions()) + 1
                         + len(self.d.probe_pin_positions))
        out.unlink()

    def test_scene_capsule_shape(self):
        name, p0, p1, r = scene_capsule(self.d, top_z_mm=0.0)
        self.assertEqual(name, "platter")
        self.assertAlmostEqual(p0[2], -self.d.thickness_mm)
        self.assertAlmostEqual(r, 200.0)


if __name__ == "__main__":
    unittest.main()
