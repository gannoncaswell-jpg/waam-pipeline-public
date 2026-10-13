from __future__ import annotations

from dataclasses import dataclass
import re

import numpy as np

from . import cell_lc, touch
from .backends.jbi import emit_search_jbi
from .clearance import TorchModel


@dataclass
class MapPlan:
    corners: np.ndarray
    holes: list[dict]
    points: np.ndarray
    tie_points: np.ndarray
    jobs: list[list[touch.Probe]]
    roles: list[list[str]]
    origin: np.ndarray
    basis: np.ndarray
    spacing_mm: float
    target: int
    inset_mm: float
    hole_margin_mm: float
    per_job: int
    hop_mm: float
    max_bow_mm: float


def _points(value, count=None):
    result = np.asarray(value, float)
    if (result.ndim != 2 or result.shape[1] != 3 or not np.isfinite(result).all()
            or (count is not None and len(result) != count)):
        raise ValueError(f"expected {'finite' if count is None else count} robot-frame XYZ points")
    return result


def _decode(records):
    records = np.asarray(records, float)
    if (records.ndim != 2 or records.shape[1] != 6 or not np.isfinite(records).all()
            or np.any(records != np.rint(records))):
        raise ValueError("expected six integer PULSE values per record")
    return np.asarray([cell_lc.tcp_from_pulses(row) for row in records])


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _frame(corners):
    corners = _points(corners, 4)
    plane = touch.fit_plane(corners)
    if plane.tilt_deg > 5:
        raise ValueError(f"corner plane tilt {plane.tilt_deg:.3f} deg exceeds 5 deg")
    if plane.residual_mm > 1.5:
        raise ValueError(f"corner plane residual {plane.residual_mm:.3f} mm exceeds 1.5 mm")
    u = corners[1] - corners[0]
    u -= np.dot(u, plane.normal) * plane.normal
    if np.linalg.norm(u) < 1e-6:
        raise ValueError("degenerate quadrilateral edge")
    u /= np.linalg.norm(u)
    basis = np.column_stack((u, np.cross(plane.normal, u), plane.normal))
    uv = (corners - plane.point) @ basis[:, :2]
    edges = np.roll(uv, -1, axis=0) - uv
    if (np.min(np.linalg.norm(edges, axis=1)) < 1e-6
            or np.min(np.abs(_cross(edges, np.roll(edges, -1, axis=0)))) < 1e-6):
        raise ValueError("degenerate quadrilateral")
    for i, j in ((0, 2), (1, 3)):
        a, b, c, d = uv[i], uv[(i + 1) % 4], uv[j], uv[(j + 1) % 4]
        if (_cross(b - a, c - a) * _cross(b - a, d - a) <= 0
                and _cross(d - c, a - c) * _cross(d - c, b - c) <= 0):
            raise ValueError("self-intersecting quadrilateral")
    if abs(np.sum(_cross(uv, np.roll(uv, -1, axis=0)))) < 1e-6:
        raise ValueError("degenerate quadrilateral area")
    turns = _cross(edges, np.roll(edges, -1, axis=0))
    if not (np.all(turns > 0) or np.all(turns < 0)):
        raise ValueError("the four corners must form a convex region: hops between stations travel straight")
    return plane.point, basis, uv


def corners_from_records(records):

    corners = _points(_decode(records), 4)
    _frame(corners)
    return corners


