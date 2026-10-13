from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction

import numpy as np

from . import cell_lc
from .substrate import MAX_TILT_DEG, Substrate
from .touch import SEARCH_OVERTRAVEL_MM, Probe


CHORD_TOL_MM = 0.05

PIT_MM = 1.0

MIN_STATION_GAP_MM = 2.0


def _require_physical(plate):
    if plate.gap_direction is None:
        raise ValueError("Sensing needs a version 2 substrate with a verified TOOL 0 offset and physical surface; register the plate again")


def sense_plan(plate, *, spacing_mm=40.0, inset_mm=10.0, max_points=25,
               approach_mm=25.0, overtravel_mm=SEARCH_OVERTRAVEL_MM) -> list[Probe]:
    _require_physical(plate)
    for name, value, positive in (("spacing_mm", spacing_mm, True),
                                  ("inset_mm", inset_mm, False),
                                  ("approach_mm", approach_mm, True),
                                  ("overtravel_mm", overtravel_mm, True)):
        if (type(value) not in (int, float) or not np.isfinite(value)
                or value < 0 or (positive and value == 0)):
            raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if type(max_points) is not int or not 2 <= max_points <= 25:
        raise ValueError("max_points must be an integer from 2 to 25 (P040-P064)")
    lo = plate.corners.min(axis=0) + inset_mm
    hi = plate.corners.max(axis=0) - inset_mm
    span = hi - lo
    if np.any(span < 0) or span.max() <= 0:
        raise ValueError("Fewer than two sense points fit inside the substrate inset")
    spacing = min(spacing_mm, float(span.max()))
    while True:
        counts = [int(np.floor(length / spacing)) + 1 for length in span]
        first = (lo + hi - (np.asarray(counts) - 1) * spacing) / 2
        uv = []
        for row in range(counts[1]):
            for column in range(counts[0]):
                point = first + spacing * np.array([column, row])
                if np.min(plate.edge_distances(point)) >= inset_mm - 1e-8:
                    uv.append(point)
                    if len(uv) > max_points:
                        break
            if len(uv) > max_points:
                break
        if len(uv) <= max_points:
            break
        spacing *= 1.1
    if len(uv) < 2:
        raise ValueError("Fewer than two sense points fit inside the substrate inset")
    probes = []
    for i, point in enumerate(uv):
        expected = plate.origin + plate.basis[:, :2] @ point + plate.physical_shift_robot
        probes.append(Probe(name=f"sense_{i:02d}", expected=expected,
                            start=expected + [0, 0, approach_mm],
                            target=expected - [0, 0, overtravel_mm]))
    return probes


def _terms(xy, quadratic):
    x, y = np.asarray(xy, float).T
    terms = [np.ones_like(x), x, y]
    if quadratic:
        terms.extend((x * x, x * y, y * y))
    return np.column_stack(terms)


