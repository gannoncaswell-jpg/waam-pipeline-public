import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.robots import hc10

DUMP = (ROOT / "docs" / "cell-data" / "controller-backup"
        / "cobot-hc10-dump-2026-09-15" / "R1-HOME.JBI")


class TestHc10PulseScale(unittest.TestCase):
    def test_r1_home_taught_record_decodes_to_a_sane_home(self):
        if not DUMP.exists():
            raise unittest.SkipTest(f"missing HC dump job {DUMP}")
        text = DUMP.read_text(errors="replace")
        m = re.search(r"^P00100=(-?\d+(?:,-?\d+){5})", text, re.MULTILINE)
        self.assertIsNotNone(m, "R1-HOME.JBI P00100 taught record not found")
        pulses = [int(x) for x in m.group(1).split(",")]
        q = [p / r for p, r in zip(pulses, hc10.PULSES_PER_DEG)]


        for k, j in enumerate(hc10.HC10DTP_JOINTS):
            self.assertTrue(
                j.lower_deg <= q[k] <= j.upper_deg,
                f"axis {'SLURBT'[k]} = {q[k]:.2f} outside [{j.lower_deg}, {j.upper_deg}]",
            )


        expected = [-1.12, 17.97, -15.34, 0.0, -58.67, 0.0]
        for k, (got, exp) in enumerate(zip(q, expected)):
            self.assertAlmostEqual(
                got, exp, delta=0.05,
                msg=f"axis {'SLURBT'[k]}: {got:.2f} != {exp:.2f} -- PULSES_PER_DEG changed",
            )
