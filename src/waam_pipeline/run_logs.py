def render_run_logs() -> str:

    return _PAGE


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team run logs · WAAM</title>
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #19313d; background: #f3f6f7; }
* { box-sizing: border-box; } body { margin: 0; } [hidden] { display: none !important; }
header { background: #fff; border-bottom: 1px solid #c5d4da; padding: 20px max(20px, calc((100vw - 1480px)/2)); }
header p { margin: 6px 0 0; } a { color: #09657c; } h1 { margin: 12px 0 0; font-size: 1.8rem; }
h2 { margin: 0 0 12px; font-size: 1.25rem; } h3 { font-size: 1rem; } p { line-height: 1.5; }
main { max-width: 1520px; margin: auto; padding: 24px 20px; }
.layout { display: grid; grid-template-columns: 285px minmax(0, 1fr); gap: 24px; align-items: start; }
.layout > *, .grid > *, .card, form, label { min-width: 0; }
.card { background: white; border: 1px solid #c5d4da; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
.help, .muted { color: #536671; font-size: .9rem; } .help { margin: 8px 0 16px; }
.eyebrow { text-transform: uppercase; letter-spacing: .1em; font-size: .72rem; font-weight: 700; color: #12667c; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.two { grid-template-columns: repeat(2, minmax(0, 1fr)); } .full { grid-column: 1 / -1; }
label { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; font-size: .9rem; } input, select, textarea, button { font: inherit; }
input, select, textarea { width: 100%; min-width: 0; min-height: 44px; border: 1px solid #98aeb8; border-radius: 6px; padding: 10px; background: #fff; color: #19313d; }
textarea { resize: vertical; min-height: 80px; } input[type=checkbox] { width: 20px; min-height: 20px; margin: 0; }
.check { display: flex; align-items: center; gap: 10px; } button, .button { display: inline-block; min-height: 44px; border: 1px solid #12667c; border-radius: 6px; padding: 10px 16px; background: #12667c; color: white; font-weight: 600; cursor: pointer; text-decoration: none; }
button.secondary, .button.secondary { background: white; color: #09657c; } button:disabled { opacity: .55; cursor: default; }
:focus-visible { outline: 3px solid #00a9bb; outline-offset: 3px; } fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 16px; }
.actions > * { min-width: 0; max-width: 100%; }
.status { margin: 0; font-size: .9rem; overflow-wrap: anywhere; } .error { color: #a01f24; } .success { color: #18613c; }
.notice { padding: 12px 16px; border-radius: 6px; background: #e7f3f5; margin: 0 0 20px; }
.run-list { display: grid; gap: 8px; margin-top: 16px; max-height: 580px; overflow-y: auto; }
.run-item { width: 100%; text-align: left; background: white; color: #19313d; border-color: #c5d4da; overflow-wrap: anywhere; }
.run-item[aria-current=true] { background: #e7f3f5; border-color: #12667c; box-shadow: inset 3px 0 #12667c; }
.run-item span { display: block; } .run-item .muted { font-size: .78rem; font-weight: 400; margin-top: 4px; }
.run-title { overflow-wrap: anywhere; } .badge { font-size: .8rem; border-radius: 20px; padding: 5px 10px; background: #e7eff2; }
.summary-line { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.table-wrap { overflow-x: auto; margin-top: 16px; } table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; vertical-align: top; padding: 11px 10px; border-bottom: 1px solid #dce4e8; overflow-wrap: anywhere; }
th { background: #f0f5f7; white-space: nowrap; } td { min-width: 80px; } td button { padding: 6px 10px; }
details > summary { cursor: pointer; font-weight: 650; line-height: 1.5; } details[open] > summary { margin-bottom: 16px; }
dl { display: grid; grid-template-columns: 160px minmax(0, 1fr); margin: 12px 0; gap: 8px 16px; font-size: .9rem; }
dt { color: #536671; } dd { margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }
.reading { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap: 12px; align-items: end; margin-bottom: 12px; }
.notes { margin: 12px 0; padding: 0; list-style: none; } .notes li { border-bottom: 1px solid #dce4e8; padding: 12px 0; }
.notes p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0; } .notes small { color: #536671; }
.empty { padding: 30px 10px; text-align: center; color: #536671; } #new-panel { max-width: 100%; }
.excel-steps { padding-left: 22px; line-height: 1.6; } .feed-link { display: grid; gap: 6px; }
.feed-link input { font-size: .85rem; } .secret { background: #f0f7f8; padding: 16px; border-radius: 8px; margin-top: 16px; }
@media (max-width: 1000px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .layout { grid-template-columns: 245px minmax(0, 1fr); gap: 16px; } }
@media (max-width: 760px) { main { padding: 16px 12px; } header { padding: 16px; } .layout, .grid { grid-template-columns: minmax(0, 1fr); } .card { padding: 16px; } .run-list { max-height: 230px; } dl { grid-template-columns: 110px minmax(0,1fr); } .reading { gap: 8px; } .reading button { padding: 10px; } }
</style></head><body>
<header><a href="/auth/logout">Sign out</a><div class="eyebrow" style="margin-top:16px">WAAM · Team records</div>
<h1>Run logs</h1><p class="muted">Record the setup, measurements and observations for one part on one plate.</p><p id="account" class="muted"></p></header>
<main><noscript><p class="notice">JavaScript is required to load and save run logs.</p></noscript>
<p id="page-status" class="status notice" role="status" aria-live="polite">Loading your runs…</p>
<details id="excel-panel" class="card"><summary>Connect Excel · no VPN needed</summary>
<p class="help">Import your accessible run records into Excel and refresh them over HTTPS. Excel access is read only and uses a separate password for your account.</p>
<ol class="excel-steps"><li>Create an Excel password below.</li><li>In Excel, choose <strong>Data → Get Data → From Web</strong> and paste a feed URL.</li><li>Choose <strong>Basic</strong> authentication. Enter the username and generated Excel password shown here, then load the table.</li><li>Repeat for the feeds you need. Use <strong>Refresh All</strong> to bring in saved run entries.</li></ol>
<p id="excel-state" class="help">Open this panel to check your Excel connection.</p>
<div class="actions"><button id="excel-create" type="button" disabled>Create Excel password</button><button id="excel-revoke" type="button" class="secondary" disabled>Revoke Excel access</button><button id="excel-refresh" type="button" class="secondary" disabled>Refresh connection status</button></div>
<p id="excel-status" class="status" role="status" aria-live="polite" style="margin-top:12px"></p>
<div id="excel-credentials" hidden><label style="margin-top:16px">Excel username<input id="excel-username" type="text" readonly autocomplete="off" spellcheck="false"></label>
<div id="excel-secret" class="secret" hidden><label>Excel password · shown once<input id="excel-password" type="text" readonly autocomplete="off" spellcheck="false"></label><p class="help">Copy this password into Excel before closing this panel or refreshing its status. It cannot be displayed again; replacing it stops the old password from working.</p><button id="excel-hide" type="button" class="secondary">Clear password from this page</button></div></div>
<div id="excel-feeds" class="grid two" style="margin-top:20px"></div>
</details>
<div class="layout"><aside class="card" aria-label="Find a run">
<h2>Your team’s runs</h2><form id="search-form"><label>Search runs<input id="search" type="search" maxlength="120" placeholder="Name, specimen or material"></label>
<label style="margin-top:12px">Status<select id="status-filter"><option value="">All runs</option><option value="open">Open</option><option value="finished">Finished</option></select></label>
<div class="actions"><button type="submit" class="secondary">Search</button><button id="new-button" type="button">New run</button></div></form>
<p id="list-status" class="help" role="status"></p><div id="run-list" class="run-list"></div></aside>
<div>
<section id="new-panel" class="card" hidden><h2>Start a run</h2><p class="help">Use a name your teammates will recognize. Setup fields are optional; enter the values actually used.</p>
<form id="start-form" data-draft><fieldset><div class="grid two"><label>Run name<input name="name" required maxlength="40" autocomplete="off" placeholder="e.g. Wall specimen 03"></label>
<label>Collaborator emails<input name="collaborators" maxlength="4000" placeholder="Separate addresses with commas"></label></div>
<p class="help">Collaborators can add measurements, notes and files. This grants access to existing accounts; no invitations are sent.</p>
<details><summary>Record the initial setup</summary><div id="start-setup" class="grid"></div></details>
<div class="actions"><button type="submit">Start run</button><button id="cancel-new" type="button" class="secondary">Cancel</button><p id="start-status" class="status" role="status"></p></div></fieldset></form></section>
<section id="welcome" class="card empty"><h2>Select a run to begin</h2><p>Choose a run on the left, or start a new run for the next part.</p></section>
<div id="detail" hidden>
<section class="card"><div class="summary-line"><span id="run-state" class="badge"></span><span id="run-class" class="badge"></span></div>
<h2 id="run-title" class="run-title"></h2><p id="run-meta" class="help"></p><p id="permission-note" class="notice"></p>
<div class="actions"><a id="csv-link" class="button secondary" href="/api/run-logs/export">Download CSV</a><button id="reload-button" type="button" class="secondary">Load latest saved record</button></div>
<p id="conflict-note" class="help" hidden>Another teammate changed this run. Your entries are still here. Load the latest saved record, review it, then save again.</p>
<details style="margin-top:20px"><summary>Saved setup and outcome</summary><dl id="setup-summary"></dl></details></section>
<section class="card"><h2>Layer / pass measurements</h2><p class="help">Layer 0 is the first layer. Pass 1 is the first pass; leave pass blank for the whole layer. Pre-weld is taken before this layer. Cooling readings belong to this layer after welding; time starts on arrival at park.</p>
<form id="layer-form" data-draft data-editor><fieldset><div class="grid">
<label>Layer (0 = first)<input name="layer" type="number" min="0" step="1" required inputmode="numeric"></label>
<label>Pass (optional)<input name="pass" type="number" min="1" step="1" inputmode="numeric"></label>
<label>Temperature unit<select name="unit"><option value="F">°F</option><option value="C">°C</option></select></label>
<label>Pre-weld temperature<input name="pre_weld" type="number" min="-50" max="2000" step="any" inputmode="decimal"></label>
<label>Measured height (mm)<input name="height_mm" type="number" min="0" max="200" step="any" inputmode="decimal"></label>
<label>Measured width (mm)<input name="width_mm" type="number" min="0" max="200" step="any" inputmode="decimal"></label>
<label>Measured current (A)<input name="current_a" type="number" min="0" max="2000" step="any" inputmode="decimal"></label>
<label>Measured voltage (V)<input name="voltage_v" type="number" min="0" max="200" step="any" inputmode="decimal"></label>
<label>Estimated travel to park (s)<input name="park_travel_s_est" type="number" min="0" max="7200" step="any" inputmode="decimal"></label>
<label class="full">Layer / pass note<input name="note" maxlength="200"></label></div>
<h3>Timed cooling readings <span class="muted">(optional, in the selected unit)</span></h3><div id="readings"></div><button id="add-reading" class="secondary" type="button">Add cooling reading</button>
<div class="actions"><button type="submit">Save measurement</button><button id="clear-layer" type="button" class="secondary">Clear form</button><p id="layer-status" class="status" role="status"></p></div></fieldset></form>
<p class="help">Saving the same layer and pass updates that row. Use Edit to load all its existing readings before making a correction.</p>
<div class="table-wrap"><table><thead><tr><th>Layer / pass</th><th>Pre-weld</th><th>Cooling</th><th>Height / width</th><th>Measured A / V</th><th>Note</th><th>Saved</th><th>Action</th></tr></thead><tbody id="layer-rows"></tbody></table></div></section>
<section class="card"><h2>Temperature observation</h2><form id="temperature-form" data-draft data-editor><fieldset><div class="grid">
<label>Temperature<input name="value" type="number" required min="-40" max="2500" step="any" inputmode="decimal"></label>
<label>Unit<select name="unit"><option value="F">°F</option><option value="C">°C</option></select></label>
<label>Stage<select name="stage"><option value="before-weld">Before weld</option><option value="after-weld">After weld</option><option value="before-interpass">Before interpass</option><option value="after-interpass">After interpass</option><option value="other">Other</option></select></label>
<label class="full">Observation note<input name="note" maxlength="200"></label></div><div class="actions"><button type="submit">Save temperature</button><button type="button" class="secondary" data-reset="temperature-form">Clear form</button><p id="temperature-status" class="status" role="status"></p></div></fieldset></form>
<div class="table-wrap"><table><thead><tr><th>Temperature</th><th>Stage</th><th>Note</th><th>Saved</th></tr></thead><tbody id="temperature-rows"></tbody></table></div></section>
<section class="card"><h2>Notes</h2><form id="note-form" data-draft data-editor><fieldset><label>What happened during this run?<textarea name="text" required maxlength="2000"></textarea></label><div class="actions"><button type="submit">Save note</button><button type="button" class="secondary" data-reset="note-form">Clear form</button><p id="note-status" class="status" role="status"></p></div></fieldset></form><ul id="notes" class="notes"></ul></section>
<section class="card"><h2>Files and arc monitor data</h2><p class="help">Attach photographs, measurements or supporting files. Each file may be up to 25 MB. Select Arc monitor for the controller’s arc CSV or ARCMON.DAT export.</p>
<form id="upload-form" data-draft data-editor><fieldset><div class="grid two"><label>File type<select name="kind"><option value="attachment">Supporting attachment</option><option value="arcmon">Arc monitor</option></select></label><label>Choose file<input name="file" type="file" required></label></div>
<div class="actions"><button type="submit">Upload file</button><button type="button" class="secondary" data-reset="upload-form">Clear form</button><p id="upload-status" class="status" role="status"></p></div></fieldset></form>
<div class="table-wrap"><table><thead><tr><th>File</th><th>Kind</th><th>Size</th><th>Added</th></tr></thead><tbody id="file-rows"></tbody></table></div></section>
<details id="manage-panel" class="card" hidden><summary>Setup and sharing</summary><p class="help">The run owner or an administrator can update the setup and collaborators. These values describe the recorded experiment.</p>
<form id="setup-form" data-draft data-editor data-manager><fieldset><div id="edit-setup" class="grid"></div><label style="margin-top:14px">Collaborator emails<input name="collaborators" maxlength="4000" placeholder="Separate addresses with commas"></label><p class="help">No invitations are sent. Teammates sign in with their own accounts.</p><div class="actions"><button type="submit">Save setup and sharing</button><button type="button" class="secondary" data-reset="setup-form">Discard changes</button><p id="setup-status" class="status" role="status"></p></div></fieldset></form></details>
<details id="finish-panel" class="card" hidden><summary>Finish run</summary><p class="help">Finish seals the record. Measurements, notes and files cannot be changed afterwards. Save all other entries first. A completed or stopped run requires arc monitor data or a recorded waiver.</p>
<form id="finish-form" data-draft data-editor data-manager><fieldset><div class="grid two">
<label>Outcome<select name="outcome"><option value="completed">Completed</option><option value="stopped">Stopped early</option><option value="not-run">Not run</option></select></label>
<label>Data class<select name="data_class"><option value="preliminary">Preliminary</option><option value="controlled">Controlled</option></select></label>
<label>Final measured height (mm)<input name="height_mm" type="number" min="0" step="any" inputmode="decimal"></label><label>Final measured width (mm)<input name="width_mm" type="number" min="0" step="any" inputmode="decimal"></label>
<label class="full">Finish notes<textarea name="notes" maxlength="2000"></textarea></label>
<label class="check full"><input name="arcmon_waiver" type="checkbox">Arc monitor export not set up yet (record a waiver)</label>
<label class="full">Waiver reason (optional)<input name="waiver_reason" maxlength="2000"></label></div><div class="actions"><button type="submit">Finish and seal run</button><button type="button" class="secondary" data-reset="finish-form">Clear form</button><p id="finish-status" class="status" role="status"></p></div></fieldset></form></details>
<details class="card"><summary>Record history</summary><div class="table-wrap"><table><thead><tr><th>Action</th><th>By</th><th>Time</th></tr></thead><tbody id="audit-rows"></tbody></table></div></details>
</div></div></div></main>
<script>
'use strict';
const $ = id => document.getElementById(id);
const SETUP_FIELDS = [
  ['specimen_id', 'Specimen ID'], ['substrate_id', 'Substrate / plate ID'], ['material', 'Substrate material'],
  ['wire_material', 'Wire material'], ['wire_diameter_mm', 'Wire diameter (mm)', true], ['shielding_gas', 'Shielding gas'],
  ['gas_flow_l_min', 'Gas flow (L/min)', true], ['travel_mm_min', 'Travel speed (mm/min)', true],
  ['wire_feed_m_min', 'Wire feed (m/min)', true], ['voltage_v', 'Set voltage (V)', true],
  ['ctwd_mm', 'Contact tip to work (mm)', true], ['arc_file', 'Arc start file / schedule']
];
const dirty = new Set();
const state = {detail: null, workspace: null, busy: false, listRequest: 0, layerHeightSource: null};
const excelState = {access: null, busy: false, secretGeneration: 0};
const draftForms = [...document.querySelectorAll('form[data-draft]')];
function field(form, name) { return form.elements.namedItem(name); }
function value(form, name) { return field(form, name).value.trim(); }
function number(form, name) { const text = value(form, name); return text === '' ? null : Number(text); }
function text(node, content) { node.textContent = content == null || content === '' ? '—' : String(content); }
function date(value) { if (!value) return '—'; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(); }
function status(id, message, error = false) { const node = $(id); node.textContent = message; node.classList.toggle('error', error); node.classList.toggle('success', !error && message.startsWith('Saved')); }
function markDirty(form) { dirty.add(form.id); status(form.id.replace('-form', '-status'), 'Unsaved changes'); }
function discardDrafts() { for (const form of draftForms) resetForm(form); }
function resetForm(form) {
  form.reset(); dirty.delete(form.id); status(form.id.replace('-form', '-status'), '');
  if (form.id === 'layer-form') { $('readings').replaceChildren(); state.layerHeightSource = null; }
}
function confirmDiscard(message = 'Discard unsaved entries and switch runs?') { return !dirty.size || window.confirm(message); }
function updateLocks() {
  const open = state.detail?.record.status === 'open';
  for (const form of draftForms) form.querySelector('fieldset').disabled = !state.workspace || state.busy ||
    (form.hasAttribute('data-editor') && (!open || (form.hasAttribute('data-manager') && !state.detail?.can_manage)));
  $('new-button').disabled = !state.workspace || state.busy; $('reload-button').disabled = state.busy;
  for (const button of document.querySelectorAll('.run-item, .edit-row')) button.disabled = state.busy;
  updateExcelLocks();
}
function url(path, parameters = {}) {
  const result = new URL(path, window.location.origin);
  if (path.startsWith('/api/run-logs') && state.workspace) result.searchParams.set('ws', state.workspace);
  for (const [key, item] of Object.entries(parameters)) if (item != null) result.searchParams.set(key, item);
  return result;
}
async function request(path, options = {}, parameters = {}) {
  if (path.startsWith('/api/run-logs') && !state.workspace) throw new Error('Sign-in could not be verified. Reload this page after signing in.');
  const response = await fetch(url(path, parameters), {credentials: 'same-origin', cache: 'no-store', ...options});
  let body;
  try { body = await response.json(); } catch { throw new Error('The server did not return a run record. Check your sign-in and try again.'); }
  if (!response.ok || !body.ok) {
    if (response.status === 403) clearExcelSecret();
    const error = new Error(body.error || 'The request could not be saved.'); error.status = response.status; error.saveUncertain = body.save_uncertain; throw error;
  }
  return body;
}
function clearExcelSecret() { $('excel-password').value = ''; $('excel-secret').hidden = true; excelState.secretGeneration++; }
function updateExcelLocks() {
  const blocked = !state.workspace || excelState.busy;
  $('excel-refresh').disabled = blocked;
  $('excel-create').disabled = blocked || !excelState.access;
  $('excel-revoke').disabled = blocked || !excelState.access?.enabled;
  $('excel-create').textContent = excelState.access?.enabled ? 'Replace Excel password' : 'Create Excel password';
}
function renderExcelAccess(data, reveal = false) {
  clearExcelSecret();
  const links = [];
  for (const [key, label] of [['runs', 'Runs and setup'], ['layers', 'Layer / pass measurements'], ['cooling', 'Cooling readings'], ['temperatures', 'Temperature observations']]) {
    if (!data.feeds?.[key]) continue;
    const address = new URL(data.feeds[key], window.location.origin);
    if (address.protocol !== 'https:' || address.origin !== window.location.origin || address.username || address.password) {
      throw new Error('Excel feed URLs must use HTTPS on this WAAM site. Open the public HTTPS site and refresh connection status.');
    }
    links.push({label, address: address.href});
  }

  excelState.access = {enabled: Boolean(data.enabled), created_at: data.created_at, username: data.username, feeds: data.feeds};
  $('excel-credentials').hidden = !data.enabled;
  $('excel-username').value = data.username || '';
  text($('excel-state'), data.enabled ? `Excel access enabled${data.created_at ? ' · Created ' + date(data.created_at) : ''}. Each feed contains the runs your account can access.` : 'Excel access is disabled. Create a password when you are ready to connect.');
  const feeds = $('excel-feeds'); feeds.replaceChildren();
  if (data.enabled) for (const {label, address} of links) {
    const container = document.createElement('label'); container.className = 'feed-link';
    const link = document.createElement('a'); link.textContent = label; link.href = address; link.target = '_blank'; link.rel = 'noopener noreferrer';
    const input = document.createElement('input'); input.type = 'text'; input.readOnly = true; input.value = address; input.setAttribute('aria-label', `${label} feed URL`);
    container.append(link, input); feeds.append(container);
  }
  if (data.enabled && reveal && typeof data.password === 'string' && data.password) {
    $('excel-password').value = data.password; $('excel-secret').hidden = false;
  }
  updateExcelLocks();
}
async function loadExcelAccess() {
  if (excelState.busy || !state.workspace) return;
  clearExcelSecret(); excelState.busy = true; updateExcelLocks(); status('excel-status', 'Checking Excel access…');
  try {
    renderExcelAccess(await request('/api/run-logs/excel-access'));
    status('excel-status', 'Connection status loaded.');
  } catch (error) {
    excelState.access = null; $('excel-credentials').hidden = true; $('excel-feeds').replaceChildren();
    status('excel-status', error.message, true);
  } finally { excelState.busy = false; updateExcelLocks(); }
}
async function changeExcelAccess(action) {
  if (excelState.busy || !state.workspace || !excelState.access) return;
  if (action === 'create' && excelState.access.enabled && !window.confirm('Replace your Excel password? Workbooks using the old password will stop refreshing until their credentials are updated.')) return;
  if (action === 'revoke' && !window.confirm('Revoke Excel access? Workbooks using this password will stop refreshing.')) return;
  clearExcelSecret(); excelState.busy = true; updateExcelLocks();
  const generation = excelState.secretGeneration;
  status('excel-status', action === 'create' ? 'Creating your Excel password…' : 'Revoking Excel access…');
  try {
    const data = await request('/api/run-logs/excel-access', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})});
    const reveal = action === 'create' && generation === excelState.secretGeneration && $('excel-panel').open;
    renderExcelAccess(data, reveal);
    status('excel-status', action === 'create' ? reveal ? 'Excel password created. Copy it below before leaving this panel.' :
      'Excel password created while this panel was closed. Replace it to display a new password.' : 'Excel access revoked. The previous password no longer works.');
  } catch (error) {
    const uncertain = !error.status || error.status >= 500;
    if (uncertain) excelState.access = null;
    status('excel-status', uncertain ? `Change not confirmed: ${error.message} Refresh connection status before trying again.` : error.message, true);
  } finally { excelState.busy = false; updateExcelLocks(); }
}
function setupInputs(container) {
  for (const [name, labelText, numeric] of SETUP_FIELDS) {
    const label = document.createElement('label'); label.textContent = labelText;
    const input = document.createElement('input'); input.name = name;
    if (numeric) { input.type = 'number'; input.min = '0'; input.step = 'any'; input.inputMode = 'decimal'; }
    else { input.type = 'text'; input.maxLength = 120; }
    label.append(input); container.append(label);
  }
}
function setupData(form) {
  const result = {};
  for (const [name, , numeric] of SETUP_FIELDS) {
    const entry = numeric ? number(form, name) : value(form, name);
    if (entry !== null && entry !== '') result[name] = entry;
    else if (form.id === 'setup-form') result[name] = null;
  }
  return {setup: result, collaborators: value(form, 'collaborators').split(',').map(email => email.trim()).filter(Boolean)};
}
function loadSetup(record) {
  const form = $('setup-form');
  for (const [name] of SETUP_FIELDS) field(form, name).value = record.setup?.[name] ?? '';
  field(form, 'collaborators').value = (record.collaborators || []).join(', ');
}
function appendCells(body, contents) {
  const row = document.createElement('tr');
  for (const item of contents) { const cell = document.createElement('td'); if (item instanceof Node) cell.append(item); else text(cell, item); row.append(cell); }
  body.append(row); return row;
}
function emptyTable(body, count, message) { const row = appendCells(body, [message]); row.firstChild.colSpan = count; row.firstChild.className = 'muted'; }
function temperature(value, unit) { return value == null ? '—' : `${value} °${unit}`; }
function renderTables(record) {
  const layerBody = $('layer-rows'); layerBody.replaceChildren();
  const rows = [...(record.layer_log || [])].sort((a, b) => a.layer - b.layer || (a.pass || 0) - (b.pass || 0));
  for (const row of rows) {
    const cooling = Object.entries(row.readings || {}).sort((a, b) => Number(a[0]) - Number(b[0])).map(([seconds, entry]) => `${seconds} s: ${temperature(entry, row.unit)}`).join('; ');
    const action = document.createElement('span'); action.textContent = 'Sealed';
    if (record.status === 'open') {
      const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'secondary edit-row'; edit.textContent = 'Edit';
      edit.setAttribute('aria-label', `Edit layer ${row.layer}, pass ${row.pass ?? 'whole layer'}`);
      edit.addEventListener('click', () => editLayer(row)); action.replaceChildren(edit);
    }
    appendCells(layerBody, [`${row.layer} / ${row.pass ?? 'all'}`, temperature(row.pre_weld, row.unit), cooling,
      `${row.height_mm ?? '—'} / ${row.width_mm ?? '—'} mm${row.height_source ? ' · ' + row.height_source : ''}`,
      `${row.current_a ?? '—'} A / ${row.voltage_v ?? '—'} V`, row.note, date(row.at), action]);
  }
  if (!rows.length) emptyTable(layerBody, 8, 'No layer measurements saved yet.');
  const temperatures = $('temperature-rows'); temperatures.replaceChildren();
  for (const row of record.temperatures || []) appendCells(temperatures, [temperature(row.value, row.unit), row.stage, row.note, date(row.at)]);
  if (!(record.temperatures || []).length) emptyTable(temperatures, 4, 'No temperature observations saved yet.');
  const files = $('file-rows'); files.replaceChildren();
  for (const file of record.files || []) appendCells(files, [file.name, file.kind, `${(file.bytes / 1024).toFixed(1)} KB`, date(file.added_at)]);
  if (!(record.files || []).length) emptyTable(files, 4, 'No files uploaded yet.');
  const notes = $('notes'); notes.replaceChildren();
  for (const note of record.notes || []) {
    const item = document.createElement('li'), body = document.createElement('p'), when = document.createElement('small');
    text(body, note.text); text(when, date(note.at)); item.append(body, when); notes.append(item);
  }
  if (!(record.notes || []).length) { const empty = document.createElement('li'); empty.textContent = 'No notes saved yet.'; empty.className = 'muted'; notes.append(empty); }
  const audit = $('audit-rows'); audit.replaceChildren();
  for (const entry of [...(record.audit || [])].reverse()) appendCells(audit, [entry.action, entry.by || entry.actor, date(entry.at)]);
  if (!(record.audit || []).length) emptyTable(audit, 3, 'No team log edits recorded yet.');
}
function renderDetail(detail, fresh = false) {
  state.detail = detail;
  const record = detail.record, open = record.status === 'open';
  $('detail').hidden = false; $('welcome').hidden = true;
  text($('run-title'), record.name || record.run); text($('run-state'), open ? 'Open run' : 'Finished · read only');
  text($('run-class'), record.data_class || 'preliminary');
  text($('run-meta'), `${record.run} · Started ${date(record.started_at)} · Owner: ${record.operator || record.owner || '—'}`);
  text($('permission-note'), !open ? 'This run is sealed. Saved measurements, notes, files and setup are available below.' :
    detail.can_manage ? 'You can record observations, manage sharing and finish this run.' : 'You can add measurements, notes and files. The owner or an administrator manages setup, sharing and finishing.');
  $('csv-link').href = url('/api/run-logs/export', {run: record.run}).href;
  $('manage-panel').hidden = !open || !detail.can_manage; $('finish-panel').hidden = !open || !detail.can_manage;
  const summary = $('setup-summary'); summary.replaceChildren();
  const pairs = SETUP_FIELDS.map(([name, label]) => [label, record.setup?.[name]]);
  pairs.push(['Collaborators', (record.collaborators || []).join(', ')], ['Outcome', record.outcome], ['Finished', record.finished_at ? date(record.finished_at) : null],
    ['Final height (mm)', record.measurements?.height_mm], ['Final width (mm)', record.measurements?.width_mm], ['Arc monitor waiver', record.arc_monitor?.waiver?.reason]);
  for (const [label, value] of pairs) { const term = document.createElement('dt'), definition = document.createElement('dd'); text(term, label); text(definition, value); summary.append(term, definition); }
  if (fresh) loadSetup(record);
  renderTables(record); updateLocks();
  for (const button of document.querySelectorAll('.run-item')) button.setAttribute('aria-current', String(button.dataset.run === record.run));
}
async function loadList() {
  const token = ++state.listRequest; status('list-status', 'Loading…');
  try {
    const data = await request('/api/run-logs', {}, {search: $('search').value.trim(), status: $('status-filter').value});
    if (token !== state.listRequest) return;
    const list = $('run-list'); list.replaceChildren();
    for (const run of data.runs) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'run-item'; button.dataset.run = run.run;
      button.setAttribute('aria-current', String(state.detail?.record.run === run.run));
      const title = document.createElement('span'), metadata = document.createElement('span');
      text(title, run.name || run.run); text(metadata, `${run.status === 'open' ? 'Open' : 'Finished'} · ${run.run}`); metadata.className = 'muted';
      button.append(title, metadata); button.addEventListener('click', () => selectRun(run.run)); list.append(button);
    }
    status('list-status', data.runs.length ? `${data.runs.length} accessible run${data.runs.length === 1 ? '' : 's'}` : 'No matching runs. Start one, or ask its owner to add your account as a collaborator.');
    updateLocks();
  } catch (error) { if (token === state.listRequest) status('list-status', error.message, true); }
}
async function selectRun(run) {
  if (state.busy || !confirmDiscard()) return;
  state.busy = true; updateLocks(); status('page-status', 'Loading run…');
  try {
    const detail = await request('/api/run-logs/detail', {}, {run});
    discardDrafts(); $('new-panel').hidden = true; $('conflict-note').hidden = true; renderDetail(detail, true);
    const location = new URL(window.location.href); location.searchParams.set('run', run); window.history.replaceState(null, '', location);
    status('page-status', 'Saved record loaded. Use the Save buttons to record your entries.');
  } catch (error) { status('page-status', error.message, true); }
  finally { state.busy = false; updateLocks(); }
}
async function save(form, operation, after) {
  if (state.busy) return;
  const target = form.id.replace('-form', '-status');
  state.busy = true; updateLocks(); status(target, 'Saving…');
  try {
    const detail = await operation();
    resetForm(form); renderDetail(detail); if (form.id === 'setup-form') loadSetup(detail.record);
    if (after) after(detail);
    $('conflict-note').hidden = true; status(target, 'Saved to this run.'); status('page-status', 'Saved. Your run record is up to date.');
    void loadList();
  } catch (error) {
    if (error.status === 409) $('conflict-note').hidden = false;
    const uncertain = error.saveUncertain || !error.status || error.status >= 500;
    if (uncertain) {
      const recovery = form.id === 'start-form' ? 'Refresh the run list and check for this run before retrying.' : 'Load the latest saved record and check before retrying.';
      status(target, `Save not confirmed: ${error.message} ${recovery} Your entries are still in the form.`, true);
      status('page-status', `The server may have saved this entry. ${recovery}`, true);
      if (form.id === 'start-form') void loadList();
    } else {
      status(target, `Not saved: ${error.message} Your entries are still in the form.`, true);
      status('page-status', error.status === 409 ? 'This run changed. Load the latest saved record before retrying; your unsaved entries will be kept.' : 'The entry was not saved. See the message beside the form.', true);
    }
  } finally { state.busy = false; updateLocks(); }
}
function update(action, data) {
  return request('/api/run-logs/update', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({revision: state.detail.revision, action, data})}, {run: state.detail.record.run});
}
function addReading(seconds = '', temperature = '') {
  const row = document.createElement('div'); row.className = 'reading';
  for (const [name, labelText, entry, min, max] of [['seconds', 'Seconds after park', seconds, 0, 7200], ['temperature', 'Temperature', temperature, -50, 2000]]) {
    const label = document.createElement('label'); label.textContent = labelText;
    const input = document.createElement('input'); input.type = 'number'; input.step = 'any'; input.min = String(min); input.max = String(max); input.required = true;
    input.dataset.reading = name; input.value = entry; input.inputMode = 'decimal'; label.append(input); row.append(label);
  }
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'secondary'; remove.textContent = 'Remove';
  remove.addEventListener('click', () => { row.remove(); markDirty($('layer-form')); }); row.append(remove); $('readings').append(row);
}
function layerData(form) {
  const readings = {};
  for (const row of $('readings').children) {
    const seconds = row.querySelector('[data-reading=seconds]').value.trim(), measured = row.querySelector('[data-reading=temperature]').value.trim();
    if (!seconds || !measured) throw new Error('Each cooling reading needs both seconds and temperature.');
    const key = String(Number(seconds));
    if (Object.hasOwn(readings, key)) throw new Error('Cooling reading times must be different.');
    readings[key] = Number(measured);
  }
  const height = number(form, 'height_mm');
  return {layer: number(form, 'layer'), pass: number(form, 'pass'), pass_base: 1, unit: value(form, 'unit'),
    pre_weld: number(form, 'pre_weld'), readings, park_travel_s_est: number(form, 'park_travel_s_est'),
    height_mm: height, height_source: height == null ? null : state.layerHeightSource || 'manual', width_mm: number(form, 'width_mm'),
    current_a: number(form, 'current_a'), voltage_v: number(form, 'voltage_v'), note: value(form, 'note')};
}
function editLayer(row) {
  if (state.busy || (dirty.has('layer-form') && !window.confirm('Discard the unsaved measurement and edit this saved row?'))) return;
  const form = $('layer-form'); resetForm(form);
  for (const key of ['layer', 'pass', 'unit', 'pre_weld', 'height_mm', 'width_mm', 'park_travel_s_est', 'current_a', 'voltage_v', 'note']) field(form, key).value = row[key] ?? '';
  for (const [seconds, reading] of Object.entries(row.readings || {})) addReading(seconds, reading);
  state.layerHeightSource = row.height_source || null; status('layer-status', 'Saved row loaded. Make your correction, then save.');
  field(form, 'layer').focus(); form.scrollIntoView({behavior: 'smooth', block: 'center'});
}
function onSubmit(id, handler) {
  $(id).addEventListener('submit', event => {
    event.preventDefault(); const form = event.currentTarget;
    if (state.busy || !form.reportValidity()) return;
    try { handler(form); } catch (error) { status(id.replace('-form', '-status'), error.message, true); }
  });
}
setupInputs($('start-setup')); setupInputs($('edit-setup'));
$('excel-panel').addEventListener('toggle', () => {
  if ($('excel-panel').open) { if (!excelState.access) void loadExcelAccess(); }
  else clearExcelSecret();
});
$('excel-create').addEventListener('click', () => { void changeExcelAccess('create'); });
$('excel-revoke').addEventListener('click', () => { void changeExcelAccess('revoke'); });
$('excel-refresh').addEventListener('click', () => { void loadExcelAccess(); });
$('excel-hide').addEventListener('click', clearExcelSecret);
for (const form of draftForms) {
  form.addEventListener('input', () => markDirty(form)); form.addEventListener('change', () => markDirty(form));
}
field($('layer-form'), 'height_mm').addEventListener('input', () => { state.layerHeightSource = 'manual'; });
$('add-reading').addEventListener('click', () => { addReading(); markDirty($('layer-form')); });
$('clear-layer').addEventListener('click', () => { if (!dirty.has('layer-form') || window.confirm('Discard the unsaved measurement?')) resetForm($('layer-form')); });
for (const button of document.querySelectorAll('[data-reset]')) button.addEventListener('click', () => {
  const form = $(button.dataset.reset);
  if (state.busy || (dirty.has(form.id) && !window.confirm('Discard the unsaved entries in this form?'))) return;
  resetForm(form); if (form.id === 'setup-form' && state.detail) loadSetup(state.detail.record);
});
$('search-form').addEventListener('submit', event => { event.preventDefault(); void loadList(); });
$('status-filter').addEventListener('change', () => { void loadList(); });
$('new-button').addEventListener('click', () => {
  if (!confirmDiscard('Discard unsaved entries and start a new run?')) return;
  discardDrafts(); state.detail = null; $('detail').hidden = true; $('welcome').hidden = true; $('new-panel').hidden = false;
  updateLocks(); field($('start-form'), 'name').focus();
});
$('cancel-new').addEventListener('click', () => {
  if (!confirmDiscard('Discard the new run details?')) return;
  resetForm($('start-form')); $('new-panel').hidden = true; $('welcome').hidden = false;
});
$('reload-button').addEventListener('click', async () => {
  if (state.busy || !state.detail) return;
  state.busy = true; updateLocks();
  try {
    const detail = await request('/api/run-logs/detail', {}, {run: state.detail.record.run});
    renderDetail(detail, !dirty.has('setup-form')); $('conflict-note').hidden = true;
    status('page-status', 'Latest saved record loaded. Your unsaved entries are kept; review the saved record before saving again.');
  } catch (error) { status('page-status', error.message, true); }
  finally { state.busy = false; updateLocks(); }
});
onSubmit('start-form', form => {
  const data = {name: value(form, 'name'), ...setupData(form)};
  void save(form, () => request('/api/run-logs/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)}),
    detail => { $('new-panel').hidden = true; loadSetup(detail.record); const location = new URL(window.location.href); location.searchParams.set('run', detail.record.run); window.history.replaceState(null, '', location); });
});
onSubmit('setup-form', form => { const data = setupData(form); void save(form, () => update('setup', data)); });
onSubmit('layer-form', form => {
  const data = layerData(form);
  const exists = (state.detail.record.layer_log || []).some(row => row.layer === data.layer && (row.pass ?? null) === data.pass);
  if (exists && !window.confirm(`Replace the saved measurements for layer ${data.layer}, pass ${data.pass ?? 'whole layer'}?`)) return;
  void save(form, () => update('layer-row', data));
});
onSubmit('temperature-form', form => {
  const data = {value: number(form, 'value'), unit: value(form, 'unit'), stage: value(form, 'stage'), note: value(form, 'note')};
  void save(form, () => update('temperature', data));
});
onSubmit('note-form', form => { const data = {text: value(form, 'text')}; if (!data.text) throw new Error('Enter a note before saving.'); void save(form, () => update('note', data)); });
onSubmit('upload-form', form => {
  const file = field(form, 'file').files[0]; if (!file) throw new Error('Choose a file to upload.');
  if (file.size > 25 * 1024 * 1024) throw new Error('Choose a file no larger than 25 MB.');
  const parameters = {run: state.detail.record.run, revision: state.detail.revision, kind: value(form, 'kind'), name: file.name};
  void save(form, () => request('/api/run-logs/add', {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file}, parameters));
});
onSubmit('finish-form', form => {
  if ([...dirty].some(id => id !== 'finish-form')) throw new Error('Save or clear your other unsaved entries before finishing.');
  const data = {outcome: value(form, 'outcome'), data_class: value(form, 'data_class'), height_mm: number(form, 'height_mm'), width_mm: number(form, 'width_mm'),
    notes: value(form, 'notes'), arcmon_waiver: field(form, 'arcmon_waiver').checked, waiver_reason: value(form, 'waiver_reason')};
  if (!window.confirm(`Finish and seal ${state.detail.record.name || state.detail.record.run}? This record cannot be edited afterwards.`)) return;
  void save(form, () => update('finish', data));
});
window.addEventListener('beforeunload', event => { if (dirty.size || state.busy || excelState.busy) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', clearExcelSecret);
async function initialize() {
  updateLocks();
  try {
    const identity = await request('/api/whoami');
    if (!identity.workspace || !identity.email || identity.guest) throw new Error('Sign in with your team account to record run logs.');
    state.workspace = identity.workspace; text($('account'), `Signed in as ${identity.email}`); updateLocks();
    void loadList();
    if ($('excel-panel').open) void loadExcelAccess();
    const requestedRun = new URL(window.location.href).searchParams.get('run');
    if (requestedRun) void selectRun(requestedRun); else status('page-status', 'Choose a run or start a new one. Changes save when you select a Save button.');
  } catch (error) { status('page-status', error.message, true); }
}
void initialize();
</script></body></html>'''
