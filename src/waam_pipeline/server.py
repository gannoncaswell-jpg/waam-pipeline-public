from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import hmac
import html as _html
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from dataclasses import asdict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from . import accounts, arcmon, arcparams, cell_hc, demos, desktop_release, mailer, oidc, job_archive, passkeys, runs, stl_library, ui_preferences, web_assets
from . import weld_ctwd
from .clearance import CONTACT_OK, Scene, TorchModel, blocking, check
from .correction import (footprint, placement_candidates, placement_limits,
                         repair_travels, rotate_part)
from .schedule import Schedule
from .orientation import place_on_face
from .plan import MeasureStop
from .slicer import MAX_LAYERS, interleave, parse_stl, scale_stl_to_mm, slice_mesh, to_gcode, transform, wall
from . import ornl
from .studio import NUMERIC_LIMITS, build_state, render_studio
from .run_sheet import render_run_sheet
from .run_logs import render_run_logs
from . import run_log_routes, run_log_excel_auth


ROBOT_SHADOW_MAX_MOVES = 700


_STATES: dict[str, dict] = {}
_LOCK = threading.Lock()


_WS_LOCKS: dict[str, threading.RLock] = {}


def _ws_lock(workspace: str) -> threading.RLock:
    with _LOCK:
        lock = _WS_LOCKS.get(workspace)
        if lock is None:
            lock = _WS_LOCKS[workspace] = threading.RLock()
        return lock


def _state(workspace: str) -> dict:
    with _LOCK:
        st = _STATES.get(workspace)
        if st is None:
            st = _STATES[workspace] = {"html": None, "toolpath": None,
                                       "request": None, "emit": None, "gen": 0}
        return st


def _forget_workspace(name: str) -> None:


    _STATES.pop(name, None)
    _WS_LOCKS.pop(name, None)


def _bump_gen(workspace: str) -> int:


    st = _state(workspace)
    with _LOCK:
        st["gen"] = int(st.get("gen") or 0) + 1
        return st["gen"]


_current: dict = _state("local")

_TOKENS: dict[str, str] = {}


_TOKEN_GATE = False
_TOKEN_EXPLICIT: str | None = None
_TOKENS_STAMP: tuple | None = None
_TOKENS_LOCK = threading.Lock()
_SHOW_CELL = True
_HOSTED_ASSETS = False
_DEFAULT_CELL_PROFILE = "lc50s"


_AUTH: oidc.AuthConfig | None = None
_AUTH_STAMP: tuple[int, int] | None = None
_AUTH_LOCK = threading.Lock()


SAVES_DIR: Path = Path(__file__).resolve().parents[2] / "saves"
EXCEL_CREDENTIALS: Path = Path(__file__).resolve().parents[2] / ".waam-run-log-excel.json"


ACCESS_LOG: Path = Path(os.environ.get("WAAM_ACCESS_LOG")
                        or Path(__file__).resolve().parents[2] / ".waam-access.log")
_LOG_LOCK = threading.Lock()


ACTIVITY_LOG: Path = Path(os.environ.get("WAAM_ACTIVITY_LOG")
                          or Path(__file__).resolve().parents[2] / ".waam-activity.log")
ACTIVITY_LOG_MAX_BYTES = 25 * 1024 * 1024
_ACT_LOCK = threading.Lock()


MAX_UPLOAD_BYTES = 64 * 1024 * 1024


MAX_UPLOAD_BYTES_GUEST = 8 * 1024 * 1024


MAX_CONCURRENT_SLICES = 3
_SLICE_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_SLICES)


_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


_JBI_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")


_HEADER_STRIP = {13: None, 10: None}


_REQUEST_KEYS = (
    "kind", "shape", "lh", "tilt", "seam", "bead", "margin",
    "filler", "wire", "travel", "ox", "oy", "oz", "sub", "name", "cell",
    "units", "yaw", "rotX", "rotY", "lay_flat", "fixture", "repair", "thermal", "cooling", "thermal_profile",
    "slicer", "contours", "stepover", "infill", "infill_angle", "infill_rotation", "travel_lift",
    "library", "cell_profile", "hc_cleaner", "substrate_registration", "substrate_margin", "bead_length",
    "layer_sense", "bead_count", "bead_pitch", "wall_length", "wall_layers", "wall_thickness",
    "copies", "copy_spacing", "wfs",
    "ctwd",
)


class _SenseSet(NamedTuple):


    layer: int
    spacing: float
    points: list
    step: bool
    base: int
    hits: list | None
    flag_base: int | None


def _layer_senses(raw) -> list[_SenseSet]:


    from .layer_sense import FLAG_BASE, LEGACY_BASE, SENSE_BASE

    if not raw:
        return []
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
        raise ValueError("Layer touch sets must be a JSON list of at most 32 KB")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Layer touch sets are not valid JSON") from None
    if not isinstance(data, list) or not 1 <= len(data) <= 50:
        raise ValueError("Layer touch sets must be a list of 1 to 50 sets")
    _, low, high, message = NUMERIC_LIMITS["layer_spacing"]
    out, last = [], -1
    for i, entry in enumerate(data, 1):
        if not isinstance(entry, dict) or set(entry) - {"layer", "spacing", "points", "step", "base", "hits", "flag_base"}:
            raise ValueError(f"Layer touch set {i}: expected layer, spacing, points and optional step, base, flag_base and hits only")
        base, hits, flag_base = entry.get("base", LEGACY_BASE), entry.get("hits"), entry.get("flag_base")
        if not ((base == LEGACY_BASE and hits is None and flag_base is None) or
                (base == SENSE_BASE and flag_base == FLAG_BASE and type(base) is int
                 and type(flag_base) is int and isinstance(hits, list)
                 and all(type(v) is int and v in (0, 1) for v in hits))):
            raise ValueError(f"Layer touch set {i}: base P{LEGACY_BASE:03d} without hit flags, or "
                             f"P{SENSE_BASE:03d}/B{FLAG_BASE:03d} with a 0/1 hit flag per point")
        step = entry.get("step", False)
        if type(step) is not bool:
            raise ValueError(f"Layer touch set {i}: step must be true or false")
        layer, spacing = entry.get("layer"), entry.get("spacing", 20)
        if type(layer) is not int or layer <= last:
            raise ValueError(f"Layer touch set {i}: layers must be whole numbers in increasing order")
        if type(spacing) not in (int, float) or not low <= spacing <= high:
            raise ValueError(f"Layer touch set {i}: {message}")
        out.append(_SenseSet(layer, float(spacing), entry.get("points"), step, base, hits, flag_base))
        last = layer
    if len({entry[3] for entry in out}) > 1:
        raise ValueError("Auto cycles re-step every later layer; remove the manual layer touch sets first, or stay in Manual")
    return out


def _sense_cap(base):
    from .layer_sense import LEGACY_BASE, LEGACY_MAX_POINTS, MAX_POINTS

    return LEGACY_MAX_POINTS if base == LEGACY_BASE else MAX_POINTS


def _replay_layer_senses(toolpath, sets, *, origin, bead_height_mm):
    from .layer_sense import apply_auto, auto_plan, fit_layer_top, layer_sense_plan

    layers = sorted({move.layer for move in toolpath.moves if move.deposit})
    first = layers[0] if layers else 0
    step, metadata, done = bead_height_mm, [], 0
    for layer, spacing, points, auto, base, hits, _flag_base in sets:
        cap = _sense_cap(base)
        if auto:
            plan, _hop = auto_plan(toolpath, first, layer, step_mm=step, spacing_mm=spacing, max_points=cap)
        else:
            plan = layer_sense_plan(toolpath, layer, bead_height_mm=bead_height_mm, spacing_mm=spacing,
                                    max_points=cap)
        top = fit_layer_top(toolpath, layer, plan, points, origin=origin, bead_height_mm=step,
                            base=base, hits=hits)
        if auto:
            toolpath, info = apply_auto(toolpath, top, first=first, last=layer,
                                        step_mm=step, bead_height_mm=bead_height_mm)
            done += 1
            info.update(cycle=done, spacing_mm=spacing, step=True)
            step, first = info["growth_mm"], layer + 1
        else:
            toolpath, info = top.apply(toolpath), top.metadata()
        metadata.append(info)
    return toolpath, metadata, {"cycles_done": done, "next_first": first,
                                "step_mm": step, "top_layer": layers[-1] if layers else -1}


def _cycle_stations(plan):
    return [{"pass_index": p.pass_index, "arc_mm": float(p.arc_mm),
             "expected": [float(v) for v in p.expected]} for p in plan]


def _cycle_temperature(raw, unit):
    if unit not in ("F", "C"):
        raise ValueError("Temperature unit must be F or C")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("Enter both cycle temperatures as finite numbers") from None
    if not math.isfinite(value) or not -40 <= value <= (2500 if unit == "F" else 1400):
        raise ValueError("temperature limit: -40..2500 F or -40..1400 C")
    return value, (value - 32) * 5 / 9 if unit == "F" else value


def _arc_schedule_from_query(query, toolpath):

    from .passes import parse_arc_schedule, resolve_arc_schedule

    raw = query.get("arc_schedule", [""])[0].strip()
    if not raw:
        return None, None
    if query.get("arc", [""])[0].strip():
        return None, "Give either one ASF# or an arc schedule, not both"
    try:
        return resolve_arc_schedule(parse_arc_schedule(raw), toolpath), None
    except ValueError as exc:
        return None, str(exc)


def _pass_headers(summary: dict) -> dict:

    from .passes import digest

    d = digest(summary.get("pass_table") or [])
    asf = d["asf"][:20]
    out = {"X-Waam-Pass-Count": str(d["count"]),
           "X-Waam-Asf": ",".join(map(str, asf)) + (",..." if len(d["asf"]) > 20 else "")}
    if d["travel_mm_min"]:
        out["X-Waam-Travel"] = "{:g}-{:g}".format(*d["travel_mm_min"])
    return out


def _asf_check_comment(arc_file) -> str:
    if isinstance(arc_file, dict) and len(set(arc_file.values())) > 1:
        return "check ASF list + dry run"
    number = next(iter(arc_file.values())) if isinstance(arc_file, dict) else arc_file
    return f"check ASF#{number} + dry run"


def _deposit_model(st: dict):
    arc = (st.get("state") or {}).get("arc") or {}
    wire, wfs = arc.get("wire_mm"), arc.get("wfs_m_min")
    return (float(wire), float(wfs)) if wire and wfs else None


def _layers_from_query(query) -> tuple[tuple[int, int] | None, str | None]:

    raw = query.get("layers", [""])[0].strip()
    if not raw:
        return None, None
    match = re.fullmatch(r"(\d{1,4})(?:\s*-\s*(\d{1,4}))?", raw)
    if not match:
        return None, "Layers to print: enter a range such as 0-3, or leave it blank for every layer"
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if last < first:
        return None, "Layers to print: the first layer must not be above the last"
    return (first, last), None


def _interpass_from_query(query) -> tuple[tuple[float, int] | None, str | None]:

    values = []
    for key, default in (("interpass", 30.0), ("interpass_every", 1)):
        raw = query.get(key, [""])[0].strip()
        if not raw:
            values.append(default)
            continue
        _, low, high, message = NUMERIC_LIMITS[key]
        try:
            value = float(raw)
        except ValueError:
            return None, message
        if (value != value or not low <= value <= high
                or (key == "interpass_every" and not value.is_integer())):
            return None, message
        values.append(int(value) if key == "interpass_every" else value)
    return (values[0], values[1]), None


def _arc_bead_summary(arc, *, bead, lh, beads, thickness, wire, travel, entered_wfs, ornl):

    from .bead_geometry import free_bead_report

    area = arc.bead_area_mm2
    rows = {"beads_across": beads, "fill_width_mm": arc.fill_width_mm, "bead_area_mm2": area}
    notes = []
    if beads > 1:
        lone = arcparams.solve(layer_height_mm=lh, bead_width_mm=bead, travel_mm_min=travel,
                               wire_dia_mm=wire, filler=arc.filler)
        rows["lone_bead_wfs_ipm"] = lone.wfs_ipm
        notes.append(f"Wire feed is per bead: {beads} beads across {thickness:g} mm at the {lh:g} mm "
                     f"Bead height each fill {area:.2f} mm² (thickness x pitch / beads), so "
                     f"{arc.wfs_ipm:.0f} ipm, not the {lone.wfs_ipm:.0f} ipm of a lone {bead:g} mm bead.")
    spacing = ornl.spacing_mm if ornl is not None and ornl.spacing_mm < bead else None
    if ornl is not None:
        notes.append(arcparams.ornl_spacing_note(bead, lh, ornl.stepover_percent))
    if spacing is not None:
        inner = arcparams.solve(layer_height_mm=lh, bead_width_mm=bead, fill_width_mm=spacing,
                                travel_mm_min=travel, wire_dia_mm=wire, filler=arc.filler)
        notes.append(f"ORNL overlaps beads {spacing:.2f} mm apart: a bead with neighbours on both "
                     f"sides fills {inner.bead_area_mm2:.2f} mm² ({inner.wfs_ipm:.0f} ipm). The feed "
                     "above is for a lone bead, so overlapped regions build taller than the Bead height.")
    if entered_wfs is not None:
        area = arcparams.deposited_area_mm2(wire, entered_wfs * arcparams.INCH / 1000.0, travel)

        pitch = (area / spacing if spacing is not None
                 else arcparams.predicted_layer_pitch_mm(area, thickness, beads))
        rows.update(entered_wfs_ipm=entered_wfs, entered_area_mm2=area, predicted_pitch_mm=pitch,
                    predicted_pitch_basis="ORNL infill, area / spacing" if spacing is not None
                    else "thickness x pitch / beads")
        note = arcparams.pitch_note(pitch, lh)
        if note:
            notes.append(note)
    try:
        free = free_bead_report(area_mm2=area, width_mm=bead, wire_dia_mm=wire)
        rows.update(free_crown_mm=free["crown_mm"], free_pitch_mm=free["pitch_mm"])
    except ValueError:
        notes.append(f"No nominal bead shape for {wire:g} mm wire; section a test bead to predict its crown.")
    return rows, notes


def _dwell_extras_from_query(query) -> tuple[dict | None, str | None]:

    out = {}
    for key, name in (("temp_check_every", "temp_check_every"), ("bead_dwell", "bead_dwell_s")):
        raw = query.get(key, [""])[0].strip()
        _, low, high, message = NUMERIC_LIMITS[key]
        try:
            value = float(raw) if raw else 0.0
        except ValueError:
            return None, message
        if (value != value or not low <= value <= high
                or (key == "temp_check_every" and not value.is_integer())
                or (key == "bead_dwell" and 0 < value < 0.01)):
            return None, message
        out[name] = int(value) if key == "temp_check_every" else value
    return out, None


def _measure_from_query(query) -> tuple[MeasureStop | None, str | None]:

    raw = query.get("measure_every", [""])[0].strip()
    _, low, high, message = NUMERIC_LIMITS["measure_every"]
    try:
        every = float(raw) if raw else 0.0
    except ValueError:
        return None, message
    if not math.isfinite(every) or not low <= every <= high or not every.is_integer():
        return None, message
    raw = query.get("measure_times", ["20, 40, 60"])[0].strip()
    message = ("Reading times after weld: enter up to 10 strictly increasing times from 1 to 3600 s, "
               "separated by commas or spaces, or leave blank")
    try:
        times = tuple(float(value) for value in re.split(r"[,\s]+", raw)) if raw else ()
    except ValueError:
        return None, message
    if (len(times) > 10 or any(not math.isfinite(value) or not 1 <= value <= 3600 for value in times)
            or any(a >= b for a, b in zip(times, times[1:]))):
        return None, message
    flags = {}
    for key, name, default, label in (
            ("measure_pause", "pause", "1", "Pause to measure the bead"),
            ("measure_clean", "clean", "0", "Clean wire at each stop"),
            ("measure_pre_weld", "pre_weld", "1", "Pause for pre-weld temperature")):
        value = query.get(key, [default])[0].strip()
        if value not in ("0", "1"):
            return None, f"{label}: enter 1 (on) or 0 (off)"
        flags[name] = value == "1"
    try:
        return (MeasureStop(every=int(every), readings_s=times, **flags) if every else None), None
    except ValueError as exc:
        return None, str(exc)


def _clean_first_from_query(query) -> tuple[bool | None, str | None]:
    value = query.get("clean_first", ["0"])[0].strip()
    if value not in ("0", "1"):
        return None, 'Clean wire before the first weld: enter 1 (on) or 0 (off)'
    return value == "1", None


def _cooling_proposal(run: dict | None, query) -> dict:

    from .interpass import cooling_pairs, fit_cooling, fit_decay, propose_dwell

    unit = query.get("unit", ["C"])[0]
    raw_setpoint = query.get("setpoint", [""])[0].strip()
    if not raw_setpoint:
        raise ValueError("Enter a max interpass temperature (the setpoint) to get a dwell proposal")
    try:
        _, setpoint_c = _cycle_temperature(raw_setpoint, unit)
    except ValueError as exc:
        raise ValueError(f"Max interpass setpoint: {exc}") from None
    raw_ambient = query.get("ambient", [""])[0].strip()
    ambient_c = _cycle_temperature(raw_ambient, unit)[1] if raw_ambient else 25.0
    pairs = cooling_pairs(run or {})
    fit_rows = runs.layer_log(run or {})
    try:
        fit = fit_decay(fit_rows, ambient_c=ambient_c)
    except ValueError:
        fit = fit_cooling(pairs, ambient_c=ambient_c)
        fit["basis"] = "cooling_pairs"
        last = pairs[-1]
        start_c, after_c = last["before_c"], last["after_c"]
        basis_note = "Based on Auto before/after interpass temperature pairs."
    else:
        if pairs:
            last = pairs[-1]
            start_c, after_c = last["before_c"], last["after_c"]
        else:
            last = fit["rows"][-1]
            start_c, after_c = last["t0_c"], last["last_c"]
        basis_note = "Based on the pooled median decay tau from the Run data sheet."
    proposal = propose_dwell(fit, start_c=start_c, setpoint_c=setpoint_c, after_c=after_c)
    proposal["notes"].append(basis_note)
    return {"fit": fit, "proposal": proposal, "basis": fit["basis"]}


def _thermal_value(query, key: str, label: str, *, required: bool = False,
                   default: float | None = None) -> float | None:
    raw = query.get(key, [""])[0].strip()
    if not raw:
        if required:
            raise ValueError(f"{label}: enter a finite number")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: enter a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label}: enter a finite number")
    return value


def _thermal_celsius(query, key: str, label: str, unit: str, *,
                     required: bool = False, default_c: float | None = None) -> float | None:
    value = _thermal_value(query, key, label, required=required)
    if value is None:
        return default_c
    try:
        return _cycle_temperature(value, unit)[1]
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def _thermal_run_exports(workspace: str, run: dict) -> tuple[list[dict], list[str]]:
    folder = runs._folder(SAVES_DIR, workspace, run["run"])
    known = {item.get("name") for item in run.get("files", [])}
    exports, notes = [], []
    for item in run.get("files", []):
        name = item.get("name")
        if item.get("kind") not in ("WELD-JOB", "CYCLE-JOB") \
                or not isinstance(name, str) or not name.endswith(".JBI"):
            continue
        sidecar_name = str(Path(name).with_suffix(".json"))
        if sidecar_name not in known or not runs.valid_file_name(run["run"], sidecar_name):
            notes.append(f"Run export {name} has no archived timing sidecar.")
            continue
        try:
            sidecar = runs._read(folder / sidecar_name)
        except (OSError, ValueError, TypeError):
            notes.append(f"Run export {name} has an unreadable timing sidecar.")
            continue
        if isinstance(sidecar, dict):
            exports.append(sidecar)
    return exports, notes


def _thermal_export_number(query: dict, key: str, default: float, *, whole=False) -> float | int:
    raw = query.get(key, default)
    if isinstance(raw, list):
        raw = raw[0] if raw else default
    if raw in (None, ""):
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"archived {key} is not numeric") from exc
    if not math.isfinite(value) or value < 0 or (whole and value != int(value)):
        raise ValueError(f"archived {key} is invalid")
    return int(value) if whole else value


def _thermal_export_bounds(sidecar: dict, available: tuple[int, int]) -> tuple[int, int]:
    summary = sidecar.get("summary") or {}
    bounds = summary.get("layers") if isinstance(summary, dict) else None
    if (isinstance(bounds, list) and len(bounds) == 2
            and all(type(value) is int for value in bounds)):
        return bounds[0], bounds[1]
    cycle = sidecar.get("cycle") or {}
    if isinstance(cycle, dict) and all(type(cycle.get(key)) is int
                                       for key in ("first", "last")):
        return cycle["first"], cycle["last"]
    query = sidecar.get("query") or {}
    raw = query.get("layers", "") if isinstance(query, dict) else ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    match = re.fullmatch(r"(\d{1,4})(?:\s*-\s*(\d{1,4}))?", str(raw).strip())
    if match:
        return int(match.group(1)), int(match.group(2) or match.group(1))
    return available


def _thermal_layer_timing(logged: dict, exports: list[dict],
                          available: tuple[int, int]) -> dict:
    layer = logged.get("layer")
    logged_at = logged.get("at")
    for sidecar in reversed(exports):
        first, last = _thermal_export_bounds(sidecar, available)
        if not first <= layer <= last:
            continue
        exported_at = sidecar.get("exported_at")
        if isinstance(logged_at, str) and isinstance(exported_at, str) \
                and exported_at > logged_at:
            continue
        try:
            query = sidecar.get("query") or {}
            if not isinstance(query, dict):
                raise ValueError("archived query is missing")
            bead = _thermal_export_number(query, "bead_dwell", 0.0)
            interpass = _thermal_export_number(query, "interpass", 30.0)
            every = _thermal_export_number(query, "interpass_every", 1, whole=True)
            measure_every = _thermal_export_number(query, "measure_every", 0, whole=True)
            if every < 1:
                raise ValueError("archived interpass cadence is invalid")
            stops = sidecar.get("measure_stops")
            if stops is None and isinstance(sidecar.get("summary"), dict):
                stops = sidecar["summary"].get("measure_stops", [])
            if not isinstance(stops, list):
                raise ValueError("archived measurement stops are invalid")
            stop = next((entry for entry in stops if isinstance(entry, dict)
                         and entry.get("after_layer") == layer), None)
            park = logged.get("park_travel_s_est")
            if stop is not None:
                park = stop.get("park_travel_s_est")
            elif measure_every:
                raise ValueError("the programmed measurement stop is missing")
            if isinstance(park, bool) or not isinstance(park, (int, float)) \
                    or not math.isfinite(park) or park < 0:
                raise ValueError("arc-off-to-reading travel time is missing")
            return {"bead_dwell_s": float(bead), "interpass_s": float(interpass),
                    "interpass_every": every, "park_travel_s_est": float(park)}
        except ValueError as exc:
            raise ValueError(str(exc)) from None
    raise ValueError("no archived export covers this layer")


def _thermal_timed_log(run: dict, exports: list[dict], passes: list[dict]
                       ) -> tuple[list[dict], dict, list[str]]:
    layers = [row["layer"] for row in passes]
    available = min(layers), max(layers)
    rows, timing, notes = [], {}, []
    for logged in runs.layer_log(run):
        key = logged.get("layer"), logged.get("pass")
        if key[0] not in layers:
            notes.append(f"Layer {key[0]} was left out of the run thermal fit: "
                         "it is not in this slice.")
            continue
        try:
            details = _thermal_layer_timing(logged, exports, available)
        except (TypeError, ValueError) as exc:
            notes.append(f"Layer {key[0]} was left out of the run thermal fit: "
                         f"its exported timing could not be reconstructed ({exc}).")
            continue
        rows.append({**logged, "park_travel_s_est": details["park_travel_s_est"]})
        timing[key] = details
    return rows, timing, notes


def _thermal_observations(run: dict, passes: list[dict], decay: dict,
                          timing: dict) -> list[dict]:
    source = {(row.get("layer"), row.get("pass")): row
              for row in run.get("layer_log", [])}
    pass_rows = {row["layer"]: row["passes"] for row in passes}
    observations = []
    for fitted in decay.get("used", []):
        key = fitted.get("layer"), fitted.get("pass")
        logged = source.get(key)
        layer_passes_ = pass_rows.get(key[0])
        programmed = timing.get(key)
        if (logged is None or logged.get("pre_weld") is None or not layer_passes_
                or programmed is None):
            continue
        selected = layer_passes_
        if key[1] is not None:
            if type(key[1]) is not int or not 1 <= key[1] <= len(layer_passes_):
                continue
            selected = [layer_passes_[key[1] - 1]]
        unit = logged.get("unit")
        start = float(logged["pre_weld"])
        if unit == "F":
            start = (start - 32.0) * 5.0 / 9.0
        elif unit != "C":
            continue
        arc_s = math.fsum(row["arc_s"] for row in selected)
        between = selected[:-1]
        wall_s = arc_s + math.fsum(row["gap_s"] for row in between)
        wall_s += programmed["bead_dwell_s"] * sum(row["bead_dwell"]
                                                    for row in between)
        observations.append({"layer": key[0], "start_c": start,
                             "pass": key[1], "end_c": fitted["t0_c"], "arc_s": arc_s,
                             "wall_s": wall_s})
    return observations


def _thermal_heating_observations(run: dict, passes: list[dict], timing: dict,
                                  ambient_c: float) -> list[dict]:
    fitted, cooling = [], {}
    for logged in run.get("layer_log", []):
        key = logged.get("layer"), logged.get("pass")
        programmed = timing.get(key)
        readings = logged.get("readings")
        if programmed is None or logged.get("pre_weld") is None or not isinstance(readings, dict):
            continue
        samples = []
        for raw_seconds, raw_value in readings.items():
            try:
                seconds, value = float(raw_seconds), float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if (not math.isfinite(seconds) or seconds < 0 or not math.isfinite(value)
                    or isinstance(raw_value, bool)):
                continue
            if logged.get("unit") == "F":
                value = (value - 32.0) * 5.0 / 9.0
            elif logged.get("unit") != "C":
                continue
            if value > ambient_c:
                samples.append((seconds, value))
        if not samples:
            continue
        seconds, end_c = min(samples)
        fitted.append({"layer": key[0], "pass": key[1], "t0_c": end_c})
        cooling[key] = programmed["park_travel_s_est"] + seconds
    observations = _thermal_observations(run, passes, {"used": fitted}, timing)
    for row in observations:
        row["cooling_s"] = cooling[(row["layer"], row["pass"])]
    return observations


