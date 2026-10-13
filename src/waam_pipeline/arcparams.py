'Arc parameters from bead geometry, per filler.'


from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple


@dataclass(frozen=True)
class Filler:


    key: str
    label: str
    density_g_mm3: float
    diameters_mm: tuple[float, ...]
    volts_range: tuple[float, float]


    amps_per_m_min_per_mm: float
    note: str = ""


    interpass_max_c: float | None = None


FILLERS: dict[str, Filler] = {
    "er70s6": Filler(
        key="er70s6", label="ER70S-6 (mild steel)",
        density_g_mm3=7.85e-3, diameters_mm=(0.889, 0.9, 1.0, 1.143, 1.2, 1.6),
        volts_range=(18.0, 26.0), amps_per_m_min_per_mm=33.0,
        note="Mild-steel filler. Validate parameters with a weld trial.",
        interpass_max_c=150.0,
    ),
    "er308l": Filler(
        key="er308l", label="ER308L (304/304L stainless)",
        density_g_mm3=7.90e-3, diameters_mm=(0.889, 0.9, 1.0, 1.143, 1.2),
        volts_range=(18.0, 26.0), amps_per_m_min_per_mm=31.0,
        note="Lower thermal conductivity than steel: heat accumulates faster, "
             "so interpass control matters more.",
        interpass_max_c=100.0,
    ),
    "er316l": Filler(
        key="er316l", label="ER316L (316/316L stainless)",
        density_g_mm3=7.98e-3, diameters_mm=(0.889, 0.9, 1.0, 1.143, 1.2),
        volts_range=(18.0, 26.0), amps_per_m_min_per_mm=31.0,
        interpass_max_c=100.0,
    ),
    "er2209": Filler(
        key="er2209", label="ER2209 (duplex 22Cr)",
        density_g_mm3=7.80e-3, diameters_mm=(1.0, 1.2),
        volts_range=(19.0, 27.0), amps_per_m_min_per_mm=31.0,
        note="Ferrite balance depends on heat input. Verify the process limits "
             "for this alloy.",
    ),
    "er5356": Filler(
        key="er5356", label="ER5356 (5xxx aluminium)",
        density_g_mm3=2.66e-3, diameters_mm=(1.0, 1.2, 1.6),
        volts_range=(19.0, 28.0), amps_per_m_min_per_mm=12.0,
        note="Feeds far faster for the same deposited mass; push-pull or a "
             "short, straight conduit is usually required.",
    ),
    "ernicr3": Filler(
        key="ernicr3", label="ERNiCr-3 (Inconel 82)",
        density_g_mm3=8.44e-3, diameters_mm=(0.9, 1.2),
        volts_range=(20.0, 28.0), amps_per_m_min_per_mm=30.0,
        note="Bead width depends on the alloy. Use measured bead dimensions.",
    ),
}

DEFAULT_FILLER = "er70s6"


INCH = 25.4
WIRE_045_MM = round(0.045 * INCH, 3)
WIRE_035_MM = round(0.035 * INCH, 3)
DEFAULT_WIRE_DIA_MM = WIRE_045_MM


def wire_area_mm2(diameter_mm: float) -> float:
    if not math.isfinite(diameter_mm) or diameter_mm <= 0:
        raise ValueError("wire diameter must be finite and positive")
    return math.pi * (float(diameter_mm) / 2.0) ** 2


def _electrode_efficiency(value: float) -> float:


    if not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("electrode efficiency must be finite, greater than 0 and at most 1")
    return float(value)


@dataclass(frozen=True)
class ArcSolution:
    filler: str
    filler_label: str
    wire_dia_mm: float
    layer_height_mm: float
    bead_width_mm: float
    travel_mm_min: float
    volume_rate_mm3_min: float
    wfs_m_min: float
    wfs_ipm: float
    deposition_kg_h: float
    current_estimate_a: float
    volts_suggested: float
    electrode_efficiency: float = 1.0
    fill_width_mm: float | None = None

    @property
    def bead_area_mm2(self) -> float:

        return self.volume_rate_mm3_min / self.travel_mm_min

    @property
    def wire_consumption_kg_h(self) -> float:

        return self.deposition_kg_h / self.electrode_efficiency

    def heat_input_kj_mm(self, volts: float | None = None,
                         amps: float | None = None,
                         efficiency: float = 1.0) -> float:


        v = float(volts if volts is not None else self.volts_suggested)
        i = float(amps if amps is not None else self.current_estimate_a)
        if not math.isfinite(efficiency) or not 0 <= efficiency <= 1:
            raise ValueError("thermal efficiency must be finite and between 0 and 1")
        if not all(math.isfinite(x) and x > 0 for x in (v, i, self.travel_mm_min)):
            raise ValueError("volts, amps and travel must be finite and positive")
        return efficiency * v * i * 60.0 / (self.travel_mm_min * 1000.0)