def holes_from_records(records, points_per_hole, *, corners=None):


    if type(points_per_hole) is not int or points_per_hole not in (3, 4):
        raise ValueError("points_per_hole must be 3 or 4")
    points = _decode(records)
    if not len(points) or len(points) % points_per_hole:
        raise ValueError("rim records must contain complete groups of 3 or 4 touches")
    if corners is None:
        plane = touch.fit_plane(points)
        origin = plane.point
        u = np.array([1.0, 0.0, 0.0])
        u -= np.dot(u, plane.normal) * plane.normal
        u /= np.linalg.norm(u)
        basis = np.column_stack((u, np.cross(plane.normal, u), plane.normal))
    else:
        origin, basis, _ = _frame(corners)
    holes = []
    for i in range(0, len(points), points_per_hole):
        uv = (points[i:i + points_per_hole] - origin) @ basis[:, :2]
        midpoint = uv.mean(axis=0)
        xy = uv - midpoint
        design = np.column_stack((2 * xy, np.ones(len(xy))))
        fit, _, rank, _ = np.linalg.lstsq(design, np.sum(xy * xy, axis=1), rcond=None)
        if rank != 3:
            raise ValueError(f"hole {i // points_per_hole + 1}: collinear rim touches")
        radius = float(np.sqrt(max(0, fit[2] + np.dot(fit[:2], fit[:2]))))
        residual = float(np.max(np.abs(np.linalg.norm(xy - fit[:2], axis=1) - radius)))
        if not 2 <= radius <= 60:
            raise ValueError(f"hole {i // points_per_hole + 1}: radius {radius:.3f} mm outside 2..60")
        if points_per_hole == 4 and residual > 1:
            raise ValueError(f"hole {i // points_per_hole + 1}: circle residual {residual:.3f} mm exceeds 1")
        relative = xy - fit[:2]
        angles = np.sort(np.arctan2(relative[:, 1], relative[:, 0]))
        gaps = np.diff(np.append(angles, angles[0] + 2 * np.pi))
        if np.degrees(gaps.max()) > (180 if points_per_hole == 3 else 150):
            raise ValueError(f"spread the rim touches evenly around hole {i // points_per_hole + 1}, "
                             "about 120 or 90 deg apart")
        center = origin + basis[:, :2] @ (midpoint + fit[:2])
        holes.append({"center": tuple(float(v) for v in center),
                      "radius": radius, "residual": residual})
    return holes


def _holes(holes, margin, *, stored=False):
    result = []
    for hole in holes:
        center = _points([hole["center"]], 1)[0]
        radius, residual = float(hole["radius"]), float(hole["residual"])
        if not np.isfinite([radius, residual]).all() or not 2 <= radius <= 60 or not 0 <= residual <= 1:
            raise ValueError("hole radius must be 2..60 mm and residual 0..1 mm")
        exclusion = radius + margin + 3 * residual + 1
        if stored and hole["exclusion_radius_mm"] != exclusion:
            raise ValueError("hole exclusion radius disagrees with its radius, margin and fit uncertainty")
        result.append({"center": tuple(float(v) for v in center), "radius": radius,
                       "residual": residual, "exclusion_radius_mm": exclusion})
    return result


def _inside(points, quad, inset):
    inside = np.zeros(len(points), dtype=bool)
    distance = np.full(len(points), np.inf)
    x, y = points.T
    for a, b in zip(quad, np.roll(quad, -1, axis=0)):
        edge = b - a
        t = np.clip((points - a) @ edge / np.dot(edge, edge), 0, 1)
        distance = np.minimum(distance, np.linalg.norm(points - a - t[:, None] * edge, axis=1))
        if abs(edge[1]) > 1e-12:
            inside ^= ((a[1] > y) != (b[1] > y)) & (x < a[0] + (y - a[1]) * edge[0] / edge[1])
    return (inside | (distance < 1e-8)) & (distance >= inset - 1e-8)


def _grid(quad, holes_uv, target, inset):
    low, high = quad.min(axis=0) + inset, quad.max(axis=0) - inset
    span = high - low
    if np.min(span) <= 0:
        raise ValueError("no grid fits inside the quadrilateral inset")
    limit = int(np.floor(target * 1.1))
    best = None

    def candidate(spacing):
        counts = np.floor(span / spacing).astype(int) + 1
        if np.prod(counts) > max(100000, target * 100):
            return None
        first = (low + high - (counts - 1) * spacing) / 2
        xx, yy = np.meshgrid(np.arange(counts[0]), np.arange(counts[1]))
        indices = np.column_stack((xx.ravel(), yy.ravel()))
        uv = first + indices * spacing
        keep = _inside(uv, quad, inset)
        for center, exclusion in holes_uv:
            keep &= np.linalg.norm(uv - center, axis=1) >= exclusion - 1e-8
        uv, indices = uv[keep], indices[keep]
        if not 2 <= len(uv) <= limit:
            return None
        order = np.lexsort((np.where(indices[:, 1] % 2, -indices[:, 0], indices[:, 0]), indices[:, 1]))
        return uv[order]

    scale = float(np.sqrt(np.prod(span) / target))

    for attempt in range(5):
        spacings = np.geomspace(scale * 0.5, max(float(span.max()), scale * 2), 360)
        for spacing in spacings:
            uv = candidate(float(spacing))
            if uv is None:
                continue
            key = (abs(len(uv) - target), len(uv) > target, -float(spacing))
            if best is None or key < best[0]:
                best = key, uv, float(spacing)
        if best is not None and len(best[1]) >= target * 0.9:
            break
        scale *= 0.5
    if best is None:
        raise ValueError("fewer than two map stations fit outside the holes and inset")
    for spacing in np.linspace(best[2] * 0.98, best[2] * 1.02, 201):
        uv = candidate(float(spacing))
        if uv is not None:
            key = (abs(len(uv) - target), len(uv) > target, -float(spacing))
            if key < best[0]:
                best = key, uv, float(spacing)
    return best[1], best[2]


