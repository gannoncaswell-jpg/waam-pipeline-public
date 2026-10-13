from __future__ import annotations

import re
import struct
from dataclasses import replace
from math import hypot, isfinite
from pathlib import Path

import numpy as np

from .toolpath import Move, Toolpath


def wall(
    length_mm: float,
    layers: int,
    layer_height_mm: float,
    feed_mm_min: float = 200.0,
) -> Toolpath:


    tp = Toolpath()
    x0, x1 = 0.0, length_mm
    for k in range(layers):
        z = (k + 1) * layer_height_mm
        start, end = (x0, x1) if k % 2 == 0 else (x1, x0)
        tp.moves.append(Move(start, 0.0, z, None, False, k, k * 2 + 1))
        tp.moves.append(Move(end, 0.0, z, feed_mm_min, True, k, k * 2 + 2))
    return tp


def interleave(
    parts: list[Toolpath],
    offsets_xy: list[tuple[float, float]],
    *,
    lift_mm: float,
    bead_height_mm: float,
) -> Toolpath:

    if not 2 <= len(parts) <= 4:
        raise ValueError("interleave needs 2 to 4 parts")
    if len(offsets_xy) != len(parts):
        raise ValueError("interleave needs one XY offset per part")
    if isinstance(lift_mm, bool) or not isinstance(lift_mm, (int, float)) \
            or not isfinite(lift_mm) or lift_mm <= 0:
        raise ValueError("interleave lift must be a positive finite distance")
    if isinstance(bead_height_mm, bool) or not isinstance(bead_height_mm, (int, float)) \
            or not isfinite(bead_height_mm) or bead_height_mm <= 0:
        raise ValueError("interleave bead height must be a positive finite distance")
    if any(not isinstance(part, Toolpath) for part in parts):
        raise ValueError("interleave parts must be Toolpaths")

    groups = []
    for copy_index, part in enumerate(parts):
        normalised = []
        offset = 0
        for group in part.layers():
            layer = list(group)
            if layer[0].deposit:
                if offset == 0:
                    raise ValueError(
                        f"copy {copy_index + 1} starts with a deposit move and has no weld start"
                    )
                layer.insert(0, replace(part.moves[offset - 1], deposit=False,
                                        feed=None, layer=layer[0].layer))
            normalised.append(layer)
            offset += len(group)
        groups.append(normalised)
    counts = [len(layer_groups) for layer_groups in groups]
    if not counts[0] or any(count != counts[0] for count in counts[1:]):
        raise ValueError("interleave parts must have the same layer count")
    labels = [[group[0].layer for group in layer_groups] for layer_groups in groups]
    if any(layer_labels != labels[0] for layer_labels in labels[1:]):
        raise ValueError("interleave parts must have matching layer indices")

    offsets = []
    for index, offset in enumerate(offsets_xy):
        if (not isinstance(offset, (tuple, list)) or len(offset) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not isfinite(value) for value in offset)):
            raise ValueError(f"copy {index + 1} needs a finite XY offset")
        offsets.append((float(offset[0]), float(offset[1])))

    footprints = []
    for index, (part, (dx, dy)) in enumerate(zip(parts, offsets)):
        points = part.deposit_points()
        if not points:
            raise ValueError(f"copy {index + 1} has no deposited footprint")
        xs = [point[0] + dx for point in points]
        ys = [point[1] + dy for point in points]
        footprints.append((min(xs), max(xs), min(ys), max(ys)))
    for left in range(len(footprints)):
        ax0, ax1, ay0, ay1 = footprints[left]
        for right in range(left + 1, len(footprints)):
            bx0, bx1, by0, by1 = footprints[right]
            gap_x = max(0.0, ax0 - bx1, bx0 - ax1)
            gap_y = max(0.0, ay0 - by1, by0 - ay1)
            if hypot(gap_x, gap_y) < 10.0 - 1e-9:
                raise ValueError(
                    f"copies {left + 1} and {right + 1} overlap their 10 mm clearance"
                )

    translated = []
    for copy_index, (layer_groups, (dx, dy)) in enumerate(zip(groups, offsets)):
        translated.append([
            [replace(move, x=move.x + dx, y=move.y + dy, copy=copy_index)
             for move in layer]
            for layer in layer_groups
        ])

    moves = []
    tallest = -float("inf")
    ordered = [(layer_index, copy_index)
               for layer_index in range(counts[0])
               for copy_index in range(len(parts))]
    for order_index, (layer_index, copy_index) in enumerate(ordered):
        layer = translated[copy_index][layer_index]
        moves.extend(layer)
        for previous, move in zip(layer, layer[1:]):
            if move.deposit:
                tallest = max(tallest, previous.z + float(bead_height_mm),
                              move.z + float(bead_height_mm))
        if order_index + 1 == len(ordered):
            continue
        next_layer, next_copy = ordered[order_index + 1]
        approach = translated[next_copy][next_layer][0]
        last = moves[-1]
        travel_z = max(last.z, tallest + float(lift_mm))
        moves.append(replace(last, z=travel_z, feed=None, deposit=False))
        moves.append(replace(last, x=approach.x, y=approach.y, z=travel_z,
                             feed=None, deposit=False))

    unhandled = [entry for part in parts for entry in part.unhandled]
    return Toolpath(moves, unhandled, copies=len(parts))


