from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from statistics import median

import numpy as np

from .toolpath import Toolpath
from .thermal import GoldakHistory, GoldakSettings


@dataclass(frozen=True)
class Response:
    temperature_c: float
    width_mm: float
    height_mm: float
    support_reserve: float


@dataclass(frozen=True)
class Profile:
    name: str
    provenance: str

    evidence: str
    filler: str
    wire_dia_mm: float
    substrate_mm: float
    ambient_c: float
    net_power_w: float
    heat_capacity_j_mm3_k: float
    heat_radius_mm: float
    cooling_tau_s: float
    bead_width_mm: float
    layer_height_mm: float
    deposition_mm_min: float
    travel_mm_min: float
    max_width_error_mm: float
    max_height_error_mm: float
    min_support_fraction: float
    min_support_reserve: float
    max_pause_s: float
    pause_resolution_s: float
    sample_mm: float
    response: tuple[Response, ...]
    heat_model: str = "gaussian"
    goldak: GoldakSettings | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        if not isinstance(data, dict):
            raise ValueError("Thermal profile must be a JSON object")
        try:
            values = {**data, "response": tuple(Response(**r) for r in data["response"])}
            if values.get("goldak") is not None:
                values["goldak"] = GoldakSettings.from_dict(values["goldak"])
            obj = cls(**values)
            obj.validate()
            return obj
        except (TypeError, KeyError, OverflowError) as exc:
            raise ValueError("Thermal profile has missing or invalid fields") from exc

    def validate(self) -> None:
        for key in ("name", "provenance", "filler"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"Thermal profile needs {key} (1–500 characters)")
        if self.evidence not in ("illustrative", "supplied"):
            raise ValueError("Thermal evidence must be illustrative or supplied")
        for key, value in asdict(self).items():
            if key in ("name", "provenance", "filler", "evidence", "response", "heat_model", "goldak"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Thermal {key} must be a finite number")
            if key == "substrate_mm" and value == 0:
                continue
            if key != "ambient_c" and not 0 < value <= 1e6:
                raise ValueError(f"Thermal {key} must be positive and at most 1e6")
        if not -273.15 < self.ambient_c < 2000:
            raise ValueError("Thermal ambient_c is out of range")
        if not 0 < self.min_support_fraction <= 1 or self.min_support_reserve < 1:
            raise ValueError("Support fraction must be in (0, 1]; reserve must be at least 1")
        if self.max_pause_s > 3600 or not 0.01 <= self.pause_resolution_s <= self.max_pause_s:
            raise ValueError("Pause search must fit within 3600 s and have a valid resolution")
        if min(self.deposition_mm_min, self.travel_mm_min) < 1 or self.heat_capacity_j_mm3_k < 1e-9:
            raise ValueError("Thermal speeds must be at least 1 mm/min and heat capacity at least 1e-9")
        if not 0.25 <= self.sample_mm <= min(self.bead_width_mm / 2, self.heat_radius_mm / 2):
            raise ValueError("sample_mm must be at least 0.25 and at most half the bead width and heat radius")
        if self.heat_model not in ("gaussian", "goldak"):
            raise ValueError("Thermal heat_model must be gaussian or goldak")
        if self.heat_model == "goldak":
            if not isinstance(self.goldak, GoldakSettings):
                raise ValueError("Goldak heat model needs explicit goldak settings")
            self.goldak.validate(self.ambient_c, self.bead_width_mm, self.layer_height_mm, self.substrate_mm)
            if self.sample_mm > self.goldak.cell_mm:
                raise ValueError("Goldak deposition sample_mm must not exceed cell_mm")
        elif self.goldak is not None:
            raise ValueError("Goldak settings require heat_model=goldak")
        if not 2 <= len(self.response) <= 64:
            raise ValueError("Thermal response needs 2–64 rows")
        last = None
        for row in self.response:
            for value in asdict(row).values():
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or abs(value) > 1e6):
                    raise ValueError("Thermal response values must be finite numbers")
            if row.width_mm <= 0 or row.height_mm <= 0 or row.support_reserve < 0:
                raise ValueError("Response dimensions must be positive; reserve cannot be negative")
            if last and not (row.temperature_c > last.temperature_c
                             and row.width_mm >= last.width_mm
                             and row.height_mm <= last.height_mm
                             and row.support_reserve <= last.support_reserve):
                raise ValueError("Response must increase in temperature/width and decrease in height/reserve")
            last = row
        first = self.response[0]
        if not math.isclose(first.temperature_c, self.ambient_c, abs_tol=1e-6):
            raise ValueError("First response temperature must equal ambient_c")
        if not (math.isclose(first.width_mm, self.bead_width_mm, abs_tol=1e-6)
                and math.isclose(first.height_mm, self.layer_height_mm, abs_tol=1e-6)):
            raise ValueError("Ambient response must match nominal bead width and layer height")

    def at(self, temperature_c: float) -> Response | None:

        if not math.isfinite(temperature_c) or not self.ambient_c <= temperature_c <= self.response[-1].temperature_c:
            return None
        for a, b in zip(self.response, self.response[1:]):
            if temperature_c <= b.temperature_c:
                t = (temperature_c - a.temperature_c) / (b.temperature_c - a.temperature_c)
                return Response(temperature_c, *(getattr(a, k) + t * (getattr(b, k) - getattr(a, k))
                                                 for k in ("width_mm", "height_mm", "support_reserve")))
        return None


