import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc as lc
from waam_pipeline.slicer import load_stl


VISUAL_COUNTS = dict(zip(
    ("base_link", "link_1_s", "link_2_l", "link_3_u", "link_4_r", "link_5_b", "link_6_t"),
    (9230, 18064, 16218, 16246, 14206, 4914, 1124),
))
DECIMATED_COUNTS = dict(zip(VISUAL_COUNTS, (245, 495, 496, 414, 425, 269, 36)))


def triangles(entry):
    return np.asarray(entry["tris"]).reshape(-1, 3, 3)


def casting_count(entry):
    groups = entry.get("groups", [])
    return groups[0]["start"] if groups else len(entry["tris"]) // 9


class TestStudioVisualMeshes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.visual = lc._decimated_link_meshes()
        is_file = Path.is_file


        missing = ROOT / "robots/meshes/ar1440/visual/gp12_link_3_u.stl"
        with patch.object(Path, "is_file", lambda path: path != missing and is_file(path)):

            cls.fallback = lc._decimated_link_meshes.__wrapped__()

    def test_full_visual_castings_keep_the_documented_link_frames(self):
        self.assertEqual(set(self.visual), set(VISUAL_COUNTS))
        self.assertEqual(sum(VISUAL_COUNTS.values()), 80002)

        offsets = {"link_1_s": [0., 0., 450.], "link_4_r": [640., 0., 0.],
                   "link_6_t": [-100., 0., 0.]}
        for link, count in VISUAL_COUNTS.items():
            with self.subTest(link=link):
                entry = self.visual[link]
                self.assertEqual(casting_count(entry), count)
                source = load_stl(ROOT / f"robots/meshes/ar1440/visual/gp12_{link}.stl")
                expected = source * 1000.0 + offsets.get(link, [0., 0., 0.])
                actual = triangles(entry)[:count] + entry["offset"]


                np.testing.assert_allclose(actual, expected, atol=0.051, rtol=0)

    def test_partial_visual_set_falls_back_to_every_decimated_link(self):
        self.assertEqual(set(self.fallback), set(DECIMATED_COUNTS))
        for link, count in DECIMATED_COUNTS.items():
            with self.subTest(link=link):
                self.assertEqual(casting_count(self.fallback[link]), count)

    def test_visual_detail_preserves_all_attachment_coordinates_and_groups(self):
        for link in VISUAL_COUNTS:
            with self.subTest(link=link):
                visual, fallback = self.visual[link], self.fallback[link]
                self.assertEqual(visual["offset"], fallback["offset"])
                hi_groups, low_groups = visual.get("groups", []), fallback.get("groups", [])
                self.assertEqual([g["kind"] for g in hi_groups],
                                 [g["kind"] for g in low_groups])
                previous = VISUAL_COUNTS[link]
                for hi, low in zip(hi_groups, low_groups):
                    self.assertEqual(hi["start"], previous)
                    self.assertGreater(hi["end"], hi["start"])
                    np.testing.assert_array_equal(
                        triangles(visual)[hi["start"]:hi["end"]],
                        triangles(fallback)[low["start"]:low["end"]],
                    )
                    previous = hi["end"]
                self.assertEqual(previous, len(visual["tris"]) // 9)

    def test_missing_both_sets_returns_no_mesh_instead_of_a_partial_arm(self):
        with patch.object(Path, "is_file", return_value=False):
            self.assertIsNone(lc._decimated_link_meshes.__wrapped__())

    def test_the_payload_is_built_once_per_process(self):

        self.assertIs(lc._decimated_link_meshes(), lc._decimated_link_meshes())


if __name__ == "__main__":
    unittest.main()
