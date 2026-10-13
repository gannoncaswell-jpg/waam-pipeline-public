import csv
import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams, bead_presets as bp

SEED = {
    "ER70S-6": [(81, 309, "STT", 140, 295, 9.525), (82, 21, "CV", 150, 320, 12.7),
                (83, 22, "Pulse", 175, 370, 19.05), (84, 274, "Rapid X", 225, 570, 19.05)],
    "ER308LSi": [(91, 348, "STT", 130, 275, 9.525), (92, 41, "CV", 150, 320, 12.7),
                 (93, 46, "Pulse", 175, 370, 19.05), (94, 284, "Rapid X", 200, 510, 19.05)],
}
GAS = {"ER70S-6": "90Ar/10CO2", "ER308LSi": "98Ar/2CO2"}


class TestSeedFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presets = bp.load_presets()

    def test_eight_nominal_presets_from_the_workbook(self):
        self.assertEqual(len(self.presets), 8)
        by_asf = {p.asf: p for p in self.presets}
        for material, rows in SEED.items():
            for asf, mode, family, wfs, travel, ctwd in rows:
                p = by_asf[asf]
                self.assertEqual((p.material, p.gas, p.r450_mode, p.process, p.wire_dia_in),
                                 (material, GAS[material], mode, family, 0.045))
                self.assertEqual((p.wfs_ipm, p.travel_mm_min, p.weld_ctwd_mm), (wfs, travel, ctwd))
                self.assertEqual(p.provenance, "NOMINAL")
                self.assertIn("NOMINAL", p.source)

    def test_bead_is_the_w_h_3_parabola_of_the_mass_balance_area(self):
        for p in self.presets:
            area = arcparams.wire_area_mm2(1.143) * p.wfs_ipm * 25.4 / p.travel_mm_min
            crown = math.sqrt(1.5 * area / 3.0)
            self.assertAlmostEqual(p.deposited_area_mm2, area, places=9)
            self.assertAlmostEqual(p.crown_height_mm, crown, places=4)
            self.assertAlmostEqual(p.bead_width_mm, 3.0 * crown, places=4)
            self.assertAlmostEqual(p.bead_height_mm, area / (3.0 * crown), places=4)
            self.assertAlmostEqual(p.bead_height_mm / p.crown_height_mm, 2 / 3, places=3)

    def test_steel_stt_numbers(self):
        stt = next(p for p in self.presets if p.asf == 81)
        self.assertEqual(stt.id, "steel-stt-asf81")
        self.assertAlmostEqual(stt.deposited_area_mm2, 12.369, places=3)
        self.assertEqual((stt.bead_width_mm, stt.bead_height_mm, stt.crown_height_mm),
                         (7.4605, 1.6579, 2.4868))


class TestLoaderValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = json.loads(bp.PRESETS_PATH.read_text(encoding="utf-8"))

    def load(self, data):
        path = Path(self.tmp.name) / "p.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return bp.load_presets(path)

    def broken(self, **change):
        data = json.loads(json.dumps(self.raw))
        data["presets"][0].update(change)
        return data

    def test_round_trip(self):
        path = Path(self.tmp.name) / "out.json"
        bp.dump_presets(bp.load_presets(), path)
        self.assertEqual(bp.load_presets(path), bp.load_presets())

    def test_refusals(self):
        cases = [dict(asf=81.0), dict(asf=True), dict(asf=0), dict(r450_mode=-1),
                 dict(provenance="GUESS"), dict(process="MIG"), dict(wire_dia_in=1.143),
                 dict(wire_dia_in=0.0625001), dict(wire_dia_in=0.019), dict(id="steel-stt-asf81\n"),
                 dict(bead_height_mm=3.0), dict(wfs_ipm=float("nan")), dict(travel_mm_min=0),
                 dict(source=" "), dict(id="Steel STT"), dict(extra=1)]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(self.broken(**change))
        missing = json.loads(json.dumps(self.raw))
        del missing["presets"][0]["crown_height_mm"]
        with self.assertRaises(ValueError):
            self.load(missing)
        dup = json.loads(json.dumps(self.raw))
        dup["presets"][1]["id"] = dup["presets"][0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            self.load(dup)
        with self.assertRaisesRegex(ValueError, "version"):
            self.load(dict(self.raw, version=2))
        with self.assertRaisesRegex(ValueError, "version"):
            self.load(dict(self.raw, version=True))

    def test_one_asf_cannot_mean_two_schedules(self):
        data = json.loads(json.dumps(self.raw))
        twin = dict(data["presets"][0], id="steel-stt-asf81-slow", travel_mm_min=250.0)
        data["presets"].append(twin)
        self.assertEqual(len(self.load(data)), 9)
        for field, value in (("process", "CV"), ("material", "ER308LSi"), ("wfs_ipm", 150.0)):
            data["presets"][-1] = dict(twin, **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"differ in {field}"):
                self.load(data)


def write_log(directory, rows):
    path = Path(directory) / "bead-log.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(bp.LOG_COLUMNS)
        writer.writerows(rows)
    return path


class TestBeadLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_kept_rows_average_per_asf_and_travel(self):
        path = write_log(self.tmp.name, [
            ["R1", "ER70S-6", "STT", "81", "nominal", "295", "140", "9.525", "7.0", "2.6", "11.5", "yes"],
            ["R2", "ER70S-6", "STT", "81", "nominal", "295", "140", "9.525", "7.4", "2.4", "11.9", "Yes"],
            ["R3", "ER70S-6", "STT", "81", "nominal", "295", "140", "9.525", "9.9", "1.0", "", "no"],
            ["R4", "ER70S-6", "STT", "81", "fast", "400", "140", "9.525", "", "", "", "yes"],
            ["R5", "ER70S-6", "CV", "82", "nominal", "320", "150", "12.7", "8.0", "2.0", "", "yes"],
        ])
        beads, problems = bp.import_bead_log(path)
        self.assertEqual([(b.asf, b.travel_mm_min) for b in beads], [(81, 295.0), (82, 320.0)])
        self.assertEqual(len(problems), 1)
        self.assertIn("'R4': kept but its width or height is blank", problems[0])
        stt, cv = beads
        self.assertEqual(stt.runs, ("R1", "R2"))
        self.assertAlmostEqual(stt.bead_width_mm, 7.2)
        self.assertAlmostEqual(stt.crown_height_mm, 2.5)
        self.assertAlmostEqual(stt.area_mm2, 11.7)
        self.assertEqual(stt.area_basis, "section")
        self.assertAlmostEqual(stt.bead_height_mm, 11.7 / 7.2)
        balance = bp.mass_balance_area_mm2(0.045, 140, 295)
        self.assertAlmostEqual(stt.transfer_efficiency, (11.5 / balance + 11.9 / balance) / 2)
        self.assertEqual(cv.area_basis, "parabola")
        self.assertAlmostEqual(cv.area_mm2, 2 / 3 * 8.0 * 2.0)
        self.assertIn("MEASURED", stt.source)
        self.assertEqual(stt.wire_dia_in, 0.045)

    def test_keep_spellings_travel_tolerance_and_wire_column(self):
        rows = [["R1", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.0", "2.6", "", keep]
                for keep in ("yes", "Y", "TRUE", "x", "1", "no", "")]
        rows[1][5] = "295.4"
        beads, problems = bp.import_bead_log(write_log(self.tmp.name, rows))
        self.assertEqual((len(beads), len(beads[0].runs), problems), (1, 5, []))
        self.assertAlmostEqual(beads[0].travel_mm_min, (295 * 4 + 295.4) / 5)
        path = Path(self.tmp.name) / "wire.csv"
        path.write_text(",".join(bp.LOG_COLUMNS + (bp.WIRE_COLUMN,)) + "\n"
                        "R1,ER308LSi,STT,77,,300,140,9.525,7,2.6,,yes,0.035\n"
                        "R2,ER308LSi,STT,78,,300,140,9.525,7,2.6,,yes,\n", encoding="utf-8")
        beads, problems = bp.import_bead_log(path)
        self.assertEqual([(b.asf, b.wire_dia_in) for b in beads], [(77, 0.035)])
        balance = bp.mass_balance_area_mm2(0.035, 140, 300)
        self.assertAlmostEqual(beads[0].transfer_efficiency, 2 / 3 * 7 * 2.6 / balance)
        self.assertIn("no wire diameter", problems[0])

    def test_unparseable_kept_rows_are_reported_not_dropped(self):
        beads, problems = bp.import_bead_log(write_log(self.tmp.name, [
            ["R1", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.O", "2.6", "", "yes"],
            ["R2", "ER70S-6", "STT", "#81", "", "295", "140", "9.525", "7.0", "2.6", "", "yes"],
            ["R3", "ER70S-6", "STT", "81", "", "295", "", "9.525", "7.0", "2.6", "", "yes"],
            ["R4", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.O", "2.6", "", "no"]]))
        self.assertEqual(beads, [])
        self.assertEqual(len(problems), 3)
        self.assertIn("Width mm '7.O' is not a number", problems[0])
        self.assertIn("ASF # '#81'", problems[1])
        self.assertIn("travel and WFS", problems[2])

    def test_a_section_bigger_than_its_box_is_recorded_with_a_clear_message(self):
        beads, problems = bp.import_bead_log(write_log(self.tmp.name, [
            ["R1", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.0", "1.5", "12.0", "yes"]]))
        self.assertEqual(len(beads), 1)
        self.assertIn("would exceed the crown", beads[0].problem)
        self.assertEqual(problems, [beads[0].problem])
        presets = bp.load_presets()
        self.assertEqual(bp.merge_measured(presets, beads), presets)
        with self.assertRaisesRegex(ValueError, "would exceed the crown"):
            bp.apply_measured(presets[0], beads[0])

    def test_bad_logs_refuse(self):
        mixed = write_log(self.tmp.name, [
            ["R1", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.0", "2.6", "", "yes"],
            ["R2", "ER70S-6", "STT", "81", "", "295", "150", "9.525", "7.4", "2.4", "", "yes"]])
        with self.assertRaisesRegex(ValueError, "mixes WFS"):
            bp.import_bead_log(mixed)
        path = Path(self.tmp.name) / "short.csv"
        path.write_text("Run ID,Width mm\nR1,7\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing columns"):
            bp.import_bead_log(path)


class TestSupersession(unittest.TestCase):
    def setUp(self):
        self.presets = bp.load_presets()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.beads, _ = bp.import_bead_log(write_log(tmp.name, [
            ["R1", "ER70S-6", "STT", "81", "", "295", "140", "9.525", "7.0", "2.6", "11.5", "yes"],
            ["R9", "ER70S-6", "STT", "81", "", "350", "140", "9.525", "6.0", "2.2", "", "yes"]]))

    def test_measured_replaces_nominal_at_its_own_travel_only(self):
        merged = bp.merge_measured(self.presets, self.beads)
        stt = next(p for p in merged if p.asf == 81)
        self.assertEqual(stt.provenance, "MEASURED")
        self.assertEqual((stt.bead_width_mm, stt.crown_height_mm), (7.0, 2.6))
        self.assertAlmostEqual(stt.bead_height_mm, 11.5 / 7.0)
        self.assertEqual((stt.wfs_ipm, stt.travel_mm_min, stt.asf), (140, 295, 81))
        self.assertEqual([p for p in merged if p.asf != 81], [p for p in self.presets if p.asf != 81])
        with self.assertRaisesRegex(ValueError, "not this schedule"):
            bp.apply_measured(next(p for p in self.presets if p.asf == 81), self.beads[1])

    def test_nominal_never_replaces_measured(self):
        measured = bp.merge_measured(self.presets, self.beads)[0]
        nominal = next(p for p in self.presets if p.asf == 81)
        with self.assertRaisesRegex(ValueError, "never replaces a MEASURED"):
            bp.supersede(measured, nominal)
        self.assertIs(bp.supersede(nominal, measured), measured)
        again = bp.apply_measured(measured, self.beads[0])
        self.assertEqual(again, measured)
        self.assertEqual(bp.supersede(measured, replace(measured, label="x")).label, "x")


if __name__ == "__main__":
    unittest.main()
