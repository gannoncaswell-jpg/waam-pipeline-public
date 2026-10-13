from __future__ import annotations

from dataclasses import dataclass

import numpy as np


SEARCH_OVERTRAVEL_MM = 5.0


@dataclass(frozen=True)
class Probe:


    name: str
    start: np.ndarray
    target: np.ndarray
    expected: np.ndarray

    @property
    def direction(self) -> np.ndarray:
        d = np.asarray(self.target, float) - np.asarray(self.start, float)
        n = float(np.linalg.norm(d))
        if n < 1e-9:
            raise ValueError(f"probe {self.name}: start and target coincide")
        return d / n

    @property
    def travel_mm(self) -> float:
        return float(np.linalg.norm(np.asarray(self.target, float) - np.asarray(self.start, float)))


@dataclass(frozen=True)
class Hit:


    name: str
    point: np.ndarray


@dataclass
class PlaneFit:
    normal: np.ndarray
    point: np.ndarray
    residual_mm: float
    tilt_deg: float
    hits: int = 0

    def height_at(self, x: float, y: float) -> float:

        n, p = self.normal, self.point
        if abs(n[2]) < 1e-9:
            raise ValueError("plane is vertical; no single height over (x, y)")
        return float(p[2] - (n[0] * (x - p[0]) + n[1] * (y - p[1])) / n[2])


def fit_plane(points) -> PlaneFit:


    P = np.asarray([np.asarray(p, float) for p in points], float)
    if P.ndim != 2 or P.shape[1] != 3:
        raise ValueError("points must be an (N, 3) array of robot-frame mm")
    if len(P) < 3:
        raise ValueError(f"need >=3 hits to fit a plane, got {len(P)}")
    centroid = P.mean(axis=0)
    u, s, vt = np.linalg.svd(P - centroid)
    if s[1] < 1e-6:
        raise ValueError("hits are collinear; a plane is not determined")
    normal = vt[2]
    if normal[2] < 0:
        normal = -normal
    resid = float(np.max(np.abs((P - centroid) @ normal)))
    tilt = float(np.degrees(np.arccos(np.clip(normal[2], -1.0, 1.0))))
    return PlaneFit(normal=normal, point=centroid, residual_mm=resid,
                    tilt_deg=tilt, hits=len(P))


def face_from_hits(hits, *, tip_offset_mm: float = 0.0) -> PlaneFit:


    fit = fit_plane([h.point if isinstance(h, Hit) else h for h in hits])
    if tip_offset_mm:
        fit = PlaneFit(
            normal=fit.normal,
            point=fit.point - fit.normal * float(tip_offset_mm),
            residual_mm=fit.residual_mm,
            tilt_deg=fit.tilt_deg,
            hits=fit.hits,
        )
    return fit


def edge_from_hits(hits, axis: int) -> float:


    if axis not in (0, 1, 2):
        raise ValueError("axis must be 0 (X), 1 (Y) or 2 (Z)")
    P = np.asarray([(h.point if isinstance(h, Hit) else h) for h in hits], float)
    if len(P) == 0:
        raise ValueError("no hits")
    return float(P[:, axis].mean())


def edge_spread_mm(hits, axis: int) -> float:

    P = np.asarray([(h.point if isinstance(h, Hit) else h) for h in hits], float)
    return float(np.ptp(P[:, axis])) if len(P) > 1 else 0.0


def part_origin_from_hits(
    face_hits,
    x_edge_hits,
    y_edge_hits,
    *,
    tip_offset_mm: float = 0.0,
) -> dict:


    face = face_from_hits(face_hits, tip_offset_mm=tip_offset_mm)
    x0 = edge_from_hits(x_edge_hits, 0)
    y0 = edge_from_hits(y_edge_hits, 1)
    return {
        "origin": np.array([x0, y0, face.height_at(x0, y0)]),
        "face_tilt_deg": face.tilt_deg,
        "face_residual_mm": face.residual_mm,
        "face_hits": face.hits,
        "x_spread_mm": edge_spread_mm(x_edge_hits, 0),
        "y_spread_mm": edge_spread_mm(y_edge_hits, 1),
        "normal": face.normal,
    }


def platter_probe_plan(
    face_z: float,
    *,
    centre_xy=(1014.0, -300.0),
    half_span_mm: float = 120.0,
    approach_mm: float = 25.0,
    overtravel_mm: float = SEARCH_OVERTRAVEL_MM,
    grid: int | None = None,
) -> list[Probe]:


    cx, cy = float(centre_xy[0]), float(centre_xy[1])
    h = float(half_span_mm)
    if grid is None:
        offsets = (
            ("face_near_left", (-h, -h)),
            ("face_near_right", (-h, +h)),
            ("face_far_left", (+h, -h)),
            ("face_far_right", (+h, +h)),
        )
    elif not isinstance(grid, int) or isinstance(grid, bool) or grid < 2:
        raise ValueError("grid must be an integer >= 2 (an N x N probe lattice)")
    else:
        axis = np.linspace(-h, h, grid)
        offsets = tuple(
            (f"face_r{i}c{j}", (float(dx), float(dy)))
            for i, dx in enumerate(axis) for j, dy in enumerate(axis)
        )
    plan: list[Probe] = []
    for name, (dx, dy) in offsets:
        expected = np.array([cx + dx, cy + dy, face_z])
        plan.append(
            Probe(
                name=name,
                start=expected + np.array([0.0, 0.0, approach_mm]),
                target=expected - np.array([0.0, 0.0, overtravel_mm]),
                expected=expected,
            )
        )
    return plan


