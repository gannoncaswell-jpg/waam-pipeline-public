from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .arcs import segment_pass
from .robots.ar1440 import PULSES_PER_DEG
from .robots.hc10 import PULSES_PER_DEG as HC10_PULSES_PER_DEG
from .toolpath import Toolpath


PARK_PULSES = (0, -60000, -60000, 0, -90000, 0)


PARK_Q = tuple(p / r for p, r in zip(PARK_PULSES, PULSES_PER_DEG))


HC_PARK_PULSES = (-3000, 48000, -36000, 0, -95000, 0)


HC_PARK_Q = tuple(p / r for p, r in zip(HC_PARK_PULSES, HC10_PULSES_PER_DEG))


MAX_JOINT_STEP_DEG = 45.0


BEAD_DWELL_MIN_LIFT_MM = 5.0
READING_HOLD_S = 5.0


def bead_gaps(toolpath: Toolpath) -> tuple[set[int], int]:


    moves, lifted, skipped = toolpath.moves, set(), 0
    for i in range(1, len(moves)):
        if moves[i].deposit or not moves[i - 1].deposit or moves[i].layer != moves[i - 1].layer:
            continue
        j = next((k for k in range(i + 1, len(moves))
                  if moves[k].deposit or moves[k].layer != moves[i].layer), None)
        if j is None or not moves[j].deposit or moves[j].layer != moves[i].layer:
            continue
        if moves[i].z - moves[i - 1].z >= BEAD_DWELL_MIN_LIFT_MM - 1e-9:
            lifted.add(i)
        else:
            skipped += 1
    return lifted, skipped


def bead_dwell_note(toolpath: Toolpath, bead_dwell_s: float) -> str | None:

    if not bead_dwell_s:
        return None
    skipped = bead_gaps(toolpath)[1]
    if not skipped:
        return None
    return (f"between-bead dwell skipped at {skipped} gap{'s' if skipped != 1 else ''}: "
            f"the travel after the pass does not lift {BEAD_DWELL_MIN_LIFT_MM:g} mm")


@dataclass(frozen=True)
class MeasureStop:


    every: int
    readings_s: tuple[float, ...] = (20.0, 40.0, 60.0)
    pause: bool = True
    clean: bool = False
    pre_weld: bool = True

    def __post_init__(self) -> None:
        if type(self.every) is not int or self.every < 1:
            raise ValueError("measure.every must be a whole number >= 1")
        if not isinstance(self.readings_s, tuple):
            raise ValueError("measure.readings_s must be a tuple of increasing times")
        previous = 0.0
        for t in self.readings_s:
            if (isinstance(t, bool) or not isinstance(t, (int, float))
                    or not 1 <= t <= 3600 or not math.isfinite(t)):
                raise ValueError("measure.readings_s times must be finite and within 1..3600 s")
            if t <= previous:
                raise ValueError("measure.readings_s must be strictly increasing")
            if not math.isclose(t * 100, round(t * 100), rel_tol=0, abs_tol=1e-7):
                raise ValueError("measure.readings_s must use TIMER resolution (0.01 s)")
            previous = t
        for name in ("pause", "clean", "pre_weld"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"measure.{name} must be a bool")


@dataclass(frozen=True)
class Step:


    kind: str
    role: str
    q: tuple[float, ...] | None = None
    xyz: tuple[float, float, float] | None = None
    seconds: float | None = None
    feed_mm_min: float | None = None
    move_index: int | None = None
    layer: int | None = None
    label: str = ""
    interp: str = "linear"
    fpt: bool = False
    pl: int = 0
    measurement: MeasureStop | None = None
    measurement_final: bool = False

    @property
    def is_motion(self) -> bool:
        return self.kind in ("joint", "linear")


ROLES = ("park", "approach", "hover", "descend", "weld", "air", "retreat",
         "settle", "interpass", "bead_dwell", "temp_check",
         "measure_reading", "measure_pause", "clean", "pre_weld")


