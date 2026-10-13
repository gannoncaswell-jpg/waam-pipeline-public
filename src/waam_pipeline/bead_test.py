from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import random
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import cell_lc, weld_ctwd
from .backends.jbi import v_token
from .clearance import LC_PART_FIXED, TorchModel
from .emit import EmitRefused, PlatterState, emit_job
from .robots.ar1440 import V_TOKEN_MM_PER_MIN
from .toolpath import Move, Toolpath


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: enter a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: enter {low:g} to {high:g}")
    return float(value)


@dataclass(frozen=True)
class TestSpec:
    name: str
    speeds_mm_min: tuple[float, ...]
    repeats: int
    length_mm: float
    pitch_mm: float
    coupon_length_mm: float
    coupon_width_mm: float
    substrate_mm: float
    edge_mm: float
    origin_x_mm: float
    origin_y_mm: float
    width_limit_mm: float
    height_limit_mm: float


    nominal_bead_height_mm: float
    seed: int = 1
    planning_only: bool = True
    process: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,19}", self.name):
            raise ValueError("Test name: use 1 to 20 uppercase letters, digits, - or _, starting with a letter")
        if not isinstance(self.planning_only, bool):
            raise ValueError("planning_only must be true or false")
        if not isinstance(self.speeds_mm_min, (tuple, list)) or not 1 <= len(self.speeds_mm_min) <= 8:
            raise ValueError("Enter one to eight distinct travel speeds")
        speeds = tuple(_number(v, "Travel speed (mm/min)", 10, 10000) for v in self.speeds_mm_min)
        if len(set(speeds)) != len(speeds):
            raise ValueError("Travel speeds must be distinct; use repeats for replication")

        if len({v_token(v, V_TOKEN_MM_PER_MIN) for v in speeds}) != len(speeds):
            raise ValueError("Travel speeds must differ by at least 6 mm/min (one 0.1 mm/s V= step)")
        object.__setattr__(self, "speeds_mm_min", speeds)
        for key, low, high in (("repeats", 1, 5), ("seed", 0, 2**31 - 1)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key}: enter a whole number from {low} to {high}")
        for key, low, high in (("length_mm", 20, 300), ("pitch_mm", 1, 200),
                              ("coupon_length_mm", 20, 414.3), ("coupon_width_mm", 20, 676),
                              ("substrate_mm", .1, 100), ("edge_mm", 1, 100),
                              ("origin_x_mm", -2000, 2000), ("origin_y_mm", -2000, 2000),
                              ("width_limit_mm", .1, 100), ("height_limit_mm", .1, 30)):
            _number(getattr(self, key), key, low, high)
        _number(self.nominal_bead_height_mm, "nominal_bead_height_mm", 0, self.height_limit_mm)
        if self.nominal_bead_height_mm <= 0:
            raise ValueError("nominal_bead_height_mm must be > 0")
        count = len(speeds) * self.repeats
        if self.pitch_mm < self.width_limit_mm + 5:
            raise ValueError("Bead pitch must exceed the clearance width by at least 5 mm")
        if self.length_mm + 2 * self.edge_mm + self.width_limit_mm > self.coupon_length_mm:
            raise ValueError("The bead and its clearance envelope do not fit the coupon length")
        if (count - 1) * self.pitch_mm + self.width_limit_mm + 2 * self.edge_mm > self.coupon_width_mm:
            raise ValueError("The trial rows and their clearance envelopes do not fit the coupon width")
        x0, y0 = self.origin_x_mm, self.origin_y_mm
        if not (cell_lc.FACEPLATE_X - cell_lc.CHARLES_PLATE[1] / 2 <= x0
                and x0 + self.coupon_length_mm <= cell_lc.FACEPLATE_X + cell_lc.CHARLES_PLATE[1] / 2
                and cell_lc.FACEPLATE_Y <= y0
                and y0 + self.coupon_width_mm <= cell_lc.FACEPLATE_Y + cell_lc.CHARLES_PLATE[0]):
            raise ValueError("The whole coupon must fit the modeled platter at the specified origin")
        allowed = {"material", "wire_dia_mm", "gas", "gas_flow_l_min", "proc", "arc_file", "wfs_ipm",
                   "voltage_v", "ctwd_mm", "max_start_temp_c", "record_reference",
                   "substrate_material", "surface_prep"}
        if not isinstance(self.process, dict) or set(self.process) - allowed:
            raise ValueError("Unknown process field; use the bead-test example schema")
        object.__setattr__(self, "process", dict(self.process))
        for key, value in self.process.items():
            if key in ("material", "gas", "record_reference", "substrate_material", "surface_prep"):
                if value is not None and (not isinstance(value, str) or len(value) > 240
                                          or any(ord(c) < 32 for c in value)):
                    raise ValueError(f"{key}: enter one line of text, at most 240 characters")
            elif value is not None:
                _number(value, key, .001, 10000)
        for key in ("proc", "arc_file"):
            value = self.process.get(key)
            if value is not None and type(value) is not int:
                raise ValueError(f"{key}: enter a whole number")

    @classmethod
    def load(cls, path: str | Path) -> TestSpec:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("A bead-test plan must be a JSON object")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid bead-test fields: {exc}") from None

    @property
    def nominal_bead_height_label(self) -> str:
        return f"h={self.nominal_bead_height_mm:g} mm (NOMINAL, unverified)"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Trial:
    id: str
    row: int
    repeat: int
    run_order: int
    speed_mm_min: float
    x0: float
    x1: float
    y: float
    nominal_bead_height_mm: float

    def toolpath(self) -> Toolpath:


        return Toolpath([Move(self.x0, self.y, 0.0, None, False, 0, 1),
                         Move(self.x1, self.y, 0.0, self.speed_mm_min, True, 0, 2)])