GMAW_ARC_EFFICIENCY = 0.8


def solve(
    *,
    layer_height_mm: float,
    bead_width_mm: float,
    travel_mm_min: float,
    wire_dia_mm: float = DEFAULT_WIRE_DIA_MM,
    filler: str = DEFAULT_FILLER,
    electrode_efficiency: float = 1.0,
    fill_width_mm: float | None = None,
) -> ArcSolution:


    f = FILLERS.get(filler)
    if f is None:
        raise ValueError(f"unknown filler {filler!r}; have {sorted(FILLERS)}")
    fill = bead_width_mm if fill_width_mm is None else fill_width_mm
    for name, val in (("layer_height_mm", layer_height_mm),
                      ("bead_width_mm", bead_width_mm),
                      ("fill_width_mm", fill),
                      ("travel_mm_min", travel_mm_min)):
        if not math.isfinite(float(val)) or float(val) <= 0:
            raise ValueError(f"{name} must be finite and positive")

    area = wire_area_mm2(wire_dia_mm)
    vol_rate = float(layer_height_mm) * float(fill) * float(travel_mm_min)
    eta = _electrode_efficiency(electrode_efficiency)
    wfs = vol_rate / (area * eta)
    wfs_m = wfs / 1000.0
    amps = f.amps_per_m_min_per_mm * wfs_m * float(wire_dia_mm)
    volts = f.volts_range[0] + (f.volts_range[1] - f.volts_range[0]) * min(
        1.0, max(0.0, (amps - 100.0) / 200.0)
    )
    return ArcSolution(
        filler=f.key,
        filler_label=f.label,
        wire_dia_mm=float(wire_dia_mm),
        layer_height_mm=float(layer_height_mm),
        bead_width_mm=float(bead_width_mm),
        travel_mm_min=float(travel_mm_min),
        volume_rate_mm3_min=vol_rate,
        wfs_m_min=wfs_m,
        wfs_ipm=wfs / 25.4,
        deposition_kg_h=vol_rate * f.density_g_mm3 * 60.0 / 1000.0,
        current_estimate_a=amps,
        volts_suggested=volts,
        electrode_efficiency=eta,
        fill_width_mm=float(fill),
    )


def warnings(sol: ArcSolution) -> list[str]:

    out: list[str] = []
    f = FILLERS[sol.filler]
    if sol.wire_dia_mm not in f.diameters_mm:
        out.append(
            f"{sol.wire_dia_mm:g} mm is not a catalogued diameter for "
            f"{f.label} ({', '.join(f'{d:g}' for d in f.diameters_mm)})"
        )
    if sol.wfs_m_min > 18.0:
        out.append(f"wire feed {sol.wfs_m_min:.1f} m/min is beyond most feeders "
                   "- widen the bead, slow the travel, or use thicker wire")
    elif sol.wfs_m_min < 1.5:
        out.append(f"wire feed {sol.wfs_m_min:.1f} m/min is very low - the arc "
                   "may not stay stable")
    if sol.current_estimate_a > 320:
        out.append(f"~{sol.current_estimate_a:.0f} A estimated: check the torch "
                   "and duty cycle before running this hot")
    ratio = sol.bead_width_mm / sol.layer_height_mm
    if ratio < 2.0:
        out.append(f"bead {sol.bead_width_mm:g} x {sol.layer_height_mm:g} mm is "
                   f"tall and narrow (w/h {ratio:.1f}) - WAAM beads usually sit "
                   "nearer 3-5 and this shape tends to slump")
    if f.note:
        out.append(f.note)
    return out


_STEPOVER_A = 0.07548
_STEPOVER_B = -0.3635
_STEPOVER_C = -2.154
_STEPOVER_D = 0.6169


STEPOVER_WHR_RANGE = (2.4, 20.0)


