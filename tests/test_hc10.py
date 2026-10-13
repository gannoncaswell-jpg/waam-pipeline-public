import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.robots import hc10
from waam_pipeline.robots.ar1440 import _rot

LIMITS = np.array([[j.lower_deg, j.upper_deg] for j in hc10.HC10DTP_JOINTS])


def random_q(rng, shrink=1.0):
    lo, hi = LIMITS[:, 0] * shrink, LIMITS[:, 1] * shrink
    return list(rng.uniform(lo, hi))


class TestZeroPose(unittest.TestCase):


    def test_flange(self):
        np.testing.assert_allclose(
            hc10.fk([0] * 6)["flange"][:3, 3], [500 + 170, 162, 275 + 700], atol=1e-9
        )

    def test_p_point(self):
        np.testing.assert_allclose(
            hc10.wrist_center([0] * 6), [500, 0, 275 + 700], atol=1e-9
        )

    def test_limits_enforced(self):
        with self.assertRaises(ValueError):
            hc10.fk([0, 0, 300, 0, 0, 0])
        with self.assertRaises(ValueError):
            hc10.fk([0] * 5)


class TestOffsetWrist(unittest.TestCase):


    @staticmethod
    def _axis_line(frames, joint):
        j = next(j for j in hc10.HC10DTP_JOINTS if j.name == joint)
        t = frames[joint]
        d = t[:3, :3] @ (np.asarray(j.axis, float) / np.linalg.norm(j.axis))
        return t[:3, 3], d

    @staticmethod
    def _skew_distance(p0, d0, p1, d1):
        n = np.cross(d0, d1)
        nn = np.linalg.norm(n)
        if nn < 1e-12:
            return float(np.linalg.norm(np.cross(p1 - p0, d0)))
        return float(abs(np.dot(p1 - p0, n / nn)))

    def test_r_and_b_intersect_but_t_does_not_share_the_point(self):
        rng = np.random.default_rng(11)
        for _ in range(25):
            frames = hc10.fk(random_q(rng))
            pr, dr = self._axis_line(frames, "joint_4_r")
            pb, db = self._axis_line(frames, "joint_5_b")
            pt, dt = self._axis_line(frames, "joint_6_t")

            self.assertLess(self._skew_distance(pr, dr, pb, db), 1e-9)
            self.assertLess(self._skew_distance(pb, db, pt, dt), 1e-9)


            self.assertAlmostEqual(
                float(np.linalg.norm(frames["joint_5_b"][:3, 3] - pr)), 162.0, places=9
            )

    def test_no_constant_offset_gives_a_wrist_centre(self):


        wrist = [
            (r, b, t)
            for r in (-60.0, 0.0, 60.0)
            for b in (-60.0, -20.0, 20.0, 60.0)
            for t in (-90.0, 0.0, 90.0)
        ]
        worst = min(
            self._candidate_spread(d, wrist) for d in np.linspace(0.0, 500.0, 101)
        )
        self.assertGreater(worst, 100.0)

    @staticmethod
    def _candidate_spread(d, wrist):
        pts = []
        for r, b, t in wrist:
            flange = hc10.fk([10.0, 30.0, -20.0, r, b, t])["flange"]
            pts.append(flange[:3, 3] - d * flange[:3, 0])
        pts = np.array(pts)
        return float(np.linalg.norm(pts - pts.mean(axis=0), axis=1).max())