def sense_plan_along(plate, toolpath, origin, *, spacing_mm=20.0, inset_mm=10.0,
                     max_points=21, approach_mm=25.0,
                     overtravel_mm=SEARCH_OVERTRAVEL_MM) -> list[Probe]:

    from .layer_sense import _at, _chords, _positive

    _require_physical(plate)
    spacing = _positive("spacing_mm", spacing_mm)
    inset = _positive("inset_mm", inset_mm, zero=True)
    approach = _positive("approach_mm", approach_mm)
    overtravel = _positive("overtravel_mm", overtravel_mm)
    if type(max_points) is not int or not 2 <= max_points <= 21:
        raise ValueError("max_points must be an integer from 2 to 21 (P044-P064)")
    origin = np.asarray(origin, float)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise ValueError("Path sensing needs a finite robot-frame origin")
    deposits = [move for move in toolpath.moves if move.deposit]
    if not deposits:
        raise ValueError("The build has no weld moves to sense")
    if toolpath.moves[0].deposit:
        raise ValueError("The toolpath's first move deposits, so it has no weld start")
    lowest = min(move.z - move.seat for move in deposits)
    passes, run = [], []
    for i, move in enumerate(toolpath.moves):
        if move.deposit:
            run.append((toolpath.moves[i - 1], move, i))
        elif run:
            passes.append(run)
            run = []
    if run:
        passes.append(run)
    passes = [run for run in passes
              if all(abs(move.z - move.seat - lowest) <= 1e-6
                     for start, end, _ in run for move in (start, end))]
    lengths = [float(_chords(run)[3].sum()) for run in passes]
    shift = np.asarray(plate.physical_shift_robot)
    inverse = np.linalg.inv(plate.basis[:2, :2])

    def stations(step):
        points = []
        for run, length in zip(passes, lengths):
            if length <= 1e-9:
                continue


            intervals = max(1, int(np.ceil(length / step - 1e-9))) if np.isfinite(step) else 1
            for j in range(intervals + 1):
                point, _ = _at(run, length * j / intervals)
                xy = point[:2] + origin[:2]
                uv = inverse @ (xy - plate.origin[:2] - shift[:2])
                if np.min(plate.edge_distances(uv)) < inset - 1e-8:
                    continue
                if any(np.linalg.norm(xy - previous[:2]) < MIN_STATION_GAP_MM for previous in points):
                    continue
                points.append(plate.origin + plate.basis[:, :2] @ uv + shift)
                if len(points) > max_points:
                    return points
        return points

    if len(stations(np.inf)) > max_points:
        raise ValueError("The base passes need more endpoint touches than max_points allows")
    while True:
        expected = stations(spacing)
        if len(expected) <= max_points:
            break
        spacing *= 1.1
    if len(expected) < 2:
        raise ValueError("Fewer than two sense points fit along the base passes inside the substrate inset")
    return [Probe(name=f"path_{i:02d}", expected=point,
                  start=point + [0, 0, approach], target=point - [0, 0, overtravel])
            for i, point in enumerate(expected)]


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _orient(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _in_circle(a, b, c, d):

    rows = [(p[0] - d[0], p[1] - d[1]) for p in (a, b, c)]
    (a1, a2), (b1, b2), (c1, c2) = rows
    a3, b3, c3 = (x * x + y * y for x, y in rows)
    return a1 * (b2 * c3 - b3 * c2) - a2 * (b1 * c3 - b3 * c1) + a3 * (b1 * c2 - b2 * c1)


def _triangulate(xy):


    pts = [(Fraction(float(x)), Fraction(float(y))) for x, y in np.asarray(xy, float)]
    count = len(pts)
    a, b, c, d = (0, 1, 2, 3) if _orient(pts[0], pts[1], pts[2]) > 0 else (3, 2, 1, 0)
    triangles = [(a, b, c), (a, c, d)]
    if _in_circle(pts[a], pts[b], pts[c], pts[d]) > 0:
        triangles = [(a, b, d), (b, c, d)]
    for i in range(4, count):
        cavity = [tri for tri in triangles if _in_circle(*(pts[k] for k in tri), pts[i]) > 0]
        boundary = {}
        for a, b, c in cavity:
            for edge in ((a, b), (b, c), (c, a)):
                key = tuple(sorted(edge))
                if key in boundary:
                    del boundary[key]
                else:
                    boundary[key] = edge
        triangles = [tri for tri in triangles if tri not in cavity]
        for a, b in boundary.values():
            area = _orient(pts[a], pts[b], pts[i])
            if area > 0:
                triangles.append((a, b, i))
            elif area < 0:
                raise ValueError("Sensed points could not be triangulated; recheck the touch stations")
    hull = abs(_orient(pts[0], pts[1], pts[2])) + abs(_orient(pts[0], pts[2], pts[3]))
    areas = [_orient(*(pts[k] for k in tri)) for tri in triangles]
    used = {k for tri in triangles for k in tri}
    if not triangles or min(areas) <= 0 or sum(areas) != hull or used != set(range(count)):
        raise ValueError("Sensed points could not be triangulated; recheck the touch stations")
    return np.asarray(triangles, dtype=int)


@dataclass(frozen=True)
class SensedPlate(Substrate):
    _coefficients: np.ndarray = field(default_factory=lambda: np.zeros(3))
    _fit_center: np.ndarray = field(default_factory=lambda: np.zeros(2))
    _scale: float = 100.0
    _samples: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    _tin_xyz: np.ndarray = field(default_factory=lambda: np.empty((0, 3)))
    _tin_tri: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=int))
    _rms: float = 0.0
    _worst: str = ""

    def shifted(self, offset):
        offset = np.asarray(offset, float)
        coefficients = self._coefficients.copy()
        coefficients[0] -= offset[2]
        return replace(self, origin=self.origin - offset, points=self.points - offset,
                       _fit_center=self._fit_center - offset[:2],
                       _coefficients=coefficients, _samples=self._samples - offset,
                       _tin_xyz=self._tin_xyz - offset)

    def _polygon(self):
        if len(self._tin_tri):
            xy = self._tin_xyz[:4, :2]
            return xy if _cross(xy[1] - xy[0], xy[2] - xy[1]) > 0 else xy[::-1]
        return self.corners @ self.basis[:2, :2].T + self.origin[:2]

    def _uv(self, xy):
        return (np.asarray(xy, float).reshape(-1, 2) - self.origin[:2]) @ np.linalg.inv(self.basis[:2, :2]).T

    def _horizontal(self, xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        sides = self.edge_distances(self._uv(xy))
        inside = np.all(sides >= -1e-8, axis=1)
        a = self._polygon()
        ab = np.roll(a, -1, axis=0) - a
        if len(self._tin_tri):
            inside = np.all(_cross(ab, xy[:, None, :] - a) / np.linalg.norm(ab, axis=1) >= -1e-8, axis=1)
        t = np.clip(((xy[:, None, :] - a) * ab).sum(2) / (ab * ab).sum(1), 0, 1)
        nearest = a + t[:, :, None] * ab
        distances = np.linalg.norm(xy[:, None, :] - nearest, axis=2)
        edge = distances.argmin(axis=1)
        horizontal = distances[np.arange(len(xy)), edge]
        horizontal[inside] *= -1
        clamped = nearest[np.arange(len(xy)), edge].copy()
        clamped[inside] = xy[inside]
        return horizontal, inside, clamped

    def _evaluate(self, xy):
        if len(self._tin_tri):
            triangle, weights = self._facet(xy)
            return (self._tin_xyz[self._tin_tri[triangle], 2] * weights).sum(axis=1)
        q = (np.asarray(xy, float).reshape(-1, 2) - self._fit_center) / self._scale
        return _terms(q, len(self._coefficients) == 6) @ self._coefficients

    def _facet(self, xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        vertices = self._tin_xyz[self._tin_tri, :2]
        a = vertices[:, 0]
        ab, ac = vertices[:, 1] - a, vertices[:, 2] - a
        delta = xy[:, None, :] - a
        determinant = _cross(ab, ac)
        u = _cross(delta, ac) / determinant
        v = _cross(ab, delta) / determinant
        weights = np.stack((1 - u - v, u, v), axis=2)
        score = weights.min(axis=2)
        triangle = score.argmax(axis=1)
        chosen = weights[np.arange(len(xy)), triangle]
        if np.any(chosen.min(axis=1) < -1e-8):
            raise ValueError("Point lies outside the sensed triangulation")
        return triangle, chosen

    def _facet_gradients(self):
        vertices = self._tin_xyz[self._tin_tri]
        delta = vertices[:, 1:] - vertices[:, :1]
        return np.linalg.solve(delta[:, :, :2], delta[:, :, 2, None])[:, :, 0]

    def surface(self, x, y):

        x, y = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float))
        xy = np.column_stack((x.ravel(), y.ravel()))
        return self._evaluate(self._horizontal(xy)[2]).reshape(x.shape)

    def datum(self, x, y):
        return self.surface(x, y) - self.physical_shift_robot[2]

    def _gradient(self, xy):
        if len(self._tin_tri):
            return self._facet_gradients()[self._facet(xy)[0]]
        q = (np.asarray(xy, float).reshape(-1, 2) - self._fit_center) / self._scale
        c = self._coefficients
        gradient = np.tile(c[1:3], (len(q), 1))
        if len(c) == 6:
            gradient += q @ np.array([[2 * c[3], c[4]], [c[4], 2 * c[5]]])
        return gradient / self._scale

    def _height_range(self):
        if len(self._tin_tri):
            return [float(self._tin_xyz[:, 2].min()), float(self._tin_xyz[:, 2].max())]
        polygon = self._polygon()
        candidates = list(polygon)
        c = self._coefficients
        if len(c) == 6:
            hessian = np.array([[2 * c[3], c[4]], [c[4], 2 * c[5]]]) / self._scale**2
            for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
                edge = b - a
                denominator = float(edge @ hessian @ edge)
                if abs(denominator) > 1e-12:
                    t = -float(self._gradient(a)[0] @ edge) / denominator
                    if 0 < t < 1:
                        candidates.append(a + t * edge)
            if np.linalg.matrix_rank(hessian) == 2:
                stationary = self._fit_center - np.linalg.solve(hessian, c[1:3] / self._scale)
                if np.min(self.edge_distances(self._uv(stationary))) >= -1e-8:
                    candidates.append(stationary)
        heights = self._evaluate(candidates)
        return [float(heights.min()), float(heights.max())]

    def distance(self, points, *, contact=False):
        points = np.asarray(points, float).reshape(-1, 3)
        horizontal, inside, clamped = self._horizontal(points[:, :2])
        top = self._evaluate(clamped)
        if contact:
            top -= self.physical_shift_robot[2]
        height = points[:, 2] - top
        vertical = np.maximum(height, -self.thickness - height)
        d = np.hypot(np.maximum(horizontal, 0), np.maximum(vertical, 0)) + np.minimum(np.maximum(horizontal, vertical), 0)
        if contact:
            d[inside] = height[inside]
        d[np.abs(d) < 1e-6] = 0
        return d

    def mesh(self):
        if len(self._tin_tri):
            top = self._tin_xyz[self._tin_tri]
            bottom = top[:, ::-1] - [0, 0, self.thickness]
            boundary = {}
            for a, b, c in self._tin_tri:
                for edge in ((a, b), (b, c), (c, a)):
                    key = tuple(sorted(edge))
                    if key in boundary:
                        del boundary[key]
                    else:
                        boundary[key] = edge
            sides = []
            for i, j in boundary.values():
                a, b = self._tin_xyz[[i, j]]
                c, d = np.array([a, b]) - [0, 0, self.thickness]
                sides.extend(((a, c, d), (a, d, b)))
            return np.concatenate((top, bottom, np.asarray(sides)))
        a, b, c, d = self._polygon()
        axis = np.linspace(0, 1, 9)
        u, v = np.meshgrid(axis, axis)
        xy = ((1 - u)[..., None] * (1 - v)[..., None] * a
              + u[..., None] * (1 - v)[..., None] * b
              + u[..., None] * v[..., None] * c
              + (1 - u)[..., None] * v[..., None] * d)
        top = np.column_stack((xy.reshape(-1, 2), self._evaluate(xy.reshape(-1, 2))))
        vertices = np.vstack((top, top - [0, 0, self.thickness]))
        faces = []
        for row in range(8):
            for column in range(8):
                i = row * 9 + column
                faces.extend(((i, i + 1, i + 10), (i, i + 10, i + 9),
                              (i + 81, i + 91, i + 82), (i + 81, i + 90, i + 91)))
        perimeter = list(range(9)) + list(range(17, 81, 9)) + list(range(79, 71, -1)) + list(range(63, 0, -9))
        for i, j in zip(perimeter, perimeter[1:] + perimeter[:1]):
            faces.extend(((i, i + 81, j + 81), (i, j + 81, j)))
        return vertices[np.asarray(faces)]

    def _edge_splits(self, start, end):
        direction = end - start
        length2 = float(direction @ direction)
        edges = {}
        for a, b, c in self._tin_tri:
            for edge in ((a, b), (b, c), (c, a)):
                key = tuple(sorted(edge))
                edges[key] = edges.get(key, 0) + 1
        exterior = not self._horizontal([start, end])[1].all()
        splits = [1.0]
        for i, j in sorted(edges):
            a, b = self._tin_xyz[[i, j], :2]
            edge = b - a
            if exterior and edges[i, j] == 1:

                projection = float(direction @ edge)
                if abs(projection) > 1e-12 * np.sqrt(length2 * float(edge @ edge)):
                    splits.extend(float((p - start) @ edge / projection) for p in (a, b))
            denominator = _cross(direction, edge)
            if abs(denominator) <= 1e-12 * np.sqrt(length2 * float(edge @ edge)):
                if abs(_cross(a - start, direction)) <= 1e-8 * np.sqrt(length2):
                    splits.extend(float((p - start) @ direction / length2) for p in (a, b))
                continue
            t = float(_cross(a - start, edge) / denominator)
            u = float(_cross(a - start, direction) / denominator)
            if -1e-8 <= u <= 1 + 1e-8:
                splits.append(t)
        result = []
        for t in sorted(v for v in splits if 1e-10 < v <= 1):
            if not result or t - result[-1] > 1e-10:
                result.append(t)
        result[-1] = 1.0
        return np.asarray(result)

    def place(self, toolpath, *, ox=0, oy=0, bottom=None, gap=0, bead_width=8.6, edge_margin=5):


        if bottom is None:
            bottom = cell_lc.lowest_weld_z(toolpath)
        points = np.asarray(toolpath.deposit_points())
        pivot = (points[:, :2].min(0) + points[:, :2].max(0)) / 2
        uv = points[:, :2] - pivot + [ox, oy]
        xy = uv @ self.basis[:2, :2].T + self.origin[:2]
        required = bead_width / 2 + edge_margin
        if np.any(self._horizontal(xy)[0] > -required + 1e-6):
            raise ValueError("The bead footprint crosses the substrate edge margin. Move or rotate it, or shorten the bead.")
        moves = []
        prev = None
        lift = 0.0
        curved = len(self._coefficients) == 6 and np.any(self._coefficients[3:] != 0)
        for move in toolpath.moves:
            local = np.array([move.x - pivot[0] + ox, move.y - pivot[1] + oy])
            xy = self.origin[:2] + self.basis[:2, :2] @ local
            target = np.r_[xy, move.z, move.seat]
            if prev is None or np.array_equal(prev[:2], xy):
                pieces = target[None, :]
            elif len(self._tin_tri):
                t = self._edge_splits(prev[:2], xy)
                if len(t) > 1024:
                    raise ValueError(f"G-code line {move.line_no}: seating the surface chord needs more than 1024 pieces")
                pieces = prev + t[:, None] * (target - prev)
                pieces[-1] = target
            else:
                ends = np.vstack((prev, target))
                heights = self.datum(ends[:, 0], ends[:, 1])
                midpoint = (prev[:2] + xy) / 2
                sag = float(self.datum(*midpoint) - heights.mean())
                n = max(1, int(np.ceil(np.sqrt(abs(sag) / CHORD_TOL_MM)))) if curved else 1
                while True:
                    if n > 1024:
                        raise ValueError(f"G-code line {move.line_no}: seating the surface chord needs more than 1024 pieces")
                    t = np.linspace(0, 1, n + 1)
                    points = prev + t[:, None] * (target - prev)
                    points[-1] = target
                    heights = self.datum(points[:, 0], points[:, 1])
                    midpoints = (points[:-1, :2] + points[1:, :2]) / 2
                    dips = (self.datum(midpoints[:, 0], midpoints[:, 1])
                            - (heights[:-1] + heights[1:]) / 2)
                    if not curved or np.max(np.abs(dips)) <= CHORD_TOL_MM:
                        break
                    n *= 2
                if curved:
                    lift = max(lift, float(dips.max()))
                pieces = points[1:]
            heights = self.datum(pieces[:, 0], pieces[:, 1])
            for point, height in zip(pieces, heights):
                z = height + point[2] - bottom + gap
                p = np.r_[point[:2], z] - self.origin
                moves.append(replace(move, x=float(p[0]), y=float(p[1]), z=float(p[2]),
                                     seat=float(point[3] + height - self.origin[2])))
            prev = target
        if lift:
            moves = [replace(move, z=move.z + lift, seat=move.seat + lift) for move in moves]
        return replace(toolpath, moves=moves)

    def metadata(self):
        data = super().metadata()
        data["sensed"] = {
            "points": len(self._samples),
            "fit": "quadratic" if len(self._coefficients) == 6 else "plane",
            "rms_residual_mm": self._rms,
            "max_residual_mm": self.residual,
            "worst": self._worst,
            "height_range_mm": self._height_range(),
        }
        if len(self._tin_tri):
            data["sensed"].update(fit="interpolated", max_facet_slope_deg=self.tilt)
        return data

    def setup_metadata(self, platter_z, platter_center_xy):
        data = super().setup_metadata(platter_z, platter_center_xy)
        xy = np.vstack((self._polygon(), self._samples[:, :2]))
        heights = self.datum(xy[:, 0], xy[:, 1]) - float(platter_z)
        data.update(center_height_above_platter_mm=float(self.datum(*self.origin[:2]) - float(platter_z)),
                    height_range_above_platter_mm=[float(heights.min()), float(heights.max())],
                    height_variation_mm=float(np.ptp(heights)))
        return data


