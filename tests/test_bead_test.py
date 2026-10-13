import csv
import importlib.util
import io
import json
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

import numpy as np

from waam_pipeline import bead_test as bt, cell_lc
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import CONTACT_OK, Scene, TorchModel, blocking, check_point
from waam_pipeline.emit import EmitRefused, PlatterState
from waam_pipeline.plan import PARK_PULSES

ROOT = Path(__file__).resolve().parents[1]


def example():
    return bt.TestSpec.load(ROOT / "examples/bead_test_lc50s.json")


def reviewed():

    return replace(example(), planning_only=False, process=dict(
        material="synthetic steel record", wire_dia_mm=1.143, gas="synthetic gas record",
        gas_flow_l_min=20, proc=3, arc_file=57, wfs_ipm=200, voltage_v=20,
        ctwd_mm=9.525, max_start_temp_c=100, substrate_material="synthetic coupon",
        surface_prep="synthetic preparation", record_reference="UNIT TEST ONLY"))


def csv_rows(spec, transform):
    rows = list(csv.DictReader(io.StringIO(bt.measurement_csv(spec))))
    transform(rows)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=bt.MEASUREMENT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class TestDesign(unittest.TestCase):
    def test_repeats_are_balanced_and_ids_have_fixed_locations(self):
        spec = example()
        rows = bt.trials(spec)
        self.assertEqual(rows, bt.trials(spec))
        self.assertEqual(sorted(t.run_order for t in rows), list(range(1, 10)))
        for repeat in range(1, 4):
            self.assertEqual(sorted(t.speed_mm_min for t in rows if t.repeat == repeat), [150, 200, 250])
        self.assertNotEqual(rows, bt.trials(replace(spec, seed=99)))
        for row in rows:
            self.assertEqual(row.id, f"B{row.row + 1:02d}")
            moves = row.toolpath().moves
            self.assertFalse(moves[0].deposit)
            self.assertTrue(moves[1].deposit)
            self.assertEqual(moves[1].feed, row.speed_mm_min)
            self.assertEqual(row.nominal_bead_height_mm, spec.nominal_bead_height_mm)

            self.assertEqual([m.z for m in moves], [0.0, 0.0])
            self.assertAlmostEqual(row.toolpath().deposit_length(), 100.)

    def test_invalid_numbers_geometry_and_schema_refuse(self):
        invalid = [dict(speeds_mm_min=[math.nan]), dict(speeds_mm_min=[True]),
                   dict(speeds_mm_min=[200, 200]), dict(speeds_mm_min=[200, 199]),
                   dict(speeds_mm_min=[1]), dict(repeats=True), dict(seed=-1),
                   dict(length_mm=math.inf), dict(pitch_mm=20), dict(coupon_width_mm=100),
                   dict(nominal_bead_height_mm=0), dict(nominal_bead_height_mm=-1),
                   dict(nominal_bead_height_mm=math.nan), dict(nominal_bead_height_mm=math.inf),
                   dict(nominal_bead_height_mm=-math.inf), dict(nominal_bead_height_mm=True),
                   dict(nominal_bead_height_mm="2.9"), dict(nominal_bead_height_mm=6.01),
                   dict(origin_x_mm=1500), dict(name="invalid\nname"),
                   dict(planning_only="false"), dict(process={"unknown": 1}),
                   dict(process={"gas": "a\nb"})]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(example(), **kwargs)

    def test_nominal_height_is_required_and_may_equal_the_clearance_limit(self):
        data = asdict(example())
        del data["nominal_bead_height_mm"]
        with self.assertRaisesRegex(TypeError, "nominal_bead_height_mm"):
            bt.TestSpec(**data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-plan.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nominal_bead_height_mm"):
                bt.TestSpec.load(path)
        spec = replace(example(), nominal_bead_height_mm=example().height_limit_mm)
        self.assertEqual(spec.nominal_bead_height_mm, spec.height_limit_mm)

    def test_nominal_height_neither_seats_the_torch_nor_sizes_the_clearance(self):


        for height in (1.7, 4.2):
            with self.subTest(height=height):
                spec = replace(example(), nominal_bead_height_mm=height)
                trial = bt.trials(spec)[0]
                origin, _, scene = bt.seated_scene(spec, trial, training_block=False)
                self.assertEqual([p[2] for p in trial.toolpath().deposit_points()], [0.0, 0.0])
                self.assertAlmostEqual(origin[2], cell_lc.platter_work_surface_z(spec.substrate_mm))
                coupon = next(b for b in scene.boxes if b[0] == "bead_test_coupon")
                self.assertAlmostEqual(origin[2] + coupon[2][2],
                                       cell_lc.platter_work_surface_z(spec.substrate_mm))
                for name, _, high in scene.boxes:
                    if name.startswith("bead_test_B"):
                        self.assertEqual(high[2], spec.height_limit_mm)

    def test_fingerprint_roundtrip_and_changes(self):
        spec = example()
        self.assertEqual(spec.fingerprint, bt.TestSpec(**json.loads(json.dumps(asdict(spec)))).fingerprint)
        self.assertNotEqual(spec.fingerprint, replace(spec, seed=17).fingerprint)
        changed = replace(spec, nominal_bead_height_mm=3.1)
        self.assertNotEqual(spec.fingerprint, changed.fingerprint)
        with self.assertRaisesRegex(ValueError, "mismatched trial"):
            bt.analyse_measurements(changed, bt.measurement_csv(spec))

    def test_all_other_beads_are_unique_obstacles(self):
        spec = example()
        for trial in bt.trials(spec):
            _, _, scene = bt.seated_scene(spec, trial, training_block=False)
            names = [name for name, *_ in scene.boxes + scene.capsules]
            self.assertEqual(len(names), len(set(names)))
            self.assertNotIn("bead_test_" + trial.id, names)
            self.assertEqual(sum(n.startswith("bead_test_B") for n in names), 8)
            self.assertIn("bead_test_coupon", scene.part_fixed)

    def test_coupon_allows_contact_but_refuses_penetration(self):
        scene = Scene(boxes=[("bead_test_coupon", np.array([0., 0., -6.]), np.array([100., 100., 0.]))])
        torch = TorchModel.yaskawa_accufast_45()
        self.assertFalse(blocking(check_point((50, 50, 0), torch, scene, contact_ok=CONTACT_OK)))
        self.assertTrue(blocking(check_point((50, 50, -1), torch, scene, contact_ok=CONTACT_OK)))
        scene.boxes[0] = ("bead_test_B99", *scene.boxes[0][1:])
        self.assertTrue(blocking(check_point((50, 50, 0), torch, scene, contact_ok=CONTACT_OK)))


class TestEmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "cell.json"
        self.cfg = json.loads(cell_lc.LC_CELL_PROFILE.read_text(encoding="utf-8"))
        self.cfg.update(proc=3, arc_file=57)
        self.write_profile()
        self.platter = PlatterState(True, False, None, "synthetic unit test")

    def write_profile(self):
        self.profile.write_text(json.dumps(self.cfg), encoding="utf-8")

    def emit(self, spec=None, trial="B01", platter=None):
        return bt.emit_trial(spec or reviewed(), trial, cell_profile=self.profile,
                             platter=platter or self.platter)

    def test_every_trial_has_one_arc_correct_speed_and_physical_coordinates(self):
        spec = reviewed()
        for trial in bt.trials(spec):
            with self.subTest(trial=trial.id):
                text, summary = self.emit(spec, trial.id)
                job = read_jbi(text)
                ins = job.instructions
                self.assertEqual(ins.count("ARCON ASF#(57)"), 1)
                self.assertEqual(ins.count("ARCOF AEF#(57)"), 1)
                arc = ins.index("ARCON ASF#(57)")
                token = ins[arc + 1].split("V=")[1]
                self.assertAlmostEqual(float(token) * 60, trial.speed_mm_min, delta=3.0)
                self.assertEqual(summary["commanded_travel_mm_min"], float(token) * 60)
                origin = np.array(summary["origin_robot_mm"])
                h = spec.nominal_bead_height_mm
                self.assertAlmostEqual(origin[2], cell_lc.platter_work_surface_z(spec.substrate_mm))
                self.assertEqual(summary["trial"]["nominal_bead_height_mm"], h)
                self.assertEqual(summary["trial"]["nominal_bead_height"], spec.nominal_bead_height_label)
                self.assertIn(f"NOMINAL h={h:g}mm", text)
                self.assertIn("height UNVERIFIED placeholder", text)


                for instruction, local in ((ins[arc-1], [trial.x0, trial.y, 0.0]),
                                           (ins[arc+1], [trial.x1, trial.y, 0.0])):
                    self.assertTrue(instruction.startswith("MOVL"))
                    pulses = [int(v) for v in job.positions[instruction.split()[1]].split(",")]
                    decoded = cell_lc.tcp_from_pulses(pulses)
                    np.testing.assert_allclose(decoded, origin + local, atol=.03, rtol=0)
                    self.assertAlmostEqual(decoded[2], cell_lc.platter_work_surface_z(spec.substrate_mm), delta=.03)


                self.assertRegex(ins[-2], r"^MOVJ C[0-9]{5} VJ=50\.00$")
                park_name = ins[-2].split()[1]
                self.assertEqual(tuple(int(v) for v in job.positions[park_name].split(",")), PARK_PULSES)
                targets = [line.split()[1] for line in ins if line.startswith(("MOVJ C", "MOVL C"))]
                self.assertEqual(len(targets), len(set(targets)))
                self.assertIn(spec.fingerprint, text)
                self.assertEqual(len(summary["cell_profile_sha256"]), 64)

    def test_draft_and_incomplete_records_do_not_emit(self):
        with self.assertRaisesRegex(EmitRefused, "example values"):
            self.emit(example())
        for field in reviewed().process:
            with self.subTest(field=field), self.assertRaisesRegex(EmitRefused, "Record the fixed"):
                self.emit(replace(reviewed(), process={k:v for k,v in reviewed().process.items() if k != field}))

    def test_profile_process_arc_tool_and_calibration_mismatches_refuse(self):
        for key, value in (("proc", 4), ("arc_file", 12), ("tool_number", 1), ("ctwd_mm", 10)):
            original = self.cfg[key]
            self.cfg[key] = value
            self.write_profile()
            with self.subTest(key=key), self.assertRaises(EmitRefused):
                self.emit()
            self.cfg[key] = original
        self.cfg["ctwd_mm"] = 10
        self.write_profile()
        with self.assertRaises(EmitRefused):
            self.emit(replace(reviewed(), process={**reviewed().process, "ctwd_mm": 10}))

    def test_recorded_ctwd_is_the_weld_ctwd(self):

        with self.assertRaises(EmitRefused) as refused:
            self.emit(replace(reviewed(), process={**reviewed().process, "ctwd_mm": 9.0}))
        self.assertEqual(refused.exception.reason, "ctwd")
        spec = replace(reviewed(), process={**reviewed().process, "ctwd_mm": 19.05})
        text, summary = self.emit(spec)
        self.assertEqual(summary["weld_ctwd_mm"], 19.05)
        self.assertIn("' WELD CTWD 19.05MM 0.750IN", text.splitlines())
        job = read_jbi(text)
        ins = job.instructions
        arc = ins.index("ARCON ASF#(57)")
        for instruction in (ins[arc - 1], ins[arc + 1]):
            pulses = [int(v) for v in job.positions[instruction.split()[1]].split(",")]
            self.assertAlmostEqual(cell_lc.tcp_from_pulses(pulses)[2],
                                   cell_lc.platter_work_surface_z(spec.substrate_mm) + 19.05 - 9.525,
                                   delta=.03)

    def test_current_repository_profile_cannot_select_other_users_arc_file(self):
        with self.assertRaises(EmitRefused):
            bt.emit_trial(reviewed(), "B01", cell_profile=cell_lc.LC_CELL_PROFILE, platter=self.platter)

    def test_attestation_and_real_block_collision_are_retained(self):
        with self.assertRaises(EmitRefused):
            bt.emit_trial(reviewed(), "B01", cell_profile=self.profile, platter=None)
        with self.assertRaisesRegex(EmitRefused, "attestation"):
            self.emit(platter=PlatterState(False, False, None, "test"))
        with self.assertRaisesRegex(EmitRefused, "manpro_training_block"):
            self.emit(platter=PlatterState(True, True, None, "test"))

    def test_measured_surface_refusal_never_falls_back(self):
        with mock.patch.object(cell_lc, "seat_job", side_effect=ValueError("measured plane tilt")):
            with self.assertRaisesRegex(ValueError, "measured plane tilt"):
                self.emit()


class TestMeasurements(unittest.TestCase):
    def test_blank_sheet_has_no_invented_width_or_variation(self):
        results = bt.analyse_measurements(example(), bt.measurement_csv(example()))
        for condition in results["conditions"]:
            self.assertEqual(condition["accepted_beads"], 0)
            self.assertIsNone(condition["mean_width_mm"])
            self.assertIsNone(condition["between_bead_sd_mm"])

    def test_nominal_is_tagged_and_never_fills_the_measured_height(self):
        spec = example()
        rows = list(csv.DictReader(io.StringIO(bt.measurement_csv(spec))))
        results = bt.analyse_measurements(spec, bt.measurement_csv(spec))
        for row, result in zip(rows, results["trials"]):
            self.assertEqual(row["nominal_bead_height"], "h=2.9 mm (NOMINAL, unverified)")
            self.assertEqual(row["height_mid_mm"], "")
            self.assertEqual(result["nominal_bead_height"], row["nominal_bead_height"])
            self.assertIsNone(result["height_mid_mm"])

    def test_independent_bead_means_and_sample_deviation(self):
        def fill(rows):
            for row, mean in zip([r for r in rows if float(r["travel_mm_min"]) == 150], [5, 7, 9]):
                row.update(status="ok", **dict(zip(bt.WIDTH_FIELDS, [mean-1, mean, mean+1])))
        result = bt.analyse_measurements(example(), csv_rows(example(), fill))
        first = result["conditions"][0]
        self.assertEqual(first["mean_width_mm"], 7)
        self.assertEqual(first["between_bead_sd_mm"], 2)
        self.assertEqual(first["accepted_beads"], 3)
        self.assertTrue(all(r["within_bead_sd_mm"] == 1 for r in result["trials"] if r["status"] == "ok"))

    def test_rejections_remain_in_record_but_not_speed_mean(self):
        def fill(rows):
            rows[0].update(status="rejected", width_25_mm=40, width_50_mm=50, width_75_mm=60, notes="defect")
        result = bt.analyse_measurements(example(), csv_rows(example(), fill))
        self.assertEqual(result["trials"][0]["mean_width_mm"], 50)
        self.assertEqual(result["trials"][0]["notes"], "defect")
        self.assertEqual(result["conditions"][0]["accepted_beads"], 0)
        self.assertTrue(result["exceeds_width_limit"])

    def test_bad_or_modified_records_refuse(self):
        mutations = [lambda rs: rs[0].update(plan="wrong"), lambda rs: rs.append(rs[0]),
                     lambda rs: rs.pop(), lambda rs: rs[0].update(status="ok"),
                     lambda rs: rs[0].update(width_25_mm="NaN"),
                     lambda rs: rs[0].update(start_temp_c="inf"),
                     lambda rs: rs[0].update(travel_mm_min="201"),
                     lambda rs: rs[0].update(nominal_bead_height="2.9"),
                     lambda rs: rs[0].update(nominal_bead_height="h=3 mm (NOMINAL, unverified)"),
                     lambda rs: rs[0].update(status="done"),
                     lambda rs: rs[0].update(width_25_mm="4"),
                     lambda rs: rs[0].update(actual_voltage_v="20")]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValueError):
                bt.analyse_measurements(example(), csv_rows(example(), change))
        for malformed in ("plan,trial\nx,y", bt.measurement_csv(example()) + "a,b\n",
                          bt.measurement_csv(example()).replace("not_run", "not_run,extra", 1)):
            with self.assertRaises(ValueError):
                bt.analyse_measurements(example(), malformed)

    def test_observed_height_and_temperature_exceedances_flagged(self):
        spec = reviewed()
        def fill(rows):
            rows[0].update(status="ok", width_25_mm=5, width_50_mm=6, width_75_mm=7,
                           height_mid_mm=7, start_temp_c=101)
        result = bt.analyse_measurements(spec, csv_rows(spec, fill))
        self.assertTrue(result["exceeds_height_limit"])
        self.assertTrue(result["exceeds_start_temp"])
        self.assertEqual(result["conditions"][0]["accepted_beads"], 1)
        self.assertIsNone(result["conditions"][0]["between_bead_sd_mm"])


