import base64
import hashlib
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import desktop_release, server
from tests.test_auth import GOOGLE_CFG, _AuthServerCase, http
from tests.test_desktop_release import isolate_pinned_key
from tests._support import IsolatedServerCase

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    Ed25519PrivateKey = None


class TestDesktopDownloadRoutes(_AuthServerCase):
    CONFIG = GOOGLE_CFG

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.iso.root)
        self.addCleanup(temporary.cleanup)
        self.downloads = Path(temporary.name) / "downloads"
        environment = mock.patch.dict(os.environ, {"WAAM_DOWNLOADS_DIR": str(self.downloads)})
        environment.start()
        self.addCleanup(environment.stop)
        isolate_pinned_key(self, temporary.name)
        desktop_release._HASH_CACHE.clear()
        self.addCleanup(desktop_release._HASH_CACHE.clear)
        sid = self.sign_in_ok(email="operator@letu.edu", email_verified=True)
        self.account = self.cookie(sid)

    def publish(self, body=b"desktop" * 20000, version="2.4.0", suffix=".dmg", signed=False):
        source = self.iso.root / f"WAAM-Studio-{version}{suffix}"
        source.write_bytes(body)
        signature = public_key = None
        if signed:
            if Ed25519PrivateKey is None:
                self.skipTest("cryptography is required to sign a test release")
            private = Ed25519PrivateKey.generate()
            public_key = private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            message = desktop_release.release_message(
                desktop_release.platform_for(source.name), version, len(body),
                hashlib.sha256(body).hexdigest())
            signature = base64.b64encode(private.sign(message)).decode("ascii")
        return body, desktop_release.publish(source, version, notes="For the team",
                                             signature=signature, public_key=public_key)

    @staticmethod
    def listed(key, release):
        listed = {
            "id": key,
            **{field: release[field] for field in (
                "version", "size", "sha256", "platform", "signed",
                "notarized", "published_at", "notes",
            )},
            "url": "/download/desktop/" + key,
        }
        if "signature" in release:
            listed["signature"] = release["signature"]
        return listed

    def test_signed_release_lists_its_signature_beside_an_unsigned_one(self):
        _mac_body, mac = self.publish()
        windows_body, windows = self.publish(body=b"signed installer", version="0.2.0",
                                             suffix=".exe", signed=True)
        signature = windows["signature"]
        code, raw, _ = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(code, 200)
        releases = json.loads(raw)["releases"]
        self.assertEqual(releases, [self.listed("macos-arm64", mac),
                                    self.listed("windows-x64", windows)])
        self.assertNotIn("signature", releases[0])
        self.assertEqual(releases[1]["signature"], signature)
        self.assertEqual(set(releases[1]) - set(releases[0]), {"signature"})
        code, downloaded, _ = http(self.base + "/download/desktop/windows-x64",
                                   headers=self.account)
        self.assertEqual((code, downloaded), (200, windows_body))

    def test_signed_in_metadata_and_download_stream_exact_bytes_with_headers_and_log(self):
        body, release = self.publish()
        self.assertTrue(self.account["Cookie"].startswith(server.SESSION_COOKIE + "="))
        code, _, _ = http(self.base + "/api/jbi", headers=self.account)
        self.assertNotIn(code, (401, 403))
        code, raw, headers = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(code, 200)
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        metadata = json.loads(raw)
        self.assertEqual(metadata, {"available": True,
                                    "releases": [self.listed("macos-arm64", release)]})

        code, downloaded, headers = http(self.base + "/download/desktop/macos-arm64",
                                         headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(downloaded, body)
        self.assertEqual(headers.get("Content-Type"), "application/x-apple-diskimage")
        self.assertEqual(headers.get("Content-Length"), str(len(body)))
        self.assertEqual(headers.get("Content-Disposition"),
                         f'attachment; filename="{release["file"]}"')
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        rows = [json.loads(line) for line in self.iso.access_log.read_text(encoding="utf-8").splitlines()]
        event = [row for row in rows if row.get("outcome") == "desktop-download"][-1]
        self.assertEqual(event["email"], "operator@letu.edu")
        self.assertEqual(event["version"], release["version"])
        self.assertEqual(event["path"], "/download/desktop/macos-arm64")


        code, downloaded, headers = http(self.base + "/download/desktop", headers=self.account)
        self.assertEqual((code, downloaded), (200, body))

    def test_windows_installer_is_listed_and_streamed_beside_the_mac_build(self):
        mac_body, mac = self.publish()
        windows_body, windows = self.publish(body=b"installer" * 9000, version="2.4.1",
                                             suffix=".exe")
        code, raw, _ = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"available": True, "releases": [
            self.listed("macos-arm64", mac), self.listed("windows-x64", windows)]})
        code, downloaded, headers = http(self.base + "/download/desktop/windows-x64",
                                         headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(downloaded, windows_body)
        self.assertEqual(headers.get("Content-Type"), "application/octet-stream")
        self.assertEqual(headers.get("Content-Length"), str(len(windows_body)))
        self.assertEqual(headers.get("Content-Disposition"),
                         f'attachment; filename="{windows["file"]}"')
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        code, downloaded, _ = http(self.base + "/download/desktop", headers=self.account)
        self.assertEqual((code, downloaded), (200, mac_body))

    def test_only_published_known_platforms_download(self):
        _body, windows = self.publish(body=b"installer only", suffix=".exe")
        code, raw, _ = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(json.loads(raw), {"available": True,
                                           "releases": [self.listed("windows-x64", windows)]})
        for path in ("/download/desktop", "/download/desktop/macos-arm64",
                     "/download/desktop/", "/download/desktop/linux-x64",
                     "/download/desktop/windows-x64/extra", "/download/desktop/..%2Fdesktop.json"):
            with self.subTest(path=path):
                code, body, headers = http(self.base + path, headers=self.account)
                self.assertEqual(code, 404)
                self.assertIn(b"No desktop build has been published", body)
                self.assertIsNone(headers.get("Content-Disposition"))

    def test_absent_release_has_clear_metadata_and_download_responses(self):
        code, body, headers = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"available": False, "releases": []})
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        code, body, headers = http(self.base + "/download/desktop", headers=self.account)
        self.assertEqual(code, 404)
        self.assertIn(b"No desktop build has been published", body)
        self.assertIsNone(headers.get("Content-Disposition"))
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_symlinked_release_outside_downloads_is_refused_as_unpublished(self):
        body, release = self.publish(body=b"outside target")
        asset = self.downloads / release["file"]
        outside = self.iso.root / "outside-release.dmg"
        outside.write_bytes(body)
        info = asset.stat()
        os.utime(outside, ns=(info.st_atime_ns, info.st_mtime_ns))
        asset.unlink()
        try:
            asset.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")

        code, raw, _ = http(self.base + "/api/desktop-release", headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"available": False, "releases": []})
        code, raw, headers = http(self.base + "/download/desktop", headers=self.account)
        self.assertEqual(code, 404)
        self.assertIn(b"No desktop build has been published", raw)
        self.assertIsNone(headers.get("Content-Disposition"))

    def test_guests_are_denied_by_the_common_gate(self):
        self.assertNotIn("/api/desktop-release", server.GUEST_ALLOWED)
        self.assertNotIn("/download/desktop", server.GUEST_ALLOWED)
        self.publish(body=b"installer", suffix=".exe")
        guest = server._new_guest()
        self.addCleanup(server.forget_guests)
        headers = {"Cookie": f"{server.GUEST_COOKIE}={guest}"}
        for path in ("/api/jbi", "/api/desktop-release", "/download/desktop",
                     "/download/desktop/windows-x64"):
            with self.subTest(path=path):
                code, body, response_headers = http(self.base + path, headers=headers)
                self.assertEqual(code, 403)
                self.assertFalse(json.loads(body)["ok"])
                self.assertIsNone(response_headers.get("Content-Disposition"))
                self.assertIn("no-store", response_headers.get("Cache-Control", ""))

    def test_stream_uses_the_descriptor_that_was_verified_before_a_concurrent_publish(self):
        first_body, first = self.publish(body=b"old bytes", version="1.0.0")
        real_open = desktop_release.open_release
        replacement = self.iso.root / "WAAM-Studio-race.dmg"
        replacement.write_bytes(b"new bytes are deliberately longer")

        def open_then_publish(*args, **kwargs):
            opened = real_open(*args, **kwargs)
            self.assertIsNotNone(opened)
            desktop_release.publish(replacement, "2.0.0")
            return opened

        with mock.patch.object(desktop_release, "open_release", side_effect=open_then_publish):
            code, body, headers = http(self.base + "/download/desktop/macos-arm64",
                                       headers=self.account)
        self.assertEqual(code, 200)
        self.assertEqual(body, first_body)
        self.assertEqual(headers.get("Content-Length"), str(first["size"]))
        self.assertEqual(headers.get("Content-Disposition"),
                         f'attachment; filename="{first["file"]}"')
        self.assertEqual(desktop_release.load()["version"], "2.0.0")