def optimal_stepover_fraction(bead_width_mm: float, bead_height_mm: float) -> float:


    if bead_width_mm <= 0 or bead_height_mm <= 0:
        raise ValueError(
            f"bead width and height must be positive, got {bead_width_mm} x {bead_height_mm}")
    whr = bead_width_mm / bead_height_mm
    lo, hi = STEPOVER_WHR_RANGE
    if not lo <= whr <= hi:
        raise ValueError(
            f"width/height {whr:.2f} is outside the fitted range {lo}-{hi} "
            f"(Hu et al. 2019 Eq. 13; it has a pole at 2.154 and must not be "
            f"extrapolated). Measure the bead, or fill with a different strategy."
        )
    return _STEPOVER_A * whr ** _STEPOVER_B / (whr + _STEPOVER_C) + _STEPOVER_D


def stepover_mm(bead_width_mm: float, bead_height_mm: float) -> float:

    return optimal_stepover_fraction(bead_width_mm, bead_height_mm) * bead_width_mm


def overlap_percent(stepover_fraction: float) -> float:

    return (1.0 - stepover_fraction) * 100.0


def stepover_fraction_from_overlap_percent(overlap_pct: float) -> float:

    return 1.0 - overlap_pct / 100.0


class StepoverBand(NamedTuple):


    nominal: float
    low: float
    high: float
    truncated: bool


def stepover_fraction_band(
    bead_width_mm: float,
    bead_height_mm: float,
    width_tol_mm: float,
    height_tol_mm: float,
) -> StepoverBand:


    if width_tol_mm < 0 or height_tol_mm < 0:
        raise ValueError("tolerances must be non-negative")
    nominal = optimal_stepover_fraction(bead_width_mm, bead_height_mm)
    corners: list[float] = []
    dropped = False
    for dw in (-width_tol_mm, width_tol_mm):
        for dh in (-height_tol_mm, height_tol_mm):
            try:
                corners.append(
                    optimal_stepover_fraction(bead_width_mm + dw, bead_height_mm + dh))
            except ValueError:
                dropped = True
    if not corners:
        return StepoverBand(nominal, nominal, nominal, True)
    return StepoverBand(nominal, min(min(corners), nominal),
                        max(max(corners), nominal), dropped)


STEPOVER_FRACTION_RANGE = (0.6317, 0.7733)
STEPOVER_TYPICAL_SPAN = STEPOVER_FRACTION_RANGE[1] - STEPOVER_FRACTION_RANGE[0]


WALL_MAX_BEADS = 50


CROWN_PER_PITCH = 1.5


def crown_from_pitch(layer_pitch_mm: float) -> float:

    if not (math.isfinite(layer_pitch_mm) and layer_pitch_mm > 0):
        raise ValueError("layer pitch must be finite and positive")
    return CROWN_PER_PITCH * float(layer_pitch_mm)


def stepover_target(bead_width_mm: float, layer_pitch_mm: float) -> tuple[float, str]:


    w = float(bead_width_mm)
    if not (math.isfinite(w) and w > 0):
        raise ValueError("bead width must be finite and positive")
    crown = crown_from_pitch(layer_pitch_mm)
    whr, (lo, hi) = w / crown, STEPOVER_WHR_RANGE
    if lo <= whr <= hi:
        return optimal_stepover_fraction(w, crown), ""
    edge = lo if whr < lo else hi
    target = optimal_stepover_fraction(edge, 1.0)
    note = (f"A {w:g} mm wide bead at {layer_pitch_mm:g} mm pitch has a {crown:g} mm crown "
            f"(1.5 x pitch, parabolic assumption), w/h {whr:.2f}, outside the overlap model's "
            f"w/h {lo:g}-{hi:g} (Hu et al. 2019), so the spacing aims at its w/h {edge:g} value, "
            f"{target:.0%} of the bead width.")
    if whr < lo:
        note += " Mueller & Hensel 2023 saw lack of fusion between beads below w/h 2.4."
    return target, note


class WallBeads(NamedTuple):
    count: int
    spacing_mm: float
    fraction: float
    optimum: float
    note: str = ""


