from __future__ import annotations

import html
import json
import re


def _temperature(value, source: str, target: str):
    if value is None or source == target:
        return value
    return (value - 32) * 5 / 9 if target == "C" else value * 9 / 5 + 32


def _number(value) -> str:
    return "" if value is None else format(value, ".12g")


def _input(field: str, value, label: str, *, temperature=False, maximum=200) -> str:
    minimum = -50 if temperature else 0
    maximum = 2000 if temperature else maximum
    return (f'<input type="number" data-field="{html.escape(field, quote=True)}" '
            f'value="{html.escape(_number(value), quote=True)}" min="{minimum}" '
            f'max="{maximum}" step="any" inputmode="decimal" '
            f'aria-label="{html.escape(label, quote=True)}">')


def _row_html(row: dict, index: int, times: list[str], unit: str) -> str:
    layer = row["layer"]
    label = str(layer)
    row_unit = row.get("unit", "F")
    pre_weld = _temperature(row.get("pre_weld"), row_unit, unit)
    cells = [f'<th scope="row">{html.escape(label)}</th>',
             f'<td>{html.escape(str(row["pass"])) if row.get("pass") is not None else "—"}</td>',
             '<td>' + _input("pre_weld", pre_weld, f"Pre-weld temperature before layer {layer}",
                            temperature=True) + '</td>']
    for seconds in times:
        value = _temperature((row.get("readings") or {}).get(seconds), row_unit, unit)
        cells.append('<td>' + _input("reading:" + seconds, value,
                                     f"Layer {layer} temperature at {seconds} seconds",
                                     temperature=True) + '</td>')
    park = _number(row.get("park_travel_s_est"))
    cells.append(f'<td class="park">{html.escape(park) if park else "—"}</td>')
    source = row.get("height_source") or ""
    cells.append('<td>' + _input("height_mm", row.get("height_mm"), f"Layer {layer} height mm")
                 + f'<span class="source">{html.escape(source)}</span></td>')
    cells.append('<td>' + _input("width_mm", row.get("width_mm"), f"Layer {layer} width mm") + '</td>')
    cells.append(f'<td><input type="text" data-field="note" maxlength="200" '
                 f'value="{html.escape(row.get("note") or "", quote=True)}" '
                 f'aria-label="Layer {layer} note"></td>')
    sensed = bool(row.get("height_unsaved"))
    status = "From sense, not saved" if sensed else ("Saved" if row.get("at") else "Not saved")
    status_class = "row-status sensed-unsaved" if sensed else "row-status"
    cells.append(f'<td><span class="{status_class}" role="status">{status}</span></td>')
    return f'<tr data-row="{index}">' + ''.join(cells) + '</tr>'