class TestDatasheetReach(unittest.TestCase):


    L = np.array([0.0, 0.0, 275.0])

    def test_1200_is_the_p_point(self):


        self.assertAlmostEqual(hc10.P_POINT_REACH_MM, 1200.0, places=9)
        best = max(
            float(np.linalg.norm(hc10.wrist_center([0.0, 90.0, u, 0.0, 0.0, 0.0]) - self.L))
            for u in np.linspace(-290, 290, 1161)
        )
        self.assertAlmostEqual(best, 1200.0, delta=0.01)
        b_origin = hc10.fk([0.0, 90.0, 90.0, 0.0, 0.0, 0.0])["joint_5_b"][:3, 3]
        self.assertAlmostEqual(
            float(np.linalg.norm(b_origin - self.L)), np.hypot(1200.0, 162.0), delta=0.01
        )

    def test_1379_is_the_straight_arm_flange_reach(self):


        self.assertAlmostEqual(hc10.FLANGE_REACH_MM, 1379.0, delta=1.0)
        straight = hc10.fk([0.0, 90.0, 90.0, 0.0, 0.0, 0.0])["flange"][:3, 3]
        np.testing.assert_allclose(straight, [1370.0, 162.0, 275.0], atol=1e-9)
        self.assertAlmostEqual(
            float(np.linalg.norm(straight - self.L)), hc10.FLANGE_REACH_MM, places=9
        )


        self.assertAlmostEqual(float(np.hypot(*straight[:2])), 1379.0, delta=1.0)

    def test_the_datasheet_figure_is_not_the_supremum(self):


        bend = np.degrees(np.arctan2(162.0, 670.0))
        q = [0.0, 90.0, 90.0 - bend, -90.0, 0.0, 0.0]
        p = hc10.fk(q)["flange"][:3, 3]
        self.assertAlmostEqual(float(np.linalg.norm(p - self.L)), 1389.307, delta=0.01)
        self.assertGreater(hc10.ENVELOPE_MAX_MM, hc10.FLANGE_REACH_MM)
        self.assertTrue(hc10.in_envelope(p))
        self.assertAlmostEqual(bend, 13.5927, delta=0.001)

    def test_envelope_bounds_every_reachable_flange_position(self):
        rng = np.random.default_rng(5)
        for _ in range(3000):
            p = hc10.fk(random_q(rng))["flange"][:3, 3]
            self.assertTrue(hc10.in_envelope(p), f"{np.linalg.norm(p - self.L)} mm")

    def test_inner_void_is_tiny(self):


        self.assertAlmostEqual(hc10.ENVELOPE_MIN_MM, 10.693, delta=0.01)
        q = [0.0, 90.0, -103.6, -90.0, 0.0, 0.0]
        d = float(np.linalg.norm(hc10.fk(q)["flange"][:3, 3] - self.L))
        self.assertAlmostEqual(d, hc10.ENVELOPE_MIN_MM, delta=0.01)


