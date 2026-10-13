'If I tried to emit a real job right now, what would stop me?'


from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHEET = REPO / "docs" / "measurement-sheet.csv"
CHECKLIST = "docs/lab-visit-checklist.md"
CALIBRATION = "docs/cell-calibration.json"


EMIT, FIRST_PRINT, SIM, LOGGED = range(4)
TIERS = (
    ("BLOCKS EMIT", "the emitter refuses today"),
    ("BLOCKS FIRST PRINT", "it emits; the job is not safe to run"),
    ("DEGRADES SIM ONLY", "wrong in the picture, not in the metal"),
    ("LOGGED, NOT BLOCKING", "nothing in the pipeline consumes it"),
)
GATING = (EMIT, FIRST_PRINT)


PROBE_WALL_MM = 150.0
PROBE_LAYERS = 6
PROBE_LAYER_H_MM = 2.0
PROBE_SUBSTRATE_MM = 0.0
PROBE_MARGIN_MM = 5.0


HEADROOM_PROBE_MM = 400.0


NARROW_MARGIN_MM = 50.0


GUESSED = frozenset({"ASSUMED", "SCALED", "PHOTO", "WEB"})

SHEET_COLUMNS = ("id", "component", "parameter", "current_value_mm", "basis",
                 "priority", "how_to_measure", "MEASURED_VALUE_MM",
                 "MEASURED_BY", "DATE", "NOTES")

_PRIORITY_ORDER = {"H": 0, "M": 1, "L": 2, "": 3}


_ASCII = str.maketrans({
    "—": "--", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "±": "+-", "Ø": "dia ",
    "°": " deg", "→": "->", " ": " ", "²": "^2",
})


def ascii_text(s: str) -> str:

    return s.translate(_ASCII).encode("ascii", "replace").decode("ascii")


OEM_TOOLING_DIA_MM = 490.0
OEM_SWING_RADIUS_MM = OEM_TOOLING_DIA_MM / 2.0
OEM_ALONG_AXIS_MM = 1450.0


def swing_radius_mm(across_axis_mm: float, height_above_axis_mm: float) -> float:


    return math.hypot(across_axis_mm, height_above_axis_mm)


@dataclass(frozen=True)
class Row:


    id: str
    component: str = ""
    parameter: str = ""
    current: str = ""
    basis: str = ""
    priority: str = ""
    how: str = ""
    measured: str = ""
    measured_by: str = ""
    date: str = ""
    notes: str = ""
    order: int = 0

    @property
    def title(self) -> str:
        return f"{self.component}: {self.parameter}" if self.component else self.parameter


def load_sheet(path: Path = SHEET) -> tuple[list[Row], list[str]]:


    warn: list[str] = []
    if not path.exists():
        return [], [f"{path} does not exist, so nothing can be cross-checked"]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"{path}: unreadable ({exc})"]

    reader = csv.DictReader(text.splitlines())
    have = [(c or "").strip() for c in (reader.fieldnames or [])]
    for col in SHEET_COLUMNS:
        if col not in have:
            warn.append(f"{path.name}: no {col!r} column (has {', '.join(have)})")

    rows: list[Row] = []
    seen: set[str] = set()
    for n, raw in enumerate(reader):
        g = {(k or "").strip(): ascii_text((v or "").strip())
             for k, v in raw.items() if k}
        rid = g.get("id", "")
        if not rid and not any(g.values()):
            continue
        if not rid:
            warn.append(f"{path.name} line {n + 2}: a row with no id, skipped")
            continue
        if rid in seen:
            warn.append(f"{path.name}: duplicate row id {rid!r}, later one kept")
        seen.add(rid)
        rows.append(Row(
            id=rid, component=g.get("component", ""),
            parameter=g.get("parameter", ""), current=g.get("current_value_mm", ""),
            basis=g.get("basis", "").upper(), priority=g.get("priority", "").upper(),
            how=g.get("how_to_measure", ""), measured=g.get("MEASURED_VALUE_MM", ""),
            measured_by=g.get("MEASURED_BY", ""), date=g.get("DATE", ""),
            notes=g.get("NOTES", ""), order=n,
        ))
    return rows, warn


_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def numbers(text: str) -> tuple[float, ...]:


    return tuple(float(m.group()) for m in _NUM.finditer(text or ""))


@dataclass
class State:


    scene_obstacles: tuple[str, ...] = ()
    contact_ok: tuple[str, ...] = ()
    blocking: dict[str, float] = field(default_factory=dict)
    headroom: dict[str, float] = field(default_factory=dict)
    emit_refusal: str | None = None
    emit_ok: str = ""
    without_block: str = ""
    job_note: str = ""
    calibration: dict = field(default_factory=dict)
    calibration_note: str = ""
    fixture_summary: str = ""
    fixture_error: str = ""
    arc_file: int | None = None
    arc_note: str = ""
    swing: str = ""
    swing_exceeds: bool = False
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def configured_arc_file() -> tuple[int | None, str]:


    from . import frames

    seen: list[str] = []
    for p in sorted((REPO / "examples").glob("cell_*.json")):
        try:
            cfg = frames.load_cell_config(p)
        except (OSError, ValueError, TypeError) as exc:
            seen.append(f"{p.name}: will not load ({exc})")
            continue
        robot = str(getattr(cfg, "robot", "?"))
        arc = getattr(cfg, "arc_file", None)
        if isinstance(cfg, frames.CellConfig) and robot == "ar1440":
            bad = cfg.validation_errors()
            if bad:
                seen.append(f"{p.name}: {'; '.join(bad)}")
                continue
            return int(arc), f"{p.name} (CellConfig arc_file {arc})"
        seen.append(f"{p.name}: robot {robot}, not this cell")
    return None, ("no CellConfig in examples/ describes the AR1440 cell, so "
                  "there is no recorded ASF# to emit with -- "
                  + "; ".join(seen))