def render_run_sheet(record: dict | None, rows: list[dict], reading_times: list[str],
                     *, workspace: str = "") -> str:

    unit = next((row.get("unit", "F") for row in rows
                 if row.get("pre_weld") is not None or row.get("readings")), "F")
    run_id = (record or {}).get("run", "")
    is_open = bool(record and record.get("status") == "open")
    initial = json.dumps({"rows": rows, "times": reading_times, "unit": unit,
                          "run": run_id, "workspace": workspace or (record or {}).get("workspace", ""),
                          "open": is_open}, ensure_ascii=True, allow_nan=False)
    for original, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e")):
        initial = initial.replace(original, escaped)
    readings = ''.join(f'<th scope="col">{html.escape(seconds)} s<br><span class="temp-unit">°{unit}</span></th>'
                       for seconds in reading_times)
    table_rows = ''.join(_row_html(row, index, reading_times, unit) for index, row in enumerate(rows))
    message = ("Changes save to this run automatically. Layers count from 0. Pre-weld is taken before "
               "this layer: plate temperature for layer 0, or at the stop after the preceding layer. "
               "Cooling readings, height and width belong to this layer after welding. Reading times "
               "start after arrival at park; the fit includes estimated park travel.") if is_open else (
                   "No open run. Export a job from the workbench to start a run before entering measurements.")
    replacements = {"__INITIAL__": initial, "__RUN__": html.escape(run_id or "No open run"),
                    "__MESSAGE__": message, "__READINGS__": readings, "__ROWS__": table_rows,
                    "__DISABLED__": "" if is_open else "disabled", "__UNIT__": unit,
                    "__F_SELECTED__": "selected" if unit == "F" else "",
                    "__C_SELECTED__": "selected" if unit == "C" else ""}
    return re.sub(r"__[A-Z_]+__", lambda match: replacements[match.group()], _PAGE)


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run data sheet · WAAM</title>
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #19313d; background: #f3f6f7; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; }
main { max-width: 1700px; margin: auto; }
a { color: #09657c; }
h1 { margin: 20px 0 6px; font-size: 1.8rem; }
h2 { font-size: 1.25rem; margin-top: 0; }
p { line-height: 1.5; }
.run { overflow-wrap: anywhere; font-weight: 600; }
.help, .source, .row-status { color: #536671; }
.toolbar { display: flex; gap: 16px; align-items: end; flex-wrap: wrap; margin: 20px 0; }
label { display: grid; gap: 5px; font-size: .9rem; }
input, select, button { font: inherit; min-height: 46px; border: 1px solid #98aeb8; border-radius: 6px; }
input, select { color: #142f3e; background: white; padding: 9px; }
input { width: 115px; }
input[data-field="note"] { width: 240px; }
button { padding: 10px 18px; background: #12667c; color: white; font-weight: 600; cursor: pointer; }
button:disabled, fieldset:disabled input { opacity: .55; cursor: default; }
:focus-visible { outline: 3px solid #00a9bb; outline-offset: 2px; }
fieldset { min-width: 0; border: 0; margin: 0; padding: 0; }
.table-wrap { overflow-x: auto; background: white; border: 1px solid #c5d4da; border-radius: 9px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; padding: 12px; border-bottom: 1px solid #dce4e8; }
thead th { background: #e7eff2; white-space: nowrap; font-size: .88rem; }
tbody th { position: sticky; left: 0; background: white; min-width: 83px; padding-top: 24px; }
tbody td:nth-child(2), .park { padding-top: 24px; }
.source, .row-status { display: block; font-size: .8rem; margin-top: 5px; min-width: 70px; }
.row-status.error, .error { color: #a01f24; }
.row-status.sensed-unsaved { color: #8a4b08; font-weight: 600; }
.decay { padding: 22px; background: white; border: 1px solid #c5d4da; border-radius: 9px; margin-top: 24px; }
.decay ul { padding-left: 22px; line-height: 1.8; }
.decay-summary { font-weight: 600; }
@media (max-width: 650px) { body { padding: 14px; } h1 { font-size: 1.5rem; } th, td { padding: 9px; }
  .toolbar { gap: 10px; } .decay { padding: 16px; } }
</style></head><body><main>
<a id="workbench-link" href="/">← Workbench</a>
<h1>Run data sheet</h1><div class="run">__RUN__</div>
<p class="help">__MESSAGE__</p>
<div class="toolbar"><label>Temperature unit<select id="unit">
<option value="F" __F_SELECTED__>°F</option><option value="C" __C_SELECTED__>°C</option>
</select></label><span class="help">Pre-weld, then cooling readings. Height and width are in mm.</span></div>
<fieldset id="entry" __DISABLED__>
<div class="toolbar"><button id="save-sensed-heights" type="button" hidden>Save sensed heights</button>
<span id="sensed-save-status" class="help" role="status"></span></div>
<div class="table-wrap" tabindex="0" role="region" aria-label="Layer measurements; scroll horizontally for more columns">
<table><thead><tr><th scope="col">Layer</th><th scope="col">Pass (1 = first)</th>
<th scope="col">Pre-weld <span class="temp-unit">°__UNIT__</span><br>(before this layer)</th>__READINGS__
<th scope="col">Est. park<br>travel s</th><th scope="col">Height mm</th><th scope="col">Width mm</th>
<th scope="col">Note</th><th scope="col">Save status</th></tr></thead><tbody id="layer-rows">__ROWS__</tbody></table></div>
<div class="toolbar"><label>Layer<input id="new-layer" type="number" min="0" step="1" inputmode="numeric" value="0"></label>
<label>Pass<input id="new-pass" type="number" min="1" step="1" inputmode="numeric" value="1"></label>
<button id="add-row" type="button">Add row</button><span id="add-status" role="status"></span></div>
</fieldset>
<section class="decay" aria-labelledby="decay-heading"><h2 id="decay-heading">Cooling decay fit</h2>
<div id="decay-status" role="status">Enter at least two cooling readings in a row to fit a decay time.</div>
<div id="decay-results"></div></section>
</main><script>
'use strict';
const WB_K = new URL(window.location.href).searchParams.get('k') || '';
const INITIAL = __INITIAL__;
const states = [];
const times = INITIAL.times;
const tbody = document.getElementById('layer-rows');
const unitSelect = document.getElementById('unit');
const sensedButton = document.getElementById('save-sensed-heights');
const sensedStatus = document.getElementById('sensed-save-status');
let displayUnit = INITIAL.unit;
let decayGeneration = 0;
let decayTimer;

function api(path) {
  const url = new URL(path, window.location.origin);
  if (WB_K) url.searchParams.set('k', WB_K);
  if (INITIAL.workspace) url.searchParams.set('ws', INITIAL.workspace);
  if (INITIAL.run) url.searchParams.set('run', INITIAL.run);
  return url;
}
document.getElementById('workbench-link').href = api('/').href;

function convert(value, source, target) {
  if (value === null || value === undefined || source === target) return value;
  return target === 'C' ? (value - 32) * 5 / 9 : value * 9 / 5 + 32;
}
function numberText(value) {
  return value === null || value === undefined ? '' : String(Number(value.toPrecision(12)));
}
function setStatus(state, text, error = false) {
  const status = state.element.querySelector('.row-status');
  status.textContent = text;
  status.classList.toggle('error', error);
}
function refreshSensedButton() {
  sensedButton.hidden = !states.some((state) => state.row.height_unsaved);
}
function drawTemperatures(state) {
  state.element.querySelector('[data-field="pre_weld"]').value = numberText(
    convert(state.row.pre_weld, state.row.unit, displayUnit));
  for (const input of state.element.querySelectorAll('[data-field^="reading:"]')) {
    const seconds = input.dataset.field.slice(8);
    input.value = numberText(convert(state.row.readings[seconds], state.row.unit, displayUnit));
  }
}
async function request(path, options) {
  const response = await fetch(api(path), {credentials: 'same-origin', ...options});
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The server could not answer. Sign in or retry the change.'); }
  if (!response.ok || !result.ok) throw new Error(result.error || 'The request failed.');
  return result;
}
async function refreshDecay() {
  if (!INITIAL.open) return;
  const generation = ++decayGeneration;
  try {
    const result = await request('/api/run/decay');
    if (generation !== decayGeneration) return;
    const fit = result.fit;
    const status = document.getElementById('decay-status');
    status.textContent = `Pooled τ: ${fit.tau_s.toFixed(1)} s · ${fit.count} usable row${fit.count === 1 ? '' : 's'}`;
    status.className = 'decay-summary';
    const results = document.getElementById('decay-results');
    results.replaceChildren();
    if (Number.isFinite(fit.spread_fraction)) {
      const spread = document.createElement('p');
      spread.textContent = `Relative spread: ${(fit.spread_fraction * 100).toFixed(1)}%.`;
      results.append(spread);
    }
    const list = document.createElement('ul');
    for (const row of fit.rows || []) {
      const item = document.createElement('li');
      item.textContent = `Layer ${row.layer}${row.pass === null || row.pass === undefined ? '' : `, pass ${row.pass}`}: τ ${row.tau_s.toFixed(1)} s`;
      list.append(item);
    }
    results.append(list);
    for (const note of fit.notes || []) {
      const paragraph = document.createElement('p');
      paragraph.textContent = note;
      results.append(paragraph);
    }
  } catch (error) {
    if (generation !== decayGeneration) return;
    document.getElementById('decay-status').textContent = error.message;
    document.getElementById('decay-status').className = 'help';
    document.getElementById('decay-results').replaceChildren();
  }
}
function queueDecay() {
  clearTimeout(decayTimer);
  decayTimer = setTimeout(refreshDecay, 150);
}
async function save(state) {
  state.pending = true;
  if (state.saving) return;
  state.saving = true;
  while (state.pending) {
    state.pending = false;
    const version = state.version;
    const payload = {...state.row, readings: {...state.row.readings}};
    delete payload.at;
    delete payload.height_unsaved;
    setStatus(state, 'Saving…');
    try {
      await request('/api/run/layer-row', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      state.row.height_unsaved = false;
      state.element.querySelector('.row-status').classList.remove('sensed-unsaved');
      refreshSensedButton();
      if (version === state.version) {
        if (state.dirty.size === 0) setStatus(state, 'Saved');
        else setStatus(state, 'Some cells are unsaved. Check their values.', true);
      }
      queueDecay();
    } catch (error) {
      if (version === state.version) {
        setStatus(state, error.message + ' Change a cell to retry.', true);
      }
    }
  }
  state.saving = false;
}
function bindRow(row, element) {
  const state = {row: {...row, readings: {...row.readings}}, element,
    pending: false, saving: false, version: 0, dirty: new Set()};
  states.push(state);
  element.addEventListener('input', (event) => {
    state.dirty.add(event.target.dataset.field);
    state.version += 1;
    setStatus(state, 'Unsaved');
  });
  element.addEventListener('change', (event) => {
    const input = event.target;
    const field = input.dataset.field;
    if (!field) return;
    if (!input.reportValidity()) { setStatus(state, 'Check this value.', true); return; }
    state.dirty.delete(field);
    const value = input.value === '' ? null : Number(input.value);
    if (field === 'pre_weld' || field.startsWith('reading:')) {
      if (state.row.unit !== displayUnit) {
        state.row.pre_weld = convert(state.row.pre_weld, state.row.unit, displayUnit);
        for (const seconds of Object.keys(state.row.readings)) {
          state.row.readings[seconds] = convert(state.row.readings[seconds], state.row.unit, displayUnit);
        }
        state.row.unit = displayUnit;
      }
      if (field === 'pre_weld') state.row.pre_weld = value;
      else if (value === null) delete state.row.readings[field.slice(8)];
      else state.row.readings[field.slice(8)] = value;
    } else if (field === 'note') state.row.note = input.value;
    else {
      state.row[field] = value;
      if (field === 'height_mm') {
        state.row.height_source = value === null ? null : 'manual';
        element.querySelector('.source').textContent = state.row.height_source || '';
      }
    }
    state.version += 1;
    save(state);
  });
  return state;
}
INITIAL.rows.forEach((row, index) => bindRow(row, tbody.querySelector(`[data-row="${index}"]`)));
refreshSensedButton();
sensedButton.addEventListener('click', async () => {
  sensedButton.disabled = true;
  sensedStatus.textContent = 'Saving sensed heightsâ€¦';
  for (const state of states.filter((item) => item.row.height_unsaved)) await save(state);
  const remaining = states.filter((state) => state.row.height_unsaved).length;
  sensedStatus.textContent = remaining ? `${remaining} sensed height${remaining === 1 ? '' : 's'} could not be saved.`
    : 'Sensed heights saved.';
  sensedButton.disabled = false;
  refreshSensedButton();
});
unitSelect.addEventListener('change', () => {
  const incomplete = states.find((state) => state.dirty.size > 0);
  if (incomplete) {
    unitSelect.value = displayUnit;
    setStatus(incomplete, 'Finish or correct this row before changing units.', true);
    return;
  }
  displayUnit = unitSelect.value;
  for (const state of states) drawTemperatures(state);
  document.querySelectorAll('.temp-unit').forEach((label) => { label.textContent = '°' + displayUnit; });
});

function numericInput(field, value, label, temperature = false) {
  const input = document.createElement('input');
  input.type = 'number'; input.dataset.field = field; input.value = numberText(value);
  input.min = temperature ? '-50' : '0'; input.max = temperature ? '2000' : '200';
  input.step = 'any'; input.inputMode = 'decimal'; input.setAttribute('aria-label', label);
  return input;
}
function newRow(row) {
  const element = document.createElement('tr');
  const layer = document.createElement('th');
  layer.scope = 'row'; layer.textContent = String(row.layer);
  element.append(layer);
  const append = (content) => {
    const cell = document.createElement('td');
    cell.append(content); element.append(cell); return cell;
  };
  append(String(row.pass));
  append(numericInput('pre_weld', null, `Pre-weld temperature before layer ${row.layer}`, true));
  for (const seconds of times) {
    append(numericInput('reading:' + seconds, null, `Layer ${row.layer} temperature at ${seconds} seconds`, true));
  }
  append(numberText(row.park_travel_s_est) || '—').className = 'park';
  const height = append(numericInput('height_mm', row.height_mm, `Layer ${row.layer} height mm`));
  const source = document.createElement('span'); source.className = 'source';
  source.textContent = row.height_source || ''; height.append(source);
  append(numericInput('width_mm', null, `Layer ${row.layer} width mm`));
  const note = document.createElement('input'); note.type = 'text'; note.dataset.field = 'note';
  note.maxLength = 200; note.setAttribute('aria-label', `Layer ${row.layer} note`); append(note);
  const status = document.createElement('span'); status.className = 'row-status';
  status.setAttribute('role', 'status'); status.textContent = 'Not saved'; append(status);
  tbody.append(element);
  return bindRow(row, element);
}
document.getElementById('add-row').addEventListener('click', () => {
  const layerInput = document.getElementById('new-layer');
  const passInput = document.getElementById('new-pass');
  const status = document.getElementById('add-status');
  if (!layerInput.reportValidity() || !passInput.reportValidity()
      || layerInput.value === '' || passInput.value === '') {
    status.textContent = 'Enter a layer and pass number.'; return;
  }
  const layer = Number(layerInput.value);
  const pass = Number(passInput.value);
  const existing = states.find((state) => state.row.layer === layer && state.row.pass === pass);
  if (existing) {
    status.textContent = 'This layer/pass already has a row.';
    existing.element.querySelector('input').focus(); return;
  }
  const scheduled = states.find((state) => state.row.layer === layer && state.row.pass === null);
  const sensed = scheduled && scheduled.row.height_source === 'sense';
  const state = newRow({layer, pass, pass_base: 1, unit: displayUnit, pre_weld: null, readings: {},
    park_travel_s_est: scheduled ? scheduled.row.park_travel_s_est : null,
    height_mm: sensed ? scheduled.row.height_mm : null,
    height_source: sensed ? 'sense' : null, width_mm: null, note: ''});
  status.textContent = `Added layer ${layer}, pass ${pass}.`;
  passInput.value = String(pass + 1);
  state.element.querySelector('input').focus();
  save(state);
});
window.addEventListener('beforeunload', (event) => {
  if (states.some((state) => state.saving || state.pending || state.dirty.size > 0 ||
      state.element.querySelector('.row-status').classList.contains('error'))) {
    event.preventDefault(); event.returnValue = '';
  }
});
refreshDecay();
</script></body></html>'''
