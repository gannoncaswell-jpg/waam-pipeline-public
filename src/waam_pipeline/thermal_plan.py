from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math

from .plan import bead_gaps
from .toolpath import Toolpath


DEFAULT_SOURCE = (
    "Single-run fit of three pyrometer surface-heating layers from the "
    "2026-10-06 run 001 ER70S-6, Pulse #83, five-bead 24 mm wall/substrate "
    "on the LC cell, using 2400 W, tau 430 s, heat during arcs, and cooling "
    "through gaps; refit per setup (a plate cooler changes tau)."
)


def _finite(value, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _positive(value, name: str, *, allow_zero: bool = False) -> float:
    number = _finite(value, name)
    if number < 0 if allow_zero else number <= 0:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return number


@dataclass(frozen=True)
class ThermalModel:
    C_j_per_k: float
    tau_s: float
    ambient_c: float
    power_w: float
    source: str

    def __post_init__(self) -> None:
        for name in ("C_j_per_k", "tau_s", "power_w"):
            value = _positive(getattr(self, name), name)
            object.__setattr__(self, name, value)
        ambient = _finite(self.ambient_c, "ambient_c")
        if not -273.15 < ambient <= 2000:
            raise ValueError("ambient_c must be above absolute zero and at most 2000 C")
        object.__setattr__(self, "ambient_c", ambient)
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty note")
        object.__setattr__(self, "source", self.source.strip())

    @property
    def K_c(self) -> float:
        return self.power_w * self.tau_s / self.C_j_per_k

    def heat(self, start_c: float, seconds: float) -> float:
        start = _finite(start_c, "start_c")
        duration = _positive(seconds, "seconds", allow_zero=True)
        decay = math.exp(-duration / self.tau_s)
        return (self.ambient_c + self.K_c * (1.0 - decay)
                + (start - self.ambient_c) * decay)

    def cool(self, start_c: float, seconds: float) -> float:
        start = _finite(start_c, "start_c")
        duration = _positive(seconds, "seconds", allow_zero=True)
        return self.ambient_c + (start - self.ambient_c) * math.exp(-duration / self.tau_s)


DEFAULT = ThermalModel(
    C_j_per_k=661.0,
    tau_s=430.0,
    ambient_c=30.0,
    power_w=2400.0,
    source=DEFAULT_SOURCE,
)


def _segment_seconds(moves, index: int, feed_mm_min: float | None = None) -> float:
    if index <= 0:
        raise ValueError("a timed move needs a preceding target")
    move, previous = moves[index], moves[index - 1]
    coordinates = (*previous.target, *move.target)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in coordinates):
        raise ValueError(f"move {index} coordinates must be finite numbers")
    feed = move.feed if move.feed is not None else feed_mm_min
    speed = _positive(feed, f"move {index} feed")
    return math.dist(previous.target, move.target) * 60.0 / speed


