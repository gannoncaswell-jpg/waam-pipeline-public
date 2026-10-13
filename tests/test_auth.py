import base64
import contextlib
import io
import json
import re
import shlex
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import accounts, authcheck, mailer, oidc, server
from tests._support import IsolatedServerCase

CLIENT_ID = "test-client-id.apps.example"
PUBLIC_URL = "https://cell.example.ts.net:8443"
REDIRECT_URI = PUBLIC_URL + "/auth/callback"
TENANT = "11111111-2222-3333-4444-555555555555"


class _NoRedirect(urllib.request.HTTPRedirectHandler):


    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", data=None, headers=None):

    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with _OPENER.open(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _seg(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def make_id_token(**claims) -> str:


    payload = {"iss": "https://accounts.google.com", "aud": CLIENT_ID,
               "exp": int(time.time()) + 3600, "sub": "subject-1"}
    payload.update(claims)
    return _seg({"alg": "RS256", "typ": "JWT"}) + "." + _seg(payload) + ".not-checked"


def sid_from(headers) -> str | None:
    jar = SimpleCookie(headers.get("Set-Cookie", ""))
    return jar[server.SESSION_COOKIE].value if server.SESSION_COOKIE in jar else None


class _AuthServerCase(IsolatedServerCase):


    CONFIG: dict | None = None
    STUB_AUTH = False
    PREFIX = "waam-auth-"

    @classmethod
    def setUpClass(cls):
        cls._saved_post_form = oidc._post_form
        cls._ws_before = set(server._STATES)
        cls.token_calls = []
        cls.staged = None

        def stub_post_form(url, data):
            cls.token_calls.append((url, dict(data)))
            if cls.staged is None:
                raise AssertionError("token exchange with nothing staged")

            return {"id_token": cls.staged, "access_token": "at-must-never-appear",
                    "token_type": "Bearer"}

        oidc._post_form = stub_post_form
        try:
            super().setUpClass()
            cls.cfg_path = cls.iso.auth_config
            if cls.CONFIG is not None:
                cls.write_config(cls.CONFIG)
        except BaseException:
            oidc._post_form = cls._saved_post_form
            raise

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        oidc._post_form = cls._saved_post_form
        for ws in set(server._STATES) - cls._ws_before:
            server._STATES.pop(ws, None)

    @classmethod
    def write_config(cls, cfg: dict) -> None:
        cls.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


    def begin(self) -> dict:


        code, _, headers = http(f"{self.base}/auth/login")
        self.assertEqual(code, 302)
        started = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(headers["Location"]).query))
        jar = SimpleCookie(headers.get("Set-Cookie", ""))
        started["_binding"] = (jar[server.LOGIN_COOKIE].value
                               if server.LOGIN_COOKIE in jar else "")
        return started

    def callback(self, params: dict, id_token: str | None, binding: str | None = ""):
        type(self).staged = id_token
        qs = urllib.parse.urlencode(params)
        headers = ({"Cookie": f"{server.LOGIN_COOKIE}={binding}"} if binding else {})
        return http(f"{self.base}/auth/callback?{qs}", headers=headers)

    def sign_in(self, **claims):

        started = self.begin()
        token = make_id_token(nonce=started["nonce"], **claims)
        return self.callback({"code": "auth-code-xyz", "state": started["state"]},
                             token, started["_binding"])

    def sign_in_ok(self, **claims) -> str:
        code, _, headers = self.sign_in(**claims)
        self.assertEqual(code, 302, "sign-in should have been accepted")
        sid = sid_from(headers)
        self.assertIsNotNone(sid)
        return sid

    def cookie(self, sid: str) -> dict:
        return {"Cookie": f"{server.SESSION_COOKIE}={sid}"}

    def log_rows(self) -> list[dict]:
        if not server.ACCESS_LOG.exists():
            return []
        return [json.loads(line) for line
                in server.ACCESS_LOG.read_text(encoding="utf-8").splitlines() if line]


GOOGLE_CFG = {
    "provider": "google",
    "client_id": CLIENT_ID,
    "client_secret": "top-secret-value",
    "public_url": PUBLIC_URL,
    "users": {"operator@letu.edu": {"name": "operator"}},
    "allow_domains": ["letu.edu"],
}


def strict(**extra) -> dict:
    cfg = dict(GOOGLE_CFG, open_signin=False)
    cfg.update(extra)
    return cfg


class TestNoAuthConfig(_AuthServerCase):
    TOKENS = {"tok-plain-0001": "plain"}
    CONFIG = None

    def test_auth_routes_do_not_exist(self):
        for path in ("/auth/login", "/auth/callback", "/auth/logout", "/auth/x"):
            code, body, _ = http(f"{self.base}{path}")
            self.assertEqual(code, 404, path)
            self.assertEqual(body, b"not found")

    def test_denial_is_the_old_plain_text(self):
        code, body, _ = http(f"{self.base}/?k=nope")
        self.assertEqual(code, 401)
        self.assertTrue(body.startswith(b"waam workbench: append ?k="))
        self.assertNotIn(b"<html", body.lower())

    def test_the_signin_door_still_leads_somewhere(self):


        for path in ("/signin", "/sign-in", "/login", "/signin/"):
            code, _, headers = http(f"{self.base}{path}")
            self.assertEqual(code, 302, path)
            self.assertEqual(headers["Location"], "/", path)

    def test_token_link_unchanged(self):
        code, page, _ = http(f"{self.base}/?k=tok-plain-0001")
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"plain"', page)
        self.assertIn(b'"email":null', page)


class TestLoginRedirect(_AuthServerCase):
    CONFIG = GOOGLE_CFG

    def test_redirects_to_google_with_every_required_parameter(self):
        code, _, headers = http(f"{self.base}/auth/login")
        self.assertEqual(code, 302)
        target = urllib.parse.urlparse(headers["Location"])
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}",
                         oidc.GOOGLE_AUTHORIZE)
        q = dict(urllib.parse.parse_qsl(target.query))
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["redirect_uri"], REDIRECT_URI)
        self.assertEqual(q["scope"], "openid email profile")
        self.assertEqual(q["code_challenge_method"], "S256")
        for key in ("state", "nonce", "code_challenge"):
            self.assertGreaterEqual(len(q[key]), 20, key)

    def test_microsoft_endpoint_carries_the_tenant(self):
        self.write_config(dict(GOOGLE_CFG, provider="microsoft", tenant=TENANT))
        try:
            _, _, headers = http(f"{self.base}/auth/login")
            self.assertIn(f"/{TENANT}/oauth2/v2.0/authorize", headers["Location"])
        finally:
            self.write_config(GOOGLE_CFG)

    def test_every_login_is_a_fresh_secret(self):
        a, b = self.begin(), self.begin()
        self.assertNotEqual(a["state"], b["state"])
        self.assertNotEqual(a["nonce"], b["nonce"])
        self.assertNotEqual(a["code_challenge"], b["code_challenge"])

    def test_pkce_challenge_is_the_hash_of_the_verifier_we_later_send(self):
        started = self.begin()
        self.callback({"code": "c", "state": started["state"]},
                      make_id_token(nonce=started["nonce"],
                                    email="operator@letu.edu", email_verified=True),
                      started["_binding"])
        _, sent = type(self).token_calls[-1]
        digest = sha256(sent["code_verifier"].encode("ascii")).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).decode().rstrip("="),
                         started["code_challenge"])
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["redirect_uri"], REDIRECT_URI)