def illustrative_profile(width_mm=8.6, height_mm=2.9, travel_mm_min=175.0,
                         *, filler="er70s6", wire_mm=1.143, substrate_mm=6.35) -> Profile:

    return Profile.from_dict(dict(
        name="Illustrative wall response", evidence="illustrative",
        filler=filler, wire_dia_mm=wire_mm, substrate_mm=substrate_mm,
        provenance="Synthetic inputs for exercising the scheduler; no material or cell calibration.",
        ambient_c=25.0, net_power_w=1800.0, heat_capacity_j_mm3_k=0.004,
        heat_radius_mm=12.0, cooling_tau_s=100.0,
        bead_width_mm=width_mm, layer_height_mm=height_mm,
        deposition_mm_min=travel_mm_min, travel_mm_min=1200.0,
        max_width_error_mm=width_mm * 0.1, max_height_error_mm=height_mm * 0.25,
        min_support_fraction=0.8, min_support_reserve=1.2,
        max_pause_s=600.0, pause_resolution_s=0.5,
        sample_mm=min(2.0, width_mm / 2),
        response=[dict(temperature_c=t, width_mm=width_mm * w,
                       height_mm=height_mm * h, support_reserve=r)
                  for t, w, h, r in [(25, 1, 1, 3), (150, 1.02, .99, 2.5),
                                     (300, 1.12, .94, 1.5), (500, 1.35, .8, .5),
                                     (900, 1.7, .6, 0)]]))


def illustrative_goldak_profile(**kwargs) -> Profile:


    p = illustrative_profile(**kwargs)
    cell = min(1.45, p.bead_width_mm / 4, p.layer_height_mm / 2)
    g = GoldakSettings.from_dict(dict(
        lateral_mm=4.3, depth_mm=4, front_mm=3, rear_mm=8, front_fraction=3/11,
        density_kg_m3=7870, solidus_c=1450, liquidus_c=1520, latent_heat_j_kg=270000,
        properties=[dict(temperature_c=t, conductivity_w_m_k=k, specific_heat_j_kg_k=c)
                    for t, k, c in [(20, 51, 480), (200, 48, 530), (400, 43, 580),
                                    (600, 37, 650), (800, 27, 680), (1000, 26, 700),
                                    (1200, 27, 720), (1400, 29, 750), (1538, 30, 800),
                                    (4000, 30, 800)]],
        plate_length_mm=40, plate_width_mm=20, cell_mm=cell, max_step_s=0.1,
        convection_w_m2_k=10, emissivity=0.8, bottom_contact_w_m2_k=1000))
    p = replace(p, name="Illustrative Goldak wall study", heat_model="goldak", goldak=g,
                net_power_w=300,
                sample_mm=cell, max_pause_s=120, pause_resolution_s=2,
                provenance="Uncalibrated numerical study: reference-repo low-alloy k/cp estimates; "
                           "assumed source, finite plate/contact, phase interval and high-T extension. "
                           "Synthetic dimensional and support response.")
    p.validate()
    return p


