import contextlib
import html
import io
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from email.utils import parseaddr
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_auth import _AuthServerCase, http, sid_from

from waam_pipeline import accounts, mailer, oidc, server

LOCAL_CFG = {
    "provider": "local",
    "public_url": "https://cell.example.ts.net:8443",
    "users": {"test_operator@letu.edu": {"name": "Test Operator"}},
    "allow_domains": ["letu.edu"],
}
GOOD = "correct horse battery"


def form(**fields) -> bytes:
    return urlencode(fields).encode("utf-8")


class _LocalCase(_AuthServerCase):
    CONFIG = LOCAL_CFG

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
        store = server.ACCOUNTS.path
        if store.exists():
            store.unlink()
        self.addCleanup(accounts.forget_rate_limits)

    def post(self, path, **fields):
        return http(self.base + path, method="POST", data=form(**fields),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})

    def register(self, email=GOOD and "test_operator@letu.edu", password=GOOD):
        return self.post("/auth/register", email=email, password=password)

    def signin(self, email="test_operator@letu.edu", password=GOOD):
        return self.post("/auth/signin", email=email, password=password)


class TheHappyPath(_LocalCase):
    def test_register_then_use_the_workbench(self):
        code, body, headers = self.register()
        self.assertEqual(code, 302, body[:300])
        sid = sid_from(headers)
        self.assertIsNotNone(sid)
        code, page, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"Test_Operator"', page)

    def test_sign_out_then_back_in_on_a_different_device(self):


        _, _, headers = self.register()
        first = sid_from(headers)
        http(f"{self.base}/auth/logout", headers=self.cookie(first))
        self.assertIsNone(oidc.session_for(first))

        code, _, headers = self.signin()
        second = sid_from(headers)
        self.assertEqual(code, 302)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        code, page, _ = http(f"{self.base}/", headers=self.cookie(second))
        self.assertIn(b'"workspace":"Test_Operator"', page)

    def test_the_session_cookie_is_locked_down(self):
        _, _, headers = self.register()
        raw = headers.get("Set-Cookie", "")
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(flag, raw)

    def test_the_doors_are_all_served_before_the_gate(self):


        code, body, _ = http(f"{self.base}/auth/login")
        self.assertEqual(code, 200)
        for door in (b"/auth/login?do=in", b"/auth/login?new=1", b"/auth/guest"):
            self.assertIn(door, body)
        code, body, _ = http(f"{self.base}/auth/login?do=in")
        self.assertEqual(code, 200)
        self.assertIn(b"/auth/signin", body)
        code, body, _ = http(f"{self.base}/auth/login?new=1")
        self.assertEqual(code, 200)
        self.assertIn(b"/auth/register", body)
        self.assertIn(b"contact the administrator", body.lower())

    def test_signin_is_post_only(self):


        for path in ("/auth/signin", "/auth/register"):
            code, _, headers = http(self.base + path)
            self.assertEqual(code, 302, path)
            self.assertEqual(headers["Location"], "/auth/login")


class TheDistributedLinksStillWork(_LocalCase):


    TOKENS = {"tok-link-0001": "Test_Operator"}

    def test_the_old_link_still_opens_the_workbench(self):
        code, page, _ = http(f"{self.base}/?k=tok-link-0001")
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"Test_Operator"', page)

    def test_the_link_carries_the_offer_to_make_an_account(self):


        code, body, _ = http(f"{self.base}/api/whoami?k=tok-link-0001")
        who = json.loads(body)
        self.assertEqual(who["sign_in_url"], "/signin")
        self.assertFalse(who["sso"])
        self.assertFalse(who["guest"])
        self.assertIsNone(who["email"])
        page = http(f"{self.base}/?k=tok-link-0001")[1]
        self.assertIn(b"Shared workspace", page)
        self.assertIn(b'href="/signin"', page)

    def test_the_link_and_the_account_land_in_the_SAME_workspace(self):


        _, _, headers = self.register()
        code, page, _ = http(f"{self.base}/",
                             headers=self.cookie(sid_from(headers)))
        self.assertEqual(code, 200)
        via_account = page.split(b'"workspace":"', 1)[1].split(b'"', 1)[0]
        via_link = http(f"{self.base}/?k=tok-link-0001")[1]             .split(b'"workspace":"', 1)[1].split(b'"', 1)[0]
        self.assertEqual(via_account, via_link, b"the account and the link "
                                                b"must open the same workspace")

    def test_a_signed_in_session_wins_over_a_link_in_the_url(self):


        _, _, headers = self.register()
        sid = sid_from(headers)
        code, body, _ = http(f"{self.base}/api/whoami?k=tok-link-0001",
                             headers=self.cookie(sid))
        who = json.loads(body)
        self.assertTrue(who["sso"])
        self.assertEqual(who["email"], "test_operator@letu.edu")


class NothingIsCached(_LocalCase):


    TOKENS = {"tok-cache-0001": "Test_Operator"}

    def test_the_workbench_page_is_never_stored(self):
        code, _, headers = http(f"{self.base}/?k=tok-cache-0001")
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_whoami_is_never_stored(self):


        code, _, headers = http(f"{self.base}/api/whoami?k=tok-cache-0001")
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_the_sign_in_pages_are_never_stored(self):
        for path in ("/auth/login", "/auth/login?new=1", "/auth/forgot"):
            _, _, headers = http(self.base + path)
            self.assertIn("no-store", headers.get("Cache-Control", ""), path)


class TellsAnAttackerNothing(_LocalCase):
    def test_wrong_password_and_unknown_address_read_identically(self):


        _, unknown, _ = self.signin(password="not the right one at all")
        self.register()
        _, wrong, _ = self.signin(password="not the right one at all")
        self.assertIn(b"do not match", wrong)
        self.assertIn(b"do not match", unknown)


        self.assertEqual(wrong, unknown)

    def test_an_unknown_address_is_not_answered_faster(self):


        self.register()
        accounts.forget_rate_limits()

        def timed(email):
            best = None
            for _ in range(3):
                accounts.forget_rate_limits()
                t0 = time.perf_counter()
                self.signin(email=email, password="wrong but long enough")
                dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            return best

        known = timed("test_operator@letu.edu")
        unknown = timed("nobody-at-all@letu.edu")

        self.assertGreater(unknown, known / 4,
                           f"unknown {unknown:.4f}s vs known {known:.4f}s")

    def test_neither_page_ever_echoes_the_password(self):
        self.register()
        secret = "hunter22 is the password"
        _, body, _ = self.signin(password=secret)
        self.assertNotIn(secret.encode(), body)
        _, body, _ = self.register(email="new@letu.edu", password=secret)
        self.assertNotIn(secret.encode(), body)

    def test_the_access_log_records_the_address_and_nothing_else(self):
        secret = "a memorable pass phrase"
        self.register(email="logged@letu.edu", password=secret)
        raw = server.ACCESS_LOG.read_text(encoding="utf-8")
        self.assertIn("logged@letu.edu", raw)
        self.assertNotIn(secret, raw)
        for row in self.log_rows():
            self.assertNotIn("password", json.dumps(row).lower())


