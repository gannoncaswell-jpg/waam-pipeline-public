from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .backends.jbi import (DEFAULT_AIR_MM_MIN, DEFAULT_WELD_MM_MIN, MAX_DWELL_S, _check_arc_file,
                           emit_cycle_jbi, emit_jbi_pulse, v_speed_mm_min, v_token)
from .backends.jbi_read import read_jbi
from .backends.ros2 import ArcIo, Ros2Job, Ros2Refused, build_ros2_job
from .clearance import (CONTACT_OK, DEFAULT_MARGIN_MM, ClearanceError, Scene,
                        TorchModel, WireHangError, blocking, check, point_guard)
from .passes import arc_numbers, asf_label, narrow_arc, normalize_arc, pass_table
from .plan import MeasureStop, Step, bead_dwell_note
from .robots.ar1440 import V_TOKEN_MM_PER_MIN as LC_V_UNIT_MM_PER_MIN
from .toolpath import Toolpath, single_part
from . import weld_ctwd


class EmitRefused(RuntimeError):


    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PlatterState:


    clear: bool
    training_block_on: bool = False
    fixture: str | None = None
    attested_by: str = ""

    def validation_errors(self) -> list[str]:
        e: list[str] = []
        if not self.clear:
            e.append("platter not attested clear: look at it, take off anything "
                     "undeclared, then attest")
        if not (self.attested_by or "").strip():
            e.append("attested_by is empty: say who looked")
        if self.fixture is not None and not str(self.fixture).strip():
            e.append("fixture must be a registered name or None")
        return e

    def stamp(self) -> str:

        return (f"platter attested by {self.attested_by.strip()}: training block "
                f"{'ON' if self.training_block_on else 'off'}, fixture "
                f"{self.fixture or 'none (bare platter)'}")


class _PreflightPlatterState(PlatterState):


    def stamp(self) -> str:
        return (f"platter NOT ATTESTED (preflight, {self.attested_by.strip()}): "
                f"training block {'ON' if self.training_block_on else 'off'}, "
                f"fixture {self.fixture or 'none (bare platter)'}")


def _preflight_platter_state(scene, *, attested_by: str) -> _PreflightPlatterState:


    on = any(b[0] == "manpro_training_block" for b in scene.boxes)
    return _PreflightPlatterState(clear=True, training_block_on=on,
                                  attested_by=attested_by)


def _scene_gate(scene) -> "int":


    obstacles = 0 if scene is None else len(scene.boxes) + len(scene.capsules)
    if obstacles == 0:
        raise EmitRefused(
            "clearance",
            "this scene models nothing, so the check cannot fail. If the "
            "platter really is bare, say so with cell_lc.lc_scene(...), which "
            "still carries the platter, the faceplate and the headstock.",
        )
    return obstacles


def _calibration_gate() -> None:


    from . import cell_lc
    reason = cell_lc.calibration_stale_reason()
    if reason:
        raise EmitRefused("calibration", reason)


def _platter_gate(platter, scene, *, preflight: bool = False) -> None:

    if platter is None:
        raise EmitRefused(
            "attestation",
            "no platter attestation. Look at the platter and say what is on it: "
            "PlatterState(clear=True, training_block_on=..., fixture=..., "
            "attested_by='who'). There is deliberately no default.",
        )
    if isinstance(platter, _PreflightPlatterState) and not preflight:
        raise EmitRefused(
            "attestation",
            "this platter state was DERIVED FROM THE SCENE by a preflight, not "
            "from a person who looked at the platter. It is only valid for a "
            "caller that declares preflight=True (waam_pipeline.readiness). "
            "Emit needs a real PlatterState.",
        )
    errs = platter.validation_errors()
    if errs:
        raise EmitRefused("attestation", "; ".join(errs))
    has_block = any(b[0] == "manpro_training_block" for b in scene.boxes)
    if has_block != platter.training_block_on:
        raise EmitRefused(
            "attestation",
            f"you attested the training block is "
            f"{'ON' if platter.training_block_on else 'OFF'} but this job was "
            f"checked against a scene with it "
            f"{'present' if has_block else 'absent'}. Build the scene from the "
            "attestation (lc_scene(training_block=...)) or re-slice with the "
            "platter setting matching what you see -- the check and the "
            "attestation have to be about the same platter.",
        )
    if platter.fixture:
        from . import cell_lc
        try:
            cell_lc.load_fixture(platter.fixture)
        except (ValueError, KeyError) as exc:
            raise EmitRefused(
                "attestation",
                f"fixture {platter.fixture!r} is not registered: {exc}. Record "
                "it with examples/touch_off_calibrate.py NAME --save",
            ) from exc


