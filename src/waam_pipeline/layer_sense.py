from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from . import cell_lc
from .substrate import MAX_TILT_DEG
from .toolpath import Move, Toolpath, single_part
from .touch import SEARCH_OVERTRAVEL_MM, Probe
from .var_dat import FLAG_BASE, MAX_POINTS, SENSE_BASE

SPACING_MM = 10.0

LEGACY_BASE = 44
LEGACY_MAX_POINTS = 21
MAX_MISSES_IN_A_ROW = 3
MIN_HIT_RATE = 0.6
MIN_PASS_HITS = 2
HOP_MM = 5.0


RESEARCH_MODE = True


def mode_provenance() -> tuple[str, ...]:

    return ("RESEARCH SENSE 5MM WARN ONLY",) if RESEARCH_MODE else ()


@dataclass(frozen=True)
class LayerProbe(Probe):
    seat: float = 0.0
    pass_index: int = 0
    arc_mm: float = 0.0


def _passes(toolpath: Toolpath, layer: int):

    passes, run, prev = [], [], None
    for i, move in enumerate(toolpath.moves):
        if move.deposit and move.layer == layer and prev is not None:
            run.append((prev, move, i))
        elif run:
            passes.append(run)
            run = []
        prev = move
    if run:
        passes.append(run)
    return passes


def _chords(run):

    a = np.array([[p.x, p.y] for p, _, _ in run])
    b = np.array([[m.x, m.y] for _, m, _ in run])
    lengths = np.linalg.norm(b - a, axis=1)
    return a, b, np.r_[0.0, np.cumsum(lengths)[:-1]], lengths


def _at(run, s):

    a, b, s0, lengths = _chords(run)
    k = int(np.clip(np.searchsorted(s0, s, side="right") - 1, 0, len(run) - 1))
    t = 0.0 if lengths[k] < 1e-9 else float(np.clip((s - s0[k]) / lengths[k], 0, 1))
    start, end, _ = run[k]
    p0, p1 = np.array(start.target), np.array(end.target)
    return p0 + t * (p1 - p0), start.seat + t * (end.seat - start.seat)


def _stations(passes, spacing, end_clear, max_points):
    lengths = [float(_chords(run)[3].sum()) for run in passes]
    sensed = sum(total >= 2 * end_clear for total in lengths)
    if not sensed:
        raise ValueError("No weld pass on this layer is long enough to touch clear of its arc start and crater")
    if sensed > max_points:
        raise ValueError(f"This layer has {sensed} weld passes; one sense job covers at most {max_points} touches")
    while True:
        stations = []
        for p, total in enumerate(lengths):
            usable = total - 2 * end_clear
            if usable < 0:
                continue
            n = int(np.floor(usable / spacing + 1e-9)) + 1
            first = end_clear + (usable - (n - 1) * spacing) / 2
            stations += [(p, first + j * spacing) for j in range(n)]
        if len(stations) <= max_points:
            return stations
        spacing *= 1.1


def _positive(name, value, *, zero=False):
    if (type(value) not in (int, float) or not np.isfinite(value)
            or value < 0 or (value == 0 and not zero)):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return float(value)