class RefusesTheObviousMistakes(_LocalCase):
    def test_failed_signup_and_signin_keep_the_email_but_not_the_password(self):
        email = "New+Welder&Cell@letu.edu"
        for path in ("/auth/register", "/auth/signin"):
            with self.subTest(path=path):
                code, body, headers = self.post(path, email=email, password="short")
                self.assertEqual(code, 400)
                self.assertIsNone(sid_from(headers))
                field = re.search(rb'<input id="email"[^>]*>', body).group()
                self.assertIn(f'value="{html.escape(email)}"'.encode(), field)
                password = re.search(rb'<input id="password"[^>]*>', body).group()
                self.assertNotIn(b'value=', password)

    def test_a_refilled_address_cannot_inject_markup(self):
        email = '\"><script>alert(1)</script>&@letu.edu'
        for path in ("/auth/register", "/auth/signin"):
            with self.subTest(path=path):
                code, body, _ = self.post(path, email=email, password=GOOD)
                self.assertEqual(code, 400)
                self.assertIn(f'value="{html.escape(email)}"'.encode(), body)
                self.assertNotIn(email.encode(), body)

    def test_a_duplicate_address_is_refused(self):
        self.register()
        code, body, headers = self.register()
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"already an account", body)
        self.assertIn(b'value="test_operator@letu.edu"', body)

    def test_a_short_password_is_refused_and_says_the_rule(self):
        code, body, _ = self.register(password="short")
        self.assertEqual(code, 400)
        self.assertIn(str(accounts.MIN_PASSWORD_LEN).encode(), body)

    def test_something_that_is_not_an_address_is_refused(self):
        code, body, _ = self.register(email="not-an-email")
        self.assertEqual(code, 400)
        self.assertIn(b"email address", body)

    def test_an_oversized_body_is_not_read_into_memory(self):


        big = form(email="a@letu.edu", password="x" * 10000)
        code, _, headers = http(self.base + "/auth/register", method="POST",
                                data=big,
                                headers={"Content-Type":
                                         "application/x-www-form-urlencoded"})
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))


class RateLimited(_LocalCase):
    def test_a_rate_limited_form_keeps_the_email_without_hashing(self):
        email = "limited@letu.edu"
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT):
            accounts.record_failure(email, "127.0.0.1")
        with patch.object(accounts, "_derive") as derive:
            for path in ("/auth/register", "/auth/signin"):
                code, body, _ = self.post(path, email=email, password=GOOD)
                self.assertEqual(code, 429)
                self.assertIn(b'value="limited@letu.edu"', body)
            derive.assert_not_called()

    def test_guessing_is_cut_off(self):
        self.register()
        seen = None
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT + 2):
            code, body, _ = self.signin(password="wrong guess number n")
            seen = code
            if code == 429:
                break
        self.assertEqual(seen, 429)
        self.assertIn(b"Too many failed attempts", body)

    def test_the_lockout_does_not_leak_the_right_password(self):


        self.register()
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT + 1):
            self.signin(password="wrong guess number n")
        code, _, headers = self.signin(password=GOOD)
        self.assertEqual(code, 429)
        self.assertIsNone(sid_from(headers))

    def test_a_success_clears_the_count(self):
        self.register()
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT - 2):
            self.signin(password="wrong guess number n")
        code, _, headers = self.signin(password=GOOD)
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT - 2):
            code, _, _ = self.signin(password="wrong guess number n")
            self.assertNotEqual(code, 429)


class TheSignupBudgets(_LocalCase):


    def set_cap(self, value):
        raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.addCleanup(self.restore_cfg, raw)
        self.cfg_path.write_text(json.dumps(dict(raw, max_accounts=value)),
                                 encoding="utf-8")
        server._AUTH = server._AUTH_STAMP = None

    def restore_cfg(self, raw):
        self.cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        server._AUTH = server._AUTH_STAMP = None

    def test_the_workbench_stops_accepting_accounts_at_the_cap(self):
        self.set_cap(2)
        with patch.object(accounts, "MAX_REGISTRATIONS_PER_CLIENT", 50):
            for n in (1, 2):
                code, _, _ = self.post("/auth/register", email=f"a{n}@letu.edu",
                                       password=GOOD)
                self.assertEqual(code, 302)
            code, body, _ = self.post("/auth/register", email="a3@letu.edu",
                                      password=GOOD)
        self.assertEqual(code, 400)
        self.assertIn(b"not accepting new accounts", body)
        self.assertEqual(len(server.ACCOUNTS.emails()), 2)

    def test_a_full_workbench_still_tells_an_existing_address_the_truth(self):


        self.post("/auth/register", email="a1@letu.edu", password=GOOD)
        self.set_cap(1)
        code, body, _ = self.post("/auth/register", email="a1@letu.edu",
                                  password=GOOD)
        self.assertEqual(code, 400)
        self.assertIn(b"already an account", body)

    def test_one_client_cannot_create_accounts_without_end(self):
        with patch.object(accounts, "MAX_REGISTRATIONS_PER_CLIENT", 2):
            for n in (1, 2):
                code, _, _ = self.post("/auth/register", email=f"b{n}@letu.edu",
                                       password=GOOD)
                self.assertEqual(code, 302)
            code, body, _ = self.post("/auth/register", email="b3@letu.edu",
                                      password=GOOD)
        self.assertEqual(code, 429)
        self.assertIn(b"Too many accounts", body)

        code, _, headers = self.signin(email="b1@letu.edu")
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))

    def test_the_configured_cap_is_the_one_that_applies(self):
        cfg = oidc.load_auth_config(self.cfg_path)
        self.assertEqual(cfg.max_accounts, accounts.MAX_ACCOUNTS)
        raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.cfg_path.write_text(json.dumps(dict(raw, max_accounts=1)), encoding="utf-8")
        self.addCleanup(self.cfg_path.write_text,
                        json.dumps(raw), "utf-8")
        self.assertEqual(oidc.load_auth_config(self.cfg_path).max_accounts, 1)
        for bad in (0, -5, "many", True):
            self.cfg_path.write_text(json.dumps(dict(raw, max_accounts=bad)),
                                     encoding="utf-8")
            self.assertIsNone(oidc.load_auth_config(self.cfg_path), bad)