def trials(spec: TestSpec) -> list[Trial]:

    rng = random.Random(spec.seed)
    result = []
    x0 = spec.edge_mm + spec.width_limit_mm / 2
    for repeat in range(1, spec.repeats + 1):
        speeds = list(spec.speeds_mm_min)
        rng.shuffle(speeds)
        rows = list(range(len(result), len(result) + len(speeds)))
        order = rows.copy()
        rng.shuffle(order)
        for row, speed in zip(rows, speeds):
            result.append(Trial(f"B{row + 1:02d}", row, repeat,
                                rows[0] + order.index(row) + 1, speed, x0, x0 + spec.length_mm,
                                spec.edge_mm + spec.width_limit_mm / 2 + row * spec.pitch_mm,
                                spec.nominal_bead_height_mm))
    return result


def seated_scene(spec: TestSpec, trial: Trial, *, training_block: bool, fixture=None):


    matrix = Toolpath([move for item in trials(spec) for move in item.toolpath().moves])
    origin, surface = cell_lc.seat_job(matrix, spec.origin_x_mm, spec.origin_y_mm,
                                     substrate_mm=spec.substrate_mm, fixture=fixture)
    scene = cell_lc.lc_scene(origin, training_block=training_block)


    scene.boxes.append(("bead_test_coupon", np.array([0., 0., -spec.substrate_mm]),
                        np.array([spec.coupon_length_mm, spec.coupon_width_mm, 0.])))
    neighbours = []
    for item in trials(spec):
        if item.id == trial.id:
            continue
        name = "bead_test_" + item.id
        pad = spec.width_limit_mm / 2
        scene.boxes.append((name, np.array([item.x0 - pad, item.y - pad, 0.]),
                            np.array([item.x1 + pad, item.y + pad, spec.height_limit_mm])))
        neighbours.append(name)
    scene.part_fixed = (*LC_PART_FIXED, "bead_test_coupon", *neighbours)
    return origin, surface, scene