def platter_from_cli(argv=None, *, default_by: str | None = None) -> PlatterState:


    import argparse
    import getpass
    import sys

    from . import cell_lc

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--platter-clear", action="store_true")
    ap.add_argument("--block", choices=("on", "off"), default=None)
    ap.add_argument("--fixture", default=None)
    ap.add_argument("--by", default=None)
    ns, _rest = ap.parse_known_args(argv)
    who_default = default_by or _safe_user(getpass)

    if ns.platter_clear:
        block = (ns.block == "on") if ns.block else cell_lc.TRAINING_BLOCK_ON_PLATTER
        return PlatterState(clear=True, training_block_on=block,
                            fixture=ns.fixture or None, attested_by=ns.by or who_default)

    if not sys.stdin.isatty():
        raise EmitRefused(
            "attestation",
            "no platter attestation: pass --platter-clear [--block on|off] "
            "[--fixture NAME] [--by NAME], or run on a terminal and answer the "
            "questions. There is deliberately no default.",
        )

    def ask(q, default=""):


        try:
            a = input(q).strip()
        except EOFError:
            raise EmitRefused(
                "attestation",
                "no platter attestation: stdin reported a terminal but gave no "
                "answer, so nobody attested. Pass --platter-clear "
                "[--block on|off] [--fixture NAME] [--by NAME], or run on a "
                "real terminal. There is deliberately no default.",
            ) from None
        return a if a else default

    print("Look at the platter before answering. This goes into the job.")
    if ask("  Is the platter clear of anything NOT declared below? [y/N] ").lower() != "y":
        raise EmitRefused("attestation", "platter not attested clear")
    dflt = "y" if cell_lc.TRAINING_BLOCK_ON_PLATTER else "n"
    block = ask(f"  Is the ManPro training block on the platter? [y/n, default {dflt}] ",
                dflt).lower() == "y"
    fixture = ask("  Fixture mount on the platter? (registered name, or blank) ") or None
    by = ask(f"  Attested by [{who_default}]: ", who_default)
    return PlatterState(clear=True, training_block_on=block, fixture=fixture, attested_by=by)


def _safe_user(getpass_mod) -> str:
    try:
        return getpass_mod.getuser()
    except Exception:
        return "unknown"


MIN_INTERPASS_S = 1.0