class PolicyIsTheSameLadder(_LocalCase):


    def test_blocking_kills_a_live_local_session(self):


        _, _, headers = self.register(email="oops@letu.edu")
        sid = sid_from(headers)
        code, _, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertEqual(code, 200)

        self.write_config(dict(LOCAL_CFG, blocked=["oops@letu.edu"]))
        try:
            code, body, _ = http(f"{self.base}/", headers=self.cookie(sid))
            self.assertEqual(code, 401)
            self.assertIn(b"no longer has access", body)
            self.assertIsNone(oidc.session_for(sid))

            code, body, headers = self.signin(email="oops@letu.edu")
            self.assertEqual(code, 403)
            self.assertIsNone(sid_from(headers))
        finally:
            self.write_config(LOCAL_CFG)

    def test_a_self_asserted_address_is_never_treated_as_verified(self):


        _, _, headers = self.register(email="stranger@letu.edu")
        sid = sid_from(headers)
        code, body, _ = http(f"{self.base}/api/whoami", headers=self.cookie(sid))
        who = json.loads(body)
        self.assertEqual(who["email"], "stranger@letu.edu")
        self.assertFalse(who["known"], "an unverified letu.edu claim must not "
                                       "count as recognised")
        rows = [r for r in self.log_rows() if r.get("email") == "stranger@letu.edu"]
        self.assertTrue(any(r.get("known") is False for r in rows))

    def test_a_rostered_address_gets_its_chosen_name(self):
        _, _, headers = self.register(email="TEST_OPERATOR@LETU.edu")
        code, body, _ = http(f"{self.base}/api/whoami",
                             headers=self.cookie(sid_from(headers)))
        who = json.loads(body)
        self.assertEqual(who["workspace"], "Test_Operator")
        self.assertTrue(who["known"])

    def test_closed_registration_turns_the_door_off(self):
        self.write_config(dict(LOCAL_CFG, open_signin=False))
        try:
            code, body, headers = self.register(email="outsider@example.com")
            self.assertEqual(code, 400)
            self.assertIsNone(sid_from(headers))
            self.assertIn(b"limited to approved users", body)

            code, _, headers = self.register(email="test_operator@letu.edu")
            self.assertEqual(code, 302)
            self.assertIsNotNone(sid_from(headers))
        finally:
            self.write_config(LOCAL_CFG)


class TheGuestDoor(_LocalCase):


    TOKENS = {"tok-guest-0001": "someone"}

    def setUp(self):
        super().setUp()
        server.forget_guests()
        self.addCleanup(server.forget_guests)

    def guest(self):
        code, _, headers = self.post("/auth/guest")
        self.assertEqual(code, 302)
        jar = headers.get("Set-Cookie", "")
        self.assertIn(server.GUEST_COOKIE, jar)
        gid = jar.split(server.GUEST_COOKIE + "=", 1)[1].split(";", 1)[0]
        return gid, {"Cookie": f"{server.GUEST_COOKIE}={gid}"}

    def test_a_guest_can_use_the_workbench(self):
        _, cookie = self.guest()
        code, page, _ = http(f"{self.base}/", headers=cookie)
        self.assertEqual(code, 200)
        self.assertIn(b'"workspace":"guest-', page)

    def test_a_guest_cannot_save_and_is_told_why(self):

        _, cookie = self.guest()
        http(f"{self.base}/", headers=cookie)
        code, body, _ = http(f"{self.base}/api/save?name=Mine", method="POST",
                             headers=cookie)
        self.assertEqual(code, 403)
        err = json.loads(body)["error"]


        self.assertIn("save", err)
        self.assertIn("Sign in", err)
        self.assertFalse((server.SAVES_DIR).glob("guest-*") and
                         list((server.SAVES_DIR).glob("guest-*/*")))

    def test_two_guests_do_not_share_a_workspace(self):


        _, a = self.guest()
        _, b = self.guest()
        _, page_a, _ = http(f"{self.base}/", headers=a)
        _, page_b, _ = http(f"{self.base}/", headers=b)
        ws = lambda page: page.split(b'"workspace":"', 1)[1].split(b'"', 1)[0]
        self.assertNotEqual(ws(page_a), ws(page_b))

    def test_a_guest_is_flagged_to_the_page_and_the_log(self):
        _, cookie = self.guest()
        code, body, _ = http(f"{self.base}/api/whoami", headers=cookie)
        who = json.loads(body)
        self.assertTrue(who["guest"])
        self.assertFalse(who["sso"])
        self.assertIsNotNone(who["sign_in_url"])
        self.assertTrue(any(r["outcome"] == "guest-start" for r in self.log_rows()))

    def test_guests_are_capped_so_a_stranger_cannot_fill_memory(self):
        for _ in range(server.MAX_GUESTS + 5):
            self.post("/auth/guest")
        self.assertLessEqual(len(server._GUESTS), server.MAX_GUESTS)

    def test_an_expired_guest_cookie_stops_working(self):
        gid, cookie = self.guest()
        code, _, _ = http(f"{self.base}/", headers=cookie)
        self.assertEqual(code, 200)
        self.assertIn(server._guest_workspace(gid), server._STATES)
        with server._GUEST_LOCK:
            server._GUESTS[gid] = time.time() - server.GUEST_TTL_S - 1
        code, _, _ = http(f"{self.base}/", headers=cookie)
        self.assertEqual(code, 401)

        self.assertNotIn(server._guest_workspace(gid), server._STATES)

    def test_a_made_up_guest_cookie_is_not_a_way_in(self):
        code, _, _ = http(f"{self.base}/",
                          headers={"Cookie": f"{server.GUEST_COOKIE}=pretend"})
        self.assertEqual(code, 401)

    def test_signing_in_stops_you_being_a_guest(self):
        gid, cookie = self.guest()
        _, _, headers = self.register()
        sid = sid_from(headers)
        both = {"Cookie": f"{server.SESSION_COOKIE}={sid}; "
                          f"{server.GUEST_COOKIE}={gid}"}
        code, body, _ = http(f"{self.base}/api/whoami", headers=both)
        who = json.loads(body)
        self.assertFalse(who["guest"])
        self.assertEqual(who["workspace"], "Test_Operator")


VERIFY_CFG = dict(LOCAL_CFG, mail={
    "host": "smtp.example.com", "port": 587,
    "username": "workbench@example.com", "password": "x" * 16,
    "from": "WAAM Workbench <workbench@example.com>"})


