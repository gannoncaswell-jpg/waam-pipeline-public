from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import readiness
from waam_pipeline.readiness import (EMIT, FIRST_PRINT, LOGGED, SIM, Mapping,
                                     Row, State)

HEADER = ("id,component,parameter,current_value_mm,basis,priority,"
          "how_to_measure,MEASURED_VALUE_MM,MEASURED_BY,DATE,NOTES\n")


def sheet(tmp: Path, *lines: str, bom: bool = True) -> Path:

    p = tmp / "measurement-sheet.csv"
    p.write_text(HEADER + "".join(l + "\n" for l in lines),
                 encoding="utf-8-sig" if bom else "utf-8")
    return p


def stub_live(display: str, nums: tuple[float, ...]):
    return lambda st: (display, nums)


class SheetLoading(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_bom_does_not_become_part_of_the_first_id(self):


        rows, warn = readiness.load_sheet(sheet(self.tmp, "alpha,c,p,1,LOG,H,how,,,,"))
        self.assertEqual([r.id for r in rows], ["alpha"])
        self.assertEqual(warn, [])

    def test_blank_lines_and_padding_survive(self):
        rows, warn = readiness.load_sheet(sheet(
            self.tmp,
            "  alpha  , c , p , 1 , log , h , how ,,,,",
            ",,,,,,,,,,",
            "beta,c,p,2,ASSUMED,M,how,,,,"))
        self.assertEqual([r.id for r in rows], ["alpha", "beta"])
        self.assertEqual(rows[0].basis, "LOG")
        self.assertEqual(rows[0].priority, "H")
        self.assertEqual(warn, [])

    def test_a_row_with_no_id_is_reported_not_fatal(self):
        rows, warn = readiness.load_sheet(sheet(
            self.tmp, ",c,p,1,LOG,H,how,,,,", "alpha,c,p,1,LOG,H,how,,,,"))
        self.assertEqual([r.id for r in rows], ["alpha"])
        self.assertTrue(any("no id" in w for w in warn), warn)

    def test_duplicate_id_warns(self):
        _, warn = readiness.load_sheet(sheet(
            self.tmp, "alpha,c,p,1,LOG,H,how,,,,", "alpha,c,p,2,LOG,H,how,,,,"))
        self.assertTrue(any("duplicate" in w for w in warn), warn)

    def test_missing_file_is_a_warning_not_an_exception(self):
        rows, warn = readiness.load_sheet(self.tmp / "nope.csv")
        self.assertEqual(rows, [])
        self.assertTrue(warn)

    def test_missing_column_is_a_warning(self):
        p = self.tmp / "measurement-sheet.csv"
        p.write_text("id,component\nalpha,c\n", encoding="utf-8")
        rows, warn = readiness.load_sheet(p)
        self.assertEqual([r.id for r in rows], ["alpha"])
        self.assertTrue(any("MEASURED_VALUE_MM" in w for w in warn), warn)

    def test_typography_is_folded_to_ascii(self):


        rows, _ = readiness.load_sheet(sheet(
            self.tmp, 'alpha,c,"a — b Ø300",1,LOG,H,how,,,,'))
        self.assertEqual(rows[0].parameter, "a -- b dia 300")
        rows[0].parameter.encode("ascii")


class NumberExtraction(unittest.TestCase):
    def test_pulls_numbers_out_of_prose(self):
        self.assertEqual(readiness.numbers("250 x 150 x 40"), (250.0, 150.0, 40.0))
        self.assertEqual(readiness.numbers("X -110.247 Y 0.324"), (-110.247, 0.324))
        self.assertEqual(readiness.numbers("?"), ())
        self.assertEqual(readiness.numbers(""), ())


class Tiering(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _items(self, rows_map, csv_lines, state):
        with mock.patch.dict(readiness._ROWS, rows_map, clear=True):
            rows, _ = readiness.load_sheet(sheet(self.tmp, *csv_lines))
            return {i.key: i for i in readiness.build_items(rows, state)[0]}

    def test_a_blocking_obstacle_makes_the_row_block_emit(self):
        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",))},
            ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(blocking={"wall": -20.0}, headroom={"wall": -20.0}))
        self.assertEqual(items["alpha"].tier, EMIT)
        self.assertTrue(any("BLOCKING" in e for e in items["alpha"].evidence))

    def test_the_same_row_drops_when_the_obstacle_stops_blocking(self):

        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",))},
            ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(headroom={"wall": 300.0}))
        self.assertEqual(items["alpha"].tier, SIM)

    def test_a_guess_with_narrow_headroom_blocks_the_first_print(self):
        room = readiness.NARROW_MARGIN_MM - 1.0
        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",))},
            ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(headroom={"wall": room}))
        self.assertEqual(items["alpha"].tier, FIRST_PRINT)

    def test_the_same_narrow_headroom_does_not_fire_for_a_measured_basis(self):
        room = readiness.NARROW_MARGIN_MM - 1.0
        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",))},
            ["alpha,c,p,1,LOG,H,how,,,,"],
            State(headroom={"wall": room}))
        self.assertEqual(items["alpha"].tier, SIM)

    def test_a_contact_ok_obstacle_never_promotes(self):


        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("plate",))},
            ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(contact_ok=("plate",), headroom={"plate": 0.0}))
        self.assertEqual(items["alpha"].tier, SIM)

    def test_the_hand_floor_raises_but_never_lowers(self):
        state = State(headroom={"wall": 900.0})
        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",), floor=FIRST_PRINT)},
            ["alpha,c,p,1,LOG,H,how,,,,"], state)
        self.assertEqual(items["alpha"].tier, FIRST_PRINT)


        items = self._items(
            {"alpha": Mapping(goes_to="x", obstacles=("wall",), floor=LOGGED)},
            ["alpha,c,p,1,LOG,H,how,,,,"],
            State(blocking={"wall": -3.0}, headroom={"wall": -3.0}))
        self.assertEqual(items["alpha"].tier, EMIT)

    def test_an_unknown_row_id_warns_and_is_carried_not_dropped(self):
        with mock.patch.dict(readiness._ROWS, {}, clear=True):
            rows, _ = readiness.load_sheet(
                self.tmp and sheet(self.tmp, "ghost,c,p,1,LOG,H,how,,,,"))
            items, warn = readiness.build_items(rows, State())
        keys = [i.key for i in items]
        self.assertIn("ghost", keys)
        self.assertTrue(any("ghost" in w for w in warn), warn)
        self.assertEqual([i for i in items if i.key == "ghost"][0].tier, LOGGED)

    def test_a_broken_live_reader_degrades_instead_of_crashing(self):
        def boom(st):
            raise RuntimeError("constant went away")

        items = self._items({"alpha": Mapping(goes_to="x", live=boom)},
                            ["alpha,c,p,1,LOG,H,how,,,,"], State())
        self.assertIn("unavailable", items["alpha"].live)

    def test_specific_causes_sort_above_broad_layout_numbers(self):
        state = State(blocking={"wall": -1.0}, headroom={"wall": -1.0})
        with mock.patch.dict(readiness._ROWS, {
            "broad": Mapping(goes_to="x", obstacles=("wall", "a", "b")),
            "narrow": Mapping(goes_to="x", obstacles=("wall",)),
        }, clear=True):
            rows, _ = readiness.load_sheet(sheet(
                self.tmp,
                "broad,c,p,1,LOG,H,how,,,,",
                "narrow,c,p,1,LOG,M,how,,,,"))
            items, _ = readiness.build_items(rows, state)
        emit = [i.key for i in items if i.tier == EMIT]
        self.assertEqual(emit[:2], ["narrow", "broad"])


