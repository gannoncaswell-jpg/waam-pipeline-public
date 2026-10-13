from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from ..clearance import CONTACT_OK, point_guard
from ..frames import CellConfig
from ..plan import HC_PARK_PULSES, PARK_PULSES, PARK_Q, MeasureStop, bead_gaps, build_plan
from ..robots import hc10
from ..robots.ar1440 import AIR_MM_MIN, PULSES_PER_DEG, V_TOKEN_MM_PER_MIN
from ..schedule import Schedule
from ..toolpath import Toolpath
from ..touch import SEARCH_OVERTRAVEL_MM

__all__ = [
    "PARK_PULSES", "PARK_Q", "HC_PARK_PULSES",
    "emit_jbi_pulse", "emit_cycle_jbi", "emit_hc_jbi", "emit_search_jbi",
    "emit_hc_search_jbi", "emit_jbi", "emit_inform_preview",
    "v_token", "v_speed_mm_min", "DEFAULT_WELD_MM_MIN", "DEFAULT_AIR_MM_MIN",
]


DEFAULT_WELD_MM_MIN = 833.0
DEFAULT_AIR_MM_MIN = AIR_MM_MIN


_V_TOKEN_RANGE = (0.1, 1500.0)


def v_token(mm_per_min: float, unit_mm_per_min: float) -> str:

    v = round(float(mm_per_min) / float(unit_mm_per_min), 1)
    lo, hi = _V_TOKEN_RANGE
    if not lo <= v <= hi:
        raise ValueError(
            f"{mm_per_min:g} mm/min is V={v:g} on this controller, outside the "
            f"INFORM range {lo:g}..{hi:g} the pendant accepts"
        )


    return f"{v:.1f}"


def v_speed_mm_min(token: str, unit_mm_per_min: float) -> float:

    try:
        v = float(token)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"V= token {token!r} is not a number") from exc
    return v * float(unit_mm_per_min)


def _pulses(q, scale=PULSES_PER_DEG) -> str:
    return ",".join(str(int(round(a * r))) for a, r in zip(q, scale))


def _dis_token(mm) -> str:


    v = float(mm)
    tenths = round(v * 10) if np.isfinite(v) else 0
    if abs(v * 10 - tenths) > 1e-6 or not 1 <= tenths <= 65535:
        raise ValueError(f"the search distance DIS= must be a whole 0.1 mm from 0.1 to "
                         f"6553.5, got {mm!r} mm")
    return str(tenths // 10) if tenths % 10 == 0 else f"{tenths // 10}.{tenths % 10}"


def _git_sha() -> str:

    import subprocess

    root = Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return sha + ("+" if dirty else "")
    except Exception:
        return "unknown"


_INFORM_COMMENT_MAX = 32


def _ascii_only(text: str, *, what: str) -> str:

    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        raise ValueError(
            f"{what} must be ASCII -- the .JBI is written as ASCII and the "
            f"pendant has no other code page; {bad!r} is not, in {text!r}"
        ) from None
    return text


def _comment(text: str) -> str:


    line = f"' {text}"[: _INFORM_COMMENT_MAX + 1]
    return _ascii_only(line, what="INFORM comment")


_INFORM_DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}(:\d{2})?$")


def _inform_date(date: str) -> str:

    if not isinstance(date, str) or not _INFORM_DATE_RE.fullmatch(date):
        raise ValueError(
            "date must be 'YYYY/MM/DD HH:MM' -- the controller rejects a "
            f"date-only ///DATE record on load (alarm 3190), got {date!r}"
        )
    return date


_JOB_NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")


def _check_job_name(name, *, what: str = "job name") -> str:


    if not isinstance(name, str) or not _JOB_NAME_RE.fullmatch(name):
        raise ValueError(
            f"{what} must be 1-32 chars of [A-Za-z0-9_-] (others load as alarm "
            f"2030, undefined job name), got {name!r}"
        )
    return name


def _folder_lines(folder) -> list[str]:


    if folder is None or folder == "" or folder == "NONE":
        return []
    if not isinstance(folder, str) or not _JOB_NAME_RE.fullmatch(folder):
        raise ValueError(f"job folder must be 1-32 chars of [A-Za-z0-9_-], got {folder!r}")
    return [f"///FOLDERNAME {folder}"]


_CONTROLLER_BACKUPS = ("docs/cell-data/controller-backup/industrial-text-export",
                       "docs/cell-data/controller-backup/yaskawa1-binary-backup",
                       "docs/cell-data/jobs")