def _interpass_gate(toolpath: Toolpath, interpass_timer_s: float,
                    allow_no_interpass: bool, layers_per_interpass: int = 1) -> None:

    if type(layers_per_interpass) is not int or layers_per_interpass < 1:
        raise EmitRefused("argument", "layers_per_interpass must be a whole number "
                          f">= 1, got {layers_per_interpass!r}")
    layers = len(toolpath.layers())
    if allow_no_interpass or layers < 2:
        return
    moves = toolpath.moves


    lit = next((i for i in range(1, len(moves))
                if moves[i].layer != moves[i - 1].layer
                and moves[i].deposit and moves[i - 1].deposit), None)
    if lit is not None:
        raise EmitRefused(
            "interpass",
            f"layer {moves[lit - 1].layer} runs into layer {moves[lit].layer} with "
            f"the arc lit (move {lit}), so no interpass dwell can be inserted. "
            "Last year's continuous wall failed at layer 4 (Final Report p50). "
            "Add a travel between layers, or pass allow_no_interpass=True if the "
            "part is cooled some other way",
        )
    if interpass_timer_s < MIN_INTERPASS_S or not math.isfinite(interpass_timer_s):
        raise EmitRefused(
            "interpass",
            f"{layers} layers with interpass_timer_s="
            f"{interpass_timer_s:g}. Last year's wall on this cell reached 520 C "
            "by layer 7 and the continuous weld failed at layer 4 (Final Report "
            "p50). Set interpass_timer_s, or pass allow_no_interpass=True if the "
            "part is cooled some other way",
        )
    if interpass_timer_s > MAX_DWELL_S:
        raise EmitRefused(
            "interpass",
            f"interpass_timer_s={interpass_timer_s:g} is over {MAX_DWELL_S:g} s. Longer "
            "dwells are split into TIMERs of at most 655.35 s, but one dwell is capped at an hour",
        )
    if (layers - 1) // layers_per_interpass == 0:
        raise EmitRefused(
            "interpass",
            f"{layers} layers with a dwell every {layers_per_interpass} layers "
            "never pauses. Last year's continuous wall failed at layer 4 (Final "
            f"Report p50). Set layers per interpass to {layers - 1} or fewer",
        )


def _now() -> str:

    return datetime.now().strftime("%Y/%m/%d %H:%M")


_FEED_TOL_MM_MIN = 1e-6

PER_MOVE = "per-move"


def _arc_gate(arc_file, asf_attested: bool, emitted: Toolpath):


    if isinstance(arc_file, Mapping):
        try:
            _check_arc_file(arc_file)
        except ValueError as exc:
            raise EmitRefused("argument", str(exc)) from exc
    arc_file = normalize_arc(narrow_arc(arc_file, emitted))
    if isinstance(arc_file, Mapping) and asf_attested is not True:
        raise EmitRefused(
            "attestation",
            f"this job uses ASF#/AEF# {asf_label(arc_file)}. Confirm each listed "
            "ASF/AEF exists on the pendant (ARC WELDING > ARC START/END COND.) "
            "before exporting; there is deliberately no default",
        )
    return arc_file


def _job_passes(text, emitted, weld_v, arc_file, model):
    try:
        return pass_table(emitted, text=text, v_unit_mm_per_min=LC_V_UNIT_MM_PER_MIN,
                          weld_v=weld_v, arc_file=arc_file, model=model)
    except ValueError as exc:
        raise EmitRefused("roundtrip", str(exc)) from exc


def _weld_v(toolpath: Toolpath, weld_v: "str | None",
            unit_mm_per_min: float = LC_V_UNIT_MM_PER_MIN) -> "str | None":


    feeds = [m.feed for m in toolpath.moves if m.deposit]
    known = [f for f in feeds if f is not None]
    if not known:
        return weld_v
    if len(known) != len(feeds):
        raise EmitRefused(
            "speed",
            f"{len(feeds) - len(known)} of {len(feeds)} deposit moves carry no "
            "feed while the rest do, so the toolpath does not say what speed "
            "to weld at. Set F on every deposit move, or on none of them",
        )
    for f in known:
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0:
            raise EmitRefused("speed", f"a deposit move asks for {f!r} mm/min; a weld "
                              "feed must be a finite positive speed")
        try:
            v_token(f, unit_mm_per_min)
        except ValueError as exc:
            raise EmitRefused("speed", str(exc)) from exc
    lo, hi = min(known), max(known)
    if hi - lo > _FEED_TOL_MM_MIN:
        if weld_v is not None:
            raise EmitRefused(
                "speed",
                f"weld_v={weld_v!r} is one speed, but the deposit moves ask for "
                f"{lo:g}..{hi:g} mm/min and each weld move carries its own V=. "
                "Drop weld_v and let the toolpath decide",
            )
        return PER_MOVE
    derived = v_token(lo, unit_mm_per_min)
    if weld_v is None:
        return derived
    try:
        given_mm_min = v_speed_mm_min(weld_v, unit_mm_per_min)
    except ValueError as exc:
        raise EmitRefused("argument", str(exc)) from exc

    if abs(float(weld_v) - float(derived)) > 1e-9:
        raise EmitRefused(
            "speed",
            f"weld_v={weld_v!r} is {given_mm_min:g} mm/min but the toolpath was "
            f"planned at {lo:g} mm/min (V={derived}). The bead depends on both, "
            "so pick one: re-slice at the speed you want to weld, or drop "
            "weld_v and let the toolpath decide",
        )


    return weld_v