def emit_trial(spec: TestSpec, trial_id: str, *, cell_profile: str | Path,
               platter: PlatterState) -> tuple[str, dict]:

    from .frames import load_cell_config

    if spec.planning_only:
        raise EmitRefused("process", "This plan contains example values; record the approved test settings first")
    required = ("material", "wire_dia_mm", "gas", "gas_flow_l_min", "proc", "arc_file", "wfs_ipm",
                "voltage_v", "ctwd_mm", "max_start_temp_c", "record_reference",
                "substrate_material", "surface_prep")
    missing = [key for key in required if spec.process.get(key) is None or not str(spec.process[key]).strip()]
    if missing:
        raise EmitRefused("process", "Record the fixed weld settings: " + ", ".join(missing))
    cfg = load_cell_config(cell_profile)
    arc_file = cell_lc.arc_file(cell_profile)
    if cfg.proc != 3 or spec.process["proc"] != 3 or spec.process["arc_file"] != arc_file:
        raise EmitRefused("process", "This first test uses CV process 3; the plan and cell profile must name the same verified ASF/AEF pair")
    if abs(spec.process["wire_dia_mm"] - 1.143) > 1e-6:
        raise EmitRefused("process", "LC process 3 is the 0.045-inch wire process (1.143 mm)")
    calibrated_ctwd = cell_lc.CALIBRATION.get("touch_off", {}).get("weld_stickout_mm")
    if (cfg.tool_number != 0 or calibrated_ctwd is None
            or not math.isclose(cfg.ctwd_mm, calibrated_ctwd, abs_tol=1e-6)):
        raise EmitRefused("process", "Use the calibrated TOOL 0 and a cell profile whose CTWD is the calibrated weld stick-out")

    try:
        weld_ctwd.resolve(spec.process["ctwd_mm"])
    except ValueError as exc:
        raise EmitRefused("ctwd", f"Recorded CTWD: {exc}") from exc
    found = [item for item in trials(spec) if item.id == trial_id]
    if not found:
        raise ValueError(f"Unknown trial {trial_id!r}")
    trial = found[0]

    if platter is None:
        raise EmitRefused("attestation", "A current platter attestation is required")
    origin, surface, scene = seated_scene(spec, trial, training_block=platter.training_block_on,
                                        fixture=platter.fixture)
    if surface is not None and platter.fixture is None:
        raise EmitRefused("attestation", "The active measured fixture must be named in the platter attestation")
    speed_token = v_token(trial.speed_mm_min, V_TOKEN_MM_PER_MIN)
    text, summary = emit_job(
        trial.toolpath(), cell_lc.pulse_solver(origin), name=f"{spec.name}-{trial.id}",
        arc_file=arc_file, platter=platter, scene=scene, torch=TorchModel.yaskawa_accufast_45(),
        bead_width_mm=spec.width_limit_mm, bead_height_mm=spec.height_limit_mm,
        margin_mm=5., weld_v=speed_token, hover_mm=max(50., cfg.safe_z_mm),
        weld_ctwd_mm=spec.process["ctwd_mm"],
        comments=(f"bead test {trial.id} run {trial.run_order}",
                  f"travel {trial.speed_mm_min:g} mm/min", "one bead then park"),
        provenance=cell_lc.calibration_summary() + (f"test {spec.fingerprint}",
                    f"CV3 WFS {spec.process['wfs_ipm']:g} ipm", f"MOVL V={speed_token} mm/s",
                    f"surface {surface.name}" if surface else "surface nominal platter",
                    f"NOMINAL h={spec.nominal_bead_height_mm:g}mm",
                    "height UNVERIFIED placeholder"),
    )
    summary.update(trial={**asdict(trial), "nominal_bead_height": spec.nominal_bead_height_label},
                   plan=spec.fingerprint, weld_v_mm_s=speed_token,
                   commanded_travel_mm_min=float(speed_token) * V_TOKEN_MM_PER_MIN,
                   origin_robot_mm=origin.tolist(), process_record=dict(spec.process),
                   calibration_sha256=hashlib.sha256(json.dumps(cell_lc.CALIBRATION, sort_keys=True).encode()).hexdigest(),
                   cell_profile_sha256=hashlib.sha256(Path(cell_profile).read_bytes()).hexdigest())
    return text, summary


WIDTH_FIELDS = ("width_25_mm", "width_50_mm", "width_75_mm")
MEASUREMENT_FIELDS = ("plan", "trial", "run_order", "repeat", "travel_mm_min", "status",
                      "nominal_bead_height",
                      *WIDTH_FIELDS, "height_mid_mm", "start_temp_c", "actual_wfs_ipm",
                      "actual_voltage_v", "actual_current_a", "notes")


def measurement_csv(spec: TestSpec) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=MEASUREMENT_FIELDS)
    writer.writeheader()
    for trial in sorted(trials(spec), key=lambda item: item.run_order):
        writer.writerow(dict(plan=spec.fingerprint, trial=trial.id, run_order=trial.run_order,
                             repeat=trial.repeat, travel_mm_min=trial.speed_mm_min, status="not_run",
                             nominal_bead_height=spec.nominal_bead_height_label))
    return stream.getvalue()