def layer_passes(toolpath: Toolpath, travel_mm_min: float | None = None) -> list[dict] | dict:

    if not isinstance(toolpath, Toolpath):
        raise ValueError("toolpath must be a Toolpath")
    if travel_mm_min is not None:
        travel_mm_min = _positive(travel_mm_min, "travel_mm_min")
    if not toolpath.moves:
        raise ValueError("toolpath has no moves")

    active = None
    runs: list[tuple[int, int]] = []
    for kind, index in toolpath.arc_events():
        if kind == "ARCON":
            if active is not None:
                raise ValueError("toolpath has overlapping arc runs")
            active = index
        elif active is None:
            raise ValueError("toolpath turns the arc off before turning it on")
        else:
            runs.append((active, index))
            active = None
    if active is not None:
        raise ValueError("toolpath leaves the arc on")
    if not runs:
        raise ValueError("toolpath has no deposit runs")

    if type(toolpath.copies) is not int or not 1 <= toolpath.copies <= 4:
        raise ValueError("toolpath copies must be a whole number from 1 to 4")
    copy_ids = {move.copy for move in toolpath.moves if move.deposit}
    if any(type(copy) is not int or copy < 0 for copy in copy_ids):
        raise ValueError("deposit move copy indices must be non-negative integers")
    copies = max(toolpath.copies, max(copy_ids, default=0) + 1)
    if not 1 <= copies <= 4 or copy_ids != set(range(copies)):
        raise ValueError("toolpath copies must be a consecutive set from 0")

    split_runs: list[dict] = []
    for start, stop in runs:
        if start == 0:
            raise ValueError("the first move deposits, so its arc time is unknown")
        chunk_start = start
        for index in range(start + 1, stop + 1):
            boundary = (index == stop
                        or toolpath.moves[index].layer != toolpath.moves[index - 1].layer
                        or toolpath.moves[index].copy != toolpath.moves[index - 1].copy)
            if not boundary:
                continue
            arc_s = math.fsum(_segment_seconds(toolpath.moves, i)
                              for i in range(chunk_start, index))
            split_runs.append({"layer": toolpath.moves[chunk_start].layer,
                               "copy": toolpath.moves[chunk_start].copy,
                               "start": chunk_start, "stop": index, "arc_s": arc_s})
            chunk_start = index

    lifted, _ = bead_gaps(toolpath)
    rows: list[dict] = []
    for run in split_runs:
        if not rows or rows[-1]["layer"] != run["layer"]:
            rows.append({"layer": run["layer"], "passes": [], "_runs": []})
        rows[-1]["_runs"].append(run)
    if copies > 1:
        for index, row in enumerate(rows[:-1]):
            run = row["_runs"][-1]
            following = rows[index + 1]["_runs"][0]
            row["tail_gap_s"] = math.fsum(
                _segment_seconds(toolpath.moves, move_index, travel_mm_min)
                for move_index in range(run["stop"], following["start"])
                if not toolpath.moves[move_index].deposit
            )
        rows[-1]["tail_gap_s"] = 0.0
    for row in rows:
        runs_in_layer = row.pop("_runs")
        for index, run in enumerate(runs_in_layer):
            gap_s = 0.0
            qualifies = False
            if index + 1 < len(runs_in_layer):
                following = runs_in_layer[index + 1]
                gap_s = math.fsum(
                    _segment_seconds(toolpath.moves, i, travel_mm_min)
                    for i in range(run["stop"], following["start"])
                    if not toolpath.moves[i].deposit
                )
                qualifies = run["stop"] in lifted
            record = {"arc_s": run["arc_s"], "gap_s": gap_s,
                      "bead_dwell": qualifies}
            if copies > 1:
                record["copy"] = run["copy"]
            row["passes"].append(record)
    return rows if copies == 1 else {"copies": copies, "layers": rows}


def _pass_record(raw, where: str) -> dict:
    if isinstance(raw, Mapping):
        if "arc_s" not in raw:
            raise ValueError(f"{where} needs arc_s")
        arc_s = _positive(raw["arc_s"], f"{where} arc_s")
        gap_s = _positive(raw.get("gap_s", 0.0), f"{where} gap_s", allow_zero=True)
        eligible = raw.get("bead_dwell", raw.get("qualifies_bead_dwell", False))
        if type(eligible) is not bool:
            raise ValueError(f"{where} bead_dwell must be a bool")
        record = {"arc_s": arc_s, "gap_s": gap_s, "bead_dwell": eligible}
        if "copy" in raw:
            copy = raw["copy"]
            if type(copy) is not int or copy < 0:
                raise ValueError(f"{where} copy must be a non-negative integer")
            record["copy"] = copy
        return record
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"arc_s": _positive(raw, f"{where} arc_s"),
                "gap_s": 0.0, "bead_dwell": False}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and 1 <= len(raw) <= 3:
        arc_s = _positive(raw[0], f"{where} arc_s")
        gap_s = (_positive(raw[1], f"{where} gap_s", allow_zero=True)
                 if len(raw) >= 2 else 0.0)
        eligible = raw[2] if len(raw) == 3 else False
        if type(eligible) is not bool:
            raise ValueError(f"{where} bead_dwell must be a bool")
        return {"arc_s": arc_s, "gap_s": gap_s, "bead_dwell": eligible}
    raise ValueError(f"{where} must be an arc time or pass record")