class TheSignupCopy(_LocalCase):
    def test_signup_rule_follows_the_config(self):
        self.addCleanup(self.write_config, LOCAL_CFG)
        for approval in (True, False):
            for opened, mail in ((True, True), (False, True), (False, False)):
                with self.subTest(approval=approval, opened=opened, mail=mail):
                    cfg = dict(VERIFY_CFG if mail else LOCAL_CFG,
                               approval_required=approval, open_signin=opened)
                    self.write_config(cfg)
                    status, body, _ = http(self.base + "/auth/login?new=1")
                    self.assertEqual(status, 200)
                    self.assertEqual(b"administrator approval" in body, approval)
                    self.assertEqual(b"Request access" in body, approval)
                    self.assertEqual(b"limited to approved users" in body, not opened)
                    self.assertEqual(b"or an address at letu.edu" in body,
                                     not opened and mail)
                    _, choice, _ = http(self.base + "/auth/login")
                    self.assertEqual(b"administrator approval" in choice, approval)

    def test_password_creation_advice_is_only_on_the_create_tab(self):
        _, signin, _ = http(self.base + "/auth/login?do=in")
        _, signup, _ = http(self.base + "/auth/login?new=1")
        for advice in (b"At least", b"Use a unique password",
                       b"Do not reuse", b"minlength="):
            self.assertIn(advice, signup)
            self.assertNotIn(advice, signin)
        self.assertIn(b'autocomplete="current-password"', signin)
        self.assertIn(b"Forgot password?", signin)
        self.assertIn(b'autocomplete="new-password"', signup)


class TheVerificationLoop(_LocalCase):


    CONFIG = VERIFY_CFG

    def setUp(self):
        super().setUp()
        accounts.forget_codes()
        self.sent = []
        self._real_send = mailer.send

        def capture(cfg, to, subject, body):
            self.sent.append({"to": to, "subject": subject, "body": body})

        mailer.send = capture
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(accounts.forget_codes)

    def code_from_email(self):
        self.assertTrue(self.sent, "no email was sent")
        digits = [w for w in self.sent[-1]["body"].split() if w.isdigit()
                  and len(w) == accounts.CODE_DIGITS]
        self.assertTrue(digits, self.sent[-1]["body"])
        return digits[0]

    def verify(self, email="test_operator@letu.edu", code=None):
        return self.post("/auth/verify", email=email,
                         code=code if code is not None else self.code_from_email())

    def test_registering_sends_a_code_and_grants_nothing_yet(self):
        code, body, headers = self.register()
        self.assertEqual(code, 200)
        self.assertIsNone(sid_from(headers), "a session before the code came "
                                             "back would make verification "
                                             "decorative")
        self.assertIn(b"Check your email", body)
        self.assertIn(b"test_operator@letu.edu", body)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], "test_operator@letu.edu")

    def test_verification_links_follow_the_public_url_on_reload(self):
        self.addCleanup(self.write_config, VERIFY_CFG)
        urls = ("https://first.example", "https://second.example:9443/")
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                self.write_config(dict(VERIFY_CFG, public_url=url))
                self.register(email=f"mail{index}@letu.edu")
                self.assertEqual(re.findall(r"https?://\S+", self.sent[-1]["body"]),
                                 [url.rstrip("/") + "/signin"])

    def test_the_right_code_finishes_the_job(self):
        self.register()
        code, _, headers = self.verify()
        self.assertEqual(code, 302)
        sid = sid_from(headers)
        self.assertIsNotNone(sid)
        self.assertTrue(server.ACCOUNTS.get("test_operator@letu.edu").verified)
        code, page, _ = http(f"{self.base}/", headers=self.cookie(sid))
        self.assertIn(b'"workspace":"Test_Operator"', page)

    def test_the_code_is_never_shown_only_mailed(self):
        _, body, _ = self.register()
        secret = self.code_from_email()
        self.assertNotIn(secret.encode(), body)
        raw = server.ACCESS_LOG.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)

    def test_a_wrong_code_is_refused_and_the_account_stays_unverified(self):
        self.register()
        real = self.code_from_email()
        wrong = "000000" if real != "000000" else "111111"
        code, body, headers = self.verify(code=wrong)
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"code is incorrect", body)
        self.assertFalse(server.ACCOUNTS.get("test_operator@letu.edu").verified)

    def test_the_code_dies_after_enough_wrong_guesses(self):


        self.register()
        real = self.code_from_email()
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(accounts.MAX_CODE_ATTEMPTS + 1):
            self.verify(code=wrong)
        code, _, headers = self.verify(code=real)
        self.assertIn(code, (400, 429))
        self.assertIsNone(sid_from(headers))

    def test_a_code_works_once(self):
        self.register()
        real = self.code_from_email()
        self.assertEqual(self.verify(code=real)[0], 302)
        code, _, headers = self.verify(code=real)
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))

    def test_resending_is_rate_limited(self):

        self.register()
        code, body, _ = self.post("/auth/resend", email="test_operator@letu.edu")
        self.assertEqual(code, 429)
        self.assertIn(b"Wait", body)
        self.assertEqual(len(self.sent), 1)

    def test_signing_in_before_verifying_puts_you_back_in_the_loop(self):

        self.register()
        accounts.forget_codes()
        code, body, headers = self.signin()
        self.assertEqual(code, 200)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"Check your email", body)
        self.assertEqual(len(self.sent), 2)

    def test_a_verified_address_earns_allow_domains(self):


        self.register(email="newperson@letu.edu")
        code, _, headers = self.verify(email="newperson@letu.edu")
        self.assertEqual(code, 302)
        _, body, _ = http(f"{self.base}/api/whoami",
                          headers=self.cookie(sid_from(headers)))
        who = json.loads(body)
        self.assertTrue(who["known"], "a verified allow_domains address should "
                                      "be recognised")

    def test_a_mail_failure_does_not_strand_the_account(self):


        def boom(cfg, to, subject, body):
            raise mailer.MailError("the mail server said no")

        mailer.send = boom
        code, body, headers = self.register()
        self.assertEqual(code, 502)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"Could not send the verification email", body)
        self.assertIn(b"Send another code", body)
        self.assertIsNotNone(server.ACCOUNTS.get("test_operator@letu.edu"))

    def test_verify_does_not_answer_whether_an_address_exists(self):
        code, _, headers = self.post("/auth/verify", email="nobody@letu.edu",
                                     code="123456")
        self.assertEqual(code, 302)
        self.assertEqual(headers["Location"], server.SIGN_IN_PATH)
        self.assertIsNone(sid_from(headers))