def load_stl(path: str | Path, scale: float = 1.0) -> np.ndarray:

    return parse_stl(Path(path).read_bytes(), scale)


def parse_stl(raw: bytes, scale: float = 1.0) -> np.ndarray:

    if raw[:5].lower() == b"solid" and b"facet" in raw[:1000]:
        floats = re.findall(
            rb"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", raw
        )
        tri = np.array(floats, dtype=float).reshape(-1, 3, 3)
    else:
        (count,) = struct.unpack_from("<I", raw, 80)
        rec = np.frombuffer(raw, dtype=np.uint8, count=count * 50, offset=84)
        rec = rec.reshape(count, 50)[:, 12:48].copy()
        tri = rec.view(np.float32).reshape(count, 3, 3).astype(float)
    return tri * scale


_STL_UNIT_SCALE_MM = {"mm": 1.0, "in": 25.4, "m": 1000.0}
_STL_UNIT_ALIASES = {
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
}


def scale_stl_to_mm(
    triangles: np.ndarray, units: str = "auto"
) -> tuple[np.ndarray, str, float]:


    tri = np.asarray(triangles, dtype=float)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3) or len(tri) == 0:
        raise ValueError("STL contains no triangles")
    if not np.isfinite(tri).all():
        raise ValueError("STL contains non-finite coordinates")

    extent = np.ptp(tri, axis=(0, 1))
    longest = float(np.max(extent))
    if longest <= 1e-9:
        raise ValueError("STL has zero-size geometry")

    requested = str(units or "auto").strip().lower()
    if requested == "auto":
        resolved = "m" if longest < 1.0 else "in" if longest < 10.0 else "mm"
    else:
        try:
            resolved = _STL_UNIT_ALIASES[requested]
        except KeyError as exc:
            raise ValueError("STL units must be auto, mm, in, or m") from exc
    scale = _STL_UNIT_SCALE_MM[resolved]
    return tri * scale, resolved, scale


def transform(
    triangles: np.ndarray,
    rotate_deg: float = 0.0,
    axis: str = "y",
    translate: tuple[float, float, float] = (0.0, 0.0, 0.0),
    drop_to_z0: bool = True,
) -> np.ndarray:


    t = np.radians(rotate_deg)
    c, s = np.cos(t), np.sin(t)
    rot = {
        "x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]),
        "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
        "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]),
    }[axis]
    out = triangles @ rot.T + np.asarray(translate, float)
    if drop_to_z0:
        out[..., 2] -= out[..., 2].min()
    return out


def inflate_mesh(triangles: np.ndarray, offset_mm: float) -> np.ndarray:


    verts = triangles.reshape(-1, 3)
    keys = np.round(verts / 1e-3).astype(np.int64)
    _, index, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    unique_verts = verts[index]
    normals_acc = np.zeros_like(unique_verts)
    tri_n = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    lengths = np.linalg.norm(tri_n, axis=1, keepdims=True)
    tri_n = np.divide(tri_n, np.maximum(lengths, 1e-12))
    for corner in range(3):
        np.add.at(normals_acc, inverse[corner::3], tri_n)
    norms = np.linalg.norm(normals_acc, axis=1, keepdims=True)
    normals = np.divide(normals_acc, np.maximum(norms, 1e-12))
    moved = unique_verts + offset_mm * normals
    return moved[inverse].reshape(triangles.shape)


def write_stl(triangles: np.ndarray, path: str | Path, name: bytes = b"waam") -> Path:

    import struct

    out = bytearray(name.ljust(80, b"\0"))
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        n = n / max(np.linalg.norm(n), 1e-12)
        out += struct.pack("<3f", *n)
        for v in tri:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    p = Path(path)
    p.write_bytes(bytes(out))
    return p


