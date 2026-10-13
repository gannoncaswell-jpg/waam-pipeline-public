import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import passkeys

try:
    from soft_webauthn import SoftWebauthnDevice
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    _READY = passkeys.available()
except Exception:
    _READY = False

URL = "https://waam-server.example-tailnet.ts.net"
ORIGIN = "https://waam-server.example-tailnet.ts.net"
EMAIL = "test_operator@letu.edu"


def _b(x):
    return bytes_to_base64url(x)


def _create_response(device, options_json):


    o = json.loads(options_json)
    att = device.create({"publicKey": {
        "challenge": base64url_to_bytes(o["challenge"]),
        "rp": {"id": o["rp"]["id"], "name": o["rp"]["name"]},
        "user": {"id": base64url_to_bytes(o["user"]["id"]),
                 "name": o["user"]["name"], "displayName": o["user"]["displayName"]},
        "pubKeyCredParams": [{"alg": -7, "type": "public-key"}],
    }}, ORIGIN)
    return json.dumps({
        "id": _b(att["rawId"]), "rawId": _b(att["rawId"]),
        "response": {"attestationObject": _b(att["response"]["attestationObject"]),
                     "clientDataJSON": _b(att["response"]["clientDataJSON"])},
        "type": "public-key", "clientExtensionResults": {}})


def _get_response(device, options_json, origin=ORIGIN):
    o = json.loads(options_json)
    opts = {"publicKey": {"challenge": base64url_to_bytes(o["challenge"]),
                          "rpId": o["rpId"]}}
    assertion = device.get(opts, origin)
    r = assertion["response"]
    return json.dumps({
        "id": _b(assertion["rawId"]), "rawId": _b(assertion["rawId"]),
        "response": {"authenticatorData": _b(r["authenticatorData"]),
                     "clientDataJSON": _b(r["clientDataJSON"]),
                     "signature": _b(r["signature"]),
                     "userHandle": _b(r["userHandle"]) if r.get("userHandle") else None},
        "type": "public-key", "clientExtensionResults": {}})


@unittest.skipUnless(_READY, "webauthn + soft-webauthn needed")
class TestPasskeyCeremonies(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-pk-"))
        self.store = passkeys.WebAuthnStore(self.tmp / ".waam-webauthn.json")
        self.device = SoftWebauthnDevice()
        passkeys.forget_challenges()
        self.addCleanup(passkeys.forget_challenges)

    def _enroll(self, label="my laptop"):
        handle, opts = passkeys.registration_options(URL, EMAIL, self.store.list_for(EMAIL))
        cred_json = _create_response(self.device, opts)
        email, cred = passkeys.verify_registration(URL, handle, cred_json, label)
        self.store.add(email, cred)
        return cred

    def test_register_then_authenticate_round_trip(self):
        cred = self._enroll()
        self.assertEqual(self.store.list_for(EMAIL)[0].id, cred.id)

        handle, aopts = passkeys.authentication_options(URL)
        assertion = _get_response(self.device, aopts)
        cid = passkeys.credential_id_from(assertion)
        found = self.store.find(cid)
        self.assertIsNotNone(found)
        email, stored = found
        self.assertEqual(email, EMAIL)
        new_count = passkeys.verify_authentication(URL, handle, assertion, stored)
        self.store.record_use(email, cid, new_count)
        self.assertGreaterEqual(self.store.list_for(EMAIL)[0].sign_count, 0)

    def test_wrong_origin_is_rejected(self):
        self._enroll()
        handle, aopts = passkeys.authentication_options(URL)
        assertion = _get_response(self.device, aopts, origin="https://evil.example.com")
        found = self.store.find(passkeys.credential_id_from(assertion))
        with self.assertRaises(passkeys.WebAuthnError):
            passkeys.verify_authentication(URL, handle, assertion, found[1])

    def test_tampered_signature_is_rejected(self):
        self._enroll()
        handle, aopts = passkeys.authentication_options(URL)
        assertion = json.loads(_get_response(self.device, aopts))
        sig = base64url_to_bytes(assertion["response"]["signature"])
        assertion["response"]["signature"] = _b(bytes([sig[0] ^ 0xFF]) + sig[1:])
        found = self.store.find(assertion["id"])
        with self.assertRaises(passkeys.WebAuthnError):
            passkeys.verify_authentication(URL, handle, json.dumps(assertion), found[1])

    def test_challenge_is_single_use(self):
        self._enroll()
        handle, aopts = passkeys.authentication_options(URL)
        assertion = _get_response(self.device, aopts)
        found = self.store.find(passkeys.credential_id_from(assertion))
        passkeys.verify_authentication(URL, handle, assertion, found[1])
        with self.assertRaises(passkeys.WebAuthnError):
            passkeys.verify_authentication(URL, handle, assertion, found[1])

    def test_store_add_find_remove(self):
        cred = self._enroll()
        self.assertEqual(self.store.find(cred.id)[0], EMAIL)
        self.assertTrue(self.store.remove(EMAIL, cred.id))
        self.assertEqual(self.store.list_for(EMAIL), [])
        self.assertIsNone(self.store.find(cred.id))

    def test_rp_id_and_origin_from_url(self):
        rp, origin = passkeys.rp_id_and_origin("https://waam-server.example-tailnet.ts.net/signin")
        self.assertEqual(rp, "waam-server.example-tailnet.ts.net")
        self.assertEqual(origin, "https://waam-server.example-tailnet.ts.net")


if __name__ == "__main__":
    unittest.main()