def gather() -> State:


    st = State()

    from . import cell_lc
    st.calibration = dict(cell_lc.CALIBRATION)


    st.calibration_note = "; ".join(
        [s for s in cell_lc.calibration_summary() if not s.startswith("cal ")]
        + [f"measured {st.calibration.get('measured_date') or 'never'}"])


    try:
        st.fixture_summary = cell_lc.load_fixture().summary()
    except Exception as exc:
        st.fixture_error = str(exc)

    try:
        st.arc_file, st.arc_note = configured_arc_file()
    except Exception as exc:
        st.errors.append(f"cell config scan failed: {exc.__class__.__name__}: {exc}")

    try:
        from .clearance import CONTACT_OK, TorchModel, blocking, check
        from .slicer import wall

        torch = TorchModel.yaskawa_accufast_45()
        tp = wall(PROBE_WALL_MM, PROBE_LAYERS, PROBE_LAYER_H_MM)
        x, y = (float(c) for c in cell_lc.DEFAULT_PART_ORIGIN[:2])
        origin = cell_lc.snap_toolpath_to_platter(
            tp, x, y, substrate_mm=PROBE_SUBSTRATE_MM)
        scene = cell_lc.lc_scene(part_origin_robot=origin)
        st.scene_obstacles = tuple(n for n, *_ in scene.boxes)
        st.contact_ok = tuple(CONTACT_OK)
        st.job_note = (
            f"{PROBE_WALL_MM:.0f} mm wall x {PROBE_LAYERS} layers at "
            f"({x:.0f}, {y:.0f}) = cell_lc.DEFAULT_PART_ORIGIN, "
            f"part z {origin[2]:.1f}")


        try:
            _probe_swing_envelope(cell_lc, tp, origin, st)
        except Exception as exc:
            st.errors.append(
                f"swing-envelope probe failed: {exc.__class__.__name__}: {exc}")


        gate = check(tp, torch, scene, bead_height_mm=PROBE_LAYER_H_MM,
                     margin_mm=PROBE_MARGIN_MM, contact_ok=CONTACT_OK)
        for v in blocking(gate):
            prev = st.blocking.get(v.obstacle)
            if prev is None or v.clearance_mm < prev:
                st.blocking[v.obstacle] = float(v.clearance_mm)


        for v in check(tp, torch, scene, bead_height_mm=PROBE_LAYER_H_MM,
                       margin_mm=HEADROOM_PROBE_MM, contact_ok=CONTACT_OK):
            prev = st.headroom.get(v.obstacle)
            if prev is None or v.clearance_mm < prev:
                st.headroom[v.obstacle] = float(v.clearance_mm)


        from .emit import EmitRefused, _preflight_platter_state, emit_job
        try:
            _, summary = emit_job(
                tp, cell_lc.pulse_solver(origin), name="READINESS",
                arc_file=st.arc_file, scene=scene, torch=torch,
                platter=_preflight_platter_state(scene, attested_by="readiness preflight (from calibration, NOT an operator)"),
                preflight=True, bead_height_mm=PROBE_LAYER_H_MM,
                date="0000/00/00 00:00", margin_mm=PROBE_MARGIN_MM)
            st.emit_ok = (f"{summary['positions']} positions, "
                          f"{summary['passes']} weld passes, "
                          f"{summary['deposit_mm']:.0f} mm of bead")
        except EmitRefused as exc:
            st.emit_refusal = f"{exc.reason}: {exc.detail}"


        try:
            bare = cell_lc.lc_scene(part_origin_robot=origin,
                                    training_block=False)
            left = blocking(check(tp, torch, bare, bead_height_mm=PROBE_LAYER_H_MM,
                                  margin_mm=PROBE_MARGIN_MM, contact_ok=CONTACT_OK))
            if left:
                worst = min(left, key=lambda v: v.clearance_mm)
                st.without_block = (
                    f"still {len(left)} blocking, worst {worst.obstacle} "
                    f"{worst.clearance_mm:.1f} mm -- "
                    + ("taking the block off is not enough on its own"
                       if cell_lc.TRAINING_BLOCK_ON_PLATTER else
                       "and the block is already modelled off"))
            else:
                emit_job(tp, cell_lc.pulse_solver(origin), name="READINESS",
                         arc_file=st.arc_file, scene=bare, torch=torch,
                         platter=_preflight_platter_state(bare, attested_by="readiness preflight (from calibration, NOT an operator)"),
                         preflight=True, bead_height_mm=PROBE_LAYER_H_MM,
                         date="0000/00/00 00:00", margin_mm=PROBE_MARGIN_MM)
                st.without_block = (
                    "CLEAR -- with the block off the platter this job emits. "
                    "Confirm by EYE that it is actually off, put it back "
                    "afterwards, and note the ASF# contents are still "
                    "unphotographed (H1)"
                    if cell_lc.TRAINING_BLOCK_ON_PLATTER else
                    "CLEAR -- the block is modelled OFF (calibration default), "
                    "so this IS the headline scene and it emits. What is really "
                    "on the platter is attested at emit by whoever looked; this "
                    "preflight attested from the file, not from the platter")
        except EmitRefused as exc:
            st.without_block = f"still refused -- {exc.reason}: {exc.detail}"
        except Exception as exc:
            st.without_block = f"probe failed: {exc.__class__.__name__}: {exc}"
    except Exception as exc:
        st.errors.append(f"emit probe failed: {exc.__class__.__name__}: {exc}")
    return st