MAX_LAYERS = 500


def _plane_segments(triangles: np.ndarray, z: float) -> list[tuple[np.ndarray, np.ndarray]]:


    above = triangles[:, :, 2] >= z
    count = above.sum(axis=1)
    tris = triangles[(count > 0) & (count < 3)]
    if not len(tris):
        return []
    a, b = tris, np.roll(tris, -1, axis=1)
    za, zb = a[:, :, 2], b[:, :, 2]
    cross = (za >= z) != (zb >= z)
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(cross, (z - za) / np.where(cross, zb - za, 1.0), 0.0)
    pts = a + f[:, :, None] * (b - a)
    first = np.argmax(cross, axis=1)
    second = 2 - np.argmax(cross[:, ::-1], axis=1)
    rows = np.arange(len(tris))
    p0, p1 = pts[rows, first], pts[rows, second]
    keep = np.linalg.norm(p0 - p1, axis=1) > 1e-9
    return [(p0[i], p1[i]) for i in np.flatnonzero(keep)]


def _chain_loops(
    segments: list[tuple[np.ndarray, np.ndarray]], tol: float = 0.05
) -> list[np.ndarray]:

    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))

    index: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in enumerate(segments):
        index.setdefault(key(a), []).append(i)
        index.setdefault(key(b), []).append(i)
    used = [False] * len(segments)
    loops = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        a, b = segments[start]
        loop = [a, b]
        while True:
            candidates = [
                i for i in index.get(key(loop[-1]), []) if not used[i]
            ]
            if not candidates:
                break
            i = candidates[0]
            used[i] = True
            sa, sb = segments[i]
            loop.append(sb if np.linalg.norm(sa - loop[-1]) < np.linalg.norm(sb - loop[-1]) else sa)
            if np.linalg.norm(loop[-1] - loop[0]) < tol:
                break
        if len(loop) >= 3:
            loops.append(np.array(loop))
    return loops


def _reduce_collinear(loop: np.ndarray, deviation_mm: float = 0.05) -> np.ndarray:


    keep = [0]
    i = 0
    while i < len(loop) - 1:
        j = i + 2
        while j < len(loop):
            a, b = loop[i], loop[j]
            chord = b - a
            n = np.linalg.norm(chord)
            if n < 1e-9:
                break
            mids = loop[i + 1 : j]
            d = np.linalg.norm(np.cross(mids - a, chord / n), axis=1)
            if np.max(d) > deviation_mm:
                break
            j += 1
        keep.append(j - 1)
        i = j - 1
    return loop[sorted(set(keep))]


def _seam_start(loop: np.ndarray, seam_angle_deg: float) -> np.ndarray:


    center = loop.mean(axis=0)
    angles = np.degrees(np.arctan2(loop[:, 1] - center[1], loop[:, 0] - center[0]))
    idx = int(np.argmin(np.abs((angles - seam_angle_deg + 180) % 360 - 180)))
    return np.roll(loop, -idx, axis=0)


def _nearest_seam(loop: np.ndarray, point: np.ndarray) -> np.ndarray:
    edges = np.roll(loop, -1, axis=0) - loop
    lengths2 = np.sum(edges[:, :2] ** 2, axis=1)
    fractions = np.divide(
        np.sum((point[:2] - loop[:, :2]) * edges[:, :2], axis=1),
        lengths2,
        out=np.zeros(len(loop)),
        where=lengths2 > 0,
    ).clip(0, 1)
    candidates = loop + fractions[:, None] * edges
    idx = int(np.argmin(np.linalg.norm(candidates[:, :2] - point[:2], axis=1)))
    if fractions[idx] == 0:
        return np.roll(loop, -idx, axis=0)
    if fractions[idx] == 1:
        return np.roll(loop, -(idx + 1), axis=0)
    return np.vstack([candidates[idx], loop[idx + 1 :], loop[: idx + 1]])