class TestPack(unittest.TestCase):
    def test_completed_report_shows_each_beads_measurements_and_status(self):
        def fill(rows):
            rows[0].update(status="ok", width_25_mm=5.1, width_50_mm=5.2, width_75_mm=5.3, height_mid_mm=1.8, start_temp_c=22)
            rows[1].update(status="rejected", notes="defect")
        spec = example()
        report = bt.render_report(spec, bt.analyse_measurements(spec, csv_rows(spec, fill)))
        self.assertIn('<td>Accepted</td><td>h=2.9 mm (NOMINAL, unverified)</td>'
                      '<td>5.1</td><td>5.2</td><td>5.3</td><td>1.8</td><td>22</td>', report)
        self.assertIn("Measured height, mm", report)
        self.assertIn('<td>Rejected</td>', report)
        self.assertIn('<td>5.200</td>', report)

    def test_nominal_label_persists_after_process_review(self):
        for spec in (example(), reviewed()):
            with self.subTest(planning_only=spec.planning_only):
                report = bt.render_report(spec)
                self.assertEqual(report.count("h=2.9 mm (NOMINAL, unverified)"), 10)
                self.assertIn("no measured height is inferred", report)

    def test_planning_pack_and_overwrite_refusal(self):
        module_spec = importlib.util.spec_from_file_location("gen_bead_test", ROOT / "examples/gen_bead_test.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "pack"
            argv = [str(ROOT / "examples/bead_test_lc50s.json"), "--out", str(out)]
            self.assertEqual(module.main(argv), 0)
            self.assertEqual(len(list(out.glob("*.JBI"))), 0)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["plan"], example().fingerprint)
            self.assertEqual(manifest["schema"], 2)
            self.assertEqual(manifest["nominal_bead_height"], "h=2.9 mm (NOMINAL, unverified)")
            for trial in manifest["trials"]:
                self.assertEqual(trial["nominal_bead_height"], manifest["nominal_bead_height"])
            self.assertEqual(module.main(argv), 1)
            self.assertEqual(module.main(argv[:-1] + [str(Path(directory)/"job"), "--trial", "B01"]), 1)
            self.assertFalse((Path(directory)/"job").exists())

    def test_report_escapes_notes_and_keeps_results_bound_to_plan(self):
        spec = replace(example(), process={"material": '<script>alert("bad")</script>'})
        report = bt.render_report(spec)
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn('<script>alert("bad")', report)
        self.assertIn("Planning example", report)
        with self.assertRaises(ValueError):
            bt.render_report(spec, {"plan": "different"})