class TestStateHandling(_AuthServerCase):
    CONFIG = GOOGLE_CFG

    def _refused(self, params, id_token=None, binding=""):
        code, body, headers = self.callback(params, id_token, binding)
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        return body

    def test_unknown_state_never_reaches_the_token_endpoint(self):
        before = len(type(self).token_calls)
        self._refused({"code": "c", "state": "never-issued"})
        self.assertEqual(len(type(self).token_calls), before,
                         "the code must not be spent on an unknown state")

    def test_missing_state_or_code(self):
        self._refused({"code": "c"})
        started = self.begin()
        self._refused({"state": started["state"]})

    def test_expired_state(self):
        started = self.begin()
        oidc._PENDING[started["state"]]["created"] -= oidc.PENDING_TTL_S + 5
        self._refused({"code": "c", "state": started["state"]})

    def test_state_is_single_use(self):
        started = self.begin()
        token = make_id_token(nonce=started["nonce"], email="operator@letu.edu",
                              email_verified=True)
        code, _, headers = self.callback({"code": "c", "state": started["state"]},
                                         token, started["_binding"])
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))
        self._refused({"code": "c", "state": started["state"]}, token,
                      started["_binding"])

    def test_a_callback_from_another_browser_is_refused(self):


        started = self.begin()
        token = make_id_token(nonce=started["nonce"], email="attacker@evil.example",
                              email_verified=True)
        before = len(type(self).token_calls)
        body = self._refused({"code": "c", "state": started["state"]}, token,
                             binding="")
        self.assertIn(b"Sign-in session mismatch", body)
        self.assertEqual(len(type(self).token_calls), before,
                         "an unbound callback must not spend the code either")

    def test_a_mismatched_binding_is_refused(self):
        started = self.begin()
        token = make_id_token(nonce=started["nonce"], email="operator@letu.edu",
                              email_verified=True)
        self._refused({"code": "c", "state": started["state"]}, token,
                      binding="some-other-browsers-value")

    def test_provider_error_is_a_page_not_a_traceback(self):
        code, body, headers = http(
            f"{self.base}/auth/callback?error=access_denied"
            "&error_description=The+user+cancelled")
        self.assertEqual(code, 400)
        self.assertIn(b"The user cancelled", body)
        self.assertIn(b"<html", body.lower())
        self.assertIsNone(sid_from(headers))


class TestClaimValidation(_AuthServerCase):
    CONFIG = GOOGLE_CFG

    def _rejected(self, **claims):
        started = self.begin()
        claims.setdefault("nonce", started["nonce"])
        code, body, headers = self.callback(
            {"code": "c", "state": started["state"]},
            make_id_token(email="operator@letu.edu", email_verified=True, **claims),
            started["_binding"])
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        return body

    def test_wrong_audience(self):
        self.assertIn(b"different application", self._rejected(aud="someone-elses-app"))

    def test_expired_token(self):
        self.assertIn(b"expired", self._rejected(exp=int(time.time()) - 3600))

    def test_missing_expiry(self):
        self._rejected(exp=None)

    def test_mismatched_nonce(self):
        self.assertIn(b"does not answer", self._rejected(nonce="a-different-nonce"))

    def test_wrong_issuer(self):
        self.assertIn(b"wrong issuer", self._rejected(iss="https://evil.example"))

    def test_audience_list_containing_us_is_accepted(self):
        started = self.begin()
        code, _, headers = self.callback(
            {"code": "c", "state": started["state"]},
            make_id_token(nonce=started["nonce"], aud=["other", CLIENT_ID],
                          email="operator@letu.edu", email_verified=True),
            started["_binding"])
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))

    def test_malformed_token_is_refused_cleanly(self):
        started = self.begin()
        code, _, headers = self.callback({"code": "c", "state": started["state"]},
                                         "not.a.jwt", started["_binding"])
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))


class TestMicrosoftClaims(_AuthServerCase):
    CONFIG = dict(GOOGLE_CFG, provider="microsoft", tenant=TENANT)

    def _ms_token(self, nonce, tid=TENANT, **claims):
        return make_id_token(nonce=nonce, iss=oidc.MS_ISSUER.format(tid=tid),
                             tid=tid, **claims)

    def test_token_from_another_tenant_is_refused(self):
        started = self.begin()
        code, body, headers = self.callback(
            {"code": "c", "state": started["state"]},
            self._ms_token(started["nonce"], tid="99999999-0000-0000-0000-000000000000",
                           email="operator@letu.edu"), started["_binding"])
        self.assertEqual(code, 400)
        self.assertIn(b"different Microsoft tenant", body)
        self.assertIsNone(sid_from(headers))

    def test_pinned_tenant_stands_in_for_email_verified(self):


        started = self.begin()
        code, _, headers = self.callback(
            {"code": "c", "state": started["state"]},
            self._ms_token(started["nonce"], email="teammate@letu.edu"),
            started["_binding"])
        self.assertEqual(code, 302)
        sid = sid_from(headers)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        self.assertTrue(json.loads(body)["known"], "an allowed domain in the "
                        "pinned tenant should count as recognised")

    def test_upn_is_used_when_there_is_no_email_claim(self):
        started = self.begin()
        code, _, headers = self.callback(
            {"code": "c", "state": started["state"]},
            self._ms_token(started["nonce"], preferred_username="Operator@LETU.edu"),
            started["_binding"])
        self.assertEqual(code, 302)
        _, body, _ = http(f"{self.base}/api/whoami",
                          headers=self.cookie(sid_from(headers)))
        self.assertEqual(json.loads(body)["email"], "operator@letu.edu")
        self.assertEqual(json.loads(body)["workspace"], "operator")


class TestOpenSignIn(_AuthServerCase):
    TOKENS = {"tok-legacy-0002": "legacy"}
    CONFIG = GOOGLE_CFG

    def test_listed_user_lands_in_the_named_workspace(self):
        code, _, headers = self.sign_in(email="Operator@LETU.edu", email_verified=True)
        self.assertEqual(code, 302)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(f"Max-Age={oidc.SESSION_TTL_S}", cookie)
        sid = sid_from(headers)

        code, page, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"operator"', page)
        self.assertIn(b'"email":"operator@letu.edu"', page)

        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        who = json.loads(body)
        self.assertEqual((who["workspace"], who["email"], who["sso"], who["known"]),
                         ("operator", "operator@letu.edu", True, True))

    def test_allowed_domain_verified_gets_a_full_email_workspace(self):
        sid = self.sign_in_ok(email="bob@letu.edu", email_verified=True)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        who = json.loads(body)
        self.assertTrue(who["known"])
        self.assertTrue(who["workspace"].startswith("bob_at_letu_edu_"), who)

    def test_stranger_is_admitted_but_marked_unrecognised(self):
        sid = self.sign_in_ok(email="stranger@gmail.com", email_verified=True)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        who = json.loads(body)
        self.assertFalse(who["known"])
        self.assertTrue(who["workspace"].startswith("stranger_at_gmail_com_"))

    def test_unverified_address_on_an_allowed_domain_is_not_recognised(self):
        sid = self.sign_in_ok(email="carol@letu.edu", email_verified=False)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        who = json.loads(body)
        self.assertFalse(who["known"], "unverified must not inherit the domain's trust")

    def test_same_local_part_different_domains_never_share_a_workspace(self):

        letu = self.sign_in_ok(email="alice@letu.edu", email_verified=True)
        gmail = self.sign_in_ok(email="alice@gmail.com", email_verified=True)
        wss = []
        for sid in (letu, gmail):
            _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
            wss.append(json.loads(body)["workspace"])
        self.assertNotEqual(*wss)

        for sid, job in ((letu, "letu-job"), (gmail, "gmail-job")):
            code, body, _ = http(f"{self.base}/api/slice?kind=wall&lh=5&cell=0&name=w",
                                 method="POST", headers=self.cookie(sid))
            sliced = json.loads(body)
            self.assertTrue(sliced["ok"])
            gen = sliced["state"]["meta"]["gen"]
            code, body, _ = http(f"{self.base}/api/save?name={job}&gen={gen}",
                                 method="POST", headers=self.cookie(sid))
            self.assertTrue(json.loads(body)["ok"], body)
        _, body, _ = http(f"{self.base}/api/jobs", headers=self.cookie(letu))
        names = [j["name"] for j in json.loads(body)["jobs"]]
        self.assertEqual(names, ["letu-job"], "one person's saves leaked into another's")

    def test_a_tab_claiming_another_workspace_is_still_refused(self):


        sid = self.sign_in_ok(email="operator@letu.edu", email_verified=True)
        code, body, _ = http(
            f"{self.base}/api/slice?kind=wall&cell=0&name=x&ws=someoneelse",
            method="POST", headers=self.cookie(sid))
        self.assertEqual(code, 409)
        self.assertIn("someoneelse", json.loads(body)["error"])
        code, _, _ = http(f"{self.base}/api/jobs?ws=operator", headers=self.cookie(sid))
        self.assertEqual(code, 200)

    def test_logout_invalidates_the_session(self):
        sid = self.sign_in_ok(email="leaver@letu.edu", email_verified=True)
        code, _, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 200)
        code, _, headers = http(f"{self.base}/auth/logout", headers=self.cookie(sid))
        self.assertEqual(code, 302)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        code, body, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 401)
        self.assertIn(b"Sign in with Google", body)

    def test_page_request_without_a_session_offers_the_button(self):
        code, body, _ = http(f"{self.base}/")
        self.assertEqual(code, 401)
        self.assertIn(b"<html", body.lower())
        self.assertIn(b'href="/auth/login"', body)

    def test_api_request_without_a_session_still_gets_text(self):
        code, body, _ = http(f"{self.base}/api/jobs")
        self.assertEqual(code, 401)
        self.assertNotIn(b"<html", body.lower())

    def test_token_links_still_work_alongside_sign_in(self):
        code, page, _ = http(f"{self.base}/?k=tok-legacy-0002")
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"legacy"', page)
        _, body, _ = http(f"{self.base}/api/whoami?k=tok-legacy-0002")
        who = json.loads(body)
        self.assertFalse(who["sso"])
        self.assertIsNone(who["email"])


