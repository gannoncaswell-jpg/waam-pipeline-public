import json
import re
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tools.lint_pages import collect_page
from waam_pipeline.run_logs import render_run_logs


class _Tree(HTMLParser):


    def __init__(self, markup):
        super().__init__()
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        node = {"tag": tag, "attrs": dict(attributes), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                break


_HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = []; this.parentNode = null;
    this.dataset = {}; this.listeners = {}; this._text = ''; this._value = attrs.value || '';
    this.name = attrs.name || ''; this.id = attrs.id || ''; this.type = attrs.type || '';
    this.className = attrs.class || ''; this.disabled = 'disabled' in attrs; this.hidden = 'hidden' in attrs;
    this.checked = 'checked' in attrs; this.files = [];
    this.classList = {toggle: (name, enabled) => {
      const names = new Set(this.className.split(' ').filter(Boolean));
      if (enabled) names.add(name); else names.delete(name); this.className = [...names].join(' ');
    }};
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) this.dataset[key.slice(5)] = value;
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get value() { return String(this._value); }
  set value(value) { this._value = String(value); }
  get firstChild() { return this.children[0]; }
  get elements() { return {namedItem: name => this.all().find(node => node.name === name)}; }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  matches(selector) {
    if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
    const parts = selector.match(/^([a-z]+)?(?:\[([^=\]]+)(?:=([^\]]+))?\])?$/);
    if (!parts || (parts[1] && this.tagName !== parts[1].toUpperCase())) return false;
    if (!parts[2]) return true;
    const key = parts[2], present = key.startsWith('data-') ? Object.hasOwn(this.dataset, key.slice(5)) : this.hasAttribute(key);
    const value = key.startsWith('data-') ? this.dataset[key.slice(5)] : this.attrs[key];
    return present && (!parts[3] || value === parts[3]);
  }
  querySelectorAll(selector) { return this.all().filter(node => selector.split(',').some(part => node.matches(part.trim()))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  async fire(name, event = {}) {
    event.currentTarget = this; event.preventDefault ||= () => {};
    await Promise.all((this.listeners[name] || []).map(listener => listener(event)));
  }
  reportValidity() { return true; }
  reset() {
    for (const node of this.all()) {
      if (node.tagName === 'SELECT') node.value = node.children.find(child => child.hasAttribute('selected'))?.attrs.value || node.children[0]?.attrs.value || '';
      else if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') node.value = node.attrs.value || '';
      node.checked = 'checked' in node.attrs; node.files = [];
    }
  }
  focus() {} scrollIntoView() {}
}
function build(raw) { const node = new Element(raw.tag, raw.attrs); node.append(...raw.children.map(build)); if (node.tagName === 'SELECT') node.value = node.children[0]?.attrs.value || ''; return node; }
const document = build(input.tree);
document.createElement = tag => new Element(tag);
document.getElementById = id => document.all().find(node => node.id === id);
const requests = [], replies = [], confirmations = [], windowEvents = {};
const window = {location: {origin: 'https://waam.test', href: 'https://waam.test/run-logs'},
  history: {replaceState: (_state, _title, url) => { window.location.href = String(url); }},
  confirm: () => confirmations.length ? confirmations.shift() : true,
  addEventListener: (name, listener) => { windowEvents[name] = listener; }};
async function fetch(url, options) {
  requests.push({url: String(url), options});
  const next = replies.shift() || (new URL(url).pathname === '/api/whoami' ?
    {ok: true, workspace: 'caller-workspace', email: 'caller@example.test'} :
    new URL(url).pathname === '/api/run-logs/excel-access' ? excelFixture() : {ok: true, runs: []});
  if (typeof next === 'function') return next();
  return {ok: next.ok !== false, status: next.status || 200, json: async () => next};
}
function fixture(extra = {}) {
  return {ok: true, revision: 'revision-1', can_manage: true, record: {run: '2026-10-11_WALL_r01', name: 'Wall 1', status: 'open',
    operator: 'owner@example.test', started_at: '2026-10-11T10:00:00Z', setup: {}, collaborators: [],
    layer_log: [], temperatures: [], files: [], notes: [], audit: [], ...extra}};
}
function excelFixture(extra = {}) {
  return {ok: true, enabled: false, username: 'caller@example.test',
    feeds: {runs: 'https://waam.test/excel/runs.csv', layers: 'https://waam.test/excel/layers.csv',
      cooling: 'https://waam.test/excel/cooling.csv', temperatures: 'https://waam.test/excel/temperatures.csv'}, ...extra};
}
async function flush() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
const context = vm.createContext({document, window, fetch, Node: Element, URL, Date, console,
  assert, requests, replies, confirmations, windowEvents, fixture, excelFixture, flush, setImmediate});
vm.runInContext(input.script, context);
(async () => { await flush(); await vm.runInContext('(async () => {' + input.test + '\n})()', context); })().catch(error => { console.error(error); process.exitCode = 1; });
"""


class TestRunLogsPage(unittest.TestCase):
    def run_js(self, source):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the run-log browser checks")
        markup = render_run_logs()
        script = collect_page("run-logs", markup)["scripts"][0]["code"]
        result = subprocess.run(
            [node, "-e", _HARNESS], input=json.dumps({"tree": _Tree(markup).root, "script": script, "test": source}),
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_is_self_contained_and_controls_are_identifiable(self):
        markup = render_run_logs()
        self.assertEqual(len(collect_page("run-logs", markup)["scripts"]), 1)
        ids = re.findall(r'\bid="([^"]+)"', markup)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotRegex(markup, r'<(?:script|link|img)[^>]+(?:src|href)="https?://')
        self.assertNotIn("innerHTML", markup)
        self.assertNotIn("localStorage", markup)
        self.assertNotIn("sessionStorage", markup)
        self.assertIn('name="layer" type="number" min="0"', markup)
        self.assertIn('name="pass" type="number" min="1"', markup)
        self.assertIn("no invitations are sent", markup)
        self.assertIn('role="status"', markup)
        self.assertIn('<a href="/auth/logout">Sign out</a>', markup)
        self.assertNotIn('<a href="/">', markup)

    def test_start_sends_only_entered_setup_and_normalized_collaborators(self):
        self.run_js(r"""
          assert.equal(requests[0].options.credentials, 'same-origin');
          assert.equal(requests[0].options.cache, 'no-store');
          assert.equal(new URL(requests[0].url).pathname, '/api/whoami');
          assert.equal(new URL(requests[1].url).searchParams.get('ws'), 'caller-workspace');
          const form = $('start-form');
          assert.equal(JSON.stringify(setupData(form)), JSON.stringify({setup: {}, collaborators: []}));
          field(form, 'name').value = ' Specimen 4 ';
          field(form, 'arc_file').value = 'ASF 12';
          field(form, 'wire_diameter_mm').value = '1.2';
          field(form, 'collaborators').value = ' one@example.test, two@example.test, ';
          replies.push(fixture({name: 'Specimen 4'}));
          await form.fire('submit'); await flush();
          const sent = requests.find(request => request.url.includes('/start'));
          const payload = JSON.parse(sent.options.body);
          assert.equal(new URL(sent.url).searchParams.get('ws'), 'caller-workspace');
          assert.equal(payload.name, 'Specimen 4');
          assert.equal(JSON.stringify(payload.setup), JSON.stringify({wire_diameter_mm: 1.2, arc_file: 'ASF 12'}));
          assert.equal(payload.collaborators.join(','), 'one@example.test,two@example.test');
          assert.equal($('new-panel').hidden, true);
          assert.equal(state.detail.record.name, 'Specimen 4');
          assert.equal(dirty.size, 0);
        """)

    def test_layer_edit_preserves_readings_units_and_measured_current_voltage(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const row = {layer: 0, pass: 1, unit: 'C', pre_weld: 42, readings: {'20': 170, '40': 95},
            height_mm: 2.4, height_source: 'sense', width_mm: 6.1, park_travel_s_est: 4,
            current_a: 155, voltage_v: 19.2, note: 'Measured'};
          editLayer(row);
          const data = layerData($('layer-form'));
          for (const key of Object.keys(row)) assert.equal(JSON.stringify(data[key]), JSON.stringify(row[key]), key);
          assert.equal(data.pass_base, 1);
          field($('layer-form'), 'pass').value = '';
          assert.equal(layerData($('layer-form')).pass, null);
          addReading('20.0', '70');
          assert.throws(() => layerData($('layer-form')), /times must be different/);
          resetForm($('layer-form')); addReading('20', '');
          assert.throws(() => layerData($('layer-form')), /both seconds and temperature/);
        """)

    def test_clearing_setup_fields_sends_explicit_nulls(self):
        self.run_js(r"""
          renderDetail(fixture({setup: {wire_diameter_mm: 1.2, shielding_gas: 'Recorded gas'}}), true);
          const form = $('setup-form');
          field(form, 'wire_diameter_mm').value = ''; field(form, 'shielding_gas').value = '';
          const data = setupData(form);
          assert.equal(data.setup.wire_diameter_mm, null); assert.equal(data.setup.shielding_gas, null);
          assert.equal(Object.keys(data.setup).length, SETUP_FIELDS.length);
        """)

    def test_save_locks_forms_updates_revision_and_preserves_other_drafts(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const note = $('note-form'), temperatureForm = $('temperature-form');
          field(note, 'text').value = 'Observation'; markDirty(note);
          field(temperatureForm, 'value').value = '155'; markDirty(temperatureForm);
          let resolve;
          replies.push(() => new Promise(done => { resolve = done; }));
          const saving = save(note, () => update('note', {text: 'Observation'}));
          assert.equal(state.busy, true);
          assert.equal(note.querySelector('fieldset').disabled, true);
          assert.equal(temperatureForm.querySelector('fieldset').disabled, true);
          assert.equal(JSON.parse(requests.at(-1).options.body).revision, 'revision-1');
          const next = fixture({notes: [{text: 'Observation', at: '2026-10-11T11:00:00Z'}]}); next.revision = 'revision-2';
          resolve({ok: true, status: 200, json: async () => next}); await saving; await flush();
          assert.equal(state.detail.revision, 'revision-2'); assert.equal(state.busy, false);
          assert.equal(field(note, 'text').value, ''); assert.equal(dirty.has('note-form'), false);
          assert.equal(field(temperatureForm, 'value').value, '155'); assert.equal(dirty.has('temperature-form'), true);
          assert.match($('note-status').textContent, /^Saved/);
        """)

    def test_validation_and_stale_errors_keep_drafts_and_reload_does_not_retry(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const form = $('note-form'); field(form, 'text').value = 'Keep this draft'; markDirty(form);
          replies.push({ok: false, status: 400, error: 'Invalid note'});
          await save(form, () => update('note', {text: value(form, 'text')}));
          assert.equal(field(form, 'text').value, 'Keep this draft'); assert.equal(dirty.has('note-form'), true);
          assert.match($('note-status').textContent, /Not saved: Invalid note/);
          replies.push({ok: false, status: 409, error: 'Changed by teammate'});
          await save(form, () => update('note', {text: value(form, 'text')}));
          assert.equal(state.detail.revision, 'revision-1'); assert.equal($('conflict-note').hidden, false);
          const next = fixture({notes: [{text: 'Teammate note'}]}); next.revision = 'revision-2'; replies.push(next);
          const before = requests.length; await $('reload-button').fire('click'); await flush();
          assert.equal(requests.length, before + 1); assert.match(requests.at(-1).url, /detail/);
          assert.equal(state.detail.revision, 'revision-2');
          assert.equal(field(form, 'text').value, 'Keep this draft'); assert.equal(dirty.has('note-form'), true);
          assert.match($('notes').textContent, /Teammate note/);
        """)

    def test_permissions_sealed_records_and_untrusted_text(self):
        self.run_js(r"""
          const detail = fixture({name: '<img src=x onerror=alert(1)>',
            notes: [{text: '<script>bad()</script>'}], files: [{name: '<b>file</b>', kind: 'ATTACH', bytes: 5}]});
          detail.can_manage = false; renderDetail(detail, true);
          assert.equal($('run-title').children.length, 0); assert.match($('run-title').textContent, /<img/);
          assert.match($('notes').textContent, /<script>/);
          assert.equal($('manage-panel').hidden, true); assert.equal($('finish-panel').hidden, true);
          assert.equal($('setup-form').querySelector('fieldset').disabled, true);
          assert.equal($('layer-form').querySelector('fieldset').disabled, false);
          detail.record.status = 'finished'; detail.can_manage = true; renderDetail(detail);
          for (const form of document.querySelectorAll('form[data-editor]')) assert.equal(form.querySelector('fieldset').disabled, true, form.id);
          assert.equal($('finish-panel').hidden, true);
          assert.equal(new URL($('csv-link').href).searchParams.get('run'), detail.record.run);
          assert.equal(new URL($('csv-link').href).searchParams.get('ws'), 'caller-workspace');
        """)

    def test_uncertain_save_tells_user_to_check_record_before_retry(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const form = $('note-form'); field(form, 'text').value = 'Preserve'; markDirty(form);
          replies.push({ok: false, status: 500, save_uncertain: true, error: 'Catalog unavailable'});
          await save(form, () => update('note', {text: 'Preserve'}));
          assert.match($('note-status').textContent, /^Save not confirmed:/);
          assert.match($('page-status').textContent, /may have saved/);
          assert.equal(field(form, 'text').value, 'Preserve'); assert.equal(dirty.has(form.id), true);
          await save(form, () => Promise.reject(new Error('Network disconnected')));
          assert.match($('note-status').textContent, /^Save not confirmed:/);
          assert.match($('note-status').textContent, /check before retrying/);
        """)

    def test_switch_and_unload_warn_and_upload_retains_raw_file(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const note = $('note-form'); field(note, 'text').value = 'Unsaved'; markDirty(note);
          confirmations.push(false); const before = requests.length; await selectRun('other-run');
          assert.equal(requests.length, before); assert.equal(field(note, 'text').value, 'Unsaved');
          let warned = false; windowEvents.beforeunload({preventDefault: () => { warned = true; }}); assert.equal(warned, true);
          resetForm(note);
          const form = $('upload-form'), file = {name: 'arc + readings.csv', size: 9};
          field(form, 'file').files = [file]; field(form, 'kind').value = 'arcmon'; markDirty(form);
          replies.push(fixture()); await form.fire('submit'); await flush();
          const upload = requests.find(request => request.url.includes('/add?'));
          assert.equal(upload.options.body, file);
          assert.equal(new URL(upload.url).searchParams.get('name'), file.name);
          assert.equal(new URL(upload.url).searchParams.get('revision'), 'revision-1');
          assert.equal(new URL(upload.url).searchParams.get('kind'), 'arcmon');
          assert.equal(dirty.size, 0);
        """)

    def test_finish_requires_saved_forms_and_confirmation_then_seals_display(self):
        self.run_js(r"""
          renderDetail(fixture(), true);
          const note = $('note-form'), finish = $('finish-form');
          field(note, 'text').value = 'Unsubmitted observation'; markDirty(note);
          const before = requests.length; await finish.fire('submit'); await flush();
          assert.equal(requests.length, before); assert.match($('finish-status').textContent, /other unsaved entries/);
          await document.querySelector('[data-reset=note-form]').fire('click');
          assert.equal(dirty.has('note-form'), false); assert.equal(field(note, 'text').value, '');
          field(finish, 'outcome').value = 'not-run'; markDirty(finish);
          confirmations.push(false); await finish.fire('submit'); await flush(); assert.equal(requests.length, before);
          confirmations.push(true); replies.push(fixture({status: 'finished', outcome: 'not-run'}));
          await finish.fire('submit'); await flush();
          const mutation = requests.find(request => request.url.includes('/update?'));
          const payload = JSON.parse(mutation.options.body);
          assert.equal(payload.action, 'finish'); assert.equal(payload.data.outcome, 'not-run');
          assert.equal(payload.data.arcmon_waiver, false); assert.equal(payload.data.height_mm, null);
          assert.equal(state.detail.record.status, 'finished'); assert.equal($('finish-panel').hidden, true);
          assert.equal($('note-form').querySelector('fieldset').disabled, true); assert.equal(dirty.size, 0);
        """)

    def test_excel_connection_loads_on_open_and_binds_current_workspace(self):
        self.run_js(r"""
          assert.equal(requests.some(item => item.url.includes('/excel-access')), false);
          $('excel-panel').open = true; await $('excel-panel').fire('toggle'); await flush();
          const request = requests.find(item => item.url.includes('/excel-access'));
          assert.equal(new URL(request.url).searchParams.get('ws'), 'caller-workspace');
          assert.equal(request.options.credentials, 'same-origin');
          assert.equal($('excel-create').disabled, false); assert.equal($('excel-revoke').disabled, true);
          assert.equal($('excel-credentials').hidden, true); assert.equal($('excel-password').value, '');
        """)

    def test_excel_create_reveals_once_then_revoke_clears_password(self):
        self.run_js(r"""
          $('excel-panel').open = true;
          renderExcelAccess(excelFixture());
          replies.push(excelFixture({enabled: true, password: 'one-time-secret', created_at: '2026-10-11T10:00:00Z'}));
          await changeExcelAccess('create');
          const mutation = requests.at(-1);
          assert.equal(new URL(mutation.url).searchParams.get('ws'), 'caller-workspace');
          assert.equal(JSON.parse(mutation.options.body).action, 'create');
          assert.equal($('excel-password').value, 'one-time-secret'); assert.equal($('excel-secret').hidden, false);
          assert.equal($('excel-username').value, 'caller@example.test'); assert.equal($('excel-feeds').children.length, 4);
          assert.equal(Object.hasOwn(excelState.access, 'password'), false);
          assert.equal(JSON.stringify(excelState).includes('one-time-secret'), false);
          assert.equal($('excel-create').textContent, 'Replace Excel password');
          replies.push(excelFixture()); await changeExcelAccess('revoke');
          assert.equal(JSON.parse(requests.at(-1).options.body).action, 'revoke');
          assert.equal($('excel-password').value, ''); assert.equal($('excel-secret').hidden, true);
          assert.equal($('excel-credentials').hidden, true); assert.equal($('excel-feeds').children.length, 0);
          assert.equal($('excel-revoke').disabled, true); assert.match($('excel-status').textContent, /revoked/);
        """)

    def test_excel_replacement_confirmation_and_transient_secret_lifetime(self):
        self.run_js(r"""
          renderExcelAccess(excelFixture({enabled: true, password: 'transient'}), true);
          confirmations.push(false); const before = requests.length; await changeExcelAccess('create');
          assert.equal(requests.length, before); assert.equal($('excel-password').value, 'transient');
          $('excel-panel').open = false; await $('excel-panel').fire('toggle');
          assert.equal($('excel-password').value, '');
          renderExcelAccess(excelFixture({enabled: true, password: 'transient'}), true);
          windowEvents.pagehide(); assert.equal($('excel-password').value, '');
          renderExcelAccess(excelFixture({enabled: true, password: 'transient'}), true);
          replies.push(excelFixture({enabled: true})); await loadExcelAccess();
          assert.equal($('excel-password').value, ''); assert.equal($('excel-secret').hidden, true);
        """)

    def test_excel_rejects_untrusted_feed_urls_and_renders_identity_as_text(self):
        self.run_js(r"""
          for (const address of ['http://waam.test/feed', 'https://other.test/feed', 'https://user:password@waam.test/feed', 'javascript:alert(1)']) {
            assert.throws(() => renderExcelAccess(excelFixture({enabled: true, feeds: {runs: address}})), /HTTPS/);
          }
          const unsafe = '<img src=x onerror=alert(1)>';
          renderExcelAccess(excelFixture({enabled: true, username: unsafe, password: '<script>secret</script>'}), true);
          assert.equal($('excel-username').value, unsafe); assert.equal($('excel-username').children.length, 0);
          assert.equal($('excel-password').value, '<script>secret</script>'); assert.equal($('excel-password').children.length, 0);
        """)

    def test_excel_uncertain_change_requires_status_refresh_before_retry(self):
        self.run_js(r"""
          renderExcelAccess(excelFixture({enabled: true, password: 'old-secret'}), true);
          replies.push({ok: false, status: 500, error: 'Connection interrupted'});
          await changeExcelAccess('create');
          assert.equal($('excel-password').value, ''); assert.equal(excelState.access, null);
          assert.equal($('excel-create').disabled, true); assert.equal($('excel-refresh').disabled, false);
          assert.match($('excel-status').textContent, /Change not confirmed/);
          const before = requests.length; await changeExcelAccess('create'); assert.equal(requests.length, before);
          replies.push(excelFixture({enabled: true})); await loadExcelAccess(); assert.equal($('excel-create').disabled, false);
        """)

    def test_excel_password_is_not_reintroduced_after_panel_closes_during_create(self):
        self.run_js(r"""
          $('excel-panel').open = true; renderExcelAccess(excelFixture());
          let resolve; replies.push(() => new Promise(done => { resolve = done; }));
          const changing = changeExcelAccess('create');
          assert.equal($('excel-create').disabled, true); assert.equal($('excel-refresh').disabled, true);
          $('excel-panel').open = false; await $('excel-panel').fire('toggle');
          resolve({ok: true, status: 200, json: async () => excelFixture({enabled: true, password: 'late-secret'})});
          await changing;
          assert.equal($('excel-password').value, ''); assert.equal($('excel-secret').hidden, true);
          assert.match($('excel-status').textContent, /panel was closed/);
        """)


if __name__ == "__main__":
    unittest.main()