def _probe_swing_envelope(cell_lc, toolpath, origin, st: State) -> None:


    touch = st.calibration.get("touch_off") or {}
    weld, tch = touch.get("weld_stickout_mm"), touch.get("touch_stickout_mm")
    artifact = (float(weld) - float(tch)) if weld is not None and tch is not None else 0.0

    ox, oy, oz = (float(c) for c in origin)
    worst, ys = 0.0, []
    for px, py, pz in toolpath.deposit_points():
        across = (ox + px) - cell_lc.FACEPLATE_X
        height = (oz + pz - artifact) - cell_lc.AXIS_Z
        worst = max(worst, swing_radius_mm(across, height))
        ys.append(oy + py)
    along = (max(ys) - min(ys)) if ys else 0.0

    st.swing_exceeds = worst > OEM_SWING_RADIUS_MM
    verdict = "OUTSIDE" if st.swing_exceeds else "inside"
    st.swing = (
        f"r{worst:.1f} mm swept about the station axis, {verdict} the OEM "
        f"envelope (r{OEM_SWING_RADIUS_MM:.0f}, dia {OEM_TOOLING_DIA_MM:.0f}); "
        f"along-axis extent {along:.0f} mm (limit {OEM_ALONG_AXIS_MM:.0f})")


def _live_face_height(st: State):
    from . import cell_lc
    h = cell_lc.PLATTER_FACE_Z - cell_lc.AXIS_Z
    return (f"{h:.1f} mm above the station axis "
            f"(cell_lc.PLATTER_FACE_Z {cell_lc.PLATTER_FACE_Z:.1f} = model "
            f"{cell_lc.PLATTER_FACE_Z_MODEL:.1f} {cell_lc.PLATTER_FACE_OFFSET_MM:+.1f} "
            f"from {CALIBRATION})"), (round(h, 3),)


def _live_fixture_size(st: State):
    from . import cell_lc
    l, w, h = cell_lc.TRAINING_BLOCK
    return (f"{l:g} x {w:g} x {h:g} mm "
            "(cell-calibration.json training_block)"), (l, w, h)


def _live_fixture_pos(st: State):
    from . import cell_lc
    mm = cell_lc.TRAINING_BLOCK_ALONG_PLATE_MM
    return (f"{mm:.1f} mm from the faceplate to the block centre, on a "
            f"{cell_lc.CHARLES_PLATE[0]:g} mm plate "
            "(cell-calibration.json training_block.along_plate_mm)"), (round(mm, 3),)


def _live_gusset_dia(st: State):
    from . import cell_lc
    return f"{cell_lc.GUSSET_DIA:g} mm (cell_lc.GUSSET_DIA)", (cell_lc.GUSSET_DIA,)


def _live_gusset_thk(st: State):
    from . import cell_lc
    return f"{cell_lc.GUSSET_THK:g} mm (cell_lc.GUSSET_THK)", (cell_lc.GUSSET_THK,)


def _live_axis_z(st: State):
    from . import cell_lc
    return f"{cell_lc.AXIS_Z:g} mm (cell_lc.AXIS_Z)", (cell_lc.AXIS_Z,)


def _live_faceplate_x(st: State):
    from . import cell_lc
    return f"{cell_lc.FACEPLATE_X:g} mm (cell_lc.FACEPLATE_X)", (cell_lc.FACEPLATE_X,)


def _live_faceplate_y(st: State):
    from . import cell_lc
    return (f"{abs(cell_lc.FACEPLATE_Y):g} mm right "
            f"(cell_lc.FACEPLATE_Y {cell_lc.FACEPLATE_Y:g})"), (abs(cell_lc.FACEPLATE_Y),)


def _live_tool_tcp(st: State):
    from . import cell_lc
    x, y, z = (float(c) for c in cell_lc.TOOL_TCP)
    return (f"TOOL 0 TCP ({x:.3f}, {y:.3f}, {z:.3f}) in our flange frame "
            f"(cell_lc.TOOL_TCP), payload {cell_lc.TOOL_MASS_KG:g} kg"), ()


def _live_feeder(st: State):
    from . import cell_lc
    x, y, z = (float(c) for c in cell_lc.FEEDER_U_ARM_TRANSLATION)
    return (f"({x:g}, {y:g}, {z:g}) in the U-link frame, Rz 180 degrees, "
            f"{cell_lc.FEEDER_MOUNT_HOLE_SET} rear-pad hole set (owner-confirmed; "
            f"placement fitted to the display mesh)"), (x, y, z)


def _live_spool(st: State):
    from . import cell_lc
    x, y, z = (float(c) for c in cell_lc.SPOOL_S_BASE_TRANSLATION)
    return (f"({x:g}, {y:g}, {z:g}) on the S-axis swivel "
            f"(cell_lc.SPOOL_S_BASE_TRANSLATION)"), (x, y, z)


def _live_payload(st: State):
    from . import cell_lc
    return (f"wrist half known: TOOL 0 payload {cell_lc.TOOL_MASS_KG:g} kg, CG "
            f"{tuple(float(c) for c in cell_lc.TOOL_COG_YASKAWA)} "
            f"(cell_lc.TOOL_MASS_KG). The U-arm feeder+bracket half is unknown, "
            f"and NOTHING in this pipeline checks the 22 kg ceiling"), ()


def _live_station_sense(st: State):
    from . import station
    return (f"STATION_SENSE {station.STATION_SENSE:+d} "
            f"(confirmed by the C1/C2/C3 swing; 0.60 deg residual)"), ()


def _live_station_pulses(st: State):
    from . import station
    predicted = 4096 * station.GEAR_RATIO / 360.0
    return (f"{predicted:.4f} p/deg confirmed by CMOS, the 161:1 nameplate, "
            f"and the mechanical specification; the emitter writes no station records"), (
                round(predicted, 2),)


def _live_soft_limits(st: State):
    from .robots import ar1440
    parts = ", ".join(f"{j.name.split('_')[-1].upper()} {j.lower_deg:g}/{j.upper_deg:g}"
                      for j in ar1440.AR1440_JOINTS)
    return f"the solver enforces {parts} (robots.ar1440.AR1440_JOINTS)", ()