class TestStrictSignIn(_AuthServerCase):
    CONFIG = strict()

    def _denied(self, **claims):
        code, body, headers = self.sign_in(**claims)
        self.assertEqual(code, 403)
        self.assertIsNone(sid_from(headers))
        return body

    def test_listed_user_is_still_admitted(self):
        sid = self.sign_in_ok(email="operator@letu.edu", email_verified=True)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        self.assertEqual(json.loads(body)["workspace"], "operator")

    def test_allowed_domain_verified_is_admitted(self):
        sid = self.sign_in_ok(email="dave@letu.edu", email_verified=True)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        self.assertTrue(json.loads(body)["workspace"].startswith("dave_at_letu_edu_"))

    def test_unverified_address_on_an_allowed_domain_is_denied(self):
        body = self._denied(email="dave2@letu.edu", email_verified=False)
        self.assertIn(b"dave2@letu.edu", body)

    def test_unknown_domain_is_denied_and_names_the_address(self):
        body = self._denied(email="stranger@gmail.com", email_verified=True)
        self.assertIn(b"stranger@gmail.com", body)


        self.assertIn(b"has not been granted access", body)

    def test_a_denied_sign_in_leaves_no_workspace_behind(self):
        self._denied(email="ghost@example.org", email_verified=True)
        self.assertNotIn("ghost_at_example_org", " ".join(server._STATES))
        self.assertFalse((server.SAVES_DIR / "ghost_at_example_org").exists())


class TestRevocation(_AuthServerCase):


    TOKENS = {"tok-someone-else-0003": "someoneelse"}
    CONFIG = GOOGLE_CFG

    def test_blocking_kills_a_live_session_without_a_restart(self):
        sid = self.sign_in_ok(email="oops@gmail.com", email_verified=True)
        code, _, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 200)

        self.write_config(dict(GOOGLE_CFG, blocked=["oops@gmail.com"]))
        try:
            code, body, _ = http(f"{self.base}/", headers=self.cookie(sid))
            self.assertEqual(code, 401)
            self.assertIn(b"no longer has access", body)
            self.assertIn(b"oops@gmail.com", body)

            self.assertIsNone(oidc.session_for(sid))

            code, _, _ = http(f"{self.base}/", headers=self.cookie(sid))
            self.assertEqual(code, 401)
            code, _, _ = http(f"{self.base}/api/jobs", headers=self.cookie(sid))
            self.assertEqual(code, 401)
            rows = [r for r in self.log_rows() if r.get("email") == "oops@gmail.com"]
            self.assertTrue(any(r["outcome"] == "REFUSED-blocked" for r in rows))
        finally:
            self.write_config(GOOGLE_CFG)

    def test_a_blocked_address_cannot_sign_in_again(self):
        self.write_config(dict(GOOGLE_CFG, blocked=["banned@example.org"]))
        try:
            code, body, headers = self.sign_in(email="banned@example.org",
                                               email_verified=True)
            self.assertEqual(code, 403)
            self.assertIsNone(sid_from(headers))
            self.assertIn(b"has been revoked", body)
            self.assertIn(b"banned@example.org", body)
        finally:
            self.write_config(GOOGLE_CFG)

    def test_blocking_a_whole_domain_works(self):
        self.write_config(dict(GOOGLE_CFG, blocked=["gmail.com"]))
        try:
            code, _, headers = self.sign_in(email="anyone@gmail.com",
                                            email_verified=True)
            self.assertEqual(code, 403)
            self.assertIsNone(sid_from(headers))
        finally:
            self.write_config(GOOGLE_CFG)

    def test_a_torn_config_keeps_the_last_good_one(self):
        sid = self.sign_in_ok(email="steady@letu.edu", email_verified=True)
        self.cfg_path.write_text("{ this is not json", encoding="utf-8")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code, _, _ = http(f"{self.base}/", headers=self.cookie(sid))
            self.assertEqual(code, 200, "a broken file must not lock everyone out")
        finally:
            self.write_config(GOOGLE_CFG)

    def test_renaming_a_workspace_in_the_config_follows_on_the_next_request(self):
        sid = self.sign_in_ok(email="mover@example.net", email_verified=True)
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        self.assertFalse(json.loads(body)["known"])
        self.write_config(dict(GOOGLE_CFG,
                               users=dict(GOOGLE_CFG["users"],
                                          **{"mover@example.net": {"name": "mover"}})))
        try:
            _, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
            who = json.loads(body)
            self.assertEqual(who["workspace"], "mover")
            self.assertTrue(who["known"])
        finally:
            self.write_config(GOOGLE_CFG)


class TestAccessLogHygiene(_AuthServerCase):
    CONFIG = GOOGLE_CFG

    def test_log_records_the_email_and_never_a_secret(self):
        sid = self.sign_in_ok(email="logged@letu.edu", email_verified=True)
        http(f"{self.base}/", headers=self.cookie(sid))
        http(f"{self.base}/api/jobs", headers=self.cookie(sid))
        raw = server.ACCESS_LOG.read_text(encoding="utf-8")

        for forbidden in (sid, "top-secret-value", "auth-code-xyz",
                          "at-must-never-appear", type(self).staged,
                          type(self).token_calls[-1][1]["code_verifier"]):
            self.assertNotIn(forbidden, raw, "the access log leaked a credential")

        rows = [r for r in self.log_rows() if r.get("email") == "logged@letu.edu"]
        self.assertTrue(rows)
        self.assertTrue(any(r["outcome"] == "ok-sso" for r in rows))
        self.assertTrue(all(r["known"] is True for r in rows if "known" in r))

    def test_unrecognised_arrivals_are_flagged_for_review(self):
        sid = self.sign_in_ok(email="whoisthis@gmail.com", email_verified=True)
        http(f"{self.base}/", headers=self.cookie(sid))
        rows = [r for r in self.log_rows() if r.get("email") == "whoisthis@gmail.com"]
        self.assertTrue(rows)
        self.assertTrue(any(r.get("known") is False for r in rows))

        from waam_pipeline import whos_on
        text = whos_on.report(hours=24, path=server.ACCESS_LOG)
        self.assertIn("whoisthis@gmail.com", text)
        self.assertIn("UNRECOGNISED", text)

    def test_token_arrivals_say_which_mechanism(self):
        server._TOKENS["tok-mech-0009"] = "mech"
        try:
            http(f"{self.base}/api/jobs?k=tok-mech-0009")
        finally:
            server._TOKENS.pop("tok-mech-0009", None)
            server._STATES.pop("mech", None)
        outcomes = {r["outcome"] for r in self.log_rows()}
        self.assertIn("ok-token", outcomes)
        self.assertNotIn("tok-mech-0009",
                         server.ACCESS_LOG.read_text(encoding="utf-8"))


class TestLoadAuthConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-authcfg-"))
        self.path = self.tmp / ".waam-auth.json"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, cfg):
        self.path.write_text(json.dumps(cfg), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            got = oidc.load_auth_config(self.path)
        return got, out.getvalue()

    def test_absent_file_is_none(self):
        self.assertIsNone(oidc.load_auth_config(self.tmp / "nothing.json"))

    def test_defaults(self):
        cfg, _ = self.load(GOOGLE_CFG)
        self.assertTrue(cfg.open_signin)
        self.assertEqual(cfg.tenant, "common")
        self.assertEqual(cfg.redirect_uri, REDIRECT_URI)
        self.assertEqual(cfg.blocked, frozenset())

    def test_emails_and_domains_are_lowercased(self):
        cfg, _ = self.load(dict(GOOGLE_CFG, users={"Operator@LETU.edu": {"name": "g"}},
                                allow_domains=["LETU.edu"], blocked=["BAD@x.com"]))
        self.assertIn("operator@letu.edu", cfg.users)
        self.assertEqual(cfg.allow_domains, ("letu.edu",))
        self.assertIn("bad@x.com", cfg.blocked)

    def test_broken_configs_are_refused_whole(self):
        for bad in (dict(GOOGLE_CFG, provider="okta"),
                    dict(GOOGLE_CFG, client_secret=""),
                    dict(GOOGLE_CFG, public_url="cell.example"),
                    dict(GOOGLE_CFG, provider="microsoft", tenant="../evil"),
                    dict(GOOGLE_CFG, open_signin=False, users={}, allow_domains=[])):
            cfg, note = self.load(bad)
            self.assertIsNone(cfg, bad)
            self.assertIn("sign-in disabled", note)

    def test_the_secret_is_never_printed(self):
        _, note = self.load(dict(GOOGLE_CFG, provider="okta"))
        self.assertNotIn("top-secret-value", note)


class TestSecureCookieOrigin(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-authorigin-"))
        self.path = self.tmp / ".waam-auth.json"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, url):
        self.path.write_text(json.dumps(dict(GOOGLE_CFG, public_url=url)),
                             encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            return oidc.load_auth_config(self.path), out.getvalue()

    def test_http_on_a_lan_address_is_refused(self):
        for url in ("http://192.168.1.50:8017", "http://cell.example",
                    "http://studio.test-tailnet.ts.net:8443"):
            cfg, note = self.load(url)
            self.assertIsNone(cfg, url)
            self.assertIn("Secure sign-in cookies", note)

    def test_loopback_http_still_works(self):


        for url in ("http://localhost:8017", "http://127.0.0.1:8017"):
            cfg, _ = self.load(url)
            self.assertIsNotNone(cfg, url)

    def test_https_anywhere_works(self):
        cfg, _ = self.load("https://cell.example:8443")
        self.assertIsNotNone(cfg)


class TestAuthCheck(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-authcheck-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def check(self, cfg=None, name=".waam-auth.json"):
        path = self.tmp / name
        if cfg is not None:
            path.write_text(json.dumps(cfg), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            ok, lines = authcheck.report(path)
        return ok, "\n".join(lines)

    def test_a_missing_file_explains_the_404(self):
        ok, text = self.check(None)
        self.assertFalse(ok)
        self.assertIn("/auth/login", text)
        self.assertIn("token links", text)

    def test_it_prints_the_redirect_uri_to_paste(self):
        ok, text = self.check(GOOGLE_CFG)
        self.assertTrue(ok, text)
        self.assertIn(REDIRECT_URI, text)

    def test_an_unpinned_microsoft_tenant_is_called_out(self):


        ok, text = self.check(dict(GOOGLE_CFG, provider="microsoft",
                                   tenant="common"))
        self.assertTrue(ok)
        self.assertIn("pins nothing", text)

    def test_blocking_someone_on_the_roster_is_a_failure(self):
        ok, text = self.check(dict(GOOGLE_CFG, blocked=["operator@letu.edu"]))
        self.assertFalse(ok)
        self.assertIn("Blocking wins", text)

    def test_the_template_is_loadable_once_filled_in(self):
        cfg = dict(authcheck.TEMPLATE)
        cfg.update(client_id="cid", client_secret="sec",
                   tenant="8f1e0000-0000-0000-0000-000000000000",
                   public_url="https://cell.example:8443")
        ok, text = self.check(cfg)
        self.assertTrue(ok, text)
        self.assertIn("pinned", text)

    def test_setup_prints_the_redirect_uri_from_the_real_config(self):


        path = self.tmp / ".waam-auth.json"
        path.write_text(json.dumps(GOOGLE_CFG), encoding="utf-8")
        text = "\n".join(authcheck.setup_steps(path))
        self.assertIn(REDIRECT_URI, text)
        self.assertIn("CHARACTER FOR CHARACTER", text)

    def test_setup_works_with_no_config_at_all(self):


        text = "\n".join(authcheck.setup_steps(self.tmp / "nothing.json"))
        self.assertIn("App registrations", text)
        self.assertIn("console.cloud.google.com", text)
        self.assertIn("/auth/callback", text)

    def test_setup_never_tells_anyone_to_paste_the_secret_anywhere(self):
        text = "\n".join(authcheck.setup_steps(None))
        self.assertIn("NOWHERE else", text)
        self.assertIn("not in a commit", text)

    def test_the_secret_never_reaches_the_report(self):
        _, text = self.check(dict(GOOGLE_CFG, provider="microsoft",
                                  tenant="common"))
        self.assertNotIn("top-secret-value", text)

    def test_it_proves_the_waam_approve_command_resolves(self):


        cfg = dict(GOOGLE_CFG, approval_required=True, approved=["a@letu.edu"],
                   approval_notify="owner@example.com",
                   mail={"host": "smtp.example.com", "port": 587,
                         "username": "workbench@example.com",
                         "password": "x" * 16,
                         "from": "WAAM Workbench <workbench@example.com>"})
        ok, text = self.check(cfg)
        self.assertTrue(ok, text)
        self.assertIn("waam-approve", text)
        self.assertIn("waam_pipeline.approve:main", text)
        self.assertIn("resolves", text)


class TestOneDoorOneButton(_AuthServerCase):


    TOKENS = {"tok-door-0001": "shopfloor"}
    CONFIG = GOOGLE_CFG

    def test_the_door_starts_the_sign_in(self):
        for path in ("/signin", "/sign-in", "/login", "/SignIn"):
            code, _, headers = http(f"{self.base}{path}")
            self.assertEqual(code, 302, path)
            self.assertEqual(headers["Location"], "/auth/login", path)

    def test_the_door_needs_no_credential(self):


        code, _, _ = http(f"{self.base}/")
        self.assertEqual(code, 401)
        code, _, _ = http(f"{self.base}/signin")
        self.assertEqual(code, 302)

    def test_the_page_is_told_what_the_button_says(self):


        code, body, _ = http(f"{self.base}/api/whoami?k=tok-door-0001")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertEqual(j["sign_in_url"], "/signin")
        self.assertEqual(j["sign_in_label"], "Google")
        self.assertFalse(j["sso"])

    def test_a_signed_in_visitor_is_not_offered_the_button(self):
        sid = self.sign_in_ok(email="operator@letu.edu", email_verified=True)
        code, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        j = json.loads(body)
        self.assertTrue(j["sso"])
        self.assertEqual(j["email"], "operator@letu.edu")

    def test_an_unsigned_visitor_is_offered_the_choice_first(self):


        code, body, _ = http(f"{self.base}/")
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("Sign in with Google", text)
        self.assertIn("Continue as a guest", text)
        self.assertIn("/auth/guest", text)
        for jargon in ("?k=", "OIDC", "OpenID", "capability"):
            self.assertNotIn(jargon, text)


class TestGuestDenyByDefault(_AuthServerCase):


    CONFIG = GOOGLE_CFG

    def guest(self):

        code, _, headers = http(f"{self.base}/auth/guest", method="POST")
        self.assertEqual(code, 302, "the guest door should admit anyone")
        jar = SimpleCookie(headers.get("Set-Cookie", ""))
        self.assertIn(server.GUEST_COOKIE, jar, "no guest cookie was set")
        gid = jar[server.GUEST_COOKIE].value
        return ({"Cookie": f"{server.GUEST_COOKIE}={gid}"},
                server._guest_workspace(gid))

    def test_redirects_and_http_errors_also_refuse_caching(self):
        for path, method, expected in (
                ("/auth/guest", "POST", 302),
                ("/auth/login", "GET", 302),
                ("/api/whoami", "GET", 200),
                ("/", "OPTIONS", 501)):
            with self.subTest(path=path, method=method):
                code, _, headers = http(self.base + path, method=method)
                self.assertEqual(code, expected)
                self.assertIn("no-store", headers.get("Cache-Control", ""))
                self.assertEqual(headers.get("Pragma"), "no-cache")

    def test_the_allowlist_is_exactly_these_four(self):

        self.assertEqual(
            server.GUEST_ALLOWED,
            frozenset({"/", "/api/slice", "/api/place", "/api/whoami"}))

    def test_a_guest_may_reach_the_studio_and_whoami(self):
        g, _ = self.guest()
        code, _, _ = http(f"{self.base}/", headers=g)
        self.assertEqual(code, 200)
        code, body, _ = http(f"{self.base}/api/whoami", headers=g)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["guest"])

    def test_a_guest_cannot_download_a_robot_job(self):

        g, _ = self.guest()
        code, body, _ = http(f"{self.base}/api/jbi", headers=g)
        self.assertEqual(code, 403)
        self.assertFalse(json.loads(body)["ok"])

    def test_every_non_guest_route_is_refused(self):
        g, _ = self.guest()
        for path in ("/api/ros2", "/api/gcode", "/api/jobs", "/api/fixtures"):
            code, _, _ = http(f"{self.base}{path}", headers=g)
            self.assertEqual(code, 403, f"a guest reached {path}")
        for path in ("/api/save", "/api/load", "/api/var-dat"):
            code, _, _ = http(f"{self.base}{path}", method="POST", headers=g)
            self.assertEqual(code, 403, f"a guest reached {path}")

    def test_deny_by_default_catches_a_route_not_yet_written(self):


        g, _ = self.guest()
        code, _, _ = http(f"{self.base}/api/not-a-real-route-yet", headers=g)
        self.assertEqual(code, 403)

    def test_a_guest_can_still_slice(self):

        g, ws = self.guest()
        code, _, _ = http(f"{self.base}/api/slice?ws={ws}",
                          method="POST", headers=g)
        self.assertNotEqual(code, 403, "slicing is what a guest came to do")


class TestSwappingTheLogin(_AuthServerCase):


    TOKENS = {"tok-swap-0001": "swap"}
    CONFIG = GOOGLE_CFG

    def tearDown(self):
        self.write_config(GOOGLE_CFG)

    def generic(self, **extra):
        cfg = dict(GOOGLE_CFG, provider="oidc",
                   issuer="https://login.letu.example",
                   authorize_endpoint="https://login.letu.example/authorize",
                   token_endpoint="https://login.letu.example/oauth/token")
        cfg.update(extra)
        return cfg

    def test_a_generic_openid_provider_works(self):


        self.write_config(self.generic(sign_in_label="your LeTourneau account"))
        code, _, headers = http(f"{self.base}/auth/login")
        self.assertEqual(code, 302)
        self.assertTrue(headers["Location"].startswith(
            "https://login.letu.example/authorize?"))

        started = self.begin()
        token = make_id_token(nonce=started["nonce"],
                              iss="https://login.letu.example",
                              email="operator@letu.edu", email_verified=True)
        code, _, hdrs = self.callback(
            {"code": "c", "state": started["state"]}, token, started["_binding"])
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(hdrs))

        self.assertEqual(self.token_calls[-1][0],
                         "https://login.letu.example/oauth/token")

    def test_a_token_from_the_wrong_issuer_is_refused(self):
        self.write_config(self.generic())
        started = self.begin()
        token = make_id_token(nonce=started["nonce"],
                              iss="https://login.somewhere-else.example",
                              email="operator@letu.edu", email_verified=True)
        code, body, hdrs = self.callback(
            {"code": "c", "state": started["state"]}, token, started["_binding"])
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(hdrs))
        self.assertIn(b"issuer", body)

    def test_the_button_wording_follows_the_swap_with_no_restart(self):


        who = f"{self.base}/api/whoami?k=tok-swap-0001"
        code, body, _ = http(who)
        self.assertEqual(json.loads(body)["sign_in_label"], "Google")

        self.write_config(dict(GOOGLE_CFG, provider="microsoft", tenant=TENANT,
                               sign_in_label="your LETU account"))
        code, body, _ = http(who)
        self.assertEqual(json.loads(body)["sign_in_label"], "your LETU account")
        code, page, _ = http(f"{self.base}/")
        self.assertIn(b"Sign in with your LETU account", page)

        code, _, headers = http(f"{self.base}/signin")
        self.assertEqual(headers["Location"], "/auth/login")

    def test_a_generic_provider_without_its_urls_is_refused_whole(self):


        for missing in ("issuer", "authorize_endpoint", "token_endpoint"):
            cfg = self.generic()
            cfg.pop(missing)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.write_config(cfg)
                got = oidc.load_auth_config(self.cfg_path)
            self.assertIsNone(got, missing)
            self.assertIn(missing, out.getvalue())

    def test_a_provider_url_on_plain_http_is_refused(self):


        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.write_config(self.generic(
                token_endpoint="http://login.letu.example/token"))
            got = oidc.load_auth_config(self.cfg_path)
        self.assertIsNone(got)
        self.assertIn("token_endpoint must be an https URL", out.getvalue())


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        oidc.forget_everything()
        self.addCleanup(oidc.forget_everything)

    def test_expired_sessions_are_pruned(self):
        sid = oidc.new_session("a@b.com", True)
        oidc._SESSIONS[sid]["expires"] = time.time() - 1
        self.assertIsNone(oidc.session_for(sid))
        self.assertNotIn(sid, oidc._SESSIONS)

    def test_sessions_are_capped(self):
        for i in range(oidc.MAX_SESSIONS + 20):
            oidc.new_session(f"u{i}@b.com", True)
        self.assertLessEqual(len(oidc._SESSIONS), oidc.MAX_SESSIONS)

    def test_pending_logins_are_capped(self):
        cfg = oidc.AuthConfig(provider="google", client_id="c", client_secret="s",
                              public_url="https://x.example")
        for _ in range(oidc.MAX_PENDING + 20):
            oidc.begin_login(cfg)
        self.assertLessEqual(len(oidc._PENDING), oidc.MAX_PENDING)

    def test_a_non_ascii_cookie_is_a_miss_not_a_crash(self):
        oidc.new_session("a@b.com", True)
        self.assertIsNone(oidc.session_for("café"))
        self.assertFalse(oidc.drop_session("café"))


APPROVAL_CFG = {
    "provider": "local",
    "public_url": PUBLIC_URL,
    "users": {"operator@letu.edu": {"name": "operator"}},
    "allow_domains": ["letu.edu"],
    "open_signin": False,
    "approval_required": True,
    "approved": ["approved@letu.edu"],
    "approval_notify": "owner@example.com",
    "mail": {"host": "smtp.example.com", "port": 587,
             "username": "workbench@example.com", "password": "x" * 16,
             "from": "WAAM Workbench <workbench@example.com>"},
}


class TestApprovalFlow(_AuthServerCase):


    CONFIG = APPROVAL_CFG
    GOOD = "correct horse battery"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._saved_store = server.ACCOUNTS
        server.ACCOUNTS = accounts.Store(cls.tmp / ".waam-accounts.json")

    @classmethod
    def tearDownClass(cls):
        server.ACCOUNTS = cls._saved_store
        super().tearDownClass()

    def setUp(self):
        accounts.forget_rate_limits()
        accounts.forget_codes()
        oidc.forget_everything()


        server._NOTIFIED_PENDING.clear()
        store = server.ACCOUNTS.path
        if store.exists():
            store.unlink()

        self.sent: list[dict] = []
        self._real_send = mailer.send

        def capture(cfg, to, subject, body):
            self.sent.append({"to": to, "subject": subject, "body": body})

        mailer.send = capture
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(accounts.forget_codes)
        self.addCleanup(accounts.forget_rate_limits)
        self.addCleanup(server._NOTIFIED_PENDING.clear)


    def post(self, path, **fields):
        return http(self.base + path, method="POST",
                    data=urllib.parse.urlencode(fields).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})

    def register(self, email):
        return self.post("/auth/register", email=email, password=self.GOOD)

    def code_to(self, email):

        for s in reversed(self.sent):
            if s["to"] == email:
                digits = [w for w in s["body"].split()
                          if w.isdigit() and len(w) == accounts.CODE_DIGITS]
                if digits:
                    return digits[0]
        self.fail(f"no verification code was emailed to {email}")

    def verify(self, email, code=None):
        return self.post("/auth/verify", email=email,
                         code=code if code is not None else self.code_to(email))

    def notices(self):
        return [s for s in self.sent if s["to"] == APPROVAL_CFG["approval_notify"]]


    def test_a_verified_allow_domains_registrant_is_held_pending(self):
        self.register("newcomer@letu.edu")
        status, body, headers = self.verify("newcomer@letu.edu")
        self.assertEqual(status, 200, body[:300])
        self.assertIn(b"request", body.lower())
        self.assertIsNone(sid_from(headers),
                          "a pending request must NOT hand out a session")

        self.assertTrue(server.ACCOUNTS.get("newcomer@letu.edu").verified)

    def test_a_roster_user_is_admitted(self):
        self.register("operator@letu.edu")
        status, body, headers = self.verify("operator@letu.edu")
        self.assertEqual(status, 302, body[:300])
        self.assertIsNotNone(sid_from(headers))

    def test_an_already_approved_address_is_admitted(self):
        self.register("approved@letu.edu")
        status, body, headers = self.verify("approved@letu.edu")
        self.assertEqual(status, 302, body[:300])
        self.assertIsNotNone(sid_from(headers))

    def test_the_owner_is_notified_exactly_once(self):
        self.register("newcomer@letu.edu")
        self.assertEqual(self.verify("newcomer@letu.edu")[0], 200)
        self.assertEqual(len(self.notices()), 1, "first pending must notify")


        again = accounts.new_code("newcomer@letu.edu", accounts.PURPOSE_VERIFY)
        self.assertEqual(self.verify("newcomer@letu.edu", code=again)[0], 200)
        self.assertEqual(len(self.notices()), 1,
                         "the owner must be emailed once per address, not per try")

    def test_admitting_someone_does_not_notify(self):
        self.register("approved@letu.edu")
        self.verify("approved@letu.edu")
        self.assertEqual(self.notices(), [], "only a HELD request emails the owner")

    def test_notice_links_to_the_selected_request_on_the_current_public_url(self):
        self.addCleanup(self.write_config, APPROVAL_CFG)
        urls = ("https://first.example/", "https://second.example:9443")
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                self.write_config(dict(APPROVAL_CFG, public_url=url))
                email = f"welder{index}+cell&review=1/2?%#@letu.edu"
                self.register(email)
                self.assertEqual(self.verify(email)[0], 200)
                notice = self.notices()[-1]
                self.assertEqual(notice["to"], APPROVAL_CFG["approval_notify"])
                links = re.findall(r"https?://\S+", notice["body"])
                self.assertEqual(len(links), 1)
                link = urllib.parse.urlsplit(links[0])
                self.assertEqual(links[0].split("/admin?", 1)[0],
                                 url.rstrip("/"))
                self.assertEqual(link.path, "/admin")
                self.assertEqual(urllib.parse.parse_qs(link.query), {"pending": [email]})
                self.assertEqual(link.fragment, "")
                fallback = [line for line in notice["body"].splitlines()
                            if "waam-approve" in line]
                self.assertEqual(len(fallback), 1)
                command = fallback[0][fallback[0].index("waam-approve"):]
                self.assertEqual(shlex.split(command), ["waam-approve", email])
                self.assertLess(notice["body"].index(links[0]),
                                notice["body"].index("waam-approve"))