class HeatHistory:


    def __init__(self, profile: Profile, pulses=()):
        self.profile = profile
        self.pulses = list(pulses)

    def temperature(self, point, time_s):
        if not self.pulses:
            return self.profile.ambient_c
        a = np.asarray(self.pulses, dtype=float)
        age = time_s - a[:, 3]
        if np.any(age < -1e-7):
            raise ValueError("Thermal history cannot be evaluated before deposition")
        p = self.profile
        d2 = ((a[:, :3] - point) ** 2).sum(axis=1)
        norm = p.heat_capacity_j_mm3_k * (2 * math.pi) ** 1.5 * p.heat_radius_mm ** 3
        rise = np.sum(a[:, 4] / norm * np.exp(-d2 / (2 * p.heat_radius_mm ** 2)
                                              - np.maximum(age, 0) / p.cooling_tau_s))
        return float(p.ambient_c + rise)

    def add(self, point, time_s, energy_j):
        self.pulses.append((*map(float, point), float(time_s), float(energy_j)))

    def clone(self):
        return HeatHistory(self.profile, self.pulses)

    def prepare(self, time_s):
        pass

    def has_heat(self):
        return bool(self.pulses)

    def deposit(self, a, b, start_s, duration_s, height_mm):
        source = (a + b) / 2
        source[2] = a[2] - self.profile.layer_height_mm + height_mm / 2
        self.add(source, start_s + duration_s, self.profile.net_power_w * duration_s)

    def diagnostics(self):
        return {"model": "gaussian_proxy"}


@dataclass(frozen=True)
class Bead:
    a: tuple[float, float, float]
    b: tuple[float, float, float]
    nominal_top: float
    top: float
    width: float
    layer: int


def support_at(a, b, width, bottom, beads, substrate_z):


    if abs(bottom - substrate_z) < 1e-6:
        return 1.0, substrate_z
    lower = [s for s in beads if abs(s.nominal_top - bottom) < 1e-5]
    if not lower:
        return 0.0, bottom
    aa = np.array([s.a[:2] for s in lower])
    bb = np.array([s.b[:2] for s in lower])
    ab = bb - aa
    norm2 = (ab * ab).sum(axis=1)
    radii = np.array([s.width / 2 for s in lower])
    tops = np.array([s.top for s in lower])
    direction = b[:2] - a[:2]
    normal = np.array([-direction[1], direction[0]]) / np.linalg.norm(direction)
    coverage, heights = [], []
    for along in (0.0, 0.5, 1.0):
        centre = a[:2] + along * direction
        points = centre + ((np.arange(21) + 0.5) / 21 - 0.5)[:, None] * width * normal
        t = np.clip(((points[:, None, :] - aa) * ab).sum(axis=2) / norm2, 0, 1)
        distances = np.linalg.norm(points[:, None, :] - (aa + t[:, :, None] * ab), axis=2)
        covered = distances <= radii + 1e-6
        present = covered.any(axis=1)
        coverage.append(float(present.mean()))
        if present.any():
            heights.extend(np.max(np.where(covered[present], tops, -np.inf), axis=1).tolist())
    return min(coverage), min(heights) if heights else bottom


