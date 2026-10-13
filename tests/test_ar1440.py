import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.robots import ar1440


class TestZeroPose(unittest.TestCase):


    def test_wrist_center(self):
        np.testing.assert_allclose(
            ar1440.wrist_center([0] * 6), [155 + 640, 0, 450 + 614 + 200], atol=1e-9
        )

    def test_flange(self):
        frames = ar1440.fk([0] * 6)
        np.testing.assert_allclose(
            frames["flange"][:3, 3], [155 + 640 + 100, 0, 450 + 614 + 200], atol=1e-9
        )

    def test_limits_enforced(self):
        with self.assertRaises(ValueError):
            ar1440.fk([0, -120, 0, 0, 0, 0])


class TestDatasheetEnvelope(unittest.TestCase):


    def _sweep(self):
        best_reach, best_height = 0.0, 0.0
        jl, ju = ar1440.AR1440_JOINTS[1], ar1440.AR1440_JOINTS[2]
        for l_deg in np.linspace(jl.lower_deg, jl.upper_deg, 111):
            for u_deg in np.linspace(ju.lower_deg, ju.upper_deg, 111):
                p = ar1440.wrist_center([0, l_deg, u_deg, 0, 0, 0])
                best_reach = max(best_reach, float(np.hypot(p[0], p[1])))
                best_height = max(best_height, float(p[2]))
        return best_reach, best_height

    def test_reach_and_height(self):
        reach, height = self._sweep()
        self.assertAlmostEqual(reach, 1439.5, delta=2.0)
        self.assertAlmostEqual(height, 1734.5, delta=2.0)

        self.assertLessEqual(reach, 1440.5)
        self.assertLessEqual(height, 1735.5)


URDF = ROOT / "robots" / "ar1440.urdf"


class TestUrdf(unittest.TestCase):
    def test_emit_and_parse(self):


        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "ar1440.urdf"
            ar1440.write_urdf(str(out))
            root = ET.parse(out).getroot()
        self.assertEqual(root.get("name"), "motoman_ar1440")


        revolute = [j for j in root.findall("joint")
                    if j.get("type") == "revolute"
                    and j.get("name") in ar1440.ARM_JOINT_NAMES]
        self.assertEqual(len(revolute), 6)
        s_limit = revolute[0].find("limit")
        self.assertAlmostEqual(float(s_limit.get("lower")), np.radians(-170), places=3)
        self.assertAlmostEqual(float(s_limit.get("upper")), np.radians(170), places=3)

        links = [ln for ln in root.findall("link") if ln.get("name").startswith("link_")]
        self.assertTrue(all(ln.find("collision") is not None for ln in links[:-1]))


class TestCommittedUrdf(unittest.TestCase):


    def test_it_carries_mesh_geometry(self):
        self.assertIn("<mesh", URDF.read_text(encoding="utf-8"),
                      "robots/ar1440.urdf was regenerated without its meshes; "
                      "an arm-collision check against primitives is worthless")

    def test_every_referenced_mesh_resolves_on_disk(self):
        refs = [m.get("filename") for m in ET.parse(URDF).getroot().iter("mesh")]
        self.assertEqual(len(refs), 14)
        for ref in refs:
            self.assertTrue((URDF.parent / ref).is_file(), ref)

    def test_collision_geometry_uses_the_collision_meshes(self):


        root = ET.parse(URDF).getroot()
        for tag, want in (("collision", "/collision/"), ("visual", "/visual/")):
            refs = [m.get("filename") for ln in root.findall("link")
                    for m in ln.findall(tag + "/geometry/mesh")]
            self.assertEqual(len(refs), 7, tag)
            for ref in refs:
                self.assertIn(want, ref, ref)


if __name__ == "__main__":
    unittest.main()