def _live_arc_file(st: State):
    if st.arc_file is None:
        return (st.arc_note or "no ASF# has been looked for yet"), ()
    return f"ASF#{st.arc_file} from {st.arc_note}", (float(st.arc_file),)


def _live_pedestal_section(st: State):
    return ("the robot_pedestal box in cell_lc.lc_scene is hand-sized in the "
            "function body; it is NOT read from any constant, so filling this "
            "row in moves nothing until lc_scene is edited too"), ()


def _live_reamer(st: State):
    return ("the reamer_yair_185091 box in cell_lc.lc_scene is located by FK "
            "over the production CLEAN_R1.JBI, not from this row"), ()


@dataclass(frozen=True)
class Mapping:
    goes_to: str
    live: object = None
    obstacles: tuple[str, ...] = ()
    floor: int = LOGGED
    note: str = ""


_SHADOW = "hardware/cell_models/cell_models.py (shadow geometry only)"

_ROWS: dict[str, Mapping] = {

    "ctrl-arc-files-12": Mapping(
        goes_to="an examples/cell_*.json `arc_file` (frames.CellConfig), and "
                "the window photos into docs/",
        live=_live_arc_file, floor=EMIT,
        note="handoff H1. emit_jbi_pulse() requires arc_file and has no "
             "default: a fallback ASF# welds somebody else's schedule."),


    "platter-fixture-size": Mapping(
        goes_to="docs/cell-calibration.json training_block.length_mm / "
                ".width_mm / .height_mm (all MILLIMETRES), then the sheet "
                "cell; keep hardware/cell_models.py in step",
        live=_live_fixture_size, obstacles=("manpro_training_block",),


        floor=SIM,
        note="handoff H2, DECIDED 2026-09-04: it is the ManPro / Yaskawa "
             "training block, not WAAM work-holding, and it defaults OFF "
             "(training_block.on_platter: false). emit_job/emit_ros2 refuse "
             "without a fresh attestation of what is on the platter, so "
             "nothing here is trusted from memory. If it IS on for a run, "
             "attest it ON -- and tape it first: these dims are photo-derived."),
    "platter-fixture-pos": Mapping(
        goes_to="docs/cell-calibration.json training_block.along_plate_mm -- "
                "MILLIMETRES from the faceplate plane to the block centre, "
                "straight off the tape; then the sheet cell",
        live=_live_fixture_pos, obstacles=("manpro_training_block",),
        floor=SIM,
        note="handoff H2. NOT OURS -- ManPro / Yaskawa training hardware on a "
             "SHARED platter, so it comes and goes without anyone telling us: "
             "LOOK AT THE PLATTER, never trust the stored number. Fails in "
             "the DANGEROUS direction: a block modelled short or mislocated "
             "lets a job through that would hit it, and so does declaring it "
             "absent from memory. Was a FRACTION of the plate length until "
             "2026-09-04, which read the same as the sheet's mm value and "
             "would have moved the block by 676x if transcribed straight in."),


    "platter-face-row": Mapping(
        goes_to=f"{CALIBRATION} platter_face_offset_mm -- NOT cell_lc.py, so "
                "the collision scene and the toolpath move together",
        live=_live_face_height,
        obstacles=("charles_platter", "manpro_training_block"),
        floor=FIRST_PRINT,
        note="the one ASSUMED number in cell_lc.py. Measured 2026-09-03 by "
             "touch-off; the level of the face is a separate open item."),
    "axis-above-robot-base": Mapping(
        goes_to="cell_lc.AXIS_Z", live=_live_axis_z,
        obstacles=("charles_platter", "manpro_training_block",
                   "mh185_headstock", "positioner_stand", "faceplate_168941-1"),
        note="settles the print's 923 against the manual's 936."),
    "station-axis-forward": Mapping(
        goes_to="cell_lc.FACEPLATE_X", live=_live_faceplate_x,
        obstacles=("charles_platter", "manpro_training_block", "platter_gusset",
                   "mh185_headstock", "positioner_stand", "faceplate_168941-1")),
    "faceplate-plane-lateral": Mapping(
        goes_to="cell_lc.FACEPLATE_Y (negative = the robot's right)",
        live=_live_faceplate_y,
        obstacles=("charles_platter", "manpro_training_block", "platter_gusset",
                   "mh185_headstock", "positioner_stand", "faceplate_168941-1")),
    "platter-gusset-dia": Mapping(
        goes_to="cell_lc.GUSSET_DIA", live=_live_gusset_dia,
        obstacles=("platter_gusset",)),
    "platter-gusset-thk": Mapping(
        goes_to="cell_lc.GUSSET_THK", live=_live_gusset_thk,
        obstacles=("platter_gusset",)),


    "ctrl-tool-file": Mapping(
        goes_to="cell_lc.TOOL_TCP + cell_lc.TOOL_INTERFERE_CAPSULES",
        live=_live_tool_tcp, floor=FIRST_PRINT,
        note="every generated pulse record is built on this TCP. TOOL 0 was "
             "photographed 2026-09-03; other tool numbers are unread."),
    "ctrl-soft-limits": Mapping(
        goes_to="robots.ar1440.AR1440_JOINTS", live=_live_soft_limits,
        floor=FIRST_PRINT,
        note="the IK refuses points outside these, so a wrong set either "
             "emits a pose the controller rejects or refuses a legal one. "
             "The two datasheet editions disagree on the wrist."),
    "ctrl-station-pulses-per-deg": Mapping(
        goes_to="station.GEAR_RATIO-derived 1831.8222 p/deg",
        live=_live_station_pulses,
        note="scale confirmed; positioner-rotation jobs remain blocked because "
             "the loadable emitter and scene do not support station records."),
    "station-handedness": Mapping(
        goes_to="station.STATION_SENSE (and station.fit_station_axis "
                "consumes the C1-C3 angles)",
        live=_live_station_sense),
    "ctrl-station-calibration": Mapping(
        goes_to="station.fit_station_axis([C1, C2, C3], angles_deg=[...]), "
                "then station.STATION_SENSE",
        note="C1/C2/C3 and their S1 angles are captured; the fitted axis and "
             "sense supersede the taped layout values."),
    "ctrl-option-function": Mapping(
        goes_to="docs/ (nothing in code reads it yet)",
        note="coordinated motion gates rotating jobs; searching gates touch "
             "sense. Neither is on the first-print path."),
    "ctrl-uframe-file": Mapping(
        goes_to="frames.CellConfig.user_frame",
        note="the pulse path is joint-space, so no UF# is ever taught for it."),
    "ctrl-version": Mapping(
        goes_to="docs/ (MotoROS2 needs YAS2.80+)"),


    "arm-cluster-mass": Mapping(
        goes_to="docs/ -- there is no constant for it and no check",
        live=_live_payload,
        note="HW1484060 7.1.1: U-axis 22 kg INCLUDING the wrist. Nothing in "
             "this pipeline enforces that; it is a pendant/ARM-control item."),
    "feeder-bracket-offset": Mapping(
        goes_to="cell_lc.FEEDER_U_ARM_MOUNTS / FEEDER_MOUNT_HOLE_SET", live=_live_feeder),
    "feeder-arm-station": Mapping(
        goes_to="cell_lc.FEEDER_U_ARM_MOUNTS / FEEDER_MOUNT_HOLE_SET", live=_live_feeder),
    "feeder-bracket-pattern": Mapping(goes_to=_SHADOW),
    "feeder-bracket-envelope": Mapping(goes_to=_SHADOW),
    "feeder-body-axes": Mapping(goes_to=_SHADOW),
    "feeder-body-on-bracket": Mapping(goes_to=_SHADOW),
    "spool-mount-offset": Mapping(
        goes_to="cell_lc.SPOOL_S_BASE_TRANSLATION", live=_live_spool),
    "spool-flange-dia": Mapping(goes_to=_SHADOW),
    "spool-width": Mapping(goes_to=_SHADOW),


    "pedestal-section": Mapping(
        goes_to=_SHADOW + " -- and cell_lc.lc_scene's robot_pedestal box, "
                "which is a SECOND hand-written copy of this number",
        live=_live_pedestal_section, obstacles=("robot_pedestal",)),
    "pedestal-top-plate": Mapping(
        goes_to="hardware/cell_models/cell_models.py PEDESTAL_TOP_PLATE; "
                "cell_lc.lc_scene reads the same dimensions",
        obstacles=("robot_pedestal_top_plate",)),
    "pedestal-connections": Mapping(
        goes_to=_SHADOW + " PEDESTAL_FOOT_PLATE / PEDESTAL_GUSSET_*",
        obstacles=("robot_pedestal_foot", "robot_pedestal_top_plate")),
    "cell-base-structure": Mapping(
        goes_to=_SHADOW + " BASE_* and cell_base_parts; cell_lc.lc_scene uses their bounds",
        obstacles=("cell_base_rear_rail", "cell_base_front_rail", "cell_base_left_rail",
                   "cell_base_right_rail", "cell_base_station_crossmember",
                   *(f"cell_base_anchor_{i}" for i in range(8)))),
    "cell-deck-edge": Mapping(
        goes_to=_SHADOW + " DECK_BACK_X (display-only deck; no scene obstacle)"),
    "spool-housing-support": Mapping(
        goes_to=_SHADOW + " SPOOL_HOUSING_* / SPOOL_SUPPORT_MOUNT",
        note="Housing/support geometry is display-only; S-axis rotation is user-confirmed."),
    "wire-conduit-routing": Mapping(
        goes_to="cell_lc.wire_conduit_specs and hardware/cell_models/cell_models.py WIRE_CONDUIT_RADIUS",
        note="Flexible display curve follows S and U. No cable dynamics or collision qualification."),
    "reamer-stand-position": Mapping(
        goes_to=_SHADOW, live=_live_reamer,
        obstacles=("reamer_yair_185091",)),
    "reamer-post-section": Mapping(goes_to=_SHADOW),
    "reamer-base-thk": Mapping(goes_to=_SHADOW),
    "reamer-floor-foot-width": Mapping(goes_to=_SHADOW),
    "reamer-floor-foot-depth": Mapping(goes_to=_SHADOW),
    "reamer-head-base-thk": Mapping(goes_to=_SHADOW),
    "mh185-stand-splice-z": Mapping(goes_to=_SHADOW),
    "mh185-column-overhang": Mapping(goes_to=_SHADOW),
    "mh185-stand-column": Mapping(goes_to=_SHADOW),
    "mh185-stand-deck-z": Mapping(goes_to=_SHADOW),
    "mh185-axis-above-foot": Mapping(goes_to=_SHADOW),
    "platter-hole-x": Mapping(goes_to=_SHADOW),
    "platter-corner-r": Mapping(goes_to=_SHADOW),
    "cell-frame-fore-aft": Mapping(
        goes_to="cell_lc.FRAME_CENTER[0] (provisional robot-frame X)"),
    "cell-frame-lateral": Mapping(
        goes_to="cell_lc.FRAME_CENTER[1] (provisional robot-frame Y)"),
    "frame-envelope-datums": Mapping(
        goes_to="hardware/cell_models/cell_models.py CELL_L / CELL_W / "
                "CELL_FLOOR_LAYOUT; keep extrusion and floor-reference datums distinct"),
    "equipment-rack-registration": Mapping(
        goes_to="hardware/cell_models/cell_models.py RACK_* dimensions and "
                "cell_lc.lc_scene_placements rack pose",
        note="Delivery sheet establishes the stacked arrangement; the user "
             "confirmed relocation to the opposite enclosure side. Deck "
             "heights, member sizes and precise robot-frame offsets remain assumed."),
    "disconnect-envelope": Mapping(
        goes_to="hardware/cell_models/cell_models.py DISCONNECT_BODY and "
                "cell_lc.lc_scene_placements power_disconnect pose"),
    "waam-fixture-registration": Mapping(
        goes_to="a measured installed transform and platter declaration, then "
                "cell_lc scene geometry and collision obstacles",
        note="The imported CAD is a reference asset only. No installed pose "
             "or collision obstacle is inferred from the historical report."),
    "frame-height": Mapping(goes_to=_SHADOW),
    "frame-door-offset": Mapping(goes_to=_SHADOW),
    "frame-bay-division": Mapping(goes_to=_SHADOW),
    "frame-panel-thk": Mapping(goes_to=_SHADOW),
    "hood-cone-height": Mapping(goes_to=_SHADOW),
    "hood-mouth-dia": Mapping(goes_to=_SHADOW),
    "hood-duct-dia": Mapping(goes_to=_SHADOW),
    "hood-mouth-height": Mapping(goes_to=_SHADOW),
    "hood-plan-position": Mapping(goes_to=_SHADOW),
    "yrc1000-plinth": Mapping(goes_to=_SHADOW),
    "yrc1000-position": Mapping(goes_to=_SHADOW),
    "r450-skid": Mapping(goes_to=_SHADOW),
    "r450-position": Mapping(goes_to=_SHADOW),
}