def layer_sense_plan(toolpath: Toolpath, layer: int, *, bead_height_mm: float,
                     spacing_mm: float = SPACING_MM, end_clear_mm: float = 10.0,
                     approach_mm: float = 25.0, overtravel_mm: float | None = None,
                     max_points: int = MAX_POINTS) -> list[LayerProbe]:


    single_part(toolpath, "Layer sensing")
    h = _positive("bead_height_mm", bead_height_mm)
    spacing = _positive("spacing_mm", spacing_mm)
    end_clear = _positive("end_clear_mm", end_clear_mm, zero=True)
    approach = _positive("approach_mm", approach_mm)
    if overtravel_mm is not None:
        overtravel = _positive("overtravel_mm", overtravel_mm)
    elif RESEARCH_MODE:
        overtravel = SEARCH_OVERTRAVEL_MM
    else:
        overtravel = float(np.floor(7.5 * h + 1e-6)) / 10
    if RESEARCH_MODE:


        if overtravel > SEARCH_OVERTRAVEL_MM:
            raise ValueError(f"The search window below the predicted top ({overtravel:g} mm) must be at most "
                             f"{SEARCH_OVERTRAVEL_MM:g} mm, under the CLEAN_R1 stick-out")
    elif overtravel >= h:
        raise ValueError(f"The search window below the predicted top ({overtravel:g} mm) must be under one bead height ({h:g} mm)")
    if type(max_points) is not int or not 1 <= max_points <= MAX_POINTS:
        raise ValueError(f"max_points must be a whole number from 1 to {MAX_POINTS} "
                         f"(P{SENSE_BASE:03d}-P{SENSE_BASE + MAX_POINTS - 1:03d})")
    if type(layer) is not int:
        raise ValueError("Choose the layer to sense by its whole-number index")
    passes = _passes(toolpath, layer)
    if not passes:
        raise ValueError(f"Layer {layer} has no weld moves to sense")
    if not any(move.layer > layer for move in toolpath.moves):
        raise ValueError(f"Layer {layer} is the last layer: there is nothing above it to re-seat")
    probes = []
    for i, (p, s) in enumerate(_stations(passes, spacing, end_clear, max_points)):
        point, seat = _at(passes[p], s)
        expected = point + [0.0, 0.0, h]
        probes.append(LayerProbe(name=f"layer{layer}_{i:02d}", expected=expected,
                                 start=expected + [0.0, 0.0, approach],
                                 target=expected - [0.0, 0.0, overtravel],
                                 seat=float(seat), pass_index=p, arc_mm=float(s)))
    return probes


def _auto_range(first, last):
    if type(first) is not int or type(last) is not int or not 0 <= first <= last:
        raise ValueError("Auto cycle layers must be whole numbers with 0 <= first <= last")
    return last - first + 1


def auto_plan(toolpath: Toolpath, first: int, last: int, *, step_mm: float,
              spacing_mm: float, max_points: int = MAX_POINTS) -> tuple[list[LayerProbe], float]:

    single_part(toolpath, "Auto planning")
    n = _auto_range(first, last)
    step = _positive("step_mm", step_mm)
    hop = max(HOP_MM, 0.5 * n * step)
    plan = layer_sense_plan(toolpath, last, bead_height_mm=step, spacing_mm=spacing_mm,
                            approach_mm=max(25.0, hop + 10.0), max_points=max_points)
    return plan, hop


def apply_auto(toolpath: Toolpath, top: LayerTop, *, first: int, last: int,
               step_mm: float, bead_height_mm: float) -> tuple[Toolpath, dict]:

    single_part(toolpath, "Auto correction")
    n = _auto_range(first, last)
    step = _positive("step_mm", step_mm)
    h = _positive("bead_height_mm", bead_height_mm)
    if top.layer != last:
        raise ValueError("The measured top must be the Auto cycle's last layer")
    seat = top.errors
    if top.raw_measured is not None:


        trusted = seat[np.abs(seat - top.raw_errors) <= 1e-9]
        seat = trusted if trusted.size else np.array([seat.max()])
    e = float(np.mean(seat))
    if not np.isfinite(e):
        raise ValueError("Auto cycle top errors must be finite")
    raw = step + e / n
    warnings = list(top.warnings)
    if not 0.4 * h <= raw <= 1.6 * h:
        text = f"the part grew {raw:.3f} mm per layer, {100 * raw / h:.1f} % of the bead height"
        if not RESEARCH_MODE:
            raise ValueError(f"{text}: stop and inspect the part")
        warnings.append(f"T{text[1:]}.")


    growth = float(max(raw, 0.6 * h) if RESEARCH_MODE else np.clip(raw, 0.6 * h, 1.4 * h))
    seated = top.apply(toolpath)
    moves = []
    for move in seated.moves:
        dz = (move.layer - last - 1) * (growth - step) if move.layer > last else 0.0
        moves.append(replace(move, z=move.z + dz, seat=move.seat + dz))
    info = dict(top.metadata(), cycle=None, layers=[first, last], n=n, step_mm=step,
                growth_raw_mm=raw, growth_mm=growth, clamped=growth != raw,
                mean_error_mm=e, warnings=warnings)
    return replace(toolpath, moves=moves), info


