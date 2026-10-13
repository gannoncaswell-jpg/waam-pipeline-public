import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, station
from waam_pipeline.robots import ar1440

URDF = ROOT / "robots" / "ar1440.urdf"


def _tree():
    return ET.parse(URDF).getroot()


def _joints(root):
    return {j.get("name"): j for j in root.findall("joint")}


class TestTwoBranches(unittest.TestCase):


    def setUp(self):
        self.root = _tree()
        self.joints = _joints(self.root)

    def test_it_is_a_tree_rooted_at_base_link(self):
        children = [j.find("child").get("link") for j in self.root.findall("joint")]
        self.assertEqual(len(children), len(set(children)), "a link has two parents")
        links = {ln.get("name") for ln in self.root.findall("link")}
        self.assertEqual(links - set(children), {"base_link"})

    def test_the_station_hangs_off_the_root_not_off_the_wrist(self):


        parent_of = {j.find("child").get("link"): j.find("parent").get("link")
                     for j in self.root.findall("joint")}
        chain, link = [], station.STATION_LINK_NAMES[-1]
        while link in parent_of:
            link = parent_of[link]
            chain.append(link)
        self.assertEqual(chain[-1], "base_link")
        for arm in [j.name for j in ar1440.AR1440_JOINTS] + ["flange"]:
            self.assertNotIn(arm, chain)
        self.assertEqual(chain.count("base_link"), 1)

    def test_the_arm_chain_is_untouched(self):
        parent = "base_link"
        for i, joint in enumerate(ar1440.AR1440_JOINTS):
            j = self.joints[joint.name]
            self.assertEqual(j.get("type"), "revolute")
            self.assertEqual(j.find("parent").get("link"), parent)
            parent = j.find("child").get("link")
            self.assertEqual(parent, f"link_{i + 1}_{joint.name[-1]}")

    def test_the_station_joint_is_the_one_the_posing_api_names(self):
        j = self.joints[station.STATION_JOINT_NAME]

        self.assertEqual(j.get("type"), "revolute")
        self.assertAlmostEqual(float(j.find("limit").get("upper")),
                               np.radians(station.SOFT_LIMIT_DEG), places=4)
        axis = [float(v) for v in j.find("axis").get("xyz").split()]
        np.testing.assert_allclose(axis, station.axis_direction(), atol=1e-12)
        xyz = np.array([float(v) for v in
                        self.joints["station_mount"].find("origin").get("xyz").split()])
        np.testing.assert_allclose(
            xyz * 1000.0,
            [cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y, cell_lc.AXIS_Z], atol=1e-6)

    def test_the_station_is_invisible_to_the_motoros2_name_match(self):


        import re

        text = URDF.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<joint name="(joint_\d_\w)"', text),
                         [j.name for j in ar1440.AR1440_JOINTS])
        self.assertIn(station.STATION_JOINT_NAME, self.joints)
        self.assertNotRegex(station.STATION_JOINT_NAME, r"^joint_\d_\w$")

    def test_posing_the_urdf_joint_reproduces_the_station_kinematics(self):


        mount = np.array([float(v) * 1000.0 for v in
                          self.joints["station_mount"].find("origin").get("xyz").split()])
        axis = np.array([float(v) for v in
                         self.joints[station.STATION_JOINT_NAME].find("axis").get("xyz").split()])
        p = np.array([180.0, 19.05, -60.0])
        for theta in (0.0, 33.0, -77.0, 155.0):
            q = ar1440.urdf_joint_positions(station_deg=theta)[station.STATION_JOINT_NAME]
            urdf_fk = mount + cell_lc._rot(tuple(axis), np.degrees(q)) @ p
            np.testing.assert_allclose(urdf_fk, station.apply(mount + p, theta), atol=1e-9)


class TestGeometryDefault(unittest.TestCase):


    def test_meshes_are_used_when_they_sit_beside_the_output(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "meshes" / "ar1440").mkdir(parents=True)
            p = Path(d) / "with.urdf"
            ar1440.write_urdf(str(p))
            self.assertIn("meshes/ar1440/collision/", p.read_text(encoding="utf-8"))

    def test_primitives_are_the_fallback_when_there_are_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "auto.urdf"
            ar1440.write_urdf(str(p))
            text = p.read_text(encoding="utf-8")
        self.assertNotIn("<mesh", text)
        self.assertIn("<cylinder", text)

    def test_primitives_stay_reachable_on_purpose(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "meshes" / "ar1440").mkdir(parents=True)
            p = Path(d) / "forced.urdf"
            ar1440.write_urdf(str(p), mesh_dir="")
            text = p.read_text(encoding="utf-8")
        self.assertNotIn("<mesh", text)
        self.assertIn("<cylinder", text)

    def test_the_station_can_be_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "arm.urdf"
            ar1440.write_urdf(str(p), station=False)
            text = p.read_text(encoding="utf-8")
        self.assertNotIn(station.STATION_JOINT_NAME, text)
        for link in station.STATION_LINK_NAMES:
            self.assertNotIn(link, text)


class TestPosingApi(unittest.TestCase):


    def test_names_are_urdf_joint_names(self):
        urdf_names = set(_joints(_tree()))
        posed = ar1440.urdf_joint_positions([0] * 6, 0.0)
        self.assertLessEqual(set(posed), urdf_names)
        self.assertEqual(len(posed), 7)

    def test_the_two_groups_pose_independently(self):
        arm = ar1440.urdf_joint_positions([10, 20, 30, 40, 50, 60])
        self.assertNotIn(station.STATION_JOINT_NAME, arm)
        self.assertEqual(len(arm), 6)
        st = ar1440.urdf_joint_positions(station_deg=-12.0)
        self.assertEqual(list(st), [station.STATION_JOINT_NAME])

    def test_degrees_in_radians_out_with_no_sign_games(self):
        posed = ar1440.urdf_joint_positions([10, -20, 30, -40, 50, -60], 25.0)
        np.testing.assert_allclose(
            [posed[j.name] for j in ar1440.AR1440_JOINTS],
            np.radians([10, -20, 30, -40, 50, -60]), atol=1e-12)

        self.assertAlmostEqual(posed[station.STATION_JOINT_NAME],
                               float(np.radians(25.0)), places=12)

    def test_a_wrong_length_pose_is_refused(self):
        with self.assertRaises(ValueError):
            ar1440.urdf_joint_positions([0, 0, 0])


if __name__ == "__main__":
    unittest.main()