class MeasuredAgainstLive(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _item(self, mapping, line, state=None):
        with mock.patch.dict(readiness._ROWS, {"alpha": mapping}, clear=True):
            rows, _ = readiness.load_sheet(sheet(self.tmp, line))
            items, _ = readiness.build_items(rows, state or State())
        return [i for i in items if i.key == "alpha"][0]

    def test_a_measurement_that_reached_the_constant_is_done(self):
        it = self._item(Mapping(goes_to="x", live=stub_live("77.9 mm", (77.9,))),
                        "alpha,c,p,103.2,ASSUMED,H,how,77.9,operator,2026-09-03,")
        self.assertEqual(it.status, "DONE")
        self.assertFalse(it.open)
        self.assertTrue(any("applied" in e for e in it.evidence), it.evidence)

    def test_station_scale_comparison_keeps_the_original_two_decimal_datum(self):
        mapping = readiness._ROWS["ctrl-station-pulses-per-deg"]
        display, comparison = mapping.live(State())
        self.assertIn("1831.8222 p/deg", display)
        self.assertEqual(comparison, (1831.82,))

        cases = ((1831.321, "DONE"), (1832.321, "NOT APPLIED"))
        for measured, status in cases:
            with self.subTest(measured=measured):
                item = self._item(
                    mapping,
                    f"alpha,c,p,1831.82,PENDANT,H,how,{measured},reviewer,2026-10-11,",
                )
                self.assertEqual(item.status, status)

    def test_a_measurement_the_code_never_picked_up_still_blocks(self):


        it = self._item(
            Mapping(goes_to="x", live=stub_live("250 x 150 x 40",
                                                (250.0, 150.0, 40.0)),
                    obstacles=("wall",), floor=FIRST_PRINT),
            "alpha,c,p,250 x 150 x 40,ASSUMED,H,how,260 x 150 x 40,g,2026-09-05,",
            State(headroom={"wall": 900.0}))
        self.assertEqual(it.status, "NOT APPLIED")
        self.assertTrue(it.open)
        self.assertEqual(it.tier, FIRST_PRINT)
        self.assertTrue(any("still says" in e for e in it.evidence), it.evidence)

    def test_a_shape_mismatch_declines_the_comparison_rather_than_guessing(self):
        it = self._item(
            Mapping(goes_to="x", live=stub_live("a vector", (1.0, 2.0, 3.0))),
            "alpha,c,p,x,PENDANT,H,how,TOOL 0: X -110.247,g,2026-09-03,")
        self.assertEqual(it.status, "DONE")
        self.assertTrue(any("not machine-comparable" in e for e in it.evidence),
                        it.evidence)

    def test_notes_without_a_value_read_as_partial(self):
        it = self._item(Mapping(goes_to="x"),
                        'alpha,c,p,?,MANUAL,H,how,,,,"half of it is answered"')
        self.assertEqual(it.status, "PARTIAL")
        self.assertTrue(it.open)


class CalibrationGates(unittest.TestCase):


    def test_an_unset_weld_stickout_blocks_the_first_print(self):
        it = readiness._stickout_gate(State(calibration={
            "platter_face_offset_mm": -25.3,
            "touch_off": {"touch_stickout_mm": 3.175, "weld_stickout_mm": None},
        }))
        self.assertEqual(it.tier, FIRST_PRINT)
        self.assertTrue(it.open)
        self.assertTrue(any("INTO the plate" in e for e in it.evidence))

    def test_setting_it_clears_the_gate(self):
        it = readiness._stickout_gate(State(calibration={
            "touch_off": {"touch_stickout_mm": 3.175, "weld_stickout_mm": 9.525},
        }))
        self.assertFalse(it.open)

    def test_the_error_is_re_derived_from_the_stored_pulses(self):


        from waam_pipeline import cell_lc
        pulses = [-1691, 33350, -49764, 6597, 14034, -2639]
        it = readiness._stickout_gate(State(calibration={
            "platter_face_offset_mm": 0.0,
            "touch_off": {"touch_stickout_mm": 3.175, "weld_stickout_mm": None,
                          "pulses_SLURBT": pulses, "substrate_mm": 0.0},
        }))
        expected = cell_lc.offset_from_touch_off(
            pulses, touch_stickout_mm=3.175, weld_stickout_mm=9.525)
        self.assertTrue(any(f"{expected:.1f}" in e for e in it.evidence),
                        it.evidence)

    def test_one_touch_point_cannot_clear_the_level_gate(self):
        it = readiness._level_gate(State(calibration={
            "fixtures": {}, "active_fixture": None,
            "touch_off": {"single_point": True}}))
        self.assertEqual(it.tier, FIRST_PRINT)
        self.assertTrue(it.open)

    def test_an_active_fixture_that_will_not_load_stays_open(self):
        it = readiness._level_gate(State(
            calibration={"fixtures": {"jig": {}}, "active_fixture": "jig"},
            fixture_error="nearly collinear touches"))
        self.assertTrue(it.open)
        self.assertTrue(any("collinear" in e for e in it.evidence))

    def test_a_fitted_work_surface_clears_it(self):
        it = readiness._level_gate(State(
            calibration={"fixtures": {"jig": {}}, "active_fixture": "jig"},
            fixture_summary="jig: 4 touches, tilt 0.100 deg"))
        self.assertFalse(it.open)


class ReportAndExitCode(unittest.TestCase):


    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _report(self, rows_map, csv_lines, state, **kw):
        with mock.patch.dict(readiness._ROWS, rows_map, clear=True):
            return readiness.report(sheet(self.tmp, *csv_lines),
                                    state=state, **kw)

    def _clear_state(self):

        return State(
            calibration={
                "fixtures": {"jig": {}}, "active_fixture": "jig",
                "touch_off": {"touch_stickout_mm": 3.175,
                              "weld_stickout_mm": 9.525}},
            fixture_summary="jig: 4 touches, tilt 0.100 deg")

    def test_an_open_blocking_item_fails(self):
        ok, lines = self._report(
            {"alpha": Mapping(goes_to="put it in cell_lc.THING",
                              obstacles=("wall",))},
            ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(blocking={"wall": -9.0}, headroom={"wall": -9.0}))
        self.assertFalse(ok)
        text = "\n".join(lines)
        self.assertIn("FAIL:", text)
        self.assertIn("BLOCKS EMIT", text)
        self.assertIn("cell_lc.THING", text)

    def test_a_row_outside_the_blocking_tiers_does_not_fail_the_gate(self):
        ok, lines = self._report(
            {"alpha": Mapping(goes_to="x")},
            ["alpha,c,p,1,ASSUMED,L,how,,,,"], self._clear_state())
        self.assertTrue(ok, "\n".join(lines))
        self.assertIn("PASS:", "\n".join(lines))

    def test_the_full_prose_appears_only_with_verbose(self):
        args = ({"alpha": Mapping(goes_to="x", obstacles=("wall",))},
                ["alpha,c,p,1,ASSUMED,H,TAPE THE THING,,,,"],
                State(blocking={"wall": -9.0}, headroom={"wall": -9.0}))
        self.assertNotIn("TAPE THE THING", "\n".join(self._report(*args)[1]))
        self.assertIn("TAPE THE THING",
                      "\n".join(self._report(*args, verbose=True)[1]))

    def test_non_blocking_tiers_are_summarised_until_all(self):
        args = ({"alpha": Mapping(goes_to="x")},
                ["alpha,cmp,PARAM-TEXT,1,ASSUMED,L,how,,,,"],
                self._clear_state())
        self.assertNotIn("PARAM-TEXT", "\n".join(self._report(*args)[1]))
        self.assertIn("PARAM-TEXT", "\n".join(self._report(*args, show_all=True)[1]))

    def test_the_emitters_own_refusal_is_quoted_verbatim(self):
        ok, lines = self._report(
            {}, ["alpha,c,p,1,ASSUMED,H,how,,,,"],
            State(emit_refusal="argument: arc_file must be an int in 1..1000",
                  calibration=self._clear_state().calibration))
        text = "\n".join(lines)
        self.assertFalse(ok)
        self.assertIn("argument: arc_file must be an int in 1..1000", text)
        self.assertIn("BLOCKS EMIT: emitter refused", text)
        self.assertIn("FAIL:", text)

    def test_the_cli_returns_nonzero_when_the_emitter_refuses(self):
        with mock.patch.object(readiness, "report", return_value=(False, ["refused"])), \
                mock.patch("builtins.print"):
            self.assertEqual(readiness.main([]), 1)

    def test_the_capture_list_cross_references_rather_than_duplicating(self):
        st = self._clear_state()
        st.blocking = st.headroom = {"wall": -1.0}
        with mock.patch.dict(
                readiness._ROWS,
                {"alpha": Mapping(goes_to="x", obstacles=("wall",))}, clear=True):
            lines = readiness.capture_list(
                sheet(self.tmp, "alpha,c,p,1,ASSUMED,H,TAPE IT,,,,"), state=st)
        text = "\n".join(lines)
        self.assertIn(readiness.CHECKLIST, text)
        self.assertIn("TAPE IT", text)
        self.assertIn("alpha", text)


class TheHandWrittenMapIsNotStale(unittest.TestCase):


    def setUp(self):
        self.rows, _ = readiness.load_sheet()
        if not self.rows:
            self.skipTest("no committed measurement sheet to check against")

    def test_every_mapped_id_still_exists_in_the_sheet(self):
        ids = {r.id for r in self.rows}
        stale = sorted(set(readiness._ROWS) - ids)
        self.assertEqual(stale, [], "readiness._ROWS names rows the sheet no "
                                    "longer has; review the map against "
                                    "hardware/cell_models/cell_models.py")

    def test_every_sheet_row_is_claimed(self):
        unmapped = sorted({r.id for r in self.rows} - set(readiness._ROWS))
        self.assertEqual(unmapped, [], "new measurement-sheet rows: add them "
                                       "to readiness._ROWS with the constant "
                                       "they settle")

    def test_every_live_reader_answers(self):
        st = readiness.State()
        for rid, m in readiness._ROWS.items():
            if m.live is None:
                continue
            with self.subTest(row=rid):
                display, nums = m.live(st)
                self.assertIsInstance(display, str)
                self.assertTrue(display.strip())
                self.assertTrue(all(isinstance(n, float) for n in nums))

    def test_every_named_obstacle_is_really_in_the_scene(self):
        from waam_pipeline import cell_lc


        have = {n for n, *_ in cell_lc.lc_scene(training_block=True).boxes}
        for rid, m in readiness._ROWS.items():
            with self.subTest(row=rid):
                self.assertLessEqual(set(m.obstacles), have)


class EndToEnd(unittest.TestCase):


    def test_it_runs_and_returns_a_gate_code(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = readiness.main([])
        self.assertIn(rc, (0, 1))
        text = buf.getvalue()
        self.assertIn("WHAT THE EMITTER DOES TODAY", text)
        for name, _ in readiness.TIERS:
            self.assertIn(name, text)
        text.encode("ascii")

    def test_capture_always_exits_clean(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(readiness.main(["--capture"]), 0)

    def test_the_probe_reads_the_live_cell(self):
        st = readiness.gather()
        self.assertEqual(st.errors, [], st.errors)
        self.assertIn("charles_platter", st.scene_obstacles)
        self.assertIn("charles_platter", st.contact_ok)

        self.assertTrue(bool(st.emit_ok) != bool(st.emit_refusal))


class TestTrainingBlockIsReportedButNeverForgiven(unittest.TestCase):


    def test_the_block_off_probe_never_clears_the_headline_refusal(self):
        st = readiness.State(
            emit_refusal="clearance: manpro_training_block at move 1: -20.0 mm",
            blocking={"manpro_training_block": -20.0},
            without_block="CLEAR -- with the block off the platter this job emits",
        )
        ok, lines = readiness.report(state=st)
        text = "\n".join(lines)
        self.assertFalse(ok, "a clear block-off probe must not pass the gate")
        self.assertIn("REFUSED", text)
        self.assertIn("manpro_training_block -20.0 mm", text)

    def test_it_says_what_removing_the_block_would_do(self):
        st = readiness.State(
            without_block="CLEAR -- with the block off the platter this job emits")
        _, lines = readiness.report(state=st)
        self.assertIn("block off", "\n".join(lines))

    def test_silent_when_the_probe_did_not_run(self):
        _, lines = readiness.report(state=readiness.State())
        self.assertNotIn("block off", "\n".join(lines))

    def test_removing_the_block_really_does_clear_this_cell(self):


        from waam_pipeline import cell_lc
        from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking, check
        from waam_pipeline.slicer import wall

        torch = TorchModel.yaskawa_accufast_45()
        tp = wall(readiness.PROBE_WALL_MM, readiness.PROBE_LAYERS,
                  readiness.PROBE_LAYER_H_MM)
        x, y = (float(c) for c in cell_lc.DEFAULT_PART_ORIGIN[:2])
        origin = cell_lc.snap_toolpath_to_platter(
            tp, x, y, substrate_mm=readiness.PROBE_SUBSTRATE_MM)

        def blockers(fitted):
            scene = cell_lc.lc_scene(part_origin_robot=origin,
                                     training_block=fitted)
            return blocking(check(tp, torch, scene,
                                  bead_height_mm=readiness.PROBE_LAYER_H_MM,
                                  margin_mm=readiness.PROBE_MARGIN_MM,
                                  contact_ok=CONTACT_OK))

        self.assertTrue(blockers(True), "the block should still gate this job")
        self.assertEqual(blockers(False), [],
                         "taking the block off should leave nothing blocking")


class SwingEnvelope(unittest.TestCase):


    HALF_WIDTH = 414.3 / 2.0
    HISTORICAL_FACE = 77.9

    def test_the_envelope_constants_are_the_drawing_dims(self):
        self.assertEqual(readiness.OEM_TOOLING_DIA_MM, 490.0)
        self.assertEqual(readiness.OEM_SWING_RADIUS_MM, 245.0)
        self.assertEqual(readiness.OEM_ALONG_AXIS_MM, 1450.0)

    def test_the_installed_plate_is_inside(self):
        r = readiness.swing_radius_mm(self.HALF_WIDTH, self.HISTORICAL_FACE)
        self.assertAlmostEqual(r, 221.3, delta=0.1)
        self.assertLess(r, readiness.OEM_SWING_RADIUS_MM)

    def test_the_plate_plus_substrate_is_still_inside(self):
        r = readiness.swing_radius_mm(self.HALF_WIDTH, self.HISTORICAL_FACE + 6.35)
        self.assertAlmostEqual(r, 223.6, delta=0.1)
        self.assertLess(r, readiness.OEM_SWING_RADIUS_MM)

    def test_a_100mm_wall_at_the_plate_edge_is_outside(self):
        r = readiness.swing_radius_mm(self.HALF_WIDTH, self.HISTORICAL_FACE + 100.0)
        self.assertAlmostEqual(r, 273.1, delta=0.1)
        self.assertGreater(r, readiness.OEM_SWING_RADIUS_MM)

    def test_an_outside_job_is_logged_and_never_gates(self):


        st = State(swing="r300.0 mm swept about the station axis, OUTSIDE ...",
                   swing_exceeds=True)
        item = next(i for i in readiness.build_items([], st)[0]
                    if i.key == "cell:swing_envelope")
        self.assertEqual(item.tier, LOGGED)
        self.assertNotIn(LOGGED, readiness.GATING)
        self.assertEqual(item.status, "OUTSIDE")

    def test_an_inside_job_has_nothing_to_do(self):
        st = State(swing="r133.4 mm swept about the station axis, inside ...",
                   swing_exceeds=False)
        item = next(i for i in readiness.build_items([], st)[0]
                    if i.key == "cell:swing_envelope")
        self.assertEqual(item.tier, LOGGED)
        self.assertFalse(item.open)

    def test_the_report_prints_the_swing_line(self):
        st = State(swing="r133.4 mm swept about the station axis, inside the "
                         "OEM envelope (r245, dia 490); along-axis extent 0 mm "
                         "(limit 1450)")
        _, lines = readiness.report(state=st)
        text = "\n".join(lines)
        self.assertIn("swing", text)
        self.assertIn("swept about the station axis", text)
        text.encode("ascii")

    def test_it_is_probed_live_and_the_representative_job_is_inside(self):


        st = readiness.gather()
        self.assertTrue(st.swing, "gather did not fill the swing line")
        self.assertFalse(st.swing_exceeds)


if __name__ == "__main__":
    unittest.main()