class TestDesktopDownloadTokenAccess(IsolatedServerCase):
    TOKENS = {"live-workspace-token": "token-workspace"}
    TOKEN_GATE = True

    def setUp(self):
        server._TOKENS = dict(self.TOKENS)
        temporary = tempfile.TemporaryDirectory(dir=self.iso.root)
        self.addCleanup(temporary.cleanup)
        self.downloads = Path(temporary.name) / "downloads"
        environment = mock.patch.dict(os.environ, {"WAAM_DOWNLOADS_DIR": str(self.downloads)})
        environment.start()
        self.addCleanup(environment.stop)
        desktop_release._HASH_CACHE.clear()
        self.addCleanup(desktop_release._HASH_CACHE.clear)
        source = self.iso.root / "WAAM-Studio-token.dmg"
        self.body = b"token download"
        source.write_bytes(self.body)
        self.release = desktop_release.publish(source, "0.9.0")

    def request_set(self, suffix="", headers=None):
        return {
            path: http(self.base + path + suffix, headers=headers)
            for path in ("/api/jbi", "/api/desktop-release", "/download/desktop",
                         "/download/desktop/macos-arm64")
        }

    def assert_refused(self, responses, status):
        for path, (code, _body, headers) in responses.items():
            with self.subTest(path=path):
                self.assertEqual(code, status)
                self.assertIsNone(headers.get("Content-Disposition"))

    def assert_accepted(self, responses):
        self.assertNotIn(responses["/api/jbi"][0], (401, 403))
        self.assertEqual(responses["/api/desktop-release"][0], 200)
        for path in ("/download/desktop", "/download/desktop/macos-arm64"):
            self.assertEqual(responses[path][0], 200)
            self.assertEqual(responses[path][1], self.body)

    def test_jbi_and_desktop_routes_share_anonymous_token_and_revocation_decisions(self):
        self.assert_refused(self.request_set(), 401)
        credentials = (
            ("?k=live-workspace-token", None),
            ("", {"X-Waam-Token": "live-workspace-token"}),
            ("", {"Cookie": "waam=live-workspace-token"}),
        )
        for suffix, headers in credentials:
            with self.subTest(suffix=suffix, headers=headers):
                self.assert_accepted(self.request_set(suffix, headers))

        server._TOKENS.clear()
        for suffix, headers in credentials:
            with self.subTest(revoked=True, suffix=suffix, headers=headers):
                self.assert_refused(self.request_set(suffix, headers), 401)

    def test_token_link_sets_existing_cookie_and_plain_download_url_uses_it(self):
        code, _, headers = http(self.base + "/?k=live-workspace-token")
        self.assertEqual(code, 200)
        jar = SimpleCookie(headers.get("Set-Cookie", ""))
        self.assertIn("waam", jar)
        self.assertEqual(jar["waam"].value, "live-workspace-token")
        cookie = {"Cookie": "waam=" + jar["waam"].value}
        self.assert_accepted(self.request_set(headers=cookie))

    def test_activity_log_redacts_auth_query_values_for_every_request(self):
        secret_path = ("/api/desktop-release?k=live-workspace-token"
                       "&access_token=another-secret&session=browser-session"
                       "&waam_sid=signed-session&ordinary=visible")
        code, _, _ = http(self.base + secret_path)
        self.assertEqual(code, 200)
        rows = [json.loads(line) for line in self.iso.activity_log.read_text(
            encoding="utf-8").splitlines()]
        logged = rows[-1]["path"]
        self.assertIn("ordinary=visible", logged)
        self.assertIn("%5Bredacted%5D", logged)
        for secret in ("live-workspace-token", "another-secret", "browser-session",
                       "signed-session"):
            self.assertNotIn(secret, logged)