@dataclass
class Item:
    key: str
    title: str
    tier: int
    status: str
    basis: str = ""
    priority: str = ""
    live: str = ""
    evidence: tuple[str, ...] = ()
    action: str = ""
    how: str = ""
    order: int = 0
    breadth: int = 0

    @property
    def open(self) -> bool:
        return self.status != "DONE"

    @property
    def sort_key(self) -> tuple:


        return (self.tier, 0 if self.open else 1, self.breadth,
                _PRIORITY_ORDER.get(self.priority, 3), self.order)


def _compare(row: Row, live_numbers: tuple[float, ...]) -> tuple[str, str]:


    got = numbers(row.measured)
    if not live_numbers:
        return "DONE", "measured; no live constant to compare it against"
    if not got or len(got) != len(live_numbers):
        return "DONE", ("measured, but the cell is not machine-comparable to "
                        "the constant -- check it by eye")
    off = max(abs(a - b) for a, b in zip(got, live_numbers))
    if off <= 0.5:
        return "DONE", "measured AND applied: the live constant matches"
    return "NOT APPLIED", (
        f"the sheet says {row.measured!r} but the code still says "
        f"{live_numbers if len(live_numbers) > 1 else live_numbers[0]} "
        f"(off by {off:g} mm). Nothing is using the measurement.")