def sense_motions(toolpath: Toolpath, layer: int, plan: list[LayerProbe], *,
                  hop_mm: float | None = None) -> tuple[Toolpath, int]:


    last = max(i for i, move in enumerate(toolpath.moves) if move.layer <= layer)
    moves = list(toolpath.moves[:last + 1])
    begin = len(moves)
    line = max(move.line_no for move in toolpath.moves) + 1
    end = moves[-1]

    def travel(p, seat):
        return Move(float(p[0]), float(p[1]), float(p[2]), None, False, layer, line,
                    seat=float(seat), copy=end.copy)

    lift = max(float(plan[0].start[2]), end.z)
    moves.append(travel((end.x, end.y, lift), end.seat))
    if hop_mm is None:
        for probe in plan:
            moves += [travel(probe.start, probe.seat), travel(probe.expected, probe.seat),
                      travel(probe.start, probe.seat)]
    else:
        moves.append(travel(plan[0].start, plan[0].seat))
        for probe in plan:
            hop = probe.expected + [0.0, 0.0, hop_mm]
            moves += [travel(hop, probe.seat), travel(probe.expected, probe.seat), travel(hop, probe.seat)]
        moves.append(travel(plan[-1].start, plan[-1].seat))
    return replace(toolpath, moves=moves), begin


def _pulse_point(record, label):
    if (not isinstance(record, list) or len(record) != 6 or
            any(type(v) not in (float, int) or not np.isfinite(v) or float(v) != int(v) for v in record)):
        raise ValueError(f"{label}: enter six whole PULSE values in S, L, U, R, B, T order")
    try:
        return cell_lc.tcp_from_pulses(record)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


@dataclass(frozen=True)
class LayerTop:


    layer: int
    stations: tuple
    station_xy: np.ndarray
    predicted: np.ndarray
    measured: np.ndarray
    labels: tuple[str, ...]
    passes: tuple
    reach_mm: float
    warnings: tuple = ()
    raw_measured: np.ndarray | None = None
    missed: tuple[str, ...] = ()
    blind: frozenset = frozenset()

    @property
    def errors(self) -> np.ndarray:

        return self.measured - self.predicted

    @property
    def raw_errors(self) -> np.ndarray:

        return self.errors if self.raw_measured is None else self.raw_measured - self.predicted

    def _project(self, xy):

        best = (np.inf, 0, 0.0)
        for p, (a, b, s0, lengths) in enumerate(self.passes):
            ab = b - a
            t = np.clip(((xy - a) * ab).sum(1) / np.maximum(lengths ** 2, 1e-12), 0, 1)
            d = np.linalg.norm(a + t[:, None] * ab - xy, axis=1)
            k = int(np.argmin(d))
            if d[k] < best[0]:
                best = (float(d[k]), p, float(s0[k] + t[k] * lengths[k]))
        return best

    def error_at(self, xy):


        xy = np.asarray(xy, float)
        distance, p, s = self._project(xy)
        if p in self.blind:
            nearest = int(np.argmin(np.linalg.norm(self.station_xy - xy, axis=1)))
            return max(0.0, float(self.errors[nearest])), distance
        mine = [i for i, (q, _) in enumerate(self.stations) if q == p]
        if mine:
            arcs = np.array([self.stations[i][1] for i in mine])
            return float(np.interp(s, arcs, self.errors[mine])), distance
        nearest = int(np.argmin(np.linalg.norm(self.station_xy - xy, axis=1)))
        return float(self.errors[nearest]), distance

    def apply(self, toolpath: Toolpath) -> Toolpath:


        stations = self.station_xy
        moves, prev = [], None
        for move in toolpath.moves:
            if move.layer <= self.layer:
                moves.append(move)
                prev = move
                continue
            end = np.array(move.target)
            splits = [1.0]
            if move.deposit and prev is not None:
                start = np.array(prev.target)
                ab = end[:2] - start[:2]
                length = float(ab @ ab)
                if length > 1e-12:
                    t = (stations - start[:2]) @ ab / length
                    near = np.linalg.norm(start[:2] + t[:, None] * ab - stations, axis=1) <= self.reach_mm
                    splits = sorted({float(v) for v in t[near & (t > 1e-6) & (t < 1 - 1e-6)]} | {1.0})
            for t in splits:
                if prev is None or t == 1.0:
                    point, seat = end, move.seat
                else:
                    start = np.array(prev.target)
                    point, seat = start + t * (end - start), prev.seat + t * (move.seat - prev.seat)
                e, distance = self.error_at(point[:2])
                if move.deposit and distance > self.reach_mm:
                    raise ValueError(
                        f"Layer {move.layer} welds {distance:.1f} mm from layer {self.layer}'s sensed path "
                        f"at ({point[0]:.1f}, {point[1]:.1f}); its top was not measured there")
                moves.append(replace(move, x=float(point[0]), y=float(point[1]),
                                     z=float(point[2] + e), seat=float(seat + e)))
            prev = move
        return replace(toolpath, moves=moves)

    def metadata(self) -> dict:
        e = self.raw_errors
        worst = int(np.argmax(np.abs(e)))
        planned = len(e) + len(self.missed)

        return {"layer": self.layer, "points": len(e), "touched": len(e), "planned": planned,
                "missed": list(self.missed), "hit_rate": len(e) / planned,
                "mean_error_mm": float(e.mean()),
                "error_range_mm": [float(e.min()), float(e.max())], "worst": self.labels[worst],
                "worst_error_mm": float(e[worst]), "errors_mm": [float(v) for v in e],
                "seat_errors_mm": [float(v) for v in self.errors],
                "labels": list(self.labels), "warnings": list(self.warnings)}


