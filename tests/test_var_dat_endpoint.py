import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests._support import IsolatedServerCase

RIN3 = ROOT / "docs" / "cell-data" / "var-exports" / "VAR_AR1440_rin3.DAT"


class TestVarDatEndpoint(IsolatedServerCase):
    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            r = urllib.request.urlopen(req)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_fills_four_corner_pulses_from_the_real_export(self):
        code, obj = self.post("/api/var-dat?base=40&count=4", RIN3.read_bytes())
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(len(obj["records"]), 4)
        self.assertEqual(obj["records"][0], [-20184, 16327, -76687, 52, 21056, -14])

    def test_unused_slot_refused_by_name(self):
        code, obj = self.post("/api/var-dat?base=2&count=1", RIN3.read_bytes())
        self.assertEqual(code, 400)
        self.assertFalse(obj["ok"])
        self.assertIn("P002", obj["error"])

    def test_flags_return_hits_and_null_misses(self):
        from tests.test_layer_sense_silicon import var_text
        body = var_text([[1, 2, 3, 4, 5, 6], None], 120, hits=[1, 0], template=RIN3).encode("latin-1")
        code, obj = self.post("/api/var-dat?base=120&count=2&flags=1&flag_base=80", body)
        self.assertEqual(code, 200, obj)
        self.assertEqual((obj["records"], obj["hits"]), ([[1, 2, 3, 4, 5, 6], None], [1, 0]))
        code, obj = self.post("/api/var-dat?base=120&count=2", body)
        self.assertEqual(code, 400)
        self.assertNotIn("hits", obj)

    def test_empty_upload_refused(self):
        code, obj = self.post("/api/var-dat", b"")
        self.assertEqual(code, 400)
        self.assertFalse(obj["ok"])


if __name__ == "__main__":
    unittest.main()
