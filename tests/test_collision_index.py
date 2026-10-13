import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from waam_pipeline.clearance import Violation, check
from waam_pipeline.studio import _TEMPLATE, build_state
from waam_pipeline.toolpath import Move, Toolpath

NODE = shutil.which("node")
INDEX_JS = re.search(r"const collisionCache = new WeakMap\(\);\n.*?\nfunction collisionIndex\(state\) \{.*?\n\}\n",
                     _TEMPLATE, re.S).group()


def tier_one_bead_name():

    plough = Toolpath([Move(0, 0, 0, None, False, 0, 1), Move(20, 0, 0, 200, True, 0, 2),
                       Move(20, 20, 0, None, False, 0, 3)])
    return next(v.obstacle for v in check(plough) if v.obstacle.startswith("bead("))


@unittest.skipUnless(NODE, "Node executes the workbench collision index")
class TestCollisionIndexOwner(unittest.TestCase):
    def index(self, second_z=0.0):
        corner = Toolpath([Move(0, 0, 0, None, False, 0, 1), Move(20, 0, 0, 200, True, 0, 2),
                           Move(20, 20, second_z, 200, True, 0, 3)])
        hit = Violation(2, 3, (20.0, 1.0, 0.0), tier_one_bead_name(), -1.0)
        state = build_state(corner, workbench=True, violations=[hit])
        script = (INDEX_JS + "const e=collisionIndex(" + json.dumps(state) + ").events[0];\n"
                  "console.log(JSON.stringify({owner:e.owner,adjacent:e.adjacent,"
                  "artifact:e.artifact,reason:e.reason}));")
        result = subprocess.run([NODE], input=script, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_both_tiers_name_the_bead_the_same_way(self):
        from sim import pybullet_driver as pbd

        self.assertEqual(tier_one_bead_name(), "bead(line 2)")
        self.assertEqual(pbd.BeadSpec(0, 2, (0, 0, 0), (1, 0, 0), 4.5, 3.0, (0.0, 0.0)).obstacle,
                         "bead(line 2)")

    def test_a_sharp_continuous_corner_is_labelled_a_likely_artifact(self):
        event = self.index()
        self.assertEqual(event["owner"], 1)
        self.assertTrue(event["adjacent"])
        self.assertTrue(event["artifact"])
        self.assertEqual(event["reason"], "likely continuous-corner artifact")

    def test_a_descending_segment_keeps_the_strike_label(self):
        event = self.index(second_z=-1.0)
        self.assertEqual(event["owner"], 1)
        self.assertFalse(event["artifact"])
        self.assertEqual(event["reason"], "tooling contact; no artifact evidence")


if __name__ == "__main__":
    unittest.main()