APPROVAL_SSO_CFG = dict(
    GOOGLE_CFG,
    open_signin=False,
    approval_required=True,
    approved=["approved@letu.edu"],
    approval_notify="owner@example.com",
    mail={"host": "smtp.example.com", "port": 587,
          "username": "workbench@example.com", "password": "x" * 16,
          "from": "WAAM Workbench <workbench@example.com>"},
)


class TestApprovalFlowSSO(_AuthServerCase):


    CONFIG = APPROVAL_SSO_CFG

    def setUp(self):
        oidc.forget_everything()


        server._NOTIFIED_PENDING.clear()
        self.sent: list[dict] = []
        self._real_send = mailer.send

        def capture(cfg, to, subject, body):
            self.sent.append({"to": to, "subject": subject, "body": body})

        mailer.send = capture
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(server._NOTIFIED_PENDING.clear)

    def notices(self):
        return [s for s in self.sent
                if s["to"] == APPROVAL_SSO_CFG["approval_notify"]]

    def test_a_pending_callback_notifies_the_owner_and_logs_pending(self):
        code, body, headers = self.sign_in(email="newcomer@letu.edu",
                                           email_verified=True)

        self.assertEqual(code, 200, body[:300])
        self.assertIn(b"request", body.lower())
        self.assertIsNone(sid_from(headers),
                          "a pending request must NOT hand out a session")

        self.assertEqual(len(self.notices()), 1,
                         "a pending SSO sign-in must notify the owner once")


        rows = [r for r in self.log_rows()
                if r.get("email") == "newcomer@letu.edu"]
        self.assertTrue(rows, "the arrival was not logged at all")
        self.assertTrue(any(r["outcome"] == "pending" for r in rows),
                        "the callback must log outcome 'pending'")
        self.assertFalse(any(str(r["outcome"]).startswith("REFUSED")
                             for r in rows),
                         "pending is a wait, so nothing here may be REFUSED")

        from waam_pipeline import approve
        self.assertIn("newcomer@letu.edu",
                      approve.pending_requests(server.ACCESS_LOG))

    def test_the_owner_is_notified_only_once(self):


        self.sign_in(email="newcomer@letu.edu", email_verified=True)
        self.sign_in(email="newcomer@letu.edu", email_verified=True)
        self.assertEqual(len(self.notices()), 1)

    def test_a_rostered_sso_user_still_signs_in_and_is_not_notified(self):

        code, _, headers = self.sign_in(email="operator@letu.edu",
                                        email_verified=True)
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))
        self.assertEqual(self.notices(), [],
                         "admitting a rostered user must not notify the owner")

    def test_an_already_approved_sso_user_is_admitted(self):
        code, _, headers = self.sign_in(email="approved@letu.edu",
                                        email_verified=True)
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))


