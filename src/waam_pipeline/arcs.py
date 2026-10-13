from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PassMotion:
    interp: list[str]
    fpt: list[bool]
    pl: list[int]
    separators: list[int]
    arcs: list[tuple[int, int, int]]


@dataclass(frozen=True)
class _Piece:
    first: int
    mid: int
    last: int
    start_tangent: np.ndarray
    end_tangent: np.ndarray


def _planar(points: np.ndarray, tol_mm: float) -> bool:
    centered = points - np.mean(points, axis=0)
    _, singular, axes = np.linalg.svd(centered, full_matrices=False)
    if singular[1] <= singular[0] * max(points.shape) * np.finfo(float).eps:
        return False
    return bool(np.max(np.abs(centered @ axes[-1])) <= tol_mm)


def _fit_piece(points: np.ndarray, first: int, last: int, *, tol_mm: float,
               min_radius_mm: float, max_radius_mm: float,
               max_sweep_deg: float) -> _Piece | None:
    run = points[first:last + 1]
    lengths = np.linalg.norm(np.diff(run, axis=0), axis=1)
    if np.any(lengths < 0.01):
        return None
    distances = np.r_[0.0, np.cumsum(lengths)]
    middle = int(np.argmin(np.abs(distances - distances[-1] / 2.0)))
    split = distances[middle] / distances[-1]
    if not 0.25 <= split <= 0.75:
        return None

    if not _planar(run, tol_mm):
        return None

    a = run[0]
    ab = run[middle] - a
    ac = run[-1] - a
    normal = np.cross(ab, ac)
    normal_length = float(np.linalg.norm(normal))
    ab_length = float(np.linalg.norm(ab))
    if normal_length <= np.finfo(float).eps * ab_length * np.linalg.norm(ac):
        return None
    normal /= normal_length
    u = ab / ab_length
    v = np.cross(normal, u)
    cx, cy = float(ac @ u), float(ac @ v)
    center = a + ab / 2.0 + v * ((ac @ ac - cx * ab_length) / (2.0 * cy))
    radius = float(np.linalg.norm(a - center))
    radius_epsilon = 1e-9 * max(1.0, radius)
    if not min_radius_mm - radius_epsilon <= radius <= max_radius_mm + radius_epsilon:
        return None

    samples = np.concatenate((run, (run[:-1] + run[1:]) / 2.0))
    delta = samples - center
    heights = delta @ normal
    radial = np.linalg.norm(delta - heights[:, None] * normal, axis=1)
    if np.any(np.hypot(radial - radius, heights) > tol_mm + 1e-10):
        return None

    radial_vectors = run - center
    radial_vectors -= (radial_vectors @ normal)[:, None] * normal
    turns = np.arctan2(np.cross(radial_vectors[:-1], radial_vectors[1:]) @ normal,
                      np.sum(radial_vectors[:-1] * radial_vectors[1:], axis=1))
    if np.all(turns > 1e-12):
        direction = 1.0
    elif np.all(turns < -1e-12):
        direction = -1.0
    else:
        return None
    if abs(float(np.sum(turns))) > np.deg2rad(max_sweep_deg) + 1e-10:
        return None

    tangent_start = direction * np.cross(normal, a - center) / radius
    tangent_end = direction * np.cross(normal, run[-1] - center) / radius
    return _Piece(first, first + middle, last, tangent_start, tangent_end)


def _straight_window(points: np.ndarray, distances: np.ndarray, vertex: int,
                     window_mm: float, tol_mm: float) -> bool:
    low = max(0.0, distances[vertex] - window_mm)
    high = min(distances[-1], distances[vertex] + window_mm)
    start = np.array([np.interp(low, distances, points[:, axis]) for axis in range(3)])
    end = np.array([np.interp(high, distances, points[:, axis]) for axis in range(3)])
    direction = end - start
    length = float(np.linalg.norm(direction))
    if length < 1e-12:
        return False
    inside = points[(distances >= low) & (distances <= high)]
    deviation = np.linalg.norm(np.cross(inside - start, direction / length), axis=1)
    return bool(np.all(deviation <= tol_mm + 1e-10))


def segment_pass(points, *, tol_mm=0.2, min_radius_mm=3.0, max_radius_mm=2000.0,
                 max_sweep_deg=180.0, tangent_deg=10.0, straight_window_mm=25.0,
                 straight_tol_mm=0.1) -> PassMotion:


    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) == 0:
        raise ValueError("pass points must have shape (n+1, 3)")
    if not np.all(np.isfinite(points)):
        raise ValueError("pass points must be finite")
    parameters = (tol_mm, min_radius_mm, max_radius_mm, max_sweep_deg,
                  tangent_deg, straight_window_mm, straight_tol_mm)
    if not all(np.isfinite(value) for value in parameters):
        raise ValueError("motion tolerances must be finite")
    if (tol_mm < 0 or min_radius_mm <= 0 or max_radius_mm < min_radius_mm
            or max_sweep_deg <= 0 or tangent_deg < 0 or straight_window_mm <= 0
            or straight_tol_mm < 0):
        raise ValueError("invalid motion tolerances")

    count = len(points)
    interp = ["linear"] * count
    fpt = [False] * count
    pl = [0] * count
    separators: list[int] = []
    arcs: list[tuple[int, int, int]] = []
    previous: _Piece | None = None
    first = 1
    while first + 3 < count:
        piece = None


        for last in range(first + 3, count):
            candidate = _fit_piece(points, first, last, tol_mm=tol_mm,
                                   min_radius_mm=min_radius_mm,
                                   max_radius_mm=max_radius_mm,
                                   max_sweep_deg=max_sweep_deg)
            if candidate is None:
                break
            piece = candidate
        if piece is None:
            first += 1
            continue

        chained = previous is not None and first == previous.last
        if chained:
            cosine = float(np.clip(previous.end_tangent @ piece.start_tangent, -1.0, 1.0))
            if np.rad2deg(np.arccos(cosine)) > tangent_deg + 1e-9:
                first += 1
                continue
            fpt[first] = True
        elif previous is not None and first == previous.last + 1:
            separators.append(first)

        interp[first:piece.last + 1] = ["drop"] * (piece.last - first + 1)
        for vertex in (piece.first, piece.mid, piece.last):
            interp[vertex] = "circular"
        arcs.append((piece.first, piece.mid, piece.last))
        previous = piece
        first = piece.last

    distances = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    following = count - 1
    separator_set = set(separators)
    for vertex in range(count - 2, 0, -1):
        if interp[vertex] == "linear" and interp[following] == "linear":
            if not any(chord in separator_set for chord in range(vertex + 1, following + 1)):
                pl[vertex] = int(_straight_window(points, distances, vertex,
                                                  straight_window_mm, straight_tol_mm))
        if interp[vertex] != "drop":
            following = vertex
    return PassMotion(interp, fpt, pl, separators, arcs)
