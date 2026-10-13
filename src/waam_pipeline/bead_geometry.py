from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from . import arcparams

if TYPE_CHECKING:
    from .toolpath import Move, Toolpath

__all__ = [
    "Provenance",
    "Profile",
    "PARABOLA",
    "CIRCULAR_ARC",
    "COSINE",
    "ELLIPSE",
    "PROFILES",
    "BeadCrossSection",
    "PathDatum",
    "bead_span",
    "SweptSegment",
    "SweptBead",
    "sweep_bead",
    "nominal_wall_bead",
    "BeadModelRegistry",
    "WETOUT_WIDTH_SENSITIVITY_MM_PER_C",
    "WETOUT_VALID_RANGE_C",
    "wet_out",
    "SectionFit",
    "fit_measured_section",
    "free_bead_report",
]


class Provenance(Enum):


    NOMINAL_ANALYTIC = "nominal-analytic"
    MEASURED = "measured"


def _clamp_height(t_mm: float, height_mm: float) -> float:


    if not math.isfinite(t_mm):
        raise ValueError(f"height query must be finite, got {t_mm}")
    eps = 1e-9 * (1.0 + abs(height_mm))
    if t_mm < -eps or t_mm > height_mm + eps:
        raise ValueError(f"height {t_mm} is outside [0, {height_mm}]")
    return min(height_mm, max(0.0, t_mm))


class Profile:


    name: str = "profile"


    area_coefficient: float | None = None

    def area(self, width_mm: float, height_mm: float) -> float:
        if self.area_coefficient is None:
            raise NotImplementedError
        return self.area_coefficient * width_mm * height_mm

    def crown(self, s_mm: float, width_mm: float, height_mm: float) -> float:

        raise NotImplementedError

    def half_width_at(self, t_mm: float, width_mm: float, height_mm: float) -> float:

        raise NotImplementedError

    def height_for_area(self, area_mm2: float, width_mm: float) -> float:

        if self.area_coefficient is None:
            raise NotImplementedError
        return area_mm2 / (self.area_coefficient * width_mm)


class _Parabola(Profile):
    name = "parabola"
    area_coefficient = 2.0 / 3.0

    def crown(self, s, w, h):
        if abs(s) > w / 2.0:
            return 0.0
        return h * (1.0 - (2.0 * s / w) ** 2)

    def half_width_at(self, t, w, h):
        t = _clamp_height(t, h)
        return (w / 2.0) * math.sqrt(max(0.0, 1.0 - t / h))


class _Cosine(Profile):
    name = "cosine"
    area_coefficient = 2.0 / math.pi

    def crown(self, s, w, h):
        if abs(s) > w / 2.0:
            return 0.0
        return h * math.cos(math.pi * s / w)

    def half_width_at(self, t, w, h):
        t = _clamp_height(t, h)
        return (w / math.pi) * math.acos(min(1.0, max(-1.0, t / h)))


class _Ellipse(Profile):


    name = "ellipse"
    area_coefficient = math.pi / 4.0

    def crown(self, s, w, h):
        if abs(s) > w / 2.0:
            return 0.0
        return h * math.sqrt(max(0.0, 1.0 - (2.0 * s / w) ** 2))

    def half_width_at(self, t, w, h):
        t = _clamp_height(t, h)
        return (w / 2.0) * math.sqrt(max(0.0, 1.0 - (t / h) ** 2))


class _CircularArc(Profile):


    name = "circular_arc"

    @staticmethod
    def _radius(w, h):


        if h > w / 2.0 + 1e-12:
            raise ValueError(
                f"a circular-arc bead needs height <= width/2 (got h={h}, w={w}); "
                "taller-than-semicircle is not a cap -- use the parabola or "
                "ellipse profile, or a wider bead")
        return (w * w + 4.0 * h * h) / (8.0 * h)

    def area(self, w, h):
        r = self._radius(w, h)
        beta = 2.0 * math.asin(min(1.0, (w / 2.0) / r))
        return 0.5 * r * r * (beta - math.sin(beta))

    def crown(self, s, w, h):
        r = self._radius(w, h)
        if abs(s) > w / 2.0:
            return 0.0
        s2 = s * s


        return h - s2 / (r + math.sqrt(max(0.0, r * r - s2)))

    def half_width_at(self, t, w, h):
        r = self._radius(w, h)
        t = _clamp_height(t, h)


        d = h - t
        return math.sqrt(max(0.0, d * (2.0 * r - d)))

    def height_for_area(self, area, w):

        max_area = self.area(w, w / 2.0)
        if area > max_area + 1e-9:
            raise ValueError(
                f"area {area:.3f} mm^2 cannot be a circular cap of width {w} mm "
                f"(a semicircle of that width holds only {max_area:.3f} mm^2). "
                "Widen the bead or use the parabola or ellipse profile.")
        lo, hi = 1e-9, w / 2.0
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if self.area(w, mid) < area:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)


