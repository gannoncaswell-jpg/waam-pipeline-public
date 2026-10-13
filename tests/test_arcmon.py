import csv
import io
from pathlib import Path
import re
import statistics
import sys
import tracemalloc
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcmon


class TestArcmon(unittest.TestCase):
    def test_committed_realtime_sample(self):
        body = (ROOT / "docs/cell-data/arcmon/1-20251103-181340.CSV").read_bytes()
        result = arcmon.parse("1-20251103-181340.CSV", body)
        self.assertEqual(result["kind"], "realtime-csv")
        self.assertEqual(result["date"], "2025/11/03 18:13:40")
        self.assertEqual(result["sampling_s"], 0.1)
        self.assertEqual(result["samples"], 300)
        self.assertEqual(result["channels"][1],
                         {"channel": "CH2", "attribute": "AIN2", "name": "CURRENT", "unit": "A"})
        rows = list(csv.reader(io.StringIO(body.decode())))
        start = rows.index(["//DATA"]) + 2
        on = [(float(row[1]), float(row[0])) for row in rows[start:]
              if len(row) == 11 and float(row[1]) > 10]
        self.assertEqual(len(on), 36)
        self.assertAlmostEqual(result["arc_on_s"], 3.6)
        for index, key in enumerate(("current", "voltage")):
            values = [row[index] for row in on]
            self.assertEqual(result[key], {"mean": statistics.fmean(values),
                                           "min": min(values), "max": max(values)})
        self.assertAlmostEqual(result["current"]["mean"], 146.49166666666667)
        self.assertAlmostEqual(result["voltage"]["mean"], 19.488888888888887)

    @staticmethod
    def synthetic(rows="999,20,21\r\n999,30,10\r\n999,25,41\r\n"):
        return ("//ARC_MONDATA\r\n//DATE,:2025/11/03 18:13:40\r\n//SAMPLING,0.1sec\r\n"
                "//CH8,AOT2,CURRENT,0,1,1,A\r\n//CH3,AIN1,VOLTAGE,0,1,1,V\r\n"
                "//CH6,AIN2,CURRENT,0,1,1,A\r\n//DATA\r\nCH8,CH3,CH6\r\n"
                + rows + "\r\n//ARC_MONDATAEND\r\n\r\n").encode()

    def test_channel_by_name_and_ain_not_position_or_output(self):
        result = arcmon.parse("trace.csv", self.synthetic())
        self.assertEqual(result["current"], {"mean": 31, "min": 21, "max": 41})
        self.assertEqual(result["voltage"], {"mean": 22.5, "min": 20, "max": 25})
        self.assertEqual(result["arc_on_s"], 0.2)

    def test_short_last_row_and_no_arc(self):
        result = arcmon.parse("trace.CSV", self.synthetic("999,20,0\r\n999,20\r\n"))
        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["arc_on_s"], 0)
        self.assertEqual(result["current"], {"mean": None, "min": None, "max": None})

    def test_row_limit(self):
        with self.assertRaisesRegex(ValueError, "100000"):
            arcmon.parse("trace.csv", self.synthetic("0,0,0\n" * 100_001))

    def test_malformed_csv_and_overflow_use_the_export_error(self):
        for rows in ("0," + "1" * (csv.field_size_limit() + 1) + ",30\n",
                     "0,20,1e308\n0,20,1e308\n"):
            with self.subTest(rows=rows[:40]):
                with self.assertRaisesRegex(ValueError, "not an arc monitor export"):
                    arcmon.parse("trace.csv", self.synthetic(rows))

    def test_readings_outside_the_physical_range_refuse_the_file(self):
        for rows, message in (("0,20,1e200\n", "CURRENT (CH6) reading 1e+200"), ("0,2000,30\n", "VOLTAGE (CH3)"),
                              ("1e13,20,30\n", "CURRENT (CH8) reading 1e+13")):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "not an arc monitor export: .*" + re.escape(message)):
                arcmon.parse("trace.csv", self.synthetic(rows))
        for sampling in ("1" + "0" * 306, "0.00001", "11"):
            body = self.synthetic().replace(b"0.1sec", sampling.encode() + b"sec")
            with self.subTest(sampling=sampling), self.assertRaisesRegex(ValueError, "sampling .* is outside"):
                arcmon.parse("trace.csv", body)
        for sampling in ("10", "0.0001"):
            self.assertEqual(arcmon.parse("trace.csv", self.synthetic().replace(b"0.1sec", sampling.encode() + b"sec"))
                             ["sampling_s"], float(sampling))
        body = (ROOT / "docs/cell-data/arcmon/1-20251103-181340.CSV").read_bytes()
        first = b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,20,300.5,1001,63"
        for row, channel in ((b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,20,300.5,1001,-1", "WELD LENGTH"),
                             (b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,20,300.5,1001,2e7", "WELD LENGTH"),
                             (b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,2e6,300.5,1001,63", "STEP NO."),
                             (b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,20,2e5,1001,63", "WELD SPEED"),
                             (b"0.0,0.0,0.0,0.0,-65.0,43.4,-86.0,20,300.5,-3,63", "WELD COND.NO")):
            with self.subTest(channel=channel, row=row), self.assertRaisesRegex(ValueError, channel):
                arcmon.parse("x.csv", body.replace(first, row, 1))

    def test_a_hostile_upload_is_refused_in_bounded_memory(self):
        body = b"//ARC_MONDATA\n" + b"00\n" * 700_000
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(ValueError, "rows before //DATA"):
                arcmon.parse("trace.csv", body)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 3 * len(body))
        for text, message in (("1," * 3000 + "1\n", "longer than 4096"), (",".join("1" * 65) + "\n", "64 fields")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                arcmon.parse("trace.csv", b"//ARC_MONDATA\n" + text.encode())
        many = self.synthetic().replace(b"CH8,CH3,CH6\r\n", ("CH8,CH3,CH6," + ",".join(f"CH{i}" for i in range(
            10, 80)) + "\r\n").encode())
        with self.assertRaisesRegex(ValueError, "not an arc monitor export"):
            arcmon.parse("trace.csv", many)

    def test_committed_arcmon_dat_and_nonzero_blocks(self):
        body = (ROOT / "docs/cell-data/controller-backup/yaskawa1-binary-backup/ARCMON.DAT").read_bytes()
        self.assertEqual(arcmon.parse("ARCMON.DAT", body), {"kind": "arcmon-dat", "blocks": []})
        changed = body.replace(b"0,0,0.0", b"1,2,3.0", 1)
        result = arcmon.parse("arcmon.dat", changed)
        self.assertEqual(result["blocks"], [{"file": 1, "values": [[1, 2, 3], [0, 0, 0, 0, 0, 0]]}])

    def test_other_files_and_malformed_exports_refused(self):
        for name, body in (("random.csv", b"a,b\n1,2\n"),
                           ("VAR.DAT", b"//VAR\n//POS\n"),
                           ("ARCMON.DAT", b"//ARCMON 1\n0,0,0\n"),
                           ("trace.csv", self.synthetic("0,nan,25\n")),
                           ("trace.csv", self.synthetic("0,20\n0,20,30\n"))):
            with self.subTest(name=name, body=body[:20]):
                with self.assertRaisesRegex(ValueError, "not an arc monitor export"):
                    arcmon.parse(name, body)


if __name__ == "__main__":
    unittest.main()