def _computed_tier(m: Mapping, row: Row, st: State) -> tuple[int, list[str]]:


    tier, why = LOGGED, []
    rest: list[str] = []
    for name in m.obstacles:
        if name in st.blocking:
            tier = min(tier, EMIT)
            why.append(f"gates {name}: {st.blocking[name]:+.1f} mm -- BLOCKING "
                       f"the representative job right now")
            continue
        room = st.headroom.get(name)
        if room is None:
            tier = min(tier, SIM)
            rest.append(f"{name} >{HEADROOM_PROBE_MM:.0f}")
            continue
        if name in st.contact_ok:
            tier = min(tier, SIM)
            rest.append(f"{name} {room:+.0f} (contact forgiven)")
            continue
        if row.basis in GUESSED and room < NARROW_MARGIN_MM:
            tier = min(tier, FIRST_PRINT)
            why.append(f"gates {name} with only {room:.1f} mm of headroom, "
                       f"which is inside the error bar of a {row.basis} number "
                       f"(limit {NARROW_MARGIN_MM:.0f} mm)")
        else:
            tier = min(tier, SIM)
            rest.append(f"{name} {room:+.0f}")
    if rest:
        why.append("also dimensions " + ", ".join(rest) + " mm on this job")
    if m.live is not None:
        tier = min(tier, SIM)
    return tier, why


def _row_items(rows: list[Row], st: State) -> tuple[list[Item], list[str]]:
    warn: list[str] = []
    out: list[Item] = []
    for row in rows:
        m = _ROWS.get(row.id)
        if m is None:
            warn.append(
                f"sheet row {row.id!r} is not known to readiness.py, so it is "
                "reported as LOGGED. Either it is new (add it to _ROWS with "
                "the constant it settles) or it is stale.")
            m = Mapping(goes_to="unmapped -- nothing in readiness.py claims it")

        live_text, live_nums = "", ()
        if m.live is not None:
            try:
                live_text, live_nums = m.live(st)
            except Exception as exc:
                live_text = f"unavailable ({exc.__class__.__name__}: {exc})"
                warn.append(f"{row.id}: could not read the live value ({exc})")

        tier, why = _computed_tier(m, row, st)
        tier = min(tier, m.floor)

        if row.measured:
            status, note = _compare(row, live_nums)
        elif row.notes or row.date or row.measured_by:


            status = "PARTIAL"
            note = f"partly answered, no value yet -- sheet NOTES: {row.notes}"
        else:
            status, note = "OPEN", ""
        if note:
            why.append(note)
        if m.note:
            why.append(m.note)


        if not live_text:
            live_text = (f"{row.current} [{row.basis or 'no basis'}] -- the "
                         f"sheet's own model column")
        elif row.basis in GUESSED and not row.measured:


            live_text = f"{live_text}  [{row.basis}]"

        out.append(Item(
            key=row.id, title=row.title, tier=tier, status=status,
            basis=row.basis, priority=row.priority, live=live_text,
            evidence=tuple(why), action=m.goes_to, how=row.how,
            order=row.order, breadth=len(m.obstacles)))
    return out, warn


