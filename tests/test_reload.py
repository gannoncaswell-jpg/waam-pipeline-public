import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import server


class TestMakeServerSocketHandoff(unittest.TestCase):
    def test_binds_when_no_inherited_fd(self):
        os.environ.pop(server._LISTEN_FD_ENV, None)
        httpd = server._make_server("127.0.0.1", 0)
        try:
            self.assertEqual(httpd.socket.getsockname()[0], "127.0.0.1")
        finally:
            httpd.server_close()

    def test_adopts_inherited_socket_without_rebinding(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        addr = listener.getsockname()
        os.environ[server._LISTEN_FD_ENV] = str(listener.fileno())
        try:
            httpd = server._make_server("127.0.0.1", addr[1])
            self.assertEqual(httpd.socket.getsockname(), addr)
            self.assertEqual(httpd.server_address, addr)
            self.assertNotIn(server._LISTEN_FD_ENV, os.environ)
            httpd.socket.detach()
        finally:
            os.environ.pop(server._LISTEN_FD_ENV, None)
            listener.close()


def _serving(url):
    try:
        urllib.request.urlopen(url, timeout=1)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _wait(cond, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False


_BANNER = re.compile(r"^WAAM Studio workbench.*http://127\.0\.0\.1:(\d+)")


def _collect_ports(stream, ports):


    for line in stream:
        found = _BANNER.match(line)
        if found:
            ports.append(found.group(1))


@unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP is Unix-only")
class TestSighupReload(unittest.TestCase):
    def test_sighup_reexecs_in_place_and_keeps_serving(self):


        logs = Path(tempfile.mkdtemp(prefix="waam-reload-"))
        self.addCleanup(shutil.rmtree, logs, ignore_errors=True)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), WAAM_RELOAD_GRACE="0",
                   WAAM_ACTIVITY_LOG=str(logs / "activity.log"),
                   WAAM_ACCESS_LOG=str(logs / "access.log"))
        proc = subprocess.Popen(
            [sys.executable, "-m", "waam_pipeline.server",
             "--host", "127.0.0.1", "--port", "0", "--no-cell", "--token", "t"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        ports: list[str] = []
        reader = threading.Thread(target=_collect_ports, args=(proc.stdout, ports),
                                  daemon=True)
        reader.start()
        try:
            self.assertTrue(_wait(lambda: ports, 20), "server never printed its port")
            url = f"http://127.0.0.1:{ports[0]}/?k=t"
            self.assertTrue(_wait(lambda: _serving(url), 20), "server never came up")
            os.kill(proc.pid, signal.SIGHUP)


            self.assertTrue(_wait(lambda: len(ports) > 1, 30),
                            "no successor banner after SIGHUP")
            self.assertIsNone(proc.poll(), "process exited instead of re-exec")
            self.assertEqual(ports[1], ports[0], "successor did not keep the socket")
            self.assertTrue(_wait(lambda: _serving(url), 20), "not serving after reload")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            reader.join(timeout=5)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