class TheForgottenPasswordLoop(_LocalCase):


    TOKENS = {"tok-reset-0001": "someone"}
    CONFIG = VERIFY_CFG

    def setUp(self):
        super().setUp()
        accounts.forget_codes()
        self.sent = []
        self._real_send = mailer.send

        def capture(cfg, to, subject, body):
            self.sent.append({"to": to, "subject": subject, "body": body})

        mailer.send = capture
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(accounts.forget_codes)

    def code_from_email(self):
        self.assertTrue(self.sent, "no email was sent")
        digits = [w for w in self.sent[-1]["body"].split()
                  if w.isdigit() and len(w) == accounts.CODE_DIGITS]
        self.assertTrue(digits, self.sent[-1]["body"])
        return digits[0]

    def make_account(self, email="test_operator@letu.edu", password=GOOD):

        self.post("/auth/register", email=email, password=password)
        code = self.code_from_email()
        _, _, headers = self.post("/auth/verify", email=email, code=code)
        accounts.forget_codes()
        accounts.forget_rate_limits()
        self.sent.clear()
        return sid_from(headers)

    def test_a_forgotten_password_can_be_set_without_the_owner(self):
        self.make_account()
        code, body, _ = self.post("/auth/forgot", email="test_operator@letu.edu")
        self.assertEqual(code, 200)
        self.assertIn(b"6-digit code", body)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("reset", self.sent[0]["subject"].lower())

        code, _, headers = self.post("/auth/reset",
                                     email="test_operator@letu.edu",
                                     code=self.code_from_email(),
                                     password="a brand new pass phrase")
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))

        self.assertEqual(self.signin(password="a brand new pass phrase")[0], 302)
        accounts.forget_rate_limits()
        self.assertEqual(self.signin(password=GOOD)[0], 400)

    def test_it_does_not_say_whether_the_address_has_an_account(self):


        self.make_account()
        _, real, _ = self.post("/auth/forgot", email="test_operator@letu.edu")
        accounts.forget_rate_limits()
        _, fake, _ = self.post("/auth/forgot", email="nobody@letu.edu")
        self.assertEqual(real.replace(b"test_operator", b"nobody"), fake)
        self.assertEqual(len(self.sent), 1, "no mail for an address with no "
                                            "account")

    def test_a_reset_ends_the_sessions_that_account_was_holding(self):


        stolen = self.make_account()
        self.assertEqual(http(f"{self.base}/", headers=self.cookie(stolen))[0], 200)

        self.post("/auth/forgot", email="test_operator@letu.edu")
        self.post("/auth/reset", email="test_operator@letu.edu",
                  code=self.code_from_email(), password="a brand new pass phrase")
        self.assertIsNone(oidc.session_for(stolen))
        self.assertEqual(http(f"{self.base}/", headers=self.cookie(stolen))[0], 401)

    def test_a_verification_code_cannot_be_spent_as_a_reset_code(self):


        self.post("/auth/register", email="fresh@letu.edu", password=GOOD)
        verify_code = self.code_from_email()
        code, body, headers = self.post("/auth/reset", email="fresh@letu.edu",
                                        code=verify_code,
                                        password="a brand new pass phrase")
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"expired", body)


        accounts.forget_rate_limits()
        accounts.forget_codes()
        self.assertEqual(self.post("/auth/signin", email="fresh@letu.edu",
                                   password=GOOD)[0], 200)

    def test_a_reset_code_cannot_be_spent_as_a_verification_code(self):
        self.make_account(email="both@letu.edu")
        self.post("/auth/forgot", email="both@letu.edu")
        reset_code = self.code_from_email()
        code, body, headers = self.post("/auth/verify", email="both@letu.edu",
                                        code=reset_code)
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))

    def test_a_short_new_password_does_not_burn_the_code(self):


        self.make_account()
        self.post("/auth/forgot", email="test_operator@letu.edu")
        real = self.code_from_email()
        code, body, _ = self.post("/auth/reset", email="test_operator@letu.edu",
                                  code=real, password="short")
        self.assertEqual(code, 400)
        self.assertIn(str(accounts.MIN_PASSWORD_LEN).encode(), body)

        code, _, headers = self.post("/auth/reset",
                                     email="test_operator@letu.edu", code=real,
                                     password="a brand new pass phrase")
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))

    def test_the_current_password_is_refused_without_spending_the_code(self):
        email = "test_operator@letu.edu"
        sid = self.make_account()
        before = server.ACCOUNTS.path.read_bytes()
        self.post("/auth/forgot", email=email)
        real = self.code_from_email()
        wrong = "000000" if real != "000000" else "111111"


        for _ in range(accounts.MAX_CODE_ATTEMPTS - 1):
            self.assertEqual(self.post("/auth/reset", email=email, code=wrong,
                                       password="a different pass phrase")[0], 400)
        code, body, headers = self.post("/auth/reset", email=email,
                                       code=real, password=GOOD)
        self.assertEqual(code, 400)
        self.assertIn(b"different from your current password", body)
        self.assertNotIn(GOOD.encode(), body)
        self.assertIsNone(sid_from(headers))
        self.assertIsNotNone(oidc.session_for(sid))
        self.assertEqual(server.ACCOUNTS.path.read_bytes(), before)
        self.assertTrue(accounts.code_pending(email, accounts.PURPOSE_RESET))
        code, _, headers = self.post("/auth/reset", email=email, code=real,
                                     password="a different pass phrase")
        self.assertEqual(code, 302)
        self.assertIsNotNone(sid_from(headers))
        self.assertIsNone(oidc.session_for(sid))
        self.assertFalse(accounts.code_pending(email))
        self.assertEqual(self.post("/auth/reset", email=email, code=real,
                                   password="another different phrase")[0], 400)

    def test_reset_checks_the_code_before_comparing_the_current_password(self):
        email = "test_operator@letu.edu"
        self.make_account()
        self.post("/auth/forgot", email=email)
        real = self.code_from_email()
        wrong = "000000" if real != "000000" else "111111"
        with patch.object(accounts, "verify_password",
                          wraps=accounts.verify_password) as verify:
            _, body, _ = self.post("/auth/reset", email=email,
                                   code=wrong, password=GOOD)
            self.assertIn(b"code is incorrect", body)
            verify.assert_not_called()
            self.post("/auth/reset", email=email, code=real, password=GOOD)
            verify.assert_called_once()

    def test_a_locked_out_reset_does_no_hashing(self):
        email = "test_operator@letu.edu"
        self.make_account()
        self.post("/auth/forgot", email=email)
        real = self.code_from_email()
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT):
            accounts.record_failure(email, "127.0.0.1")
        with patch.object(accounts, "_derive") as derive:
            status, _, _ = self.post("/auth/reset", email=email,
                                     code=real, password=GOOD)
            self.assertEqual(status, 429)
            derive.assert_not_called()
        self.assertTrue(accounts.code_pending(email, accounts.PURPOSE_RESET))

    def test_reset_links_follow_the_public_url_on_reload(self):
        self.addCleanup(self.write_config, VERIFY_CFG)
        self.make_account()
        for url in ("https://first.example/", "https://second.example:9443"):
            with self.subTest(url=url):
                accounts.forget_codes()
                self.write_config(dict(VERIFY_CFG, public_url=url))
                self.post("/auth/forgot", email="test_operator@letu.edu")
                self.assertEqual(re.findall(r"https?://\S+", self.sent[-1]["body"]),
                                 [url.rstrip("/") + "/signin"])

    def test_a_wrong_reset_code_is_rate_limited(self):
        self.make_account()
        self.post("/auth/forgot", email="test_operator@letu.edu")
        real = self.code_from_email()
        wrong = "000000" if real != "000000" else "111111"
        seen = None
        for _ in range(accounts.MAX_FAILS_PER_ACCOUNT + 2):
            seen, _, _ = self.post("/auth/reset",
                                   email="test_operator@letu.edu",
                                   code=wrong, password="a brand new phrase")
            if seen == 429:
                break
        self.assertEqual(seen, 429)

    def test_a_completed_reset_also_proves_the_address(self):


        self.post("/auth/register", email="never@letu.edu", password=GOOD)
        accounts.forget_codes()
        accounts.forget_rate_limits()
        self.sent.clear()
        self.assertFalse(server.ACCOUNTS.get("never@letu.edu").verified)

        self.post("/auth/forgot", email="never@letu.edu")
        code, _, headers = self.post("/auth/reset", email="never@letu.edu",
                                     code=self.code_from_email(),
                                     password="a brand new pass phrase")
        self.assertEqual(code, 302)
        self.assertTrue(server.ACCOUNTS.get("never@letu.edu").verified)
        _, body, _ = http(f"{self.base}/api/whoami",
                          headers=self.cookie(sid_from(headers)))
        self.assertTrue(json.loads(body)["known"])

    def test_the_reset_code_never_appears_in_a_page_or_the_log(self):
        self.make_account()
        _, body, _ = self.post("/auth/forgot", email="test_operator@letu.edu")
        secret = self.code_from_email()
        self.assertNotIn(secret.encode(), body)
        self.assertNotIn(secret, server.ACCESS_LOG.read_text(encoding="utf-8"))


