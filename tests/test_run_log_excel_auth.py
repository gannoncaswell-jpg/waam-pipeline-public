import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import run_log_excel_auth as auth

EMAIL = "owner@example.invalid"
SCOPE = "a" * 64
OTHER_SCOPE = "b" * 64


def basic(email, password):
    return "Basic " + base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")


class TestExcelCredentials(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / auth.FILE_NAME

    def check(self, email, password, scope=SCOPE):
        return auth.authenticate(self.path, basic(email, password), scope_for_email=lambda email: scope)

    def test_random_token_is_returned_once_and_only_digest_is_persisted(self):
        self.assertEqual(auth.status(self.path, EMAIL, scope=SCOPE), {"enabled": False, "created_at": None})
        self.assertFalse(self.path.exists())
        result = auth.create(self.path, " OWNER@EXAMPLE.INVALID ", scope=SCOPE)
        self.assertRegex(result["password"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(self.check("OWNER@EXAMPLE.INVALID", result["password"]), EMAIL)
        body = self.path.read_text(encoding="utf-8")
        self.assertNotIn(result["password"], body)
        record = json.loads(body)["credentials"][EMAIL]
        self.assertEqual(record["digest"], hashlib.sha256(result["password"].encode("ascii")).hexdigest())
        self.assertEqual(set(record), {"digest", "scope", "created_at"})
        status = auth.status(self.path, EMAIL, scope=SCOPE)
        self.assertEqual(status, {"enabled": True, "created_at": result["created_at"]})
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rotation_and_revoke_do_not_change_other_accounts(self):
        first = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        other = auth.create(self.path, "other@example.invalid", scope=SCOPE)["password"]
        second = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        self.assertNotEqual(first, second)
        self.assertIsNone(self.check(EMAIL, first))
        self.assertEqual(self.check(EMAIL, second), EMAIL)
        self.assertEqual(auth.revoke(self.path, EMAIL), {"enabled": False, "created_at": None})
        self.assertIsNone(self.check(EMAIL, second))
        self.assertEqual(self.check("other@example.invalid", other), "other@example.invalid")
        self.assertEqual(auth.revoke(self.path, EMAIL), {"enabled": False, "created_at": None})

    def test_authority_change_password_reset_and_current_denial_refuse(self):
        password = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        self.assertIsNone(self.check(EMAIL, password, OTHER_SCOPE))
        self.assertIsNone(self.check(EMAIL, password, None))
        self.assertEqual(auth.status(self.path, EMAIL, scope=OTHER_SCOPE), {"enabled": False, "created_at": None})
        current = auth.create(self.path, EMAIL, scope=OTHER_SCOPE)["password"]
        self.assertEqual(self.check(EMAIL, current, OTHER_SCOPE), EMAIL)
        self.assertIsNone(self.check(EMAIL, password, OTHER_SCOPE))

    def test_wrong_and_unknown_tokens_use_digest_comparison_before_policy(self):
        password = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        callback = Mock(return_value=SCOPE)
        with patch.object(auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest) as compare:
            self.assertIsNone(auth.authenticate(self.path, basic("unknown@example.invalid", password),
                                                scope_for_email=callback))
            compare.assert_called_once()
        callback.assert_not_called()
        wrong = ("A" if password[0] != "A" else "B") + password[1:]
        self.assertIsNone(auth.authenticate(self.path, basic(EMAIL, wrong), scope_for_email=callback))
        callback.assert_not_called()

    def test_malformed_and_oversize_headers_refuse_without_reading_storage(self):
        raw = base64.b64encode(b"owner@example.invalid:no-colon-in-token:extra").decode("ascii")
        cases = (None, "", "Bearer abc", "Basic", "Basic ", "Basic !!", "Basic " + raw,
                 "Basic " + base64.b64encode(b"\xff:wrong").decode("ascii"),
                 "Basic " + base64.b64encode(b"without-colon").decode("ascii"),
                 "Basic " + "A" * auth.MAX_HEADER_BYTES, "Basic é", "Basic abc\n")
        with patch.object(auth, "_load") as load:
            for header in cases:
                with self.subTest(header=header):
                    self.assertIsNone(auth.authenticate(self.path, header, scope_for_email=lambda email: SCOPE))
            load.assert_not_called()

    def test_corrupt_or_unsupported_file_fails_closed_without_overwriting(self):
        password = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        cases = (b"not json", b'{"version":2,"credentials":{}}',
                 b'{"version":1,"version":1,"credentials":{}}',
                 b'{"version":1,"credentials":{"bad@example.invalid":{"digest":"secret-value"}}}')
        for body in cases:
            self.path.write_bytes(body)
            for action in (lambda: auth.status(self.path, EMAIL, scope=SCOPE),
                           lambda: auth.create(self.path, EMAIL, scope=SCOPE),
                           lambda: auth.revoke(self.path, EMAIL),
                           lambda: self.check(EMAIL, password)):
                with self.assertRaises(auth.CredentialError) as caught:
                    action()
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(self.path.read_bytes(), body)

    def test_failed_atomic_replace_keeps_previous_credential_and_cleans_pending(self):
        password = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        original = self.path.read_bytes()
        with patch.object(auth.os, "replace", side_effect=OSError("sensitive-diagnostic")):
            with self.assertRaises(auth.CredentialError) as caught:
                auth.create(self.path, EMAIL, scope=SCOPE)
        self.assertNotIn("sensitive-diagnostic", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.check(EMAIL, password), EMAIL)
        self.assertEqual(list(self.root.glob(".waam-excel-pending-*")), [])

    def test_concurrent_rotations_keep_other_accounts_and_one_token_each(self):
        emails = [f"person{number}@example.invalid" for number in range(12)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            credentials = list(pool.map(lambda email: auth.create(self.path, email, scope=SCOPE), emails))
        for email, credential in zip(emails, credentials):
            self.assertEqual(self.check(email, credential["password"]), email)
        with ThreadPoolExecutor(max_workers=4) as pool:
            rotations = list(pool.map(lambda _: auth.create(self.path, EMAIL, scope=SCOPE), range(8)))
        self.assertEqual(sum(self.check(EMAIL, item["password"]) == EMAIL for item in rotations), 1)

    def test_hardlinked_files_are_refused_for_reads_and_mutations(self):
        password = auth.create(self.path, EMAIL, scope=SCOPE)["password"]
        alias = self.root / "alias"
        try:
            os.link(self.path, alias)
        except OSError as error:
            self.skipTest(f"hard links unavailable: {error}")
        original = self.path.read_bytes()
        for action in (lambda: self.check(EMAIL, password),
                       lambda: auth.create(self.path, EMAIL, scope=SCOPE),
                       lambda: auth.revoke(self.path, EMAIL)):
            with self.assertRaises(auth.CredentialError):
                action()
        self.assertEqual(alias.read_bytes(), original)

    def test_linked_directory_and_file_are_refused(self):
        actual = self.root / "actual"
        actual.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(actual, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaises(auth.CredentialError):
            auth.create(link / auth.FILE_NAME, EMAIL, scope=SCOPE)
        self.path.symlink_to(actual / "missing")
        with self.assertRaises(auth.CredentialError):
            auth.create(self.path, EMAIL, scope=SCOPE)
        self.assertEqual(list(actual.iterdir()), [])

    def test_public_posix_storage_and_capacity_are_refused(self):
        auth.create(self.path, EMAIL, scope=SCOPE)
        if os.name == "posix":
            self.path.chmod(0o644)
            with self.assertRaises(auth.CredentialError):
                auth.status(self.path, EMAIL, scope=SCOPE)
            self.path.chmod(0o600)
        with patch.object(auth, "MAX_CREDENTIALS", 1):
            with self.assertRaises(auth.CredentialError):
                auth.create(self.path, "second@example.invalid", scope=SCOPE)
            self.assertTrue(auth.create(self.path, EMAIL, scope=SCOPE)["enabled"])

    def test_invalid_email_scope_and_utf16_overflow_are_rejected(self):
        for email in (None, "no-email", "a:b@example.invalid", "a\x00@example.invalid",
                      "😀" * 160 + "@example.invalid"):
            with self.assertRaises(ValueError):
                auth.create(self.path, email, scope=SCOPE)
        for scope in (None, "", "secret", "A" * 64):
            with self.assertRaises(ValueError):
                auth.create(self.path, EMAIL, scope=scope)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
