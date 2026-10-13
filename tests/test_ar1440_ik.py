import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.robots import ar1440


def random_q(rng):

    q = []
    for j in ar1440.AR1440_JOINTS:
        lo, hi = j.lower_deg, j.upper_deg
        margin = 0.1 * (hi - lo)
        q.append(rng.uniform(lo + margin, hi - margin))
    return q


class TestIkRoundTrip(unittest.TestCase):
    def test_fk_ik_fk_round_trip(self):
        rng = np.random.default_rng(42)
        checked = 0
        for _ in range(200):
            q = random_q(rng)
            target = ar1440.fk(q)["flange"]
            sol = ar1440.ik(target)
            if sol is None:
                continue
            back = ar1440.fk(sol)["flange"]
            np.testing.assert_allclose(back[:3, 3], target[:3, 3], atol=0.5)
            np.testing.assert_allclose(back[:3, :3], target[:3, :3], atol=1e-3)
            checked += 1


        self.assertGreater(checked, 185)

    def test_no_systematic_hole_across_the_l_range(self):


        rng = np.random.default_rng(7)
        buckets: dict[int, list[int]] = {}
        for _ in range(1200):
            q = [rng.uniform(j.lower_deg, j.upper_deg) for j in ar1440.AR1440_JOINTS]
            target = ar1440.fk(q)["flange"]
            sol = ar1440.ik(target)
            hit = buckets.setdefault(int(q[1] // 30 * 30), [0, 0])
            hit[0 if sol is not None else 1] += 1
            if sol is not None:
                back = ar1440.fk(sol)["flange"]
                np.testing.assert_allclose(back[:3, 3], target[:3, 3], atol=1e-6)
        for lo, (ok, miss) in sorted(buckets.items()):
            rate = miss / (ok + miss)
            self.assertLess(
                rate, 0.02,
                f"L in [{lo}, {lo + 30}): {miss}/{ok + miss} poses unrecoverable",
            )

    def test_zero_pose_recovered(self):
        target = ar1440.fk([0, 10, -20, 0, 30, 0])["flange"]
        sol = ar1440.ik(target)
        self.assertIsNotNone(sol)
        back = ar1440.fk(sol)["flange"]
        np.testing.assert_allclose(back[:3, 3], target[:3, 3], atol=0.5)

    def test_unreachable_returns_none(self):
        pose = np.eye(4)
        pose[:3, 3] = [3000.0, 0.0, 500.0]
        self.assertIsNone(ar1440.ik(pose))

    def test_all_solutions_multiple_branches(self):


        target = ar1440.fk([20, 30, -40, 40, 45, 5])["flange"]
        sols = ar1440.ik(target, all_solutions=True)
        self.assertIsNotNone(sols)
        self.assertGreaterEqual(len(sols), 2)

    def test_reachable_helper(self):

        self.assertTrue(ar1440.reachable([700.0, 0.0, 500.0], approach=(0, 0, -1)))
        self.assertFalse(ar1440.reachable([2500.0, 0.0, 500.0]))


if __name__ == "__main__":
    unittest.main()


class TestUrdfMeshes(unittest.TestCase):
    def test_mesh_urdf_references_resolve(self):
        import re
        import tempfile
        import xml.dom.minidom

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ar1440.urdf"

            ar1440.write_urdf(str(p), mesh_dir="meshes/ar1440")
            text = p.read_text()
        xml.dom.minidom.parseString(text)
        refs = re.findall(r'filename="([^"]+)"', text)
        self.assertEqual(len(refs), 14)
        for r in refs:
            self.assertTrue((ROOT / "robots" / r).exists(), r)

    def test_primitive_fallback_unchanged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prim.urdf"
            ar1440.write_urdf(str(p))
            text = p.read_text()
        self.assertNotIn("<mesh", text)
        self.assertIn("<cylinder", text)


class TestEnvelopeSticker(unittest.TestCase):


    def test_all_four_extents(self):
        jl, ju = ar1440.AR1440_JOINTS[1], ar1440.AR1440_JOINTS[2]
        ls = np.linspace(jl.lower_deg, jl.upper_deg, 50)
        us = np.linspace(ju.lower_deg, ju.upper_deg, 50)
        pts = np.array([
            ar1440.wrist_center([0.0, l, u, 0.0, 0.0, 0.0]) for l in ls for u in us
        ])
        self.assertAlmostEqual(pts[:, 0].max(), 1440.0, delta=1.5)
        self.assertAlmostEqual(pts[:, 2].max(), 1734.0, delta=1.5)
        self.assertAlmostEqual(-pts[:, 0].min(), 1129.0, delta=1.5)
        self.assertAlmostEqual(-pts[:, 2].min(), 777.0, delta=1.5)
