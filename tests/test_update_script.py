from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _bash():
    if os.name == "posix":
        return shutil.which("bash")


    git = shutil.which("git")
    for folder in Path(git).resolve().parents if git else ():
        if (folder / "bin" / "bash.exe").exists() and (folder / "usr" / "bin").is_dir():
            return str(folder / "bin" / "bash.exe")
    return None


BASH = _bash()
STUB_PYTHON = """#!/bin/sh
printf 'python %s | dsn=%s\\n' "$*" "${WAAM_RUN_LOG_PG_DSN-unset}" >> "$STUB_LOG"
case "$*" in
    --version) echo "Python 3.10.12" ;;
    *"run_log_postgres migrate"*)
        if [ -n "$STUB_MIGRATE_FAIL" ]; then echo "ERROR Read role 'waam_read' does not exist." >&2; exit 1; fi
        echo "runlog: applied 0002" ;;
esac
exit 0
"""
STUB_SYSTEMCTL = """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$STUB_LOG"
[ "$1" = show ] && printf 'EnvironmentFiles=%s\\n' "$STUB_ENV_FILES"
exit 0
"""
STUB_SUDO = '#!/bin/sh\nexec "$@"\n'


class _Gated(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@unittest.skipUnless(BASH, "deploy/update.sh needs bash")
class TestUpdateMigratesRunLogs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="waam-update-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        repo = self.tmp / "repo"
        (repo / "deploy").mkdir(parents=True)
        (repo / "src" / "waam_pipeline").mkdir(parents=True)
        (repo / "src" / "waam_pipeline" / "server.py").write_text("", encoding="utf-8")

        (repo / "deploy" / "update.sh").write_bytes((ROOT / "deploy" / "update.sh").read_bytes().replace(b"\r\n", b"\n"))
        self.script = repo / "deploy" / "update.sh"
        for name, body in (("venv/bin/python", STUB_PYTHON), ("bin/systemctl", STUB_SYSTEMCTL),
                           ("bin/sudo", STUB_SUDO)):
            path = self.tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body.encode())
            path.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.run_logs_env = self.tmp / "run-logs.env"

        (self.tmp / "gitconfig").write_text("", encoding="utf-8")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                        PATH=str(self.tmp / "bin") + os.pathsep + os.environ["PATH"],
                        STUB_LOG=self.log.as_posix(), WAAM_RUN_LOGS_ENV=self.run_logs_env.as_posix(),
                        STUB_ENV_FILES=f"/etc/waam/workbench.env (ignore_errors=yes) "
                                       f"{self.run_logs_env.as_posix()} (ignore_errors=yes)")
        self.env.pop("WAAM_RUN_LOG_PG_DSN", None)
        identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
        for command in (["init", "-q"], ["add", "-A"], [*identity, "commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(repo), *command], env=self.env, check=True,
                           capture_output=True, timeout=60)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Gated)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def update(self, *flags, **env):
        command = [BASH, self.script.as_posix(), "--skip-tests", "--yes", "--service", "waam-test-unit",
                   "--venv", (self.tmp / "venv").as_posix(), "--backup-dir", (self.tmp / "backups").as_posix(),
                   "--port", str(self.port), *flags]
        result = subprocess.run(command, env={**self.env, **env}, capture_output=True, text=True, timeout=120)
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return result, calls

    @staticmethod
    def migrations(calls):
        return [line for line in calls if "run_log_postgres migrate" in line]

    def test_without_the_run_logs_file_nothing_is_migrated(self):
        result, calls = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run logs are not on PostgreSQL here, nothing to migrate", result.stdout)
        self.assertEqual(self.migrations(calls), [])
        self.assertIn("systemctl restart waam-test-unit", calls)

    def test_configured_run_logs_migrate_before_the_restart(self):
        self.run_logs_env.write_text("# run logs\nWAAM_RUN_LOG_PG_DSN=dbname=waam_runs\n", encoding="utf-8")
        result, calls = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runlog: applied 0002", result.stdout)
        migrations = self.migrations(calls)
        self.assertEqual(migrations, ["python -m waam_pipeline.run_log_postgres migrate | dsn=dbname=waam_runs"])
        self.assertLess(calls.index(migrations[0]), calls.index("systemctl restart waam-test-unit"))
        self.assertTrue(all(line.endswith("dsn=unset") for line in calls
                            if line.startswith("python") and line not in migrations), calls)
        result, calls = self.update("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would run: WAAM_RUN_LOG_PG_DSN=dbname=waam_runs", result.stdout)
        self.assertEqual(self.migrations(calls), [])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "needs POSIX permissions and a non-root user")
    def test_an_unsearchable_config_directory_fails_instead_of_skipping(self):
        folder = self.tmp / "etc-waam"
        folder.mkdir()
        (folder / "run-logs.env").write_text("WAAM_RUN_LOG_PG_DSN=dbname=waam_runs\n", encoding="utf-8")
        folder.chmod(0o600)
        self.addCleanup(folder.chmod, 0o700)
        result, calls = self.update(WAAM_RUN_LOGS_ENV=(folder / "run-logs.env").as_posix())
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("cannot search", result.stderr)
        self.assertEqual(self.migrations(calls), [])
        self.assertFalse(any("restart" in line for line in calls), calls)

    def test_a_failed_or_unsafe_migration_stops_the_deploy_before_the_restart(self):
        self.run_logs_env.write_text("WAAM_RUN_LOG_PG_DSN=dbname=waam_runs\n", encoding="utf-8")
        cases = (({"STUB_MIGRATE_FAIL": "1"}, "", ("migration failed", "'waam_read' does not exist"), 1),
                 ({"STUB_ENV_FILES": "/etc/waam/workbench.env (ignore_errors=yes)"}, "", ("does not load",), 0),
                 ({}, "WAAM_RUN_LOG_PG_DSN=dbname=waam_runs password=private-value\n", ("password",), 0),
                 ({}, "WAAM_RUN_LOG_PG_DSN=\n", ("sets it empty",), 0))
        for env, contents, shown, migrated in cases:
            with self.subTest(reason=shown[0]):
                if contents:
                    self.run_logs_env.write_text(contents, encoding="utf-8")
                result, calls = self.update(**env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("FAILED", result.stderr)
                for text in shown:
                    self.assertIn(text, result.stderr)
                self.assertEqual(len(self.migrations(calls)), migrated)
                self.assertFalse(any("restart" in line for line in calls), calls)
                self.assertNotIn("private-value", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