def _passes(toolpath, p):
    moves = toolpath.moves
    if not moves or moves[0].deposit:
        raise ValueError("Thermal simulation needs a travel move defining the first weld start")
    if len(moves) > 20000:
        raise ValueError("Thermal simulation is limited to 20000 moves")
    for m in moves:
        if not all(math.isfinite(v) and abs(v) <= 1e6 for v in m.target):
            raise ValueError("Thermal toolpath coordinates must be finite and within 1e6 mm")
    result, current, travel_s, seen, count = [], [], 0.0, set(), 0
    for i, (prev, move) in enumerate(zip(moves, moves[1:]), 1):
        a, b = np.array(prev.target), np.array(move.target)
        length = float(np.linalg.norm(b - a))
        if move.deposit:
            if abs(a[2] - b[2]) > 1e-6 or (current and move.layer != current[-1][4]):
                raise ValueError("Thermal response currently supports planar single-bead layers only")
            if length < 1e-9:
                continue
            n = max(1, math.ceil(length / p.sample_mm))
            count += n
            if count > 4000:
                raise ValueError("Thermal simulation is limited to 4000 deposition samples")
            for k in range(n):
                current.append((a + (b - a) * k / n, a + (b - a) * (k + 1) / n,
                                length / n * 60 / p.deposition_mm_min, i, move.layer))
        else:
            if current:
                layer = current[0][4]
                if layer in seen:
                    raise ValueError("Multiple passes in one layer need a calibrated overlap/remelting model")
                result.append((current, travel_s))
                seen.add(layer)
                current, travel_s = [], 0.0
            travel_s += length * 60 / p.travel_mm_min
    if current:
        if current[0][4] in seen:
            raise ValueError("Multiple passes in one layer need a calibrated overlap/remelting model")
        result.append((current, travel_s))
    if not result:
        raise ValueError("Thermal simulation needs nonzero deposition length")
    return result


def simulate(toolpath: Toolpath, profile: Profile, *, policy="adaptive", substrate_z_mm=0.0,
             viz: bool = False, viz_max_payload_bytes: int | None = None) -> dict:


    profile.validate()
    if policy not in ("adaptive", "continuous"):
        raise ValueError("Cooling policy must be adaptive or continuous")
    p = profile
    passes = _passes(toolpath, p)
    substrate = float(substrate_z_mm)
    if not math.isfinite(substrate) or abs(substrate) > 1e6:
        raise ValueError("Thermal substrate plane must be finite and within 1e6 mm")
    viz_requested = bool(viz)
    history = (GoldakHistory(p, passes, substrate, enable_viz=viz_requested,
                             viz_max_payload_bytes=viz_max_payload_bytes)
               if p.heat_model == "goldak" else HeatHistory(p))
    beads, time_s, rows = [], 0.0, []
    evaluations = 0
    cooled_states = {}

    def trial(samples, start):
        nonlocal evaluations
        evaluations += len(samples)
        if evaluations > 120000:
            raise ValueError("Thermal preview exceeded its bounded sample evaluation budget")


        earlier = [t for t in cooled_states if t <= start]
        heat = (cooled_states[max(earlier)] if earlier else history).clone()
        heat.prepare(start)
        if p.heat_model == "goldak" and len(cooled_states) < 32:
            cooled_states[start] = heat.clone()
        added, now, reasons = [], start, set()
        peak, width_error, height_error, coverage, reserve = p.ambient_c, 0.0, 0.0, 1.0, math.inf
        for a, b, dt, move_index, layer in samples:
            bottom = float(a[2] - p.layer_height_mm)


            point = (a + b) / 2
            point[2] = bottom
            temp = heat.temperature(point, now)
            peak = max(peak, temp)
            response = p.at(temp)
            if response is None:
                reasons.add("response_range")
                break
            fraction, support_z = support_at(a, b, response.width_mm, bottom, beads, substrate)
            coverage = min(coverage, fraction)
            reserve = min(reserve, response.support_reserve * fraction)
            predicted_top = support_z + response.height_mm
            width_error = max(width_error, abs(response.width_mm - p.bead_width_mm))
            height_error = max(height_error, abs(predicted_top - float(a[2])))
            if fraction < p.min_support_fraction:
                reasons.add("support_coverage")
            if response.support_reserve * fraction < p.min_support_reserve:
                reasons.add("hot_support")
            if width_error > p.max_width_error_mm:
                reasons.add("width_error")
            if height_error > p.max_height_error_mm:
                reasons.add("height_error")
            added.append(Bead(tuple(a), tuple(b), float(a[2]), predicted_top,
                              response.width_mm, layer))
            heat.deposit(a, b, now, dt, response.height_mm)
            now += dt
        metrics = dict(peak_interpass_c=peak, width_error_mm=width_error,
                       height_error_mm=height_error, support_fraction=coverage,
                       support_reserve=None if math.isinf(reserve) else reserve,
                       reasons=sorted(reasons))
        if "response_range" in reasons:


            for key in ("width_error_mm", "height_error_mm", "support_fraction", "support_reserve"):
                metrics[key] = None
        return metrics, heat, added, now

    for samples, travel_s in passes:
        cooled_states.clear()
        time_s += travel_s
        initial_trial = trial(samples, time_s)
        before = initial_trial[0]
        chosen = None
        pause = 0.0
        if not before["reasons"]:
            chosen = initial_trial
        elif policy == "adaptive" and history.has_heat():


            low, high = 0.0, min(p.pause_resolution_s, p.max_pause_s)
            while True:
                candidate = trial(samples, time_s + high)
                if not candidate[0]["reasons"]:
                    chosen = candidate
                    break
                if high >= p.max_pause_s:
                    break
                low, high = high, min(2 * high, p.max_pause_s)
            if chosen:
                while high - low > p.pause_resolution_s:
                    middle = (low + high) / 2
                    candidate = trial(samples, time_s + middle)
                    if candidate[0]["reasons"]:
                        low = middle
                    else:
                        high, chosen = middle, candidate
                pause = high
        accepted = chosen is not None
        metrics = chosen[0] if accepted else before
        rows.append(dict(pass_index=len(rows), layer=samples[0][4],
                         start_move=samples[0][3], end_move=samples[-1][3],
                         status="continue" if accepted and not pause else "cool" if accepted else "stop",
                         start_s=time_s + pause, duration_s=sum(s[2] for s in samples),
                         pause_s=pause, trigger=before, **metrics))
        if not accepted:
            break
        _, history, added, time_s = chosen
        beads.extend(added)
    completed = len(rows) == len(passes) and rows[-1]["status"] != "stop"
    viz_payload = None
    if viz_requested:
        viz_payload = (history.viz_payload() if p.heat_model == "goldak"
                       else {"available": False, "reason": "viz is only available for Goldak simulations"})
    return dict(status="complete" if completed else "stopped", policy=policy,
                substrate_z_mm=substrate,
                planning_only=True, profile=asdict(p), passes=rows, heat=history.diagnostics(),
                total_passes=len(passes), completed_passes=sum(r["status"] != "stop" for r in rows),
                cooling_s=sum(r["pause_s"] for r in rows), elapsed_s=time_s,
                viz=viz_payload,
                limitations=("Unvalidated Goldak thermal model on nominal deposited geometry; "
                             if p.heat_model == "goldak" else "Unvalidated local heat proxy; ")
                            + "empirical dimensions and sampled bead support; "
                            "no melt-pool flow, buckling, residual-stress distortion or robot timing. "
                            "Simulation cooling is not applied to robot exports.")