def analyse_measurements(spec: TestSpec, text: str) -> dict:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != list(MEASUREMENT_FIELDS):
        raise ValueError("Use the measurement CSV supplied with this test plan")
    lookup = {trial.id: trial for trial in trials(spec)}
    seen, results = set(), []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Every measurement row must have the supplied CSV columns")
        key = row["trial"]
        if key not in lookup or key in seen or row["plan"] != spec.fingerprint:
            raise ValueError("Unknown, duplicate or mismatched trial record")
        if row["nominal_bead_height"] != spec.nominal_bead_height_label:
            raise ValueError(f"{key}: nominal_bead_height differs from the NOMINAL/unverified test plan")
        seen.add(key)
        trial = lookup[key]
        for name, expected in (("travel_mm_min", trial.speed_mm_min), ("repeat", trial.repeat),
                               ("run_order", trial.run_order)):
            try:
                actual = float(row[name])
            except (TypeError, ValueError):
                raise ValueError(f"{key}: invalid {name}") from None
            if actual != expected:
                raise ValueError(f"{key}: {name} differs from the test plan")
        status = row["status"].strip()
        if status not in ("not_run", "ok", "rejected"):
            raise ValueError(f"{key}: status must be not_run, ok or rejected")
        values = []
        for name in WIDTH_FIELDS:
            raw = row[name].strip()
            try:
                values.append(None if not raw else _number(float(raw), name, .001, 100))
            except (TypeError, ValueError):
                raise ValueError(f"{key}: invalid {name}") from None
        if status == "ok" and any(value is None for value in values):
            raise ValueError(f"{key}: enter all three width measurements before marking ok")
        if status == "not_run" and any(value is not None for value in values):
            raise ValueError(f"{key}: record ok or rejected for a measured bead")
        observed = {}
        for name, low, high in (("height_mid_mm", 0., 100.), ("start_temp_c", -100., 2000.),
                                ("actual_wfs_ipm", 0., 10000.), ("actual_voltage_v", 0., 100.),
                                ("actual_current_a", 0., 2000.)):
            raw = row[name].strip()
            try:
                observed[name] = None if not raw else _number(float(raw), name, low, high)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: invalid {name}") from None
        if status == "not_run" and any(value is not None for value in observed.values()):
            raise ValueError(f"{key}: record ok or rejected for an observed trial")
        temp_limit = spec.process.get("max_start_temp_c")
        complete = all(value is not None for value in values)
        results.append(dict(trial=key, status=status, speed_mm_min=trial.speed_mm_min,
                            nominal_bead_height=spec.nominal_bead_height_label,
                            widths_mm=values, **observed, notes=row["notes"],
                            mean_width_mm=statistics.mean(values) if complete else None,
                            within_bead_sd_mm=statistics.stdev(values) if complete else None,
                            exceeds_width_limit=any(v is not None and v > spec.width_limit_mm for v in values),
                            exceeds_height_limit=observed["height_mid_mm"] is not None and observed["height_mid_mm"] > spec.height_limit_mm,
                            exceeds_start_temp=temp_limit is not None and observed["start_temp_c"] is not None and observed["start_temp_c"] > temp_limit))
    if seen != set(lookup):
        raise ValueError("The measurement sheet is missing trial rows; leave unrun rows as not_run")
    conditions = []
    for speed in spec.speeds_mm_min:
        means = [row["mean_width_mm"] for row in results if row["speed_mm_min"] == speed and row["status"] == "ok"]
        conditions.append(dict(speed_mm_min=speed, accepted_beads=len(means), planned_beads=spec.repeats,
                               mean_width_mm=statistics.mean(means) if means else None,
                               between_bead_sd_mm=statistics.stdev(means) if len(means) > 1 else None))
    return dict(plan=spec.fingerprint, trials=results, conditions=conditions,
                exceeds_width_limit=any(row["exceeds_width_limit"] for row in results),
                exceeds_height_limit=any(row["exceeds_height_limit"] for row in results),
                exceeds_start_temp=any(row["exceeds_start_temp"] for row in results))