_SHARED_CELL_JOBS = frozenset(['---START---', '1-30-2026-ABYSS', '1TEST1', '888', 'ABDR-BUTTER-A52', 'ABDR-BUTTER-L-59', 'ABYSS01', 'ABYSS02', 'ABYSS03VERT', 'ABYSSTACK', 'ANDW', 'ARDB-KOMATSU-L-59', 'AUTOV2-1', 'BASIC-W-ARC-GAJ', 'CA', 'CARTERSFINAL', 'CHECKPOINT_ABORT', 'CHECKPOINT_END', 'CHECKPOINT_START', 'CIRCULAR-FILLET', 'CJP', 'CLEAN_R1', 'COLEHUNTERJOHMMY', 'DEACON', 'DEMO', 'GABEJ-FILLET', 'GET_WELD_DATA', 'HELPME', 'HOME_CHECK', 'HOME_R1', 'IRGEGN', 'IR_WALL_11INCH_15', 'JASONBOP', 'JOBJOBW', 'JUMPJOB', 'KB', 'KEY', 'KOMATSU-PROJECT', 'LAB1', 'LAB3-FLAT', "LAB3-GROUPV2'2", 'LASTLAB', 'MANPRO_REV_2', 'MASTER', 'PESSOA-STT', 'PESSOAMANPROGENPROJ', 'PESSOAMJF', 'R1-CIRCLE', 'R1-EDGE', 'R1-SUSPENDSEARCH', 'R1-TOUCH', 'ROBOT(S)_WORK_1', 'SABEJO-WPSDQC-KOMATSU', 'SET_WELD_PROFILE', 'SHIELDTEST', 'SHIP', 'SIMPSTSQA', 'TABLE_HOME', 'TADLAB2', 'TADLAB3-FLAT', 'TAD_AUTOMATION_FINAL', 'TEAM2-30', 'TEAM2V2', 'TEAM3', 'TESTCPJ', 'V2-2', 'WAAMPARAT', 'WEAVE_TEST', 'WELD-MET-2-LAB1', 'YRC1000-WELD-SIMPLE']) | frozenset({"CLEAN_R1", "R1-TOUCH", "MANPRO", "MANPRO_REV_2",
                               "TEAM2V2", "BOPTRIALV1"})


@functools.lru_cache(maxsize=1)
def controller_job_names() -> frozenset[str]:


    root = Path(__file__).resolve().parents[3]
    names = set(_SHARED_CELL_JOBS)
    for folder in _CONTROLLER_BACKUPS:
        path = root / folder
        if path.is_dir():
            names.update(p.stem.upper() for p in path.iterdir()
                         if p.suffix.upper() in (".JBI", ".JBS"))
    return frozenset(names)


def _check_arc_file(arc_file) -> None:

    numbers = arc_file.values() if isinstance(arc_file, Mapping) else (arc_file,)
    if isinstance(arc_file, Mapping) and not arc_file:
        raise ValueError("an arc schedule must name at least one layer")
    for n in numbers:
        if type(n) is not int or not 1 <= n <= 1000:
            raise ValueError(f"arc_file must be an int in 1..1000 (UWI ASF#/AEF# range), got {n!r}")


def _asf_lines(arc_file) -> list[str]:


    if not isinstance(arc_file, Mapping):
        return [f"ASF#/AEF# {arc_file}"]
    out, line = [], ""
    for n in sorted(set(arc_file.values())):
        piece = f"{line},{n}" if line else f"ASF#/AEF# {n}"
        if len(piece) > _INFORM_COMMENT_MAX - 1:
            out.append(line)
            piece = f"ASF#/AEF# {n}"
        line = piece
    return out + [line]


def _provenance_lines(arc_file, scene, torch, margin_mm, extra: tuple[str, ...]) -> list[str]:


    obstacles = 0 if scene is None else len(scene.boxes) + len(scene.capsules)
    out = [f"waam-pipeline {_git_sha()}", *_asf_lines(arc_file)]
    if torch is not None and obstacles:
        out.append(f"clearance yes: {obstacles} obstacles")
        out.append(f"check margin {margin_mm:g} mm")
    else:
        out.append("clearance NO - UNCHECKED")
    out.extend(extra)
    return out


TOLERANT_LVARS = "1,0,0,0,0,0,0,0"
TOLERANT_MAX_MISSES = 3


def _tolerant_clear_lines(flag_base: int, count: int) -> list[str]:
    return ["SET LB000 0", *(f"SET B{flag_base + i:03d} LB000" for i in range(count))]


def _render_pulse_job(positions, lines, *, name, date, folder=None, lvars=None) -> str:
    header = [
        "/JOB",
        f"//NAME {name}",
        *_folder_lines(folder),
        "//POS",
        f"///NPOS {len(positions)},0,0,0,0,0",
        "///TOOL 0",
        "///POSTYPE PULSE",
        "///PULSE",
    ]
    header += [f"C{i:05d}={rec}" for i, rec in enumerate(positions)]
    header += ["//INST", f"///DATE {_inform_date(date)}", "///ATTR SC,RW", "///GROUP1 RB1"]
    if lvars is not None:
        header.append(f"///LVARS {lvars}")
    return "\r\n".join(header + ["NOP", *lines, "END"]) + "\r\n"