class TestIkRoundTrip(unittest.TestCase):


    TOL_MM = 0.01
    TOL_DEG = 0.005

    def _round_trip(self, q, seed=None):
        target = hc10.fk(q)["flange"]
        sol = hc10.ik(target, seed=seed)
        self.assertTrue(
            all(
                j.lower_deg - 1e-9 <= a <= j.upper_deg + 1e-9
                for j, a in zip(hc10.HC10DTP_JOINTS, sol)
            ),
            sol,
        )
        back = hc10.fk(sol)["flange"]
        dp = float(np.linalg.norm(back[:3, 3] - target[:3, 3]))
        dr = float(np.degrees(np.linalg.norm(hc10._rotvec(target[:3, :3] @ back[:3, :3].T))))
        self.assertLessEqual(dp, self.TOL_MM)
        self.assertLessEqual(dr, self.TOL_DEG)
        return sol, dp, dr

    def test_full_range_round_trip(self):
        rng = np.random.default_rng(42)
        worst_p = worst_r = 0.0
        for _ in range(200):
            _, dp, dr = self._round_trip(random_q(rng))
            worst_p, worst_r = max(worst_p, dp), max(worst_r, dr)


        self.assertLess(worst_p, self.TOL_MM + 1e-12)
        self.assertLess(worst_r, self.TOL_DEG + 1e-12)

    def test_no_systematic_hole_across_the_envelope(self):


        rng = np.random.default_rng(7)
        buckets: dict[int, list[int]] = {}
        for _ in range(250):
            q = random_q(rng)
            target = hc10.fk(q)["flange"]
            d = float(np.linalg.norm(target[:3, 3] - np.array([0.0, 0.0, 275.0])))
            hit = buckets.setdefault(int(d // 200 * 200), [0, 0])
            try:
                sol = hc10.ik(target)
            except hc10.IkRefused:
                hit[1] += 1
                continue
            hit[0] += 1
            back = hc10.fk(sol)["flange"]
            np.testing.assert_allclose(back[:3, 3], target[:3, 3], atol=self.TOL_MM)
        self.assertGreaterEqual(len(buckets), 4, "sample did not span the envelope")
        for lo, (ok, miss) in sorted(buckets.items()):
            self.assertEqual(
                miss, 0, f"flange {lo}-{lo + 200} mm from L: {miss}/{ok + miss} lost"
            )

    def test_warm_seed_follows_one_branch(self):


        rng = np.random.default_rng(3)
        q = np.array([0.0, 45.0, 45.0, 0.0, 45.0, 0.0])
        prev = list(q)
        for _ in range(40):
            q = np.clip(q + rng.uniform(-2.0, 2.0, 6), LIMITS[:, 0] * 0.8, LIMITS[:, 1] * 0.8)
            sol, _, _ = self._round_trip(list(q), seed=prev)
            self.assertLess(
                float(np.max(np.abs(np.array(sol) - np.array(prev)))),
                20.0,
                "warm-seeded IK jumped branch",
            )
            prev = sol

    def test_deterministic(self):
        target = hc10.fk([20.0, 30.0, -40.0, 40.0, 45.0, 5.0])["flange"]
        self.assertEqual(hc10.ik(target), hc10.ik(target))

    def test_all_solutions_finds_several_branches(self):
        target = hc10.fk([20.0, 30.0, -40.0, 40.0, 45.0, 5.0])["flange"]
        sols = hc10.ik(target, all_solutions=True)
        self.assertGreaterEqual(len(sols), 2)
        for sol in sols:
            back = hc10.fk(sol)["flange"]
            np.testing.assert_allclose(back[:3, 3], target[:3, 3], atol=self.TOL_MM)


class TestIkRefuses(unittest.TestCase):


    def _pose(self, xyz, r=np.eye(3)):
        pose = np.eye(4)
        pose[:3, :3] = r
        pose[:3, 3] = xyz
        return pose

    def test_far_outside_refuses_with_the_envelope_reason(self):
        with self.assertRaises(hc10.IkRefused) as cm:
            hc10.ik(self._pose([3000.0, 0.0, 500.0]))
        self.assertEqual(cm.exception.reason, "envelope")
        self.assertIn("3008", cm.exception.detail)
        self.assertIn("reachable shell", cm.exception.detail)

    def test_just_outside_refuses_rather_than_returning_a_near_miss(self):
        d = hc10.ENVELOPE_MAX_MM + 5.0
        pose = self._pose([d, 0.0, 275.0])
        with self.assertRaises(hc10.IkRefused):
            hc10.ik(pose)
        self.assertIsNone(hc10.ik(pose, strict=False))

    def test_unreachable_orientation_inside_the_envelope(self):


        reasons = set()
        rng = np.random.default_rng(2)
        for _ in range(5):
            axis = rng.normal(size=3)
            r = _rot(axis / np.linalg.norm(axis), float(rng.uniform(-180, 180)))
            pose = self._pose([1385.0, 0.0, 275.0], r)
            self.assertTrue(hc10.in_envelope(pose[:3, 3]))
            try:
                hc10.ik(pose)
            except hc10.IkRefused as exc:
                reasons.add(exc.reason)
        self.assertEqual(reasons, {"orientation"})

    def test_bad_argument(self):
        with self.assertRaises(hc10.IkRefused) as cm:
            hc10.ik(np.eye(3))
        self.assertEqual(cm.exception.reason, "argument")

    def test_never_returns_a_pose_it_did_not_hit(self):


        rng = np.random.default_rng(19)
        solved = refused = 0
        for _ in range(30):
            axis = rng.normal(size=3)
            r = _rot(axis / np.linalg.norm(axis), float(rng.uniform(-180, 180)))
            pose = self._pose(rng.uniform(-1500, 1500, 3), r)
            sol = hc10.ik(pose, strict=False)
            if sol is None:
                refused += 1
                continue
            solved += 1
            back = hc10.fk(sol)["flange"]
            np.testing.assert_allclose(back[:3, 3], pose[:3, 3], atol=0.01)
            np.testing.assert_allclose(back[:3, :3], pose[:3, :3], atol=1e-3)
        self.assertGreater(solved, 0)
        self.assertGreater(refused, 0)

    def test_reachable_helper(self):
        self.assertTrue(hc10.reachable([800.0, 0.0, 400.0], approach=(0, 0, -1)))
        self.assertFalse(hc10.reachable([2500.0, 0.0, 500.0]))


URDF = ROOT / "robots" / "hc10dtp.urdf"


class TestUrdf(unittest.TestCase):


    @classmethod
    def setUpClass(cls):


        cls.tmpdir = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmpdir, ignore_errors=True)
        cls.OUT = Path(cls.tmpdir) / "hc10dtp.urdf"
        hc10.write_urdf(str(cls.OUT), mesh_dir="meshes")
        cls.root = ET.parse(cls.OUT).getroot()

    def test_structure(self):
        self.assertEqual(self.root.get("name"), "motoman_hc10dtp")
        revolute = [j for j in self.root.findall("joint") if j.get("type") == "revolute"]
        self.assertEqual([j.get("name") for j in revolute],
                         [j.name for j in hc10.HC10DTP_JOINTS])
        links = [ln.get("name") for ln in self.root.findall("link")]
        self.assertEqual(
            links,
            ["base_link", "link_1_s", "link_2_l", "link_3_u", "link_4_r",
             "link_5_b", "link_6_t", "flange"],
        )
        fixed = [j for j in self.root.findall("joint") if j.get("type") == "fixed"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual(fixed[0].find("child").get("link"), "flange")

    def test_limits_are_the_datasheet_values_in_radians(self):
        for elem, joint in zip(
            [j for j in self.root.findall("joint") if j.get("type") == "revolute"],
            hc10.HC10DTP_JOINTS,
        ):
            lim = elem.find("limit")
            self.assertAlmostEqual(float(lim.get("lower")), np.radians(joint.lower_deg), places=3)
            self.assertAlmostEqual(float(lim.get("upper")), np.radians(joint.upper_deg), places=3)
        s, l, u, r, b, t = [
            (float(j.find("limit").get("upper")))
            for j in self.root.findall("joint")
            if j.get("type") == "revolute"
        ]
        for got, want in zip((s, l, u, r, b, t), (210, 180, 290, 210, 180, 210)):
            self.assertAlmostEqual(np.degrees(got), want, places=2)

    def test_urdf_reproduces_fk(self):


        joints = [j for j in self.root.findall("joint") if j.get("type") == "revolute"]
        origins, axes = [], []
        for j in joints:
            origins.append(np.array([float(v) for v in j.find("origin").get("xyz").split()]) * 1000.0)
            axes.append(tuple(float(v) for v in j.find("axis").get("xyz").split()))
        rng = np.random.default_rng(13)
        for _ in range(20):
            q = random_q(rng)
            t = np.eye(4)
            for origin, axis, angle in zip(origins, axes, q):
                step = np.eye(4)
                step[:3, 3] = origin
                step[:3, :3] = _rot(axis, angle)
                t = t @ step
            np.testing.assert_allclose(t, hc10.fk(q)["flange"], atol=1e-7)

    def test_mesh_references_resolve_and_use_collision_stls(self):
        refs = [g.find("mesh").get("filename")
                for g in self.root.iter("geometry") if g.find("mesh") is not None]
        self.assertEqual(len(refs), 14)
        for ref in refs:
            self.assertTrue(ref.endswith(".stl"), ref)
            self.assertTrue((ROOT / "robots" / ref).exists(), ref)


        self.assertTrue(any("hc10dtp_b00/collision/link_6_t.stl" in r for r in refs))
        self.assertEqual(sum("hc10dtp_b00" in r for r in refs), 2)

    def test_mesh_offsets_undo_the_two_folded_origins(self):
        by_link = {ln.get("name"): ln for ln in self.root.findall("link")}
        got = {
            name: tuple(float(v) for v in ln.find("collision").find("origin").get("xyz").split())
            for name, ln in by_link.items()
            if ln.find("collision") is not None
        }
        self.assertEqual(got["link_1_s"], (0.0, 0.0, 0.275))
        self.assertEqual(got["link_6_t"], (-0.17, 0.0, 0.0))
        for link in ("base_link", "link_2_l", "link_3_u", "link_4_r", "link_5_b"):
            self.assertEqual(got[link], (0.0, 0.0, 0.0))

    def test_primitive_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prim.urdf"
            hc10.write_urdf(str(p))
            text = p.read_text()
        self.assertNotIn("<mesh", text)
        self.assertIn("<cylinder", text)
        ET.fromstring(text)


class TestCommittedUrdf(unittest.TestCase):


    def test_it_carries_mesh_geometry(self):
        self.assertIn("<mesh", URDF.read_text(encoding="utf-8"),
                      "robots/hc10dtp.urdf was regenerated without its meshes; "
                      "an arm-collision check against primitives is worthless")

    def test_every_referenced_mesh_resolves_on_disk(self):
        refs = [m.get("filename") for m in ET.parse(URDF).getroot().iter("mesh")]
        self.assertEqual(len(refs), 14)
        for ref in refs:
            self.assertTrue((URDF.parent / ref).is_file(), ref)


class TestCellConsistency(unittest.TestCase):
    def test_examples_cell_names_this_robot(self):
        import json

        cfg = json.loads((ROOT / "examples" / "cell_hc10.json").read_text())
        self.assertEqual(cfg["robot"], "hc10")


if __name__ == "__main__":
    unittest.main()
