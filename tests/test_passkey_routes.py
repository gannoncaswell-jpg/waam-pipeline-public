import json
import sys
import unittest
from pathlib import Path
from urllib import request as urlreq, error as urlerr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import passkeys, server
from tests._support import IsolatedServerCase

try:
    from soft_webauthn import SoftWebauthnDevice
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    _READY = passkeys.available()
except Exception:
    _READY = False

PUBLIC_URL = "https://localhost"
ORIGIN = "https://localhost"
CFG = {"provider": "local", "public_url": PUBLIC_URL, "open_signin": True,
       "verify_email": False, "approval_required": False}


def _b(x):
    return bytes_to_base64url(x)


@unittest.skipUnless(_READY, "webauthn + soft-webauthn needed")
class TestPasskeyRoutes(IsolatedServerCase):
    STUB_AUTH = False
    PREFIX = "waam-pkroutes-"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.iso.auth_config.write_text(json.dumps(CFG), encoding="utf-8")


    def _req(self, path, data=None, cookie=None, form=None):


        headers = {"Origin": self.base, "Sec-Fetch-Site": "same-origin"}
        body = None
        if form is not None:
            body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        r = urlreq.Request(self.base + path, data=body,
                           method="POST" if (body is not None) else "GET", headers=headers)
        opener = urlreq.build_opener(type("NR", (urlreq.HTTPRedirectHandler,),
                                          {"redirect_request": lambda *a, **k: None})())
        try:
            resp = opener.open(r, timeout=10)
            return resp.status, resp.read(), resp.headers.get("Set-Cookie", "")
        except urlerr.HTTPError as e:
            return e.code, e.read(), e.headers.get("Set-Cookie", "")

    def _session_cookie(self, email="test_operator@letu.edu", pw="passphrase-123"):

        code, _b_, setck = self._req("/auth/register", form={"email": email, "password": pw})
        self.assertIn(server.SESSION_COOKIE, setck, "register should issue a session")
        return setck.split(";", 1)[0]

    def _enroll(self, device, cookie):
        code, body, _ = self._req("/api/passkey/register/options", data={}, cookie=cookie)
        self.assertEqual(code, 200, body)
        j = json.loads(body)
        o = j["options"]
        att = device.create({"publicKey": {
            "challenge": base64url_to_bytes(o["challenge"]),
            "rp": {"id": o["rp"]["id"], "name": o["rp"]["name"]},
            "user": {"id": base64url_to_bytes(o["user"]["id"]),
                     "name": o["user"]["name"], "displayName": o["user"]["displayName"]},
            "pubKeyCredParams": [{"alg": -7, "type": "public-key"}]}}, ORIGIN)
        cred = {"id": _b(att["rawId"]), "rawId": _b(att["rawId"]),
                "response": {"attestationObject": _b(att["response"]["attestationObject"]),
                             "clientDataJSON": _b(att["response"]["clientDataJSON"])},
                "type": "public-key", "clientExtensionResults": {}}
        code, body, _ = self._req("/api/passkey/register/verify",
                                  data={"handle": j["handle"], "credential": cred, "label": "test key"},
                                  cookie=cookie)
        self.assertEqual(code, 200, body)
        return j["handle"]

    def _passkey_login(self, device):
        code, body, _ = self._req("/auth/passkey/login/options", data={})
        self.assertEqual(code, 200, body)
        j = json.loads(body)
        o = j["options"]
        assertion = device.get({"publicKey": {"challenge": base64url_to_bytes(o["challenge"]),
                                               "rpId": o["rpId"]}}, ORIGIN)
        r = assertion["response"]
        cred = {"id": _b(assertion["rawId"]), "rawId": _b(assertion["rawId"]),
                "response": {"authenticatorData": _b(r["authenticatorData"]),
                             "clientDataJSON": _b(r["clientDataJSON"]),
                             "signature": _b(r["signature"]),
                             "userHandle": _b(r["userHandle"]) if r.get("userHandle") else None},
                "type": "public-key", "clientExtensionResults": {}}
        return self._req("/auth/passkey/login/verify", data={"handle": j["handle"], "credential": cred})

    def test_enroll_then_passkey_signin_issues_a_working_session(self):
        device = SoftWebauthnDevice()
        cookie = self._session_cookie()
        self._enroll(device, cookie)
        code, body, setck = self._passkey_login(device)
        self.assertEqual(code, 200, body)
        self.assertIn(server.SESSION_COOKIE, setck, "passkey sign-in must issue a session")

        pk_cookie = setck.split(";", 1)[0]
        code, body, _ = self._req("/api/whoami", cookie=pk_cookie)
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body).get("email"), "test_operator@letu.edu")

    def _set_admins(self, *emails):
        (self.tmp / ".waam-auth.json").write_text(
            json.dumps(dict(CFG, admins=list(emails))), encoding="utf-8")
        server._AUTH = server._AUTH_STAMP = None
        self.addCleanup(self._clear_admins)

    def _clear_admins(self):
        (self.tmp / ".waam-auth.json").write_text(json.dumps(CFG), encoding="utf-8")
        server._AUTH = server._AUTH_STAMP = None

    def test_an_admin_passkey_must_verify_the_user(self):


        email = "admin-pk@letu.edu"
        device = SoftWebauthnDevice()
        cookie = self._session_cookie(email)
        self._enroll(device, cookie)
        code, body, _ = self._passkey_login(device)
        self.assertEqual(code, 200, body)

        self._set_admins(email)
        code, body, _ = self._passkey_login(device)
        self.assertEqual(code, 400, body)
        self.assertIn(b"PIN", body)
        code, body, _ = self._req("/api/passkey/register/options", data={}, cookie=cookie)
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["options"]["authenticatorSelection"]
                         ["userVerification"], "required")

    def test_enroll_requires_an_account(self):
        code, body, _ = self._req("/api/passkey/register/options", data={})
        self.assertIn(code, (401, 403), body)

    def test_unknown_passkey_is_rejected(self):


        device = SoftWebauthnDevice()
        device.create({"publicKey": {
            "challenge": b"x" * 32, "rp": {"id": "localhost", "name": "WAAM Studio"},
            "user": {"id": b"stranger", "name": "x@y.z", "displayName": "x"},
            "pubKeyCredParams": [{"alg": -7, "type": "public-key"}]}}, ORIGIN)
        code, body, _ = self._passkey_login(device)
        self.assertEqual(code, 400, body)


if __name__ == "__main__":
    unittest.main()