def _gate(
    toolpath: Toolpath,
    scene: Scene,
    torch: TorchModel,
    platter: "PlatterState | None",
    *,
    bead_width_mm: float,
    bead_height_mm: float,
    margin_mm: float,
    interpass_timer_s: float,
    allow_no_interpass: bool,
    layers_per_interpass: int = 1,
    preflight: bool = False,
    weld_ctwd_mm: float | None = None,
    clean_first: bool = False,
) -> tuple[Toolpath, list, dict]:


    if not math.isfinite(bead_height_mm) or bead_height_mm <= 0.0:
        raise ValueError(f"bead_height_mm must be finite and > 0, got {bead_height_mm}")
    _calibration_gate()
    _interpass_gate(toolpath, interpass_timer_s, allow_no_interpass, layers_per_interpass)
    _scene_gate(scene)
    _platter_gate(platter, scene, preflight=preflight)
    try:
        weld_ctwd.resolve(weld_ctwd_mm)
        if type(clean_first) is not bool:
            raise ValueError("clean_first must be a bool")
    except ValueError as exc:
        raise EmitRefused("ctwd", str(exc)) from exc
    raised, ctwd, delta, violations, hanging, notes = weld_ctwd.checked(
        toolpath, weld_ctwd_mm, clean_first=clean_first,
        torch=torch, scene=scene, bead_width_mm=bead_width_mm,
        bead_height_mm=bead_height_mm, margin_mm=margin_mm, contact_ok=CONTACT_OK)
    if hanging:
        raise EmitRefused("ctwd", weld_ctwd.hang_refusal(hanging, delta))
    blockers = blocking(violations)
    if blockers:
        worst = min(blockers, key=lambda v: v.clearance_mm)
        raise EmitRefused(
            "clearance",
            f"{worst.obstacle} at move {worst.move_index} "
            f"{tuple(round(float(c), 1) for c in worst.point)}: "
            f"{worst.clearance_mm:.1f} mm",
        )
    return raised, violations, {"weld_ctwd_mm": ctwd, "weld_ctwd_delta_mm": delta,
                                "weld_ctwd_notes": notes}


def _ctwd_lines(ctwd: dict) -> tuple[str, ...]:

    return (weld_ctwd.stamp(ctwd["weld_ctwd_mm"]),) if ctwd["weld_ctwd_delta_mm"] else ()


def _clean_first_lines(clean_first: bool) -> tuple[str, ...]:
    return ("CLEAN WIRE BEFORE FIRST WELD",) if clean_first else ()


def _measure_stops(steps: list[Step], air_v: str | None) -> list[dict]:


    if not any(st.measurement is not None for st in steps):
        return []
    from .cell_lc import TOOL_TCP
    from .robots.ar1440 import fk

    speed = v_speed_mm_min(air_v or v_token(DEFAULT_AIR_MM_MIN, LC_V_UNIT_MM_PER_MIN),
                          LC_V_UNIT_MM_PER_MIN) / 60
    found, previous, pending, distance = [], None, None, 0.0
    for st in steps:
        if st.kind == "arc_off":
            distance = 0.0
        if not st.is_motion:
            continue
        if st.measurement is not None:
            pending = st
        if previous is not None:
            if previous.xyz is not None and st.xyz is not None:
                distance += math.dist(previous.xyz, st.xyz)
            elif pending is not None and st.role == "park":
                start, end = fk(previous.q)["flange"], fk(st.q)["flange"]
                distance += math.dist(start[:3, :3] @ TOOL_TCP + start[:3, 3],
                                      end[:3, :3] @ TOOL_TCP + end[:3, 3])
        if pending is not None and st.role == "park":
            config = pending.measurement
            entry = {"after_layer": pending.layer, "readings_s": list(config.readings_s),
                     "pause": config.pause, "clean": config.clean,
                     "pre_weld": config.pre_weld and not pending.measurement_final,
                     "park_travel_s_est": float(distance / speed)}
            if pending.measurement_final:
                entry["final"] = True
            found.append(entry)
            pending = None
        previous = st
    return found