def _spiralize_run(
    tp: Toolpath,
    loops: list[np.ndarray],
    start_layer: int,
    layer_height_mm: float,
    feed_mm_min: float,
    seam_angle_deg: float,
) -> None:
    winding = None
    for offset, loop in enumerate(loops):
        k = start_layer + offset
        z_base = k * layer_height_mm
        z_top = (k + 1) * layer_height_mm
        if np.linalg.norm(loop[0] - loop[-1]) < 1e-6:
            loop = loop[:-1]
        area = np.sum(
            loop[:, 0] * np.roll(loop[:, 1], -1)
            - loop[:, 1] * np.roll(loop[:, 0], -1)
        )
        if winding is None:
            winding = area
        elif area * winding < 0:
            loop = loop[::-1]

        if offset == 0:
            loop = _seam_start(loop, seam_angle_deg)

            if tp.moves and tp.moves[-1].target == (loop[0][0], loop[0][1], z_base):
                loop = np.roll(loop, -1, axis=0)
            tp.moves.append(Move(loop[0][0], loop[0][1], z_base, None, False, k, len(tp.moves) + 1))
        else:
            loop = _nearest_seam(loop, np.asarray(tp.moves[-1].target))

        targets = np.vstack([loop, loop[0]])
        origin = np.asarray(tp.moves[-1].target)
        lengths = np.linalg.norm(np.diff(np.vstack([origin, targets])[:, :2], axis=0), axis=1)
        distances = np.cumsum(lengths)

        for p, length, distance in zip(targets, lengths, distances):
            if length == 0:
                continue
            z = z_base + (z_top - z_base) * distance / distances[-1]
            tp.moves.append(Move(p[0], p[1], z, feed_mm_min, True, k, len(tp.moves) + 1))


    for p in list(loop[1:]) + [loop[0]]:
        tp.moves.append(Move(p[0], p[1], z_top, feed_mm_min, True, k, len(tp.moves) + 1))


def _travel_order(
    loops: list[np.ndarray], point: np.ndarray | None,
) -> list[tuple[np.ndarray, bool]]:
    pending = []
    for loop in loops:
        closed = np.linalg.norm(loop[0] - loop[-1]) < 1.0
        if closed and np.linalg.norm(loop[0] - loop[-1]) < 1e-6:
            loop = loop[:-1]
        pending.append((loop, closed))
    if point is None and pending:
        point = pending[0][0][0]
    ordered = []
    while pending:
        best_distance = float("inf")
        for i, (loop, closed) in enumerate(pending):
            candidates = loop if closed else loop[[0, -1]]
            distances = np.sum((candidates[:, :2] - point[:2]) ** 2, axis=1)
            entry = int(np.argmin(distances))

            if distances[entry] < best_distance:
                best_distance = distances[entry]
                best = i, entry
        i, entry = best
        loop, closed = pending.pop(i)
        if closed:
            loop = np.roll(loop, -entry, axis=0)
        elif entry == 1:
            loop = loop[::-1]
        ordered.append((loop, closed))
        point = loop[0] if closed else loop[-1]
    return ordered


def _add_run_ins(tp: Toolpath, run_in_mm: float) -> None:
    moves = []
    for i, start in enumerate(tp.moves):
        moves.append(start)
        if start.layer != 0 or start.deposit:
            continue
        direction = None
        previous = start
        for j in range(i + 1, len(tp.moves)):
            target = tp.moves[j]
            if not target.deposit or target.layer != 0:
                break
            edge = np.asarray(target.target[:2]) - previous.target[:2]
            length = np.linalg.norm(edge)
            if length >= 1e-9:
                direction = edge / length
                break
            previous = target
        if direction is None:
            continue
        x, y = np.asarray(start.target[:2]) - run_in_mm * direction
        moves[-1] = replace(start, x=x, y=y)

        moves.append(replace(start, feed=tp.moves[i + 1].feed, deposit=True))
    tp.moves = moves