def _spikes(physical, triangles, limit_mm):

    spikes = set()
    for i in range(4, len(physical)):
        ring = sorted(set(triangles[np.any(triangles == i, axis=1)].ravel()) - {i})
        design = np.column_stack((np.ones(len(ring)), physical[ring, :2] - physical[i, :2]))
        if np.linalg.matrix_rank(design) == 3:
            height = np.linalg.lstsq(design, physical[ring, 2], rcond=None)[0][0]
            if physical[i, 2] - height > limit_mm:
                spikes.add(i)
    return spikes


def _plate_reference(physical, center, limit_mm, labels, spikes, polygon):


    q = (physical[:, :2] - center) / 100.0
    quadratic = len(physical) >= 10 and np.linalg.matrix_rank(_terms(q, True)) == 6
    coefficients = np.linalg.lstsq(_terms(q, quadratic), physical[:, 2], rcond=None)[0]
    residuals = physical[:, 2] - _terms(q, quadratic) @ coefficients
    for i in np.argsort(-np.abs(residuals)):
        r = float(residuals[i])
        if abs(r) <= limit_mm:
            break
        if i >= 4 and r > 0 and i in spikes:
            continue
        kind = "a low reading" if r < 0 else "a raised region that is not an isolated high spot"
        raise ValueError(f"{labels[i]} differs from the smooth fit by {r:+.2f} mm (limit {limit_mm:g} mm, as for "
                         f"the smooth fit): {kind}. Only an isolated high spot, above its own neighbouring "
                         "touches, may stand further above it. Recheck the touches")

    corners = (np.asarray(polygon, float) - center) / 100.0
    gradient = np.tile(coefficients[1:3], (len(corners), 1))
    if quadratic:
        c = coefficients
        gradient = gradient + corners @ np.array([[2 * c[3], c[4]], [c[4], 2 * c[5]]])

    tilt = float(np.degrees(np.arctan(np.linalg.norm(gradient / 100.0, axis=1).max())))
    if tilt > MAX_TILT_DEG:
        raise ValueError(f"The plate's smooth trend tilts {tilt:.2f} degrees (limit {MAX_TILT_DEG:g}). "
                         "Level the plate for vertical-torch welding, or recheck the touches")
    return coefficients, quadratic