def _stickout_gate(st: State) -> Item:
    touch = (st.calibration.get("touch_off") or {})
    weld = touch.get("weld_stickout_mm")
    tch = touch.get("touch_stickout_mm")
    why: list[str] = []
    if weld is None:
        status = "OPEN"
        why.append(f"touch_off.weld_stickout_mm is unset in {CALIBRATION}, so "
                   f"platter_face_offset_mm is correct ONLY for welding at the "
                   f"{tch} mm it was touched off at")
        why.append("welding LONGER than you touched off drives the wire INTO "
                   "the plate; welding shorter only lifts the arc")
        try:
            from . import cell_lc
            pulses = touch.get("pulses_SLURBT")
            if pulses and tch is not None:
                for label, mm in (("3/8 in", 9.525), ("1/2 in", 12.7)):
                    off = cell_lc.offset_from_touch_off(
                        pulses, substrate_mm=float(touch.get("substrate_mm", 0.0)),
                        touch_stickout_mm=float(tch), weld_stickout_mm=mm)
                    dz = off - float(st.calibration.get("platter_face_offset_mm", 0.0))
                    why.append(f"at {label} stickout the offset is {off:.1f} "
                               f"(the wire runs {dz:.1f} mm into the plate)")
        except Exception as exc:
            why.append(f"could not re-derive the offset ({exc})")
    else:
        status = "DONE"
        why.append(f"weld stickout {weld} mm, touched off at {tch} mm")
    return Item(
        key="cal:weld_stickout", title="welding stickout the job will run at",
        tier=FIRST_PRINT, status=status, basis="DECIDE", priority="H",
        live=f"touch {tch} mm / weld {weld} ({CALIBRATION} touch_off)",
        evidence=tuple(why),
        action=f"{CALIBRATION} touch_off.weld_stickout_mm, then re-derive with "
               f"cell_lc.offset_from_touch_off(...). Better: re-touch AT the "
               f"welding stickout so the difference is zero.",
        how="Decide the process stickout, then either write it into the "
            "calibration file or re-take the touch-off at it.",
        order=1)


def _level_gate(st: State) -> Item:
    fixtures = st.calibration.get("fixtures") or {}
    active = st.calibration.get("active_fixture")
    touch = (st.calibration.get("touch_off") or {})
    why: list[str] = []
    if fixtures and active:
        status, live = "DONE", f"fixture {active!r} ({len(fixtures)} recorded)"
        if st.fixture_summary:
            why.append(st.fixture_summary)
        else:
            status = "OPEN"
            why.append(f"active_fixture will not load: {st.fixture_error}")
    else:
        status = "OPEN"
        live = (f"one touch-off point at "
                f"{st.calibration.get('measured_at_xy')} -- a scalar, blind to tilt")
        why.append(f"{CALIBRATION}: fixtures {{}} and active_fixture "
                   f"{active!r}, so the height comes from a single point")
        if touch.get("single_point"):
            why.append("touch_off.single_point is true: one point cannot "
                       "express a tilted plate, and averaging one is not a fit")
    return Item(
        key="cal:platter_level", title="platter level -- two more touch-offs",
        tier=FIRST_PRINT, status=status, basis="MEASURE", priority="H",
        live=live, evidence=tuple(why),
        action=f"jog to contact at 2+ more well-spread XY, then "
               f"`python examples/touch_off_calibrate.py NAME --save` and set "
               f"active_fixture in {CALIBRATION}",
        how="Repeat the wire touch-off at two more positions across the "
            "platter, well apart. Record the pulses and the part-frame XY of "
            "each. If the three readings disagree by more than a millimetre "
            "or two, SAY SO rather than averaging it away.",
        order=2)


def _swing_envelope(st: State) -> Item:


    if not st.swing:
        return Item(
            key="cell:swing_envelope",
            title="OEM tooling swing envelope (dia 490, 1450 along the axis)",
            tier=LOGGED, status="DONE", basis="MODEL",
            live="not probed", order=3,
            action="docs/redline-analysis-2026-09-07.md #2; report only.")
    why = [
        st.swing,
        "historical reference (redline-analysis-2026-09-07 #2): the plate edge sweeps "
        "r221.3 (inside), a 100 mm wall on that edge r273.1 (OUTSIDE dia 490)",
        "report only -- the scale is confirmed, but nothing emits a station "
        "rotation while scene/emitter support is absent, so this never gates; it "
        "matters only if S1 is driven under the part",
    ]
    return Item(
        key="cell:swing_envelope",
        title="OEM tooling swing envelope: does the part clear the positioner "
              "tooling limit if S1 rotates",
        tier=LOGGED, status="OUTSIDE" if st.swing_exceeds else "DONE",
        basis="MODEL", live=st.swing, evidence=tuple(why),
        action="docs/redline-analysis-2026-09-07.md #2. A vendor reference dim; "
               "cell_lc.py models no station rotation, so nothing consumes it. "
               "If a job must sweep, keep tall build away from the plate edge.",
        order=3)


def build_items(rows: list[Row], st: State) -> tuple[list[Item], list[str]]:

    items, warn = _row_items(rows, st)
    items += [_stickout_gate(st), _level_gate(st), _swing_envelope(st)]
    items.sort(key=lambda i: i.sort_key)
    return items, warn


def _wrap(text: str, width: int, indent: str, hang: str | None = None) -> list[str]:


    import textwrap
    return textwrap.wrap(
        text, width=width, initial_indent=indent,
        subsequent_indent=" " * len(indent) if hang is None else hang,
    ) or [indent.rstrip()]