def build_plan(
    toolpath: Toolpath,
    solve,
    *,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    settle_s: float = 2.0,
    guard=None,
    park_q: tuple[float, ...] = PARK_Q,
    max_joint_step_deg: float = MAX_JOINT_STEP_DEG,
    smooth: bool = False,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    clean_first: bool = False,
) -> list[Step]:


    if hover_mm <= 0:
        raise ValueError(
            f"hover_mm must be positive (it is a RETREAT height), got {hover_mm}"
        )
    if type(layers_per_interpass) is not int or layers_per_interpass < 1:
        raise ValueError(
            f"layers_per_interpass must be a whole number >= 1, got {layers_per_interpass!r}"
        )
    if type(temp_check_every) is not int or temp_check_every < 0:
        raise ValueError(f"temp_check_every must be a whole number >= 0, got {temp_check_every!r}")
    if measure is not None and not isinstance(measure, MeasureStop):
        raise ValueError("measure must be a MeasureStop or None")
    if type(clean_first) is not bool:
        raise ValueError("clean_first must be a bool")
    if not (isinstance(bead_dwell_s, (int, float)) and math.isfinite(bead_dwell_s)
            and bead_dwell_s >= 0):
        raise ValueError(f"bead_dwell_s must be finite and >= 0, got {bead_dwell_s!r}")
    if not toolpath.moves:
        raise ValueError("empty toolpath: nothing to emit")


    drop = max((m.ctwd_lift for m in toolpath.moves), default=0.0)

    welded = False

    def check(xyz, where: str) -> None:
        if guard is not None:
            if drop and (not clean_first or welded):
                guard(xyz, where, wire_drop_mm=drop)
            else:
                guard(xyz, where)

    events: dict[int, list[str]] = {}
    for kind, idx in toolpath.arc_events():
        events.setdefault(idx, []).append(kind)

    motion: dict[int, tuple[str, bool, int]] = {}
    separators: set[int] = set()
    if smooth:
        for start, kinds in events.items():
            if start == 0 or "ARCON" not in kinds:
                continue


            stop = next(i for i in range(start + 1, len(toolpath.moves) + 1)
                        if "ARCOFF" in events.get(i, [])
                        or (i < len(toolpath.moves)
                            and toolpath.moves[i].layer != toolpath.moves[start].layer))
            points = [(m.x, m.y, m.z) for m in toolpath.moves[start - 1:stop]]
            fitted = segment_pass(points)
            for j in range(1, len(points)):
                motion[start + j - 1] = (fitted.interp[j], fitted.fpt[j], fitted.pl[j])
            separators.update(start + j - 1 for j in fitted.separators)

    bead_breaks = bead_gaps(toolpath)[0] if bead_dwell_s > 0 else set()

    steps: list[Step] = [Step("joint", "park", q=tuple(park_q), label="park")]
    if clean_first and any(move.deposit for move in toolpath.moves):
        steps.append(Step("call", "clean", label="CLEAN_R1"))
        steps.append(Step("joint", "park", q=tuple(park_q), label="park"))

    q_prev: tuple[float, ...] | None = None
    p_prev: tuple[float, float, float] | None = None
    lift_prev = 0.0
    arc_lit = False
    layer_changes = 0
    prev_layer: int | None = None
    layers_done = 0

    def step_ok(q, where: str) -> None:
        if q_prev is None:
            return
        worst = max(abs(a - b) for a, b in zip(q_prev, q))
        if worst > max_joint_step_deg:
            raise ValueError(
                f"IK branch discontinuity at {where}: {worst:.1f} deg jump "
                "between adjacent linear moves"
            )

    def measure_controls(config: MeasureStop, layer: int, *, final: bool = False) -> None:
        previous_tick = 0
        for t in config.readings_s:
            tick = round(t * 100)
            if tick <= previous_tick:
                raise ValueError("measure.readings_s must remain strictly increasing "
                                 "at TIMER resolution (0.01 s)")
            steps.append(Step("dwell", "measure_reading",
                              seconds=(tick - previous_tick) / 100, layer=layer))
            seconds = f"{tick / 100:.2f}".rstrip("0").rstrip(".")
            steps.append(Step("message", "measure_reading", layer=layer,
                              label=f"TEMP {seconds.replace('.', ' POINT ')} S AFTER WELD"))
            previous_tick = tick
        if config.readings_s:
            steps.append(Step("dwell", "measure_reading", seconds=READING_HOLD_S, layer=layer))
            previous_tick += round(READING_HOLD_S * 100)
        steps.append(Step("message", "measure_reading", layer=layer))
        if config.pause:
            steps.append(Step("pause", "measure_pause", layer=layer))
        if config.clean:
            steps.append(Step("call", "clean", layer=layer, label="CLEAN_R1"))
            steps.append(Step("joint", "park", q=tuple(park_q), label="park"))
        if not final:
            remaining = max(0, round(interpass_timer_s * 100) - previous_tick) / 100
            if remaining:
                steps.append(Step("dwell", "interpass", seconds=remaining, layer=layer))
            if config.pre_weld:
                steps.append(Step("pause", "pre_weld", layer=layer))

    for i, move in enumerate(toolpath.moves):
        if "ARCOFF" in events.get(i, []):
            steps.append(Step("arc_off", "air", move_index=i, layer=move.layer))
            arc_lit = False


        lit = i > 0 and move.deposit and toolpath.moves[i - 1].deposit
        boundary = prev_layer is None or (move.layer != prev_layer and not lit)
        if prev_layer is not None and move.layer != prev_layer:
            layer_changes += 1
            if lit and measure is not None and layer_changes % measure.every == 0:
                raise ValueError(
                    f"layer {prev_layer} runs into layer {move.layer} with the arc lit "
                    f"(move {i}), so the measurement stop due there cannot pause. Add a "
                    "travel between layers or change the measurement cadence"
                )


            if lit and temp_check_every and layer_changes % temp_check_every == 0:
                raise ValueError(
                    f"layer {prev_layer} runs into layer {move.layer} with the arc lit "
                    f"(move {i}), so the temperature check due there cannot pause. Add a "
                    "travel between layers or change the check cadence"
                )


        if boundary:


            if move.deposit:
                if p_prev is None:
                    raise ValueError(
                        "the toolpath's first move deposits, so the IR does not "
                        "say where that weld begins and the arc could only be "
                        "struck at the park posture. Prepend a travel move to "
                        "the weld start."
                    )
                start, lift = p_prev, lift_prev
            else:
                start, lift = (move.x, move.y, move.z), move.ctwd_lift


            if welded:
                lift = 0.0
            elif lift >= hover_mm:
                raise ValueError(f"the weld CTWD raise ({lift:g} mm) reaches the "
                                 f"{hover_mm:g} mm hover; raise the hover")
            hover = (start[0], start[1], start[2] - lift + hover_mm)
            where = f"hover before layer {move.layer}"
            check(hover, where)
            q_h = solve(hover)
            if q_prev is None:
                steps.append(Step("joint", "approach", q=tuple(q_h), xyz=hover,
                                  layer=move.layer, label=where))
                if settle_s > 0:
                    steps.append(Step("dwell", "settle", seconds=settle_s,
                                      layer=move.layer))
            else:
                layers_done += 1
                measuring = measure is not None and layer_changes % measure.every == 0
                dwell = interpass_timer_s if layers_done % layers_per_interpass == 0 else 0.0
                if measuring:
                    if arc_lit:
                        raise ValueError(f"a measurement stop before layer {move.layer} "
                                         "would pause with the arc lit")
                    retreat = (p_prev[0], p_prev[1],
                               p_prev[2] - (0.0 if welded else lift_prev) + hover_mm)
                    retreat_where = f"measurement retreat after layer {prev_layer}"
                    check(retreat, retreat_where)
                    q_r = solve(retreat)
                    step_ok(q_r, retreat_where)
                    steps.append(Step("linear", "retreat", q=tuple(q_r), xyz=retreat,
                                      layer=prev_layer, label=retreat_where, measurement=measure))
                    steps.append(Step("joint", "park", q=tuple(park_q), label="park"))
                    measure_controls(measure, move.layer)
                    steps.append(Step("joint", "approach", q=tuple(q_h), xyz=hover,
                                      layer=move.layer, label=where))
                    q_prev = tuple(q_h)
                step_ok(q_h, where)
                steps.append(Step("linear", "hover", q=tuple(q_h), xyz=hover,
                                  layer=move.layer, label=where))
                if not measuring and dwell > 0:
                    steps.append(Step("dwell", "interpass",
                                      seconds=interpass_timer_s, layer=move.layer))
                if not measuring and temp_check_every and layer_changes % temp_check_every == 0:
                    if arc_lit:
                        raise ValueError(f"a temperature check before layer {move.layer} "
                                         "would pause with the arc lit")
                    steps.append(Step("pause", "temp_check", layer=move.layer))
            q_prev = tuple(q_h)
            if move.deposit:


                where = f"descend to layer {move.layer} start"
                check(start, where)
                q_s = solve(start)
                step_ok(q_s, where)
                steps.append(Step("linear", "descend", q=tuple(q_s), xyz=start,
                                  layer=move.layer, label=where))
                q_prev = tuple(q_s)
        prev_layer = move.layer
        if i in separators:
            previous = toolpath.moves[i - 1]
            separator = replace(move, x=(previous.x + move.x) / 2,
                                y=(previous.y + move.y) / 2,
                                z=(previous.z + move.z) / 2,
                                seat=(previous.seat + move.seat) / 2)
            midpoint = (separator.x, separator.y, separator.z)
            where = f"separator before move {i} (layer {move.layer})"
            check(midpoint, where)
            q_s = solve(midpoint)
            step_ok(q_s, where)
            steps.append(Step("linear", "weld", q=tuple(q_s), xyz=midpoint,
                              feed_mm_min=separator.feed, move_index=i,
                              layer=separator.layer, label=where))
            q_prev = tuple(q_s)
        target = (move.x, move.y, move.z)
        q = solve(target)
        step_ok(q, f"move {i} (layer {move.layer})")
        q_prev = tuple(q)
        p_prev = target
        lift_prev = move.ctwd_lift
        welded = welded or move.deposit
        if "ARCON" in events.get(i, []):
            steps.append(Step("arc_on", "weld", move_index=i, layer=move.layer))
            arc_lit = True
        interp, fpt, pl = motion.get(i, ("linear", False, 0))
        steps.append(Step(
            "linear", "weld" if move.deposit else "air", q=tuple(q), xyz=target,
            feed_mm_min=move.feed, move_index=i, layer=move.layer,
            label=f"move {i} (layer {move.layer})",
            interp=interp, fpt=fpt, pl=pl,
        ))
        if i in bead_breaks:
            if arc_lit:
                raise ValueError(f"a between-bead dwell at move {i} would wait with the arc lit")
            steps.append(Step("dwell", "bead_dwell", seconds=float(bead_dwell_s),
                              layer=move.layer))

    if "ARCOFF" in events.get(len(toolpath.moves), []):
        steps.append(Step("arc_off", "air", move_index=len(toolpath.moves)))

    last = toolpath.moves[-1]
    retreat = (last.x, last.y, last.z - (0.0 if welded else last.ctwd_lift) + hover_mm)
    check(retreat, "final retreat")
    q_r = solve(retreat)
    step_ok(q_r, "final retreat")
    final_measure = (measure if welded and measure is not None
                     and (layer_changes + 1) % measure.every == 0 else None)
    steps.append(Step("linear", "retreat", q=tuple(q_r), xyz=retreat,
                      layer=last.layer, label="final retreat", measurement=final_measure,
                      measurement_final=final_measure is not None))
    steps.append(Step("joint", "park", q=tuple(park_q), label="park"))
    if final_measure is not None:
        measure_controls(final_measure, last.layer, final=True)
    return steps