def wall_beads(thickness_mm: float, bead_width_mm: float, layer_pitch_mm: float) -> WallBeads:


    t, w, h = float(thickness_mm), float(bead_width_mm), float(layer_pitch_mm)
    if not all(math.isfinite(v) and v > 0 for v in (t, w, h)):
        raise ValueError("Wall thickness, bead width and bead height must be positive")
    optimum, model_note = stepover_target(w, h)
    notes = [model_note] if model_note else []
    if t < w or math.isclose(t, w):
        if not math.isclose(t, w):
            notes.append(f"{t:g} mm is thinner than one {w:g} mm bead: one bead is laid.")
        return WallBeads(1, 0.0, 0.0, float("nan"), " ".join(notes))
    lo, hi = STEPOVER_FRACTION_RANGE
    gap = t - w
    first = max(1, math.ceil(gap / (hi * w) - 1e-9))
    last = math.floor(gap / (lo * w) + 1e-9)
    if first <= last:
        if first + 1 > WALL_MAX_BEADS:
            raise ValueError(f"Wall thickness: {first + 1} beads per layer is more than {WALL_MAX_BEADS}. "
                             "Use wider beads or a thinner wall")
        candidates = list(range(first, min(last, WALL_MAX_BEADS - 1) + 1))
    else:
        ideal = gap / (optimum * w)
        candidates = sorted({max(1, math.floor(ideal)), max(1, math.ceil(ideal))})
    allowed = [n for n in candidates if n + 1 <= WALL_MAX_BEADS] or candidates
    overlapping = [n for n in allowed if gap / n < w]
    gaps = min(overlapping or allowed, key=lambda n: abs(gap / n / w - optimum))
    if gaps + 1 > WALL_MAX_BEADS:
        raise ValueError(f"Wall thickness: {gaps + 1} beads per layer is more than {WALL_MAX_BEADS}. "
                         "Use wider beads or a thinner wall")
    fraction = gap / gaps / w
    if fraction >= 1:
        notes.append(f"{gaps + 1} beads {gap / gaps:.2f} mm apart do not overlap ({fraction:.0%} of the "
                     "bead width): expect a gap and lack of fusion between them.")
    elif not lo <= fraction <= hi:
        effect = ("tighter, so expect a ridged rather than flat layer top (the per-bead feed "
                  "still averages the layer at its pitch)" if fraction < lo
                  else "wider, so expect a valley, or lack of fusion, between beads")
        notes.append(f"{gaps + 1} beads {gap / gaps:.2f} mm apart is {fraction:.0%} of the bead width, "
                     f"outside the model's {lo:.0%}-{hi:.0%}: {effect}.")
    return WallBeads(gaps + 1, gap / gaps, fraction, optimum, " ".join(notes))


def wall_bead_area_mm2(thickness_mm: float, layer_pitch_mm: float, beads: int = 1) -> float:


    t, lh = float(thickness_mm), float(layer_pitch_mm)
    if not all(math.isfinite(v) and v > 0 for v in (t, lh)):
        raise ValueError("wall thickness and layer pitch must be finite and positive")
    return t * lh / _beads_across(beads)


def _beads_across(beads) -> int:
    if isinstance(beads, bool) or not isinstance(beads, int) or beads < 1:
        raise ValueError("beads across must be a whole number of at least 1")
    return beads


def predicted_layer_pitch_mm(bead_area_mm2: float, thickness_mm: float, beads: int = 1) -> float:


    if not all(math.isfinite(v) and v > 0 for v in (bead_area_mm2, thickness_mm)):
        raise ValueError("bead area and wall thickness must be finite and positive")
    return bead_area_mm2 * _beads_across(beads) / thickness_mm


def ornl_spacing_note(bead_width_mm: float, layer_pitch_mm: float, stepover_percent: float) -> str:


    target, model_note = stepover_target(bead_width_mm, layer_pitch_mm)
    note = (f"Hu et al. 2019 suggests {target:.0%} bead spacing for this bead "
            f"(crown = 1.5 x Bead height, parabolic assumption); ORNL is spacing at "
            f"{stepover_percent:g}%.")
    return f"{note} {model_note}" if model_note else note


PITCH_NOTE_FRACTION = 0.10


def pitch_note(predicted_mm: float, layer_pitch_mm: float) -> str:


    if not all(math.isfinite(v) and v > 0 for v in (predicted_mm, layer_pitch_mm)):
        raise ValueError("predicted and planned layer pitch must be finite and positive")
    off = predicted_mm / layer_pitch_mm - 1.0
    if abs(off) <= PITCH_NOTE_FRACTION:
        return ""
    drift = ctwd_drift_per_layer(layer_pitch_mm, predicted_mm)
    effect = (f"the contact tip closes on the part by about {-drift:.2f} mm per layer" if drift < 0
              else f"CTWD grows by about {drift:.2f} mm per layer")
    return (f"This feed builds about {predicted_mm:.2f} mm per layer, {abs(off):.0%} "
            f"{'above' if off > 0 else 'below'} the {layer_pitch_mm:g} mm Bead height the torch "
            f"steps by: {effect}. Touch a layer, or change the wire feed or Bead height.")