def emit_job(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    arc_file: int,
    scene: Scene,
    torch: TorchModel,
    date: str | None = None,
    bead_height_mm: float,
    bead_width_mm: float = 9.0,
    margin_mm: float = DEFAULT_MARGIN_MM,
    weld_v: str | None = None,
    air_v: str | None = None,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    allow_no_interpass: bool = False,
    comments: tuple[str, ...] = (),
    provenance: tuple[str, ...] = (),
    platter: "PlatterState | None" = None,


    preflight: bool = False,
    layers: tuple[int, int] | None = None,
    folder: str | None = None,
    smooth: bool = False,
    asf_attested: bool = False,
    deposit_model: tuple[float, float] | None = None,
    weld_ctwd_mm: float | None = None,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
) -> tuple[str, dict]:


    toolpath, violations, ctwd = _gate(
        toolpath, scene, torch, platter,
        bead_width_mm=bead_width_mm, bead_height_mm=bead_height_mm,
        margin_mm=margin_mm, interpass_timer_s=interpass_timer_s,
        allow_no_interpass=allow_no_interpass,
        layers_per_interpass=layers_per_interpass, preflight=preflight,
        weld_ctwd_mm=weld_ctwd_mm, clean_first=clean_first)
    emitted, provenance = _layer_range(toolpath, layers, provenance)
    clean_first = clean_first and any(move.deposit for move in emitted.moves)
    arc_file = _arc_gate(arc_file, asf_attested, emitted)
    _interpass_gate(emitted, interpass_timer_s, allow_no_interpass, layers_per_interpass)
    weld_v = _weld_v(emitted, weld_v)
    per_move = weld_v == PER_MOVE
    if per_move:
        weld_v = None
    if isinstance(arc_file, Mapping):
        provenance = tuple(provenance) + ("ASF/AEF list attested",)

    planned_steps: list[Step] = []
    try:
        text = emit_jbi_pulse(
            emitted,
            solve,
            name=name,
            date=date or _now(),
            arc_file=arc_file,
            scene=scene,
            torch=torch,
            margin_mm=margin_mm,
            weld_v=weld_v,
            air_v=air_v,
            hover_mm=hover_mm,
            interpass_timer_s=interpass_timer_s,
            layers_per_interpass=layers_per_interpass,
            comments=comments,
            provenance=(tuple(provenance) + _ctwd_lines(ctwd)
                        + _clean_first_lines(clean_first) + (platter.stamp(),)),
            folder=folder,
            smooth=smooth,
            weld_v_by_feed=per_move,
            temp_check_every=temp_check_every,
            bead_dwell_s=bead_dwell_s,
            measure=measure, clean_first=clean_first, planned_steps=planned_steps,
        )
    except WireHangError as exc:
        raise EmitRefused("ctwd", str(exc)) from exc
    except ClearanceError as exc:

        raise EmitRefused("clearance", str(exc)) from exc
    except ValueError as exc:


        raise EmitRefused("argument", str(exc)) from exc

    summary = _validate(text, name, folder)
    summary["violations"] = len(violations)
    summary["deposit_mm"] = round(float(emitted.deposit_length()), 1)
    summary["platter"] = platter.stamp()
    summary.update(ctwd)
    if layers is not None:
        summary["layers"] = list(layers)
    else:
        deposit_layers = [move.layer for move in emitted.moves if move.deposit]
        summary["layers"] = [min(deposit_layers), max(deposit_layers)] if deposit_layers else []
    summary["asf"] = arc_numbers(arc_file)
    summary["pass_table"] = _job_passes(text, emitted, weld_v, arc_file, deposit_model)
    summary["measure_stops"] = _measure_stops(planned_steps, air_v)
    summary["clean_first"] = clean_first
    note = bead_dwell_note(emitted, bead_dwell_s)
    if note:
        summary["notes"] = [note]
    return text, summary