class ResetWithNoMailer(_LocalCase):


    CONFIG = LOCAL_CFG

    def test_it_says_so_instead_of_pretending(self):
        code, body, _ = self.post("/auth/forgot", email="test_operator@letu.edu")
        self.assertEqual(code, 503)
        self.assertIn(b"Email reset is unavailable", body)
        self.assertIn(b"Contact the administrator to reset your password", body)


class TheMailSender(unittest.TestCase):
    def test_a_bare_sender_gets_the_studio_name(self):
        for sender in (None, "owner@gmail.com"):
            with self.subTest(sender=sender):
                cfg = mailer.MailConfig.load(dict(VERIFY_CFG["mail"],
                                                  username="owner@gmail.com",
                                                  **{"from": sender}))
                with patch.object(mailer, "_connect") as connect:
                    mailer.send_code(cfg, "reader@letu.edu", "123456")
                    smtp = connect.return_value.__enter__.return_value
                    msg = smtp.send_message.call_args.args[0]
                self.assertEqual(parseaddr(str(msg["From"])),
                                 ("WAAM Studio", "owner@gmail.com"))
                self.assertEqual(str(msg["To"]), "reader@letu.edu")

    def test_an_explicit_sender_name_is_preserved(self):
        cfg = mailer.MailConfig.load(dict(VERIFY_CFG["mail"],
                                          **{"from": "Cell Studio <owner@gmail.com>"}))
        with patch.object(mailer, "_connect") as connect:
            mailer.send_reset_code(cfg, "reader@letu.edu", "123456")
            smtp = connect.return_value.__enter__.return_value
            msg = smtp.send_message.call_args.args[0]
        self.assertEqual(parseaddr(str(msg["From"])),
                         ("Cell Studio", "owner@gmail.com"))


class TheOutboundMailBudget(unittest.TestCase):


    def setUp(self):
        mailer.forget_send_budget()
        self.addCleanup(mailer.forget_send_budget)
        self.cfg = mailer.MailConfig.load(VERIFY_CFG["mail"])

    def test_the_hourly_budget_stops_the_flood(self):
        with patch.object(mailer, "MAX_SENDS_PER_WINDOW", 3),                 patch.object(mailer, "_connect") as connect:
            for _ in range(3):
                mailer.send_code(self.cfg, "reader@letu.edu", "123456")
            with self.assertRaises(mailer.MailError) as caught:
                mailer.send_code(self.cfg, "reader@letu.edu", "123456")
        self.assertIn("in an hour", str(caught.exception))
        self.assertEqual(connect.call_count, 3, "the refused send must not dial out")

    def test_the_window_lets_it_recover(self):
        with patch.object(mailer, "MAX_SENDS_PER_WINDOW", 1),                 patch.object(mailer, "SEND_WINDOW_S", 0),                 patch.object(mailer, "_connect"):
            mailer.send_code(self.cfg, "reader@letu.edu", "123456")
            mailer.send_code(self.cfg, "reader@letu.edu", "123456")


class TheMailConfigureHelper(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-mailcfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = self.tmp / ".waam-auth.json"
        self.example = self.tmp / ".waam-auth.json.example"
        self.example.write_text(json.dumps({
            "provider": "local", "public_url": "https://c.example:8443",
            "mail": {"host": "smtp.gmail.com", "port": 587,
                     "username": "FILL-IN@gmail.com",
                     "password": "FILL-IN-app-password",
                     "from": "WAAM Studio <FILL-IN@gmail.com>"}}),
            encoding="utf-8")

    def run_configure(self, answers, secret):
        import getpass
        from waam_pipeline import mailer as m
        it = iter(answers)
        real_input, real_getpass = __builtins__["input"] if isinstance(
            __builtins__, dict) else __builtins__.input, getpass.getpass
        import builtins
        builtins.input = lambda *a: next(it)
        getpass.getpass = lambda *a: secret
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = m._main(["--configure", "--file", str(self.cfg)])
            return rc, out.getvalue()
        finally:
            builtins.input = real_input
            getpass.getpass = real_getpass

    def test_it_writes_the_password_without_ever_printing_it(self):
        secret = "abcd efgh ijkl mnop"
        rc, out = self.run_configure(["y", "tester@gmail.com"], secret)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret.replace(" ", ""), out)
        mail = json.loads(self.cfg.read_text(encoding="utf-8"))["mail"]


        self.assertEqual(mail["password"], "abcdefghijklmnop")
        self.assertEqual(mail["username"], "tester@gmail.com")
        self.assertIn("tester@gmail.com", mail["from"])
        self.assertNotIn("FILL-IN", json.dumps(mail).upper())

    def test_configuring_a_bare_from_address_adds_the_studio_name(self):
        cfg = dict(VERIFY_CFG, mail=dict(VERIFY_CFG["mail"],
                                        **{"from": "old@gmail.com"}))
        self.cfg.write_text(json.dumps(cfg), encoding="utf-8")
        rc, _ = self.run_configure(["tester@gmail.com"], "abcdefghijklmnop")
        self.assertEqual(rc, 0)
        sender = json.loads(self.cfg.read_text(encoding="utf-8"))["mail"]["from"]
        self.assertEqual(parseaddr(sender), ("WAAM Studio", "tester@gmail.com"))

    def test_declining_the_prompt_creates_nothing(self):

        rc, out = self.run_configure(["n"], "abcdefghijklmnop")
        self.assertEqual(rc, 1)
        self.assertFalse(self.cfg.exists())
        self.assertIn("nothing changed", out)

    def test_an_empty_password_changes_nothing(self):
        rc, out = self.run_configure(["y", "tester@gmail.com"], "")
        self.assertEqual(rc, 1)
        self.assertIn("nothing entered", out)

    def test_a_bad_address_changes_nothing(self):
        rc, out = self.run_configure(["y", "not-an-address"], "abcdefghijklmnop")
        self.assertEqual(rc, 1)
        self.assertIn("does not look like", out)