def line_probe_plan(
    start_xy,
    end_xy,
    face_z: float,
    *,
    stations: int = 5,
    approach_mm: float = 25.0,
    overtravel_mm: float = SEARCH_OVERTRAVEL_MM,
) -> list[Probe]:


    a = np.asarray(start_xy, float).reshape(-1)
    b = np.asarray(end_xy, float).reshape(-1)
    if a.shape != (2,) or b.shape != (2,) or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("line_probe_plan: start_xy and end_xy are robot-base (x, y) in mm")
    if not np.isfinite(face_z):
        raise ValueError("line_probe_plan: face_z must be finite")
    if not isinstance(stations, int) or isinstance(stations, bool) or stations < 2:
        raise ValueError("line_probe_plan: stations must be an integer >= 2")
    if not (approach_mm > 0 and overtravel_mm > 0):
        raise ValueError("line_probe_plan: approach_mm and overtravel_mm must be positive")
    if float(np.linalg.norm(b - a)) < 1.0:
        raise ValueError("line_probe_plan: the two endpoints must be at least 1 mm apart")
    plan: list[Probe] = []
    for i in range(stations):
        x, y = a + (i / (stations - 1)) * (b - a)
        expected = np.array([float(x), float(y), float(face_z)])
        plan.append(
            Probe(
                name=f"line_s{i}",
                start=expected + np.array([0.0, 0.0, approach_mm]),
                target=expected - np.array([0.0, 0.0, overtravel_mm]),
                expected=expected,
            )
        )
    return plan


@dataclass(frozen=True)
class LineProfile:


    s: np.ndarray
    z: np.ndarray
    span_mm: float
    slope_deg: float
    flatness_mm: float
    peak_to_peak_mm: float

    def z_at(self, s_mm: float) -> float:

        return float(np.interp(float(s_mm), self.s, self.z))

    def summary(self) -> str:
        return (f"{len(self.s)} stations over {self.span_mm:.0f} mm: tilt "
                f"{self.slope_deg:+.2f} deg along the line, flatness "
                f"{self.flatness_mm:.2f} mm, peak-to-peak {self.peak_to_peak_mm:.2f} mm")


def line_profile(points, start_xy, end_xy) -> LineProfile:


    a = np.asarray(start_xy, float).reshape(-1)
    b = np.asarray(end_xy, float).reshape(-1)
    pts = np.asarray(points, float)
    if a.shape != (2,) or b.shape != (2,):
        raise ValueError("line_profile: start_xy and end_xy are robot-base (x, y) in mm")
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 2:
        raise ValueError("line_profile: need >= 2 probed points as (x, y, z) rows")
    if not (np.isfinite(pts).all() and np.isfinite(a).all() and np.isfinite(b).all()):
        raise ValueError("line_profile: a coordinate is not finite")
    d = b - a
    span = float(np.linalg.norm(d))
    if span < 1.0:
        raise ValueError("line_profile: the two endpoints must be at least 1 mm apart")
    u = d / span
    s = (pts[:, :2] - a) @ u
    order = np.argsort(s)
    s, z = s[order], pts[order, 2]
    m, c = np.polyfit(s, z, 1)
    resid = z - (m * s + c)
    return LineProfile(
        s=s, z=z, span_mm=span,
        slope_deg=float(np.degrees(np.arctan(m))),
        flatness_mm=float(np.abs(resid).max()),
        peak_to_peak_mm=float(z.max() - z.min()),
    )


def hits_from_points(plan: list[Probe], points) -> list[Hit]:


    plan = list(plan)
    pts = [np.asarray(p, float) for p in points]
    if len(pts) != len(plan):
        raise ValueError(
            f"got {len(pts)} contact points for {len(plan)} probes -- the "
            "read-back must have one record per probe, in plan order")
    return [Hit(name=pr.name, point=pt) for pr, pt in zip(plan, pts)]


def check_hits(plan: list[Probe], hits: list[Hit], *, tolerance_mm: float = 15.0) -> list[str]:


    by_name = {h.name: h for h in hits}
    problems: list[str] = []
    for p in plan:
        h = by_name.get(p.name)
        if h is None:
            problems.append(f"{p.name}: no hit recorded")
            continue
        d = float(np.linalg.norm(np.asarray(h.point, float) - np.asarray(p.expected, float)))
        if d > tolerance_mm:
            problems.append(f"{p.name}: hit {d:.1f} mm from expected (>{tolerance_mm:.0f})")
    extra = set(by_name) - {p.name for p in plan}
    for name in sorted(extra):
        problems.append(f"{name}: hit with no matching probe in the plan")
    return problems