def emit_cycle_job(toolpath: Toolpath, solve, *, name: str, arc_file: int,
                   cycle: int, layers: tuple[int, int], step_mm: float,
                   tail: dict | None, scene: Scene, torch: TorchModel,
                   bead_height_mm: float, date: str | None = None,
                   bead_width_mm: float = 9.0, margin_mm: float = DEFAULT_MARGIN_MM,
                   weld_v: str | None = None, air_v: str | None = None,
                   hover_mm: float = 50.0, interpass_timer_s: float = 30.0,
                   layers_per_interpass: int = 1, comments: tuple[str, ...] = (),
                   provenance: tuple[str, ...] = (), platter: PlatterState | None = None,
                   folder: str | None = None, smooth: bool = False, asf_attested: bool = False,
                   deposit_model: tuple[float, float] | None = None,
                   weld_ctwd_mm: float | None = None,
                   temp_check_every: int = 0, bead_dwell_s: float = 0.0,
                   measure: MeasureStop | None = None,
                   clean_first: bool = False) -> tuple[str, dict]:


    from .layer_sense import SENSE_BASE, sense_motions

    single_part(toolpath, "Auto cycle export")

    raised, violations, ctwd = _gate(
        toolpath, scene, torch, platter,
        bead_width_mm=bead_width_mm, bead_height_mm=bead_height_mm,
        margin_mm=margin_mm, interpass_timer_s=interpass_timer_s,
        allow_no_interpass=False, layers_per_interpass=layers_per_interpass,
        weld_ctwd_mm=weld_ctwd_mm, clean_first=clean_first)
    emitted, provenance = _layer_range(raised, layers, provenance)
    arc_file = _arc_gate(arc_file, asf_attested, emitted)
    if isinstance(arc_file, Mapping):
        provenance = tuple(provenance) + ("ASF/AEF list attested",)
    first, last = layers
    deposit_layers = [move.layer for move in toolpath.moves if move.deposit]
    if not deposit_layers:
        raise EmitRefused("argument", "An Auto cycle needs weld layers")
    top = max(deposit_layers)
    expected_last = min(first + layers_per_interpass - 1, top)
    if last != expected_last:
        raise EmitRefused("argument", f"Auto cycle layers must end at {expected_last} "
                          f"for {layers_per_interpass} layers per cycle")
    if (tail is None) != (last == top):
        raise EmitRefused("argument", "The final Auto cycle must omit its touch tail; "
                          "every earlier cycle must include it")
    weld_v = _weld_v(emitted, weld_v)
    per_move = weld_v == PER_MOVE
    if per_move:
        weld_v = None
    planned_steps: list[Step] = []
    try:
        if tail is not None:
            sensed, begin = sense_motions(toolpath, last, tail["plan"], hop_mm=tail["hop_mm"])
            touch_hits = [v for v in blocking(check(
                sensed, torch, scene, bead_width_mm=bead_width_mm,
                bead_height_mm=step_mm, margin_mm=margin_mm, contact_ok=CONTACT_OK))
                if v.move_index >= begin]
            if touch_hits:
                worst = min(touch_hits, key=lambda v: v.clearance_mm)
                raise EmitRefused("clearance", f"{worst.obstacle} at touch move {worst.move_index}: "
                                  f"{worst.clearance_mm:.1f} mm")
        text = emit_cycle_jbi(
            emitted, solve, name=name, date=date or _now(), arc_file=arc_file,
            cycle={"cycle": cycle, "first": first, "last": last, "step_mm": step_mm},
            tail=None if tail is None else dict(tail, interpass_s=interpass_timer_s, pvar_base=SENSE_BASE),
            scene=scene, torch=torch, margin_mm=margin_mm, weld_v=weld_v,
            air_v=air_v, hover_mm=hover_mm, smooth=smooth, comments=comments,
            provenance=(tuple(provenance) + _ctwd_lines(ctwd)
                        + _clean_first_lines(clean_first) + (platter.stamp(),)), folder=folder,
            weld_v_by_feed=per_move,
            temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s,
            measure=measure, clean_first=clean_first, planned_steps=planned_steps,
        )
    except WireHangError as exc:
        raise EmitRefused("ctwd", str(exc)) from exc
    except ClearanceError as exc:
        raise EmitRefused("clearance", str(exc)) from exc
    except ValueError as exc:
        raise EmitRefused("argument", str(exc)) from exc
    summary = _validate(text, name, folder)
    summary.update(violations=len(violations), deposit_mm=round(float(emitted.deposit_length()), 1),
                   platter=platter.stamp(), cycle=cycle, layers=list(layers),
                   probes=0 if tail is None else len(tail["plan"]), last_cycle=tail is None,
                   step_mm=step_mm, asf=arc_numbers(arc_file),
                   pass_table=_job_passes(text, emitted, weld_v, arc_file, deposit_model), **ctwd)
    summary["measure_stops"] = _measure_stops(planned_steps, air_v)
    summary["clean_first"] = clean_first
    note = bead_dwell_note(emitted, bead_dwell_s)
    if note:
        summary["notes"] = [note]
    return text, summary


