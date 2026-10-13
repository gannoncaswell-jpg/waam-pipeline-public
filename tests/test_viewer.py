import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import parse
from waam_pipeline.clearance import check
from waam_pipeline.viewer import write_html


class TestViewer(unittest.TestCase):
    def test_emit_square(self):
        tp = parse((ROOT / "examples" / "square.gcode").read_text(encoding="utf-8"))
        out = ROOT / "examples" / "square_viewer_test.html"
        write_html(tp, check(tp), out, "square")
        html = out.read_text(encoding="utf-8")
        self.assertIn("<canvas", html)
        self.assertIn("<title>square</title>", html)
        data = json.loads(re.search(r"const DATA = (\{.*?\});\n", html).group(1))
        self.assertEqual(len(data["moves"]), 12)
        self.assertEqual(data["violations"], [])
        self.assertEqual(sum(m["d"] for m in data["moves"]), 8)
        out.unlink()

    def test_violations_embedded(self):
        gcode = "G90\nM83\n" + "".join(
            f";LAYER:{k}\nG0 X0 Y{y} Z{3 * (k + 1)}\nG1 X60 Y{y} Z{3 * (k + 1)} E1\n"
            for y in (0.0, 15.0)
            for k in range(8)
        )
        tp = parse(gcode)
        out = ROOT / "examples" / "viol_viewer_test.html"
        write_html(tp, check(tp), out)
        data = json.loads(
            re.search(r"const DATA = (\{.*?\});\n", out.read_text(encoding="utf-8")).group(1)
        )
        self.assertTrue(data["violations"])
        self.assertIn("obstacle", data["violations"][0])
        out.unlink()


if __name__ == "__main__":
    unittest.main()