def _thermal_plan(st: dict, query: dict, run: dict | None = None, *,
                  run_exports: list[dict] | None = None,
                  run_export_notes: list[str] | None = None) -> dict:
    from .interpass import fit_decay
    from .thermal_plan import DEFAULT, ThermalModel, fit_lumped, layer_passes, schedule

    unit = query.get("unit", ["C"])[0].strip().upper()
    if unit not in ("C", "F"):
        raise ValueError("Temperature unit must be F or C")
    target = _thermal_celsius(query, "target_preweld", "Target pre-weld", unit,
                              required=True)
    peak = _thermal_celsius(query, "max_peak", "Max peak", unit)
    ambient = _thermal_celsius(query, "ambient", "Ambient", unit, default_c=30.0)
    power = _thermal_value(query, "power_w", "Power", default=DEFAULT.power_w)
    if power is None or power <= 0:
        raise ValueError("Power must be a positive finite number")
    requested = query.get("model", ["default"])[0].strip().lower()
    if requested not in ("default", "run"):
        raise ValueError("Model must be default or run")

    toolpath = st.get("toolpath")
    params = (st.get("request") or {}).get("params") or {}
    try:
        travel = float(params.get("travel", 175.0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("The sliced travel speed is invalid; slice the part again") from exc
    passes = layer_passes(toolpath, travel_mm_min=travel)
    pass_layers = passes["layers"] if isinstance(passes, dict) else passes
    copies = passes.get("copies", 1) if isinstance(passes, dict) else 1
    model_notes = list(run_export_notes or [])
    effective_model = "default"
    fit_valid = True
    fit_reasons = []
    model = ThermalModel(DEFAULT.C_j_per_k, DEFAULT.tau_s, ambient, power, DEFAULT.source)
    if requested == "run":
        try:
            if not run:
                raise ValueError("no run is open")
            if copies > 1:
                raise ValueError("run fits do not identify which interleaved copy was measured")
            timed_rows, timing, timing_notes = _thermal_timed_log(
                run, run_exports or [], pass_layers
            )
            model_notes.extend(timing_notes)
            timed_run = {**run, "layer_log": timed_rows}
            try:
                decay = fit_decay(timed_rows, ambient_c=ambient)
            except ValueError as exc:
                if not str(exc).startswith("Decay fit needs at least 1 usable row"):
                    raise
                observations = _thermal_heating_observations(
                    timed_run, pass_layers, timing, ambient
                )
                if len(observations) < 2:
                    raise ValueError(f"{exc}; fewer than 2 usable layer heating observations") from None
                fitted = fit_lumped({"layers": observations}, power_w=power, ambient_c=ambient)
                model_notes.append("fitted from layer heating only (no timed decay readings)")
            else:
                observations = _thermal_observations(timed_run, pass_layers, decay, timing)
                if not observations:
                    raise ValueError("no fitted decay row has a pre-weld reading that matches this slice")
                fitted = fit_lumped({"layers": observations, "decay": decay},
                                    power_w=power, ambient_c=ambient)
            fit_valid = bool(fitted.get("valid", True))
            if not fit_valid:
                fit_reasons.extend(fitted.get("reasons") or fitted.get("notes", []))
            source = (f"Active run {run.get('run', 'unknown')} fit from "
                      f"{fitted['count']} layer observation(s)")
            model = ThermalModel(fitted["C_j_per_k"], fitted["tau_s"],
                                 ambient, power, source)
            effective_model = "run"
            model_notes.append("Run-fit heating time uses the archived export's bead dwell, "
                               "interpass cadence, and measurement travel timing.")
            model_notes.extend(fitted.get("notes", []))
        except (TypeError, ValueError) as exc:
            fit_valid = True
            fit_reasons = []
            model_notes.append(f"Fit this run could not be used ({exc}); using DEFAULT.")

    planned = schedule(model, passes, start_c=ambient, target_preweld_c=target,
                       max_peak_c=peak)
    valid = fit_valid and bool(planned.get("valid", True))
    reasons = list(fit_reasons)
    if not planned.get("valid", True):
        reasons.extend(planned.get("reasons") or planned.get("notes", []))
    row_keys = (("layer", "copy", "start_c", "peak_c", "end_c", "next_preweld_c")
                if copies > 1 else
                ("layer", "start_c", "peak_c", "end_c", "next_preweld_c"))
    rows = [{key: row[key] for key in row_keys} for row in planned["rows"]]
    answer = {
        "model": {"key": effective_model, "C": model.C_j_per_k, "tau": model.tau_s,
                  "ambient": model.ambient_c, "power": model.power_w,
                  "source": model.source, "notes": model_notes},
        "layer_dwell_s": planned["layer_dwell_s"],
        "bead_dwell_s": planned["bead_dwell_s"],
        "rows": rows,
        "max_peak_c": planned["max_peak_c"],
        "added_time_s": planned["total_added_s"],
        "notes": planned["notes"],
        "valid": valid,
        "reasons": list(dict.fromkeys(reasons)),
    }
    if copies > 1:
        answer["copies"] = copies
    return answer


_THERMAL_TRACE_KEYS = ("thermal_target", "thermal_peak", "thermal_model",
                       "thermal_c", "thermal_tau")


def _thermal_trace_from_query(query) -> tuple[dict | None, str | None]:
    present = [key for key in _THERMAL_TRACE_KEYS if key in query]
    if not present:
        return None, None
    if len(present) != len(_THERMAL_TRACE_KEYS):
        return None, "Thermal plan trace must include target, peak, model, C and tau"
    model = query["thermal_model"][0].strip().lower()
    if model not in ("default", "run"):
        return None, "Thermal plan model must be default or run"
    values = {}
    for key in ("thermal_target", "thermal_peak", "thermal_c", "thermal_tau"):
        try:
            value = float(query[key][0])
        except (TypeError, ValueError, OverflowError):
            return None, f"{key} must be a finite number"
        if not math.isfinite(value):
            return None, f"{key} must be a finite number"
        values[key] = value
    if not -273.15 < values["thermal_target"] <= 2500 \
            or not -273.15 < values["thermal_peak"] <= 2500:
        return None, "Thermal plan temperatures must be above absolute zero and at most 2500 C"
    if values["thermal_c"] <= 0 or values["thermal_tau"] <= 0:
        return None, "Thermal plan C and tau must be positive"
    target = f"{values['thermal_target']:.4g}"
    peak = f"{values['thermal_peak']:.4g}"
    comment = f"THERMAL PRE {target}C PEAK {peak}C"
    if len(comment) + 2 > 32:
        comment = (f"THERMAL PRE {values['thermal_target']:.0f}C "
                   f"PEAK {values['thermal_peak']:.0f}C")
    if len(comment) + 2 > 32:
        return None, "Thermal plan temperatures are too long for the job comment"
    return {**values, "thermal_model": model, "comment": comment}, None


def _run_sheet_data(workspace: str, record: dict | None) -> tuple[list[dict], list[str]]:
    record = record or {}
    rows = {}
    times = set()

    def blank(layer, park=None):
        return {"layer": layer, "pass": None, "unit": "C", "pre_weld": None,
                "readings": {}, "park_travel_s_est": park, "height_mm": None,
                "height_source": None, "width_mm": None, "note": ""}

    files = record.get("files", [])
    export = next((item for item in reversed(files)
                   if item["kind"] in ("WELD-JOB", "CYCLE-JOB") and item["name"].endswith(".JBI")), None)
    if export:
        name = str(Path(export["name"]).with_suffix(".json"))
        if name in {item["name"] for item in files} and runs.valid_file_name(record["run"], name):
            sidecar = runs._read(runs._folder(SAVES_DIR, workspace, record["run"]) / name)
            summary = sidecar.get("summary") or {}
            stops = sidecar.get("measure_stops", summary.get("measure_stops", []))
            bounds = summary.get("layers") or []
            last = bounds[-1] if bounds else (sidecar.get("cycle") or {}).get("last")
            if type(last) is not int or last < 0:
                last = max((stop["after_layer"] for stop in stops
                            if type(stop.get("after_layer")) is int), default=-1)
            for layer in range(last + 1):
                rows[layer, None] = blank(layer)
            for stop in stops:
                layer = stop.get("after_layer")
                if type(layer) is not int or not 0 <= layer <= last:
                    continue
                rows[layer, None] = blank(layer, stop.get("park_travel_s_est"))
                times.update(float(t) for t in stop.get("readings_s", []))
    growth = {}
    for cycle in record.get("cycles", []):
        info = {**(cycle.get("summary") or {}), **cycle}
        layers = info.get("layers", [])
        value = info.get("growth_mm")
        if (len(layers) == 2 and type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= 200):
            growth[layers[1]] = value
    for saved in runs.layer_log(record):
        rows[saved["layer"], saved.get("pass")] = dict(saved)
        times.update(float(t) for t in saved.get("readings", {}))
    for row in rows.values():
        if row["height_mm"] is None and row["layer"] in growth:
            row.update(height_mm=growth[row["layer"]], height_source="sense",
                       height_unsaved=True)
    ordered = sorted(rows.values(), key=lambda row: (row["layer"], -1 if row["pass"] is None else row["pass"]))
    return ordered, [str(int(t)) if t.is_integer() else str(t) for t in sorted(times)]


def run_slice(params: dict, stl_bytes: bytes | None, workspace: str = "local",
              email: str | None = None, *, prepared: dict | None = None,
              registration_action: str | None = None) -> dict:


    with _ws_lock(workspace):
        st = _state(workspace)
        if st.get("registration_pending") and registration_action is None:
            raise ValueError("Substrate registration is pending. Register the corners or clear registration before checking this build again")
        if params.get("substrate_registration") or ((st.get("request") or {}).get("params") or {}).get("substrate_registration"):
            st["registration_pending"] = True
        try:
            return _run_slice(params, stl_bytes, workspace, email, prepared=prepared)
        except Exception:
            if st.get("registration_pending") and st.get("state"):
                state = dict(st["state"], meta=dict(st["state"]["meta"], registration_pending=True))
                st["state"] = state
                st["html"] = render_studio(state, hosted=_HOSTED_ASSETS)
            raise


def _run_slice(params: dict, stl_bytes: bytes | None, workspace: str = "local",
               email: str | None = None, *, prepared: dict | None = None) -> dict:
    import numpy as np

    def number(key, default):
        _, low, high, message = NUMERIC_LIMITS[key]
        try:
            value = float(params.get(key, default))
        except (ValueError, TypeError, OverflowError):
            raise ValueError(message) from None
        if not np.isfinite(value) or not low <= value <= high:
            raise ValueError(message)
        return value

    def count(key, default):
        value = number(key, default)
        if not value.is_integer():
            raise ValueError(NUMERIC_LIMITS[key][3])
        return int(value)

    slicer_engine = str(params.get("slicer", "builtin"))
    if slicer_engine not in ("builtin", "ornl"):
        raise ValueError("Slicing engine: choose builtin or ornl")
    lh = number("lh", 2.9)
    tilt = number("tilt", 0.0)
    seam = number("seam", 25.0) if slicer_engine == "builtin" else 25.0
    bead = number("bead", 8.6)
    margin = number("margin", 5.0)
    name = str(params.get("name", "workbench part"))
    want_cell = _SHOW_CELL and params.get("cell", "1") != "0"
    selected_cell = str(params.get("cell_profile", _DEFAULT_CELL_PROFILE))
    if selected_cell not in ("lc50s", "hc10"):
        raise ValueError("Cell profile: choose lc50s or hc10")
    if selected_cell == "hc10" and not want_cell:
        raise ValueError("AW-HC preview requires the cell layer; enable cell geometry")
    hc_cleaner = str(params.get("hc_cleaner", cell_hc.DEFAULT_CLEANER))
    if hc_cleaner not in ("none", "yaskawa", "tregaskiss", "binzel"):
        raise ValueError("HC cleaner: choose none, yaskawa, tregaskiss or binzel")
    hc_preview = want_cell and selected_cell == "hc10"
    if hc_preview and params.get("correct") == "1":
        raise ValueError("AW-HC clearance is not calibrated; adjust its nominal placement manually")
    ox = number("ox", 0.0)
    oy = number("oy", 0.0)
    gap = number("oz", 0.0)
    substrate = number("sub", 6.35)

    demo_shape = None if params.get("kind") == "stl" else str(params.get("shape", demos.DEFAULT_DEMO))
    bead_length = number("bead_length", 101.6) if demo_shape == "bead" else 101.6
    bead_count = count("bead_count", 1) if demo_shape == "bead" else 1
    bead_pitch = number("bead_pitch", 25.0) if demo_shape == "bead" else 25.0
    wall_length = number("wall_length", 150.0) if demo_shape == "wall" else 150.0
    wall_layers = count("wall_layers", 6) if demo_shape == "wall" else 6
    wall_thickness = number("wall_thickness", 0.0) if demo_shape == "wall" else 0.0
    copies = count("copies", 1) if demo_shape in ("wall", "bead") else 1
    bead_set = demo_shape == "bead" and bead_count > 1
    substrate_margin = number("substrate_margin", 5.0)
    registered = None
    sensed_plate = False
    layer_senses = _layer_senses(params.get("layer_sense", ""))
    if layer_senses and (not want_cell or selected_cell != "lc50s"):
        raise ValueError("Layer touch sets need the LC50S cell")
    if layer_senses and copies > 1:
        raise ValueError("Layer touch does not support interleaved copies yet; station planning assumes one part")
    if layer_senses and bead_set:
        raise ValueError("A bead set's beads sit side by side, not on each other, so a layer touch "
                         "cannot re-seat the next bead. Touch the plate with Sense the surface instead")
    if layer_senses and params.get("correct") == "1":
        raise ValueError("The part is already printed where it is; automatic placement correction cannot move it")
    registration_raw = params.get("substrate_registration", "")
    if registration_raw:
        from .sensed import SensedPlate
        from .substrate import from_request


        try:
            version = json.loads(registration_raw).get("version")
        except (ValueError, TypeError, AttributeError):
            version = None
        sensed_plate = version == 3


        single_bead = demo_shape == "bead" and bead_count == 1 and copies == 1
        if not want_cell or selected_cell != "lc50s":
            raise ValueError("Sensed substrate placement requires the LC50S cell" if sensed_plate
                             else "Four-corner registration requires the LC50S cell")
        if version == 1 and not single_bead:
            raise ValueError("A ruler-gap (version 1) registration locates one bead only. "
                             "Re-register the corners with the TOOL 0 tip distance to seat this part")
        if params.get("correct") == "1":
            if sensed_plate:
                raise ValueError("Use the substrate offsets to move this part within its measured corners")
            raise ValueError("Use the substrate offsets to move this bead within its measured corners")
        if not sensed_plate and single_bead and params.get("thermal", "off") != "off":
            raise ValueError("Turn interpass simulation off for the registered single-bead test")
        registered = from_request(registration_raw, substrate)
        sensed_plate = isinstance(registered, SensedPlate)
    filler = str(params.get("filler", arcparams.DEFAULT_FILLER))
    wire = number("wire", arcparams.DEFAULT_WIRE_DIA_MM)
    travel = number("travel", 175.0)
    entered_wfs = number("wfs", 0) if str(params.get("wfs", "")).strip() else None

    weld_ctwd_mm = number("ctwd", 0) if str(params.get("ctwd") or "").strip() else None
    ctwd_value, ctwd_delta, ctwd_notes = weld_ctwd.resolve(weld_ctwd_mm)
    yaw = number("yaw", 0.0)
    rot_x, rot_y = number("rotX", 0.0), number("rotY", 0.0)
    lay_flat = str(params.get("lay_flat", "0"))
    if lay_flat not in ("0", "1"):
        raise ValueError("lay_flat must be 0 or 1")
    if params.get("kind") != "stl" and (rot_x or rot_y or lay_flat == "1"):
        raise ValueError("Place on face requires an STL mesh; demo paths have no solid faces")
    orient_part = params.get("kind") == "stl" and bool(rot_x or rot_y or tilt or lay_flat == "1")
    orientation_key = (rot_x, rot_y, tilt, lay_flat)
    if orient_part:
        gap = 0.0
    if prepared is not None and prepared.get("orientation_key") != orientation_key:
        prepared = None
    shape_settings = (bead_length, travel, bead_count, bead_pitch, wall_length, wall_layers, wall_thickness)
    if prepared is not None and prepared.get("shape_settings") != shape_settings:
        prepared = None
    ornl_options = None
    if slicer_engine == "ornl":
        if params.get("kind") != "stl":
            raise ValueError("Upload a closed solid STL to slice with ORNL")
        contours = number("contours", 2)
        if not contours.is_integer():
            raise ValueError("ORNL contours: enter a whole number from 1 to 20")
        ornl_options = ornl.Options(
            layer_height_mm=lh, bead_width_mm=bead, feed_mm_min=travel,
            contours=int(contours), stepover_percent=number("stepover", 75),
            pattern=str(params.get("infill", "lines")),
            angle_deg=number("infill_angle", 0), angle_rotation_deg=number("infill_rotation", 90),
            travel_lift_mm=number("travel_lift", 5),
        )
    if filler not in arcparams.FILLERS:
        raise ValueError("Filler: choose a filler from the list")
    if want_cell:
        margin = max(margin, 5.0)
    fixture = params.get("fixture")
    if fixture not in (None, "0", "1"):
        raise ValueError("fixture must be 0 (removed) or 1 (installed)")
    if params.get("repair", "1") not in ("0", "1"):
        raise ValueError("repair must be 0 or 1")


    thermal_mode = params.get("thermal", "off")
    cooling_policy = params.get("cooling", "adaptive")
    if thermal_mode not in ("off", "illustrative", "profile"):
        raise ValueError("Thermal scenario must be off, illustrative or profile")
    if cooling_policy not in ("adaptive", "continuous"):
        raise ValueError("Cooling policy must be adaptive or continuous")
    if thermal_mode != "off" and bead_set:
        raise ValueError("The interpass simulation models a stacked wall; turn it off for a set of beads")
    wall_beads = arcparams.wall_beads(wall_thickness, bead, lh) if wall_thickness else None
    copy_spacing = None
    copy_lift = 5.0
    if demo_shape in ("wall", "bead"):
        if demo_shape == "bead":
            copy_width = bead + (bead_count - 1) * bead_pitch
        else:
            copy_width = max(bead, wall_thickness)
        copy_spacing = number("copy_spacing", copy_width + 30.0)
        if copies > 1 and copy_spacing < copy_width + 10.0 - 1e-9:
            raise ValueError(f"Copy spacing: enter at least {copy_width + 10.0:g} mm "
                             "to leave 10 mm between part footprints")
        if copies > 1:
            copy_lift = number("travel_lift", 5.0)
    if thermal_mode != "off" and copies > 1:
        raise ValueError("The illustrative interpass simulation does not support interleaved copies; "
                         "turn it off and use the Thermal plan after slicing")
    if thermal_mode != "off" and wall_beads and wall_beads.count > 1:
        raise ValueError(f"The interpass simulation cannot model {wall_beads.count} beads per layer: its "
                         "bead response and support check have no side-by-side overlap or remelting, so "
                         "the second bead's height and support would be wrong. Turn it off for a thicker wall "
                         "(docs/interpass-control.md)")
    thermal_profile = None
    if thermal_mode != "off":
        from .interpass import Profile, illustrative_profile

        if thermal_mode == "illustrative":
            thermal_profile = illustrative_profile(bead, lh, travel, filler=filler,
                                                   wire_mm=wire, substrate_mm=substrate)
        else:
            raw = params.get("thermal_profile", "")
            if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 12000:
                raise ValueError("Upload a thermal response profile (JSON, at most 12000 bytes)")
            try:
                thermal_profile = Profile.from_dict(json.loads(raw))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Thermal profile: {exc}") from exc
            for field, value in dict(bead_width_mm=bead, layer_height_mm=lh,
                                      deposition_mm_min=travel, wire_dia_mm=wire,
                                      substrate_mm=substrate).items():
                if not np.isclose(getattr(thermal_profile, field), value, rtol=0, atol=1e-6):
                    raise ValueError(f"Thermal profile {field} does not match the current build")
            if thermal_profile.filler != filler:
                raise ValueError("Thermal profile filler does not match the current build")

    part_mesh = None
    stl_info = None
    slicing = {"engine": "builtin", "label": "Thin-wall contours"}
    if prepared is not None:
        toolpath = prepared["toolpath"]
        part_mesh = prepared["mesh"]
        stl_info = prepared["stl_info"]
        slicing = prepared.get("slicing", slicing)
    elif params.get("kind") == "stl":


        try:
            tri = parse_stl(stl_bytes or b"")
            if not len(tri) or not np.isfinite(tri).all():
                raise ValueError("empty or non-finite mesh")
        except (ValueError, TypeError, struct.error, OverflowError):
            raise ValueError(
                "Choose a binary or ASCII STL file."
            ) from None
        try:


            with np.errstate(over="raise", invalid="raise", divide="raise"):
                tri, resolved_units, unit_scale = scale_stl_to_mm(
                    tri, str(params.get("units", "auto"))
                )
                if not np.isfinite(tri).all():
                    raise FloatingPointError


                orientation = None
                if orient_part:
                    tri, orientation = place_on_face(tri, rot_x, rot_y, legacy_tilt=tilt)
                else:
                    tri = transform(tri, rotate_deg=tilt, axis="y")
                if not np.isfinite(tri).all():
                    raise FloatingPointError
                part_mesh = tri
                if ornl_options is not None:
                    toolpath, slicing = ornl.slice_mesh(tri, ornl_options)
                else:
                    n_layers = round(float(tri[:, :, 2].max()) / lh)
                    if n_layers > MAX_LAYERS:


                        raise ValueError(
                            f"This part needs {n_layers} layers at {lh:g} mm; the limit "
                            f"is {MAX_LAYERS}. Increase the bead height, or check "
                            "the STL units if the part is larger than expected."
                        )
                    try:
                        toolpath = slice_mesh(tri, layer_height_mm=lh, feed_mm_min=travel,
                                              seam_rotation_deg_per_layer=seam)
                    except ValueError as exc:


                        raise ValueError(
                            f"STL produced no sliceable contours at {lh:g} mm "
                            f"layer height ({exc}); check STL units, mesh "
                            "closure, or reduce the layer height"
                        ) from exc
                size = tri.max(axis=(0, 1)) - tri.min(axis=(0, 1))
        except ArithmeticError:
            raise ValueError(
                "STL geometry is outside the supported range. "
                "Check the units and export a binary or ASCII STL from your CAD"
            ) from None
        stl_info = {
            "requested_units": str(params.get("units", "auto")),
            "resolved_units": resolved_units,
            "scale_to_mm": unit_scale,
            "size_mm": [round(float(v), 3) for v in size],
        }
        if orientation is not None:
            stl_info["orientation"] = orientation
    else:
        shape = str(params.get("shape", demos.DEFAULT_DEMO))
        if shape == "bead":
            if bead_count > 1 and bead_pitch < bead + 1.0:
                raise ValueError(f"Bead spacing: {bead_pitch:g} mm leaves no gap between "
                                 f"{bead:g} mm wide beads; enter at least {bead + 1.0:g} mm")
            toolpath = demos.demo_bead(length_mm=bead_length, layer_height_mm=lh, feed_mm_min=travel,
                                       count=bead_count, pitch_mm=bead_pitch)
        else:
            options = dict(length_mm=wall_length, layers=wall_layers, thickness_mm=wall_thickness,
                           bead_width_mm=bead) if shape == "wall" else {}


            toolpath = demos.build(shape, layer_height_mm=lh, **options)

            toolpath = demos.with_feed(toolpath, travel)
            if shape == "wall" and wall_beads and (wall_beads.count > 1 or wall_beads.note):
                slicing = {"engine": "builtin", "label": "Side-by-side wall beads",
                           "wall": {"beads": wall_beads.count, "spacing_mm": wall_beads.spacing_mm,
                                    "fraction": wall_beads.fraction, "optimum": wall_beads.optimum,
                                    "thickness_mm": wall_thickness, "note": wall_beads.note}}

    if not any(m.deposit for m in toolpath.moves):
        detail = ""
        if stl_info is not None:
            dims = " x ".join(f"{v:g}" for v in stl_info["size_mm"])
            detail = f" (interpreted size {dims} mm)"
        raise ValueError(
            "STL produced no sliceable contours at "
            f"{lh:g} mm layer height{detail}; check STL units, mesh closure, "
            "or reduce the layer height"
        )

    prepared = {"toolpath": toolpath, "mesh": part_mesh, "stl_info": stl_info, "slicing": slicing,
                "orientation_key": orientation_key, "shape_settings": shape_settings}
    if copies > 1:
        middle = (copies - 1) / 2.0
        offsets = [(0.0, (index - middle) * copy_spacing) for index in range(copies)]
        toolpath = interleave([toolpath] * copies, offsets, lift_mm=copy_lift,
                              bead_height_mm=lh)
    toolpath, part_mesh = rotate_part(toolpath, part_mesh, yaw)
    bounds = footprint(toolpath, part_mesh, bead)


    bottom = min(p[2] for p in toolpath.deposit_points())
    if part_mesh is not None:
        part_mesh = part_mesh + np.array([0.0, 0.0, bottom - float(part_mesh[:, :, 2].min())])


    scene = Scene()
    robot = scene_meshes = None
    origin = None
    cell_note = "cell layer off"
    profile_meta = None
    if registered is not None:
        from . import cell_lc

        if version == 1 and (len(toolpath.moves) != 2 or toolpath.moves[0].deposit or not toolpath.moves[1].deposit):
            raise ValueError("Registered substrate placement supports one deposited bead segment only")
        origin = registered.origin.copy()
        if part_mesh is not None:
            points = np.asarray(toolpath.deposit_points())
            mesh_pivot = (points[:, :2].min(0) + points[:, :2].max(0)) / 2
            part_mesh = part_mesh.copy()
            part_mesh[:, :, :2] = (part_mesh[:, :, :2] - mesh_pivot + [ox, oy]) @ registered.basis[:2, :2].T
            part_mesh[:, :, 2] += float(registered.datum(*origin[:2])) - origin[2] - bottom + gap
        toolpath = registered.place(toolpath, ox=ox, oy=oy, bottom=bottom, gap=gap,
                                   bead_width=bead, edge_margin=substrate_margin)
        bottom = 0.0
        fixture_present = cell_lc.TRAINING_BLOCK_ON_PLATTER if fixture is None else fixture == "1"
        scene = cell_lc.lc_scene(origin, training_block=fixture_present)
        scene.substrates.append(registered.shifted(origin))
        scene.part_fixed = (*cell_lc.LC_PART_FIXED, registered.name)
    elif want_cell and not hc_preview:
        from . import cell_lc


        origin = np.array(cell_lc.DEFAULT_PART_ORIGIN, dtype=float)
        origin[1] += 120.0


        origin[0] += ox
        origin[1] += oy
        low, high = placement_limits(bounds)

        origin[:2] = np.clip(origin[:2], low, high)
        origin = cell_lc.snap_origin_to_platter(
            float(origin[0]), float(origin[1]), substrate_mm=substrate,
            first_deposit_z_mm=bottom, gap_mm=gap)
        fixture_present = cell_lc.TRAINING_BLOCK_ON_PLATTER if fixture is None else fixture == "1"
        scene = cell_lc.lc_scene(origin, training_block=fixture_present)
    elif hc_preview:

        profile_meta = cell_hc.profile(hc_cleaner)
        origin = cell_hc.place_origin(bounds, ox=ox, oy=oy, bottom=bottom,
                                     substrate=substrate, gap=gap)
        fixture_present = False

    toolpath, layer_meta, auto_meta = _replay_layer_senses(
        toolpath, layer_senses, origin=origin, bead_height_mm=lh)


    torch = TorchModel() if hc_preview else TorchModel.yaskawa_accufast_45()


    repair_options = dict(bead_width_mm=bead, bead_height_mm=lh,
                          margin_mm=margin, enabled=params.get("repair", "1") == "1")
    if hc_preview:


        violations, correction = None, None
    else:
        corrected = repair_travels(toolpath, torch, scene, **repair_options)
        initial_origin = None if origin is None else origin.copy()
        searched = 0

        static_hits = [v for v in blocking(corrected.violations) if not v.obstacle.startswith("bead(")]
        if want_cell and params.get("correct") == "1" and static_hits:
            for xy in placement_candidates(bounds, origin):
                candidate_origin = np.array([*xy, origin[2]])
                candidate_scene = cell_lc.lc_scene(candidate_origin, training_block=fixture_present)
                trial = repair_travels(toolpath, torch, candidate_scene, **repair_options)
                searched += 1
                if not blocking(trial.violations):
                    trial.before = corrected.before
                    corrected, origin, scene = trial, candidate_origin, candidate_scene
                    break
        toolpath, violations = corrected.toolpath, corrected.violations
        correction = corrected.summary()
        correction["placements_checked"] = searched
        correction["relocated"] = bool(want_cell and not np.allclose(origin, initial_origin))
        correction["obstacles"] = sorted({v.obstacle for v in blocking(violations)})
        if ctwd_delta:

            _, ctwd_value, ctwd_delta, violations, hanging, ctwd_notes = weld_ctwd.checked(
                toolpath, weld_ctwd_mm, torch=torch, scene=scene, **{
                    k: v for k, v in repair_options.items() if k != "enabled"}, contact_ok=CONTACT_OK)
            correction["obstacles"] = sorted({v.obstacle for v in blocking(violations)})
            if hanging:
                ctwd_notes = [*ctwd_notes, "Export will refuse: " + weld_ctwd.hang_refusal(hanging, ctwd_delta)]
    toolpath.copies = copies
    placement = None
    effective_params = {k: str(params[k]) for k in _REQUEST_KEYS if params.get(k) is not None}
    if weld_ctwd_mm is None:
        effective_params.pop("ctwd", None)

    for key in ("rotX", "rotY", "lay_flat"):
        effective_params.pop(key, None)
    if orient_part:
        effective_params.update(rotX=str(rot_x), rotY=str(rot_y), lay_flat=lay_flat, oz="0.0")


    for key, value in dict(kind=params.get("kind", "wall"),
                           shape=params.get("shape", demos.DEFAULT_DEMO),
                           lh=lh, bead=bead, sub=substrate, oz=gap, yaw=yaw,
                           tilt=tilt, seam=seam, filler=filler, wire=wire,
                           travel=travel, repair=params.get("repair", "1"),
                           thermal=thermal_mode, cooling=cooling_policy, slicer=slicer_engine,
                           cell_profile=selected_cell, hc_cleaner=hc_cleaner).items():
        effective_params.setdefault(key, str(value))
    for key in ("bead_length", "bead_count", "bead_pitch", "wall_length", "wall_layers", "wall_thickness",
                "copies", "copy_spacing"):
        effective_params.pop(key, None)
    if demo_shape == "bead":
        effective_params.update(bead_length=str(bead_length), substrate_margin=str(substrate_margin),
                                bead_count=str(bead_count), bead_pitch=str(bead_pitch))
    elif demo_shape == "wall":
        effective_params.update(wall_length=str(wall_length), wall_layers=str(wall_layers),
                                wall_thickness=str(wall_thickness))
    if copies > 1:
        effective_params.update(copies=str(copies), copy_spacing=str(copy_spacing))
        effective_params["travel_lift"] = str(copy_lift)
    if ornl_options is not None:
        effective_params.update(contours=str(ornl_options.contours), stepover=str(ornl_options.stepover_percent),
                                infill=ornl_options.pattern, infill_angle=str(ornl_options.angle_deg),
                                infill_rotation=str(ornl_options.angle_rotation_deg),
                                travel_lift=str(ornl_options.travel_lift_mm))
    effective_params["margin"] = str(margin)
    if want_cell:

        deposit_points = np.asarray(prepared["toolpath"].deposit_points())
        pivot = (deposit_points[:, :2].min(axis=0) + deposit_points[:, :2].max(axis=0)) / 2
    if hc_preview:
        effective_params.update(ox=str(origin[0] - cell_hc.DEFAULT_PART_ORIGIN[0]),
                                oy=str(origin[1] - cell_hc.DEFAULT_PART_ORIGIN[1]), fixture="0")
        center_offsets = (cell_hc.TABLE_XY_MIN + cell_hc.TABLE_XY_MAX - bounds[0] - bounds[1]) / 2 - cell_hc.DEFAULT_PART_ORIGIN[:2]
        placement = {"origin": origin.tolist(), "bottom_z": bottom,
                     "pivot": pivot.tolist(), "center_offsets": center_offsets.tolist(),
                     "footprint": [bounds[0].tolist(), bounds[1].tolist()],
                     "fixture": False, "surface": "AW-HC nominal table", "planning_only": True}
        scene_meshes = cell_hc.scene_meshes(origin, cleaner=hc_cleaner)
        if len(toolpath.moves) <= ROBOT_SHADOW_MAX_MOVES:
            robot = cell_hc.hc_robot_shadow(toolpath, origin, include_meshes=not _HOSTED_ASSETS)
            miss = robot.get("unreachable", 0)
            cell_note = (f"AW-HC preview: {len(toolpath.moves)} poses, {miss} unreachable. "
                         "Nominal cell geometry; clearance not evaluated.")
        else:
            cell_note = (f"AW-HC vendor layout; arm hidden ({len(toolpath.moves)} moves > "
                        f"{ROBOT_SHADOW_MAX_MOVES}); clearance not evaluated")
    elif want_cell:
        effective_params.update(ox=str(ox if registered is not None else origin[0] - cell_lc.DEFAULT_PART_ORIGIN[0]),
                                oy=str(oy if registered is not None else origin[1] - cell_lc.DEFAULT_PART_ORIGIN[1] - 120.0),
                                fixture="1" if fixture_present else "0")
        if registered is not None:
            center_offsets = np.zeros(2)
        else:
            center_limits = placement_limits(bounds)
            center_offsets = (center_limits[0] + center_limits[1]) / 2 - np.array(
                [cell_lc.DEFAULT_PART_ORIGIN[0], cell_lc.DEFAULT_PART_ORIGIN[1] + 120.0])
        placement = {"origin": origin.tolist(), "bottom_z": bottom,
                     "pivot": pivot.tolist(), "center_offsets": center_offsets.tolist(),
                     "footprint": [bounds[0].tolist(), bounds[1].tolist()],
                     "fixture": fixture_present}
        scene_meshes = cell_lc.lc_scene_meshes(origin, training_block=fixture_present)
        if registered is not None:
            scene_meshes = dict(scene_meshes)
            scene_meshes[registered.name] = registered.shifted(origin).mesh().reshape(-1).tolist()
            placement.update(surface="Registered substrate", plane_basis=registered.basis.T.tolist(),
                             display_pivot=(registered.basis[:, :2] @ [ox, oy] + [0, 0, gap]).tolist())
            if sensed_plate:
                xy = registered.basis[:2, :2] @ [ox, oy]
                placement["display_pivot"] = [*xy.tolist(), float(registered.shifted(origin).datum(*xy)) + gap]
        if len(toolpath.moves) <= ROBOT_SHADOW_MAX_MOVES:
            robot = cell_lc.robot_shadow(toolpath, origin, include_meshes=not _HOSTED_ASSETS)
            miss = robot.get("unreachable", 0)
            cell_note = f"cell + robot ({len(toolpath.moves)} poses, {miss} unreachable)"
        else:
            cell_note = f"cell shown, arm hidden ({len(toolpath.moves)} moves > {ROBOT_SHADOW_MAX_MOVES})"
    stop = blocking(violations or [])
    if placement is not None:
        requested_xy = {"ox": ox, "oy": oy}
        accepted_xy = {key: float(effective_params[key]) for key in requested_xy}
        placement["requested_offsets"] = requested_xy
        placement["accepted_offsets"] = accepted_xy
        if any(abs(requested_xy[key] - accepted_xy[key]) > 1e-7 for key in requested_xy):
            reason = ("Clearance correction moved the part" if correction and correction.get("relocated") else
                      "The footprint was kept inside the platter" if not hc_preview else
                      "The footprint was kept inside the nominal table")
            placement["adjustment"] = (
                f"{reason}: requested X {ox:.3f}, Y {oy:.3f} mm; "
                f"accepted X {accepted_xy['ox']:.3f}, Y {accepted_xy['oy']:.3f} mm. "
                "Review the accepted position in Place before continuing.")


    arc = None
    arc_warnings: list[str] = []
    schedule = None
    beads_across = wall_beads.count if wall_beads else 1
    filled = wall_thickness if beads_across > 1 else bead
    bead_rows: dict = {}
    try:
        arc = arcparams.solve(
            layer_height_mm=lh, bead_width_mm=bead, fill_width_mm=filled / beads_across,
            travel_mm_min=travel, wire_dia_mm=wire, filler=filler,
        )
        arc_warnings = arcparams.warnings(arc)
        bead_rows, bead_notes = _arc_bead_summary(
            arc, bead=bead, lh=lh, beads=beads_across, thickness=filled, wire=wire, travel=travel,
            entered_wfs=entered_wfs, ornl=ornl_options)
        arc_warnings = bead_notes + arc_warnings
        schedule = Schedule.ramp(
            0, max(0, len(toolpath.layers()) - 1),
            wfs=arc.wfs_ipm, volts=arc.volts_suggested,
        )
    except ValueError as exc:
        arc_warnings = [str(exc)]

    arc_payload = None if arc is None else {
        "key": arc.filler,
        "filler": arc.filler_label,
        "wire_mm": arc.wire_dia_mm,
        "wfs_m_min": arc.wfs_m_min,
        "wfs_ipm": arc.wfs_ipm,
        "deposition_kg_h": arc.deposition_kg_h,
        "amps_est": arc.current_estimate_a,
        "volts_est": arc.volts_suggested,
        "heat_kj_mm": arc.heat_input_kj_mm(),
        **bead_rows,
    }
    thermal = {"status": "not_configured", "planning_only": True}
    if thermal_profile is not None:
        from .interpass import simulate

        try:
            thermal_toolpath, thermal_bottom = toolpath, bottom
            if registered is not None:
                from dataclasses import replace


                thermal_toolpath = replace(toolpath, moves=[replace(move, z=move.z - move.seat)
                                                           for move in toolpath.moves])
                thermal_bottom = cell_lc.lowest_weld_z(thermal_toolpath)


            thermal = simulate(thermal_toolpath, thermal_profile, policy=cooling_policy,
                               substrate_z_mm=thermal_bottom - lh - (gap if want_cell else 0.0))
        except ValueError as exc:


            thermal = {"status": "unavailable", "planning_only": True, "error": str(exc)}
    display_toolpath, display_violations = toolpath, violations
    physical_shift_z = float(registered.physical_shift_robot[2]) if registered is not None else 0.0
    if physical_shift_z:
        from dataclasses import replace

        display_toolpath = replace(toolpath, moves=[replace(move, z=move.z + physical_shift_z) for move in toolpath.moves])
        display_violations = [replace(v, point=(v.point[0], v.point[1], v.point[2] + physical_shift_z)) for v in violations]
        placement["bottom_z"] += physical_shift_z
        placement["display_pivot"][2] += physical_shift_z
        if part_mesh is not None:
            part_mesh = part_mesh + [0, 0, physical_shift_z]
    state = build_state(
        display_toolpath,
        schedule=schedule,
        arc=arc_payload,
        arc_warnings=arc_warnings,
        thermal=thermal,
        shape=None if params.get("kind") == "stl" else str(params.get("shape", demos.DEFAULT_DEMO)),
        title=name,
        violations=display_violations,
        scene=scene,
        torch=torch,
        tooling_available=not hc_preview,
        cell_profile=profile_meta,
        cell_name=cell_hc.LABEL if hc_preview else ("ArcWorld LC50S" if want_cell else "workbench"),
        robot=robot,
        scene_meshes=scene_meshes,
        scene_reference_boxes=cell_hc.reference_boxes(origin, cleaner=hc_cleaner) if hc_preview else None,
        part_mesh=part_mesh,
        workbench=True,
        workspace=workspace,
        email=email,
        stl_info=stl_info,
        placement=placement,
        correction=correction,
        request_params=effective_params,
        slicing=slicing,
        ornl_status=ornl.availability(),
    )
    state["meta"]["gen"] = _bump_gen(workspace)
    state["meta"]["registration_pending"] = False
    state["meta"]["substrate"] = registered.metadata() if registered is not None else None
    state["meta"]["layer_sense"] = layer_meta
    state["meta"]["auto"] = auto_meta
    if registered is not None:
        state["meta"]["substrate"]["surface_setup"] = registered.setup_metadata(
            cell_lc.PLATTER_FACE_Z,
            (cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y + cell_lc.CHARLES_PLATE[0] / 2),
        )
    state["meta"]["registration_ctwd_mm"] = TorchModel.yaskawa_accufast_45().stackup.ctwd_mm
    state["meta"]["weld_ctwd"] = {"mm": ctwd_value, "default_mm": weld_ctwd.default_mm(),
                                  "delta_mm": ctwd_delta, "notes": ctwd_notes}
    if registered is not None:
        state["torch"]["command_from_physical_z_mm"] = -physical_shift_z
    if _HOSTED_ASSETS:
        state = web_assets.transport_state(state)
    page = render_studio(state, hosted=_HOSTED_ASSETS)
    st = _state(workspace)
    with _LOCK:
        st["toolpath"] = toolpath
        st["prepared"] = prepared
        st["html"] = page
        st["state"] = state
        st["registration_pending"] = False


        st["request"] = {
            "params": effective_params,
            "stl": stl_bytes if params.get("kind") == "stl" else None,
        }


        st["emit"] = {
            "cell_profile": selected_cell,


            "fixture": bool(want_cell and fixture_present),
            "origin": origin,
            "scene": scene if want_cell else None,
            "torch": torch,
            "bead": bead,
            "bead_height": lh,
            "margin": margin,
            "part": name,
            "substrate_registration": registration_raw,
            "bead_set": bead_set,
            "copies": copies,
            "weld_ctwd_mm": weld_ctwd_mm,
        }
    return {
        "ok": True,
        "state": state,
        "moves": len(toolpath.moves),
        "layers": len(toolpath.layers()),
        "violations": None if hc_preview else len(violations),
        "blocking": None if hc_preview else len(stop),
        "clearance_checked": not hc_preview,
        "cell": cell_note,
        "origin": None if origin is None else [round(float(v), 1) for v in origin],
        "arc": None if arc is None else {
            "filler": arc.filler_label,
            "wire_mm": arc.wire_dia_mm,
            "wfs_m_min": round(arc.wfs_m_min, 2),
            "wfs_ipm": round(arc.wfs_ipm, 0),
            "deposition_kg_h": round(arc.deposition_kg_h, 2),
            "amps_est": round(arc.current_estimate_a, 0),
            "volts_est": round(arc.volts_suggested, 1),
            "heat_kj_mm": round(arc.heat_input_kj_mm(), 3),
            "travel_mm_min": arc.travel_mm_min,
        },
        "arc_warnings": arc_warnings,
        "thermal": thermal,
        "slicing": slicing,
        "unreachable": (robot or {}).get("unreachable", 0),
        "stl": stl_info,
        "placement": placement,
        "correction": correction,
    }


def run_placement(params: dict, workspace="local", email=None):

    params = dict(params)
    action = params.pop("registration_action", None)
    if action not in (None, "apply", "clear"):
        raise ValueError("Choose apply or clear for the substrate registration action")
    if action is not None and ("substrate_registration" not in params or
                               bool(params["substrate_registration"]) != (action == "apply")):
        raise ValueError("The substrate registration action must match its corner record")
    allowed = {"ox", "oy", "oz", "yaw", "rotX", "rotY", "lay_flat", "tilt",
               "sub", "fixture", "repair", "correct", "substrate_registration", "substrate_margin", "bead_length",
               "bead_count", "bead_pitch", "copies", "copy_spacing", "layer_sense"}
    if set(params) - allowed:
        raise ValueError("Placement accepts only position, rotation, substrate, fixture and correction settings")


    with _ws_lock(workspace):
        with _LOCK:
            st = _STATES.get(workspace) or {}
            source, request = st.get("prepared"), st.get("request")
        if not source or not request:
            raise ValueError("Slice a part before moving it")
        merged = dict(request["params"], **params)
        return run_slice(merged, request["stl"], workspace, email, prepared=source,
                         registration_action=action)


def _default_page(workspace: str, email: str | None = None) -> str:
    st = _state(workspace)
    if st["html"] is None:
        run_slice({"kind": "wall", "name": "Demo wall"}, None,
                  workspace, email)
    return st["html"]


ACCOUNTS = accounts.Store()


WEBAUTHN = passkeys.WebAuthnStore()

UI_PREFERENCES = ui_preferences.Store()

SESSION_COOKIE = "waam_sid"
LOGIN_COOKIE = "waam_login"


SIGN_IN_PATH = "/signin"

GUEST_COOKIE = "waam_guest"


GUEST_TTL_S = 6 * 3600
MAX_GUESTS = 64


GUEST_ALLOWED = frozenset({"/", "/api/slice", "/api/place", "/api/whoami"})
_GUESTS: dict[str, float] = {}
_GUEST_LOCK = threading.Lock()


_NOTIFIED_PENDING: set[str] = set()
_NOTIFIED_LOCK = threading.Lock()


def _new_guest() -> str:
    gid = secrets.token_urlsafe(9)
    now = time.time()
    with _GUEST_LOCK:
        for old in [g for g, t in _GUESTS.items() if now - t > GUEST_TTL_S]:
            _GUESTS.pop(old, None)
            _forget_workspace(_guest_workspace(old))
        while len(_GUESTS) >= MAX_GUESTS:
            oldest = min(_GUESTS, key=_GUESTS.get)
            _GUESTS.pop(oldest, None)
            _forget_workspace(_guest_workspace(oldest))
        _GUESTS[gid] = now
    return gid


def _guest_workspace(gid: str) -> str:


    return "guest-" + _sanitize_workspace(gid)[:12]


def _live_guest(gid: str | None) -> bool:
    if not gid:
        return False
    with _GUEST_LOCK:
        created = _GUESTS.get(gid)
        if created is None:
            return False
        if time.time() - created > GUEST_TTL_S:


            _GUESTS.pop(gid, None)
            _forget_workspace(_guest_workspace(gid))
            return False
    return True


def forget_guests() -> None:

    with _GUEST_LOCK:
        for gid in list(_GUESTS):
            _forget_workspace(_guest_workspace(gid))
        _GUESTS.clear()
SIGN_IN_ALIASES = ("/signin", "/sign-in", "/login")


def _refresh_auth() -> oidc.AuthConfig | None:


    global _AUTH, _AUTH_STAMP
    try:
        st = oidc._auth_config_file().stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    with _AUTH_LOCK:
        if stamp == _AUTH_STAMP:
            oidc.set_session_authority(_AUTH)
            return _AUTH
        if stamp is None:
            _AUTH, _AUTH_STAMP = None, None
            oidc.set_session_authority(None)
            return None
        cfg = oidc.load_auth_config()
        _AUTH_STAMP = stamp
        if cfg is None and _AUTH is not None:
            print("  .waam-auth.json is unusable - keeping the last good sign-in config",
                  flush=True)
            oidc.set_session_authority(_AUTH)
            return _AUTH
        _AUTH = cfg
        oidc.set_session_authority(cfg)
        return _AUTH


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _refresh_tokens() -> dict[str, str]:


    global _TOKENS, _TOKENS_STAMP
    stamp = (_stamp(_tokens_file()), _stamp(_token_file()))
    with _TOKENS_LOCK:
        if stamp == _TOKENS_STAMP:
            return _TOKENS
        fresh: dict[str, str] = {}
        f = _tokens_file()
        torn = False
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                for tok, info in (raw.get("tokens") or {}).items():
                    if tok and isinstance(tok, str):
                        name = info.get("name", "") if isinstance(info, dict) else ""
                        fresh[tok] = _sanitize_workspace(str(name))
            except (OSError, ValueError):
                torn = True
        if torn:
            print("  .waam-tokens.json is unreadable - keeping the last good "
                  "roster", flush=True)
            _TOKENS_STAMP = stamp
            return _TOKENS

        for tok in (_TOKEN_EXPLICIT, _read_shared_token()):
            if tok and tok not in fresh:
                fresh[tok] = "default"
        _TOKENS, _TOKENS_STAMP = fresh, stamp
        return _TOKENS


def _read_shared_token() -> str | None:
    f = _token_file()
    if not f.exists():
        return None
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _speed_fix_note(saved: dict, params: dict) -> str | None:


    if saved.get("v_unit") == "mm/s" or params.get("cell_profile") != "lc50s":
        return None
    try:
        ran = 6.0 * float(params.get("travel", 175.0))
    except ValueError:
        return None
    entered = params.get("travel", "175")
    params["travel"] = f"{ran:g}"
    return (f"Saved before the travel-speed fix: its Travel of {entered} mm/min ran at "
            f"{ran:g} mm/min on the LC, so it loads as {ran:g}. Save it again to keep that.")


def _workspace_for_email(email: str) -> str:


    local, _, domain = email.partition("@")
    stem = _sanitize_workspace(f"{local}_at_{domain}")[:33].rstrip("_") or "user"
    return f"{stem}_{hashlib.sha256(email.encode('utf-8')).hexdigest()[:6]}"


def _approval(cfg: oidc.AuthConfig, email: str, workspace: str,
              known: bool, why: str) -> tuple[str | None, bool, str]:


    if cfg.approval_required and email not in cfg.approved:
        return None, False, "pending"
    return workspace, known, why


def _sso_decision(cfg: oidc.AuthConfig, email: str,
                  verified: bool) -> tuple[str | None, bool, str]:


    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return None, False, "no-email"
    domain = e.rsplit("@", 1)[1]


    if e in cfg.blocked or domain in cfg.blocked:
        return None, False, "blocked"
    entry = cfg.users.get(e)
    if entry is not None:
        chosen = str(entry.get("name") or "").strip()
        return (_sanitize_workspace(chosen) if chosen else _workspace_for_email(e)), True, "roster"
    if domain in cfg.allow_domains and verified:
        return _approval(cfg, e, _workspace_for_email(e), True, "domain")
    if domain in cfg.allow_domains and not cfg.open_signin:
        return None, False, "unverified"
    if cfg.open_signin:
        return _approval(cfg, e, _workspace_for_email(e), False, "open")
    return None, False, "unknown"


def _may_register(cfg: oidc.AuthConfig, email: str) -> bool:


    e = (email or "").strip().lower()
    if "@" not in e:
        return False
    if e in cfg.blocked or e.rsplit("@", 1)[1] in cfg.blocked:
        return False
    if cfg.users.get(e) is not None:
        return True
    return cfg.verify_email and e.rsplit("@", 1)[1] in cfg.allow_domains


def _cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    try:
        jar = SimpleCookie(raw)
    except Exception:
        return None
    return jar[name].value if name in jar else None


def _auth(handler: BaseHTTPRequestHandler, query: dict) -> tuple[str, str | None] | None:


    handler.waam_who = {"outcome": "ok", "email": None, "known": None}
    cfg = _refresh_auth()
    if cfg is not None:
        sid = _cookie_value(handler, SESSION_COOKIE)
        sess = oidc.session_for(sid)
        if sess is not None:
            valid = secrets.compare_digest(sess.get("authority", ""), oidc.session_authority(cfg))
            if valid and cfg.local:
                try:
                    account = ACCOUNTS.get(sess["email"])
                except accounts.AccountError:
                    raise oidc.SessionStoreError() from None
                valid = account is not None and secrets.compare_digest(
                    sess.get("credential", ""), accounts.credential_revision(account))
            if not valid:
                oidc.drop_session(sid)
                handler.waam_who = {"outcome": "REFUSED-session", "email": None, "known": False}
                return None
            workspace, known, why = _sso_decision(cfg, sess["email"], sess["verified"])
            if workspace is None:


                oidc.drop_session(sid)
                handler.waam_who = {"outcome": f"REFUSED-{why}",
                                    "email": sess["email"], "known": False}
                return None
            else:
                handler.waam_who = {"outcome": "ok-sso", "email": sess["email"],
                                    "known": known}
                return (workspace, None)


    if cfg is not None:
        gid = _cookie_value(handler, GUEST_COOKIE)
        if _live_guest(gid):
            handler.waam_who = {"outcome": "ok-guest", "email": None,
                                "known": False, "guest": True}
            return (_guest_workspace(gid), None)


    tokens = _refresh_tokens() if _TOKEN_GATE else _TOKENS
    if not tokens and not _TOKEN_GATE:
        return ("local", None)


    candidates = []
    qk = query.get("k", [None])[0]
    if qk:
        candidates.append(qk)
    hk = handler.headers.get("X-Waam-Token")
    if hk:
        candidates.append(hk)
    cookie_tok = _cookie_value(handler, "waam")
    if cookie_tok:
        candidates.append(cookie_tok)
    for cand in candidates:
        cb = cand.encode("utf-8", "surrogatepass")
        for tok, workspace in tokens.items():
            if hmac.compare_digest(cb, tok.encode("utf-8")):
                handler.waam_who = {"outcome": "ok-token", "email": None,
                                    "known": None}
                return (workspace, tok)
    if handler.waam_who["outcome"] == "ok":
        handler.waam_who["outcome"] = "REFUSED"
    return None


def _same_token(a: str | None, b: str | None) -> bool:

    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8"))


def _excel_scope(email: str) -> str | None:

    cfg = _refresh_auth()
    if cfg is None or urlparse(cfg.public_url).scheme != "https":
        return None
    credential = ""
    verified = True
    if cfg.local:
        account = ACCOUNTS.get(email)
        if account is None:
            return None
        credential = accounts.credential_revision(account)
        verified = account.verified
    workspace, _, _ = _sso_decision(cfg, email, verified)
    if workspace is None:
        return None
    return hashlib.sha256((oidc.session_authority(cfg) + ":" + credential).encode()).hexdigest()


def _is_admin(handler: BaseHTTPRequestHandler, cfg: oidc.AuthConfig | None) -> bool:


    if cfg is None or not cfg.admins:
        return False
    who = getattr(handler, "waam_who", None) or {}
    if who.get("outcome") != "ok-sso":
        return False
    email = (who.get("email") or "").strip().lower()
    return bool(email) and email in cfg.admins


def _admin_state(cfg: oidc.AuthConfig) -> dict:


    from . import approve

    blocked = set(cfg.blocked)
    roster = set(cfg.users)
    approved = set(cfg.approved)
    admins = set(cfg.admins)

    seen: dict[str, float] = {}
    verified: dict[str, bool] = {}
    store_emails: list[str] = []
    try:
        store_emails = ACCOUNTS.emails()
        for e in store_emails:
            acct = ACCOUNTS.get(e)
            if acct is not None:
                seen[e] = acct.last_seen
                verified[e] = acct.verified
    except accounts.AccountError:


        store_emails = []

    active = []
    for e in sorted(set(store_emails) | roster | approved | admins):
        if e in blocked:
            state = "blocked"
        elif e in roster:
            state = "roster"
        elif e in approved:
            state = "approved"
        elif cfg.approval_required:
            state = "pending"
        else:
            state = "open"
        ls = seen.get(e)
        active.append({
            "email": e,
            "state": state,
            "admin": e in admins,
            "verified": verified.get(e),
            "last_seen": (_dt.datetime.fromtimestamp(ls, _dt.timezone.utc)
                          .isoformat(timespec="seconds") if ls else None),
        })

    pending = [{"email": e} for e in approve.pending_requests()
               if e not in approved and e not in roster and e not in blocked]
    return {"ok": True, "active": active, "pending": pending}


def _client(handler: BaseHTTPRequestHandler) -> tuple[str, str | None]:


    peer = handler.client_address[0] if handler.client_address else "?"
    xff = handler.headers.get("X-Forwarded-For")
    if xff and _is_loopback(peer):


        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1], peer
    return peer, None


def _log_trace(context: str) -> None:


    try:
        print(f"[waam] {context} failed:", file=sys.stderr)
        traceback.print_exc()
    except Exception:
        pass


def _log_access(handler: BaseHTTPRequestHandler, path: str, workspace: str | None,
                outcome: str, email: str | None = None,
                known: bool | None = None, version: str | None = None) -> None:


    try:
        who, via = _client(handler)
        row = {
            "t": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "ip": who,
            "via": via,
            "workspace": workspace,
            "outcome": outcome,
            "path": path,
            "agent": (handler.headers.get("User-Agent") or "")[:200],
        }
        if email:
            row["email"] = email
        if known is not None:
            row["known"] = bool(known)
        if version is not None:
            row["version"] = version
        with _LOG_LOCK:
            with ACCESS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _log_auth(handler: BaseHTTPRequestHandler, path: str,
              workspace: str | None) -> None:

    who = getattr(handler, "waam_who", None) or {}
    _log_access(handler, path, workspace, who.get("outcome", "ok"),
                who.get("email"), who.get("known"))


def _activity_path(value: object) -> object:
    if not isinstance(value, str):
        return value
    parsed = urlparse(value)
    if not parsed.query:
        return value

    def sensitive(key: str) -> bool:
        normalized = key.casefold().replace("-", "_")
        return (normalized == "k" or normalized in {
                    "authorization", "code", "credential", "jwt", "nonce",
                    "password", "sid", "state", "ticket",
                }
                or normalized.endswith("_sid")
                or "token" in normalized or "session" in normalized
                or "secret" in normalized)

    query = [(key, "[redacted]" if sensitive(key) else item)
             for key, item in parse_qsl(parsed.query, keep_blank_values=True)]
    return parsed._replace(query=urlencode(query)).geturl()


def _job_dir(workspace: str, job: str) -> Path | None:


    if not isinstance(job, str) or not _JOB_RE.fullmatch(job):
        return None
    ws_root = (SAVES_DIR / workspace).resolve()
    path = (ws_root / job.replace(" ", "_")).resolve()
    if path.parent != ws_root or path == ws_root:
        return None
    return path


def _reindex_profile_path(workspace: str, name: str) -> Path | None:
    if not isinstance(name, str) or not _JOB_RE.fullmatch(name):
        return None
    ws_root = (SAVES_DIR / workspace).resolve()
    folder = (ws_root / "_reindex").resolve()
    path = (folder / f"{name}.json").resolve()
    if folder.parent != ws_root or path.parent != folder:
        return None
    return path


def _git_commit() -> str | None:

    try:
        from .backends.jbi import _git_sha

        sha = _git_sha()
        return None if sha == "unknown" else sha
    except Exception:
        return None


_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%% | WAAM Studio</title>
<style>
  :root { --bg:#101318; --panel:#171c24; --line:#2a3342; --ink:#dde3ec;
          --dim:#8b95a5; --accent:#4ea1ff; --bad:#ff5d52; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink); min-height:100vh;
         display:flex; align-items:center; justify-content:center; padding:24px;
         font:13px/1.55 system-ui, "Segoe UI", sans-serif; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:26px 28px; max-width:430px; width:100%; }
  .logo { font-weight:700; letter-spacing:.06em; color:var(--accent); font-size:12px; }
  h1 { font-size:17px; font-weight:600; margin:10px 0 12px; }
  p { color:var(--dim); margin-bottom:12px; }
  .who { color:var(--ink); font-family:ui-monospace, Consolas, monospace;
         background:#0c0f14; border:1px solid var(--line); border-radius:6px;
         padding:6px 9px; display:inline-block; word-break:break-all; }
  a.btn { display:block; text-align:center; text-decoration:none; margin-top:16px;
          background:var(--accent); color:#06121f; font-weight:600;
          border-radius:6px; padding:10px; }
  a.plain { color:var(--dim); }
  .bad { color:var(--bad); margin-bottom:12px; }

  .tabs { display:flex; gap:6px; margin:16px 0 14px; }
  .tab { flex:1; text-align:center; text-decoration:none; color:var(--dim);
         border:1px solid var(--line); border-radius:6px; padding:7px 6px; }
  .tab.on { color:var(--ink); border-color:var(--accent); background:#0c0f14; }
  label { display:block; color:var(--dim); font-size:12px; margin:10px 0 4px; }
  input { width:100%; background:#0c0f14; color:var(--ink);
          border:1px solid var(--line); border-radius:6px; padding:9px 10px;
          font:inherit; }
  input:focus { outline:none; border-color:var(--accent); }
  .hint { font-size:11px; margin:6px 0 0; }
  a.btn.ghost, button.btn.ghost { background:transparent; color:var(--ink);
                                  border:1px solid var(--line); }
  .rule { display:flex; align-items:center; gap:10px; margin:18px 0 4px;
          color:var(--dim); font-size:11px; }
  .rule::before, .rule::after { content:""; flex:1; height:1px;
                                background:var(--line); }
  button.btn { display:block; width:100%; margin-top:16px; cursor:pointer;
               background:var(--accent); color:#06121f; font-weight:600;
               border:0; border-radius:6px; padding:10px; font:inherit;
               font-weight:600; }
</style></head>
<body><div class="card">
<div class="logo">WAAM STUDIO</div>
%%BODY%%
</div></body></html>
"""


def _page(title: str, body: str) -> str:
    return _PAGE.replace("%%TITLE%%", _html.escape(title)).replace("%%BODY%%", body)


def _signin_page(cfg: oidc.AuthConfig, who: dict | None = None,
                 error: str = "", mode: str = "choose", email: str = "") -> str:


    note = ""
    outcome = (who or {}).get("outcome") or ""
    if outcome.startswith("REFUSED-") and (who or {}).get("email"):
        note = (f'<p class="bad">The account <span class="who">'
                f'{_html.escape(who["email"])}</span> no longer has access here, '
                f'and it has been signed out.</p>')
    if mode == "choose":
        return _choice_page(cfg, note)
    if cfg.local:
        return _local_signin_page(cfg, note, error, mode, email)
    return _page("Sign in", f"""
<h1>WAAM Studio</h1>
{note}
<p>Prepare parts, review toolpaths and export robot programs.
Sign in to access your saved builds and part library.</p>
<a class="btn" href="/auth/login">Sign in with {_html.escape(cfg.label)}</a>
<p style="margin-top:16px;font-size:12px;color:var(--dim,#888)">
Contact the administrator if you cannot sign in. If you received a workspace
link, you can open it directly.</p>
""")


def _choice_page(cfg: oidc.AuthConfig, note: str = "") -> str:


    if cfg.local:
        make = ('<a class="btn ghost" href="/auth/login?new=1">'
                'Create an account</a>')
        first = '<a class="btn" href="/auth/login?do=in">Sign in</a>'
    else:
        make = ""
        first = (f'<a class="btn" href="/auth/login">Sign in with '
                 f'{_html.escape(cfg.label)}</a>')
    access = ("New accounts require administrator approval." if cfg.approval_required else
              "Create an account to save builds and export files.")
    return _page("WAAM Studio", f"""
<h1>WAAM Studio</h1>
{note}
<p>Prepare parts, review toolpaths and export robot programs.
Sign in to access your saved builds and part library.</p>
{first}
{make}
<div class="rule"><span>or</span></div>
<form method="POST" action="/auth/guest">
  <button class="btn ghost" type="submit">Continue as a guest</button>
</form>
<p class="hint">Guest access supports slicing and preview. <strong>Sign in to save or export.</strong>
{access}</p>
""")


def _local_signin_page(cfg: oidc.AuthConfig, note: str = "", error: str = "",
                       mode: str = "in", email: str = "") -> str:


    bad = (f'<p class="bad">{_html.escape(error)}</p>' if error else "")
    making = mode == "up"
    rule = ("New accounts require administrator approval." if cfg.approval_required else
            "Create an account to save builds and upload parts.")
    if not cfg.open_signin:
        where = (" or an address at " + ", ".join(cfg.allow_domains)
                 if cfg.verify_email and cfg.allow_domains else "")
        rule += f" Registration is limited to approved users{where}."
    advice = (f'<p class="hint">At least {accounts.MIN_PASSWORD_LEN} characters. '
              'Use a unique password for this account.</p>' if making else "")
    help_text = ("Do not reuse your LeTourneau password. Contact the administrator if you need help registering." if making else
                 '<a class="plain" href="/auth/forgot">Forgot password?</a>'
                 ' &nbsp;·&nbsp; No account yet? '
                 '<a class="plain" href="/auth/login?new=1">Create an account</a>')


    passkey_block = ("" if making or not passkeys.available() else r"""
<div class="rule"><span>or</span></div>
<button class="btn ghost" type="button" id="passkeyBtn">Sign in with a passkey</button>
<p class="bad" id="passkeyMsg" hidden></p>
<p class="hint">To add a passkey, sign in with your password and choose &ldquo;Add a passkey&rdquo; in the workbench.</p>
<script>
(function(){
  function u2b(s){s=s.replace(/-/g,'+').replace(/_/g,'/');s+='='.repeat((4-s.length%4)%4);var b=atob(s),a=new Uint8Array(b.length);for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return a.buffer;}
  function b2u(buf){var a=new Uint8Array(buf),s='';for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
  var btn=document.getElementById('passkeyBtn'),msg=document.getElementById('passkeyMsg');
  function fail(t){msg.textContent=t;msg.hidden=false;btn.disabled=false;}
  if(!window.PublicKeyCredential){btn.disabled=true;btn.title='This browser has no passkey support.';return;}
  btn.onclick=async function(){
    msg.hidden=true;btn.disabled=true;
    try{
      var r=await fetch('/auth/passkey/login/options',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      var j=await r.json(); if(!j.ok){return fail(j.error||'Passkeys are unavailable.');}
      var o=j.options; o.challenge=u2b(o.challenge);
      if(o.allowCredentials){o.allowCredentials.forEach(function(c){c.id=u2b(c.id);});}
      var a; try{a=await navigator.credentials.get({publicKey:o});}catch(e){return fail('Passkey cancelled or unavailable.');}
      var cred={id:a.id,rawId:b2u(a.rawId),type:a.type,response:{authenticatorData:b2u(a.response.authenticatorData),clientDataJSON:b2u(a.response.clientDataJSON),signature:b2u(a.response.signature),userHandle:a.response.userHandle?b2u(a.response.userHandle):null},clientExtensionResults:{}};
      r=await fetch('/auth/passkey/login/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({handle:j.handle,credential:cred})});
      j=await r.json();
      if(j.ok){location.href=j.redirect||'/';}else{fail(j.error||'Passkey sign-in failed.');}
    }catch(e){fail('Passkey sign-in failed. Use your password instead.');}
  };
})();
</script>""")
    return _page("Sign in", f"""
<h1>WAAM Studio</h1>
{note}{bad}
<p>Sign in to access your saved builds and part library.</p>

<div class="tabs">
  <a class="tab{'' if making else ' on'}" href="/auth/login?do=in">Sign in</a>
  <a class="tab{' on' if making else ''}" href="/auth/login?new=1">Create an account</a>
</div>
{'<p class="hint">' + _html.escape(rule) + '</p>' if making else ''}

<form method="POST" action="{'/auth/register' if making else '/auth/signin'}">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" autocomplete="username"
         autocapitalize="none" spellcheck="false" required
         placeholder="you@letu.edu" value="{_html.escape(email)}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required
         autocomplete="{'new-password' if making else 'current-password'}"
         {'minlength="' + str(accounts.MIN_PASSWORD_LEN) + '"' if making else ''}>
  {advice}
  <button class="btn" type="submit">
    {('Request access' if cfg.approval_required else 'Create account') if making else 'Sign in'}</button>
</form>
{passkey_block}
<p style="margin-top:16px;font-size:12px;color:var(--dim,#888)">
{help_text}
</p>
<p style="font-size:12px"><a class="plain" href="/signin">&larr; Back to sign in</a></p>
""")


def _verify_page(email: str, error: str = "", sent: bool = True) -> str:


    bad = (f'<p class="bad">{_html.escape(error)}</p>' if error else "")
    lead = ("We sent a 6-digit code to" if sent else "Enter the 6-digit code for")
    return _page("Enter your code", f"""
<h1>Check your email</h1>
{bad}
<p>{lead} <span class="who">{_html.escape(email)}</span>.
The code expires in 15 minutes.</p>
<form method="POST" action="/auth/verify">
  <input type="hidden" name="email" value="{_html.escape(email)}">
  <label for="code">6-digit code</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
         pattern="[0-9 ]*" maxlength="12" required autofocus
         placeholder="123456" style="letter-spacing:.35em; font-size:18px;
         font-family:ui-monospace, Consolas, monospace">
  <button class="btn" type="submit">Continue</button>
</form>
<form method="POST" action="/auth/resend" style="margin-top:10px">
  <input type="hidden" name="email" value="{_html.escape(email)}">
  <button class="btn ghost" type="submit">Send another code</button>
</form>
<p class="hint">Check your spam folder. If the address is incorrect,
<a class="plain" href="/auth/login?new=1">enter a different address</a>.</p>
""")


def _forgot_page(error: str = "") -> str:

    bad = (f'<p class="bad">{_html.escape(error)}</p>' if error else "")
    return _page("Reset password", f"""
<h1>Reset password</h1>
{bad}
<p>Enter your email address. If an account exists, we will send a password reset code.</p>
<form method="POST" action="/auth/forgot">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" autocomplete="username"
         autocapitalize="none" spellcheck="false" required autofocus
         placeholder="you@letu.edu">
  <button class="btn" type="submit">Send reset code</button>
</form>
<p class="hint"><a class="plain" href="/auth/login?do=in">&larr; Back to sign in</a></p>
""")


def _reset_page(email: str, error: str = "") -> str:


    bad = (f'<p class="bad">{_html.escape(error)}</p>' if error else "")
    return _page("Set a new password", f"""
<h1>Set a new password</h1>
{bad}
<p>If an account exists for <span class="who">{_html.escape(email)}</span>,
a reset code has been sent. The code expires in 15 minutes.</p>
<form method="POST" action="/auth/reset">
  <input type="hidden" name="email" value="{_html.escape(email)}">
  <label for="code">6-digit code</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
         pattern="[0-9 ]*" maxlength="12" required autofocus placeholder="123456"
         style="letter-spacing:.35em; font-size:18px;
         font-family:ui-monospace, Consolas, monospace">
  <label for="password">New password</label>
  <input id="password" name="password" type="password" required
         autocomplete="new-password" minlength="{accounts.MIN_PASSWORD_LEN}">
  <p class="hint">At least {accounts.MIN_PASSWORD_LEN} characters, and not
  your LeTourneau password.</p>
  <button class="btn" type="submit">Reset password and sign in</button>
</form>
<p class="hint">Check your spam folder or
<a class="plain" href="/auth/forgot">try a different address</a>.</p>
""")


def _message_page(title: str, detail: str, retry: bool = False) -> str:
    again = ('<a class="btn" href="/auth/login">Try again</a>' if retry else
             '<a class="btn" href="/">Back to the workbench</a>')
    return _page(title, f"""
<h1>{_html.escape(title)}</h1>
<p>{detail}</p>
{again}
""")


def _denied_page(cfg: oidc.AuthConfig, email: str, why: str) -> str:


    shown = _html.escape(email) if email else "Email address unavailable"
    if why == "pending":


        return _page("Request received", f"""
<h1>Access request received</h1>
<p>Email verified: <span class="who">{shown}</span>.</p>
<p>Your account is awaiting administrator approval. The administrator has been notified.</p>
<p>After approval, <a class="plain" href="/signin">sign in</a> with the same account. Your saved work is kept.</p>
""")
    if why == "blocked":
        reason = "Access to this account has been revoked."
    elif why == "unverified":
        reason = ("The sign-in provider did not verify your email address.")
    else:
        reason = "This account has not been granted access."
    return _page("Access unavailable", f"""
<h1 class="bad">Access unavailable</h1>
<p>You signed in as <span class="who">{shown}</span>.</p>
<p>{reason}</p>
<p>Contact the administrator to request access for this email address.</p>
<p><a class="plain" href="/auth/logout">Sign out</a> to use another account.</p>
""")


def _admin_denied_page() -> str:


    return _page("Not authorized", """
<h1 class="bad">Not authorized</h1>
<p>Administrator access is required. Contact the administrator if you need access.</p>
<p><a class="plain" href="/">Back to the workbench</a></p>
""")


_ADMIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Access | WAAM Studio</title>
<style>
  :root { --bg:#101318; --panel:#171c24; --panel2:#0c0f14; --line:#2a3342;
          --ink:#dde3ec; --dim:#8b95a5; --accent:#4ea1ff; --bad:#ff5d52;
          --ok:#4bd08b; --warn:#e0b23c; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink); min-height:100vh; padding:20px;
         font:13px/1.5 system-ui, "Segoe UI", sans-serif; }
  .wrap { max-width:900px; margin:0 auto; }
  header { display:flex; align-items:center; gap:12px; margin-bottom:8px; }
  .logo { font-weight:700; letter-spacing:.06em; color:var(--accent); font-size:12px; }
  h1 { font-size:17px; font-weight:600; }
  header a.plain { margin-left:auto; color:var(--dim); text-decoration:none; }
  .lede { color:var(--dim); margin-bottom:14px; max-width:70ch; }
  .note { min-height:18px; margin:6px 0 14px; font-size:12px; }
  .note.ok { color:var(--ok); }
  .note.bad { color:var(--bad); }
  section { background:var(--panel); border:1px solid var(--line);
            border-radius:10px; padding:14px 16px; margin-bottom:16px; }
  h2 { font-size:14px; font-weight:600; margin-bottom:10px; }
  .count { color:var(--dim); font-weight:400; font-size:12px; margin-left:6px; }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; color:var(--dim); font-weight:500; font-size:11px;
       letter-spacing:.04em; padding:4px 8px; border-bottom:1px solid var(--line); }
  td { padding:7px 8px; border-bottom:1px solid var(--panel2); vertical-align:middle; }
  tr:last-child td { border-bottom:0; }
  tr.selected { background:var(--panel2); outline:2px solid var(--accent); }
  .mono { font-family:ui-monospace, Consolas, monospace; word-break:break-all; }
  .dim { color:var(--dim); }
  td.act { text-align:right; white-space:nowrap; }
  .empty { color:var(--dim); font-size:12px; }
  button { cursor:pointer; font:inherit; font-weight:600; border-radius:6px;
           border:1px solid var(--line); padding:5px 12px; background:var(--panel2);
           color:var(--ink); }
  button.ok { background:var(--accent); color:#06121f; border-color:var(--accent); }
  button.bad { background:transparent; color:var(--bad); border-color:var(--bad); }
  button:disabled { opacity:.5; cursor:default; }
  .tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:20px;
         border:1px solid var(--line); color:var(--dim); }
  .tag.roster { color:var(--ok); border-color:var(--ok); }
  .tag.approved { color:var(--accent); border-color:var(--accent); }
  .tag.pending { color:var(--warn); border-color:var(--warn); }
  .tag.blocked { color:var(--bad); border-color:var(--bad); }
  .tag.admin { color:var(--warn); border-color:var(--warn); margin-left:6px; }
</style></head>
<body><div class="wrap">
<header>
  <span class="logo">WAAM STUDIO</span><h1>Access</h1>
  <a class="plain" href="/">Back to the workbench</a>
</header>
<p class="lede">Manage access requests and accounts. Revoking access signs the user out and
blocks further requests.</p>
<div class="note" id="msg" role="status" aria-live="polite"></div>
<div id="board"></div>
</div>
<script>
(function(){
  "use strict";
  var board = document.getElementById("board");
  var msg = document.getElementById("msg");
  var pendingEmail = (new URLSearchParams(window.location.search)
                      .get("pending") || "").trim().toLowerCase();
  var selectedRow = null;
  function fmtSeen(iso){
    if(!iso) return "Not available";
    try{ return new Date(iso).toLocaleString(); }catch(e){ return iso; }
  }
  function note(text, bad){
    msg.textContent = text;
    msg.className = "note " + (bad ? "bad" : "ok");
  }
  function post(path, email){
    var body = new URLSearchParams(); body.set("email", email);
    return fetch(path, {method:"POST",
        headers:{"Content-Type":"application/x-www-form-urlencoded"},
        body: body.toString()}).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){
        return {status:r.status, j:j};
      });
    });
  }
  function act(path, email, verb){
    note((verb === "approve" ? "Approving " : "Revoking access for ") + email + "\\u2026", false);
    return post(path, email).then(function(res){
      if(res.status === 200 && res.j && res.j.ok){
        note(res.j.message || (verb === "approve" ? "Access approved for " : "Access revoked for ") + email + ".", false);
        load();
      }else{
        note((res.j && res.j.error) ? res.j.error
             : ("Could not update access (" + res.status + "). Retry the request."), true);
      }
    }).catch(function(){ note("Could not reach the server.", true); });
  }
  function approve(email){ return act("/api/admin/approve", email, "approve"); }
  function revoke(email){
    if(!window.confirm("Revoke access for " + email +
        "?\\nThis signs them out and blocks further requests.")) return;
    return act("/api/admin/revoke", email, "revoke");
  }
  function actionButton(label, cls, email, fn){
    var b = document.createElement("button");
    b.textContent = label; b.className = cls;
    b.addEventListener("click", function(){ fn(email); });
    return b;
  }
  function tag(text){
    var s = document.createElement("span");
    s.className = "tag " + text; s.textContent = text.charAt(0).toUpperCase() + text.slice(1); return s;
  }
  function pendingSection(rows){
    var sec = document.createElement("section");
    var h = document.createElement("h2");
    h.textContent = "Pending access requests";
    var c = document.createElement("span"); c.className = "count";
    c.textContent = rows.length; h.appendChild(c); sec.appendChild(h);
    if(!rows.length){
      var p = document.createElement("p"); p.className = "empty";
      p.textContent = "No pending access requests."; sec.appendChild(p); return sec;
    }
    var tbl = document.createElement("table");
    tbl.innerHTML = "<thead><tr><th>Address</th><th></th></tr></thead>";
    var tb = document.createElement("tbody");
    rows.forEach(function(row){
      var tr = document.createElement("tr");
      if(row.email === pendingEmail){
        tr.className = "selected"; tr.tabIndex = -1; selectedRow = tr;
      }
      var e = document.createElement("td"); e.className = "mono";
      e.textContent = row.email; tr.appendChild(e);
      var a = document.createElement("td"); a.className = "act";
      a.appendChild(actionButton("Approve", "ok", row.email, approve));
      tr.appendChild(a); tb.appendChild(tr);
    });
    tbl.appendChild(tb); sec.appendChild(tbl); return sec;
  }
  function activeSection(rows){
    var sec = document.createElement("section");
    var h = document.createElement("h2");
    h.textContent = "Accounts";
    var c = document.createElement("span"); c.className = "count";
    c.textContent = rows.length; h.appendChild(c); sec.appendChild(h);
    if(!rows.length){
      var p = document.createElement("p"); p.className = "empty";
      p.textContent = "No accounts yet."; sec.appendChild(p); return sec;
    }
    var tbl = document.createElement("table");
    tbl.innerHTML = "<thead><tr><th>Address</th><th>State</th>" +
                    "<th>Last seen</th><th></th></tr></thead>";
    var tb = document.createElement("tbody");
    rows.forEach(function(row){
      var tr = document.createElement("tr");
      var e = document.createElement("td"); e.className = "mono";
      e.textContent = row.email;
      if(row.admin) e.appendChild(tag("admin"));
      tr.appendChild(e);
      var st = document.createElement("td");
      st.appendChild(tag(row.state)); tr.appendChild(st);
      var ls = document.createElement("td"); ls.className = "dim";
      ls.textContent = fmtSeen(row.last_seen); tr.appendChild(ls);
      var a = document.createElement("td"); a.className = "act";
      if(row.state === "blocked"){
        var s = document.createElement("span"); s.className = "dim";
        s.textContent = "Revoked"; a.appendChild(s);
      }else if(row.admin){


        var d = document.createElement("span"); d.className = "dim";
        d.textContent = "Administrator"; a.appendChild(d);
      }else{
        a.appendChild(actionButton("Revoke", "bad", row.email, revoke));
      }
      tr.appendChild(a); tb.appendChild(tr);
    });
    tbl.appendChild(tb); sec.appendChild(tbl); return sec;
  }
  function render(state){
    selectedRow = null;
    board.innerHTML = "";
    board.appendChild(pendingSection(state.pending || []));
    board.appendChild(activeSection(state.active || []));
    if(selectedRow){
      selectedRow.scrollIntoView({block:"center"});
      selectedRow.focus({preventScroll:true});
    }
  }
  function load(){
    return fetch("/api/admin/state").then(function(r){
      if(r.status !== 200){ note("Access unavailable (" + r.status + "). Sign in with an administrator account.", true); return; }
      return r.json().then(function(j){
        if(j && j.ok){ render(j); }
        else{ note((j && j.error) || "Could not load accounts. Refresh the page to retry.", true); }
      });
    }).catch(function(){ note("Could not reach the server.", true); });
  }
  load();
})();
</script>
</body></html>
"""


def _admin_page(cfg: oidc.AuthConfig) -> str:


    return _ADMIN_PAGE


def _platter_from_query(query: dict, em: dict, workspace: str, email: str | None = None):


    from .emit import PlatterState

    def flag(k):
        v = query.get(k, [None])[0]
        return None if v is None else v.strip().lower() in ("1", "true", "yes", "on")

    clear, block = flag("clear"), flag("block")
    fixture = (query.get("fixture", [""])[0] or "").strip() or None

    by = (query.get("by", [""])[0] or "").strip() or email or workspace
    if clear is None or block is None:
        return None, ("Platter check required. Confirm the physical setup in the "
                      "export dialog.")
    if not clear:
        return None, "Inspect the physical platter and confirm it is clear before exporting."
    sliced_with = bool(em.get("fixture"))
    if block != sliced_with:
        return None, (f"The training block was declared {'present' if block else 'absent'}, "
                      f"but this slice was checked with it "
                      f"{'present' if sliced_with else 'absent'}. Set the platter "
                      "setting to match the physical setup, then check and export again.")
    return PlatterState(clear=True, training_block_on=block, fixture=fixture,
                        attested_by=by), None


def _new_signin_session(cfg: oidc.AuthConfig, email: str, verified: bool,
                       account: accounts.Account | None = None) -> str:
    if cfg.local and account is None:
        raise oidc.SessionStoreError()
    return oidc.new_session(email, verified, authority=oidc.session_authority(cfg),
                            credential=accounts.credential_revision(account) if account else "")


def _behind_tls(handler: BaseHTTPRequestHandler) -> bool:


    headers = getattr(handler, "headers", None)
    proto = (headers.get("X-Forwarded-Proto") if headers else None) or ""
    return proto.split(",")[0].strip().lower() == "https"


def _controller_job_taken(handler, name: str) -> bool:


    from .backends.jbi import controller_job_names

    if name not in controller_job_names():
        return False
    handler._json(409, {"ok": False, "reason": "job_name", "error":
                        f"{name} is already a job on the LC controller. Loading this "
                        "file would overwrite it; choose a new job name."})
    return True


def _ctwd_from_query(query: dict, em: dict) -> tuple[float | None, str | None]:

    checked = em.get("weld_ctwd_mm")
    if "ctwd" not in query:
        return checked, None
    raw = query["ctwd"][0].strip()
    try:
        asked = weld_ctwd.resolve(raw or None)[0]
    except ValueError as exc:
        return None, str(exc)
    if abs(asked - weld_ctwd.resolve(checked)[0]) > 1e-9:
        return None, "Weld CTWD changed since this part was checked. Slice & check again before exporting"
    return checked, None


def _folder_from_query(query) -> tuple[str | None, str | None]:

    from .backends.jbi import _folder_lines

    raw = query.get("folder", [""])[0].strip()
    if not raw:
        return None, None
    try:
        _folder_lines(raw)
    except ValueError:
        return None, ("Controller job folder: up to 32 letters, digits, - or _, "
                      "exactly as the pendant's JOB FOLDER LIST shows it")
    return raw, None


class _Handler(BaseHTTPRequestHandler):
    server_version = "waam-workbench"

    sys_version = ""


    timeout = 30

    def handle_one_request(self) -> None:
        self._t0 = time.monotonic()
        try:
            super().handle_one_request()
        except oidc.SessionStoreError as exc:


            self.close_connection = True
            self._send(503, json.dumps({"ok": False, "error": str(exc)}).encode(),
                       "application/json", extra={"Retry-After": "5"})

    def log_request(self, code="-", size="-"):


        try:
            ip, _via = _client(self)
            who = getattr(self, "waam_who", None) or {}
            try:
                status = int(code)
            except (TypeError, ValueError):
                status = str(code)
            row = {"t": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                   "ip": ip, "method": getattr(self, "command", None),
                   "path": _activity_path(getattr(self, "path", None)), "status": status,
                   "ms": round((time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000, 1)}
            if who.get("email"):
                row["email"] = who["email"]
            with _ACT_LOCK:
                try:
                    if ACTIVITY_LOG.stat().st_size > ACTIVITY_LOG_MAX_BYTES:
                        ACTIVITY_LOG.replace(ACTIVITY_LOG.with_name(ACTIVITY_LOG.name + ".1"))
                except FileNotFoundError:
                    pass
                with ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
        except Exception:
            pass

    def end_headers(self) -> None:


        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")


        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if _behind_tls(self):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _send(self, code: int, body: bytes, ctype: str, cookie: str | None = None,
              extra: dict[str, str] | None = None) -> None:
        extra = dict(extra or {})


        if (len(body) >= 1024 and not self.path.startswith(("/auth/", "/signin", "/sign-in", "/login"))
                and "Content-Encoding" not in extra and "Content-Disposition" not in extra):
            extra["Vary"] = "Accept-Encoding"
            if web_assets.accepts_gzip(self.headers.get("Accept-Encoding", "")):
                body = gzip.compress(body, compresslevel=1, mtime=0)
                extra["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for k, v in (extra or {}).items():


            self.send_header(k, str(v).translate(_HEADER_STRIP))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict, cookie: str | None = None) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json", cookie)

    def _desktop_download(self, opened: desktop_release.OpenRelease, workspace: str,
                          cookie: str | None = None, path: str = "/download/desktop") -> None:
        with opened:
            release = opened.manifest
            source = opened.source
            try:
                self.send_response(200)
                ctype = ("application/x-apple-diskimage"
                         if Path(release["file"]).suffix.lower() == ".dmg"
                         else "application/octet-stream")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(release["size"]))
                self.send_header("Content-Disposition", f'attachment; filename="{release["file"]}"')
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                who = getattr(self, "waam_who", None) or {}
                _log_access(self, path, workspace, "desktop-download",
                            who.get("email"), who.get("known"), release["version"])
                for block in iter(lambda: source.read(64 * 1024), b""):
                    self.wfile.write(block)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _wrong_workspace(self, claimed: str, actual: str) -> bool:


        if not claimed or claimed == actual:
            return False
        self._json(409, {"ok": False, "error":
                         f"this tab belongs to '{claimed}' but the browser is "
                         f"now signed in as '{actual}' - another link was "
                         f"opened here. Reopen your own link to continue."})
        return True

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _deny(self, path: str) -> None:


        cfg = _refresh_auth()
        if cfg is not None and not path.startswith("/api/"):
            who = getattr(self, "waam_who", None) or {}
            self._send(401, _signin_page(cfg, who).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        self._send(401, b"waam workbench: append ?k=<token> (printed by the server)\n",
                   "text/plain; charset=utf-8")

    def _guest_gate(self, path: str) -> bool:


        if (getattr(self, "waam_who", None) or {}).get("guest") \
                and path not in GUEST_ALLOWED:
            _log_access(self, path, None, "guest-denied")
            self._json(403, {"ok": False, "error":
                             "Guest access supports slicing and preview. Sign in to "
                             "save builds and export files."})
            return True
        return False

    def _account_access(self, path: str, query: dict) -> tuple[str, str | None] | None:

        auth = _auth(self, query)
        if auth is None:
            _log_auth(self, path, None)
            self._deny(path)
            return None
        workspace, _tok = auth
        _log_auth(self, path, workspace)
        if self._guest_gate(path):
            return None
        return auth

    def _admin_gate(self, path: str, workspace: str | None) -> bool:


        if path.startswith("/api/admin/") and not _is_admin(self, _refresh_auth()):
            self._deny_admin(path, workspace)
            return True
        return False

    def _deny_admin(self, path: str, workspace: str | None) -> None:


        email = (getattr(self, "waam_who", None) or {}).get("email")
        _log_access(self, path, workspace, "admin-denied", email)
        self._json(403, {"ok": False, "error": "administrators only"})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/run-logs/excel/"):
            self._excel_feed(url.path)
            return
        query = parse_qs(url.query, keep_blank_values=url.path in ("/api/sense-job", "/api/cycle-job"))
        with_blanks = parse_qs(url.query, keep_blank_values=True)
        for key in ("ctwd", "measure_every", "measure_times", "measure_pause", "measure_clean", "measure_pre_weld"):
            if key not in query and key in with_blanks:
                query[key] = [""]
        if url.path.startswith("/auth/"):
            self._auth_route(url.path, query)
            return
        if url.path.rstrip("/").lower() in SIGN_IN_ALIASES:


            cfg = _refresh_auth()
            _log_access(self, url.path, None,
                        "signin-door" if cfg else "signin-door-off")
            self._redirect("/auth/login" if cfg else "/")
            return
        auth = self._account_access(url.path, query)
        if auth is None:
            return
        workspace, tok = auth
        if self._admin_gate(url.path, workspace):
            return
        if url.path in ("/api/jbi", "/api/cycle-job", "/api/sense-job", "/api/layer-sense-job", "/api/ros2", "/api/gcode"):


            if self._cross_site_post():
                return
            if not self._valid_run_name(query):
                return
        cookie = None
        qk = query.get("k", [None])[0]
        if _same_token(qk, tok):


            cookie = f"waam={tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400"
        email = (getattr(self, "waam_who", None) or {}).get("email")
        if url.path == "/" and "visual" in query:


            asset = web_assets.find(query["visual"][0])
            if asset is None:
                self._send(404, b"unknown visual", "text/plain")
                return
            compressed = web_assets.accepts_gzip(self.headers.get("Accept-Encoding", ""))
            headers = {"Vary": "Accept-Encoding"}
            if compressed:
                headers["Content-Encoding"] = "gzip"
            self._send(200, asset["gzip"] if compressed else asset["body"], asset["type"], cookie, headers)
        elif url.path == "/":
            self._send(200, _default_page(workspace, email).encode("utf-8"),
                       "text/html; charset=utf-8", cookie)
        elif url.path == "/run-logs":
            if not email or (getattr(self, "waam_who", None) or {}).get("guest"):
                self._json(403, {"ok": False, "error": "Sign in to use run logs."})
                return
            self._send(200, render_run_logs().encode("utf-8"), "text/html; charset=utf-8", cookie)
        elif url.path == "/api/run-logs/excel-access":
            if not self._wrong_workspace(query.get("ws", [""])[0], workspace):
                self._api_excel_access(workspace)
        elif url.path in ("/api/run-logs", "/api/run-logs/detail", "/api/run-logs/export"):
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            action = "list" if url.path == "/api/run-logs" else url.path.rsplit("/", 1)[1]
            run_log_routes.dispatch(self, SAVES_DIR, workspace, action, query,
                                    admin=_is_admin(self, _refresh_auth()))
        elif url.path == "/run-sheet":
            if not email or workspace.startswith("guest-"):
                self._json(403, {"ok": False, "error": "Sign in to keep a run log."})
                return
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            try:
                with _ws_lock(workspace):
                    record = runs.active(SAVES_DIR, workspace)
                    rows, times = _run_sheet_data(workspace, record)
                self._send(200, render_run_sheet(record, rows, times, workspace=workspace).encode("utf-8"),
                           "text/html; charset=utf-8", cookie)
            except (ValueError, OSError):
                self._json(500, {"ok": False, "error": "The run sheet could not be read. Please retry."})
        elif url.path == "/admin":


            cfg = _refresh_auth()
            if not _is_admin(self, cfg):
                _log_access(self, url.path, workspace, "admin-denied", email)
                self._send(403, _admin_denied_page().encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            self._send(200, _admin_page(cfg).encode("utf-8"),
                       "text/html; charset=utf-8", cookie)
        elif url.path == "/api/admin/state":
            cfg = _refresh_auth()
            if not _is_admin(self, cfg):
                self._deny_admin(url.path, workspace)
                return
            self._json(200, _admin_state(cfg))
        elif url.path == "/api/desktop-release":
            releases = [
                {"id": key,
                 **{field: release[field] for field in (
                     "version", "size", "sha256", "platform", "signed",
                     "notarized", "published_at", "notes",
                 )},
                 "url": "/download/desktop/" + key,
                 **({"signature": release["signature"]} if "signature" in release else {})}
                for key, release in desktop_release.load_all().items()
            ]
            self._json(200, {"available": bool(releases), "releases": releases})
        elif url.path == "/download/desktop" or url.path.startswith("/download/desktop/"):

            key = (desktop_release.DEFAULT_PLATFORM if url.path == "/download/desktop"
                   else url.path[len("/download/desktop/"):])
            opened = (desktop_release.open_release(platform=key)
                      if key in desktop_release.PLATFORMS else None)
            if opened is None:
                self._send(404, b"No desktop build has been published on this server yet.\n",
                           "text/plain; charset=utf-8")
            else:
                self._desktop_download(opened, workspace, cookie, url.path)
        elif url.path == "/api/gcode":
            with _ws_lock(workspace):
                if self._registration_pending(workspace):
                    return
                with _LOCK:
                    st = _STATES.get(workspace) or {}
                    tp = st.get("toolpath")
                    part = (st.get("emit") or {}).get("part", "part")
                if tp is None:
                    self._send(404, b"No sliced build is available.", "text/plain")
                elif self._stale_gen(workspace, query):


                    return
                else:


                    name = re.sub(r"\.stl$", "", str(part), flags=re.I)
                    name = re.sub(r"[^A-Za-z0-9 _.-]", "_", name).strip(" .")[:100] or "part"
                    body = to_gcode(tp).encode("utf-8")
                    archive = self._archive(workspace, "gcode", body, name=name)
                    filed = self._file_run(workspace, "GCODE", body, name=name, ext=".gcode", query=query)
                    self._send(200, body,
                               "text/plain; charset=utf-8", cookie, extra={
                                   "Content-Disposition": f'attachment; filename="{name}.gcode"',
                                   "X-Waam-Archive": archive,
                                   "X-Waam-Run": filed,
                               })
        elif url.path in ("/api/run", "/api/run/cooling", "/api/run/decay"):
            if not self._wrong_workspace(query.get("ws", [""])[0], workspace):
                self._api_run(workspace, "summary" if url.path == "/api/run" else url.path.rsplit("/", 1)[1], query)
        elif url.path == "/api/thermal-plan":
            if not self._wrong_workspace(query.get("ws", [""])[0], workspace):
                with _ws_lock(workspace):
                    self._api_thermal_plan(workspace, query)
        elif url.path == "/api/whoami":


            who = getattr(self, "waam_who", None) or {}
            cfg = _refresh_auth()
            self._json(200, {"ok": True, "workspace": workspace,
                             "gen": _state(workspace).get("gen"),
                             "email": who.get("email"),
                             "known": who.get("known"),
                             "sso": who.get("outcome") == "ok-sso",
                             "guest": bool(who.get("guest")),


                             "admin": _is_admin(self, cfg),
                             "library_owner": (stl_library.owner_id(who["email"])
                                               if who.get("outcome") == "ok-sso" else None),


                             "sign_in_url": SIGN_IN_PATH if cfg else None,
                             "sign_in_label": cfg.label if cfg else None})
        elif url.path == "/api/preferences":
            self._api_preferences(query)
        elif url.path in ("/api/jbi", "/api/cycle-job"):
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            with _ws_lock(workspace):
                if not self._registration_pending(workspace):
                    if url.path == "/api/cycle-job":
                        self._api_cycle_job(workspace, query)
                    else:
                        self._api_jbi(workspace, query)
        elif url.path == "/api/sense-job":
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            with _ws_lock(workspace):
                self._api_sense_job(workspace, query)
        elif url.path in ("/api/reindex/profile", "/api/reindex/profiles"):
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            with _ws_lock(workspace):
                if url.path == "/api/reindex/profile":
                    self._api_reindex_profile(workspace, query)
                else:
                    self._api_reindex_profiles(workspace)
        elif url.path == "/api/layer-sense-job":
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            with _ws_lock(workspace):
                if not self._registration_pending(workspace):
                    self._api_layer_sense_job(workspace, query)
        elif url.path == "/api/ros2":
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            with _ws_lock(workspace):
                if not self._registration_pending(workspace):
                    self._api_ros2(workspace, query)
        elif url.path == "/api/fixtures":
            from . import cell_lc
            fx = cell_lc.CALIBRATION.get("fixtures") or {}
            self._json(200, {"ok": True, "fixtures": sorted(fx),
                             "active": cell_lc.CALIBRATION.get("active_fixture")})
        elif url.path in ("/api/library", "/api/library/file", "/api/admin/library"):
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            self._api_library_get(workspace, url.path, query)
        elif url.path == "/api/jobs":
            if self._wrong_workspace(query.get("ws", [""])[0], workspace):
                return
            self._api_jobs(workspace)
        elif url.path == "/api/passkey/list":
            self._api_passkey(url.path, workspace)
        else:
            self._send(404, b"not found", "text/plain")

    def _api_preferences(self, query: dict) -> None:

        who = getattr(self, "waam_who", None) or {}
        email = who.get("email")
        if who.get("outcome") != "ok-sso" or not email or who.get("guest"):
            self._json(403, {"ok": False, "error": "Sign in to save personal display preferences."})
            return
        identity = ui_preferences.owner_id(email)
        expected = query.get("owner", [""])[0]

        if (self.command == "POST" or "owner" in query) and not _same_token(expected, identity):
            self._json(409, {"ok": False, "error":
                            "The signed-in account changed. Reopen Settings before saving preferences."})
            return
        try:
            if self.command == "POST":
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self._json(415, {"ok": False, "error": "Send UI preferences as application/json."})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > ui_preferences.MAX_BYTES:
                    self._json(413 if length > ui_preferences.MAX_BYTES else 400,
                               {"ok": False, "error": "Send a complete UI preferences object (at most 4 KB)."})
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ui_preferences.InvalidPreferences("The preferences request was incomplete. Retry.")
                preferences = UI_PREFERENCES.save(email, ui_preferences.decode(body))
            else:
                preferences = UI_PREFERENCES.get(email)
        except ui_preferences.InvalidPreferences as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except ui_preferences.PreferencesUnavailable as exc:
            self._json(503, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "owner_id": identity, "preferences": preferences})


    def _auth_route(self, path: str, query: dict) -> None:


        cfg = _refresh_auth()
        if cfg is None or path not in ("/auth/login", "/auth/callback",
                                       "/auth/logout", "/auth/signin",
                                       "/auth/register", "/auth/guest",
                                       "/auth/verify", "/auth/resend",
                                       "/auth/forgot", "/auth/reset",
                                       "/auth/passkey/login/options",
                                       "/auth/passkey/login/verify"):


            self._send(404, b"not found", "text/plain")
            return
        if path.startswith("/auth/passkey/login/"):
            self._auth_passkey_login(cfg, path)
            return
        if path == "/auth/logout":
            sid = _cookie_value(self, SESSION_COOKIE)
            had = oidc.drop_session(sid)
            _log_access(self, path, None, "logout" if had else "logout-none")
            self._redirect("/", f"{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; "
                                "SameSite=Lax; Max-Age=0")
            return
        if path == "/auth/login":
            want = query.get("do", [""])[0] or (
                "up" if query.get("new", [""])[0] else "")
            if cfg.local or want == "choose":


                mode = {"in": "in", "up": "up"}.get(want, "choose")
                _log_access(self, path, None, f"signin-{mode}")
                self._send(200, _signin_page(cfg, mode=mode).encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            _log_access(self, path, None, "signin-start")
            target, binding = oidc.begin_login(cfg)


            self._redirect(target, f"{LOGIN_COOKIE}={binding}; Path=/auth/; "
                                   f"HttpOnly; Secure; SameSite=Lax; "
                                   f"Max-Age={oidc.PENDING_TTL_S}")
            return
        if path == "/auth/guest":
            if self.command != "POST":
                self._redirect(SIGN_IN_PATH)
                return
            gid = _new_guest()
            _log_access(self, path, _guest_workspace(gid), "guest-start")


            self._redirect("/", f"{GUEST_COOKIE}={gid}; Path=/; HttpOnly; "
                                "Secure; SameSite=Lax")
            return
        if path == "/auth/forgot":
            if not cfg.local:


                self._redirect(SIGN_IN_PATH)
                return
            if self.command != "POST":
                self._send(200, _forgot_page().encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            self._auth_forgot(cfg)
            return
        if path == "/auth/reset":
            if self.command != "POST" or not cfg.local:
                self._redirect(SIGN_IN_PATH)
                return
            self._auth_reset(cfg)
            return
        if path in ("/auth/verify", "/auth/resend"):
            if self.command != "POST":
                self._redirect(SIGN_IN_PATH)
                return
            self._auth_verify(cfg, path)
            return
        if path in ("/auth/signin", "/auth/register"):


            if self.command != "POST":
                self._redirect("/auth/login")
                return
            self._auth_local(cfg, path)
            return
        self._auth_callback(cfg, query)

    def _read_form(self, limit: int = 4096) -> dict:


        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _read_json(self, limit: int = 16384) -> dict:


        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        try:
            obj = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _auth_passkey_login(self, cfg: oidc.AuthConfig, path: str) -> None:


        if not passkeys.available():
            self._json(501, {"ok": False, "error": "Passkeys are not enabled on this server."})
            return
        if self.command != "POST":
            self._redirect(SIGN_IN_PATH)
            return
        client, _via = _client(self)
        if path.endswith("/options"):
            handle, options = passkeys.authentication_options(cfg.public_url)
            _log_access(self, path, None, "passkey-challenge")
            self._json(200, {"ok": True, "handle": handle, "options": json.loads(options)})
            return

        body = self._read_json()
        credential = json.dumps(body.get("credential"))
        try:
            cid = passkeys.credential_id_from(credential)
        except passkeys.WebAuthnError:
            self._json(400, {"ok": False, "error": "Malformed passkey response."})
            return
        found = WEBAUTHN.find(cid)
        if found is None:
            _log_access(self, path, None, "REFUSED-passkey", None, False)
            self._json(400, {"ok": False, "error": "That passkey is not recognized. Sign in with your password."})
            return
        email, cred = found
        locked = accounts.locked_out(email, client)
        if locked:
            _log_access(self, path, None, "REFUSED-ratelimit", email, False)
            self._json(429, {"ok": False, "error": locked})
            return
        try:


            new_count = passkeys.verify_authentication(
                cfg.public_url, body.get("handle", ""), credential, cred,
                require_uv=(email or "").strip().lower() in cfg.admins)
        except passkeys.WebAuthnError:
            accounts.record_failure(email, client)
            _log_access(self, path, None, "REFUSED-passkey", email, False)


            self._json(400, {"ok": False, "error":
                             "Passkey sign-in failed. If your passkey asks for a PIN, "
                             "fingerprint or face, complete it and try again -- or use "
                             "your password."})
            return
        try:
            WEBAUTHN.record_use(email, cid, new_count)
        except passkeys.WebAuthnError:
            pass
        acct = ACCOUNTS.get(email)
        verified = acct.verified if acct else False
        workspace, known, why = _sso_decision(cfg, email, verified)
        if why == "pending":
            self._handle_pending(cfg, email, path)
            return
        if workspace is None:
            _log_access(self, path, None, f"REFUSED-{why}", email, False)
            self._json(403, {"ok": False, "error": "This account no longer has access here."})
            return
        accounts.clear_failures(email, client)
        sid = _new_signin_session(cfg, email, verified, acct)
        _log_access(self, path, workspace, "passkey-signin-ok", email, known)
        self._json(200, {"ok": True, "redirect": "/"},
                   cookie=f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; Secure; "
                          f"SameSite=Lax; Max-Age={oidc.SESSION_TTL_S}")

    def _api_passkey(self, path: str, workspace: str) -> None:


        cfg = _refresh_auth()
        if cfg is None or not passkeys.available():
            self._json(501, {"ok": False, "error": "Passkeys are not enabled on this server."})
            return
        who = getattr(self, "waam_who", None) or {}
        email = who.get("email")
        if not email or who.get("outcome") != "ok-sso":
            self._json(403, {"ok": False, "error": "Sign in with an account to manage passkeys."})
            return
        if path == "/api/passkey/list":
            self._json(200, {"ok": True, "passkeys": [
                {"id": c.id, "label": c.label, "created": c.created, "last_used": c.last_used}
                for c in WEBAUTHN.list_for(email)]})
            return
        require_uv = (email or "").strip().lower() in cfg.admins
        if path == "/api/passkey/register/options":
            handle, options = passkeys.registration_options(
                cfg.public_url, email, WEBAUTHN.list_for(email), require_uv=require_uv)
            self._json(200, {"ok": True, "handle": handle, "options": json.loads(options)})
            return
        if path == "/api/passkey/register/verify":
            body = self._read_json()
            try:
                handle_email, cred = passkeys.verify_registration(
                    cfg.public_url, body.get("handle", ""),
                    json.dumps(body.get("credential")), str(body.get("label", "passkey")),
                    require_uv=require_uv)
                if handle_email != email:
                    raise passkeys.WebAuthnError("This passkey request was not issued for your account.")
                WEBAUTHN.add(email, cred)
            except passkeys.WebAuthnError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            _log_access(self, path, workspace, "passkey-registered", email, True)
            self._json(200, {"ok": True, "label": cred.label, "count": len(WEBAUTHN.list_for(email))})
            return
        if path == "/api/passkey/remove":
            body = self._read_json()
            removed = WEBAUTHN.remove(email, str(body.get("id", "")))
            _log_access(self, path, workspace, "passkey-removed" if removed else "passkey-remove-none", email, True)
            self._json(200, {"ok": True, "removed": removed})
            return
        self._json(404, {"ok": False, "error": "unknown passkey route"})

    def _auth_local(self, cfg: oidc.AuthConfig, path: str) -> None:


        form = self._read_form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        making = path == "/auth/register"
        mode = "up" if making else "in"
        client, _via = _client(self)

        def refuse(message: str, code: int = 400, outcome: str = "REFUSED") -> None:
            _log_access(self, path, None, outcome, email or None, False)
            self._send(code, _signin_page(cfg, error=message,
                                          mode=mode, email=email).encode("utf-8"),
                       "text/html; charset=utf-8")


        locked = accounts.locked_out(email, client)
        if locked:
            refuse(locked, 429, "REFUSED-ratelimit")
            return


        if making:
            budget = accounts.registration_allowed(client)
            if budget:
                refuse(budget, 429, "REFUSED-ratelimit")
                return

        try:
            if making:
                if not cfg.open_signin and not _may_register(cfg, email):


                    where = (" or an address at "
                             + ", ".join(cfg.allow_domains)
                             if cfg.verify_email and cfg.allow_domains else "")
                    raise accounts.AccountError(
                        f"{email}: registration is limited to approved users{where}. "
                        "Contact the administrator to request access.")


                acct = ACCOUNTS.register(email, password, limit=cfg.max_accounts)
                accounts.record_registration(client)
                if cfg.verify_email:


                    self._send_code(cfg, acct.email, path, first=True)
                    return
            else:
                acct = ACCOUNTS.authenticate(email, password)
                if cfg.verify_email and not acct.verified:


                    self._send_code(cfg, acct.email, path, first=False)
                    return
        except accounts.AccountError as exc:


            accounts.record_failure(email, client)
            refuse(str(exc))
            return


        workspace, known, why = _sso_decision(cfg, acct.email, acct.verified)


        if why == "pending":
            self._handle_pending(cfg, acct.email, path)
            return
        if workspace is None:
            _log_access(self, path, None, f"REFUSED-{why}", acct.email, False)
            self._send(403, _denied_page(cfg, acct.email, why).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        accounts.clear_failures(email, client)
        sid = _new_signin_session(cfg, acct.email, acct.verified, acct)
        _log_access(self, path, workspace,
                    "register-ok" if making else "signin-ok", acct.email, known)
        self._redirect("/", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; Secure; "
                            f"SameSite=Lax; Max-Age={oidc.SESSION_TTL_S}")

    def _send_code(self, cfg: oidc.AuthConfig, email: str, path: str,
                   *, first: bool) -> None:


        wait = accounts.resend_wait(email)
        if wait and not first:
            self._send(429, _verify_page(
                email, f"A code was sent recently. Wait {wait} seconds before "
                       "requesting another.", sent=False).encode("utf-8"),
                "text/html; charset=utf-8")
            return
        code = accounts.new_code(email, accounts.PURPOSE_VERIFY)
        try:
            mailer.send_code(cfg.mail, email, code,
                             url=cfg.public_url.rstrip("/") + SIGN_IN_PATH)
        except mailer.MailError as exc:
            _log_access(self, path, None, "REFUSED-mail", email, False)
            self._send(502, _verify_page(email, "Could not send the verification email. Contact the administrator, then request a new code.", sent=False)
                       .encode("utf-8"), "text/html; charset=utf-8")
            return
        _log_access(self, path, None, "code-sent", email, False)
        self._send(200, _verify_page(email).encode("utf-8"),
                   "text/html; charset=utf-8")

    def _auth_forgot(self, cfg: oidc.AuthConfig) -> None:


        form = self._read_form()
        email = (form.get("email") or "").strip().lower()
        client, _via = _client(self)

        if not accounts.EMAIL_RE.fullmatch(email):
            self._send(400, _forgot_page("Enter a valid email address.").encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if not cfg.verify_email:


            self._send(503, _forgot_page(
                "Email reset is unavailable. Contact the administrator "
                "to reset your password.").encode("utf-8"),
                "text/html; charset=utf-8")
            return

        locked = accounts.locked_out(email, client)
        if locked:
            _log_access(self, "/auth/forgot", None, "REFUSED-ratelimit",
                        email, False)
            self._send(429, _forgot_page(locked).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        try:
            acct = ACCOUNTS.get(email)
        except accounts.AccountError:
            acct = None
        if acct is not None and not accounts.resend_wait(email):
            code = accounts.new_code(email, accounts.PURPOSE_RESET)
            try:
                mailer.send_reset_code(
                    cfg.mail, email, code,
                    url=cfg.public_url.rstrip("/") + SIGN_IN_PATH)
                _log_access(self, "/auth/forgot", None, "reset-sent", email, False)
            except mailer.MailError:


                _log_access(self, "/auth/forgot", None, "REFUSED-mail",
                            email, False)
        else:
            _log_access(self, "/auth/forgot", None, "reset-noop", email, False)
        self._send(200, _reset_page(email).encode("utf-8"),
                   "text/html; charset=utf-8")

    def _auth_reset(self, cfg: oidc.AuthConfig) -> None:

        form = self._read_form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        client, _via = _client(self)

        problems = accounts.password_problems(password)
        if problems:


            self._send(400, _reset_page(email, problems[0]).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        locked = accounts.locked_out(email, client)
        if locked:
            _log_access(self, "/auth/reset", None, "REFUSED-ratelimit",
                        email, False)
            self._send(429, _reset_page(email, locked).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        try:
            def check_new_password():
                if accounts.verify_password(ACCOUNTS.get(email), password):
                    raise accounts.AccountError(
                        "Choose a password different from your current password.")

            accounts.check_code(email, form.get("code") or "",
                                accounts.PURPOSE_RESET,
                                before_consume=check_new_password)


            acct = ACCOUNTS.reset_password(email, password, verified=True)
        except accounts.AccountError as exc:
            accounts.record_failure(email, client)
            _log_access(self, "/auth/reset", None, "REFUSED-code", email, False)
            self._send(400, _reset_page(email, str(exc)).encode("utf-8"),
                       "text/html; charset=utf-8")
            return


        ended = oidc.drop_sessions_for(email)
        workspace, known, why = _sso_decision(cfg, acct.email, acct.verified)


        if why == "pending":
            self._handle_pending(cfg, acct.email, "/auth/reset")
            return
        if workspace is None:
            _log_access(self, "/auth/reset", None, f"REFUSED-{why}",
                        acct.email, False)
            self._send(403, _denied_page(cfg, acct.email, why).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        accounts.clear_failures(email, client)
        sid = _new_signin_session(cfg, acct.email, acct.verified, acct)
        _log_access(self, "/auth/reset", workspace,
                    f"reset-ok-{ended}-ended", acct.email, known)
        self._redirect("/", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; Secure; "
                            f"SameSite=Lax; Max-Age={oidc.SESSION_TTL_S}")

    def _notify_pending(self, cfg: oidc.AuthConfig, email: str,
                        path: str) -> None:


        if not (cfg.mail and cfg.approval_notify):
            return
        with _NOTIFIED_LOCK:
            if email in _NOTIFIED_PENDING:
                return
            _NOTIFIED_PENDING.add(email)
        try:
            url = (cfg.public_url.rstrip("/") + "/admin?"
                   + urlencode({"pending": email}))
            mailer.send(cfg.mail, cfg.approval_notify,
                        "WAAM workbench: access requested",
                        f"{email} is waiting for approval to use the "
                        f"workbench.\n\n"
                        f"Review and approve in WAAM Studio:\n{url}\n\n"
                        f"Or on the server: waam-approve {shlex.quote(email)}\n\n"
                        f"Until approved they cannot emit a robot job or save.")
        except mailer.MailError:


            with _NOTIFIED_LOCK:
                _NOTIFIED_PENDING.discard(email)
            _log_access(self, path, None, "REFUSED-mail", email, False)

    def _handle_pending(self, cfg: oidc.AuthConfig, email: str, path: str) -> None:


        self._notify_pending(cfg, email, path)
        _log_access(self, path, None, "pending", email, False)
        self._send(200, _denied_page(cfg, email, "pending").encode("utf-8"),
                   "text/html; charset=utf-8")

    def _auth_verify(self, cfg: oidc.AuthConfig, path: str) -> None:

        form = self._read_form()
        email = (form.get("email") or "").strip().lower()
        client, _via = _client(self)

        acct = None
        if accounts.EMAIL_RE.fullmatch(email):
            try:
                acct = ACCOUNTS.get(email)
            except accounts.AccountError:
                acct = None
        if acct is None:


            self._redirect(SIGN_IN_PATH)
            return

        if path == "/auth/resend":
            self._send_code(cfg, email, path, first=False)
            return

        locked = accounts.locked_out(email, client)
        if locked:
            _log_access(self, path, None, "REFUSED-ratelimit", email, False)
            self._send(429, _verify_page(email, locked, sent=False)
                       .encode("utf-8"), "text/html; charset=utf-8")
            return
        try:
            accounts.check_code(email, form.get("code") or "",
                                accounts.PURPOSE_VERIFY)
        except accounts.AccountError as exc:
            accounts.record_failure(email, client)
            _log_access(self, path, None, "REFUSED-code", email, False)
            self._send(400, _verify_page(email, str(exc), sent=False)
                       .encode("utf-8"), "text/html; charset=utf-8")
            return

        acct = ACCOUNTS.mark_verified(email)


        workspace, known, why = _sso_decision(cfg, acct.email, True)
        if why == "pending":


            self._handle_pending(cfg, acct.email, path)
            return
        if workspace is None:
            _log_access(self, path, None, f"REFUSED-{why}", acct.email, False)
            self._send(403, _denied_page(cfg, acct.email, why).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        accounts.clear_failures(email, client)
        sid = _new_signin_session(cfg, acct.email, True, acct)
        _log_access(self, path, workspace, "verify-ok", acct.email, known)
        self._redirect("/", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; Secure; "
                            f"SameSite=Lax; Max-Age={oidc.SESSION_TTL_S}")

    def _auth_callback(self, cfg: oidc.AuthConfig, query: dict) -> None:
        err = query.get("error", [""])[0]
        if err:


            desc = query.get("error_description", [""])[0]
            _log_access(self, "/auth/callback", None, "REFUSED-provider")
            self._send(400, _message_page(
                f"{cfg.label} did not complete the sign-in",
                _html.escape(desc or err), retry=True).encode("utf-8"),
                "text/html; charset=utf-8")
            return


        pending = oidc.take_pending(query.get("state", [""])[0])
        if pending is None or not query.get("code", [""])[0]:
            _log_access(self, "/auth/callback", None, "REFUSED-state")
            self._send(400, _message_page(
                "That sign-in link has expired",
                "Sign-ins have to be finished within 10 minutes, and each one "
                "works only once. Start again.", retry=True).encode("utf-8"),
                "text/html; charset=utf-8")
            return


        if not oidc.binding_ok(pending, _cookie_value(self, LOGIN_COOKIE)):
            _log_access(self, "/auth/callback", None, "REFUSED-binding")
            self._send(400, _message_page(
                "Sign-in session mismatch",
                "Start a new sign-in from the workbench in this browser.",
                retry=True).encode("utf-8"), "text/html; charset=utf-8")
            return

        try:
            id_token = oidc.exchange_code(cfg, query["code"][0], pending["verifier"])
            claims = oidc.id_token_claims(id_token)
            oidc.validate_claims(cfg, claims, pending["nonce"])
        except oidc.AuthError as exc:
            _log_access(self, "/auth/callback", None, "REFUSED-claims")
            self._send(400, _message_page(
                "That sign-in could not be verified",
                _html.escape(str(exc)), retry=True).encode("utf-8"),
                "text/html; charset=utf-8")
            return
        del id_token

        email, verified = oidc.email_of(cfg, claims)
        workspace, known, why = _sso_decision(cfg, email, verified)
        if why == "pending":


            self._handle_pending(cfg, email, "/auth/callback")
            return
        if workspace is None:
            _log_access(self, "/auth/callback", None, f"REFUSED-{why}",
                        email or None, False)
            self._send(403, _denied_page(cfg, email, why).encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        sid = _new_signin_session(cfg, email, verified)
        _log_access(self, "/auth/callback", workspace, "signin-ok", email, known)


        self._redirect("/", f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; Secure; "
                            f"SameSite=Lax; Max-Age={oidc.SESSION_TTL_S}")

    def _own_origins(self) -> set[str]:


        allowed = set()
        cfg = _refresh_auth()
        if cfg is not None and cfg.public_url:
            parsed = urlparse(cfg.public_url)
            scheme = (parsed.scheme or "").lower()
            if scheme and parsed.hostname:


                port = parsed.port
                suffix = "" if port in (None, {"http": 80, "https": 443}.get(scheme)) else f":{port}"
                allowed.add(f"{scheme}://{parsed.hostname.lower()}{suffix}")
        names = [self.headers.get("Host")]
        if _is_loopback(self.client_address[0] if self.client_address else "?"):
            names.append((self.headers.get("X-Forwarded-Host") or "").split(",")[-1])
        for name in names:
            host = (name or "").strip().lower()
            if host:
                allowed.update({f"https://{host}", f"http://{host}"})
        return allowed

    def _cross_site_post(self) -> bool:


        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip().rstrip("/").lower()
        refused = site == "cross-site" or (bool(origin) and origin not in self._own_origins())
        if refused:
            _log_access(self, urlparse(self.path).path, None, "REFUSED-cross-site")
            self._send(403, b"cross-site request refused", "text/plain")
        return refused

    def do_POST(self):
        url = urlparse(self.path)

        query = parse_qs(url.query, keep_blank_values=True)
        if self._cross_site_post():
            return
        if url.path.startswith("/auth/"):


            self._auth_route(url.path, query)
            return
        auth = _auth(self, query)
        if auth is None:
            _log_auth(self, url.path, None)
            self._deny(url.path)
            return
        workspace, _tok = auth
        _log_auth(self, url.path, workspace)
        if self._guest_gate(url.path):
            return
        if self._admin_gate(url.path, workspace):
            return
        if self._wrong_workspace(query.get("ws", [""])[0], workspace):
            return
        if url.path == "/api/slice":
            if not self._slice_slot():
                return
            try:
                with _ws_lock(workspace):
                    if "gen" not in query or not self._stale_gen(workspace, query):
                        self._api_slice(workspace, query)
            finally:
                _SLICE_SEM.release()
        elif url.path in ("/api/library/upload", "/api/library/units", "/api/admin/library/import"):
            self._api_library_post(workspace, url.path, query)
        elif url.path == "/api/place":
            if not self._slice_slot():
                return
            params = {k: v[0] for k, v in query.items() if k not in ("k", "ws", "gen")}
            email = (getattr(self, "waam_who", None) or {}).get("email")
            try:
                with _ws_lock(workspace):
                    if "gen" in query and self._stale_gen(workspace, query):
                        return
                    result = run_placement(params, workspace, email)
            except (ValueError, TypeError) as exc:
                result = {"ok": False, "error": str(exc)}
            finally:
                _SLICE_SEM.release()
            self._json(200, result)
        elif url.path == "/api/preferences":
            self._api_preferences(query)
        elif url.path == "/api/save":
            with _ws_lock(workspace):
                if not self._registration_pending(workspace):
                    self._api_save(workspace, query)
        elif url.path == "/api/load":


            if not self._slice_slot():
                return
            try:
                self._api_load(workspace, query)
            finally:
                _SLICE_SEM.release()
        elif url.path == "/api/admin/approve":
            self._api_admin(workspace, "approve")
        elif url.path == "/api/admin/revoke":
            self._api_admin(workspace, "revoke")
        elif url.path in ("/api/passkey/register/options",
                          "/api/passkey/register/verify", "/api/passkey/remove"):
            self._api_passkey(url.path, workspace)
        elif url.path == "/api/var-dat":
            with _ws_lock(workspace):
                self._api_var_dat(query, workspace)
        elif url.path in ("/api/cycle-import", "/api/run/cycle-applied"):
            with _ws_lock(workspace):
                if not self._registration_pending(workspace):
                    self._api_cycle_import(workspace, query, applied=url.path.endswith("cycle-applied"))
        elif url.path == "/api/run-logs/excel-access":
            self._api_excel_access(workspace, write=True)
        elif url.path in ("/api/run-logs/start", "/api/run-logs/update", "/api/run-logs/add"):
            run_log_routes.dispatch(
                self, SAVES_DIR, workspace, url.path.rsplit("/", 1)[1], query,
                admin=_is_admin(self, _refresh_auth()),
                commit=_git_commit() if url.path.endswith("/start") else None)
        elif url.path in ("/api/run/add", "/api/run/temperature", "/api/run/layer-row", "/api/run/note", "/api/run/finish"):
            with _ws_lock(workspace):
                self._api_run(workspace, url.path.rsplit("/", 1)[1], query)
        elif url.path in ("/api/reindex/profile", "/api/reindex/touch-job", "/api/reindex/emit"):
            with _ws_lock(workspace):
                self._api_reindex(workspace, url.path.rsplit("/", 1)[1], query)
        elif url.path in ("/api/reindex/setup", "/api/reindex/source",
                          "/api/reindex/touches", "/api/reindex/preview"):
            self._api_reindex(workspace, url.path.rsplit("/", 1)[1], query)
        else:
            self._send(404, b"not found", "text/plain")

    def _api_excel_access(self, workspace: str, *, write=False):
        who = getattr(self, "waam_who", None) or {}
        email = who.get("email")
        if not email or who.get("outcome") != "ok-sso" or who.get("guest") or workspace.startswith("guest-"):
            self._json(403, {"ok": False, "error": "Sign in with your account to manage Excel access."})
            return
        try:
            scope = _excel_scope(email)
            if scope is None:
                self._json(403, {"ok": False, "error": "Excel access requires an approved account and an HTTPS server address."})
                return
            if write:
                obj = json.loads(self._read_run_body(4096))
                if not isinstance(obj, dict) or set(obj) != {"action"} or obj["action"] not in ("create", "revoke"):
                    raise ValueError("Choose create or revoke for Excel access.")
                if obj["action"] == "create":
                    cfg = _refresh_auth()
                    if not cfg.local:
                        session = oidc.session_for(_cookie_value(self, SESSION_COOKIE))
                        if session is None or not session.get("verified"):
                            self._json(403, {"ok": False, "error": "Verify your sign-in email before creating Excel access."})
                            return
                    result = run_log_excel_auth.create(EXCEL_CREDENTIALS, email, scope=scope)
                else:
                    result = run_log_excel_auth.revoke(EXCEL_CREDENTIALS, email)
            else:
                result = run_log_excel_auth.status(EXCEL_CREDENTIALS, email, scope=scope)
            feeds = {name: "/api/run-logs/excel/" + name + ".csv"
                     for name in ("runs", "layers", "cooling", "temperatures")}
            self._json(200, {"ok": True, **result, "username": email, "feeds": feeds})
        except (ValueError, TypeError) as error:
            self._json(400, {"ok": False, "error": str(error)})
        except (OSError, accounts.AccountError, oidc.SessionStoreError):
            self._json(503, {"ok": False, "error": "Excel access could not be updated or read. Refresh its status before retrying."})

    def _excel_feed(self, path: str):

        datasets = {"/api/run-logs/excel/" + name + ".csv": name
                    for name in ("runs", "layers", "cooling", "temperatures")}
        if path not in datasets:
            self._send(404, b"Unknown Excel feed.\n", "text/plain; charset=utf-8")
            return
        try:


            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self._send(403, b"Use the configured HTTPS server address.\n", "text/plain; charset=utf-8")
                return
            email = run_log_excel_auth.authenticate(
                EXCEL_CREDENTIALS, self.headers.get("Authorization"), scope_for_email=_excel_scope)
            if email is None:
                self._send(401, b"An active personal Excel credential is required.\n", "text/plain; charset=utf-8",
                           extra={"WWW-Authenticate": 'Basic realm="WAAM Run Logs", charset="UTF-8"'})
                return
            body = run_log_routes.reporting_csv(SAVES_DIR, email, datasets[path])
            _log_access(self, path, None, "excel-read", email)
            self._send(200, body, "text/csv; charset=utf-8",
                       extra={"Content-Disposition": 'attachment; filename="' + datasets[path] + '.csv"'})
        except (OSError, ValueError, accounts.AccountError, oidc.SessionStoreError):
            self._send(503, b"Run-log reporting is temporarily unavailable.\n", "text/plain; charset=utf-8")

    def _slice_slot(self) -> bool:


        if _SLICE_SEM.acquire(blocking=False):
            return True
        self._send(429, json.dumps({"ok": False, "error":
                                    "The workbench is busy. Try again in a moment."}
                                   ).encode("utf-8"),
                   "application/json", extra={"Retry-After": "5"})
        return False

    def _registration_pending(self, workspace: str) -> bool:
        if not _state(workspace).get("registration_pending"):
            return False
        self._json(409, {"ok": False, "reason": "registration", "error":
                         "Substrate registration was not applied. Register the corners "
                         "successfully or clear registration and check the build before "
                         "saving or exporting. The previous preview is retained."})
        return True

    def _stale_gen(self, workspace: str, query: dict) -> bool:


        st = _state(workspace)
        with _LOCK:
            current = st.get("gen")
        claimed = query.get("gen", [""])[0].strip()

        if claimed.isascii() and claimed.isdecimal() and current and int(claimed) == current:
            return False
        self._json(409, {"ok": False, "reason": "stale", "error":
                         "This page is not showing the current build -- someone "
                         "else re-sliced this workspace. Reload and check the "
                         "part before exporting."})
        return True

    def _library_identity(self, query: dict, *, admin: bool = False) -> str | None:

        who = getattr(self, "waam_who", None) or {}
        if who.get("outcome") != "ok-sso" or not who.get("email") or who.get("guest"):
            self._json(403, {"ok": False, "error": "Sign in to use your STL library."})
            return None
        if admin and not _is_admin(self, _refresh_auth()):
            self._deny_admin(urlparse(self.path).path, None)
            return None
        expected = query.get("owner", [""])[0]
        actual = stl_library.owner_id(who["email"])
        if not _same_token(expected, actual):
            self._json(409, {"ok": False, "error":
                             "The signed-in account changed. Refresh this page before using the STL library."})
            return None
        return who["email"]

    def _read_stl_body(self) -> bytes | None:
        guest = bool((getattr(self, "waam_who", None) or {}).get("guest"))
        limit = MAX_UPLOAD_BYTES_GUEST if guest else MAX_UPLOAD_BYTES
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(400, {"ok": False, "error": "bad Content-Length"})
            return None
        if length > limit:
            self._json(413, {"ok": False, "error":
                             f"STL too large; limit is {limit // (1024 * 1024)} MB"
                             + (". Sign in to slice a bigger part." if guest else "")})
            return None
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._json(400, {"ok": False, "error": "The STL upload was incomplete. Please retry."})
            return None
        return body

    def _api_library_get(self, workspace: str, path: str, query: dict) -> None:
        email = self._library_identity(query, admin=path.startswith("/api/admin/"))
        if email is None:
            return
        try:
            if path == "/api/library/file":
                body, entry = stl_library.read(SAVES_DIR, stl_library.workspace_for(email),
                                                query.get("id", [""])[0], email=email)
                name = re.sub(r"[^A-Za-z0-9 _.-]", "_", entry["name"]).strip(" .")[:140] or "part.stl"
                self._send(200, body, "model/stl", extra={
                    "Content-Disposition": f'attachment; filename="{name}"'})
                return
            result = stl_library.listing(SAVES_DIR, email, all_users=path == "/api/admin/library")
            self._json(200, {"ok": True, **result})
        except (OSError, ValueError, TypeError):
            self._json(400, {"ok": False, "error": "The STL library could not be read or verified. Retry or re-upload your original."})

    def _api_library_post(self, workspace: str, path: str, query: dict) -> None:
        email = self._library_identity(query, admin=path.startswith("/api/admin/"))
        if email is None:
            return
        try:
            if path == "/api/library/upload":
                body = self._read_stl_body()
                if body is None:
                    return
                entry = stl_library.preserve(SAVES_DIR, email, body,
                                             name=query.get("name", [""])[0],
                                             units=query.get("units", ["auto"])[0])
            elif path == "/api/library/units":
                entry = stl_library.set_units(SAVES_DIR, email, query.get("id", [""])[0],
                                               query.get("units", ["auto"])[0])
            else:
                entry = stl_library.copy_into(SAVES_DIR, email, query.get("source_owner", [""])[0],
                                               query.get("id", [""])[0])
            self._json(200, {"ok": True, "entry": entry, "owner_id": stl_library.owner_id(email)})
        except (OSError, ValueError, TypeError) as exc:


            message = str(exc) if isinstance(exc, ValueError) else "The STL could not be saved or verified. Keep your original and retry."
            self._json(400, {"ok": False, "error": message})

    def _api_var_dat(self, query: dict, workspace: str = "local") -> None:


        purpose = query.get("purpose", ["none"])[0]
        kinds = {"plate-corners": "PLATE-CORNERS-VAR", "plate-sense": "PLATE-SENSE-VAR",
                 "layer-touch": "LAYER-TOUCH-VAR"}
        if purpose not in (*kinds, "none"):
            self._json(400, {"ok": False, "error": "purpose must be plate-corners, plate-sense, layer-touch or none"})
            return
        if purpose != "none" and not self._valid_run_name(query):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= 262144:
            self._json(400, {"ok": False, "error":
                             "Upload a VAR.DAT export (it was empty or too large)."})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._json(400, {"ok": False, "error": "The upload was incomplete. Please retry."})
            return
        want_flags = query.get("flags", ["0"])[0] == "1"
        try:
            base = int(query.get("base", ["40"])[0])
            count = int(query.get("count", ["4"])[0])
            flag_base = int(query.get("flag_base", [""])[0]) if want_flags else None
        except (ValueError, IndexError):
            fields = "Base, flag base and count" if want_flags else "Base and count"
            self._json(400, {"ok": False, "error": f"{fields} must be whole numbers."})
            return
        flags = None
        try:
            from .var_dat import probe_hits, probe_records
            if want_flags:
                records, flags = probe_hits(body.decode("latin-1"), base, count, flag_base=flag_base)
            else:
                records = probe_records(body.decode("latin-1"), base, count)
        except (ValueError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        filed = "none"
        if purpose != "none":
            filed = self._file_run(workspace, kinds[purpose], body, name="VAR", ext=".DAT",
                                   query=query, start_if_none=True, source="robot")
        self._archive(workspace, "vardat", body, name=purpose if purpose != "none" else "import")
        reply = {"ok": True, "records": records,
                 **({} if flags is None else {"hits": flags, "flag_base": flag_base})}
        self._send(200, json.dumps(reply).encode("utf-8"),
                   "application/json", extra={"X-Waam-Run": filed})

    def _api_slice(self, workspace: str, query: dict) -> None:
        params = {k: v[0] for k, v in query.items() if k not in ("k", "ws", "owner", "gen")}
        source_id = params.get("library")
        inspect_only = params.pop("inspect", "0") == "1"
        who = getattr(self, "waam_who", None) or {}
        email = who.get("email")
        if source_id or ("owner" in query and who.get("outcome") == "ok-sso"):
            email = self._library_identity(query)
            if email is None:
                return
        raw = self._read_stl_body()
        if raw is None:
            return
        body = raw or None
        if source_id and body is not None:
            self._json(400, {"ok": False, "error": "Choose one STL source: a library part or a new upload."})
            return
        archive = "not-requested"
        entry = None
        try:
            if source_id:
                if source_id.startswith("sample:"):
                    shape = source_id.partition(":")[2]
                    if shape not in demos.DEMOS:
                        raise ValueError("Unknown library sample")
                    params.update(kind="wall", shape=shape, name=demos.DEMOS[shape].label)
                else:
                    body, entry = stl_library.read(SAVES_DIR, stl_library.workspace_for(email), source_id, email=email)
                    params.update(kind="stl", name=entry["name"])
                    params.setdefault("units", entry["units"])
                    entry = stl_library.set_units(SAVES_DIR, email, source_id, params["units"], body=body)
            reuse = params.pop("reuse", "0") == "1"
            if reuse and params.get("kind") == "stl" and body is None:
                with _LOCK:
                    previous = (_STATES.get(workspace) or {}).get("request") or {}
                    body = previous.get("stl")
                if body is None:
                    raise ValueError("The previous upload is no longer in this session. Select it from your library or upload the STL again.")
            if raw and params.get("kind") == "stl" and email and not who.get("guest"):
                try:
                    name = params.get("name", "part.stl")
                    if not name.lower().endswith(".stl"):
                        name += ".stl"
                    entry = stl_library.preserve(SAVES_DIR, email, raw, name=name, units=params.get("units", "auto"))
                    params["library"] = entry["id"]
                    archive = "saved"
                except (OSError, ValueError):
                    archive = "failed"
            if inspect_only:
                if params.get("kind") != "stl":
                    raise ValueError("Choose an STL to inspect its units")
                result = {"ok": True, "stl": stl_library.inspect(body or b"", params.get("units", "auto"))}
            else:
                result = run_slice(params, body, workspace, email)
        except FileNotFoundError:
            result = {"ok": False, "error": "That STL is no longer available. Select another library part or upload the original again."}
        except OSError:
            result = {"ok": False, "error": "The STL could not be read or processed. Retry or re-upload your original."}
        except ValueError as exc:


            result = {"ok": False, "error": str(exc)}
        except Exception:
            _log_trace("slice")
            result = {"ok": False, "error":
                      "The slice failed unexpectedly. The server logged the details; "
                      "retry, and tell the administrator if it happens again."}
        if entry is not None:
            result["library_entry"] = entry
        if archive != "not-requested":
            result["archive"] = archive
        if archive == "failed":
            result["archive_warning"] = "The VM could not preserve this upload. Keep your original file and retry later."
        self._json(200, result)

    def _archive(self, workspace: str, kind: str, body: bytes, *, name: str = "") -> str:


        who = getattr(self, "waam_who", None) or {}
        if not who.get("email") or who.get("guest") or workspace.startswith("guest-"):
            return "not-requested"
        try:
            if kind == "stl":
                stl_library.preserve(SAVES_DIR, who["email"], body,
                                     name=name if name.lower().endswith(".stl") else (name or "part") + ".stl")
            else:
                job_archive.preserve(SAVES_DIR, workspace, kind, body, name=name)
        except (OSError, ValueError):
            self.log_error("automatic file archive failed (%s)", kind)
            return "failed"
        return "saved"

    def _valid_run_name(self, query: dict) -> bool:
        name = query.get("run_name", [""])[0]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{0,40}", name):
            self._json(400, {"ok": False, "error":
                             "Run name: up to 40 letters, digits, spaces, _ and - only."})
            return False
        return True

    def _file_run(self, workspace: str, kind: str, body: bytes, *, name: str,
                  ext: str, query: dict, start_if_none: bool = False,
                  source: str = "workbench", summary: dict | None = None, platter=None,
                  detail: str | None = None, cycle: dict | None = None,
                  stations: list | None = None) -> str:
        who = getattr(self, "waam_who", None) or {}
        if not who.get("email") or who.get("guest") or workspace.startswith("guest-"):
            return "none"
        try:
            if not start_if_none and runs.active(SAVES_DIR, workspace) is None:
                return "none"
            with _LOCK:
                request = dict((_STATES.get(workspace) or {}).get("request") or {})
            commit = _git_commit()
            sidecar = None
            summary = dict(summary) if summary is not None else None
            table = summary.pop("pass_table", None) if summary is not None else None
            if kind.endswith("-JOB") and ext == ".JBI":
                fields = {key: query[key][0] for key in
                          ("arc", "arc_schedule", "asf_attest", "layers", "interpass",
                           "interpass_every", "folder", "smooth",
                           "spacing", "max_interpass", "max_unit", "ctwd", "temp_check_every",
                           "bead_dwell", "clean_first", "measure_every", "measure_times", "measure_pause",
                           "measure_clean", "measure_pre_weld", *_THERMAL_TRACE_KEYS) if key in query}
                if platter is not None:
                    fields["platter"] = asdict(platter)
                sidecar = {"kind": kind, "job": name,
                           "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                           "commit": commit, "params": request.get("params") or {},
                           "query": fields, "summary": summary, "sha256": hashlib.sha256(body).hexdigest(),
                           "measure_stops": (summary or {}).get("measure_stops", [])}
                if table is not None:
                    from .passes import digest
                    sidecar["passes"] = digest(table)
                if cycle is not None:
                    sidecar.update(cycle=cycle, stations=stations)
            stored = runs.add_file(
                SAVES_DIR, workspace, kind=kind, body=body, detail=name if detail is None else detail, ext=ext,
                source=source, sidecar=sidecar, operator=who["email"], part_hint=name,
                name=query.get("run_name", [""])[0] or None, commit=commit,
                start_if_none=start_if_none, passes=table)
            if stored is not None and source == "workbench" and request.get("stl"):
                runs.add_file(SAVES_DIR, workspace, kind="PART", body=request["stl"], detail="",
                              ext=".stl", source="workbench", operator=who["email"],
                              part_hint=name, start_if_none=False)
            return stored or "none"
        except (OSError, ValueError):
            self.log_error("run filing failed (%s)", kind)
            return "failed"

    def _read_run_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= limit:
            raise ValueError(f"Request body limit: 1..{limit} bytes")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("The upload was incomplete. Please retry.")
        return body

    def _api_run(self, workspace: str, action: str, query: dict) -> None:
        who = getattr(self, "waam_who", None) or {}
        if not who.get("email") or who.get("guest") or workspace.startswith("guest-"):
            self._json(403, {"ok": False, "error": "Sign in to keep a run log."})
            return
        try:
            if action == "summary":
                self._json(200, {"ok": True, **runs.summary(SAVES_DIR, workspace)})
                return
            if action == "cooling":
                self._json(200, {"ok": True, **_cooling_proposal(
                    runs.summary(SAVES_DIR, workspace)["active"], query)})
                return
            if action in ("decay", "layer-row") and query.get("run", [""])[0]:
                record = runs.active(SAVES_DIR, workspace)
                if (record or {}).get("run") != query["run"][0]:
                    self._json(409, {"ok": False, "error": "The open run changed. Reopen the Run data sheet."})
                    return
            if action == "decay":
                from .interpass import fit_decay
                record = runs.active(SAVES_DIR, workspace)
                self._json(200, {"ok": True, "fit": fit_decay(
                    runs.layer_log(record or {}))})
                return
            if action == "add":
                raw = self._read_run_body(25 * 1024 * 1024)
                kind = query.get("kind", [""])[0]
                name = query.get("name", [""])[0]
                if kind not in ("attachment", "arcmon"):
                    raise ValueError("kind must be attachment or arcmon")
                if not name or Path(name).name != name:
                    raise ValueError("invalid file name")
                parsed = arcmon.parse(name, raw) if kind == "arcmon" else None
                stored = runs.add_file(
                    SAVES_DIR, workspace, kind="ARCMON" if kind == "arcmon" else "ATTACH",
                    body=raw, detail=Path(name).stem, ext=Path(name).suffix,
                    source="robot" if kind == "arcmon" else "operator",
                    operator=who["email"], part_hint="PART", start_if_none=False,
                    arcmon_summary=parsed)
                if stored is None:
                    raise ValueError("No run is open. A run starts with the plate's first VAR.DAT "
                                     "or the first robot job you export.")
                self._json(200, {"ok": True, "name": stored, **runs.summary(SAVES_DIR, workspace)})
                return
            obj = json.loads(self._read_run_body(16384))
            if not isinstance(obj, dict):
                raise ValueError("Send a JSON object")
            if action == "temperature":
                runs.add_temperature(SAVES_DIR, workspace, value=obj.get("value"), unit=obj.get("unit"),
                                     stage=obj.get("stage"), note=obj.get("note", ""))
            elif action == "layer-row":
                pass_number = obj.get("pass")
                if pass_number is not None and (type(pass_number) is not int or pass_number < 1):
                    raise ValueError("Pass must be a whole number >= 1, or blank for the whole layer")
                if pass_number is None:
                    obj.pop("pass_base", None)
                else:
                    obj["pass_base"] = 1
                runs.set_layer_row(SAVES_DIR, workspace, obj)
            elif action == "note":
                runs.add_note(SAVES_DIR, workspace, text=obj.get("text"))
            elif action == "finish":
                waived = obj.get("arcmon_waiver", False)
                if not isinstance(waived, bool):
                    raise ValueError("arcmon_waiver must be true or false")
                reason = obj.get("waiver_reason", "")
                if not isinstance(reason, str) or len(reason) > 2000:
                    raise ValueError("Waiver reason limit: 2000 characters")
                runs.finish(SAVES_DIR, workspace, outcome=obj.get("outcome"),
                            data_class=obj.get("data_class", "preliminary"),
                            measurements={"height_mm": obj.get("height_mm"), "width_mm": obj.get("width_mm")},
                            notes=obj.get("notes", ""),
                            waiver={"reason": reason or "Arc monitor export not set up yet"} if waived else None,
                            by=who["email"])
            self._json(200, {"ok": True, **runs.summary(SAVES_DIR, workspace)})
        except (ValueError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except OSError:
            self.log_error("run archive operation failed (%s)", action)
            self._json(500, {"ok": False, "error": "The run log could not be saved or read. Please retry."})

    def _api_thermal_plan(self, workspace: str, query: dict) -> None:
        who = getattr(self, "waam_who", None) or {}
        if not who.get("email") or who.get("guest") or workspace.startswith("guest-"):
            self._json(403, {"ok": False, "error": "Sign in to compute a thermal plan."})
            return
        with _LOCK:
            st = _STATES.get(workspace) or {}
            has_slice = st.get("toolpath") is not None
            gen = st.get("gen")
        if not has_slice:
            self._json(409, {"ok": False, "error":
                             "No build is ready. Slice and check a part before computing dwells."})
            return
        if self._stale_gen(workspace, query):
            return
        try:
            requested = query.get("model", ["default"])[0].strip().lower()
            run = runs.active(SAVES_DIR, workspace) if requested == "run" else None
            exports, export_notes = _thermal_run_exports(workspace, run) if run else ([], [])
            self._json(200, {"ok": True, "gen": gen, **_thermal_plan(
                st, query, run, run_exports=exports, run_export_notes=export_notes
            )})
        except (TypeError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except OSError:
            self._json(500, {"ok": False, "error":
                             "The active run could not be read. Please retry."})

    def _api_save(self, workspace: str, query: dict) -> None:


        if (getattr(self, "waam_who", None) or {}).get("guest"):
            self._json(403, {"ok": False, "error":
                             "Sign in to save builds."})
            return
        job = query.get("name", [""])[0]
        d = _job_dir(workspace, job)
        if d is None:
            self._json(400, {"ok": False, "error":
                             "bad job name: start with a letter/digit; letters, "
                             "digits, space, _ and - only; max 40 chars"})
            return
        with _LOCK:
            st = _STATES.get(workspace) or {}
            req = st.get("request")
            tp = st.get("toolpath")
            slicing = (st.get("prepared") or {}).get("slicing")
        if not req or tp is None:
            self._json(400, {"ok": False, "error": "No build is ready. Slice and check a part first."})
            return
        if self._stale_gen(workspace, query):
            return
        payload = {
            "job": job,
            "saved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "commit": _git_commit(),
            "v_unit": "mm/s",
            "params": req["params"],
            "stl": "part.stl" if req.get("stl") else None,
            "slicing": slicing,
        }
        d.mkdir(parents=True, exist_ok=True)
        (d / "request.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        if req.get("stl"):
            (d / "part.stl").write_bytes(req["stl"])
        (d / "part.gcode").write_text(to_gcode(tp), encoding="utf-8")
        self._json(200, {"ok": True, "job": d.name})

    def _api_admin(self, workspace: str, action: str) -> None:


        from . import approve
        path = "/api/admin/" + action
        cfg = _refresh_auth()
        if not _is_admin(self, cfg):
            self._deny_admin(path, workspace)
            return
        email = (self._read_form().get("email") or "").strip().lower()
        if not oidc._EMAIL_RE.fullmatch(email):
            self._json(400, {"ok": False,
                             "error": "that is not an email address"})
            return
        if action == "approve":
            changed, reason = approve.approve_email(email)
        else:
            changed, reason = approve.block_email(email)
            if changed:


                oidc.drop_sessions_for(email)


        _log_access(self, path, workspace, "admin-" + action, email)
        self._json(200, {"ok": True, "changed": changed, "message": reason,
                         "email": email})

    def _read_reindex_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 600000:
            raise ValueError("Send a JSON object of at most 600000 bytes")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("The upload was incomplete. Please retry.")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("The request is not valid JSON") from None
        if not isinstance(obj, dict):
            raise ValueError("The request must be a JSON object")
        return obj

    def _api_reindex_profile(self, workspace: str, query: dict) -> None:
        path = _reindex_profile_path(workspace, query.get("name", [""])[0])
        if path is None:
            self._json(400, {"ok": False, "error": "bad profile name"})
            return
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._json(404, {"ok": False, "error": "Profile not found"})
            return
        except (OSError, ValueError):
            _log_trace("read reindex profile")
            self._json(500, {"ok": False, "error": "The profile could not be read."})
            return
        self._json(200, {"ok": True, "profile": profile})

    def _api_reindex_profiles(self, workspace: str) -> None:
        from . import reindex

        path = _reindex_profile_path(workspace, "profile")
        if path is None:
            self._json(400, {"ok": False, "error": "bad profile folder"})
            return
        profiles = []
        for file in sorted(path.parent.glob("*.json"), key=lambda p: p.stem):
            resolved = _reindex_profile_path(workspace, file.stem)
            if resolved is None:
                continue
            try:
                profile = reindex.validate_profile(json.loads(resolved.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            source = profile["source_job"]
            profiles.append({"name": file.stem, "saved_at": profile.get("saved_at"),
                             "has_reference": profile["reference"] is not None,
                             "source": source["name"] if source is not None else None})
        self._json(200, {"ok": True, "profiles": profiles})

    def _api_reindex_save(self, workspace: str, query: dict, obj: dict) -> None:
        import tempfile

        from . import reindex

        if (getattr(self, "waam_who", None) or {}).get("guest"):
            self._json(403, {"ok": False, "error": "Sign in to save profiles."})
            return
        name = query.get("name", [""])[0]
        path = _reindex_profile_path(workspace, name)
        if path is None:
            self._json(400, {"ok": False, "error": "bad profile name"})
            return
        try:
            profile = reindex.validate_profile(obj.get("profile"))
            reindex.plan_from_profile(profile)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        profile["saved_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".profile-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(profile, stream, indent=2)
            os.replace(temporary, path)
        except OSError:
            _log_trace("save reindex profile")
            self._json(500, {"ok": False, "error": "The profile could not be saved."})
            return
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._json(200, {"ok": True, "name": name, "saved_at": profile["saved_at"]})

    @staticmethod
    def _reindex_records(obj: dict) -> list:
        from . import reindex, var_dat

        text = obj.get("var_dat")
        if not isinstance(text, str):
            raise ValueError("var_dat must be text from a VAR.DAT export")
        return var_dat.probe_records(text, reindex.TOUCH_BASE, 8)

    @staticmethod
    def _reindex_default_name(profile: dict) -> str:
        from . import reindex

        if profile["reference"] is None or profile["source_job"] is None:
            raise ValueError("Re-indexing requires both reference touches and a source_job in the profile")
        source = reindex.source_summary(profile["source_job"]["text"])
        return (source["name"][:29] + "_RX").upper()

    def _api_reindex(self, workspace: str, action: str, query: dict) -> None:
        from . import reindex

        try:
            obj = self._read_reindex_json()
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        if action == "profile":
            self._api_reindex_save(workspace, query, obj)
            return
        if action == "touch-job":
            self._api_reindex_touch_job(workspace, query, obj)
            return
        if action == "emit":
            self._api_reindex_emit(workspace, query, obj)
            return
        try:
            if action == "setup":
                datum, plan = reindex.plan_from_profile(obj.get("profile"))
                result = {"summary": reindex.setup_summary(datum, plan)}
            elif action == "source":
                result = {"summary": reindex.source_summary(obj.get("text"))}
            elif action == "touches":
                _, plan = reindex.plan_from_profile(obj.get("profile"))
                records = self._reindex_records(obj)
                reindex.check_touches(plan, records)
                self._archive(workspace, "vardat", obj["var_dat"].encode("latin-1", "replace"),
                              name="reindex-touches")
                result = {"records": records}
            else:
                profile = reindex.validate_profile(obj.get("profile"))
                name = self._reindex_default_name(profile)
                _, report = reindex.reindex_from_profile(
                    profile, self._reindex_records(obj), name=name,
                    date=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"))
                result = {"report": report}
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace(f"reindex {action}")
            self._json(500, {"ok": False, "error": "The re-index check failed; the server logged the details."})
            return
        self._json(200, {"ok": True, **result})

    def _api_reindex_touch_job(self, workspace: str, query: dict, obj: dict) -> None:
        import numpy as np

        from . import cell_lc, reindex
        from .backends.jbi import emit_search_jbi
        from .clearance import ClearanceError

        name = (query.get("name", [""])[0] or "REIDX_TOUCH").strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if _controller_job_taken(self, name):
            return
        folder, ferr = _folder_from_query(query)
        if ferr:
            self._json(400, {"ok": False, "error": ferr})
            return
        try:
            _, plan = reindex.plan_from_profile(obj.get("profile"))
            origin = np.zeros(3)
            scene = cell_lc.lc_scene(origin, training_block=False)
            torch = TorchModel.yaskawa_accufast_45()
            text = emit_search_jbi(
                plan, cell_lc.pulse_solver(origin), name=name,
                date=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"), scene=scene, torch=torch,
                rin=3, pvar_base=reindex.TOUCH_BASE, overtravel_mm=reindex.OVERTRAVEL_MM,
                hop_mm=None, clean_job="CLEAN_R1", folder=folder,
                provenance=cell_lc.calibration_summary() + (
                    "manpro reindex 8 pts P044-P051", "block = measured datum faces",
                    f"workspace {workspace[:20]}"),
            )
            body = text.encode("ascii")
        except ClearanceError as exc:
            self._json(409, {"ok": False, "error": f"A touch motion fouls the cell: {exc}"})
            return
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace("reindex touch job")
            self._json(500, {"ok": False, "error":
                             "The touch job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return
        archive = self._archive(workspace, "jbi", body, name=name)
        self._send(200, body, "text/plain; charset=us-ascii", None, extra={
            "X-Waam-Archive": archive,
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Probes": "8", "X-Waam-Base": "44",
        })

    def _api_reindex_emit(self, workspace: str, query: dict, obj: dict) -> None:
        import numpy as np

        from . import reindex

        try:
            profile = reindex.validate_profile(obj.get("profile"))
            default_name = self._reindex_default_name(profile)
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        name = (query.get("name", [""])[0] or default_name).strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if _controller_job_taken(self, name):
            return
        folder, ferr = _folder_from_query(query)
        if ferr:
            self._json(400, {"ok": False, "error": ferr})
            return
        try:
            text, report = reindex.reindex_from_profile(
                profile, self._reindex_records(obj), name=name,
                date=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"), folder=folder)
            body = text.encode("ascii")
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace("reindex emit")
            self._json(500, {"ok": False, "error":
                             "The re-indexed job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return
        archive = self._archive(workspace, "jbi", body, name=name)
        self._send(200, body, "text/plain; charset=us-ascii", None, extra={
            "X-Waam-Archive": archive,
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Reindex-Shift-Mm": f'{np.linalg.norm(report["shift"]["center_shift_mm"]):.2f}',
            "X-Waam-Reindex-Rotation-Deg": f'{np.linalg.norm(report["shift"]["rotation_deg"]):.3f}',
        })

    def _api_sense_job(self, workspace: str, query: dict) -> None:
        import numpy as np

        from . import cell_lc
        from .backends.jbi import emit_search_jbi
        from .clearance import ClearanceError
        from .sensed import sense_plan, sense_plan_along
        from .substrate import from_request
        from .touch import SEARCH_OVERTRAVEL_MM

        layout = query.get("layout", ["grid"])[0]
        if layout not in ("grid", "path"):
            self._json(400, {"ok": False, "error": "Sense layout: choose grid or path"})
            return
        if layout == "path":
            if self._registration_pending(workspace):
                return
            st = _state(workspace)
            with _LOCK:
                tp = st.get("toolpath")
                em = st.get("emit")
            if tp is None or em is None:
                self._json(409, {"ok": False, "error": "No build is ready. Slice and check a part first."})
                return
            if em.get("cell_profile", "lc50s") != "lc50s":
                self._json(409, {"ok": False, "reason": "cell_profile", "error":
                                 "Along-path surface sensing requires the LC50S cell."})
                return
            if em["scene"] is None or em["origin"] is None:
                self._json(409, {"ok": False, "error":
                                 "cell layer is off - re-slice with the cell on before sensing along the part."})
                return
            if self._stale_gen(workspace, query):
                return
            if not any(move.deposit for move in tp.moves):
                self._json(409, {"ok": False, "error": "The current build has no weld moves to sense along."})
                return

        def number(key, default, low, high, message):
            try:
                value = float(query.get(key, [default])[0])
            except (ValueError, TypeError, OverflowError):
                raise ValueError(message) from None
            if not np.isfinite(value) or not low <= value <= high:
                raise ValueError(message)
            return value

        name = (query.get("name", [""])[0] or "SENSE_PLATE").strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if _controller_job_taken(self, name):
            return
        folder, ferr = _folder_from_query(query)
        if ferr:
            self._json(400, {"ok": False, "error": ferr})
            return
        try:
            _, low, high, message = NUMERIC_LIMITS["sub"]
            thickness = number("sub", 6.35, low, high, message)
            spacing = number("spacing", 20 if layout == "path" else 40, *NUMERIC_LIMITS["sense_spacing"][1:])
            inset = number("inset", 10, *NUMERIC_LIMITS["sense_inset"][1:])
            points = number("points", 21, *NUMERIC_LIMITS["sense_points"][1:])
            if not points.is_integer():
                raise ValueError(NUMERIC_LIMITS["sense_points"][3])
            fixture = query.get("fixture", [None])[0]
            if fixture not in (None, "0", "1"):
                raise ValueError("fixture must be 0 (removed) or 1 (installed)")
            raw = query.get("substrate_registration", [""])[0]
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError("Substrate registration is not valid JSON") from None
            if isinstance(record, dict) and record.get("version") == 3:
                raise ValueError("Send the four-corner (version 2) record; sensed touches are replaced by a new sense run")
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        try:
            plate = from_request(raw, thickness)
            if layout == "path":
                plan = sense_plan_along(plate, tp, em["origin"], spacing_mm=spacing,
                                        inset_mm=inset, max_points=int(points),
                                        approach_mm=25.0, overtravel_mm=SEARCH_OVERTRAVEL_MM)
            else:
                plan = sense_plan(plate, spacing_mm=spacing, inset_mm=inset, max_points=int(points),
                                  approach_mm=25.0, overtravel_mm=SEARCH_OVERTRAVEL_MM)
            origin = np.zeros(3)
            solve = cell_lc.pulse_solver(origin)
            fixture_present = cell_lc.TRAINING_BLOCK_ON_PLATTER if fixture is None else fixture == "1"
            scene = cell_lc.lc_scene(origin, training_block=fixture_present)
            scene.substrates.append(plate)
            torch = TorchModel.yaskawa_accufast_45()
            n = len(plan)
            text = emit_search_jbi(
                plan, solve, name=name, date=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"),
                scene=scene, torch=torch, rin=3, pvar_base=44, overtravel_mm=SEARCH_OVERTRAVEL_MM, hop_mm=5.0,
                folder=folder,
                clean_job="CLEAN_R1", provenance=cell_lc.calibration_summary() + (
                    f"sense {n} pts P044-P{43+n:03d}", f"workspace {workspace[:20]}"),
            )
            body = text.encode("ascii")
        except ClearanceError as exc:
            self._json(409, {"ok": False, "error": f"An approach point fouls the cell: {exc}"})
            return
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace("sense job")
            self._json(500, {"ok": False, "error":
                             "The sense job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return

        archive = self._archive(workspace, "jbi", body, name=name)
        filed = self._file_run(workspace, "PLATE-SENSE-JOB", body, name=name, ext=".JBI",
                               query=query, start_if_none=True, summary={"probes": n, "base": 44})
        self._send(200, body, "text/plain; charset=us-ascii", None, extra={
            "X-Waam-Archive": archive,
            "X-Waam-Run": filed,
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Probes": str(n),
            "X-Waam-Base": "44",
        })

    def _api_layer_sense_job(self, workspace: str, query: dict) -> None:


        import numpy as np

        from . import cell_lc
        from .backends.jbi import emit_search_jbi
        from .clearance import ClearanceError
        from .layer_sense import (FLAG_BASE, HOP_MM, SENSE_BASE, SPACING_MM,
                                  layer_sense_plan, mode_provenance, sense_motions)

        st = _state(workspace)
        with _LOCK:
            tp = st.get("toolpath")
            em = st.get("emit")
        if tp is None or em is None:
            self._json(409, {"ok": False, "error": "No build is ready. Slice and check a part first."})
            return
        if em.get("cell_profile", "lc50s") != "lc50s":
            from .cell_hc import EXPORT_REASON
            self._json(409, {"ok": False, "reason": "cell_profile", "error": EXPORT_REASON})
            return
        if em["scene"] is None or em["origin"] is None:
            self._json(409, {"ok": False, "error":
                             "cell layer is off - re-slice with the cell on before touching a layer."})
            return
        if self._stale_gen(workspace, query):
            return
        if em.get("copies", 1) > 1:
            self._json(409, {"ok": False, "error":
                             "Layer touch does not support interleaved copies yet; station planning assumes one part."})
            return
        if em.get("bead_set"):
            self._json(409, {"ok": False, "error": "A bead set's beads sit side by side, not on each other, so a layer touch "
                             "cannot re-seat the next bead. Touch the plate with Sense the surface instead"})
            return
        name = (query.get("name", [""])[0] or "LAYER_TOUCH").strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if _controller_job_taken(self, name):
            return
        folder, ferr = _folder_from_query(query)
        if ferr:
            self._json(400, {"ok": False, "error": ferr})
            return
        try:
            _, low, high, message = NUMERIC_LIMITS["sense_layer"]
            layer = float(query.get("layer", [""])[0])
            if not np.isfinite(layer) or not layer.is_integer() or not low <= layer <= high:
                raise ValueError(message)
            _, low, high, message = NUMERIC_LIMITS["layer_spacing"]
            spacing = float(query.get("spacing", [str(SPACING_MM)])[0])
            if not np.isfinite(spacing) or not low <= spacing <= high:
                raise ValueError(message)
        except (ValueError, TypeError) as exc:
            text = str(exc) if str(exc) in (NUMERIC_LIMITS["sense_layer"][3], NUMERIC_LIMITS["layer_spacing"][3]) \
                else "Layer touch: enter a whole layer number and a spacing in mm"
            self._json(400, {"ok": False, "error": text})
            return
        try:
            plan = layer_sense_plan(tp, int(layer), bead_height_mm=em["bead_height"], spacing_mm=spacing)
            if query.get("plan", [""])[0] == "1":

                self._json(200, {"ok": True, "count": len(plan), "base": SENSE_BASE,
                                 "flag_base": FLAG_BASE, "layer": int(layer)})
                return
            motions, begin = sense_motions(tp, int(layer), plan, hop_mm=HOP_MM)
            hits = [v for v in blocking(check(motions, em["torch"], em["scene"], bead_width_mm=em["bead"],
                                              bead_height_mm=em["bead_height"],
                                              margin_mm=max(em["margin"], 5.0), contact_ok=CONTACT_OK))
                    if v.move_index >= begin]
            if hits:
                worst = min(hits, key=lambda v: v.clearance_mm)
                raise ClearanceError(f"{worst.obstacle} at {tuple(round(c, 1) for c in worst.point)}: "
                                     f"{worst.clearance_mm:.1f} mm")
            n = len(plan)
            text = emit_search_jbi(
                plan, cell_lc.pulse_solver(em["origin"]), name=name,
                date=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"),
                scene=em["scene"], torch=em["torch"], rin=3, pvar_base=SENSE_BASE, flag_base=FLAG_BASE,
                overtravel_mm=float(plan[0].expected[2] - plan[0].target[2]), clean_job="CLEAN_R1",
                hop_mm=HOP_MM, folder=folder, tolerant=True,
                provenance=cell_lc.calibration_summary() + (
                    f"layer {int(layer)} top, {n} pts P{SENSE_BASE:03d}-P{SENSE_BASE + n - 1:03d}",
                    f"workspace {workspace[:20]}") + mode_provenance(),
            )
            body = text.encode("ascii")
        except ClearanceError as exc:
            self._json(409, {"ok": False, "error": f"A touch motion fouls the part or cell: {exc}"})
            return
        except ValueError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace("layer sense job")
            self._json(500, {"ok": False, "error":
                             "The layer touch job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return
        archive = self._archive(workspace, "jbi", body, name=name)
        filed = self._file_run(workspace, "LAYER-TOUCH-JOB", body, name=name, ext=".JBI",
                                query=query, start_if_none=True,
                                summary={"probes": n, "base": SENSE_BASE, "flag_base": FLAG_BASE,
                                         "flags": True, "layer": int(layer)})
        self._send(200, body, "text/plain; charset=us-ascii", None, extra={
            "X-Waam-Archive": archive,
            "X-Waam-Run": filed,
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Probes": str(n),
            "X-Waam-Base": str(SENSE_BASE),
            "X-Waam-Flag-Base": str(FLAG_BASE),
            "X-Waam-Layer": str(int(layer)),
        })

    def _cycle_build(self, workspace, query):
        who = getattr(self, "waam_who", None) or {}
        if not who.get("email") or who.get("guest") or workspace.startswith("guest-"):
            self._json(403, {"ok": False, "error": "Sign in to use Auto cycles."})
            return None
        st = _state(workspace)
        tp, em = st.get("toolpath"), st.get("emit")
        error = None
        if tp is None or em is None:
            error = "No build is ready. Slice and check a part first."
        elif em.get("cell_profile", "lc50s") != "lc50s":
            error = "Auto cycles need the LC50S cell; HC builds are planning-only."
        elif em["scene"] is None or em["origin"] is None:
            error = "cell layer is off - re-slice with the cell on before emitting."
        elif em.get("copies", 1) > 1:
            error = "Auto cycles do not support interleaved copies yet; station planning assumes one part."
        elif em.get("bead_set"):
            error = "Auto cycles cannot print a bead set; its beads sit side by side."
        if error:
            self._json(409, {"ok": False, "error": error})
            return None
        if self._stale_gen(workspace, query):
            return None
        sets = _layer_senses((st.get("request") or {}).get("params", {}).get("layer_sense", ""))
        if any(not entry[3] for entry in sets):
            self._json(409, {"ok": False, "error": "Auto cycles re-step every later layer; remove the manual layer touch sets first, or stay in Manual"})
            return None
        meta = (st.get("state") or {}).get("meta", {})
        auto = meta.get("auto")
        if auto is None:
            layers = sorted({m.layer for m in tp.moves if m.deposit})
            auto = {"cycles_done": 0, "next_first": layers[0] if layers else 0,
                    "top_layer": layers[-1] if layers else -1, "step_mm": em["bead_height"]}
        return st, tp, em, sets, auto

    def _api_cycle_job(self, workspace, query):
        from . import cell_lc
        from .emit import EmitRefused, emit_cycle_job
        from .layer_sense import FLAG_BASE, SENSE_BASE, SPACING_MM, auto_plan, mode_provenance

        build = self._cycle_build(workspace, query)
        if build is None:
            return
        st, tp, em, sets, auto = build
        c, first, top = len(sets) + 1, auto["next_first"], auto["top_layer"]
        if first > top:
            self._json(409, {"ok": False, "error": "The part is complete. Finish the run."})
            return
        arc_file, aerr = _arc_schedule_from_query(query, tp)
        raw = query.get("arc", [""])[0].strip()
        if aerr:
            self._json(400, {"ok": False, "error": aerr})
            return
        if arc_file is None and not raw:
            self._json(400, {"ok": False, "error": "ASF#: enter the number shown on the controller pendant."})
            return
        try:
            if arc_file is None:
                arc_file = int(raw)
                if not NUMERIC_LIMITS["arc"][1] <= arc_file <= NUMERIC_LIMITS["arc"][2]:
                    raise ValueError
        except ValueError:
            self._json(400, {"ok": False, "error": NUMERIC_LIMITS["arc"][3]})
            return
        base = query.get("name", ["WAAMJOB"])[0].strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,27}", base):
            self._json(400, {"ok": False, "error": "Auto job base: 1..28 letters, digits, - or _, starting with a letter or digit"})
            return
        name = f"{base}_C{c:02d}"
        if _controller_job_taken(self, name):
            return
        folder, error = _folder_from_query(query)
        interpass, ierr = _interpass_from_query(query)
        extras, xerr = _dwell_extras_from_query(query)
        measure, merr = _measure_from_query(query)
        clean_first, clerr = _clean_first_from_query(query)
        platter, perr = _platter_from_query(query, em, workspace,
                                            (getattr(self, "waam_who", None) or {}).get("email"))
        weld_ctwd_mm, cerr = _ctwd_from_query(query, em)
        if error or ierr or xerr or merr or clerr or perr or cerr:
            self._json(400, {"ok": False,
                             "error": error or ierr or xerr or merr or clerr or perr or cerr,
                             **({"reason": "ctwd"}
                                if cerr and not (error or ierr or xerr or merr or clerr or perr) else {})})
            return
        try:
            spacing = float(query.get("spacing", [str(SPACING_MM)])[0])
            _, low, high, message = NUMERIC_LIMITS["layer_spacing"]
            if not math.isfinite(spacing) or not low <= spacing <= high:
                raise ValueError(message)
            maximum = query.get("max_interpass", [""])[0].strip()
            max_unit = query.get("max_unit", ["F"])[0]
            max_value, max_c = _cycle_temperature(maximum, max_unit) if maximum else (None, None)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        try:
            run = runs.active(SAVES_DIR, workspace)
            cycles = (run or {}).get("cycles", [])
            for previous in cycles:
                number = previous["cycle"]
                if number < c:
                    if (number > len(sets) or previous.get("records") != sets[number - 1].points
                            or previous.get("hits") != sets[number - 1].hits
                            or previous.get("flag_base") != sets[number - 1].flag_base
                            or previous["layers"][1] != sets[number - 1].layer):
                        self._json(409, {"ok": False, "error": "Apply the previous exported cycle's touches to this build before exporting the next cycle."})
                        return
                elif number > c or previous.get("status") in ("imported", "applied"):
                    self._json(409, {"ok": False, "error": "Apply the previous exported cycle's touches to this build before exporting the next cycle."})
                    return
            if c > 1 and not any(item["cycle"] == c - 1 for item in cycles):
                self._json(409, {"ok": False, "error": "The previous Auto cycle is not in the open run. Restore its run and build before exporting."})
                return
            if maximum and c > 1:
                temperatures = (run or {}).get("temperatures", [])
                reading = temperatures[-1] if temperatures else None
                if reading is None or reading["value_c"] > max_c:
                    when = ""
                    if reading is not None:
                        when = _dt.datetime.fromisoformat(reading["at"]).strftime("%H:%M")
                    last_reading = (f"the last reading ({reading['value']:g} {reading['unit']} at {when}) is above the "
                                    if reading else "a temperature reading is required below the ")
                    self._json(409, {"ok": False, "reason": "temperature", "hold": True, "error":
                                    f"Hold: {last_reading}{max_value:g} {max_unit} maximum interpass. Wait, record a new reading, then export again."})
                    return
            seconds, n = interpass
            last = min(first + n - 1, top)
            step = auto["step_mm"]
            plan, hop = ([], 0.0) if last == top else auto_plan(tp, first, last, step_mm=step, spacing_mm=spacing)
            text, summary = emit_cycle_job(
                tp, cell_lc.pulse_solver(em["origin"]), name=name, arc_file=arc_file,
                cycle=c, layers=(first, last), step_mm=step,
                tail={"plan": plan, "hop_mm": hop, "pvar_base": SENSE_BASE,
                      "flag_base": FLAG_BASE} if plan else None,
                platter=platter, folder=folder, scene=em["scene"], torch=em["torch"],
                bead_width_mm=em["bead"], bead_height_mm=em["bead_height"],
                margin_mm=max(em["margin"], 5.0), interpass_timer_s=seconds,
                layers_per_interpass=n, smooth=query.get("smooth", ["0"])[0] == "1",
                weld_ctwd_mm=weld_ctwd_mm, measure=measure, clean_first=clean_first, **extras,
                comments=("waam-pipeline workbench", f"part {str(em['part'])[:24]}", _asf_check_comment(arc_file)),
                provenance=cell_lc.calibration_summary() + (f"workspace {workspace[:20]}",) + mode_provenance(),
                asf_attested=query.get("asf_attest", [""])[0] == "1", deposit_model=_deposit_model(st))
            body = text.encode("ascii")
            cycle_data = {"cycle": c, "first": first, "last": last, "step_mm": step, "hop": hop,
                           "spacing": spacing, "interpass": seconds, "n": n, "sense_base": SENSE_BASE,
                           "flag_base": FLAG_BASE,
                           "origin": [float(v) for v in em["origin"]]}
            filed = self._file_run(workspace, "CYCLE-JOB", body, name=name, detail=f"C{c:02d}-{name}",
                                   ext=".JBI", query=query, start_if_none=True, summary=summary,
                                   platter=platter, cycle=cycle_data, stations=_cycle_stations(plan))
            if filed in ("none", "failed"):
                raise OSError("Cycle job filing failed")
            params = (st.get("request") or {}).get("params", {})
            arc = (st.get("state") or {}).get("arc") or {}
            runs.set_cycle(SAVES_DIR, workspace, {
                "cycle": c, "layers": [first, last], "n": last - first + 1, "last_cycle": last == top,
                "step_mm": step, "job": name, "file": filed, "touches": len(plan), "sense_base": SENSE_BASE,
                "flag_base": FLAG_BASE,
                "spacing_mm": spacing, "interpass_s": seconds,
                "measure_stops": summary.get("measure_stops", []),
                "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "status": "exported",
                "build_settings": {"bead_height_mm": em["bead_height"], "bead_width_mm": em["bead"],
                                    "travel_mm_min": params.get("travel", 175), "wire_feed_m_min": arc.get("wfs_m_min"),
                                    "weld_ctwd_mm": summary["weld_ctwd_mm"],
                                    "clean_first": summary.get("clean_first", False)}})
        except (EmitRefused, ValueError) as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        except OSError:
            self._json(500, {"ok": False, "error": "The cycle could not be saved in the run. Retry before running it."})
            return
        except Exception:
            _log_trace("cycle job")
            self._json(500, {"ok": False, "error": "The cycle job could not be generated. Nothing was emitted."})
            return
        archive = self._archive(workspace, "jbi", body, name=name)
        self._send(200, body, "text/plain; charset=us-ascii", extra={
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Cycle": str(c), "X-Waam-Layers": f"{first}-{last}",
            "X-Waam-Last": str(int(last == top)), "X-Waam-Probes": str(len(plan)),
            "X-Waam-Base": str(SENSE_BASE), "X-Waam-Flag-Base": str(FLAG_BASE),
            "X-Waam-Layer": str(last),
            "X-Waam-Archive": archive, "X-Waam-Run": filed, **_pass_headers(summary),
            "X-Waam-Notes": "; ".join(summary.get("notes", []))})

    def _api_cycle_import(self, workspace, query, *, applied=False):
        from .layer_sense import FLAG_BASE, LEGACY_BASE, SENSE_BASE, apply_auto, auto_plan, fit_layer_top
        from .var_dat import probe_hits, probe_records

        build = self._cycle_build(workspace, query)
        if build is None:
            return
        st, tp, em, sets, auto = build
        try:
            c = int(query.get("cycle", [""])[0])
        except ValueError:
            self._json(400, {"ok": False, "error": "Cycle must be a whole number"})
            return
        try:
            run = runs.active(SAVES_DIR, workspace)
            cycles = (run or {}).get("cycles", [])
            record = max(cycles, key=lambda item: item["cycle"]) if cycles else None
            expected_status = "imported" if applied else "exported"
            if (record is None or record["cycle"] != c or record["last_cycle"]
                    or record["status"] != expected_status):
                self._json(409, {"ok": False, "error": "Only the latest exported, non-last, not-yet-imported cycle can be imported or applied."})
                return
            if applied:
                meta = (st.get("state") or {}).get("meta", {}).get("layer_sense", [])
                if (len(sets) != c or len(meta) != c or sets[-1].layer != record["layers"][1]
                        or sets[-1].spacing != record["spacing_mm"] or sets[-1].points != record.get("records")
                        or sets[-1].hits != record.get("hits")
                        or sets[-1].flag_base != record.get("flag_base")
                        or meta[-1].get("cycle") != c or meta[-1].get("layers") != record["layers"]
                        or meta[-1].get("step_mm") != record["step_mm"]):
                    self._json(409, {"ok": False, "error": "Re-slice with this cycle's imported touches before marking it applied."})
                    return
                record.update(meta[-1])
                record.update(status="applied", summary=meta[-1],
                              applied_at=_dt.datetime.now(_dt.timezone.utc).isoformat())
                runs.set_cycle(SAVES_DIR, workspace, record)
                self._json(200, {"ok": True, "summary": meta[-1]})
                return
            unit = query.get("unit", [""])[0]
            before, _ = _cycle_temperature(query.get("t_before", [None])[0], unit)
            after, _ = _cycle_temperature(query.get("t_after", [None])[0], unit)
            body = self._read_run_body(262144)
            first, last = record["layers"]
            changed = f"the build changed since cycle {c} was exported: restore its settings, or re-export the cycle"
            if len(sets) != c - 1 or auto["next_first"] != first or abs(auto["step_mm"] - record["step_mm"]) > 1e-9:
                self._json(409, {"ok": False, "error": changed})
                return
            folder = runs._folder(SAVES_DIR, workspace, run["run"])
            if not runs.valid_file_name(run["run"], record["file"]):
                raise ValueError("Invalid cycle job record")
            sidecar = runs._read((folder / record["file"]).with_suffix(".json"))

            has_base, has_flag_base = "sense_base" in record, "flag_base" in record
            if not has_base and not has_flag_base:
                base, flag_base = LEGACY_BASE, None
            elif has_base and has_flag_base:
                base, flag_base = record["sense_base"], record.get("flag_base")
                if base != SENSE_BASE or flag_base != FLAG_BASE:
                    raise ValueError("Invalid flagged cycle job record: sense_base and flag_base are required")
            else:
                raise ValueError("Invalid flagged cycle job record: sense_base and flag_base are required")
            try:
                plan, _hop = auto_plan(tp, first, last, step_mm=auto["step_mm"], spacing_mm=record["spacing_mm"],
                                       max_points=_sense_cap(base))
            except ValueError:
                self._json(409, {"ok": False, "error": changed})
                return
            stored, current = sidecar.get("stations", []), _cycle_stations(plan)
            different = len(stored) != len(current)
            for old, new in zip(stored, current):
                different |= (old["pass_index"] != new["pass_index"] or abs(old["arc_mm"] - new["arc_mm"]) > 0.01
                              or math.dist(old["expected"], new["expected"]) > 0.01)
            different |= math.dist(sidecar["cycle"]["origin"], em["origin"]) > 0.01
            if flag_base is not None:
                different |= sidecar["cycle"].get("flag_base") != flag_base
            if different:
                self._json(409, {"ok": False, "error": changed})
                return
            if base == LEGACY_BASE:
                records, hits = probe_records(body.decode("latin-1"), base, len(plan)), None
            else:
                records, hits = probe_hits(body.decode("latin-1"), base, len(plan), flag_base=flag_base)
            top = fit_layer_top(tp, last, plan, records, origin=em["origin"], bead_height_mm=auto["step_mm"],
                                base=base, hits=hits)
            _corrected, summary = apply_auto(tp, top, first=first, last=last,
                                             step_mm=auto["step_mm"], bead_height_mm=em["bead_height"])
            summary.update(cycle=c, spacing_mm=record["spacing_mm"], step=True)
            filed = self._file_run(workspace, "CYCLE-VAR", body, name=f"C{c:02d}", ext=".DAT",
                                   query=query, source="robot")
            if filed in ("none", "failed"):
                raise OSError("Cycle VAR filing failed")
            self._archive(workspace, "vardat", body, name=f"cycle-C{c:02d}")
            runs.add_temperature(SAVES_DIR, workspace, value=before, unit=unit, stage="before-interpass", cycle=c)
            measured = runs.add_temperature(SAVES_DIR, workspace, value=after, unit=unit, stage="after-interpass", cycle=c)
            record.update(status="imported", imported_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                          records=records, hits=hits, summary=summary, var_file=filed,
                          t_before=measured["temperatures"][-2], t_after=measured["temperatures"][-1])
            runs.set_cycle(SAVES_DIR, workspace, record)
            self._json(200, {"ok": True, "records": records, "hits": hits, "base": base,
                             "flag_base": flag_base, "summary": summary})
        except (ValueError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except OSError:
            self._json(500, {"ok": False, "error": "The cycle record could not be saved or read. Please retry."})

    def _api_jbi(self, workspace: str, query: dict) -> None:


        st = _state(workspace)
        with _LOCK:
            tp = st.get("toolpath")
            em = st.get("emit")
        if tp is None or em is None:
            self._json(409, {"ok": False, "error":
                             "No build is ready. Slice and check a part first."})
            return

        if em.get("cell_profile", "lc50s") != "lc50s":
            from .cell_hc import EXPORT_REASON
            self._json(409, {"ok": False, "reason": "cell_profile", "error": EXPORT_REASON})
            return


        arc_file, aerr = _arc_schedule_from_query(query, tp)
        if aerr:
            self._json(400, {"ok": False, "error": aerr})
            return
        raw = query.get("arc", [""])[0].strip()
        if arc_file is None and not raw:
            self._json(400, {"ok": False, "error":
                             "ASF#: enter the number shown on the controller pendant."})
            return
        if arc_file is None:
            try:
                arc_file = int(raw)
            except ValueError:
                self._json(400, {"ok": False, "error": NUMERIC_LIMITS["arc"][3]})
                return
            if not NUMERIC_LIMITS["arc"][1] <= arc_file <= NUMERIC_LIMITS["arc"][2]:
                self._json(409, {"ok": False, "reason": "argument",
                                 "error": NUMERIC_LIMITS["arc"][3]})
                return

        name = (query.get("name", [""])[0] or "WAAMJOB").strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if _controller_job_taken(self, name):
            return
        layers, lerr = _layers_from_query(query)
        if lerr:
            self._json(400, {"ok": False, "error": lerr})
            return
        folder, ferr = _folder_from_query(query)
        if ferr:
            self._json(400, {"ok": False, "error": ferr})
            return
        interpass, ierr = _interpass_from_query(query)
        extras, xerr = _dwell_extras_from_query(query)
        measure, merr = _measure_from_query(query)
        clean_first, clerr = _clean_first_from_query(query)
        thermal, terr = _thermal_trace_from_query(query)
        if ierr or xerr or merr or clerr or terr:
            self._json(400, {"ok": False, "error": ierr or xerr or merr or clerr or terr})
            return
        weld_ctwd_mm, cerr = _ctwd_from_query(query, em)
        if cerr:
            self._json(400, {"ok": False, "reason": "ctwd", "error": cerr})
            return


        if em["scene"] is None or em["origin"] is None:
            self._json(409, {"ok": False, "error":
                             "cell layer is off - the clearance check ran against "
                             "an empty scene. Re-slice with the cell on before emitting."})
            return
        if self._stale_gen(workspace, query):
            return

        platter, perr = _platter_from_query(query, em, workspace,
                                            (getattr(self, "waam_who", None) or {}).get("email"))
        if perr:
            self._json(400, {"ok": False, "reason": "attestation", "error": perr})
            return

        from . import cell_lc
        from .emit import EmitRefused, emit_job

        try:
            text, summary = emit_job(
                tp,
                cell_lc.pulse_solver(em["origin"]),
                name=name,
                platter=platter,
                arc_file=arc_file,
                layers=layers,
                folder=folder,
                scene=em["scene"],
                torch=em["torch"],
                bead_width_mm=em["bead"],
                bead_height_mm=em["bead_height"],
                margin_mm=max(em["margin"], 5.0),
                interpass_timer_s=interpass[0],
                layers_per_interpass=interpass[1],
                weld_ctwd_mm=weld_ctwd_mm,
                measure=measure,
                clean_first=clean_first,
                smooth=query.get("smooth", ["0"])[0] == "1",
                **extras,
                comments=(
                    "waam-pipeline workbench",
                    f"part {str(em['part'])[:24]}",
                    _asf_check_comment(arc_file),
                ) + ((thermal["comment"],) if thermal else ()),
                provenance=cell_lc.calibration_summary() + (
                    f"origin {em['origin'][0]:.0f},{em['origin'][1]:.0f}",
                    f"workspace {workspace[:20]}",
                ),
                asf_attested=query.get("asf_attest", [""])[0] == "1",
                deposit_model=_deposit_model(st),
            )
        except EmitRefused as exc:

            self._json(409, {"ok": False, "reason": exc.reason,
                             "error": f"refused to emit - {exc}"})
            return
        except Exception:
            _log_trace("emit")
            self._json(500, {"ok": False, "error":
                             "The robot job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return

        body = text.encode("ascii")
        circular = [line for line in text.splitlines() if line.startswith("MOVC ")]
        arcs = (len(circular) + sum(line.endswith(" FPT") for line in circular)) // 3
        pl = sum(line.startswith("MOVL ") and line.endswith(" PL=1") for line in text.splitlines())
        archive = self._archive(workspace, "jbi", body, name=name)
        filed = self._file_run(workspace, "WELD-JOB", body, name=name, ext=".JBI",
                               query=query, start_if_none=True, summary=summary, platter=platter)
        self._send(200, body, "text/plain; charset=us-ascii", None, extra={
            "X-Waam-Archive": archive,
            "X-Waam-Run": filed,
            "Content-Disposition": f'attachment; filename="{name}.JBI"',
            "X-Waam-Positions": str(summary["positions"]),
            "X-Waam-Passes": str(summary["passes"]),
            "X-Waam-Smooth": f"{arcs},{pl}",
            **_pass_headers(summary),
            "X-Waam-Notes": "; ".join(summary.get("notes", [])),
        })

    def _api_ros2(self, workspace: str, query: dict) -> None:


        st = _state(workspace)
        with _LOCK:
            tp = st.get("toolpath")
            em = st.get("emit")
        if tp is None or em is None:
            self._json(409, {"ok": False, "error":
                             "No build is ready. Slice and check a part first."})
            return

        if em.get("cell_profile", "lc50s") != "lc50s":
            from .cell_hc import EXPORT_REASON
            self._json(409, {"ok": False, "reason": "cell_profile", "error": EXPORT_REASON})
            return

        name = (query.get("name", [""])[0] or "WAAMJOB").strip().upper()
        if not _JBI_NAME_RE.fullmatch(name):
            self._json(400, {"ok": False, "error":
                             "bad job name: A-Z, 0-9, - and _ only, starting "
                             "alphanumeric, max 32 chars"})
            return
        if em["scene"] is None or em["origin"] is None:
            self._json(409, {"ok": False, "error":
                             "cell layer is off - the clearance check ran against "
                             "an empty scene. Re-slice with the cell on before emitting."})
            return
        if self._stale_gen(workspace, query):
            return

        platter, perr = _platter_from_query(query, em, workspace,
                                            (getattr(self, "waam_who", None) or {}).get("email"))
        if perr:
            self._json(400, {"ok": False, "reason": "attestation", "error": perr})
            return

        layers, lerr = _layers_from_query(query)
        if lerr:
            self._json(400, {"ok": False, "error": lerr})
            return
        interpass, ierr = _interpass_from_query(query)
        extras, xerr = _dwell_extras_from_query(query)
        measure, merr = _measure_from_query(query)
        clean_first, clerr = _clean_first_from_query(query)
        if ierr or xerr or merr or clerr:
            self._json(400, {"ok": False, "error": ierr or xerr or merr or clerr})
            return
        weld_ctwd_mm, cerr = _ctwd_from_query(query, em)
        if cerr:
            self._json(400, {"ok": False, "reason": "ctwd", "error": cerr})
            return

        from . import cell_lc
        from .emit import EmitRefused, emit_ros2

        try:
            job, summary = emit_ros2(
                tp,
                cell_lc.pulse_solver(em["origin"]),
                name=name,
                platter=platter,
                layers=layers,
                scene=em["scene"],
                torch=em["torch"],
                bead_width_mm=em["bead"],
                bead_height_mm=em["bead_height"],
                margin_mm=max(em["margin"], 5.0),
                interpass_timer_s=interpass[0],
                layers_per_interpass=interpass[1],
                weld_ctwd_mm=weld_ctwd_mm,
                measure=measure,
                clean_first=clean_first,
                **extras,
            )
        except EmitRefused as exc:
            self._json(409, {"ok": False, "reason": exc.reason,
                             "error": f"refused to emit - {exc}"})
            return
        except Exception:
            _log_trace("emit")
            self._json(500, {"ok": False, "error":
                             "The robot job could not be generated. Nothing was "
                             "emitted; the server logged the details."})
            return

        body = job.to_json().encode("utf-8")
        archive = self._archive(workspace, "ros2", body, name=name)
        filed = self._file_run(workspace, "ROS2-JOB", body, name=name, ext=".json", query=query,
                               summary=summary)
        self._send(200, body, "application/json; charset=utf-8", None, extra={
            "X-Waam-Archive": archive,
            "X-Waam-Run": filed,
            "Content-Disposition": f'attachment; filename="{name}.ros2.json"',
            "X-Waam-Segments": str(summary["segments"]),
            "X-Waam-Points": str(summary["points"]),
            **_pass_headers(summary),
        })

    def _api_jobs(self, workspace: str) -> None:
        jobs = []
        root = SAVES_DIR / workspace
        if root.is_dir():
            for child in root.iterdir():
                if not child.is_dir() or not _JOB_RE.fullmatch(child.name):
                    continue
                rq = child / "request.json"
                if not rq.exists():
                    continue
                saved_at = None
                try:
                    saved_at = json.loads(rq.read_text(encoding="utf-8")).get("saved_at")
                except (OSError, ValueError):
                    pass
                jobs.append({"name": child.name, "saved_at": saved_at})
        jobs.sort(key=lambda j: j.get("saved_at") or "", reverse=True)
        self._json(200, {"ok": True, "jobs": jobs})

    def _api_load(self, workspace: str, query: dict) -> None:
        job = query.get("name", [""])[0]
        d = _job_dir(workspace, job)
        if d is None:
            self._json(400, {"ok": False, "error": "bad job name"})
            return
        rq = d / "request.json"
        if not rq.exists():
            self._json(404, {"ok": False, "error": f"no saved job named {job!r}"})
            return


        try:
            saved = json.loads(rq.read_text(encoding="utf-8"))
            params = {k: str(v) for k, v in (saved.get("params") or {}).items()
                      if k in _REQUEST_KEYS}


            params.setdefault("cell_profile", "lc50s")
            note = _speed_fix_note(saved, params)
            stl = None
            stl_file = d / "part.stl"
            if params.get("kind") == "stl" and stl_file.exists():
                stl = stl_file.read_bytes()
            run_slice(params, stl, workspace,
                      (getattr(self, "waam_who", None) or {}).get("email"),
                      registration_action="replace")
        except ValueError as exc:
            self._json(200, {"ok": False, "error": str(exc)})
            return
        except Exception:
            _log_trace("load")
            self._json(200, {"ok": False, "error":
                             "That saved build could not be replayed. The server "
                             "logged the details."})
            return


        self._json(200, {"ok": True, "params": params, **({"note": note} if note else {})})

    def log_message(self, fmt, *args):
        pass


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def _token_file() -> Path:
    return Path(__file__).resolve().parents[2] / ".waam-token"


def _shareable_host() -> str:


    import socket

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        addrs = [i[4][0] for i in infos]
    except OSError:
        addrs = []
    for a in addrs:
        if a.startswith("100.") and 64 <= int(a.split(".")[1]) <= 127:
            return a
    for a in addrs:
        if not a.startswith("127."):
            return a
    return "127.0.0.1"


def _tokens_file() -> Path:
    return Path(__file__).resolve().parents[2] / ".waam-tokens.json"


def _sanitize_workspace(name: str) -> str:

    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    return clean[:40] or "user"


def load_tokens(explicit: str | None = None) -> dict[str, str]:


    tokens: dict[str, str] = {}
    f = _tokens_file()
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            for tok, info in (raw.get("tokens") or {}).items():
                if tok and isinstance(tok, str):
                    name = info.get("name", "") if isinstance(info, dict) else ""
                    tokens[tok] = _sanitize_workspace(str(name))
        except (OSError, ValueError):
            tokens = {}
            print("  .waam-tokens.json unreadable - falling back to the single shared token")
    if tokens:
        already_issued = [explicit]
        tf = _token_file()
        if tf.exists():
            try:
                already_issued.append(tf.read_text(encoding="utf-8").strip())
            except OSError:
                pass
        for tok in already_issued:
            if tok and tok not in tokens:
                tokens[tok] = "default"
        return tokens
    return {resolve_token(explicit): "default"}


def resolve_token(explicit: str | None = None, persist: bool = True) -> str:


    if explicit:
        return explicit
    env = os.environ.get("WAAM_TOKEN")
    if env:
        return env
    f = _token_file()
    if f.exists():
        saved = f.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    fresh = secrets.token_urlsafe(12)
    if persist:
        try:
            f.write_text(fresh + "\n", encoding="utf-8")
        except OSError:
            pass
    return fresh


_LISTEN_FD_ENV = "WAAM_LISTEN_FD"


def _make_server(host: str, port: int) -> ThreadingHTTPServer:


    inherited = os.environ.pop(_LISTEN_FD_ENV, None)
    if inherited is None:
        return ThreadingHTTPServer((host, port), _Handler)
    sock = socket.socket(ThreadingHTTPServer.address_family,
                         socket.SOCK_STREAM, fileno=int(inherited))
    httpd = ThreadingHTTPServer((host, port), _Handler, bind_and_activate=False)
    httpd.socket.close()
    httpd.socket = sock
    httpd.server_address = sock.getsockname()
    return httpd


def _serve(httpd: ThreadingHTTPServer, enable_reload: bool) -> None:

    if not (enable_reload and hasattr(signal, "SIGHUP")):
        httpd.serve_forever()
        return
    reload_requested = threading.Event()
    signal.signal(signal.SIGHUP, lambda *_: reload_requested.set())
    threading.Thread(target=httpd.serve_forever, name="waam-serve",
                     daemon=True).start()
    try:
        while not reload_requested.wait(1.0):
            pass
    except KeyboardInterrupt:
        httpd.shutdown()
        return


    print("SIGHUP: graceful reload (re-exec)", flush=True)
    httpd.shutdown()
    time.sleep(float(os.environ.get("WAAM_RELOAD_GRACE", "3")))
    sock = httpd.socket
    sock.set_inheritable(True)
    os.environ[_LISTEN_FD_ENV] = str(sock.fileno())
    os.execv(sys.executable,
             [sys.executable, "-m", "waam_pipeline.server", *sys.argv[1:]])


def serve(
    port: int = 8017,
    host: str = "127.0.0.1",
    show_cell: bool = True,
    token: str | None = None,
    require_token: bool = False,
    cell_profile: str = "lc50s",
    enable_reload: bool = False,
) -> None:


    global _TOKENS, _SHOW_CELL, _TOKEN_GATE, _TOKEN_EXPLICIT, _TOKENS_STAMP, _HOSTED_ASSETS, _DEFAULT_CELL_PROFILE
    if cell_profile not in ("lc50s", "hc10"):
        raise ValueError("Cell profile: choose lc50s or hc10")
    _DEFAULT_CELL_PROFILE = cell_profile
    _SHOW_CELL = show_cell
    _HOSTED_ASSETS = True
    web_assets.assets()
    _TOKENS = {}
    _TOKEN_GATE = bool(token or require_token or not _is_loopback(host))
    _TOKEN_EXPLICIT = token
    _TOKENS_STAMP = None
    if _TOKEN_GATE:


        _TOKENS = load_tokens(token)
        _TOKEN_EXPLICIT = token or _read_shared_token()
        _TOKENS_STAMP = (_stamp(_tokens_file()), _stamp(_token_file()))
    oidc.enable_session_persistence(oidc._auth_config_file().with_name(".waam-sessions.sqlite3"))
    cfg = _refresh_auth()
    httpd = _make_server(host, port)
    port = httpd.server_address[1]


    shown = _shareable_host() if host == "0.0.0.0" else host  # nosec B104


    if not _TOKENS:
        print(f"WAAM Studio workbench: http://{shown}:{port}/", flush=True)
    else:
        print(f"WAAM Studio workbench on http://{shown}:{port} - one link per person:",
              flush=True)
        for tok, workspace in _TOKENS.items():
            print(f"  {workspace}: http://{shown}:{port}/?k={tok}", flush=True)
        print(f"  bound on {host} - each link is its own workspace; "
              "the token gates every route.", flush=True)
        print("  pipeline checks stay on this machine; browsers render the preview locally.",
              flush=True)
    if cfg is not None:
        if cfg.open_signin:
            posture = ("ANY account gets its own workspace and is flagged "
                       "unrecognised in the log")
        elif cfg.verify_email and cfg.allow_domains:
            posture = ("closed - a VERIFIED address at "
                       + ", ".join(cfg.allow_domains)
                       + " admits itself; nobody else can register")
        else:
            posture = "closed - only the listed users may register"


        print("", flush=True)
        print("  GIVE PEOPLE THIS ONE LINK:", flush=True)
        print(f"      {cfg.public_url.rstrip('/')}{SIGN_IN_PATH}", flush=True)
        if cfg.local:
            doors = "sign in / create an account / continue as a guest"
            print(f"      -> {doors}", flush=True)
            proof = ("email VERIFIED by a 6-digit code" if cfg.verify_email
                     else "NO email verification - addresses are self-asserted")
            print(f"  sign-in: accounts kept here, {proof} "
                  f"({len(cfg.users)} named, {len(cfg.blocked)} blocked)",
                  flush=True)
        else:
            print(f"      -> a page with one button: 'Sign in with {cfg.label}'",
                  flush=True)
            print(f"  sign-in: {cfg.label} ({len(cfg.users)} named, "
                  f"{len(cfg.blocked)} blocked)", flush=True)
        print(f"  posture: {posture}. Review with "
              "`python -m waam_pipeline.whos_on`.", flush=True)
    else:


        print("  sign-in: off (no .waam-auth.json). /auth/login does not "
              "exist; links are the only credential.", flush=True)
        print("  to add it: `python -m waam_pipeline.authcheck --setup` "
              "prints the registration steps", flush=True)
        print("             with this cell's redirect URI already filled in.",
              flush=True)
    _serve(httpd, enable_reload)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="waam_pipeline.server")
    ap.add_argument("--port", type=int, default=8017)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 exposes it (a token is then required)")
    ap.add_argument("--no-cell", action="store_true",
                    help="skip the cell + arm layer (faster, empty-space checks)")
    ap.add_argument("--cell-profile", choices=("lc50s", "hc10"), default="lc50s",
                    help="initial cell; hc10 is the AW-HC geometry reference")
    ap.add_argument("--token", default=None,
                    help="shared token (default: $WAAM_TOKEN, else persisted .waam-token; "
                         "a .waam-tokens.json roster adds per-person links)")
    ap.add_argument("--require-token", action="store_true",
                    help="enforce the token even on a loopback bind (use behind a tunnel)")
    a = ap.parse_args()
    serve(a.port, a.host, show_cell=not a.no_cell,
          token=a.token, require_token=a.require_token, cell_profile=a.cell_profile,
          enable_reload=True)