def emit_ros2(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    scene: Scene,
    torch: TorchModel,
    arc_io: ArcIo | None = None,
    weld: bool = False,
    provenance: tuple[str, ...] = (),
    bead_height_mm: float,
    bead_width_mm: float = 9.0,
    margin_mm: float = DEFAULT_MARGIN_MM,
    weld_v: str | None = None,
    air_v: str | None = None,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    hold_axes: tuple[str, ...] = (),
    joint_speed_frac: float = 1.0,
    allow_no_interpass: bool = False,
    point_queue: bool = False,
    allow_weld_splits: bool = False,
    platter: "PlatterState | None" = None,
    layers: tuple[int, int] | None = None,
    weld_ctwd_mm: float | None = None,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
) -> tuple[Ros2Job, dict]:


    if type(clean_first) is not bool:
        raise EmitRefused("argument", "clean_first must be a bool")
    if clean_first:
        raise EmitRefused(
            "argument",
            'Turn off "Clean wire (CLEAN_R1) before the first weld" for ROS 2 export; '
            "MotoROS2 cannot CALL the cleaner job.",
        )
    toolpath, violations, ctwd = _gate(
        toolpath, scene, torch, platter,
        bead_width_mm=bead_width_mm, bead_height_mm=bead_height_mm,
        margin_mm=margin_mm, interpass_timer_s=interpass_timer_s,
        allow_no_interpass=allow_no_interpass,
        layers_per_interpass=layers_per_interpass, weld_ctwd_mm=weld_ctwd_mm,
        clean_first=False)
    emitted, provenance = _layer_range(toolpath, layers, provenance)
    _interpass_gate(emitted, interpass_timer_s, allow_no_interpass, layers_per_interpass)
    weld_v = _weld_v(emitted, weld_v)
    per_move = weld_v == PER_MOVE
    planned = emitted
    if per_move:


        weld_v = None
        unit = LC_V_UNIT_MM_PER_MIN
        planned = replace(emitted, moves=[
            replace(m, feed=v_speed_mm_min(v_token(m.feed, unit), unit)) if m.deposit else m
            for m in emitted.moves])

    try:
        job = build_ros2_job(
            planned, solve, name=name, use_ir_feed=per_move,
            guard=point_guard(torch, scene, margin_mm),
            arc_io=arc_io, weld=weld,
            weld_v=weld_v, air_v=air_v, hover_mm=hover_mm,
            interpass_timer_s=interpass_timer_s,
            layers_per_interpass=layers_per_interpass, hold_axes=hold_axes,
            joint_speed_frac=joint_speed_frac,
            point_queue=point_queue, allow_weld_splits=allow_weld_splits,
            temp_check_every=temp_check_every, bead_dwell_s=bead_dwell_s, measure=measure,


            provenance=tuple(provenance) + _ctwd_lines(ctwd) + (platter.stamp(),),
        )
    except WireHangError as exc:
        raise EmitRefused("ctwd", str(exc)) from exc
    except ClearanceError as exc:
        raise EmitRefused("clearance", str(exc)) from exc
    except Ros2Refused as exc:
        raise EmitRefused(exc.reason, exc.detail) from exc
    except ValueError as exc:
        raise EmitRefused("argument", str(exc)) from exc


    parsed = json.loads(job.to_json())
    n_seg = sum(1 for s in parsed["steps"] if s["kind"] == "trajectory")
    if parsed["name"] != name or n_seg != job.stats["segments"]:
        raise EmitRefused(
            "roundtrip",
            f"name={parsed['name']!r} segments={n_seg} vs {job.stats['segments']}",
        )

    summary = dict(job.stats)
    summary["name"] = name
    summary["welding"] = job.welding
    summary["violations"] = len(violations)
    summary["deposit_mm"] = round(float(emitted.deposit_length()), 1)
    if layers is not None:
        summary["layers"] = list(layers)
    summary["platter"] = platter.stamp()
    try:
        summary["pass_table"] = pass_table(
            emitted, v_unit_mm_per_min=LC_V_UNIT_MM_PER_MIN,
            weld_v=weld_v or v_token(DEFAULT_WELD_MM_MIN, LC_V_UNIT_MM_PER_MIN))
    except ValueError as exc:
        raise EmitRefused("roundtrip", str(exc)) from exc
    summary.update(ctwd)
    return job, summary


