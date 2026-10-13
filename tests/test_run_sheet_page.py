import copy
import json
import re
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools import lint_checks, lint_pages
from waam_pipeline.run_sheet import render_run_sheet


class _Inputs(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.rows = []
        self.current = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr" and "data-row" in attributes:
            self.current = {}
            self.rows.append(self.current)
        if tag == "input" and self.current is not None and "data-field" in attributes:
            self.current[attributes["data-field"]] = attributes

    def handle_endtag(self, tag):
        if tag == "tr":
            self.current = None


def _row(layer, **kwargs):
    return {"layer": layer, "pass": None, "unit": "F", "pre_weld": None,
            "readings": {}, "park_travel_s_est": None, "height_mm": None,
            "height_source": None, "width_mm": None, "note": "", **kwargs}


def _initial(page):
    return json.loads(re.search(r"const INITIAL = (.*);\n", page).group(1))


class TestRunSheetPage(unittest.TestCase):
    def setUp(self):
        self.record = {"run": "2026-10-07_sheet_01", "workspace": "sheet-test", "status": "open"}

    def test_actual_rows_readings_height_and_units(self):
        rows = [_row(0, pre_weld=77.0),
                _row(4, unit="C", pre_weld=100.0, readings={"20": 200.0, "40": 170.0},
                     park_travel_s_est=12.5, height_mm=2.1, height_source="sense", width_mm=5.2),
                _row(4, **{"pass": 2}, readings={"60": 135.0}, note="hand measurement")]
        page = render_run_sheet(self.record, rows, ["20", "40", "60"])
        inputs = _Inputs(page).rows
        self.assertEqual(len(inputs), 3)
        self.assertIn('<th scope="row">0</th>', page)
        self.assertNotIn("0 · Plate", page)
        self.assertIn('Pre-weld <span class="temp-unit">°F</span><br>(before this layer)', page)
        self.assertIn("plate temperature for layer 0", page)
        self.assertEqual(inputs[0]["pre_weld"]["aria-label"], "Pre-weld temperature before layer 0")
        self.assertIn('<th scope="row">4</th><td>2</td>', page)
        self.assertEqual(inputs[1]["pre_weld"]["value"], "212")
        self.assertEqual(inputs[1]["reading:20"]["value"], "392")
        self.assertEqual(inputs[1]["reading:40"]["value"], "338")
        self.assertEqual(inputs[1]["reading:60"]["value"], "")
        self.assertEqual(inputs[1]["height_mm"]["value"], "2.1")
        self.assertEqual(inputs[1]["width_mm"]["value"], "5.2")
        self.assertIn('<span class="source">sense</span>', page)
        self.assertIn('<td class="park">12.5</td>', page)
        self.assertEqual(inputs[2]["note"]["value"], "hand measurement")
        self.assertEqual(inputs[2]["reading:60"]["min"], "-50")
        self.assertEqual(inputs[2]["reading:60"]["max"], "2000")
        self.assertIn('maxlength="200"', page)
        self.assertIn("Pass (1 = first)", page)
        self.assertIn('id="new-pass" type="number" min="1"', page)

    def test_sensed_prefill_has_distinct_unsaved_status_and_bulk_save(self):
        page = render_run_sheet(
            self.record,
            [_row(1, height_mm=2.7, height_source="sense", height_unsaved=True,
                  at="2026-10-10T12:00:00+00:00")],
            [],
        )
        self.assertIn('class="row-status sensed-unsaved"', page)
        self.assertIn("From sense, not saved", page)
        self.assertIn('id="save-sensed-heights"', page)
        self.assertIn("Save sensed heights", page)
        self.assertIn("delete payload.height_unsaved", page)
        self.assertIn("state.row.height_unsaved = false", page)

    def test_numbered_rows_keep_the_one_based_marker(self):
        page = render_run_sheet(
            self.record, [_row(1, **{"pass": 2}, pass_base=1)], [])
        self.assertEqual(_initial(page)["rows"][0]["pass_base"], 1)
        self.assertIn("{layer, pass, pass_base: 1, unit:", page)
        self.assertNotIn("pass-note", page)

    def test_celsius_display_keeps_original_row_units(self):
        rows = [_row(0, unit="C", pre_weld=25.0),
                _row(1, pre_weld=77.0, readings={"20": 392.0})]
        original = copy.deepcopy(rows)
        page = render_run_sheet(self.record, rows, ["20"])
        inputs = _Inputs(page).rows
        self.assertEqual(inputs[1]["pre_weld"]["value"], "25")
        self.assertEqual(inputs[1]["reading:20"]["value"], "200")
        self.assertIn('<option value="C" selected>', page)
        self.assertIn('Pre-weld <span class="temp-unit">°C</span><br>(before this layer)', page)
        self.assertEqual(_initial(page)["rows"], original)
        self.assertEqual(rows, original)

    def test_notes_are_escaped_and_template_markers_are_preserved(self):
        note = '</script><script>alert("x")</script>& __RUN__ __ROWS__ __INITIAL__ \u2028\u2029'
        page = render_run_sheet(self.record, [_row(1, note=note)], ["20"])
        self.assertEqual(page.count('<script>'), 1)
        self.assertEqual(page.count('</script>'), 1)
        self.assertEqual(_Inputs(page).rows[0]["note"]["value"], note)
        self.assertEqual(_initial(page)["rows"][0]["note"], note)
        self.assertIn(r"\u003c/script\u003e", page)
        self.assertIn(r"\u2028\u2029", page)

    def test_closed_and_absent_runs_disable_entry(self):
        page = render_run_sheet(None, [_row(0)], ["20"], workspace="explicit-workspace")
        self.assertIn('<fieldset id="entry" disabled>', page)
        self.assertIn('No open run', page)
        self.assertFalse(_initial(page)["open"])
        self.assertEqual(_initial(page)["workspace"], "explicit-workspace")
        closed = {**self.record, "status": "finished"}
        self.assertIn('<fieldset id="entry" disabled>', render_run_sheet(closed, [], []))
        self.assertNotIn('<fieldset id="entry" disabled>', render_run_sheet(self.record, [], []))

    def test_api_initialization_keeps_workspace_run_and_token(self):
        page = render_run_sheet(self.record, [_row(0)], ["20"], workspace="actual-workspace")
        self.assertLess(page.index("const WB_K"), page.index("function api("))
        self.assertLess(page.index("const WB_K"), page.index("api('/').href"))
        self.assertEqual(_initial(page)["workspace"], "actual-workspace")
        self.assertIn("url.searchParams.set('ws', INITIAL.workspace)", page)
        self.assertIn("url.searchParams.set('run', INITIAL.run)", page)
        self.assertIn("url.searchParams.set('k', WB_K)", page)
        self.assertIn("credentials: 'same-origin'", page)

    def test_lint_manifest_includes_rendered_sheet(self):
        manifest = lint_pages.render_pages()
        pages = manifest["pages"]
        self.assertEqual(len(pages), 10)
        self.assertEqual(len({page["name"] for page in pages}), len(pages))
        sheet = next(page for page in pages if page["name"] == "run-sheet")
        self.assertEqual(len(sheet["scripts"]), 1)
        source = sheet["scripts"][0]["code"]
        self.assertIn("const INITIAL = ", source)
        self.assertIn("/api/run/layer-row", source)
        self.assertIn('"workspace": "lint-fixture"', source)
        self.assertNotIn("__INITIAL__", source)
        self.assertNotIn("<script>", source)

    def test_javascript_check_uses_common_manifest_and_propagates_failure(self):
        for lint_exit in (0, 1):
            with self.subTest(lint_exit=lint_exit):
                result = subprocess.CompletedProcess(["mock-node"], lint_exit, "", "")
                with mock.patch.object(lint_checks.shutil, "which", return_value="mock-node"), \
                        mock.patch.object(lint_checks, "execute", return_value=result) as execute:
                    self.assertEqual(lint_checks.run_check("javascript"), lint_exit)
                execute.assert_called_once_with([
                    "mock-node", str(ROOT / "tools" / "lint-js.mjs"), "--python", sys.executable,
                ])


if __name__ == "__main__":
    unittest.main()