def render_report(spec: TestSpec, measurements: dict | None = None) -> str:

    esc = html.escape
    items = trials(spec)
    if measurements is not None and measurements.get("plan") != spec.fingerprint:
        raise ValueError("Measurement results belong to a different test plan")
    measured_rows = {row["trial"]: row for row in measurements["trials"]} if measurements else {}
    scale = min(780 / spec.coupon_length_mm, 560 / spec.coupon_width_mm)
    def xy(x, y):
        return 40 + x * scale, 45 + (spec.coupon_width_mm - y) * scale
    width, height = 80 + spec.coupon_length_mm * scale, 90 + spec.coupon_width_mm * scale
    lines, rows = [], []
    for trial in items:
        x, y = xy(trial.x0, trial.y)
        x1, _ = xy(trial.x1, trial.y)
        lines.append(f'<g class="bead" data-id="{trial.id}"><title>{trial.id}: {trial.speed_mm_min:g} mm/min, run {trial.run_order}</title><line x1="{x}" y1="{y}" x2="{x1}" y2="{y}"/><text x="{x1+8}" y="{y+4}">{trial.id}</text></g>')
        measured = measured_rows.get(trial.id)
        status = {"not_run": "Not run", "ok": "Accepted", "rejected": "Rejected"}[measured["status"]] if measured else "Not run"
        values = [*measured["widths_mm"], measured["height_mid_mm"], measured["start_temp_c"]] if measured else [None] * 5
        cells = ''.join('<td></td>' if value is None else f'<td>{value:g}</td>' for value in values)
        rows.append(f'<tr data-id="{trial.id}"><td>{trial.run_order}</td><td>{trial.id}</td><td>{trial.repeat}</td><td>{trial.speed_mm_min:g}</td><td>{status}</td><td>{esc(spec.nominal_bead_height_label)}</td>{cells}</tr>')
    rows = [row for _, row in sorted(zip([t.run_order for t in items], rows))]
    labels = {"material": "Wire material", "wire_dia_mm": "Wire diameter, mm", "gas": "Shielding gas",
              "gas_flow_l_min": "Gas flow, L/min", "proc": "Process number", "arc_file": "ASF / AEF number",
              "wfs_ipm": "Wire feed, in/min", "voltage_v": "Voltage, V", "ctwd_mm": "CTWD, mm",
              "max_start_temp_c": "Maximum start temperature, °C", "substrate_material": "Coupon material",
              "surface_prep": "Surface preparation", "record_reference": "Settings record"}
    fixed = "".join(f'<dt>{label}</dt><dd>{esc(str(spec.process[key])) if spec.process.get(key) is not None else "Not recorded"}</dd>' for key, label in labels.items())
    result_html = "<p>Measurements are pending.</p>"
    if measurements is not None:
        result_rows = []
        for row in measurements["conditions"]:
            mean = "Pending" if row["mean_width_mm"] is None else f'{row["mean_width_mm"]:.3f}'
            sd = "Needs two beads" if row["between_bead_sd_mm"] is None else f'{row["between_bead_sd_mm"]:.3f}'
            result_rows.append(f'<tr><td>{row["speed_mm_min"]:g}</td><td>{row["accepted_beads"]}/{row["planned_beads"]}</td><td>{mean}</td><td>{sd}</td></tr>')
        result_html = '<table><tr><th>Travel, mm/min</th><th>Accepted beads</th><th>Mean width, mm</th><th>Between-bead SD, mm</th></tr>'+''.join(result_rows)+'</table>'
        if measurements["exceeds_width_limit"] or measurements["exceeds_height_limit"]:
            result_html += '<p class="notice">A measured bead exceeds the modeled width or height. Recheck clearance before continuing.</p>'
        if measurements["exceeds_start_temp"]:
            result_html += '<p class="notice">A trial exceeded the recorded starting-temperature limit. Review its acceptance.</p>'
    status = "Planning example. Speeds, coupon and placement are unverified." if spec.planning_only else "Test plan. Export each bead separately after checking the cell."
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(spec.name)} | Bead-width test</title>
<style>body{{font:15px system-ui,sans-serif;color:#172632;background:#edf2f5;margin:0}}main{{max-width:1100px;margin:28px auto;padding:30px;background:white;border-radius:12px}}h1{{margin:6px 0;font-size:30px}}h2{{font-size:19px;margin-top:28px}}.tag{{color:#147fa5;font-size:12px;font-weight:700;letter-spacing:1px}}.notice{{background:#fff5de;padding:14px;border-left:4px solid #b97d16}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}svg{{width:100%;height:560px;background:#f4f7f9;border:1px solid #d0dae0}}.bead line{{stroke:#1682aa;stroke-width:5;stroke-linecap:round}}.bead text{{font:14px system-ui;fill:#172632}}.bead.active line{{stroke:#d06b26;stroke-width:8}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{border-bottom:1px solid #cdd8df;padding:10px;text-align:left}}th{{background:#edf3f7}}td:empty{{min-width:48px;height:24px}}tr.active td{{background:#fff2db}}dl{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}dt{{color:#596b78}}dd{{margin:0;overflow-wrap:anywhere}}button{{background:#176f96;color:white;border:0;padding:10px 18px;border-radius:5px;cursor:pointer}}small{{color:#5c6d77}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}main{{margin:0;padding:18px}}.table-wrap{{overflow:auto}}}}@media print{{body{{background:white;font-size:12px}}main{{margin:0;padding:0}}.grid{{grid-template-columns:1fr 1fr;gap:18px}}svg{{height:450px;break-inside:avoid}}.bead text{{font-size:18px}}button{{display:none}}tr{{break-inside:avoid}}.measurements{{break-before:page}}.table-wrap{{overflow:visible}}th,td{{padding:8px 5px}}}}</style>
<main><div class="tag">WAAM STUDIO · LC50S / AR1440</div><h1>Bead-width test</h1><p>{esc(spec.name)} · Plan {spec.fingerprint}</p><p class="notice">{status}</p>
<div class="grid"><section><h2>Coupon layout</h2><svg viewBox="0 0 {width} {height}" role="img" aria-label="Numbered bead locations on the coupon"><rect x="40" y="45" width="{spec.coupon_length_mm*scale}" height="{spec.coupon_width_mm*scale}" fill="none" stroke="#7a919e"/>{''.join(lines)}<text x="40" y="25" font-size="13">+Y up · +X right · mm</text></svg><p>{spec.coupon_length_mm:g} × {spec.coupon_width_mm:g} × {spec.substrate_mm:g} mm coupon. Beads are {spec.length_mm:g} mm long, {spec.pitch_mm:g} mm apart.</p><p>Nominal bead height: <strong>{esc(spec.nominal_bead_height_label)}</strong>. Recorded with each trial, pending the A36/ER70S-6 bead model or our own DOE data; no measured height is inferred. It does not set the torch height: the contact tip runs at the recorded CTWD above the coupon top.</p><small>Clearance envelope: width {spec.width_limit_mm:g} mm, height {spec.height_limit_mm:g} mm. These are limits to verify, not predicted bead dimensions.</small></section><section><h2>Fixed settings</h2><dl>{fixed}</dl><h2>Run method</h2><ol><li>Label the coupon with the bead IDs and follow the listed run order.</li><li>Keep wire feed, process, gas, torch orientation and CTWD fixed.</li><li>Check the recorded starting-temperature limit before each independent job.</li><li>After cooling, measure width at 25%, 50% and 75% of bead length. Record height and visible defects separately.</li><li>Use accepted bead means to compare speeds. Rejected beads remain in the record.</li></ol><p>One job deposits one bead and returns to park. No automatic restart or timed cooling assumption is added.</p></section></div>
<h2 class="measurements">Measurements</h2><p>Measure at {spec.length_mm*.25:g}, {spec.length_mm*.5:g} and {spec.length_mm*.75:g} mm from the start. Three points on one bead describe its variation; separate beads provide replication.</p><div class="table-wrap"><table id="runs"><thead><tr><th>Run</th><th>Bead</th><th>Repeat</th><th>Travel, mm/min</th><th>Status</th><th>Nominal seating height</th><th>Width 25%</th><th>Width 50%</th><th>Width 75%</th><th>Measured height, mm</th><th>Start °C</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><h2>Results</h2>{result_html}<p>Single-bead width does not establish multilayer step-over, layer height or a qualified weld process.</p><button onclick="window.print()">Print worksheet</button></main>
<script>for(const el of document.querySelectorAll('[data-id]'))el.addEventListener('click',()=>{{for(const e of document.querySelectorAll('[data-id]'))e.classList.toggle('active',e.dataset.id===el.dataset.id);}});</script></html>'''