def report(sheet: Path = SHEET, *, verbose: bool = False,
           show_all: bool = False, state: State | None = None
           ) -> tuple[bool, list[str]]:

    rows, warn = load_sheet(sheet)
    st = gather() if state is None else state
    items, more = build_items(rows, st)
    warn += more

    measured = sum(1 for r in rows if r.measured)
    out = [
        f"sheet        {sheet}",
        f"             {len(rows)} rows, {measured} measured, "
        f"{len(rows) - measured} blank",
        f"calibration  {CALIBRATION}: {st.calibration_note or 'not read'}",
    ]
    if st.job_note:
        out.append(f"probe job    {st.job_note}")
    if st.scene_obstacles:
        out.append(f"scene        {len(st.scene_obstacles)} obstacles: "
                   + ", ".join(st.scene_obstacles))
    out += ["", "WHAT THE EMITTER DOES TODAY"]
    if st.emit_refusal:
        out.append(f"  emit_job   REFUSED -- {st.emit_refusal}")
    elif st.emit_ok:
        out.append(f"  emit_job   emits ({st.emit_ok})")
    else:
        out.append("  emit_job   not probed")
    if st.blocking:
        for name, mm in sorted(st.blocking.items(), key=lambda kv: kv[1]):
            out.append(f"  gate       {name} {mm:+.1f} mm  (blocking)")
    else:
        out.append("  gate       clearance clean on the representative job")
    if st.without_block:
        out.append(f"  block off  {st.without_block}")
    if st.swing:
        out.append(f"  swing      {st.swing}")
    for e in st.errors:
        out.append(f"  probe      {e}")

    for tier, (name, gloss) in enumerate(TIERS):
        group = [i for i in items if i.tier == tier]
        if not group:
            continue
        opens = [i for i in group if i.open]
        head = f"== {name} ==  ({gloss})"
        if tier not in GATING and not show_all:
            part = sum(1 for i in group if i.status == "PARTIAL")
            out += ["", head,
                    f"  {len(group)} item(s), {len(opens)} open"
                    + (f" ({part} partly answered)" if part else "")
                    + ". --all to list."]
            continue
        out += ["", head]
        shown = group if show_all else (opens or group)
        for n, it in enumerate(shown, 1):
            out.append(f"{n:2d}. [{it.priority or '-'}] {it.key}   {it.status}")
            out += _wrap(it.title, 78, "    ")
            out += _wrap(f"now:  {it.live}", 78, "      ", "            ")
            for e in it.evidence:
                out += _wrap(e, 78, "      - ", "        ")
            out += _wrap(f"->    {it.action}", 78, "      ", "            ")
            if verbose and it.how:
                out += _wrap(f"take: {it.how}", 78, "      ", "            ")
        done = [i.key for i in group if not i.open]
        if not show_all and done and len(shown) < len(group):
            out.append(f"    (+{len(done)} already done: {', '.join(done)})")

    blocked = [i for i in items if i.tier in GATING and i.open]
    emitter_blocked = bool(st.emit_refusal)
    if warn:
        out += [""] + [f"warn: {w}" for w in warn]
    out.append("")
    if blocked or emitter_blocked:
        count = len(blocked) + int(emitter_blocked)
        out.append(f"FAIL: {count} item(s) in BLOCKS EMIT / BLOCKS FIRST "
                   f"PRINT are open.")
        if emitter_blocked:
            out.append(f"      BLOCKS EMIT: emitter refused the representative job -- {st.emit_refusal}")
        out.append("      python -m waam_pipeline.readiness --capture   "
                   "<- what to take, in order")
    else:
        out.append("PASS: nothing in the blocking tiers is open. The emitter "
                   "will produce a job")
        out.append("      and the cell numbers behind it are measured.")
    return not blocked and not emitter_blocked, out


def capture_list(sheet: Path = SHEET, state: State | None = None) -> list[str]:


    rows, _ = load_sheet(sheet)
    st = gather() if state is None else state
    items, _ = build_items(rows, st)
    todo = [i for i in items if i.tier in GATING and i.open]

    out = ["EMIT-PATH CAPTURE LIST -- ordered by what unblocks what",
           "",
           f"This is the emit-path subset only. The whole-visit card is "
           f"{CHECKLIST}:",
           "take the pendant USB dump and the nameplates from there as well; "
           "the dump",
           "answers several of these in one action.",
           ""]
    if not todo:
        out.append("Nothing is open. Take the sheet anyway for the M and L rows.")
        return out
    for n, it in enumerate(todo, 1):
        out.append(f"{n}. [{TIERS[it.tier][0]}] {it.key}")
        out += _wrap(it.title, 78, "   ")
        out += _wrap(f"now:  {it.live}", 78, "     ", "           ")
        if it.how:
            out += _wrap(f"take: {it.how}", 78, "     ", "           ")
        out += _wrap(f"goes: {it.action}", 78, "     ", "           ")
        out.append("")
    out += ["Then, back at a keyboard:",
            "    python -m waam_pipeline.readiness",
            "It re-reads the sheet and the calibration file and will say which",
            "measurements landed in the code and which are still only on paper."]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sheet", type=Path, default=SHEET,
                    help="check this worksheet instead of the committed one")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the full how_to_measure prose for each item")
    ap.add_argument("-a", "--all", action="store_true", dest="show_all",
                    help="list the non-blocking tiers too, and the done items")
    ap.add_argument("--capture", action="store_true",
                    help="print the ordered capture list for a lab visit")
    args = ap.parse_args(argv)

    if args.capture:
        print("\n".join(capture_list(args.sheet)))
        return 0
    ok, lines = report(args.sheet, verbose=args.verbose, show_all=args.show_all)
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
