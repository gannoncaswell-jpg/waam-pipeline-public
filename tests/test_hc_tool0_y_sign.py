import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_hc, cell_lc, plan
from waam_pipeline.robots import hc10


def _rot_y(deg):
    a = np.radians(deg)
    return np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])


def home_tcp(to_flange):
    q = np.array(plan.HC_PARK_PULSES, float) / np.array(hc10.PULSES_PER_DEG, float)
    f = hc10.fk(q)["flange"]
    return f, f[:3, 3] + f[:3, :3] @ (to_flange @ cell_hc.HC_TOOL0_TCP_NATIVE_MM)


class TestHcTool0YSignPrediction(unittest.TestCase):
    def test_the_manuals_flange_definition_gives_the_hc_the_lc_map(self):

        r = hc10.fk([0.0] * 6)["flange"][:3, :3]
        zf, xf = r[:, 0], np.array([0.0, 0.0, 1.0])
        axes = r.T @ np.column_stack([xf, np.cross(zf, xf), zf])
        np.testing.assert_allclose(axes, cell_lc.YASKAWA_FLANGE_TO_OURS, atol=1e-12)

    def test_r1_home_base_y_splits_the_two_maps_by_4_mm(self):
        _, proper = home_tcp(cell_lc.YASKAWA_FLANGE_TO_OURS)
        _, mirror = home_tcp(cell_hc.HC_TOOL0_NATIVE_TO_FLANGE)
        np.testing.assert_allclose(proper, (617.79, 151.96, 192.01), atol=0.01)
        np.testing.assert_allclose(mirror, (617.71, 147.88, 192.01), atol=0.01)

    def test_r1_home_orientation_under_the_proper_map(self):
        f, _ = home_tcp(cell_lc.YASKAWA_FLANGE_TO_OURS)
        tool = f[:3, :3] @ cell_lc.YASKAWA_FLANGE_TO_OURS @ _rot_y(cell_hc.HC_TOOL0_TCP_RY_DEG)

        ry = np.degrees(np.arcsin(-tool[2, 0]))
        rx = np.degrees(np.arctan2(tool[2, 1], tool[2, 2]))
        rz = np.degrees(np.arctan2(tool[1, 0], tool[0, 0]))
        np.testing.assert_allclose((abs(rx), ry, rz), (180.0, 45.977, -1.123), atol=0.001)


if __name__ == "__main__":
    unittest.main()