def _hit_rules(layer, plan, hit, base, warnings):


    hit = list(hit)
    label = [f"P{base + i:03d}" for i in range(len(plan))]
    problems, run = [], 0
    for i, ok in enumerate(hit):
        run = 0 if ok else run + 1
        if run >= MAX_MISSES_IN_A_ROW and i < len(hit) - 1:
            problems.append(f"The touch job stopped after {run} misses in a row at {label[i]} (alarm 8814); "
                            f"{label[i + 1]}-{label[-1]} were not touched and are left out.")
            hit[i + 1:] = [False] * (len(hit) - i - 1)
            break
    if not any(hit):
        raise ValueError(f"Layer {layer}: no station touched. Nothing was measured; check the wire-touch "
                         "sensor and that this VAR.DAT is from this touch job.")
    n_hit = sum(hit)
    if n_hit < MIN_HIT_RATE * len(hit) - 1e-9:
        problems.append(f"Layer {layer}: {n_hit} of {len(hit)} stations touched ({100 * n_hit / len(hit):.0f} %); "
                        f"at least {100 * MIN_HIT_RATE:.0f} % must touch.")
    for p in sorted({probe.pass_index for probe in plan}):
        mine = [i for i, probe in enumerate(plan) if probe.pass_index == p]
        got = sum(hit[i] for i in mine)
        need = min(MIN_PASS_HITS, len(mine))
        if got < need:
            seat = ("it uses the nearest touched station without lowering the model" if not got
                    else "it is seated flat on its one touch")
            problems.append(f"Layer {layer} pass {p + 1}: {got} of {len(mine)} stations touched, "
                            f"at least {need} needed; {seat}.")
        run = []
        for i in mine + [None]:
            if i is not None and not hit[i]:
                run.append(i)
                continue
            if len(run) >= MAX_MISSES_IN_A_ROW:
                problems.append(f"Layer {layer} pass {p + 1}: {label[run[0]]}-{label[run[-1]]} missed, "
                                f"a gap of more than {MAX_MISSES_IN_A_ROW} station spacings.")
            run = []
    if problems and not RESEARCH_MODE:
        raise ValueError(" ".join(problems) + " Clean the layer and touch it again, or inspect the part.")
    warnings += problems
    return hit