def _settings(target, inset_mm, hole_margin_mm, per_job, hop_mm, max_bow_mm):
    if type(target) is not int or target < 2:
        raise ValueError("target must be an integer >= 2")
    if type(per_job) is not int or not 4 <= per_job <= 20:
        raise ValueError("per_job must be an integer from 4 to 20 (P044-P063)")
    for name, value, low, high in (("hop_mm", hop_mm, 2, 20),
                                    ("max_bow_mm", max_bow_mm, 0.5, 10),
                                    ("inset_mm", inset_mm, 0, np.inf),
                                    ("hole_margin_mm", hole_margin_mm, 0, np.inf)):
        if type(value) not in (int, float) or not np.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite and in {low}..{high}")
    if hop_mm < max_bow_mm + 3:
        raise ValueError("a hop must clear the worst surface departure from the corner plane plus 3 mm")


def map_plan(corners, holes, *, target=100, inset_mm=10, hole_margin_mm=8,
             per_job=20, ties=2, hop_mm=5.0, max_bow_mm=2.0):


    _settings(target, inset_mm, hole_margin_mm, per_job, hop_mm, max_bow_mm)
    if type(ties) is not int or ties != 2:
        raise ValueError("ties must be 2")
    corners = _points(corners, 4).copy()
    origin, basis, quad = _frame(corners)
    holes = _holes(holes, hole_margin_mm)
    holes_uv = [((np.asarray(h["center"]) - origin) @ basis[:, :2], h["exclusion_radius_mm"])
                for h in holes]
    uv, spacing = _grid(quad, holes_uv, target, inset_mm)
    points = origin + uv @ basis[:, :2].T
    nearest = np.argsort(np.linalg.norm(uv - uv.mean(axis=0), axis=1), kind="stable")
    a = int(nearest[0])
    for candidate in nearest:
        if np.max(np.linalg.norm(uv - uv[candidate], axis=1)) >= 100:
            a = int(candidate)
            break
    b = int(np.argmax(np.linalg.norm(uv - uv[a], axis=1)))
    if b == a:
        raise ValueError("need two distinct tie stations")
    tie_points = points[[a, b]]
    new = np.delete(points, [a, b], axis=0)
    capacity = per_job - 4
    if capacity == 0 and len(new):
        raise ValueError("per_job=4 leaves no room for new stations after four tie touches; use >=5")
    chunks = [new[i:i + capacity] for i in range(0, len(new), capacity)] if len(new) else [new]
    jobs, roles = [], []
    for j, chunk in enumerate(chunks, 1):
        expected = np.vstack((tie_points, chunk, tie_points))
        jobs.append([touch.Probe(name=f"j{j}_{k:02d}", expected=p.copy(),
                                 start=p + [0, 0, 25], target=p - [0, 0, touch.SEARCH_OVERTRAVEL_MM])
                     for k, p in enumerate(expected)])
        roles.append(["tie", "tie"] + ["new"] * len(chunk) + ["tie", "tie"])
    plan = MapPlan(corners, holes, points, tie_points, jobs, roles, origin, basis,
                   spacing, target, float(inset_mm), float(hole_margin_mm), per_job,
                   float(hop_mm), float(max_bow_mm))
    _planned_design(plan, degree=2)
    return plan


def render_jobs(plan, *, name_prefix="PLATMAP", folder="WAAM", date):

    plan = load_plan(plan_json(plan))
    names = [f"{name_prefix}{j}" for j in range(1, len(plan.jobs) + 1)]
    if any(re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,31}", name) is None for name in names):
        raise ValueError("job names must be <=32 characters: A-Z, 0-9, _ or -, starting alphanumeric")
    origin = np.zeros(3)
    scene = cell_lc.lc_scene(origin, training_block=False)
    torch = TorchModel.yaskawa_accufast_45()
    return {name: emit_search_jbi(
        probes, cell_lc.pulse_solver(origin), name=name, date=date, scene=scene, torch=torch,
        rin=3, pvar_base=44, overtravel_mm=touch.SEARCH_OVERTRAVEL_MM, hop_mm=plan.hop_mm,
        clean_job="CLEAN_R1", folder=folder, provenance=(
            f"platter map job {j}/{len(plan.jobs)}", "ties first+last", "A B then new then A B",
            "S1 0 deg; ManPro block off", f"hop {plan.hop_mm:g} mm; P044-P{43 + len(probes):03d}"))
        for j, (name, probes) in enumerate(zip(names, plan.jobs), 1)}


