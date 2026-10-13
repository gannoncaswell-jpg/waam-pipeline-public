from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import accounts, oidc, passkeys, server, ui_preferences

_SERVER_GLOBALS = (
    "ACTIVITY_LOG", "ACCESS_LOG", "SAVES_DIR", "EXCEL_CREDENTIALS", "ACCOUNTS", "WEBAUTHN", "UI_PREFERENCES",
    "_TOKENS", "_TOKEN_GATE", "_TOKENS_STAMP", "_AUTH", "_AUTH_STAMP",
    "_tokens_file", "_token_file", "_refresh_auth", "_refresh_tokens",
)


@contextlib.contextmanager
def isolated_server(*, tokens=None, token_gate=False, stub_auth=True,
                    prefix="waam-test-"):


    saved = {name: getattr(server, name) for name in _SERVER_GLOBALS}
    saved_files = (accounts._accounts_file, oidc._auth_config_file)
    root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        server.ACTIVITY_LOG = root / "activity.log"
        server.ACCESS_LOG = root / "access.log"
        server.SAVES_DIR = root / "saves"
        server.EXCEL_CREDENTIALS = root / ".waam-run-log-excel.json"
        server.ACCOUNTS = accounts.Store(root / ".waam-accounts.json")
        server.WEBAUTHN = passkeys.WebAuthnStore(root / ".waam-webauthn.json")
        server.UI_PREFERENCES = ui_preferences.Store(root / ".waam-ui-preferences")
        accounts._accounts_file = lambda: root / ".waam-accounts.json"
        oidc._auth_config_file = lambda: root / ".waam-auth.json"
        server._tokens_file = lambda: root / ".waam-tokens.json"
        server._token_file = lambda: root / ".waam-token"
        server._TOKENS = dict(tokens or {})
        server._TOKEN_GATE = token_gate
        server._TOKENS_STAMP = None
        server._AUTH = None
        server._AUTH_STAMP = None
        if stub_auth:
            server._refresh_auth = lambda: None
            server._refresh_tokens = lambda: server._TOKENS
        oidc.forget_everything()
        yield SimpleNamespace(
            root=root,
            auth_config=root / ".waam-auth.json",
            accounts_file=root / ".waam-accounts.json",
            webauthn_file=root / ".waam-webauthn.json",
            saves=root / "saves",
            access_log=root / "access.log",
            activity_log=root / "activity.log",
        )
    finally:
        for name, value in saved.items():
            setattr(server, name, value)
        accounts._accounts_file, oidc._auth_config_file = saved_files
        oidc.forget_everything()
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def serving(handler=None):

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler or server._Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


class IsolatedServerCase(unittest.TestCase):


    TOKENS: dict = {}
    TOKEN_GATE = False
    STUB_AUTH = True
    PREFIX = "waam-test-"

    @classmethod
    def setUpClass(cls):
        cls._isolation = contextlib.ExitStack()
        try:
            cls.iso = cls._isolation.enter_context(isolated_server(
                tokens=cls.TOKENS, token_gate=cls.TOKEN_GATE,
                stub_auth=cls.STUB_AUTH, prefix=cls.PREFIX))
            cls.tmp = cls.iso.root
            cls.httpd = cls._isolation.enter_context(serving())
        except BaseException:
            cls._isolation.close()
            raise
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls._isolation.close()