def fit_layer_top(toolpath: Toolpath, layer: int, plan: list[LayerProbe], records, *,
                  origin, bead_height_mm: float, base: int = SENSE_BASE, hits=None,
                  max_error_mm: float | None = None, xy_tolerance_mm: float = 1.0,
                  reach_mm: float = 5.0) -> LayerTop:


    if not isinstance(records, list) or len(records) != len(plan):
        raise ValueError(f"Layer {layer} sense: expected {len(plan)} touch records, one per planned station")
    if type(base) is not int or not 0 <= base <= 250 - len(plan):
        raise ValueError("The sense results must fit in P000-P249")
    origin = np.asarray(origin, float)
    h = _positive("bead_height_mm", bead_height_mm)
    limit = h if max_error_mm is None else _positive("max_error_mm", max_error_mm)
    warnings = []
    if hits is None:
        hit = [True] * len(plan)
    elif (not isinstance(hits, list) or len(hits) != len(plan)
          or any(type(v) not in (bool, int) or v not in (0, 1) for v in hits)):
        raise ValueError(f"Layer {layer} sense: expected {len(plan)} hit flags (0 or 1), one per planned station")
    else:
        hit = _hit_rules(layer, plan, [bool(v) for v in hits], base, warnings)
    used = [i for i in range(len(plan)) if hit[i]]
    measured, labels = [], []
    for i in used:
        probe, record = plan[i], records[i]
        label = f"P{base + i:03d}"
        point = _pulse_point(record, label) - origin
        off = float(np.linalg.norm(point[:2] - probe.expected[:2]))
        if off > xy_tolerance_mm:
            raise ValueError(f"{label} touched {off:.1f} mm from its planned station on layer {layer}. "
                             "This export is not from this sense job, or the part was moved.")
        if not probe.target[2] - 0.5 <= point[2] <= probe.start[2] + 0.5:
            raise ValueError(f"{label} is outside its search window on layer {layer}. Recheck the export.")
        error = point[2] - probe.expected[2]
        if abs(error) > limit:
            text = f"{label}: the top of layer {layer} is {error:+.2f} mm off the model (limit {limit:g} mm)."
            if not RESEARCH_MODE:
                raise ValueError(f"{text} Stop and inspect the part.")
            if error < -h:
                text += " It may have found the layer below."
            warnings.append(text)
        measured.append(point[2])
        labels.append(label)
    passes = tuple(_chords(run) for run in _passes(toolpath, layer))
    fitted = [plan[i] for i in used]
    top = LayerTop(layer=layer, stations=tuple((p.pass_index, p.arc_mm) for p in fitted),
                   station_xy=np.array([p.expected[:2] for p in fitted], float),
                   predicted=np.array([p.expected[2] for p in fitted]), measured=np.array(measured),
                   labels=tuple(labels), passes=passes, reach_mm=float(reach_mm),
                   missed=tuple(f"P{base + i:03d}" for i in range(len(plan)) if not hit[i]),
                   blind=frozenset({p.pass_index for p in plan} - {p.pass_index for p in fitted}))
    slope = np.tan(np.radians(MAX_TILT_DEG))
    for i in range(len(fitted) - 1):
        (p, s), (q, t) = top.stations[i], top.stations[i + 1]
        if p == q and t > s:
            step = abs(top.errors[i + 1] - top.errors[i])
            if step > slope * (t - s) + 1e-9:
                text = (f"The top of layer {layer} steps {step:.2f} mm between {labels[i]} and "
                        f"{labels[i + 1]} ({t - s:.1f} mm apart).")
                if not RESEARCH_MODE:
                    raise ValueError(f"{text} Stop and inspect the part.")
                warnings.append(text)
    if RESEARCH_MODE:
        top = _safe_seat(top, h, slope, warnings)
    return replace(top, warnings=tuple(warnings)) if warnings else top


def _safe_seat(top: LayerTop, h: float, slope: float, warnings: list) -> LayerTop:


    e = np.maximum(top.errors, -h)
    stations = top.stations
    limits = [slope * (stations[i + 1][1] - stations[i][1])
              if stations[i][0] == stations[i + 1][0] and stations[i + 1][1] > stations[i][1] else None
              for i in range(len(e) - 1)]
    for i, lim in enumerate(limits):
        if lim is not None:
            e[i + 1] = max(e[i + 1], e[i] - lim)
    for i in reversed(range(len(limits))):
        if limits[i] is not None:
            e[i] = max(e[i], e[i + 1] - limits[i])
    raised = [i for i in range(len(e)) if e[i] > top.errors[i] + 1e-9]
    if raised:
        warnings.append("Later layers are seated higher than read at " +
                        ", ".join(f"{top.labels[i]} ({top.errors[i]:+.2f} -> {e[i]:+.2f} mm)" for i in raised) +
                        " so they cannot be driven into metal the touch missed.")
    return replace(top, measured=top.predicted + e, raw_measured=top.measured)