PARABOLA = _Parabola()
CIRCULAR_ARC = _CircularArc()
COSINE = _Cosine()
ELLIPSE = _Ellipse()
PROFILES = {p.name: p for p in (PARABOLA, CIRCULAR_ARC, COSINE, ELLIPSE)}


def _positive(name: str, value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and > 0, got {value}")
    return float(value)


@dataclass(frozen=True)
class BeadCrossSection:


    width_mm: float
    height_mm: float
    area_mm2: float
    profile: Profile
    provenance: Provenance
    source: str

    def __post_init__(self) -> None:

        for name, v in (("width_mm", self.width_mm), ("height_mm", self.height_mm),
                        ("area_mm2", self.area_mm2)):
            if not math.isfinite(v) or v <= 0.0:
                raise ValueError(f"{name} must be finite and > 0, got {v}")
        if self.provenance is Provenance.MEASURED and not (self.source or "").strip():
            raise ValueError(
                "a MEASURED cross-section needs a non-empty source; build it via "
                "BeadCrossSection.measured() so the measurement is traceable")
        expected = self.profile.area(self.width_mm, self.height_mm)
        if not math.isclose(self.area_mm2, expected, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError(
                f"area {self.area_mm2:.6g} is inconsistent with a {self.profile.name} "
                f"of w={self.width_mm:.6g} h={self.height_mm:.6g} (={expected:.6g})")


    @classmethod
    def from_area_and_width(cls, area_mm2: float, width_mm: float, *,
                            profile: Profile = PARABOLA,
                            provenance: Provenance = Provenance.NOMINAL_ANALYTIC,
                            source: str = "") -> "BeadCrossSection":
        area_mm2 = _positive("area_mm2", area_mm2)
        width_mm = _positive("width_mm", width_mm)
        height = profile.height_for_area(area_mm2, width_mm)
        return cls(width_mm, height, area_mm2, profile, provenance,
                   source or _default_source(profile, provenance))

    @classmethod
    def from_width_and_height(cls, width_mm: float, height_mm: float, *,
                              profile: Profile = PARABOLA,
                              provenance: Provenance = Provenance.NOMINAL_ANALYTIC,
                              source: str = "") -> "BeadCrossSection":
        width_mm = _positive("width_mm", width_mm)
        height_mm = _positive("height_mm", height_mm)
        area = profile.area(width_mm, height_mm)
        return cls(width_mm, height_mm, area, profile, provenance,
                   source or _default_source(profile, provenance))

    @classmethod
    def from_area_and_aspect(cls, area_mm2: float, width_over_height: float, *,
                             profile: Profile = PARABOLA,
                             provenance: Provenance = Provenance.NOMINAL_ANALYTIC,
                             source: str = "") -> "BeadCrossSection":


        area_mm2 = _positive("area_mm2", area_mm2)
        r = _positive("width_over_height", width_over_height)
        k = profile.area_coefficient
        if k is None:
            raise ValueError(
                f"{profile.name} has no constant area coefficient; give width or "
                "height explicitly rather than an aspect ratio")
        width = math.sqrt(r * area_mm2 / k)
        return cls.from_width_and_height(width, width / r, profile=profile,
                                         provenance=provenance, source=source)

    @classmethod
    def measured(cls, width_mm: float, height_mm: float, *,
                 source: str, profile: Profile = PARABOLA) -> "BeadCrossSection":


        if not source or not source.strip():
            raise ValueError("a measured cross-section requires a non-empty source")
        return cls.from_width_and_height(width_mm, height_mm, profile=profile,
                                         provenance=Provenance.MEASURED,
                                         source=f"measured: {source}")


    @property
    def layer_pitch_mm(self) -> float:


        return self.area_mm2 / self.width_mm

    @property
    def outer_radius_mm(self) -> float:

        return self.width_mm / 2.0

    def half_width_at(self, height_above_bottom_mm: float) -> float:
        return self.profile.half_width_at(height_above_bottom_mm,
                                          self.width_mm, self.height_mm)

    def crown_at(self, transverse_offset_mm: float) -> float:
        return self.profile.crown(transverse_offset_mm, self.width_mm, self.height_mm)

    def outline(self, n: int = 33) -> list[tuple[float, float]]:

        if n < 3:
            raise ValueError("need at least 3 points")
        half = self.width_mm / 2.0
        return [(-half + i * self.width_mm / (n - 1),
                 self.crown_at(-half + i * self.width_mm / (n - 1)))
                for i in range(n)]

    def label(self) -> str:
        if self.provenance is Provenance.MEASURED:
            tag = f"{self.source}; {self.profile.name} shape assumed"
        else:
            tag = f"NOMINAL, unverified -- {self.profile.name}, {self.source}"
        return (f"bead w={self.width_mm:.3g} x h={self.height_mm:.3g} mm "
                f"(A={self.area_mm2:.3g} mm^2, pitch={self.layer_pitch_mm:.3g} mm) "
                f"[{tag}]")


def _default_source(profile: Profile, provenance: Provenance) -> str:
    if provenance is Provenance.MEASURED:
        return ""
    return "seeded from literature, not measured on this cell"


def nominal_wall_bead(*, layer_height_mm: float, bead_width_mm: float,
                      wire_dia_mm: float = arcparams.DEFAULT_WIRE_DIA_MM,
                      profile: Profile = PARABOLA) -> BeadCrossSection:


    layer_height_mm = _positive("layer_height_mm", layer_height_mm)
    bead_width_mm = _positive("bead_width_mm", bead_width_mm)
    wire_dia_mm = _positive("wire_dia_mm", wire_dia_mm)
    if abs(wire_dia_mm - arcparams.WIRE_035_MM) < 1e-3:
        raise ValueError(
            f"no nominal bead SHAPE is seeded for the {arcparams.WIRE_035_MM} mm "
            "(0.035 in) wire: the crown-shape literature we hold is ~0.045 in / "
            "1.2 mm carbon steel and the 0.035 in stainless process is "
            "unrepresented. Section a real bead and pass it to "
            "BeadCrossSection.measured().")
    area = layer_height_mm * bead_width_mm
    return BeadCrossSection.from_area_and_width(
        area, bead_width_mm, profile=profile,
        provenance=Provenance.NOMINAL_ANALYTIC,
        source=(f"wall nominal: layer {layer_height_mm:g} x width {bead_width_mm:g} mm, "
                f"{wire_dia_mm:g} mm wire"))


def free_bead_report(*, area_mm2: float, width_mm: float,
                     wire_dia_mm: float = arcparams.DEFAULT_WIRE_DIA_MM) -> dict:


    xs = nominal_wall_bead(layer_height_mm=_positive("area_mm2", area_mm2) / _positive("width_mm", width_mm),
                           bead_width_mm=width_mm, wire_dia_mm=wire_dia_mm)
    return {"area_mm2": xs.area_mm2, "width_mm": xs.width_mm, "crown_mm": xs.height_mm,
            "pitch_mm": xs.layer_pitch_mm, "profile": xs.profile.name,
            "provenance": "NOMINAL", "label": xs.label()}


class PathDatum(Enum):


    TOP = "top"
    BOTTOM = "bottom"
    MIDHEIGHT = "midheight"


def bead_span(path_z_mm: float, height_mm: float,
              datum: PathDatum = PathDatum.BOTTOM) -> tuple[float, float]:

    if not math.isfinite(path_z_mm):
        raise ValueError(f"path_z_mm must be finite, got {path_z_mm}")
    height_mm = _positive("height_mm", height_mm)
    if datum is PathDatum.TOP:
        return (path_z_mm - height_mm, path_z_mm)
    if datum is PathDatum.BOTTOM:
        return (path_z_mm, path_z_mm + height_mm)
    if datum is PathDatum.MIDHEIGHT:
        return (path_z_mm - height_mm / 2.0, path_z_mm + height_mm / 2.0)
    raise ValueError(f"unknown PathDatum {datum!r}; pass a PathDatum member")


@dataclass(frozen=True)
class SweptSegment:


    start: tuple[float, float, float]
    end: tuple[float, float, float]
    cross_section: BeadCrossSection
    start_span: tuple[float, float]
    end_span: tuple[float, float]

    @property
    def bottom_z(self) -> float:
        return min(self.start_span[0], self.end_span[0])

    @property
    def top_z(self) -> float:
        return max(self.start_span[1], self.end_span[1])

    @property
    def length_mm(self) -> float:
        return math.dist(self.start, self.end)

    def clearance_capsule(self) -> dict:


        r = self.cross_section.outer_radius_mm
        z0 = 0.5 * (self.start_span[0] + self.start_span[1])
        z1 = 0.5 * (self.end_span[0] + self.end_span[1])
        p0 = (self.start[0], self.start[1], z0)
        p1 = (self.end[0], self.end[1], z1)
        return {"radius_mm": r, "axis": (p0, p1)}


@dataclass(frozen=True)
class SweptBead:


    cross_section: BeadCrossSection
    datum: PathDatum
    segments: list[SweptSegment]

    def bounds_z(self) -> tuple[float, float]:
        if not self.segments:
            raise ValueError("no deposit segments were swept")
        return (min(s.bottom_z for s in self.segments),
                max(s.top_z for s in self.segments))


def sweep_bead(toolpath: "Toolpath", cross_section: BeadCrossSection, *,
               datum: PathDatum = PathDatum.BOTTOM) -> SweptBead:


    moves: list["Move"] = toolpath.moves
    segments: list[SweptSegment] = []
    for i, m in enumerate(moves):
        if not m.deposit or i == 0:
            continue
        prev = moves[i - 1]

        b0, t0 = bead_span(prev.z, cross_section.height_mm, datum)
        b1, t1 = bead_span(m.z, cross_section.height_mm, datum)
        segments.append(SweptSegment(
            start=prev.target, end=m.target, cross_section=cross_section,
            start_span=(b0, t0), end_span=(b1, t1)))
    if not segments:
        raise ValueError(
            "toolpath has no welding segment to sweep (a lone first-move deposit "
            "has no start point); nothing to build a bead on")
    return SweptBead(cross_section, datum, segments)


class BeadModelRegistry:


    def __init__(self) -> None:
        self._by_key: dict[str, BeadCrossSection] = {}

    def set(self, key: str, xsec: BeadCrossSection) -> None:
        prev = self._by_key.get(key)
        if (prev is not None and prev.provenance is Provenance.MEASURED
                and xsec.provenance is not Provenance.MEASURED):
            raise ValueError(
                f"refusing to replace the MEASURED bead for {key!r} with a "
                f"{xsec.provenance.value} one: DOE data supersedes the nominal "
                "seed one way only")
        self._by_key[key] = xsec

    def get(self, key: str) -> BeadCrossSection | None:
        return self._by_key.get(key)

    def resolve(self, key: str, fallback: BeadCrossSection) -> BeadCrossSection:

        return self._by_key.get(key, fallback)

    def keys(self) -> Iterable[str]:
        return self._by_key.keys()


WETOUT_WIDTH_SENSITIVITY_MM_PER_C: float | None = None


WETOUT_VALID_RANGE_C: tuple[float, float] | None = None


def wet_out(base: BeadCrossSection, interpass_temp_c: float,
            reference_temp_c: float) -> BeadCrossSection:


    if not isinstance(base, BeadCrossSection):
        raise TypeError("base must be a BeadCrossSection")
    for name, value in (("interpass_temp_c", interpass_temp_c),
                        ("reference_temp_c", reference_temp_c)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value}")
    if WETOUT_WIDTH_SENSITIVITY_MM_PER_C is None:
        raise ValueError(
            "dw/d(interpass_T) has never been measured on this cell. The wet-out "
            "DIRECTION is known (higher interpass -> wider, shorter bead on mild "
            "steel; docs/adaptive-interpass.md) but the SLOPE is not, and no "
            "source we hold gives it for ER70S-6 / 82-18. Section beads at "
            "controlled interpass temperatures on this cell, set "
            "WETOUT_WIDTH_SENSITIVITY_MM_PER_C, then re-run. Do not guess it.")
    if WETOUT_VALID_RANGE_C is not None:
        lo, hi = WETOUT_VALID_RANGE_C
        if not lo <= interpass_temp_c <= hi:
            raise ValueError(
                f"interpass {interpass_temp_c:g} C is outside the measured wet-out "
                f"range [{lo:g}, {hi:g}] C; the linear model must not extrapolate")
    new_width = base.width_mm + WETOUT_WIDTH_SENSITIVITY_MM_PER_C * (
        interpass_temp_c - reference_temp_c)
    if new_width <= 0.0:
        raise ValueError(
            f"wet-out produced a non-positive width ({new_width:.3f} mm) at "
            f"interpass {interpass_temp_c:g} vs reference {reference_temp_c:g} C; "
            "the linear model has left its physical range")


    return BeadCrossSection.from_area_and_width(
        base.area_mm2, new_width, profile=base.profile,
        provenance=Provenance.NOMINAL_ANALYTIC,
        source=(f"wet-out to interpass {interpass_temp_c:g} C (ref "
                f"{reference_temp_c:g} C) of [{base.source}]"))


@dataclass(frozen=True)
class SectionFit:


    width_mm: float
    height_mm: float
    measured_area_mm2: float
    residuals_mm: dict
    best_profile: Profile
    cross_section: BeadCrossSection
    deposited_area_mm2: float | None
    crown_area_ratio: float | None

    def report(self) -> str:
        ranked = ", ".join(f"{n} {r:.3f}" for n, r in
                           sorted(self.residuals_mm.items(), key=lambda kv: kv[1]))
        extra = ""
        if self.deposited_area_mm2 is not None:
            extra = (f"; crown area {self.measured_area_mm2:.2f} vs mass-balance "
                     f"{self.deposited_area_mm2:.2f} mm^2 (ratio "
                     f"{self.crown_area_ratio:.2f}, ~retention not dilution)")
        return (f"best fit {self.best_profile.name} (RMS mm: {ranked}); "
                f"w={self.width_mm:.2f} h={self.height_mm:.2f} mm, "
                f"crown A={self.measured_area_mm2:.2f} mm^2{extra}")


def fit_measured_section(outline, *, source: str,
                         wire_dia_mm: float | None = None,
                         wfs_m_min: float | None = None,
                         travel_mm_min: float | None = None,
                         electrode_efficiency: float = 1.0) -> SectionFit:


    pts = [(float(s), float(t)) for s, t in outline]
    if len(pts) < 5:
        raise ValueError("need at least 5 outline points to compare profiles")
    if any(not (math.isfinite(s) and math.isfinite(t)) for s, t in pts):
        raise ValueError("outline points must be finite")
    if min(t for _, t in pts) < -1e-6:
        raise ValueError("outline heights are measured from the substrate (t >= 0)")
    smin = min(s for s, _ in pts)
    smax = max(s for s, _ in pts)
    shift = 0.5 * (smin + smax)
    pts = sorted(((s - shift, t) for s, t in pts), key=lambda p: p[0])
    width = smax - smin
    height = max(t for _, t in pts)
    if width <= 0.0 or height <= 0.0:
        raise ValueError(f"degenerate section: width {width}, height {height}")

    area = 0.0
    for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
        area += 0.5 * (t0 + t1) * (s1 - s0)

    half = width / 2.0
    residuals: dict = {}
    for name, prof in PROFILES.items():
        try:
            sq = [(t - prof.crown(s, width, height)) ** 2
                  for s, t in pts if abs(s) <= half + 1e-9]
            residuals[name] = math.sqrt(sum(sq) / len(sq)) if sq else math.inf
        except ValueError:
            residuals[name] = math.inf
    best = PROFILES[min(residuals, key=residuals.get)]

    dep = ratio = None
    if wire_dia_mm is not None and wfs_m_min is not None and travel_mm_min is not None:
        dep = arcparams.deposited_area_mm2(wire_dia_mm, wfs_m_min, travel_mm_min,
                                           electrode_efficiency=electrode_efficiency)
        ratio = area / dep if dep > 0 else None

    return SectionFit(
        width_mm=width, height_mm=height, measured_area_mm2=area,
        residuals_mm=residuals, best_profile=best,
        cross_section=BeadCrossSection.measured(width, height, source=source, profile=best),
        deposited_area_mm2=dep, crown_area_ratio=ratio)