def _fit_interpolated(plate, physical, samples, labels, max_low_mm):
    for i, point in enumerate(physical):
        near = np.flatnonzero(np.linalg.norm(physical[:i, :2] - point[:2], axis=1) < 1.0)
        if len(near):
            raise ValueError(f"{labels[i]} and {labels[int(near[0])]} are closer than 1.0 mm in XY; each touch needs its own station")
    polygon = physical[:4, :2]
    if _cross(polygon[1] - polygon[0], polygon[2] - polygon[1]) < 0:
        polygon = polygon[::-1]
    edges = np.roll(polygon, -1, axis=0) - polygon
    distances = _cross(edges, physical[4:, None, :2] - polygon) / np.linalg.norm(edges, axis=1)
    outside = np.flatnonzero(np.any(distances < -1e-8, axis=1))
    if len(outside):
        raise ValueError(f"{labels[int(outside[0]) + 4]}: sensed point lies outside the physical corner polygon. Recheck the touch and corner registration.")
    triangles = _triangulate(physical[:, :2])
    delta = physical[triangles][:, 1:] - physical[triangles][:, :1]
    gradients = np.linalg.solve(delta[:, :, :2], delta[:, :, 2, None])[:, :, 0]
    slopes = np.degrees(np.arctan(np.linalg.norm(gradients, axis=1)))
    steepest = int(np.argmax(slopes))
    if slopes[steepest] > MAX_TILT_DEG:
        names = ", ".join(labels[i] for i in triangles[steepest])
        raise ValueError(f"Facet {names} has slope {slopes[steepest]:.2f} degrees (limit {MAX_TILT_DEG:g} degrees). Level the plate or recheck the touches.")
    for i in range(4, len(physical)):
        neighbours = set(triangles[np.any(triangles == i, axis=1)].ravel()) - {i}
        depth = min(physical[j, 2] for j in neighbours) - physical[i, 2]
        if depth > PIT_MM:
            raise ValueError(f"{labels[i]} is a narrow pit, {depth:.2f} mm below its lowest neighbour; recheck the touch, or use the smooth fit")
    center = plate.origin[:2].copy()
    coefficients, quadratic = _plate_reference(physical, center, max_low_mm, labels,
                                               _spikes(physical, triangles, max_low_mm),
                                               plate.corners @ plate.basis[:2, :2].T + plate.origin[:2])
    terms = _terms((physical[:, :2] - center) / 100.0, quadratic)
    residuals = (physical[:, 2] - terms @ coefficients)[4:]
    worst = int(np.argmax(np.abs(residuals)))
    result = SensedPlate(points=plate.points.copy(), origin=plate.origin.copy(),
                         basis=plate.basis.copy(), corners=plate.corners.copy(),
                         thickness=plate.thickness, residual=float(abs(residuals[worst])),
                         tilt=0.0, torch_tilt_deg=plate.torch_tilt_deg,
                         gap_direction=plate.gap_direction,
                         tool0_tip_offset_mm=plate.tool0_tip_offset_mm,
                         physical_shift_robot=plate.physical_shift_robot,
                         _coefficients=coefficients, _fit_center=center,
                         _samples=np.asarray(samples), _tin_xyz=physical,
                         _tin_tri=triangles, _rms=float(np.sqrt(np.mean(residuals**2))),
                         _worst=labels[worst + 4])
    origin = result.origin.copy()
    origin[2] = result.datum(*origin[:2])
    return replace(result, origin=origin, tilt=float(slopes[steepest]))


