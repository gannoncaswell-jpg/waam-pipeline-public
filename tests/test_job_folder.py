from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc, emit, touch
from waam_pipeline.backends.jbi import _render_search_jbi
from waam_pipeline.backends.jbi_read import read_jbi

EXPORTS = ROOT / "docs" / "cell-data" / "controller-backup" / "industrial-text-export"
DATE = "2026/09/25 12:00"


def parse(text):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "JOB.JBI"
        path.write_bytes(text.encode("ascii"))
        return read_jbi(path)


class TestJobFolder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = touch.line_probe_plan((1021.7, -232.5), (1019.4, -130.9), 179.0, stations=2)
        cls.solve = staticmethod(cell_lc.pulse_solver(np.zeros(3)))

    def job(self, **kw):
        return _render_search_jbi(self.plan, self.solve, name="FOLDER_T", date=DATE, **kw)

    def test_the_folder_line_sits_where_the_controller_writes_it(self):

        real = (EXPORTS / "BASIC-W-ARC-GAJ.JBI").read_bytes().decode("ascii").splitlines()[:4]
        ours = self.job(folder="WAAM").split("\r\n")[:4]
        self.assertEqual([line.split(" ")[0] for line in ours], [line.split(" ")[0] for line in real])
        self.assertEqual(ours, ["/JOB", "//NAME FOLDER_T", "///FOLDERNAME WAAM", "//POS"])
        self.assertEqual(parse(self.job(folder="WAAM")).folder, "WAAM")

    def test_no_folder_leaves_the_job_unchanged(self):
        plain = self.job()
        self.assertNotIn("FOLDERNAME", plain)
        for none in (None, "", "NONE"):
            with self.subTest(folder=none):
                self.assertEqual(self.job(folder=none), plain)
        self.assertEqual(parse(plain).folder, "")
        lines = plain.split("\r\n")
        self.assertEqual(self.job(folder="WAAM").split("\r\n"), lines[:2] + ["///FOLDERNAME WAAM"] + lines[2:])

    def test_the_parser_reads_real_exports(self):
        self.assertEqual(read_jbi(EXPORTS / "BASIC-W-ARC-GAJ.JBI").folder, "GABEJ-YRC1000")
        self.assertEqual(read_jbi(ROOT / "docs" / "cell-data" / "jobs" / "CLEAN_R1.JBI").folder, "")

    def test_bad_folders_are_refused(self):
        for bad in ("WAAM FOLDER", "A" * 33, "../x", "WA.AM", "WÄAM", 5):
            with self.subTest(folder=bad), self.assertRaisesRegex(ValueError, "job folder"):
                self.job(folder=bad)

    def test_the_round_trip_reads_the_folder_back(self):
        text = self.job(folder="WAAM")
        self.assertEqual(emit._validate(text, "FOLDER_T", "WAAM")["name"], "FOLDER_T")
        for asked in (None, "WAAM2"):
            with self.subTest(asked=asked), self.assertRaisesRegex(emit.EmitRefused, "folder='WAAM'"):
                emit._validate(text, "FOLDER_T", asked)


if __name__ == "__main__":
    unittest.main()
