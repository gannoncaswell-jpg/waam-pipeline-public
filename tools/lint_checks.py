'Run the same required static checks used by unittest and CI.'
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIRS = ("src", "tests", "examples", "sim", "ros2", "tools", "hardware", "deploy")
SECURITY_DIRS = tuple(path for path in PYTHON_DIRS if path != "tests")


def require_package(name: str) -> None:
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}(?:\[[^\]]+\])?==([^\s;]+)$",
                      requirements, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"Missing exact {name} pin in requirements-dev.txt")
    try:
        installed = metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"{name} is required. Run: python -m pip install -r requirements-dev.txt") from exc
    if installed != match[1]:
        raise RuntimeError(f"{name} {match[1]} is required; found {installed}. "
                           "Run: python -m pip install -r requirements-dev.txt")


def execute(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def bandit_verdict(result: subprocess.CompletedProcess[str]) -> int:
    try:
        report = json.loads(result.stdout)
        errors = report["errors"]
        findings = report["results"]
        loc = report["metrics"]["_totals"]["loc"]
    except (ValueError, KeyError, TypeError):
        print("Bandit did not produce a complete JSON report.", file=sys.stderr)
        print(result.stdout + result.stderr, file=sys.stderr)
        return 1
    if errors or not isinstance(loc, (int, float)) or loc <= 0:
        print(f"Bandit did not scan all requested code: {errors}; scanned lines: {loc}",
              file=sys.stderr)
        return 1
    for finding in findings:
        print(f"{finding['filename']}:{finding['line_number']}: "
              f"{finding['test_id']} {finding['issue_text']}")
    if result.returncode or findings:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return 1
    print(f"Bandit passed ({loc} lines; medium+ severity and confidence).")
    return 0


def run_check(name: str) -> int:
    if name == "ruff":
        require_package("ruff")
        result = execute([sys.executable, "-m", "ruff", "check", *PYTHON_DIRS])
    elif name == "bandit":
        require_package("bandit")
        return bandit_verdict(execute([
            sys.executable, "-m", "bandit", "-r", *SECURITY_DIRS,
            "-ll", "-ii", "-f", "json", "--quiet",
        ]))
    else:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for ESLint. Install Node 24 LTS and run npm ci --ignore-scripts.")
        result = execute([node, str(ROOT / "tools" / "lint-js.mjs"),
                          "--python", sys.executable])
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return int(result.returncode != 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("ruff", "bandit", "javascript"))
    args = parser.parse_args(argv)
    failed = False
    for name in (args.check,) if args.check else ("ruff", "bandit", "javascript"):
        print(f"Checking {name}...", flush=True)
        try:
            failed |= bool(run_check(name))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"{name} could not run: {exc}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