MIN_COOLING_PAIRS = 2


MIN_EXCESS_C = 5.0
MAX_FIT_RMS_C = 10.0
MAX_TAU_SPREAD = 2.0
MIN_PROPOSED_S = 1.0
MAX_PROPOSED_S = 3600.0


def cooling_pairs(run: dict) -> list[dict]:

    pairs = []
    for cycle in (run or {}).get("cycles", []):
        readings = {}
        for label, stage in (("t_before", "before-interpass"), ("t_after", "after-interpass")):
            readings[label] = cycle.get(label) or next(
                (r for r in reversed(run.get("temperatures", []))
                 if r.get("cycle") == cycle.get("cycle") and r.get("stage") == stage), None)
        if readings["t_before"] and readings["t_after"] and cycle.get("interpass_s") is not None:
            pairs.append({"cycle": cycle.get("cycle"), "before_c": readings["t_before"]["value_c"],
                          "after_c": readings["t_after"]["value_c"], "seconds": cycle["interpass_s"]})
    return pairs


def fit_cooling(pairs, *, ambient_c: float = 25.0) -> dict:


    amb = float(ambient_c)
    if not math.isfinite(amb) or not -40.0 <= amb <= 60.0:
        raise ValueError("Ambient temperature must be between -40 and 60 C")
    used, skipped = [], []
    for raw in pairs:
        p = dict(raw)
        tb, ta, t = p.get("before_c"), p.get("after_c"), p.get("seconds")
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                      for v in (tb, ta, t))
        reason = ("not finite numbers" if not numbers else "no dwell" if t <= 0
                  else "did not cool" if ta >= tb
                  else f"T-after within {MIN_EXCESS_C:g} C of ambient" if ta - amb < MIN_EXCESS_C
                  else None)
        (skipped if reason else used).append({**p, "reason": reason} if reason else p)
    if len(used) < MIN_COOLING_PAIRS:
        raise ValueError(f"Cooling fit needs at least {MIN_COOLING_PAIRS} cycles that cooled from "
                         f"T-before to T-after above ambient; {len(used)} usable so far")
    t = np.array([p["seconds"] for p in used], dtype=float)
    y = -np.log((np.array([p["after_c"] for p in used]) - amb)
                / (np.array([p["before_c"] for p in used]) - amb))
    tau = float(np.sum(t * t) / np.sum(t * y))
    for p, ti, yi in zip(used, t, y):
        p["tau_s"] = float(ti / yi)
        p["predicted_after_c"] = amb + (p["before_c"] - amb) * math.exp(-ti / tau)
        p["residual_c"] = p["after_c"] - p["predicted_after_c"]
    rms = float(np.sqrt(np.mean([p["residual_c"] ** 2 for p in used])))
    taus = [p["tau_s"] for p in used]
    spread = max(taus) / min(taus)
    notes = []
    if rms > MAX_FIT_RMS_C:
        notes.append(f"RMS residual {rms:.1f} C is above {MAX_FIT_RMS_C:g} C")
    if spread > MAX_TAU_SPREAD:
        notes.append(f"per-cycle tau varies {spread:.1f}x (limit {MAX_TAU_SPREAD:g}x)")
    return {"tau_s": tau, "tau_last_s": used[-1]["tau_s"], "ambient_c": amb, "rms_c": rms,
            "tau_spread": spread, "valid": not notes, "notes": notes, "used": used,
            "skipped": skipped}


