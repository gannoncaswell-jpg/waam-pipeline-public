import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, var_dat

RIN3 = ROOT / "docs" / "cell-data" / "var-exports" / "VAR_AR1440_rin3.DAT"


EXPECT_PULSES = {
    40: [-20184, 16327, -76687, 52, 21056, -14],
    41: [-18308, 25640, -62625, 51, 18385, -17],
    42: [-16748, 34791, -47689, 56, 14989, -21],
    43: [-12555, 33827, -49365, 55, 15417, -19],
    44: [-13744, 24573, -64344, 49, 18764, -16],
    45: [-15175, 15172, -78507, 52, 21442, -14],
    46: [-10049, 14328, -79800, 52, 21693, -15],
    47: [-9089, 24007, -65605, 50, 19208, -15],
    48: [-8298, 33251, -50575, 55, 15820, -19],
}
DOC_XYZ = {
    40: (939.87, -235.63, 144.79), 41: (1039.89, -235.62, 144.91),
    42: (1139.90, -235.63, 144.01), 43: (1139.94, -175.62, 143.81),
    44: (1039.91, -175.65, 144.69), 45: (939.93, -175.64, 143.81),
    46: (939.94, -115.63, 143.19), 47: (1039.96, -115.62, 142.59),
    48: (1139.99, -115.64, 142.43),
}


class TestRealExport(unittest.TestCase):
    def test_probe_records_match_the_file_verbatim(self):
        records = var_dat.probe_records(RIN3, base=40, count=9)
        self.assertEqual(records, [EXPECT_PULSES[i] for i in range(40, 49)])

    def test_records_decode_to_the_validation_table(self):
        for i, rec in zip(range(40, 49), var_dat.probe_records(RIN3, 40, 9)):
            xyz = cell_lc.tcp_from_pulses(rec)
            np.testing.assert_allclose(xyz, DOC_XYZ[i], atol=0.05)

    def test_reads_straight_into_the_touch_fit(self):

        records = var_dat.probe_records(RIN3, 40, 9)
        surface = cell_lc.fit_work_surface("var_dat_rin3", records)
        self.assertLess(surface.residual_mm, 1.0)
        self.assertLess(surface.tilt_deg, 2.0)

    def test_unused_and_out_of_range_slots_are_named(self):
        pulses = var_dat.parse_position_pulses(RIN3)
        self.assertIn(0, pulses)
        self.assertEqual(pulses[0], [0, 0, 0, 0, 0, 0])
        self.assertNotIn(2, pulses)
        with self.assertRaisesRegex(ValueError, "P002"):
            var_dat.probe_records(RIN3, base=2, count=1)
        with self.assertRaisesRegex(ValueError, "P300"):
            var_dat.probe_records(RIN3, base=300, count=1)


class TestParsingRules(unittest.TestCase):
    HEAD = "//VAR\r\n///B\r\n0,0,0,0,0,0,0,0,0,0\r\n///P\r\n"

    def test_no_position_section_raises(self):
        with self.assertRaisesRegex(ValueError, "///P"):
            var_dat.parse_position_pulses("//VAR\r\n///B\r\n0\r\n")

    def test_cartesian_frame_is_skipped_not_guessed(self):
        text = self.HEAD + '"ROBOT"0,0,0,000000000000000000000000,0,0,1,2,3,4,5,6,0,0\r\n///BP\r\n'
        self.assertEqual(var_dat.parse_position_pulses(text), {})
        with self.assertRaisesRegex(ValueError, "P000"):
            var_dat.probe_records(text, 0, 1)

    def test_wrong_field_count_raises_rather_than_misreads(self):
        text = self.HEAD + '"PULSE"0,0,0,0,0,0,1,2,3,4,5,6\r\n'
        with self.assertRaisesRegex(ValueError, "14 PULSE fields"):
            var_dat.parse_position_pulses(text)

    def test_section_ends_at_next_header(self):
        text = self.HEAD + ('"PULSE"0,0,0,x,0,0,7,8,9,10,11,12,0,0\r\n'
                            '///BP\r\n"PULSE"0,0,0,x,0,0,99,99,99,99,99,99,0,0\r\n')
        pulses = var_dat.parse_position_pulses(text)
        self.assertEqual(pulses, {0: [7, 8, 9, 10, 11, 12]})

    def test_bad_base_and_count_are_refused(self):
        for bad in (dict(base=-1, count=1), dict(base=0, count=0), dict(base=0, count=-3)):
            with self.assertRaises(ValueError):
                var_dat.probe_records(RIN3, **bad)


if __name__ == "__main__":
    unittest.main()