def deposited_area_mm2(wire_dia_mm: float, wfs_m_min: float,
                       travel_mm_min: float, *, electrode_efficiency: float = 1.0) -> float:


    if not all(math.isfinite(x) and x > 0 for x in (wire_dia_mm, wfs_m_min, travel_mm_min)):
        raise ValueError("wire diameter, wire feed and travel must all be finite and positive")
    return (wire_area_mm2(wire_dia_mm) * (wfs_m_min * 1000.0) / travel_mm_min
            * _electrode_efficiency(electrode_efficiency))


def free_bead_area_mm2(bead_width_mm: float, bead_height_mm: float) -> float:


    if bead_width_mm <= 0 or bead_height_mm <= 0:
        raise ValueError("bead width and height must be positive")
    return (2.0 / 3.0) * bead_width_mm * bead_height_mm


def layer_height_for_area(area_mm2: float, wall_width_mm: float) -> float:

    if area_mm2 <= 0 or wall_width_mm <= 0:
        raise ValueError("area and wall width must be positive")
    return area_mm2 / wall_width_mm


def wfs_for_layer_height(layer_height_mm: float, wall_width_mm: float,
                         travel_mm_min: float,
                         wire_dia_mm: float = DEFAULT_WIRE_DIA_MM, *,
                         electrode_efficiency: float = 1.0) -> float:


    if not all(math.isfinite(x) and x > 0 for x in (layer_height_mm, wall_width_mm, travel_mm_min)):
        raise ValueError(
            "layer height, wall width and travel must all be positive")
    area = layer_height_mm * wall_width_mm
    return (area * travel_mm_min / wire_area_mm2(wire_dia_mm) / 1000.0
            / _electrode_efficiency(electrode_efficiency))


def compensating_wfs(wfs_m_min: float, width_before_mm: float,
                     width_after_mm: float) -> float:


    if min(wfs_m_min, width_before_mm, width_after_mm) <= 0:
        raise ValueError("wire feed and both widths must be positive")
    return wfs_m_min * width_after_mm / width_before_mm


CTWD_WIDTH_SENSITIVITY_MM_PER_MM: float | None = None


def width_for_ctwd(width_mm: float, ctwd_change_mm: float) -> float:


    if CTWD_WIDTH_SENSITIVITY_MM_PER_MM is None:
        raise ValueError(
            "dw/d(CTWD) has never been measured on this cell, and no source in "
            "docs/acquisition-list.md gives it as a fitted slope. One study DOES vary "
            "CTWD on ER70S-6 in short-circuit (Weld World 2026, doi 10.1007/s40194-026-02385-3) and reports NEGLIGIBLE bead-width change from "
            "12 to 20 mm -- if that holds here, this correction is near zero. "
            "Measure it before closing a CTWD loop; see "
            "width_for_ctwd.__doc__ for the experiment."
        )
    return width_mm + CTWD_WIDTH_SENSITIVITY_MM_PER_MM * ctwd_change_mm


def ctwd_drift_per_layer(commanded_z_step_mm: float, actual_layer_mm: float) -> float:


    if commanded_z_step_mm <= 0 or actual_layer_mm <= 0:
        raise ValueError("commanded step and actual layer height must be positive")
    return commanded_z_step_mm - actual_layer_mm


def cumulative_ctwd_mm(nominal_ctwd_mm: float, commanded_z_step_mm: float,
                       actual_layers_mm) -> list[float]:


    if nominal_ctwd_mm <= 0:
        raise ValueError("nominal CTWD must be positive")
    out: list[float] = []
    ctwd = float(nominal_ctwd_mm)
    for h in actual_layers_mm:
        ctwd += ctwd_drift_per_layer(commanded_z_step_mm, float(h))
        out.append(ctwd)
    return out


def surface_deviation_from_corrections(z_corrections_mm) -> list[float]:


    out: list[float] = []
    total = 0.0
    for c in z_corrections_mm:
        total += float(c)
        out.append(total)
    return out