def fit_decay(rows, ambient_c: float = 25.0) -> dict:

    try:
        amb = float(ambient_c)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Ambient temperature must be between -40 and 60 C") from exc
    if isinstance(ambient_c, bool) or not math.isfinite(amb) or not -40.0 <= amb <= 60.0:
        raise ValueError("Ambient temperature must be between -40 and 60 C")
    used, skipped, notes = [], [], []
    for row in rows:
        label = f"Layer {row.get('layer')}, pass {row.get('pass')}"
        offset = row.get("park_travel_s_est")
        offset = 0.0 if offset is None else offset
        unit = row.get("unit")
        readings = row.get("readings", {})
        reason = None
        if (unit not in ("C", "F") or not isinstance(readings, dict)
                or isinstance(offset, bool) or not isinstance(offset, (int, float))
                or not math.isfinite(offset) or not 0 <= offset <= 7200):
            reason = "invalid unit, readings, or park travel time"
        samples = []
        if reason is None:
            for seconds, value in readings.items():
                try:
                    second = float(seconds)
                except (TypeError, ValueError, OverflowError):
                    second = math.nan
                if (not math.isfinite(second) or not 0 <= second <= 7200
                        or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not -50 <= value <= 2000):
                    notes.append(f"{label}: skipped invalid reading at {seconds} s")
                    continue
                value_c = (value - 32.0) * 5.0 / 9.0 if unit == "F" else float(value)
                if value_c <= amb + MIN_EXCESS_C:
                    notes.append(f"{label}: skipped reading at {seconds} s within "
                                 f"{MIN_EXCESS_C:g} C of ambient or below")
                    continue
                samples.append((offset + second, value_c))
            samples.sort()
            if len(samples) < 2:
                reason = "fewer than 2 usable readings"
            elif samples[-1][0] == samples[0][0]:
                reason = "readings need distinct times"
        if reason is None:
            times = [sample[0] for sample in samples]
            values = [sample[1] for sample in samples]
            logs = [math.log(value - amb) for value in values]
            mean_time, mean_log = math.fsum(times) / len(times), math.fsum(logs) / len(logs)
            centered = [time - mean_time for time in times]
            variance = math.fsum(time ** 2 for time in centered)
            slope = (math.fsum(time * (log - mean_log) for time, log in zip(centered, logs))
                     / variance) if variance else math.nan
            if not math.isfinite(slope) or slope >= 0:
                reason = "readings did not cool"
            else:
                tau = -1.0 / slope
                intercept = mean_log - slope * mean_time
                try:
                    t0 = amb + math.exp(intercept)
                except OverflowError:
                    t0 = math.inf
                if not math.isfinite(tau) or not math.isfinite(t0):
                    reason = "cooling fit is not finite"
                else:
                    predicted = [amb + math.exp(mean_log + slope * time) for time in centered]
                    rms = math.sqrt(math.fsum((value - pred) ** 2 for value, pred in zip(values, predicted))
                                    / len(values))
                    used.append({"layer": row.get("layer"), "pass": row.get("pass"),
                                 "tau_s": tau, "t0_c": t0, "rms_c": rms,
                                 "count": len(samples), "park_travel_s_est": float(offset),
                                 "first_c": float(values[0]), "last_c": float(values[-1])})
        if reason is not None:
            skipped.append({"layer": row.get("layer"), "pass": row.get("pass"), "reason": reason})
            notes.append(f"{label}: {reason}")
    if not used:
        raise ValueError("Decay fit needs at least 1 usable row with 2 distinct readings "
                         f"that cool above ambient; 0 usable so far. {'; '.join(notes)}")
    taus = [row["tau_s"] for row in used]
    tau = float(median(taus))
    spread = max(taus) / min(taus)
    rms = math.sqrt(sum(row["rms_c"] ** 2 * row["count"] for row in used)
                    / sum(row["count"] for row in used))
    quality = []
    if rms > MAX_FIT_RMS_C:
        quality.append(f"RMS residual {rms:.1f} C is above {MAX_FIT_RMS_C:g} C")
    if spread > MAX_TAU_SPREAD:
        quality.append(f"per-row tau varies {spread:.1f}x (limit {MAX_TAU_SPREAD:g}x)")
    return {"basis": "layer_decay", "tau_s": tau, "tau_last_s": used[-1]["tau_s"],
            "ambient_c": amb, "count": len(used), "tau_spread": spread,
            "spread_fraction": (max(taus) - min(taus)) / tau, "rms_c": rms,
            "valid": not quality, "notes": notes + quality, "rows": used, "used": used,
            "skipped": skipped}