def _layer_range(toolpath: Toolpath, layers, provenance):

    if layers is None:
        return toolpath, tuple(provenance)
    first, last = layers
    if type(first) is not int or type(last) is not int or not 0 <= first <= last:
        raise EmitRefused("argument", f"layers must be whole numbers first <= last, got {layers!r}")
    top = max(m.layer for m in toolpath.moves)
    if last > top:
        raise EmitRefused("argument", f"layer {last} does not exist; the part has layers 0 to {top}")
    try:
        emitted = toolpath.layer_range(first, last)
    except ValueError as exc:
        raise EmitRefused("argument", str(exc)) from exc
    return emitted, tuple(provenance) + (f"layers {first}-{last} of 0-{top}",)


def _validate(text: str, name: str, folder: str | None = None) -> dict:


    import tempfile

    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.JBI"
        probe.write_bytes(text.encode("ascii"))
        parsed = read_jbi(probe)
    n_c = sum(1 for k in parsed.positions if k.startswith("C"))
    want_folder = "" if folder in (None, "", "NONE") else folder
    if (parsed.name != name or parsed.groups != ["RB1"] or parsed.npos[0] != n_c
            or parsed.folder != want_folder):
        raise EmitRefused(
            "roundtrip",
            f"name={parsed.name!r} groups={parsed.groups} folder={parsed.folder!r} "
            f"npos={parsed.npos} vs {n_c} C-records",
        )
    return {
        "name": parsed.name,
        "positions": n_c,
        "passes": sum(1 for ln in parsed.instructions if ln.startswith("ARCON")),
    }


def write_job(text: str, path: Path) -> Path:


    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("ascii"))
    tmp.replace(path)
    return path