def calibrated_layer_height_mm(nominal_layer_mm: float,
                               measured_total_height_mm: float,
                               layers_deposited: int) -> float:


    if nominal_layer_mm <= 0 or measured_total_height_mm <= 0:
        raise ValueError("nominal layer height and measured height must be positive")
    if layers_deposited < 1:
        raise ValueError("layers_deposited must be at least 1")
    return measured_total_height_mm / layers_deposited


def residual_drift_mm(nominal_layer_mm: float, actual_layer_mm: float,
                      layers: int) -> float:


    if layers < 0:
        raise ValueError("layers must not be negative")
    return ctwd_drift_per_layer(nominal_layer_mm, actual_layer_mm) * layers


CTWD_CURRENT_SENSITIVITY_A_PER_MM: float | None = None


def ctwd_from_current(measured_current_a: float, reference_current_a: float,
                      reference_ctwd_mm: float) -> float:


    if measured_current_a <= 0 or reference_current_a <= 0:
        raise ValueError("currents must be positive")
    if reference_ctwd_mm <= 0:
        raise ValueError("reference CTWD must be positive")
    if CTWD_CURRENT_SENSITIVITY_A_PER_MM is None:
        raise ValueError(
            "dI/d(CTWD) has never been measured on this cell. Current is the "
            "only continuous CTWD observable we have, and its slope is not in "
            "any source we hold as a slope. Measure "
            "it before closing a through-the-arc loop; see "
            "ctwd_from_current.__doc__."
        )
    return reference_ctwd_mm + (
        (measured_current_a - reference_current_a) / CTWD_CURRENT_SENSITIVITY_A_PER_MM
    )


NARROW_CURRENT_SPAN = 0.35


LESNEWICH_A: float | None = None
LESNEWICH_B: float | None = None


WASZINK_1982_MILD_STEEL_1_2MM = (0.263 * 0.06, 4.58e-5 * 0.06)


CHERNOVOL_2022_1_2MM = [(2.9, 123.0), (3.8, 160.0), (4.6, 181.0)]


CHERNOVOL_ASSUMED_L_MM = 11.0


def _lesnewich_coeffs(a, b):
    a = LESNEWICH_A if a is None else a
    b = LESNEWICH_B if b is None else b
    if a is None or b is None:
        raise ValueError(
            "the Lesnewich melting-rate coefficients a and b have not been "
            "measured on this cell, and the published data we hold cannot "
            "supply them (see fit_lesnewich.__doc__). Fit them from a current "
            "sweep at two or more stickouts, or pass a= and b= explicitly."
        )
    if not all(math.isfinite(x) and x > 0 for x in (a, b)):
        raise ValueError("a and b are finite and positive in Lesnewich's model")
    return float(a), float(b)


def melting_rate_m_min(current_a: float, stickout_mm: float,
                       a: float | None = None, b: float | None = None) -> float:

    if not all(math.isfinite(x) and x > 0 for x in (current_a, stickout_mm)):
        raise ValueError("current and stickout must be finite and positive")
    a, b = _lesnewich_coeffs(a, b)
    return a * current_a + b * stickout_mm * current_a ** 2


def current_for_wfs(wfs_m_min: float, stickout_mm: float,
                    a: float | None = None, b: float | None = None) -> float:


    if not all(math.isfinite(x) and x > 0 for x in (wfs_m_min, stickout_mm)):
        raise ValueError("wire feed and stickout must be finite and positive")
    a, b = _lesnewich_coeffs(a, b)
    q = b * stickout_mm


    return 2.0 * wfs_m_min / (a + math.sqrt(a * a + 4.0 * q * wfs_m_min))


def d_current_d_stickout(wfs_m_min: float, stickout_mm: float,
                         a: float | None = None, b: float | None = None) -> float:


    a, b = _lesnewich_coeffs(a, b)
    i = current_for_wfs(wfs_m_min, stickout_mm, a, b)
    return -(b * i * i) / (a + 2.0 * b * stickout_mm * i)


def resistive_fraction(current_a: float, stickout_mm: float,
                       a: float | None = None, b: float | None = None) -> float:


    a, b = _lesnewich_coeffs(a, b)
    arc = a * current_a
    res = b * stickout_mm * current_a ** 2
    return res / (arc + res)