class TestPendingIsOneDefinitionEverywhere(_AuthServerCase):


    CONFIG = APPROVAL_CFG

    def setUp(self):
        accounts.forget_rate_limits()
        accounts.forget_codes()
        oidc.forget_everything()
        server._NOTIFIED_PENDING.clear()
        store = server.ACCOUNTS.path
        if store.exists():
            store.unlink()
        self.sent = []
        self.fail_sends = False
        self._real_send = mailer.send

        def capture(cfg, to, subject, body):
            if self.fail_sends and to == APPROVAL_CFG["approval_notify"]:
                raise mailer.MailError("smtp said no")
            self.sent.append({"to": to, "subject": subject, "body": body})

        mailer.send = capture
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(accounts.forget_codes)
        self.addCleanup(accounts.forget_rate_limits)
        self.addCleanup(server._NOTIFIED_PENDING.clear)

    GOOD = "correct horse battery staple"

    def post(self, path, **fields):
        return http(self.base + path, method="POST",
                    data=urllib.parse.urlencode(fields).encode("utf-8"),
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"})

    def code_to(self, email):
        for s in reversed(self.sent):
            if s["to"] == email:
                digits = [w for w in s["body"].split()
                          if w.isdigit() and len(w) == accounts.CODE_DIGITS]
                if digits:
                    return digits[0]
        self.fail(f"no code was emailed to {email}")

    def notices(self):
        return [s for s in self.sent
                if s["to"] == APPROVAL_CFG["approval_notify"]]

    def rows_for(self, email):
        return [r for r in self.log_rows() if r.get("email") == email]

    def become_pending(self, email):

        self.post("/auth/register", email=email, password=self.GOOD)
        code, body, _ = self.post("/auth/verify", email=email,
                                  code=self.code_to(email))
        self.assertEqual(code, 200, "verifying should leave them holding")
        return body


    def test_signing_in_again_while_pending_is_held_not_refused(self):
        email = "again@letu.edu"
        self.become_pending(email)
        self.assertEqual(len(self.notices()), 1)

        code, body, headers = self.post("/auth/signin", email=email,
                                        password=self.GOOD)


        self.assertEqual(code, 200, body[:300])
        self.assertIsNone(sid_from(headers),
                          "a held account must NOT get a session by re-signing")
        outcomes = [r["outcome"] for r in self.rows_for(email)]
        self.assertIn("pending", outcomes)
        self.assertFalse([o for o in outcomes
                          if str(o).startswith("REFUSED-pending")],
                         "a wait must never be logged as REFUSED-pending")

        self.assertEqual(len(self.notices()), 1)


    def test_resetting_a_password_while_pending_is_held_not_refused(self):
        email = "reset@letu.edu"
        self.become_pending(email)
        self.post("/auth/forgot", email=email)
        code, body, headers = self.post("/auth/reset", email=email,
                                        code=self.code_to(email),
                                        password="a whole new passphrase here")
        self.assertEqual(code, 200, body[:300])
        self.assertIsNone(sid_from(headers),
                          "a held account must NOT get a session by resetting")
        outcomes = [r["outcome"] for r in self.rows_for(email)]
        self.assertFalse([o for o in outcomes
                          if str(o).startswith("REFUSED-pending")])


    def test_a_failed_notice_leaves_the_address_un_debounced(self):
        email = "smtpdown@letu.edu"
        self.fail_sends = True
        self.become_pending(email)
        self.assertEqual(self.notices(), [],
                         "the send was made to fail, so nothing was delivered")


        self.assertNotIn(email, server._NOTIFIED_PENDING,
                         "a FAILED send must not count as notified")

        self.fail_sends = False
        self.post("/auth/signin", email=email, password=self.GOOD)
        self.assertEqual(len(self.notices()), 1,
                         "the next attempt must actually reach the owner")


    def test_the_address_is_reserved_before_the_send_not_after(self):


        email = "reserved@letu.edu"
        seen = {}

        real_capture = mailer.send

        def watch(cfg, to, subject, body):
            if to == APPROVAL_CFG["approval_notify"]:
                seen["reserved_during_send"] = (
                    email in server._NOTIFIED_PENDING)
            return real_capture(cfg, to, subject, body)

        mailer.send = watch
        self.addCleanup(lambda: setattr(mailer, "send", real_capture))

        self.become_pending(email)
        self.assertEqual(len(self.notices()), 1)
        self.assertTrue(seen.get("reserved_during_send"),
                        "the address must be reserved BEFORE mailer.send "
                        "runs, or a concurrent request double-mails the owner")


    def test_a_failed_notice_is_logged_against_the_route_it_came_from(self):
        email = "mailfail@letu.edu"
        self.fail_sends = True
        self.become_pending(email)


        self.post("/auth/signin", email=email, password=self.GOOD)
        mail_rows = [r for r in self.rows_for(email)
                     if r["outcome"] == "REFUSED-mail"]
        self.assertTrue(mail_rows, "a failed owner-notice must be logged")
        paths = {r["path"] for r in mail_rows}
        self.assertIn("/auth/verify", paths, "the first failure was on verify")
        self.assertIn("/auth/signin", paths,
                      "a notice that died on /auth/signin must be logged "
                      "there, not filed under /auth/verify")


ADMIN_CFG = dict(GOOGLE_CFG, admins=["boss@letu.edu"])


class TestAdminConsole(_AuthServerCase):


    TOKENS = {"tok-admin-0007": "tokspace"}
    CONFIG = ADMIN_CFG

    ADMIN = "boss@letu.edu"
    NONADMIN = "operator@letu.edu"
    ADMIN_GET = ("/admin", "/admin?pending=waiting%2Bcell%40letu.edu",
                 "/api/admin/state")
    ADMIN_POST = ("/api/admin/approve", "/api/admin/revoke")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()


        cls._saved_store = server.ACCOUNTS
        server.ACCOUNTS = accounts.Store(cls.tmp / ".waam-accounts.json")

    @classmethod
    def tearDownClass(cls):
        server.ACCOUNTS = cls._saved_store
        super().tearDownClass()

    def setUp(self):


        self.write_config(ADMIN_CFG)
        oidc.forget_everything()
        server.forget_guests()


    def admin_cookie(self):
        return self.cookie(self.sign_in_ok(email=self.ADMIN, email_verified=True))

    def nonadmin_cookie(self):
        return self.cookie(self.sign_in_ok(email=self.NONADMIN, email_verified=True))

    def guest_cookie(self):
        code, _, headers = http(f"{self.base}/auth/guest", method="POST")
        self.assertEqual(code, 302, "the guest door should admit anyone")
        jar = SimpleCookie(headers.get("Set-Cookie", ""))
        return {"Cookie": f"{server.GUEST_COOKIE}={jar[server.GUEST_COOKIE].value}"}

    def post_admin(self, path, email, headers):
        h = dict(headers)
        h["Content-Type"] = "application/x-www-form-urlencoded"
        return http(self.base + path, method="POST",
                    data=urllib.parse.urlencode({"email": email}).encode("utf-8"),
                    headers=h)

    def cfg_now(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))


    def test_an_admin_reaches_the_page_and_the_state(self):
        h = self.admin_cookie()
        code, body, _ = http(f"{self.base}/admin", headers=h)
        self.assertEqual(code, 200)
        self.assertIn(b"WAAM STUDIO", body)
        self.assertNotIn(b"Not authorized", body)
        code, body, _ = http(f"{self.base}/api/admin/state", headers=h)
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertTrue(j["ok"])
        self.assertIn("active", j)
        self.assertIn("pending", j)


    def test_a_signed_in_non_admin_is_denied_everywhere(self):
        h = self.nonadmin_cookie()
        for path in self.ADMIN_GET:
            code, _, _ = http(f"{self.base}{path}", headers=h)
            self.assertEqual(code, 403, path)
        for path in self.ADMIN_POST:
            code, _, _ = self.post_admin(path, "someone@letu.edu", h)
            self.assertEqual(code, 403, path)

    def test_the_non_admin_denial_is_the_gate_not_an_accident(self):


        h = self.nonadmin_cookie()
        real = server._is_admin
        server._is_admin = lambda *a, **k: True
        try:
            code, _, _ = http(f"{self.base}/admin", headers=h)
            self.assertEqual(code, 200, "forcing the gate open must admit /admin")
            code, body, _ = http(f"{self.base}/api/admin/state", headers=h)
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["ok"])
        finally:
            server._is_admin = real


    def test_a_token_only_caller_is_denied(self):
        k = "?k=tok-admin-0007"
        for path in self.ADMIN_GET:
            query = k.replace("?", "&", 1) if "?" in path else k
            code, _, _ = http(f"{self.base}{path}{query}")
            self.assertEqual(code, 403, path)
        code, _, _ = self.post_admin("/api/admin/approve" + k, "x@letu.edu", {})
        self.assertEqual(code, 403)

    def test_the_token_denial_is_the_gate_not_an_accident(self):


        real = server._is_admin
        server._is_admin = lambda *a, **k: True
        try:
            code, body, _ = http(f"{self.base}/api/admin/state?k=tok-admin-0007")
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["ok"])
        finally:
            server._is_admin = real


    def test_a_guest_is_denied_everywhere(self):
        g = self.guest_cookie()
        for path in self.ADMIN_GET:
            code, _, _ = http(f"{self.base}{path}", headers=g)
            self.assertEqual(code, 403, path)
        for path in self.ADMIN_POST:
            code, _, _ = self.post_admin(path, "x@letu.edu", g)
            self.assertEqual(code, 403, path)

    def test_the_admin_gate_refuses_a_guest_even_past_the_guest_gate(self):


        g = self.guest_cookie()
        saved = server.GUEST_ALLOWED
        server.GUEST_ALLOWED = frozenset(
            saved | set(self.ADMIN_GET) | set(self.ADMIN_POST))
        try:
            for path in self.ADMIN_GET:
                code, _, _ = http(f"{self.base}{path}", headers=g)
                self.assertEqual(code, 403,
                                 f"the admin gate must refuse a guest at {path}")
            for path in self.ADMIN_POST:
                code, _, _ = self.post_admin(path, "x@letu.edu", g)
                self.assertEqual(code, 403, path)
        finally:
            server.GUEST_ALLOWED = saved


    def test_approve_endpoint_writes_approved_lowercased(self):
        h = self.admin_cookie()
        code, body, _ = self.post_admin("/api/admin/approve", "Newcomer@LETU.edu", h)
        self.assertEqual(code, 200, body)
        j = json.loads(body)
        self.assertTrue(j["ok"] and j["changed"], body)
        self.assertIn("newcomer@letu.edu", self.cfg_now()["approved"])

    def test_revoke_endpoint_writes_blocked_and_ends_the_session(self):
        victim = self.cookie(
            self.sign_in_ok(email="victim@letu.edu", email_verified=True))
        code, _, _ = http(f"{self.base}/", headers=victim)
        self.assertEqual(code, 200, "the victim is signed in before the revoke")

        code, body, _ = self.post_admin("/api/admin/revoke", "victim@letu.edu",
                                        self.admin_cookie())
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["changed"], body)
        self.assertIn("victim@letu.edu", self.cfg_now()["blocked"])


        code, _, _ = http(f"{self.base}/", headers=victim)
        self.assertEqual(code, 401, "revoke must end the live session")


    def test_state_returns_active_and_pending(self):


        with server.ACCESS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"t": "2026-09-07T00:00:00+00:00", "outcome": "pending",
                 "email": "waiting@letu.edu", "path": "/auth/callback",
                 "workspace": None}) + "\n")
        code, body, _ = http(f"{self.base}/api/admin/state",
                             headers=self.admin_cookie())
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertIn("waiting@letu.edu", [r["email"] for r in j["pending"]])
        active = {r["email"]: r for r in j["active"]}
        self.assertEqual(active[self.NONADMIN]["state"], "roster")
        self.assertTrue(active[self.ADMIN]["admin"],
                        "an admin must be visible and flagged in the console")


    def test_whoami_exposes_admin(self):
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.admin_cookie())
        self.assertTrue(json.loads(body)["admin"], "the admin's whoami must say so")
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.nonadmin_cookie())
        self.assertFalse(json.loads(body)["admin"], "a non-admin must not")
        _, body, _ = http(f"{self.base}/api/whoami?k=tok-admin-0007")
        self.assertFalse(json.loads(body)["admin"], "a token caller is never admin")
        _, body, _ = http(f"{self.base}/api/whoami", headers=self.guest_cookie())
        self.assertFalse(json.loads(body)["admin"], "a guest is never admin")


