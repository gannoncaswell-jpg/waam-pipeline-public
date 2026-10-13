from __future__ import annotations

import math
from dataclasses import replace

from .toolpath import Toolpath

MM_PER_IN = 25.4

MAX_WELD_CTWD_MM = 40.0

NOTE_ABOVE_MM = 31.75


def default_mm() -> float:

    from . import cell_lc

    value = cell_lc.CALIBRATION.get("touch_off", {}).get("weld_stickout_mm")
    if value is None:
        raise ValueError("touch_off.weld_stickout_mm is unset in docs/cell-calibration.json, "
                         "so there is no calibrated CTWD to raise from")
    return float(value)


def resolve(weld_ctwd_mm: float | None) -> tuple[float, float, list[str]]:


    default = default_mm()
    if weld_ctwd_mm is None:
        return default, 0.0, []
    if isinstance(weld_ctwd_mm, bool):
        raise ValueError("weld CTWD must be a number of millimetres")
    try:
        value = float(weld_ctwd_mm)
    except (TypeError, ValueError):
        raise ValueError("weld CTWD must be a number of millimetres") from None
    if not math.isfinite(value):
        raise ValueError("weld CTWD must be finite")
    if value < default - 1e-9:
        raise ValueError(f"Weld CTWD {value:g} mm is below the calibrated {default:g} mm "
                         f"({default / MM_PER_IN:.3f} in). Seating assumes at least that "
                         "stick-out, so a shorter CTWD drives the wire into the plate")
    if value > MAX_WELD_CTWD_MM:
        raise ValueError(f"Weld CTWD {value:g} mm is above the {MAX_WELD_CTWD_MM:g} mm limit")
    delta = value - default
    if abs(delta) <= 1e-9:
        return default, 0.0, []
    notes = []
    if value > NOTE_ABOVE_MM:
        notes.append(f"Weld CTWD {value:g} mm ({value / MM_PER_IN:.3f} in) is above 1.25 in, "
                     "the top of the R450 pulse adaptive range")
    return value, delta, notes


def weld_vertices(toolpath: Toolpath) -> list[bool]:

    moves = toolpath.moves
    return [m.deposit or (i + 1 < len(moves) and moves[i + 1].deposit)
            for i, m in enumerate(moves)]


def apply(toolpath: Toolpath, weld_ctwd_mm: float | None) -> tuple[Toolpath, float, float, list[str]]:


    if any(m.ctwd_lift for m in toolpath.moves):
        raise ValueError("this toolpath already carries a weld CTWD raise; apply it once, "
                         "to the seated toolpath")
    value, delta, notes = resolve(weld_ctwd_mm)
    weld = weld_vertices(toolpath)
    if not delta or not any(weld):
        return toolpath, default_mm(), 0.0, []
    first = weld.index(True)
    moves = [replace(m, z=m.z + delta, ctwd_lift=delta) if i >= first else m
             for i, m in enumerate(toolpath.moves)]
    return replace(toolpath, moves=moves), value, delta, notes


def checked(toolpath: Toolpath, weld_ctwd_mm: float | None, *, clean_first: bool = False,
            **check_kwargs):


    from .clearance import blocking, check

    if type(clean_first) is not bool:
        raise ValueError("clean_first must be a bool")
    raised, value, delta, notes = apply(toolpath, weld_ctwd_mm)
    if not delta:
        return raised, value, 0.0, check(raised, **check_kwargs), [], notes
    first = weld_vertices(raised).index(True)
    entry_drop = 0.0 if clean_first else delta
    violations = check(raised, entry_wire_drop_mm=entry_drop,
                       entry_wire_through_move=first, **check_kwargs)
    deposit = next(i for i, m in enumerate(raised.moves) if m.deposit)
    hits = [(v, v.move_index <= first) for v in blocking(violations)
            if not raised.moves[v.move_index].deposit
            and (v.move_index <= first or v.move_index > deposit)]
    return raised, value, delta, violations, hits, notes


def hang_refusal(hits, delta: float) -> str:

    worst, before = min(hits, key=lambda h: h[0].clearance_mm)
    where = (f"travel move {worst.move_index} puts it {worst.clearance_mm:.1f} mm from "
             f"{worst.obstacle}")
    if before:
        return (f"the wire may still hang up to {delta:.2f} mm longer than the default from a "
                f"previous raised-CTWD weld, and before the first weld {where}. Enable "
                '"Clean wire before the first weld", or add travel lift')
    return (f"after a weld the wire hangs up to {delta:.2f} mm longer than the default "
            f"until CLEAN_R1 cuts it, and {where}. Add travel lift")


def stamp(weld_ctwd_mm: float) -> str:

    return f"WELD CTWD {weld_ctwd_mm:.2f}MM {weld_ctwd_mm / MM_PER_IN:.3f}IN"