def fit_lesnewich(points, stickout_mm: float,
                  allow_narrow: bool = False) -> tuple[float, float, float]:


    pts = [(float(w), float(i)) for w, i in points]
    if len(pts) < 3:
        raise ValueError(
            f"need at least 3 (wfs, current) points to fit and check 2 "
            f"coefficients, got {len(pts)}")
    if stickout_mm <= 0:
        raise ValueError("stickout must be positive")
    currents = [i for _, i in pts]
    span = (max(currents) - min(currents)) / max(currents)
    if span < NARROW_CURRENT_SPAN and not allow_narrow:
        raise ValueError(
            f"current range {min(currents):.0f}-{max(currents):.0f} A spans only "
            f"{span * 100:.0f} percent of the maximum. Separating the arc term "
            f"from the resistive one needs a wide sweep; below about "
            f"{NARROW_CURRENT_SPAN * 100:.0f} percent the fit is dominated by "
            "measurement noise. Widen the current range, or pass "
            "allow_narrow=True if you are knowingly fitting somebody else's "
            "published table and will treat b as indicative only."
        )
    import numpy as np

    w = np.array([p[0] for p in pts])
    i = np.array([p[1] for p in pts])
    (a, c), *_ = np.linalg.lstsq(np.column_stack([i, i ** 2]), w, rcond=None)
    if a <= 0 or c <= 0:
        raise ValueError(
            f"fit returned a non-physical coefficient (a={a:.3e}, b*L={c:.3e}); "
            "both must be positive. Check the data and the current range.")
    pred = a * i + c * i ** 2
    ss_res = float(((w - pred) ** 2).sum())
    ss_tot = float(((w - w.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(a), float(c / stickout_mm), r2


ELECTRODE_EXTENSION_BAND_MM = {
    "short-circuit": (0.375 * INCH, 0.5 * INCH),
    "spray": (0.75 * INCH, INCH),
    "pulsed-spray": (0.75 * INCH, INCH),
}


def ctwd_warnings(ctwd_mm: float, current_a: float,
                  wire_dia_mm: float = DEFAULT_WIRE_DIA_MM, *,
                  observed_mode: str | None = None,
                  arc_length_mm: float | None = None) -> list[str]:


    if not all(math.isfinite(x) and x > 0 for x in (ctwd_mm, current_a, wire_dia_mm)):
        raise ValueError("CTWD, current and wire diameter must be finite and positive")
    if observed_mode is not None and observed_mode not in ELECTRODE_EXTENSION_BAND_MM:
        raise ValueError("observed mode must be short-circuit, spray, or pulsed-spray")
    if arc_length_mm is not None and (
            not math.isfinite(arc_length_mm) or not 0 < arc_length_mm < ctwd_mm):
        raise ValueError("measured arc length must be finite, positive and less than CTWD")
    if observed_mode is None or arc_length_mm is None:
        return ["CTWD is not validated: supply the observed transfer mode and measured "
                "arc length. Current alone cannot distinguish pulsed spray from "
                "short-circuit/globular; electrode extension is not CTWD."]
    lo, hi = ELECTRODE_EXTENSION_BAND_MM[observed_mode]
    extension = ctwd_mm - arc_length_mm
    out = ["Generic electrode-extension guidance only; not a qualified cell process window."]
    if extension < lo or extension > hi:
        side = "below" if extension < lo else "above"
        out.append(f"Electrode extension {extension:.3f} mm (CTWD minus arc length) "
                   f"is {side} the {lo:.3f}-{hi:.3f} mm guidance for {observed_mode}.")
    return out


D11_AMPERAGE_TOL = 0.10
D11_VOLTAGE_TOL = 0.07


D11_MIN_PREHEAT_C_CATEGORY_B_THIN = 0.0


def d11_amperage_band(nominal_a: float) -> tuple[float, float]:

    if nominal_a <= 0:
        raise ValueError("nominal amperage must be positive")
    return (nominal_a * (1 - D11_AMPERAGE_TOL), nominal_a * (1 + D11_AMPERAGE_TOL))


def ctwd_budget_mm(wfs_m_min: float, stickout_mm: float,
                   a: float | None = None, b: float | None = None,
                   tol: float = D11_AMPERAGE_TOL) -> float:


    i = current_for_wfs(wfs_m_min, stickout_mm, a, b)
    slope = d_current_d_stickout(wfs_m_min, stickout_mm, a, b)
    return abs(tol * i / slope)