def _normalise_layers(passes_by_layer) -> tuple[list[dict], int]:
    declared_copies = None
    if isinstance(passes_by_layer, Mapping):
        declared_copies = passes_by_layer.get("copies")
        if "layers" in passes_by_layer:
            raw_layers = passes_by_layer["layers"]
        elif "passes" in passes_by_layer:
            raw_layers = [passes_by_layer]
        else:
            raw_layers = [{"layer": layer, "passes": passes}
                          for layer, passes in passes_by_layer.items()]
    else:
        raw_layers = passes_by_layer
    if (not isinstance(raw_layers, Iterable)
            or isinstance(raw_layers, (str, bytes))):
        raise ValueError("passes_by_layer must contain layer records")

    layers = []
    for layer_index, raw_layer in enumerate(raw_layers):
        tail_gap_s = 0.0
        if isinstance(raw_layer, Mapping):
            label = raw_layer.get("layer", layer_index)
            tail_gap_s = _positive(raw_layer.get("tail_gap_s", 0.0),
                                   f"layer {label} tail_gap_s", allow_zero=True)
            if "passes" in raw_layer:
                raw_passes = raw_layer["passes"]
            elif "arc_s" in raw_layer and isinstance(raw_layer["arc_s"], Sequence):
                arcs = raw_layer["arc_s"]
                gaps = raw_layer.get("gap_s", [0.0] * len(arcs))
                dwell = raw_layer.get("bead_dwell", [False] * len(arcs))
                if not (len(arcs) == len(gaps) == len(dwell)):
                    raise ValueError(f"layer {label} arc, gap, and dwell lists must match")
                raw_passes = list(zip(arcs, gaps, dwell))
            else:
                raise ValueError(f"layer {label} needs passes")
        elif isinstance(raw_layer, Sequence) and not isinstance(raw_layer, (str, bytes)):
            label, raw_passes = layer_index, raw_layer
        else:
            raise ValueError(f"layer {layer_index} must be a layer record")
        if (not isinstance(raw_passes, Iterable)
                or isinstance(raw_passes, (str, bytes))):
            raise ValueError(f"layer {label} passes must be a sequence")
        passes = [_pass_record(raw, f"layer {label} pass {i + 1}")
                  for i, raw in enumerate(raw_passes)]
        if not passes:
            raise ValueError(f"layer {label} has no passes")
        layers.append({"layer": label, "passes": passes, "tail_gap_s": tail_gap_s})
    if not layers:
        raise ValueError("passes_by_layer has no usable layers")
    if declared_copies is not None and (type(declared_copies) is not int
                                        or not 1 <= declared_copies <= 4):
        raise ValueError("copies must be a whole number from 1 to 4")
    seen = {pass_["copy"] for layer in layers for pass_ in layer["passes"]
            if "copy" in pass_}
    copies = declared_copies or (max(seen) + 1 if seen else 1)
    if not 1 <= copies <= 4:
        raise ValueError("copies must be a whole number from 1 to 4")
    expected = set(range(copies))
    if seen and seen != expected:
        raise ValueError("copy indices must be a consecutive set from 0")
    if copies > 1:
        for layer in layers:
            if any("copy" not in pass_ for pass_ in layer["passes"]):
                raise ValueError(f"layer {layer['layer']} needs a copy on every pass")
            if {pass_["copy"] for pass_ in layer["passes"]} != expected:
                raise ValueError(f"layer {layer['layer']} needs passes for every copy")
    return layers, copies


def _simulate_layers(model: ThermalModel, layers: list[dict], *, start_c: float,
                     layer_dwell_s: float, bead_dwell_s: float,
                     layer_overhead_s: float) -> dict:
    temperature = start_c
    rows = []
    maximum = -math.inf
    for layer in layers:
        layer_start = temperature
        peaks = []
        for index, pass_ in enumerate(layer["passes"]):
            temperature = model.heat(temperature, pass_["arc_s"])
            peaks.append(temperature)
            maximum = max(maximum, temperature)
            if index + 1 < len(layer["passes"]):
                off_s = pass_["gap_s"]
                if pass_["bead_dwell"]:
                    off_s += bead_dwell_s
                temperature = model.cool(temperature, off_s)
        end_c = temperature
        temperature = model.cool(temperature, layer_dwell_s + layer_overhead_s)
        rows.append({"layer": layer["layer"], "start_c": layer_start,
                     "pass_peaks_c": peaks, "peak_c": max(peaks),
                     "end_c": end_c, "next_preweld_c": temperature})
    return {"rows": rows, "max_peak_c": maximum}