class TestAdminCannotRevokeItselfIntoALockout(TestAdminConsole):


    def test_the_endpoint_refuses_to_revoke_an_admin(self):


        h = self.admin_cookie()
        code, body, _ = self.post_admin("/api/admin/revoke", self.ADMIN, h)
        self.assertEqual(code, 200, body[:200])
        j = json.loads(body)
        self.assertFalse(j["changed"], "an admin must not be revocable")
        self.assertNotIn(self.ADMIN,
                         [str(b).lower()
                          for b in self.cfg_now().get("blocked", [])],
                         "the admin was written into `blocked`")

        code, _, _ = http(f"{self.base}/api/admin/state", headers=h)
        self.assertEqual(code, 200, "the admin locked themselves out")

    def test_another_admin_cannot_be_revoked_either(self):


        cfg = dict(ADMIN_CFG)
        cfg["admins"] = [self.ADMIN, self.NONADMIN]
        self.write_config(cfg)
        h = self.admin_cookie()
        code, body, _ = self.post_admin("/api/admin/revoke", self.NONADMIN, h)
        self.assertEqual(code, 200, body[:200])
        self.assertFalse(json.loads(body)["changed"])

    def test_an_admins_object_still_protects_them(self):


        cfg = dict(ADMIN_CFG)
        cfg["admins"] = {self.ADMIN: {}}
        self.write_config(cfg)
        h = self.admin_cookie()

        code, _, _ = http(f"{self.base}/api/admin/state", headers=h)
        self.assertEqual(code, 200, "the object form should still grant admin")
        code, body, _ = self.post_admin("/api/admin/revoke", self.ADMIN, h)
        self.assertEqual(code, 200, body[:200])
        self.assertFalse(json.loads(body)["changed"],
                         "an admin declared as an object must still be safe")
        self.assertNotIn(self.ADMIN,
                         [str(b).lower()
                          for b in self.cfg_now().get("blocked", [])])
        code, _, _ = http(f"{self.base}/api/admin/state", headers=h)
        self.assertEqual(code, 200, "the admin locked themselves out")

    def test_a_non_admin_is_still_revocable(self):

        cfg = dict(ADMIN_CFG)
        cfg["admins"] = [self.ADMIN]
        self.write_config(cfg)
        h = self.admin_cookie()
        code, body, _ = self.post_admin("/api/admin/revoke",
                                        "ordinary@letu.edu", h)
        self.assertEqual(code, 200, body[:200])
        self.assertTrue(json.loads(body)["changed"],
                        "revoking a normal account must still work")

    def test_the_page_offers_no_revoke_button_on_an_admin_row(self):
        code, body, _ = http(f"{self.base}/admin", headers=self.admin_cookie())
        self.assertEqual(code, 200)
        self.assertIn(b"row.admin", body,
                      "the row renderer must branch on the admin flag")
        self.assertIn(b"administrator", body)