def slice_mesh(
    triangles: np.ndarray,
    layer_height_mm: float,
    feed_mm_min: float = 200.0,
    seam_rotation_deg_per_layer: float = 25.0,
    point_deviation_mm: float = 0.05,
    spiralize: bool = False,
    optimize_travel: bool = False,
    run_in_mm: float = 0.0,
) -> Toolpath:


    if not np.isfinite(run_in_mm) or run_in_mm < 0:
        raise ValueError("run_in_mm must be finite and >= 0")
    tp = Toolpath()
    z_min = float(triangles[:, :, 2].min())
    z_max = float(triangles[:, :, 2].max())


    if abs(z_min) > 1e-3:
        raise ValueError(
            f"the mesh must be transformed to millimetres with its bottom at "
            f"Z=0; its lowest point is z={z_min:.3f} mm. slicer.transform() "
            "drops it, at any tilt"
        )
    n_layers = max(1, int(round(z_max / layer_height_mm)))
    if n_layers > MAX_LAYERS:


        raise ValueError(
            f"this part needs {n_layers} layers at {layer_height_mm:g} mm; the limit "
            f"is {MAX_LAYERS}. Increase the bead height or reduce the part height"
        )
    layer_loops = []
    for k in range(n_layers):
        z_cut = (k + 0.5) * layer_height_mm
        loops = _chain_loops(_plane_segments(triangles, z_cut))
        loops = [_reduce_collinear(lp, point_deviation_mm) for lp in loops]
        loops = [lp for lp in loops if len(lp) >= 3]
        layer_loops.append(loops)


    empty = [k for k, loops in enumerate(layer_loops) if not loops]
    if empty:
        raise ValueError(
            f"{len(empty)} of {n_layers} cut planes inside the mesh produced no "
            f"contour, the first at z={(empty[0] + 0.5) * layer_height_mm:g} mm. "
            "The mesh is not closed, or the wall there is thinner than the "
            "chaining tolerance -- slicing on would drop those layers silently"
        )

    line = 1
    k = 0
    while k < n_layers:
        if spiralize:
            end = k
            while end < n_layers:
                loops = layer_loops[end]
                if len(loops) != 1 or not (np.linalg.norm(loops[0][0] - loops[0][-1]) < 1.0):
                    break
                end += 1
            if end - k >= 2:
                _spiralize_run(
                    tp, [layer_loops[i][0] for i in range(k, end)], k,
                    layer_height_mm, feed_mm_min, k * seam_rotation_deg_per_layer,
                )
                line = len(tp.moves) + 1
                k = end
                continue
        loops = layer_loops[k]
        z_dep = (k + 1) * layer_height_mm
        ordered = [(loop, np.linalg.norm(loop[0] - loop[-1]) < 1.0) for loop in loops]
        if optimize_travel:
            point = np.asarray(tp.moves[-1].target) if tp.moves else None
            ordered = _travel_order(loops, point)
        for loop, closed in ordered:
            if closed and not optimize_travel:
                if np.linalg.norm(loop[0] - loop[-1]) < 1e-6:
                    loop = loop[:-1]
                loop = _seam_start(loop, k * seam_rotation_deg_per_layer)
            tp.moves.append(Move(loop[0][0], loop[0][1], z_dep, None, False, k, line))
            line += 1


            targets = list(loop[1:]) + ([loop[0]] if closed else [])
            for p in targets:
                tp.moves.append(Move(p[0], p[1], z_dep, feed_mm_min, True, k, line))
                line += 1
        k += 1
    if run_in_mm > 0:
        _add_run_ins(tp, run_in_mm)
    return tp


def to_gcode(toolpath: Toolpath) -> str:

    lines = ["; generated by waam_pipeline.slicer", "G21", "G90", "M83"]
    layer = None
    for m in toolpath.moves:
        if m.layer != layer:
            lines.append(f";LAYER:{m.layer}")
            layer = m.layer
        cmd = "G1" if m.deposit else "G0"
        words = f"{cmd} X{m.x:.3f} Y{m.y:.3f} Z{m.z:.3f}"
        if m.deposit:
            words += " E1"
        if m.feed is not None and m.deposit:
            words += f" F{m.feed:.0f}"
        lines.append(words)
    return "\n".join(lines) + "\n"


def elbow_mesh(
    bend_radius_mm: float = 100.0,
    tube_radius_mm: float = 25.0,
    rings: int = 60,
    around: int = 40,
) -> np.ndarray:


    us = np.linspace(0.0, np.pi / 2, rings)
    vs = np.linspace(0.0, 2 * np.pi, around, endpoint=False)
    grid = np.empty((rings, around, 3))
    for i, u in enumerate(us):
        center = np.array([bend_radius_mm * (1 - np.cos(u)), 0.0, bend_radius_mm * np.sin(u)])
        n_hat = np.array([-np.cos(u), 0.0, np.sin(u)])
        b_hat = np.array([0.0, 1.0, 0.0])
        for j, v in enumerate(vs):
            grid[i, j] = center + tube_radius_mm * (np.cos(v) * n_hat + np.sin(v) * b_hat)
    tris = []
    for i in range(rings - 1):
        for j in range(around):
            j2 = (j + 1) % around
            a, b, c, d = grid[i, j], grid[i, j2], grid[i + 1, j], grid[i + 1, j2]
            tris.append([a, b, c])
            tris.append([b, d, c])
    return np.array(tris)