def _simulate_copies(model: ThermalModel, layers: list[dict], copies: int, *,
                     start_c: float | list[float], layer_dwell_s: float,
                     bead_dwell_s: float, layer_overhead_s: float) -> dict:
    temperatures = ([float(start_c)] * copies if isinstance(start_c, (int, float))
                    else list(start_c))
    if len(temperatures) != copies:
        raise ValueError("each copy needs one starting temperature")
    rows = []
    last_rows = [None] * copies
    maximum = -math.inf
    for layer in layers:
        remaining = [0] * copies
        for pass_ in layer["passes"]:
            remaining[pass_["copy"]] += 1
        records = [{"layer": layer["layer"], "copy": copy, "start_c": None,
                    "pass_peaks_c": [], "end_c": None}
                   for copy in range(copies)]
        for index, pass_ in enumerate(layer["passes"]):
            active = pass_["copy"]
            if records[active]["start_c"] is None:
                records[active]["start_c"] = temperatures[active]
                if last_rows[active] is not None:
                    last_rows[active]["next_preweld_c"] = temperatures[active]
            for copy in range(copies):
                temperatures[copy] = (model.heat(temperatures[copy], pass_["arc_s"])
                                      if copy == active else
                                      model.cool(temperatures[copy], pass_["arc_s"]))
            peak = temperatures[active]
            records[active]["pass_peaks_c"].append(peak)
            maximum = max(maximum, peak)
            remaining[active] -= 1
            if remaining[active] == 0:
                records[active]["end_c"] = temperatures[active]
            if index + 1 < len(layer["passes"]):
                off_s = pass_["gap_s"]
                if pass_["bead_dwell"]:
                    off_s += bead_dwell_s
                temperatures = [model.cool(value, off_s) for value in temperatures]
        boundary_s = layer.get("tail_gap_s", 0.0) + layer_dwell_s + layer_overhead_s
        temperatures = [model.cool(value, boundary_s)
                        for value in temperatures]
        for record in records:
            if record["start_c"] is None:
                raise ValueError(f"layer {layer['layer']} has no pass for copy {record['copy']}")
            record["peak_c"] = max(record["pass_peaks_c"])
            record["next_preweld_c"] = temperatures[record["copy"]]
            rows.append(record)
            last_rows[record["copy"]] = record
    return {"rows": rows, "max_peak_c": maximum, "_final_c": temperatures}


def _simulate_pattern(model: ThermalModel, layers: list[dict], copies: int, *,
                      start_c: float | list[float], layer_dwell_s: float,
                      bead_dwell_s: float, layer_overhead_s: float) -> dict:
    if copies == 1:
        return _simulate_layers(model, layers, start_c=float(start_c),
                                layer_dwell_s=layer_dwell_s,
                                bead_dwell_s=bead_dwell_s,
                                layer_overhead_s=layer_overhead_s)
    return _simulate_copies(model, layers, copies, start_c=start_c,
                            layer_dwell_s=layer_dwell_s,
                            bead_dwell_s=bead_dwell_s,
                            layer_overhead_s=layer_overhead_s)


def simulate(model: ThermalModel, passes_by_layer, *, start_c: float,
             layer_dwell_s: float, bead_dwell_s: float,
             layer_overhead_s: float = 0) -> dict:

    if not isinstance(model, ThermalModel):
        raise ValueError("model must be a ThermalModel")
    start = _finite(start_c, "start_c")
    if start <= -273.15:
        raise ValueError("start_c must be above absolute zero")
    layer_dwell = _positive(layer_dwell_s, "layer_dwell_s", allow_zero=True)
    bead_dwell = _positive(bead_dwell_s, "bead_dwell_s", allow_zero=True)
    overhead = _positive(layer_overhead_s, "layer_overhead_s", allow_zero=True)
    layers, copies = _normalise_layers(passes_by_layer)
    result = _simulate_pattern(model, layers, copies, start_c=start,
                               layer_dwell_s=layer_dwell,
                               bead_dwell_s=bead_dwell, layer_overhead_s=overhead)
    result.pop("_final_c", None)
    if copies > 1:
        result["copies"] = copies
    return result


def _smallest_second(limit: float, acceptable) -> tuple[float, bool]:
    if acceptable(0.0):
        return 0.0, True
    whole_limit = math.floor(limit)
    if whole_limit < 1 or not acceptable(float(whole_limit)):
        if limit != whole_limit and acceptable(limit):
            return limit, True
        return limit, False
    low, high = 0, whole_limit
    while high - low > 1:
        middle = (low + high) // 2
        if acceptable(float(middle)):
            high = middle
        else:
            low = middle
    return float(high), True