class ClosedButNotRosterOnly(_LocalCase):


    CONFIG = dict(VERIFY_CFG, open_signin=False)

    def setUp(self):
        super().setUp()
        accounts.forget_codes()
        self.sent = []
        self._real_send = mailer.send
        mailer.send = lambda cfg, to, subject, body: self.sent.append(
            {"to": to, "subject": subject, "body": body})
        self.addCleanup(lambda: setattr(mailer, "send", self._real_send))
        self.addCleanup(accounts.forget_codes)

    def code(self):
        return [w for w in self.sent[-1]["body"].split()
                if w.isdigit() and len(w) == accounts.CODE_DIGITS][0]

    def test_an_unrostered_letu_address_admits_itself(self):

        code, body, headers = self.post("/auth/register", email="dai@letu.edu",
                                        password=GOOD)
        self.assertEqual(code, 200, body[:400])
        self.assertIsNone(sid_from(headers))
        code, _, headers = self.post("/auth/verify", email="dai@letu.edu",
                                     code=self.code())
        self.assertEqual(code, 302)
        _, who, _ = http(f"{self.base}/api/whoami",
                         headers=self.cookie(sid_from(headers)))
        who = json.loads(who)
        self.assertEqual(who["email"], "dai@letu.edu")
        self.assertTrue(who["known"], "a verified allow_domains address is "
                                      "recognised without a roster entry")

    def test_an_outside_address_cannot_even_register(self):
        code, body, headers = self.post("/auth/register",
                                        email="stranger@gmail.com",
                                        password=GOOD)
        self.assertEqual(code, 400)
        self.assertIsNone(sid_from(headers))
        self.assertIn(b"limited to approved users", body)
        self.assertIn(b"letu.edu", body)
        refusal = re.search(rb'<p class="bad">(.*?)</p>', body, re.S).group(1)
        self.assertIn(b"stranger@gmail.com", refusal)
        self.assertEqual(self.sent, [], "no code should go to an address that "
                                        "could never be admitted")

    def test_a_blocked_address_cannot_register_its_way_back_in(self):
        self.write_config(dict(VERIFY_CFG, open_signin=False,
                               blocked=["gone@letu.edu"]))
        try:
            code, _, headers = self.post("/auth/register", email="gone@letu.edu",
                                         password=GOOD)
            self.assertEqual(code, 400)
            self.assertIsNone(sid_from(headers))
            self.assertEqual(self.sent, [])
        finally:
            self.write_config(dict(VERIFY_CFG, open_signin=False))

    def test_without_a_mailer_closed_really_does_mean_roster_only(self):

        self.write_config(dict(LOCAL_CFG, open_signin=False))
        try:
            code, body, _ = self.post("/auth/register", email="dai@letu.edu",
                                      password=GOOD)
            self.assertEqual(code, 400)
            self.assertIn(b"limited to approved users", body)
            self.assertNotIn(b"letu.edu.", body)
        finally:
            self.write_config(dict(VERIFY_CFG, open_signin=False))