TIMER_MAX_S = 655.35
MAX_DWELL_S = 3600.0


def timer_lines(seconds) -> list[str]:


    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)):
        raise ValueError(f"a {seconds!r} s dwell is not a finite TIMER time")
    if not 0.01 <= seconds <= MAX_DWELL_S:
        raise ValueError(f"a {seconds:g} s dwell is outside 0.01 to {MAX_DWELL_S:g} s "
                         f"(TIMER takes 0.01 to {TIMER_MAX_S} s; longer dwells are split)")
    total = round(seconds * 100)
    count = -(-total // round(TIMER_MAX_S * 100))
    base, extra = divmod(total, count)
    return [f"TIMER T={(base + (k < extra)) / 100:.2f}" for k in range(count)]


def _temperature_pause() -> list[str]:

    return [_message("READ TEMP THEN PRESS START"), "PAUSE", _message("")]


def _pulse_body(
    toolpath: Toolpath,
    solve,
    *,
    positions: list[str],
    arc_file: "int | Mapping[int, int]",
    park_pulses: tuple[int, ...],
    pulses_per_deg: tuple[float, ...],
    v_unit_mm_per_min: float,
    scene=None,
    torch=None,
    margin_mm: float = 5.0,
    weld_v: str | None = None,
    air_v: str | None = None,
    air_mm_min: float = DEFAULT_AIR_MM_MIN,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    comments: tuple[str, ...] = (),
    provenance: tuple[str, ...] = (),
    smooth: bool = False,
    weld_v_by_feed: bool = False,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
    planned_steps: list | None = None,
) -> list[str]:


    weld_v = weld_v if weld_v is not None else v_token(DEFAULT_WELD_MM_MIN,
                                                       v_unit_mm_per_min)
    air_v = air_v if air_v is not None else v_token(air_mm_min, v_unit_mm_per_min)


    guard = point_guard(torch, scene, margin_mm)

    park_rec = ",".join(str(p) for p in park_pulses)
    lines: list[str] = []

    def add(rec: str) -> str:


        positions.append(rec)
        return f"C{len(positions) - 1:05d}"

    def cpos(q) -> str:
        return add(_pulses(q, pulses_per_deg))


    steps = build_plan(
        toolpath, solve, hover_mm=hover_mm,
        interpass_timer_s=interpass_timer_s,
        layers_per_interpass=layers_per_interpass, guard=guard,
        park_q=tuple(p / r for p, r in zip(park_pulses, pulses_per_deg)),
        smooth=smooth, temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s,
        measure=measure, clean_first=clean_first,
    )
    if planned_steps is not None:
        planned_steps.extend(steps)

    for c in _provenance_lines(arc_file, scene, torch, margin_mm, provenance):
        lines.append(_comment(c))
    if smooth:
        arcs = sum(st.interp == "circular" for st in steps)
        arcs = (arcs + sum(st.fpt for st in steps)) // 3
        blends = sum(st.pl == 1 for st in steps)
        lines.append(_comment(f"smooth: {arcs} arcs, {blends} PL=1"))
    for c in comments:
        lines.append(_comment(c))
    if measure is not None:
        lines.append(_comment(f"MEASURE STOP EVERY {measure.every} LAYERS")[:32])
    skipped = bead_gaps(toolpath)[1] if bead_dwell_s else 0
    if skipped:
        lines.append(_comment(f"BEAD DWELL SKIP {skipped}: NO LIFT"))

    asf = arc_file
    for st in steps:
        if st.kind == "arc_on":
            if isinstance(arc_file, Mapping):
                if st.layer not in arc_file:
                    raise ValueError(f"the arc schedule has no ASF# for layer {st.layer}")
                asf = arc_file[st.layer]
            lines.append(f"ARCON ASF#({asf})")
        elif st.kind == "arc_off":
            lines.append(f"ARCOF AEF#({asf})")
        elif st.kind == "dwell":
            lines += timer_lines(st.seconds)
        elif st.kind == "pause":
            if st.role == "measure_pause":
                lines += [_message("MEASURE BEAD THEN PRESS START"), "PAUSE", _message("")]
            elif st.role == "pre_weld":
                lines += [_message("READ PRE WELD TEMP THEN START"), "PAUSE", _message("")]
            else:
                lines += _temperature_pause()
        elif st.kind == "message":
            lines.append(_message(st.label))
        elif st.kind == "call":
            lines.append(f"CALL JOB:{_check_job_name(st.label, what='call job')}")
        elif st.kind == "joint":


            if st.role == "park":
                lines.append(f"MOVJ {add(park_rec)} VJ=50.00")
            else:
                lines.append(f"MOVJ {cpos(st.q)} VJ=25.00")
        else:
            if st.role == "weld":
                if st.interp == "drop":
                    continue
                if isinstance(arc_file, Mapping) and arc_file.get(st.layer, asf) != asf:
                    raise ValueError(f"a pass runs from an ASF#{asf} layer into layer {st.layer} "
                                     f"(ASF#{arc_file[st.layer]}) without an arc-off; the schedule "
                                     "can only change at an ARCON")
                v = v_token(st.feed_mm_min, v_unit_mm_per_min) if weld_v_by_feed else weld_v
                if st.interp == "circular":
                    lines.append(f"MOVC {cpos(st.q)} V={v}" + (" FPT" if st.fpt else ""))
                else:
                    lines.append(f"MOVL {cpos(st.q)} V={v}" + (" PL=1" if st.pl == 1 else ""))
            else:
                lines.append(f"MOVL {cpos(st.q)} V={air_v}")
    return lines


def _emit_pulse_job(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    date: str,
    arc_file: "int | Mapping[int, int]",
    park_pulses: tuple[int, ...],
    pulses_per_deg: tuple[float, ...],
    v_unit_mm_per_min: float,
    scene=None,
    torch=None,
    margin_mm: float = 5.0,
    weld_v: str | None = None,
    air_v: str | None = None,
    air_mm_min: float = DEFAULT_AIR_MM_MIN,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    comments: tuple[str, ...] = (),
    provenance: tuple[str, ...] = (),
    folder: str | None = None,
    smooth: bool = False,
    weld_v_by_feed: bool = False,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
    planned_steps: list | None = None,
) -> str:

    positions: list[str] = []
    lines = _pulse_body(
        toolpath, solve, positions=positions, arc_file=arc_file,
        park_pulses=park_pulses, pulses_per_deg=pulses_per_deg,
        v_unit_mm_per_min=v_unit_mm_per_min, scene=scene, torch=torch,
        margin_mm=margin_mm, weld_v=weld_v, air_v=air_v, air_mm_min=air_mm_min, hover_mm=hover_mm,
        interpass_timer_s=interpass_timer_s, layers_per_interpass=layers_per_interpass,
        comments=comments, provenance=provenance, smooth=smooth,
        weld_v_by_feed=weld_v_by_feed,
        temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s,
        measure=measure, clean_first=clean_first, planned_steps=planned_steps,
    )
    return _render_pulse_job(positions, lines, name=name, date=date, folder=folder)


def emit_jbi_pulse(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    date: str,
    arc_file: "int | Mapping[int, int]",
    scene=None,
    torch=None,
    margin_mm: float = 5.0,
    weld_v: str | None = None,
    air_v: str | None = None,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    comments: tuple[str, ...] = (),
    provenance: tuple[str, ...] = (),
    folder: str | None = None,
    smooth: bool = False,
    weld_v_by_feed: bool = False,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
    planned_steps: list | None = None,
) -> str:


    _check_job_name(name)
    _check_arc_file(arc_file)


    return _emit_pulse_job(
        toolpath, solve, name=name, date=date, arc_file=arc_file,
        park_pulses=PARK_PULSES, pulses_per_deg=PULSES_PER_DEG,
        v_unit_mm_per_min=V_TOKEN_MM_PER_MIN,
        scene=scene, torch=torch, margin_mm=margin_mm, weld_v=weld_v,
        air_v=air_v, hover_mm=hover_mm, interpass_timer_s=interpass_timer_s,
        layers_per_interpass=layers_per_interpass,
        comments=comments, provenance=provenance, folder=folder,
        smooth=smooth, weld_v_by_feed=weld_v_by_feed,
        temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s,
        measure=measure, clean_first=clean_first, planned_steps=planned_steps,
    )


def _message(text: str) -> str:

    if not isinstance(text, str) or re.fullmatch(r"[A-Z0-9 ]{0,32}", text) is None:
        raise ValueError("MSG must contain at most 32 uppercase ASCII letters, digits or spaces")
    return f'MSG "{text}"'


def emit_cycle_jbi(toolpath: Toolpath, solve, *, name: str, date: str,
                   arc_file: "int | Mapping[int, int]", cycle: dict, tail: dict | None, scene=None,
                   torch=None, margin_mm: float = 5.0, weld_v: str | None = None,
                   air_v: str | None = None, hover_mm: float = 50.0,
                   smooth: bool = False, comments: tuple[str, ...] = (),
                   provenance: tuple[str, ...] = (), folder: str | None = None,
                   weld_v_by_feed: bool = False,
                   temp_check_every: int = 0, bead_dwell_s: float = 0.0,
                   measure: MeasureStop | None = None,
                   clean_first: bool = False,
                   planned_steps: list | None = None) -> str:

    _check_job_name(name)
    _check_arc_file(arc_file)
    c, first, last = (cycle.get(key) for key in ("cycle", "first", "last"))
    step = cycle.get("step_mm")
    if (type(c) is not int or c < 1 or type(first) is not int or type(last) is not int
            or not 0 <= first <= last):
        raise ValueError("cycle must name its positive cycle number and first <= last layers")
    if type(step) not in (int, float) or not np.isfinite(step) or step <= 0:
        raise ValueError("cycle step_mm must be finite and positive")
    notes = tuple(comments) + (f"AUTO CYCLE {c}: LAYERS {first}-{last}",)
    if tail is not None:
        plan = list(tail["plan"])
        base = tail.get("pvar_base", 120)
        flag_base = tail.get("flag_base", 80)
        if not 1 <= len(plan) <= 200 - base:
            raise ValueError(f"An Auto touch plan must contain 1 to {200 - base} points (P{base:03d}-P199)")
        hop = tail.get("hop_mm")
        if type(hop) not in (int, float) or not np.isfinite(hop) or hop <= 0:
            raise ValueError("An Auto cycle needs a finite positive hop_mm")
        wait = timer_lines(tail["interpass_s"])
        notes += (f"TOUCH LAYER {last}: {len(plan)} PTS P{base:03d}-P{base + len(plan) - 1:03d}",
                  f"STEP {step:g} MM PER LAYER")
    positions: list[str] = []
    lines = _pulse_body(
        toolpath, solve, positions=positions, arc_file=arc_file,
        park_pulses=PARK_PULSES, pulses_per_deg=PULSES_PER_DEG,
        v_unit_mm_per_min=V_TOKEN_MM_PER_MIN, scene=scene, torch=torch,
        margin_mm=margin_mm, weld_v=weld_v, air_v=air_v, hover_mm=hover_mm,
        interpass_timer_s=0.0, comments=notes, provenance=provenance, smooth=smooth,
        weld_v_by_feed=weld_v_by_feed,
        temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s,
        measure=measure, clean_first=clean_first, planned_steps=planned_steps,
    )
    if tail is not None:
        search = _search_body(
            plan, solve, name=name, positions=positions, scene=scene, torch=torch,
            margin_mm=margin_mm, search_v="10.0", rin=3, pvar_base=base,
            flag_base=flag_base,
            overtravel_mm=float(plan[0].expected[2] - plan[0].target[2]),
            clean_job="CLEAN_R1", hop_mm=hop, tolerant=True, clear_flags=False,
        )
        lines = _tolerant_clear_lines(flag_base, len(plan)) + lines
        lines += [_comment("INTERPASS: TEMP, WAIT, TEMP"),
                  _message("READ TEMP 1 THEN PRESS START"), "PAUSE", _message(""),
                  *wait,
                  _message("READ TEMP 2 THEN PRESS START"), "PAUSE", _message(""),
                  *search]
    return _render_pulse_job(positions, lines, name=name, date=date, folder=folder,
                             lvars=None if tail is None else TOLERANT_LVARS)


def emit_search_jbi(*args, **kwargs) -> str:


    return _render_search_jbi(*args, **kwargs)


def _search_body(
    plan,
    solve,
    *,
    name: str,
    positions: list[str],
    scene=None,
    torch=None,
    margin_mm: float = 5.0,


    search_v: str = "10.0",
    approach_vj: str = "25.00",
    rin: int = 3,
    pvar_base: int = 40,
    flag_base: int | None = 80,
    overtravel_mm: float = SEARCH_OVERTRAVEL_MM,
    clean_job: str | None = None,
    park_pulses: tuple = PARK_PULSES,
    pulses_per_deg: tuple = PULSES_PER_DEG,
    provenance: tuple[str, ...] = (),
    hop_mm: float | None = None,
    hop_v: str = "100.0",
    guard_v: str = "100.0",
    contact_ok: tuple[str, ...] = CONTACT_OK,
    tolerant: bool = False,
    clear_flags: bool = True,
) -> list[str]:


    _check_job_name(name)
    if clean_job and clean_job.upper() == name.upper():
        raise ValueError(f"a probe job named {name} would replace the {clean_job} it calls to "
                         "clean the wire, and then call itself; choose another name")
    plan = list(plan)
    if not plan:
        raise ValueError("no probes in the plan")
    if hop_mm is not None and any(getattr(pr, "via", None) is not None for pr in plan):
        raise ValueError("probes with a via cannot be combined with hop_mm")
    if tolerant and any(getattr(pr, "via", None) is not None for pr in plan):
        raise ValueError("a miss-tolerant search cannot use via approaches")
    if tolerant and not (type(pvar_base) is int and pvar_base == 120 and len(plan) <= 80):
        raise ValueError("a miss-tolerant search's results must use P120-P199")
    if tolerant and not (type(flag_base) is int and flag_base == 80 and len(plan) <= 80):
        raise ValueError("a miss-tolerant search's hit flags must use B080-B159")
    if rin == 4:
        touch_io, accufast_out = 1892, 4037
        already_alm, already_msg = 8813, "ACCUFAST SIGNAL ALREADY ON"
    elif rin == 3:
        touch_io, accufast_out = 1891, None
        already_alm, already_msg = 8812, "WIRE ALREADY TOUCHED"
    else:
        raise ValueError(f"rin must be 3 (wire touch) or 4 (AccuFast), got {rin!r}")

    guard = point_guard(torch, scene, margin_mm, contact_ok=contact_ok)
    dis = _dis_token(overtravel_mm)
    if hop_mm is not None and (type(hop_mm) not in (int, float) or not np.isfinite(hop_mm) or hop_mm <= 0):
        raise ValueError(f"hop_mm must be a positive distance, got {hop_mm!r}")

    park_rec = ",".join(str(p) for p in park_pulses)

    def add(rec: str) -> str:


        positions.append(rec)
        return f"C{len(positions) - 1:05d}"

    def cpos(q) -> str:
        return add(_pulses(q, pulses_per_deg))

    obstacles = 0 if scene is None else len(scene.boxes) + len(scene.capsules)
    lines: list[str] = []
    for c in (
        f"waam-pipeline {_git_sha()}",
        "TOUCH-SENSE PROBE - REVIEW REQ",
        "NOT auto-runnable",
        f"{len(plan)} probes, RIN#{rin}",
        f"result -> P{pvar_base:03d}.. (PULSE)",
        *((f"hit flag -> B{flag_base:03d}.. 1=touch", f"{TOLERANT_MAX_MISSES} misses in a row stop")
          if tolerant else ()),
        (f"clearance yes: {obstacles} obstacles" if (torch is not None and obstacles)
         else "clearance NO - UNCHECKED"),
    ):
        lines.append(_comment(c))
    if hop_mm is not None:
        lines.append(_comment(f"hop {hop_mm:g} mm between touches"))
    for c in provenance:
        lines.append(_comment(c))

    if tolerant and clear_flags:
        lines += _tolerant_clear_lines(flag_base, len(plan))


    if clean_job:
        _check_job_name(clean_job, what="clean_job")
        lines.append(_comment(f"clean+cut wire first: {clean_job}"))
        lines.append(f"CALL JOB:{clean_job}")

    lines.append(f"DOUT OT#({touch_io}) ON")
    if accufast_out is not None:
        lines.append(f"DOUT OT#({accufast_out}) ON")
    lines.append(f"MOVJ {add(park_rec)} VJ=50.00")

    for i, pr in enumerate(plan):
        via = getattr(pr, "via", None)

        visits_start = hop_mm is None or i in (0, len(plan) - 1)
        if guard is not None and via is not None:
            guard(via, f"probe {pr.name} via")
        if guard is not None and visits_start:
            guard(pr.start, f"probe {pr.name} approach")
        if guard is not None and via is not None:
            descent = np.asarray(pr.start, float) - np.asarray(via, float)
            distance = float(np.linalg.norm(descent))
            for along in np.arange(2.0, distance, 2.0):
                guard(np.asarray(via, float) + descent * (along / distance),
                      f"probe {pr.name} descent")
        start_rec = _pulses(solve(pr.start), pulses_per_deg)
        pvar = pvar_base + i
        flag = None if flag_base is None else flag_base + i
        entry = []
        if via is not None:
            via_rec = _pulses(solve(via), pulses_per_deg)
            start_tgt = start_rec.split(",")
            entry += [
                f"MOVJ {add(via_rec)} VJ={approach_vj}",
                f"WAIT IN#({touch_io})=OFF T=3.00",
                f"IFTHENEXP IN#({touch_io})=ON",
                f'\t SETUALM {already_alm} "{already_msg}" 1 SMODE=0',
                "ENDIF",
                f"GETS PX{pvar:03d} $PX000",
            ] + [f"SETE P{pvar:03d} ({e + 1}) {start_tgt[e]}" for e in range(6)] + [
                f"MOVL P{pvar:03d} V={guard_v} SRCH RIN#({rin})=ON T=0.10 DIS=0.1",
                "GETS B000 $B002",
                "IFTHENEXP B000=1",
                '\t SETUALM 8815 "UNEXPECTED TOUCH ON APPROACH" 1 SMODE=0',
                "ENDIF",
            ]
        elif hop_mm is None or i == 0:
            entry.append(f"MOVJ {add(start_rec)} VJ={approach_vj}")
        if hop_mm is not None:
            expected = np.asarray(pr.expected, float)
            if hop_mm >= float(np.linalg.norm(np.asarray(pr.start, float) - expected)):
                raise ValueError(f"probe {pr.name}: a {hop_mm:g} mm hop must sit below its approach start")
            hop = expected - hop_mm * pr.direction
            if guard is not None:
                guard(hop, f"probe {pr.name} hop")
            hop_rec = _pulses(solve(hop), pulses_per_deg)
            entry.append(f"MOVL {add(hop_rec)} V={hop_v}")


        tgt = [int(round(a * r)) for a, r in zip(solve(pr.expected), pulses_per_deg)]
        lines += [
            _comment(f"probe {pr.name}"),
            *entry,
            f"WAIT IN#({touch_io})=OFF T=3.00",
            f"IFTHENEXP IN#({touch_io})=ON",


            f'\t SETUALM {already_alm} "{already_msg}" 1 SMODE=0',
            "ENDIF",
            f"GETS PX{pvar:03d} $PX000",
        ] + [f"SETE P{pvar:03d} ({e + 1}) {tgt[e]}" for e in range(6)] + [
            f"MOVL P{pvar:03d} V={search_v} SRCH RIN#({rin})=ON T=0.10 DIS={dis}",
        ]
        if tolerant:

            lines += [
                f"GETS B{flag:03d} $B002",
                f"IFTHENEXP B{flag:03d}=0",
                "\t INC LB000",
                f"\t IFTHENEXP LB000>={TOLERANT_MAX_MISSES}",
                f'\t\t SETUALM 8814 "{TOLERANT_MAX_MISSES} MISSES IN A ROW" 1 SMODE=0',
                "\t ENDIF",
                "ELSE",
                "\t SET LB000 0",
                f"\t GETS PX{pvar:03d} $PX002",
                "ENDIF",
            ]
        else:
            lines += [
                "GETS B000 $B002",
                "IFTHENEXP B000=0",
                '\t SETUALM 8814 "MAX SEARCH DISTANCE EXCEEDED" 1 SMODE=0',
                "ENDIF",
                f"GETS PX{pvar:03d} $PX002",
            ]
        if via is not None:
            lines.append(f"MOVL {add(start_rec)} V={hop_v}")
            lines.append(f"MOVL {add(via_rec)} V={hop_v}")
        elif hop_mm is None:
            lines.append(f"MOVJ {add(start_rec)} VJ={approach_vj}")
        else:
            lines.append(f"MOVL {add(hop_rec)} V={hop_v}")
            if i == len(plan) - 1:
                lines.append(f"MOVL {add(start_rec)} V={hop_v}")

    lines.append(f"DOUT OT#({touch_io}) OFF")
    if accufast_out is not None:
        lines.append(f"DOUT OT#({accufast_out}) OFF")
    lines.append(f"MOVJ {add(park_rec)} VJ=50.00")
    return lines


def _render_search_jbi(
    plan,
    solve,
    *,
    name: str,
    date: str,
    scene=None,
    torch=None,
    margin_mm: float = 5.0,


    search_v: str = "10.0",
    approach_vj: str = "25.00",
    rin: int = 3,
    pvar_base: int = 40,
    flag_base: int | None = 80,
    overtravel_mm: float = SEARCH_OVERTRAVEL_MM,
    clean_job: str | None = None,
    park_pulses: tuple = PARK_PULSES,
    pulses_per_deg: tuple = PULSES_PER_DEG,
    provenance: tuple[str, ...] = (),
    hop_mm: float | None = None,
    hop_v: str = "100.0",
    guard_v: str = "100.0",
    folder: str | None = None,
    contact_ok: tuple[str, ...] = CONTACT_OK,
    tolerant: bool = False,
) -> str:

    positions: list[str] = []
    lines = _search_body(
        plan, solve, name=name, positions=positions, scene=scene, torch=torch,
        margin_mm=margin_mm, search_v=search_v, approach_vj=approach_vj,
        rin=rin, pvar_base=pvar_base, flag_base=flag_base, overtravel_mm=overtravel_mm,
        clean_job=clean_job, park_pulses=park_pulses, pulses_per_deg=pulses_per_deg,
        provenance=provenance, hop_mm=hop_mm, hop_v=hop_v, guard_v=guard_v,
        contact_ok=contact_ok, tolerant=tolerant,
    )
    return _render_pulse_job(positions, lines, name=name, date=date, folder=folder,
                             lvars=TOLERANT_LVARS if tolerant else None)


def emit_hc_jbi(*args, **kwargs) -> str:


    raise ValueError(
        "HC10 pulse emit is disabled: the HC cell scene is UNCALIBRATED "
        "(hc_scene is nominal, no cell survey) and HC emit needs a two-person "
        "review. The pulse scale is now confirmed (HW2480390) but that does not "
        "open emit. Use _render_hc_jbi for offline format tests only. See "
        "CLAUDE.md hc10 rule + docs/cobot-hc10-dump-analysis.md."
    )


def _render_hc_jbi(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    date: str,
    arc_file: int,
    scene=None,
    torch=None,
    margin_mm: float = 5.0,
    weld_v: str | None = None,
    air_v: str | None = None,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    comments: tuple[str, ...] = (),
    provenance: tuple[str, ...] = (),
) -> str:


    _check_job_name(name)
    if not isinstance(arc_file, int) or isinstance(arc_file, bool) or not 1 <= arc_file <= 1000:
        raise ValueError(
            f"arc_file must be an int in 1..1000 (UWI ASF#/AEF# range), got {arc_file!r}"
        )
    return _emit_pulse_job(
        toolpath, solve, name=name, date=date, arc_file=arc_file,
        park_pulses=HC_PARK_PULSES, pulses_per_deg=hc10.PULSES_PER_DEG,
        v_unit_mm_per_min=hc10.V_TOKEN_MM_PER_MIN, air_mm_min=hc10.AIR_MM_MIN,
        scene=scene, torch=torch, margin_mm=margin_mm, weld_v=weld_v,
        air_v=air_v, hover_mm=hover_mm, interpass_timer_s=interpass_timer_s,
        comments=comments, provenance=provenance,
    )


def emit_hc_search_jbi(*args, **kwargs) -> str:


    raise ValueError(
        "HC10 touch-search export is disabled: the search input (which RIN the "
        "48V-TS latches on) is UNCONFIRMED on this cobot, the contact stop is "
        "unproven, hc_scene is uncalibrated, and a two-person review is owed. "
        "Confirm on the pendant with {IN/OUT}->{RIN} first. Use "
        "_render_hc_search_jbi for offline format tests only. See "
        "docs/pendant-integration-2026-09-17.md + docs/cobot-hc10-dump-analysis.md."
    )


def _render_hc_search_jbi(
    plan,
    solve,
    *,
    name: str,
    date: str,
    scene=None,
    torch=None,
    margin_mm: float = 5.0,
    search_v: str = "50.0",
    approach_vj: str = "25.00",
    rin: int = 3,
    pvar_base: int = 40,
    overtravel_mm: float = 12.0,
    clean_job: str | None = None,
    provenance: tuple[str, ...] = (),
) -> str:


    hc_provenance = ("HC RIN UNCONFIRMED-CHK PENDANT",) + tuple(provenance)
    return _render_search_jbi(
        plan, solve, name=name, date=date, scene=scene, torch=torch,
        margin_mm=margin_mm, search_v=search_v, approach_vj=approach_vj,
        rin=rin, pvar_base=pvar_base, overtravel_mm=overtravel_mm,
        clean_job=clean_job, park_pulses=HC_PARK_PULSES,
        pulses_per_deg=hc10.PULSES_PER_DEG, provenance=hc_provenance,
    )


def emit_jbi(toolpath: Toolpath, cfg: CellConfig) -> str:
    raise NotImplementedError(
        "Cartesian .JBI records (///RECTAN + RCONF) stay blocked: no Cartesian "
        "job exists in the pendant dump to template from. Use emit_jbi_pulse() "
        "with cell_lc.pulse_solver() for loadable PULSE jobs."
    )


def emit_inform_preview(
    toolpath: Toolpath, cfg: CellConfig, schedule: Schedule | None = None
) -> str:


    lines = [
        "// PREVIEW ONLY - not a loadable .JBI (record format pending pendant dump)",
        f"// cell={cfg.name} robot={cfg.robot} TOOL#{cfg.tool_number} UF#{cfg.user_frame}",
        "NOP",
    ]
    events: dict[int, list[str]] = {}
    for kind, idx in toolpath.arc_events():
        events.setdefault(idx, []).append(kind)

    def emit_events(idx: int) -> None:
        for kind in events.get(idx, []):
            if kind == "ARCON":
                if cfg.weave_file is not None:
                    lines.append(f"WVON WEV#({cfg.weave_file})")
                lines.append(f"ARCON ASF#({cfg.arc_file})")
            else:


                lines.append("ARCOF")
                if cfg.weave_file is not None:
                    lines.append("WVOF")

    prev_layer: int | None = None
    prev_arcset: str | None = None

    def emit_layer_prelude(layer: int) -> None:
        nonlocal prev_arcset
        if schedule is None:
            return
        params = schedule.for_layer(layer)
        if params is None:
            return
        if params.dwell_s:
            lines.append(f"TIMER T={params.dwell_s:.2f}")
        arcset = params.arcset_line(cfg.proc)
        if arcset is not None and arcset != prev_arcset:
            lines.append(arcset)
            prev_arcset = arcset

    for i, move in enumerate(toolpath.moves):
        boundary = prev_layer is not None and move.layer != prev_layer
        if boundary:
            emit_events(i)
            lines.append(f"CALL JOB:{cfg.between_layer_job}")
            emit_layer_prelude(move.layer)
        elif prev_layer is None:
            emit_layer_prelude(move.layer)
        prev_layer = move.layer
        if not boundary:
            emit_events(i)
        kind = "weld SPD=ASF" if move.deposit else "air"
        lines.append(
            f"MOVL X={move.x:.3f} Y={move.y:.3f} Z={move.z:.3f}"
            f"  // {kind}, layer {move.layer}, gcode L{move.line_no}"
        )
    emit_events(len(toolpath.moves))
    lines.append("END")
    return "\n".join(lines)