def schedule(model: ThermalModel, passes_by_layer, *, start_c: float,
             target_preweld_c: float, max_peak_c: float | None = None,
             max_bead_dwell_s: float = 3600,
             max_layer_dwell_s: float = 3600) -> dict:

    if not isinstance(model, ThermalModel):
        raise ValueError("model must be a ThermalModel")
    layers, copies = _normalise_layers(passes_by_layer)
    start = _finite(start_c, "start_c")
    target = _finite(target_preweld_c, "target_preweld_c")
    peak_limit = None if max_peak_c is None else _finite(max_peak_c, "max_peak_c")
    bead_limit = _positive(max_bead_dwell_s, "max_bead_dwell_s", allow_zero=True)
    layer_limit = _positive(max_layer_dwell_s, "max_layer_dwell_s", allow_zero=True)
    if min(start, target, peak_limit if peak_limit is not None else start) <= -273.15:
        raise ValueError("temperatures must be above absolute zero")

    notes = ["Predictive lumped model only; refit and compare with measured interpass temperatures."]
    if target <= model.ambient_c:
        notes.append(f"Target {target:g} C is at or below ambient {model.ambient_c:g} C; "
                     "finite cooling cannot reach it from above.")

    def constraints(layer_s: float, bead_s: float) -> bool:
        result = _simulate_pattern(model, layers, copies, start_c=start,
                                   layer_dwell_s=layer_s,
                                   bead_dwell_s=bead_s, layer_overhead_s=0.0)
        preweld_ok = all(row["start_c"] <= target + 1e-9
                         for row in result["rows"])
        peak_ok = peak_limit is None or result["max_peak_c"] <= peak_limit + 1e-9
        return preweld_ok and peak_ok

    bead_dwell, bead_met = _smallest_second(
        bead_limit, lambda bead_s: constraints(layer_limit, bead_s)
    )
    layer_dwell, layer_met = _smallest_second(
        layer_limit, lambda layer_s: constraints(layer_s, bead_dwell)
    )
    result = _simulate_pattern(model, layers, copies, start_c=start,
                               layer_dwell_s=layer_dwell,
                               bead_dwell_s=bead_dwell, layer_overhead_s=0.0)
    if not bead_met or not layer_met:
        notes.append(f"The requested pre-weld/peak limits are not achievable within the "
                     f"{bead_limit:g} s maximum bead dwell and {layer_limit:g} s maximum "
                     "layer dwell; returning the best achievable.")
    if start > target + 1e-9:
        notes.append(f"The supplied first-layer start {start:g} C is already above the "
                     f"{target:g} C pre-weld target.")

    eligible = sum(pass_["bead_dwell"] for layer in layers
                   for pass_ in layer["passes"][:-1])
    layer_boundaries = max(0, len(layers) - 1)
    total_added = eligible * bead_dwell + layer_boundaries * layer_dwell
    actual_preweld_ok = all(row["start_c"] <= target + 1e-9
                            for row in result["rows"])
    actual_peak_ok = peak_limit is None or result["max_peak_c"] <= peak_limit + 1e-9
    valid = bead_met and layer_met and actual_preweld_ok and actual_peak_ok
    answer = {"layer_dwell_s": layer_dwell, "bead_dwell_s": bead_dwell,
              "rows": result["rows"], "max_peak_c": result["max_peak_c"],
              "total_added_s": total_added,
              "target_preweld_c": target, "max_peak_limit_c": peak_limit,
              "valid": valid, "notes": notes}
    if copies > 1:
        answer["copies"] = copies
    return answer


def _layer_prediction(row: dict, *, tau_s: float, power_w: float,
                      ambient_c: float, inverse_capacity: float) -> float:
    decay = math.exp(-row["wall_s"] / tau_s)
    post_decay = math.exp(-row.get("cooling_s", 0.0) / tau_s)
    duty_power = power_w * row["arc_s"] / row["wall_s"]
    gain = duty_power * tau_s * (1.0 - decay) * post_decay
    heated = ambient_c + (row["start_c"] - ambient_c) * decay
    return ambient_c + (heated - ambient_c) * post_decay + gain * inverse_capacity


