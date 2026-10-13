import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.lint_pages import collect_page, remove_vendor


class TestLintExtraction(unittest.TestCase):
    def test_only_executable_scripts_are_collected_in_browser_order(self):
        result = collect_page("fixture", '<script type="application/json">{"value": 1}</script>'
                              '<script>const first=1;</script><script>console.log(first);</script>'
                              '<button onclick="window.print()">Print</button>')
        self.assertEqual([item["code"] for item in result["scripts"]],
                         ["const first=1;", "console.log(first);"])
        self.assertEqual(result["handlers"], ["window.print()"])

    def test_missing_unclosed_or_unhandled_scripts_fail(self):
        for markup in ("<p>No code</p>", '<script type="application/json">{}</script>',
                       "<script>const incomplete=1;", '<script src="other.js"></script>',
                       '<script type="module">export const value=1;</script>'):
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                collect_page("fixture", markup)

    def test_vendor_removal_keeps_authored_code_and_requires_an_exact_match(self):
        vendor = "/* Upstream */ const upstream=1;"
        authored = "window.renderer=missingIdentifier();"
        result = remove_vendor(f"<script>{vendor}\n{authored}</script>", vendor)
        self.assertIn(authored, result)
        self.assertNotIn("const upstream", result)
        for markup in (authored, vendor + vendor):
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                remove_vendor(markup, vendor)


if __name__ == "__main__":
    unittest.main()