def propose_dwell(fit: dict, *, start_c: float, setpoint_c: float, after_c: float | None = None) -> dict:


    amb = fit["ambient_c"]
    if not all(math.isfinite(v) for v in (start_c, setpoint_c)):
        raise ValueError("Temperatures must be finite")
    if setpoint_c - amb < MIN_EXCESS_C:
        raise ValueError(f"The setpoint must be at least {MIN_EXCESS_C:g} C above ambient "
                         f"({amb:g} C): Newton cooling only approaches ambient")
    basis = fit.get("basis", "cooling_pairs")
    tau = fit["tau_s"] if basis == "layer_decay" else max(fit["tau_s"], fit["tau_last_s"])
    notes = list(fit["notes"])

    def wait(t0):
        return tau * math.log((t0 - amb) / (setpoint_c - amb)) if t0 > setpoint_c else 0.0

    raw = wait(start_c)
    seconds = min(max(math.ceil(raw), MIN_PROPOSED_S), MAX_PROPOSED_S)
    if raw <= 0:
        notes.append(f"T-before {start_c:.0f} C is already at or below the setpoint")
    elif raw > MAX_PROPOSED_S:
        notes.append(f"needs {raw:.0f} s; clamped to {MAX_PROPOSED_S:g} s, so expect a hold")
    result = {"seconds": float(seconds), "raw_s": raw, "tau_used_s": tau, "start_c": start_c,
              "setpoint_c": setpoint_c, "notes": notes, "valid": fit["valid"], "basis": basis}
    if after_c is not None and math.isfinite(after_c):
        result["hold_s"] = float(math.ceil(wait(after_c)))
    return result