def _capacity_at_tau(rows: list[dict], tau_s: float, power_w: float,
                     ambient_c: float) -> tuple[float, float]:
    numer = denominator = 0.0
    terms = []
    for row in rows:
        decay = math.exp(-row["wall_s"] / tau_s)
        post_decay = math.exp(-row.get("cooling_s", 0.0) / tau_s)
        heated = ambient_c + (row["start_c"] - ambient_c) * decay
        base = ambient_c + (heated - ambient_c) * post_decay
        gain = (power_w * row["arc_s"] / row["wall_s"] * tau_s
                * (1.0 - decay) * post_decay)
        terms.append((row["end_c"] - base, gain))
        numer += gain * (row["end_c"] - base)
        denominator += gain * gain
    inverse = numer / denominator if denominator else math.nan
    if not math.isfinite(inverse) or inverse <= 0:
        return math.inf, math.inf
    residual = math.fsum((observed - gain * inverse) ** 2
                         for observed, gain in terms)
    return 1.0 / inverse, residual


def _fit_tau_and_capacity(rows: list[dict], power_w: float,
                          ambient_c: float) -> tuple[float, float]:
    walls = [row["wall_s"] for row in rows]
    low = max(1e-3, min(walls) * 1e-3)
    high = min(1e8, max(walls) * 1e4)
    logs = [math.log(low) + i * (math.log(high) - math.log(low)) / 320
            for i in range(321)]
    scored = [(_capacity_at_tau(rows, math.exp(value), power_w, ambient_c)[1], value)
              for value in logs]
    best_index = min(range(len(scored)), key=lambda i: scored[i][0])
    left = logs[max(0, best_index - 1)]
    right = logs[min(len(logs) - 1, best_index + 1)]
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    x1, x2 = right - ratio * (right - left), left + ratio * (right - left)
    f1 = _capacity_at_tau(rows, math.exp(x1), power_w, ambient_c)[1]
    f2 = _capacity_at_tau(rows, math.exp(x2), power_w, ambient_c)[1]
    for _ in range(80):
        if f1 <= f2:
            right, x2, f2 = x2, x1, f1
            x1 = right - ratio * (right - left)
            f1 = _capacity_at_tau(rows, math.exp(x1), power_w, ambient_c)[1]
        else:
            left, x1, f1 = x1, x2, f2
            x2 = left + ratio * (right - left)
            f2 = _capacity_at_tau(rows, math.exp(x2), power_w, ambient_c)[1]
    tau = math.exp((left + right) / 2.0)
    capacity, _ = _capacity_at_tau(rows, tau, power_w, ambient_c)
    return tau, capacity


