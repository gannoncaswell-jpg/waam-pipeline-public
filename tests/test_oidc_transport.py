import io
import sys
import unittest
import urllib.parse
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import oidc


def reply(request, status, body, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, request.full_url, status)
    response.msg = "OK" if status == 200 else "Redirect"
    return response


class TestTokenTransport(unittest.TestCase):
    def setUp(self):
        cached_opener = patch.object(urllib.request, "_opener", None)
        cached_opener.start()
        self.addCleanup(cached_opener.stop)
        self.cfg = oidc.AuthConfig(
            provider="oidc", client_id="example-client",
            client_secret="example-secret&=", public_url="https://workbench.example",
            token_url="https://provider.example/token")

    def test_https_form_and_json_exchange(self):
        requests = []

        def send(_handler, request):
            requests.append(request)
            return reply(request, 200, b'{"id_token": "example-token"}')

        with patch.object(urllib.request.HTTPSHandler, "https_open", send):
            token = oidc.exchange_code(self.cfg, "example-code", "example-verifier")
        self.assertEqual(token, "example-token")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, self.cfg.token_endpoint)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.timeout, 15)
        self.assertEqual(urllib.parse.parse_qs(request.data.decode("ascii")), {
            "grant_type": ["authorization_code"], "code": ["example-code"],
            "redirect_uri": [self.cfg.redirect_uri], "client_id": [self.cfg.client_id],
            "client_secret": [self.cfg.client_secret], "code_verifier": ["example-verifier"],
        })

    def test_initial_url_must_be_absolute_https(self):
        for url in ("http://provider.example/token", "ftp://provider.example/token",
                    "file:///token", "data:application/json,{}", "/token",
                    "//provider.example/token", "https:///token"):
            with self.subTest(url=url), patch.object(
                    urllib.request.OpenerDirector, "open",
                    side_effect=AssertionError("transport reached")):
                with self.assertRaises(ValueError):
                    oidc._post_form(url, {"client_secret": "example-secret"})

    def test_token_redirects_never_reach_a_second_endpoint(self):
        destinations = ("https://provider.example/canonical-token",
                        "https://other.example/token", "http://other.example/token",
                        "ftp://other.example/token", "file:///token")
        for status in (301, 302, 303, 307, 308):
            for destination in destinations:
                with self.subTest(status=status, destination=destination):
                    requests = []

                    def send(_handler, request, status=status,
                             destination=destination, requests=requests):
                        requests.append(request.full_url)
                        if request.full_url == self.cfg.token_endpoint:
                            return reply(request, status, b"", destination)
                        return reply(request, 200, b'{"id_token": "redirected-token"}')

                    with patch.object(urllib.request.HTTPSHandler, "https_open", send), \
                            patch.object(urllib.request.HTTPHandler, "http_open", send), \
                            patch.object(urllib.request.FTPHandler, "ftp_open", send), \
                            patch.object(urllib.request.FileHandler, "file_open", send):
                        with self.assertRaises(oidc.AuthError) as caught:
                            oidc.exchange_code(self.cfg, "example-code", "example-verifier")
                    self.assertEqual(requests, [self.cfg.token_endpoint])
                    self.assertEqual(str(caught.exception),
                                     f"the provider refused the sign-in (HTTP {status})")


if __name__ == "__main__":
    unittest.main()