class TestAdminSurfaceIsDenyByDefault(TestAdminConsole):


    UNWRITTEN = "/api/admin/not-a-real-route-yet"

    def test_an_unwritten_admin_route_is_closed_to_a_non_admin(self):
        code, _, _ = http(self.base + self.UNWRITTEN,
                          headers=self.nonadmin_cookie())
        self.assertEqual(code, 403,
                         "a route added later must be closed by default")

    def test_the_same_route_is_closed_on_POST(self):
        code, _, _ = self.post_admin(self.UNWRITTEN, "x@letu.edu",
                                     self.nonadmin_cookie())
        self.assertEqual(code, 403)

    def test_an_admin_reaching_it_gets_404_not_403(self):


        code, _, _ = http(self.base + self.UNWRITTEN,
                          headers=self.admin_cookie())
        self.assertEqual(code, 404,
                         "an admin should reach dispatch and get a plain 404")


class TestAdminConfigLoading(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-admincfg-"))
        self.path = self.tmp / ".waam-auth.json"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, cfg):
        self.path.write_text(json.dumps(cfg), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            return oidc.load_auth_config(self.path), out.getvalue()

    def test_admins_are_lowercased(self):
        cfg, _ = self.load(dict(GOOGLE_CFG, admins=["Boss@LETU.edu"]))
        self.assertEqual(cfg.admins, frozenset({"boss@letu.edu"}))

    def test_absent_admins_is_empty(self):
        cfg, _ = self.load(GOOGLE_CFG)
        self.assertEqual(cfg.admins, frozenset())

    def test_a_non_email_admin_entry_refuses_the_whole_config(self):
        cfg, note = self.load(dict(GOOGLE_CFG, admins=["letu.edu"]))
        self.assertIsNone(cfg, "a bare domain must not be accepted as an admin")
        self.assertIn("sign-in disabled", note)


if __name__ == "__main__":
    unittest.main()