def fit_lumped(observations, *, power_w: float, ambient_c: float) -> dict:

    power = _positive(power_w, "power_w")
    ambient = _finite(ambient_c, "ambient_c")
    if not -273.15 < ambient <= 2000:
        raise ValueError("ambient_c must be above absolute zero and at most 2000 C")

    decay_fit = None
    if isinstance(observations, Mapping):
        raw_layers = observations.get("layers", observations.get("layer_observations", []))
        raw_decay = observations.get("decay", observations.get("decay_observations", []))
        if "tau_s" in observations:
            decay_fit = observations
        elif isinstance(raw_decay, Mapping) and "tau_s" in raw_decay:
            decay_fit = raw_decay
    elif isinstance(observations, Iterable) and not isinstance(observations, (str, bytes)):
        items = list(observations)
        raw_layers = [row for row in items if isinstance(row, Mapping)
                      and {"start_c", "end_c", "arc_s", "wall_s"} <= row.keys()]
        decay_fit = next((row for row in items if isinstance(row, Mapping)
                          and "tau_s" in row and "start_c" not in row), None)
    else:
        raise ValueError("observations must contain layer or decay observations")

    if isinstance(raw_layers, Mapping):
        raw_layers = [raw_layers]
    if not isinstance(raw_layers, Iterable) or isinstance(raw_layers, (str, bytes)):
        raise ValueError("layer observations must be a sequence")
    layers = []
    for index, raw in enumerate(raw_layers):
        if not isinstance(raw, Mapping):
            raise ValueError(f"layer observation {index + 1} must be a record")
        missing = {"start_c", "end_c", "arc_s", "wall_s"} - raw.keys()
        if missing:
            raise ValueError(f"layer observation {index + 1} is missing {sorted(missing)}")
        row = {name: _finite(raw[name], f"layer observation {index + 1} {name}")
               for name in ("start_c", "end_c", "arc_s", "wall_s")}
        row["cooling_s"] = _finite(raw.get("cooling_s", 0.0),
                                    f"layer observation {index + 1} cooling_s")
        if row["arc_s"] <= 0 or row["wall_s"] <= 0 or row["arc_s"] > row["wall_s"]:
            raise ValueError(f"layer observation {index + 1} needs 0 < arc_s <= wall_s")
        if row["cooling_s"] < 0:
            raise ValueError(f"layer observation {index + 1} cooling_s must be non-negative")
        if min(row["start_c"], row["end_c"]) <= -273.15:
            raise ValueError(f"layer observation {index + 1} temperatures are below absolute zero")
        row["layer"] = raw.get("layer", index + 1)
        layers.append(row)

    notes = ["Lumped fit uses pyrometer surface temperatures; refit for each setup."]
    tau = None
    decay_valid = True
    if decay_fit is not None:
        tau = _positive(decay_fit.get("tau_s"), "decay tau_s")
        fit_notes = decay_fit.get("notes", [])
        if isinstance(fit_notes, Sequence) and not isinstance(fit_notes, (str, bytes)):
            notes.extend(str(note) for note in fit_notes)
        decay_valid = bool(decay_fit.get("valid", True))
        if decay_fit.get("ambient_c") is not None:
            decay_ambient = _finite(decay_fit["ambient_c"], "decay ambient_c")
            if not math.isclose(decay_ambient, ambient, abs_tol=1e-9):
                notes.append(f"Decay fit ambient {decay_ambient:g} C differs from "
                             f"the requested {ambient:g} C.")

    if not layers:
        raise ValueError("Lumped fit needs at least one usable layer observation")
    if tau is not None:
        capacity, _ = _capacity_at_tau(layers, tau, power, ambient)
        tau_basis = "decay"
    else:
        if len(layers) < 2:
            raise ValueError("Lumped fit without decay data needs at least two layer observations")
        tau, capacity = _fit_tau_and_capacity(layers, power, ambient)
        tau_basis = "layers"
    if not math.isfinite(capacity):
        raise ValueError("Layer observations contain no usable positive heating response")

    fitted_rows = []
    per_layer_capacity = []
    inverse = 1.0 / capacity
    for row in layers:
        predicted = _layer_prediction(row, tau_s=tau, power_w=power,
                                      ambient_c=ambient, inverse_capacity=inverse)
        decay = math.exp(-row["wall_s"] / tau)
        post_decay = math.exp(-row["cooling_s"] / tau)
        heated = ambient + (row["start_c"] - ambient) * decay
        base = ambient + (heated - ambient) * post_decay
        gain = (power * row["arc_s"] / row["wall_s"] * tau
                * (1.0 - decay) * post_decay)
        individual = gain / (row["end_c"] - base) if row["end_c"] > base else math.nan
        if math.isfinite(individual) and individual > 0:
            per_layer_capacity.append(individual)
        fitted_rows.append({**row, "predicted_end_c": predicted,
                            "residual_c": row["end_c"] - predicted,
                            "C_j_per_k": individual})
    rms = math.sqrt(math.fsum(row["residual_c"] ** 2 for row in fitted_rows)
                    / len(fitted_rows))
    spread = ((max(per_layer_capacity) - min(per_layer_capacity)) / capacity
              if len(per_layer_capacity) >= 2 else 0.0)
    capacity_spread = (max(per_layer_capacity) / min(per_layer_capacity)
                       if len(per_layer_capacity) >= 2 else 1.0)
    if spread > 0.25:
        notes.append(f"Per-layer heat capacity spans {spread:.0%} of the fitted value.")
    return {"C_j_per_k": capacity, "tau_s": tau, "power_w": power,
            "ambient_c": ambient, "tau_basis": tau_basis, "count": len(layers),
            "decay_count": int(decay_fit is not None),
            "rms_c": rms, "C_spread": capacity_spread, "spread_fraction": spread,
            "tau_spread_fraction": 0.0, "residuals_c":
            [row["residual_c"] for row in fitted_rows], "rows": fitted_rows,
            "used": fitted_rows, "notes": notes, "valid": decay_valid}