def plan_json(plan):

    rows = []
    for j, (job, roles) in enumerate(zip(plan.jobs, plan.roles), 1):
        for k, (probe, role) in enumerate(zip(job, roles)):
            tie = ("A" if k in (0, len(job) - 2) else "B") if role == "tie" else None
            rows.append({"job": j, "index": k, "pvar": 44 + k, "role": role, "tie": tie,
                         "name": probe.name, "expected": np.asarray(probe.expected).tolist(),
                         "start": np.asarray(probe.start).tolist(), "target": np.asarray(probe.target).tolist()})
    return {"version": 1, "tie_layout": "AB-new-AB", "corners": plan.corners.tolist(),
            "holes": [{**h, "center": list(h["center"])} for h in plan.holes],
            "points": plan.points.tolist(), "tie_points": plan.tie_points.tolist(),
            "origin": plan.origin.tolist(), "basis": plan.basis.tolist(),
            "spacing_mm": plan.spacing_mm, "target": plan.target, "inset_mm": plan.inset_mm,
            "hole_margin_mm": plan.hole_margin_mm, "per_job": plan.per_job,
            "hop_mm": plan.hop_mm, "max_bow_mm": plan.max_bow_mm, "probes": rows}


def load_plan(obj):

    if not isinstance(obj, dict) or obj.get("version") != 1 or obj.get("tie_layout") != "AB-new-AB":
        raise ValueError("unsupported platter-map plan version or tie layout")
    try:
        target, inset, margin = obj["target"], obj["inset_mm"], obj["hole_margin_mm"]
        per_job, hop = obj["per_job"], obj["hop_mm"]
        max_bow = obj["max_bow_mm"]
        _settings(target, inset, margin, per_job, hop, max_bow)
        corners, holes = _points(obj["corners"], 4), _holes(obj["holes"], margin, stored=True)
        origin, basis, quad = _frame(corners)
        if (not np.allclose(_points([obj["origin"]], 1)[0], origin, rtol=0, atol=1e-8)
                or not np.allclose(_points(obj["basis"], 3), basis, rtol=0, atol=1e-8)):
            raise ValueError("plan frame disagrees with its corners")
        points, tie_points = _points(obj["points"]), _points(obj["tie_points"], 2)
        spacing = float(obj["spacing_mm"])
        if not np.isfinite(spacing) or spacing <= 0 or not 2 <= len(points) <= np.floor(target * 1.1):
            raise ValueError("invalid grid spacing or station count")
        local = (points - origin) @ basis
        if np.max(np.abs(local[:, 2])) > 1e-7 or not _inside(local[:, :2], quad, inset).all():
            raise ValueError("plan stations leave the corner plane or quadrilateral inset")
        for hole in holes:
            center = (np.asarray(hole["center"]) - origin) @ basis[:, :2]
            if np.min(np.linalg.norm(local[:, :2] - center, axis=1)) < hole["exclusion_radius_mm"] - 1e-8:
                raise ValueError("plan station violates hole clearance")
        keys = [tuple(p) for p in points]
        tie_keys = [tuple(p) for p in tie_points]
        if len(set(keys)) != len(keys) or len(set(tie_keys)) != 2 or not set(tie_keys) <= set(keys):
            raise ValueError("plan must have unique stations and two distinct kept ties")
        lattice = (local[:, :2] - local[0, :2]) / spacing
        if not np.allclose(lattice, np.rint(lattice), rtol=0, atol=1e-7):
            raise ValueError("plan stations are not on the recorded uniform grid")
        jobs, roles, new_points = [], [], []
        rows = obj["probes"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("plan contains no probes")
        position = 0
        while position < len(rows):
            j = len(jobs) + 1
            chunk = []
            while position < len(rows) and rows[position]["job"] == j:
                chunk.append(rows[position])
                position += 1
            if not 4 <= len(chunk) <= per_job:
                raise ValueError(f"job {j}: invalid touch count or job order")
            probes, job_roles = [], []
            for k, row in enumerate(chunk):
                role = "tie" if k in (0, 1, len(chunk) - 2, len(chunk) - 1) else "new"
                tie = ("A" if k in (0, len(chunk) - 2) else "B") if role == "tie" else None
                expected, start, end = _points([row["expected"], row["start"], row["target"]], 3)
                if (row["index"] != k or row["pvar"] != 44 + k or row["role"] != role
                        or row["tie"] != tie or row["name"] != f"j{j}_{k:02d}"):
                    raise ValueError(f"job {j} P{44 + k:03d}: invalid probe metadata")
                if (tuple(expected) not in set(keys)
                        or not np.allclose(start, expected + [0, 0, 25], rtol=0, atol=1e-8)
                        or not np.allclose(end, expected - [0, 0, touch.SEARCH_OVERTRAVEL_MM], rtol=0, atol=1e-8)):
                    raise ValueError(f"job {j} P{44 + k:03d}: invalid station or search window")
                if role == "tie":
                    if tuple(expected) != tie_keys[0 if tie == "A" else 1]:
                        raise ValueError(f"job {j} P{44 + k:03d}: wrong tie station")
                else:
                    new_points.append(tuple(expected))
                probes.append(touch.Probe(row["name"], start, end, expected))
                job_roles.append(role)
            jobs.append(probes)
            roles.append(job_roles)
        if new_points != [key for key in keys if key not in tie_keys]:
            raise ValueError("job probes do not cover new grid stations once in sweep order")
        plan = MapPlan(corners, holes, points, tie_points, jobs, roles, origin, basis,
                       spacing, target, inset, margin, per_job, hop, max_bow)
    except (KeyError, TypeError, OverflowError, IndexError) as exc:
        raise ValueError(f"invalid platter-map plan: {exc}") from None
    return plan


def _terms(uv, degree):
    u, v = np.asarray(uv).T
    columns = [np.ones(len(u)), u, v]
    if degree == 2:
        columns.extend((u * u, u * v, v * v))
    return np.column_stack(columns)


def _station_uv(points, plan):
    points = np.asarray(points).copy()
    normal = plan.basis[:, 2]
    points[:, 2] = (plan.origin[2] - (points[:, :2] - plan.origin[:2]) @ normal[:2] / normal[2])
    return (points - plan.origin) @ plan.basis[:, :2]


def _design(plan, uv, degree):
    jobs = np.repeat(np.arange(len(plan.jobs)), [len(job) for job in plan.jobs])
    offsets = (jobs[:, None] == np.arange(1, len(plan.jobs))).astype(float)
    indices = [k for job in plan.jobs for k in range(len(job))]
    return np.column_stack((_terms(uv, degree), offsets, indices))


def _check_design(design):
    singular = np.linalg.svd(design, compute_uv=False)
    tolerance = singular[0] * max(design.shape) * np.finfo(float).eps
    if np.count_nonzero(singular > tolerance) != design.shape[1]:
        raise ValueError("map fit is underdetermined: need more independent planned stations for this degree")
    condition = singular[0] / singular[-1]
    if condition > 1e8:
        raise ValueError(f"map fit design condition number {condition:.3g} exceeds 1e8")


def _planned_design(plan, degree):
    uv = (plan.points - plan.origin) @ plan.basis[:, :2]
    lattice = np.rint((uv - uv[0]) / plan.spacing_mm)
    if degree == 2 and any(len(np.unique(lattice[:, axis])) < 3 for axis in (0, 1)):
        raise ValueError("degree 2 requires at least 3 distinct u and v values in the planned grid")
    stations = {tuple(point): local for point, local in zip(plan.points, uv)}
    layout = [stations[tuple(probe.expected)] for job in plan.jobs for probe in job]
    design = _design(plan, layout, degree)
    _check_design(design)
    return design


def fit_map(plan, records_per_job, *, degree=2):


    if type(degree) is not int or degree not in (1, 2):
        raise ValueError("degree must be 1 or 2")
    plan = load_plan(plan_json(plan))
    _planned_design(plan, degree)
    records_per_job = list(records_per_job)
    if len(records_per_job) != len(plan.jobs):
        raise ValueError(f"need {len(plan.jobs)} VAR.DAT jobs, got {len(records_per_job)}")
    rows, points = [], []
    consistency = 0.0
    for j, (job, records) in enumerate(zip(plan.jobs, records_per_job), 1):
        if len(records) != len(job):
            raise ValueError(f"job {j}: need P044-P{43 + len(job):03d}, got {len(records)} records")
        hits = []
        for k, (probe, record) in enumerate(zip(job, records)):
            label = f"job {j} P{44 + k:03d}"
            try:
                hit = _decode([record])[0]
            except ValueError as exc:
                raise ValueError(f"{label}: {exc}") from None
            if np.linalg.norm(hit[:2] - probe.expected[:2]) > 1:
                raise ValueError(f"{label}: touch is >1 mm off its station in XY")
            if not probe.target[2] <= hit[2] <= probe.expected[2] + plan.hop_mm + 0.5:
                raise ValueError(f"{label}: touch is outside its search window")
            role = plan.roles[j - 1][k]
            tie = ("A" if k in (0, len(job) - 2) else "B") if role == "tie" else None
            rows.append({"job": j, "index": k, "pvar": 44 + k, "role": role, "tie": tie,
                         "x": float(hit[0]), "y": float(hit[1]), "z": float(hit[2])})
            points.append(hit)
            hits.append(hit)
        for first, last in ((0, len(job) - 2), (1, len(job) - 1)):
            difference = abs(hits[last][2] - hits[first][2])
            consistency = max(consistency, float(difference))
            if difference > 0.5:
                raise ValueError(f"job {j} P{44 + first:03d}/P{44 + last:03d}: tie differs by {difference:.3f} mm (>0.5)")
    points = np.asarray(points)
    design = _design(plan, _station_uv(points, plan), degree)
    _check_design(design)
    scale = np.linalg.norm(design, axis=0)
    scale[scale == 0] = 1
    coefficients, _, rank, _ = np.linalg.lstsq(design / scale, points[:, 2], rcond=None)
    coefficients /= scale
    if rank != design.shape[1]:
        raise ValueError("map fit is underdetermined: need more independent stations for this degree")
    tie_differences = []
    position = 0
    for job in plan.jobs:
        corrected = points[position:position + len(job), 2] - np.arange(len(job)) * coefficients[-1]
        tie_differences.append(float(np.mean(corrected[[1, -1]]) - np.mean(corrected[[0, -2]])))
        position += len(job)
    median_difference = float(np.median(tie_differences))
    for j, (job, difference) in enumerate(zip(plan.jobs, tie_differences), 1):
        departure = abs(difference - median_difference)
        if departure > 0.3:
            raise ValueError(f"job {j} P044/P045/P{42 + len(job):03d}/P{43 + len(job):03d}: "
                             f"tie B-A differs from the median by {departure:.3f} mm (>0.3)")
    residuals = points[:, 2] - design @ coefficients
    for row, residual in zip(rows, residuals):
        row["residual"] = float(residual)
    nterms = 6 if degree == 2 else 3
    grid_z = _terms(_station_uv(plan.points, plan), degree) @ coefficients[:nterms]
    xy = plan.points[:, :2] - plan.origin[:2]
    plane_design = np.column_stack((np.ones(len(xy)), xy))
    best_plane = np.linalg.lstsq(plane_design, grid_z, rcond=None)[0]
    departure = grid_z - plane_design @ best_plane
    slope = best_plane[1:]
    return {"plane_tilt_deg": float(np.degrees(np.arctan(np.linalg.norm(slope)))),
            "tilt_direction_deg": float(np.degrees(np.arctan2(slope[1], slope[0])) % 360),
            "poly_coefficients": coefficients[:nterms].tolist(),
            "polynomial_terms": ["1", "u", "v", "u^2", "u*v", "v^2"][:nterms],
            "frame": {"origin": plan.origin.tolist(), "basis": plan.basis.tolist()},
            "job_offsets_mm": {str(j + 1): (0.0 if j == 0 else float(coefficients[nterms + j - 1]))
                               for j in range(len(plan.jobs))},
            "drift_mm_per_touch": float(coefficients[-1]), "residuals": residuals.tolist(),
            "tie_differences_mm": {str(j): value for j, value in enumerate(tie_differences, 1)},
            "rms_mm": float(np.sqrt(np.mean(residuals ** 2))),
            "max_abs_mm": float(np.max(np.abs(residuals))),
            "flatness_pv_mm": float(np.ptp(departure)), "tie_consistency_mm": consistency,
            "points": rows}