def fit_sensed(plate, records, *, base=40, max_residual_mm=0.5, fit="smooth") -> SensedPlate:
    _require_physical(plate)
    if fit not in ("smooth", "interpolate"):
        raise ValueError("Sensed fit must be smooth or interpolate")
    if type(base) is not int or not 0 <= base <= 225:
        raise ValueError("Sensed point base must be an integer from 0 to 225")
    if not isinstance(records, list) or not 2 <= len(records) <= 25:
        raise ValueError("Sensed substrate needs from 2 to 25 PULSE records")
    if (type(max_residual_mm) not in (int, float) or not np.isfinite(max_residual_mm)
            or max_residual_mm < 0):
        raise ValueError("Maximum sensed residual must be finite and nonnegative")
    samples = []
    labels = [f"corner {i}" for i in range(1, 5)]
    for i, record in enumerate(records):
        label = f"P{base + i:03d}"
        if (not isinstance(record, list) or len(record) != 6 or
                any(type(v) not in (float, int) or not np.isfinite(v) or float(v) != int(v) for v in record)):
            raise ValueError(f"{label}: enter six whole PULSE values in S, L, U, R, B, T order")
        try:
            point = cell_lc.tcp_from_pulses(record)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
        uv = np.linalg.solve(plate.basis[:2, :2], point[:2] - plate.origin[:2])
        if np.min(plate.edge_distances(uv)) < -1e-8:
            raise ValueError(f"{label}: sensed point lies outside the substrate polygon. Recheck the touch and corner registration.")
        samples.append(point)
        labels.append(label)
    physical = np.vstack((plate.points + plate.physical_shift_robot, samples))
    if fit == "interpolate":
        return _fit_interpolated(plate, physical, samples, labels, max_residual_mm)
    center = plate.origin[:2].copy()
    scale = 100.0
    terms = _terms((physical[:, :2] - center) / scale, len(physical) >= 10)
    coefficients, _, rank, _ = np.linalg.lstsq(terms, physical[:, 2], rcond=None)
    if rank < terms.shape[1]:
        raise ValueError("Sensed points do not span a surface fit. Spread the touches across the substrate.")
    residuals = physical[:, 2] - terms @ coefficients
    worst = int(np.argmax(np.abs(residuals)))
    residual = float(abs(residuals[worst]))
    if residual > max_residual_mm:
        raise ValueError(f"{labels[worst]} differs from the fitted surface by {residual:.2f} mm (limit {max_residual_mm:g} mm). Recheck the touch and substrate surface.")
    origin = plate.origin.copy()
    origin[2] = coefficients[0] - plate.physical_shift_robot[2]
    result = SensedPlate(points=plate.points.copy(), origin=origin, basis=plate.basis.copy(),
                         corners=plate.corners.copy(), thickness=plate.thickness,
                         residual=residual, tilt=0.0, torch_tilt_deg=plate.torch_tilt_deg,
                         gap_direction=plate.gap_direction,
                         tool0_tip_offset_mm=plate.tool0_tip_offset_mm,
                         physical_shift_robot=plate.physical_shift_robot,
                         _coefficients=coefficients, _fit_center=center, _scale=scale,
                         _samples=np.asarray(samples), _rms=float(np.sqrt(np.mean(residuals**2))),
                         _worst=labels[worst])

    slope = np.linalg.norm(result._gradient(result._polygon()), axis=1).max()
    tilt = float(np.degrees(np.arctan(slope)))
    if tilt > MAX_TILT_DEG:
        raise ValueError("Sensed substrate tilt exceeds 5 degrees. Level the plate for vertical-torch welding.")
    return replace(result, tilt=tilt)
