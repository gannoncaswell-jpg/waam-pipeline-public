from dataclasses import dataclass, replace

import numpy as np

from .clearance import (CONTACT_OK, DEFAULT_MARGIN_MM, Scene, TorchModel,
                        Violation, blocking, check)
from .toolpath import EmptyToolpath, Toolpath


@dataclass
class Correction:
    toolpath: Toolpath
    violations: list[Violation]
    before: int
    repaired_travels: int = 0

    def summary(self):
        remaining = len(blocking(self.violations))
        return {"before": self.before, "remaining": remaining,
                "repaired_travels": self.repaired_travels,
                "status": "clear" if not remaining else "unresolved"}


def repair_travels(toolpath: Toolpath, torch: TorchModel, scene: Scene, *,
                   bead_width_mm=9.0, bead_height_mm=3.0,
                   margin_mm=DEFAULT_MARGIN_MM,
                   enabled=True) -> Correction:


    options = dict(bead_width_mm=bead_width_mm, bead_height_mm=bead_height_mm,
                   margin_mm=margin_mm, contact_ok=CONTACT_OK)
    original = check(toolpath, torch, scene, **options)
    hits = blocking(original)
    result = Correction(toolpath, original, len(hits))
    if not enabled or not hits:
        return result
    bad = {v.move_index for v in hits}
    spheres = torch.spheres()
    reach = float(spheres[:, 1].max()) + margin_mm
    bottom = float((spheres[:, 0] - spheres[:, 1]).min())
    candidate, owners = [], []
    repaired = 0
    built_top = -np.inf
    for i, move in enumerate(toolpath.moves):
        if i and i in bad and not move.deposit:
            prev = toolpath.moves[i - 1]
            lo = np.minimum(prev.target[:2], move.target[:2]) - reach
            hi = np.maximum(prev.target[:2], move.target[:2]) + reach
            top = built_top + bead_width_mm / 2
            for _, a, b in scene.boxes:
                if np.all(b[:2] >= lo) and np.all(a[:2] <= hi):
                    top = max(top, b[2])
            for _, a, b, radius in scene.capsules:
                if (np.all(np.maximum(a[:2], b[:2]) + radius >= lo) and
                        np.all(np.minimum(a[:2], b[:2]) - radius <= hi)):
                    top = max(top, a[2] + radius, b[2] + radius)
            for substrate in scene.substrates:
                vertices = substrate.mesh().reshape(-1, 3)
                if np.all(vertices.max(0)[:2] >= lo) and np.all(vertices.min(0)[:2] <= hi):
                    top = max(top, vertices[:, 2].max())
            safe_z = max(prev.z, move.z, top - bottom + margin_mm + 1.0)

            if np.isfinite(safe_z) and safe_z - max(prev.z, move.z) <= 300.0:
                for x, y in ((prev.x, prev.y), (move.x, move.y)):
                    p = replace(move, x=x, y=y, z=float(safe_z), deposit=False)
                    if not candidate or candidate[-1].target != p.target:
                        candidate.append(p)
                        owners.append(i)
                repaired += 1
        candidate.append(move)
        owners.append(i)
        if move.deposit:

            built_top = max(built_top, bead_height_mm + max(
                move.z, toolpath.moves[i - 1].z if i else move.z))
    if not repaired:
        return result
    trial = replace(toolpath, moves=candidate)
    checked = check(trial, torch, scene, **options)
    still_bad = {owners[v.move_index] for v in blocking(checked)}
    if still_bad < bad:
        return Correction(trial, checked, len(hits), repaired)
    return result


def _deposit_points(toolpath, what: str) -> np.ndarray:


    pts = np.array(toolpath.deposit_points())
    if not pts.size:
        raise EmptyToolpath(f"cannot {what}: this toolpath deposits nothing")
    return pts


def rotate_part(toolpath, mesh, degrees):

    pts = _deposit_points(toolpath, "rotate the part")
    pivot = (pts[:, :2].min(axis=0) + pts[:, :2].max(axis=0)) / 2
    a = np.radians(degrees)
    rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    moves = []
    for m in toolpath.moves:
        xy = (np.array([m.x, m.y]) - pivot) @ rot.T + pivot
        moves.append(replace(m, x=float(xy[0]), y=float(xy[1])))
    if mesh is not None:
        mesh = mesh.copy()
        mesh[:, :, :2] = (mesh[:, :, :2] - pivot) @ rot.T + pivot
    return replace(toolpath, moves=moves), mesh


def footprint(toolpath, mesh, bead_width):
    pts = _deposit_points(toolpath, "measure the footprint")
    lo, hi = pts[:, :2].min(axis=0) - bead_width / 2, pts[:, :2].max(axis=0) + bead_width / 2
    if mesh is not None:
        lo = np.minimum(lo, mesh[:, :, :2].min(axis=(0, 1)))
        hi = np.maximum(hi, mesh[:, :, :2].max(axis=(0, 1)))
    return lo, hi


def placement_limits(bounds):
    from . import cell_lc as cell
    length, width, _ = cell.CHARLES_PLATE
    lo = np.array([cell.FACEPLATE_X - width / 2, cell.FACEPLATE_Y]) - bounds[0]
    hi = np.array([cell.FACEPLATE_X + width / 2, cell.FACEPLATE_Y + length]) - bounds[1]
    if np.any(lo > hi):
        raise ValueError("Part footprint does not fit on the platter; rotate it or reduce its size")
    return lo, hi


def placement_candidates(bounds, origin):

    from . import cell_lc as cell
    lo, hi = placement_limits(bounds)
    xs = [origin[0], lo[0], hi[0], (lo[0] + hi[0]) / 2]
    ys = [origin[1], lo[1], hi[1], (lo[1] + hi[1]) / 2]
    block = next(b for b in cell.lc_scene(np.zeros(3), training_block=True).boxes
                 if b[0] == "manpro_training_block")
    for axis, values in enumerate((xs, ys)):
        values.extend([block[1][axis] - bounds[1][axis] - 35,
                       block[2][axis] - bounds[0][axis] + 35])
    points = {tuple(np.clip([x, y], lo, hi)) for x in xs for y in ys}
    points.discard(tuple(origin[:2]))
    return sorted(points, key=lambda p: (np.linalg.norm(np.array(p) - origin[:2]), p))[:16]