class ClosingTheSharedLink(_LocalCase):


    TOKENS = {"tok-shared-0001": "default"}

    def setUp(self):
        super().setUp()
        self.roster = self.tmp / ".waam-tokens.json"
        self.shared = self.tmp / ".waam-token"
        self._saved = (server._TOKEN_GATE, server._TOKEN_EXPLICIT,
                       server._TOKENS_STAMP, server._TOKENS,
                       server._tokens_file, server._token_file)
        server._tokens_file = lambda: self.roster
        server._token_file = lambda: self.shared
        server._TOKEN_GATE = True
        server._TOKEN_EXPLICIT = None
        server._TOKENS_STAMP = None
        server._TOKENS = {}
        self.addCleanup(self._restore)

    def _restore(self):
        (server._TOKEN_GATE, server._TOKEN_EXPLICIT, server._TOKENS_STAMP,
         server._TOKENS, server._tokens_file, server._token_file) = self._saved

    def write_roster(self, tokens: dict):
        self.roster.write_text(json.dumps(
            {"tokens": {t: {"name": n} for t, n in tokens.items()}}),
            encoding="utf-8")

    def test_removing_a_link_takes_effect_without_a_restart(self):
        self.write_roster({"tok-a": "Someone", "tok-b": "Other"})
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 200)

        self.write_roster({"tok-b": "Other"})
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 401)
        self.assertEqual(http(f"{self.base}/?k=tok-b")[0], 200)

    def test_deleting_the_shared_token_closes_it(self):


        self.shared.write_text("shared-tok-0001\n", encoding="utf-8")
        self.write_roster({"tok-a": "Someone"})
        self.assertEqual(http(f"{self.base}/?k=shared-tok-0001")[0], 200)

        self.shared.unlink()
        self.assertEqual(http(f"{self.base}/?k=shared-tok-0001")[0], 401)
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 200,
                         "closing the shared link must not close the others")

    def test_closing_the_shared_token_never_MINTS_a_new_one(self):


        self.shared.write_text("shared-tok-0001\n", encoding="utf-8")
        self.write_roster({"tok-a": "Someone"})
        http(f"{self.base}/?k=shared-tok-0001")
        self.shared.unlink()
        http(f"{self.base}/?k=tok-a")
        self.assertFalse(self.shared.exists(),
                         "a refresh must never create a token file")

    def test_an_empty_roster_refuses_everybody_rather_than_falling_open(self):


        self.write_roster({"tok-a": "Someone"})
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 200)

        self.write_roster({})
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 401)
        self.assertEqual(http(f"{self.base}/")[0], 401)

    def test_a_torn_roster_keeps_the_last_good_one(self):


        self.write_roster({"tok-a": "Someone"})
        self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 200)

        self.roster.write_text("{ this is not json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(http(f"{self.base}/?k=tok-a")[0], 200)
        self.assertEqual(http(f"{self.base}/?k=nope")[0], 401)

    def test_an_account_still_works_after_the_shared_link_is_closed(self):


        self.shared.write_text("shared-tok-0001\n", encoding="utf-8")
        self.write_roster({})
        _, _, headers = self.register()
        sid = sid_from(headers)
        self.assertIsNotNone(sid)

        self.shared.unlink()
        self.assertEqual(http(f"{self.base}/?k=shared-tok-0001")[0], 401)
        self.assertEqual(http(f"{self.base}/", headers=self.cookie(sid))[0], 200)

    def test_signing_up_never_needed_the_shared_link_at_all(self):


        self.write_roster({})
        for path in ("/auth/login", "/auth/login?new=1", "/auth/forgot"):
            self.assertEqual(http(self.base + path)[0], 200, path)


class TheCodeConsumption(unittest.TestCase):
    def setUp(self):
        accounts.forget_codes()
        self.addCleanup(accounts.forget_codes)

    def test_a_rejected_change_leaves_the_valid_code_unspent(self):
        email = "waiting@letu.edu"
        code = accounts.new_code(email, accounts.PURPOSE_RESET)

        def refuse():
            raise accounts.AccountError("choose another password")

        with self.assertRaisesRegex(accounts.AccountError, "choose another"):
            accounts.check_code(email, code, accounts.PURPOSE_RESET,
                                before_consume=refuse)
        accounts.check_code(email, code, accounts.PURPOSE_RESET)
        self.assertFalse(accounts.code_pending(email))

    def test_a_verify_code_never_runs_the_reset_check(self):
        email = "waiting@letu.edu"
        code = accounts.new_code(email, accounts.PURPOSE_VERIFY)
        called = []
        with self.assertRaisesRegex(accounts.AccountError, "expired"):
            accounts.check_code(email, code, accounts.PURPOSE_RESET,
                                before_consume=lambda: called.append(True))
        self.assertEqual(called, [])
        accounts.check_code(email, code, accounts.PURPOSE_VERIFY)

    def test_concurrent_checks_run_the_change_only_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        email = "waiting@letu.edu"
        code = accounts.new_code(email, accounts.PURPOSE_RESET)
        barrier = threading.Barrier(2)
        derive = accounts._derive
        called = []

        def meet(*args):
            hashed = derive(*args)
            barrier.wait(timeout=5)
            return hashed

        def spend():
            try:
                accounts.check_code(email, code, accounts.PURPOSE_RESET,
                                    before_consume=lambda: called.append(True))
                return True
            except accounts.AccountError:
                return False

        with patch.object(accounts, "_derive", side_effect=meet):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: spend(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(called, [True])


class TheStoreItself(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waam-acct-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = accounts.Store(self.tmp / "accounts.json")

    def test_the_password_is_never_written_anywhere(self):
        secret = "a memorable pass phrase"
        self.store.register("a@letu.edu", secret)
        raw = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        self.assertIn("scrypt hashes, no", raw)
        rec = json.loads(raw)["accounts"][0]
        self.assertEqual(rec["n"], accounts.SCRYPT_N)
        self.assertNotEqual(rec["salt"], "")

    def test_two_accounts_with_one_password_get_different_hashes(self):


        a = self.store.register("a@letu.edu", "the same pass phrase")
        b = self.store.register("b@letu.edu", "the same pass phrase")
        self.assertNotEqual(a.salt, b.salt)
        self.assertNotEqual(a.hash, b.hash)

    def test_the_cap_applies_without_a_config_to_read(self):


        with patch.object(accounts, "MAX_ACCOUNTS", 1):
            self.store.register("a@letu.edu", "the pass phrase here")
            with self.assertRaisesRegex(accounts.AccountError, "not accepting"):
                self.store.register("b@letu.edu", "the pass phrase here")
            self.store.register("b@letu.edu", "the pass phrase here", limit=2)

    def test_round_trip(self):
        self.store.register("a@letu.edu", "the pass phrase here")
        got = self.store.authenticate("A@LETU.edu", "the pass phrase here")
        self.assertEqual(got.email, "a@letu.edu")
        with self.assertRaises(accounts.AccountError):
            self.store.authenticate("a@letu.edu", "the wrong phrase here")

    def test_an_unreadable_store_refuses_rather_than_falling_open(self):


        self.store.path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(accounts.AccountError):
            self.store.get("a@letu.edu")
        with self.assertRaises(accounts.AccountError):
            self.store.register("a@letu.edu", "the pass phrase here")

    def test_one_corrupt_record_does_not_lock_everyone_out(self):
        self.store.register("good@letu.edu", "the pass phrase here")
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        raw["accounts"].append({"email": "broken@letu.edu"})
        self.store.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertIsNotNone(self.store.get("good@letu.edu"))
        self.assertIsNone(self.store.get("broken@letu.edu"))

    def test_the_owner_can_reset_a_forgotten_password(self):
        self.store.register("a@letu.edu", "the old pass phrase")
        self.store.reset_password("a@letu.edu", "the new pass phrase")
        with self.assertRaises(accounts.AccountError):
            self.store.authenticate("a@letu.edu", "the old pass phrase")
        self.assertIsNotNone(self.store.authenticate("a@letu.edu",
                                                     "the new pass phrase"))

    def test_a_weak_stored_hash_is_upgraded_on_the_next_sign_in(self):


        self.store.register("a@letu.edu", "the pass phrase here")
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        salt, digest = accounts.hash_password("the pass phrase here")
        raw["accounts"][0].update(n=2 ** 12, salt=salt, hash=accounts._derive(
            "the pass phrase here", bytes.fromhex(salt), 2 ** 12,
            accounts.SCRYPT_R, accounts.SCRYPT_P).hex())
        self.store.path.write_text(json.dumps(raw), encoding="utf-8")

        weak = self.store.get("a@letu.edu")
        self.assertTrue(accounts.needs_rehash(weak))
        self.store.authenticate("a@letu.edu", "the pass phrase here")
        self.assertFalse(accounts.needs_rehash(self.store.get("a@letu.edu")))

    def test_deleting_is_not_revoking(self):


        self.store.register("a@letu.edu", "the pass phrase here")
        self.assertTrue(self.store.delete("a@letu.edu"))
        self.assertFalse(self.store.delete("a@letu.edu"))
        self.store.register("a@letu.edu", "a different phrase")


if __name__ == "__main__":
    unittest.main()
