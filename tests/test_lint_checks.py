import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import lint_checks


class TestStaticAnalysis(unittest.TestCase):
    def test_required_project_linters(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "lint_checks.py")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ruff_rejects_undefined_python_name(self):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--stdin-filename",
             "src/waam_pipeline/lint_negative_control.py", "-"],
            input="print(waam_name_that_does_not_exist)\n", cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("F821", result.stdout)

    def test_bandit_rejects_shell_injection_pattern(self):
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-ll", "-ii", "-f", "json", "-"],
            input="import subprocess\ndef run(user_input):\n    subprocess.run(user_input, shell=True)\n",
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("B602", {item["test_id"] for item in json.loads(result.stdout)["results"]})

    def test_bandit_incomplete_scan_cannot_pass(self):
        for errors, loc in (([{"reason": "syntax error"}], 20), ([], 0)):
            with self.subTest(errors=errors, loc=loc):
                report = {"errors": errors, "results": [], "metrics": {"_totals": {"loc": loc}}}
                result = subprocess.CompletedProcess([], 0, json.dumps(report), "")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertNotEqual(lint_checks.bandit_verdict(result), 0)

    def test_missing_node_is_an_error(self):
        with patch.object(lint_checks.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Node.js is required"):
                lint_checks.run_check("javascript")


if __name__ == "__main__":
    unittest.main()
