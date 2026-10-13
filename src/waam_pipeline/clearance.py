from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from .toolpath import Toolpath


_CLEARANCE_EPS_MM = 1e-6


DEFAULT_MARGIN_MM = 5.0

YASKAWA_45_TCP = np.array([-110.247, 0.324, 351.972])
YASKAWA_45_CAPSULES: tuple[tuple[tuple[float, float, float], tuple[float, float, float], float], ...] = (
    ((0.0, 0.0, 0.0), (0.0, 0.0, 154.0), 50.0),
    ((0.0, 0.0, 154.0), (0.0, 0.0, 261.0), 20.0),
    ((0.0, 0.0, 261.0), (-90.0, 0.0, 351.0), 20.0),
)


@dataclass(frozen=True)
class TorchStackup:


    ctwd_mm: float
    contact_tip_protrusion_mm: float | None = None
    nozzle_diameter_mm: float | None = None
    nozzle_length_mm: float | None = None

    def __post_init__(self) -> None:
        for name in ("ctwd_mm", "contact_tip_protrusion_mm",
                     "nozzle_diameter_mm", "nozzle_length_mm"):
            value = getattr(self, name)
            if value is None and name != "ctwd_mm":
                continue
            if value is None or not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
            if name in ("nozzle_diameter_mm", "nozzle_length_mm") and value == 0:
                raise ValueError(f"{name} must be positive when known")

    @classmethod
    def from_profile(cls, path: str | Path | None = None) -> "TorchStackup":


        if path is None:
            path = Path(__file__).resolve().parents[2] / "examples" / "cell_lc50s.json"
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        dimensions = data.get("torch_stackup", {})
        return cls(ctwd_mm=data["ctwd_mm"], **{
            name: dimensions.get(name) for name in (
                "contact_tip_protrusion_mm", "nozzle_diameter_mm", "nozzle_length_mm")
        })


    @property
    def contact_tip_end_z_mm(self) -> float:
        return self.ctwd_mm

    @property
    def nozzle_face_z_mm(self) -> float | None:
        if self.contact_tip_protrusion_mm is None:
            return None
        return self.contact_tip_end_z_mm + self.contact_tip_protrusion_mm

    @property
    def nozzle_body_end_z_mm(self) -> float | None:
        face = self.nozzle_face_z_mm
        if face is None or self.nozzle_length_mm is None:
            return None
        return face + self.nozzle_length_mm

    def bead_clearance(self, rho, dz, env, bead_r, bead_bottom, bead_top):


        face, end = self.nozzle_face_z_mm, self.nozzle_body_end_z_mm
        sections = [(self.contact_tip_end_z_mm, np.inf, None)]
        if face is not None and end is not None and self.nozzle_diameter_mm is not None:
            sections = [
                (self.contact_tip_end_z_mm, face, None),
                (face, end, self.nozzle_diameter_mm / 2),
                (end, np.inf, None),
            ]
        best = np.full(rho.shape, np.inf)
        for lower, upper, radius in sections:
            if lower == upper:
                continue
            if radius is None:
                d = np.full(rho.shape, np.inf)
                for axial, tube_r, lat in env:
                    ring = np.hypot(rho - lat, dz - axial) - tube_r - bead_r
                    ring = np.maximum(ring, axial - tube_r - bead_top)
                    np.minimum(d, ring, out=d)
            else:
                d = rho - radius - bead_r
            d = np.maximum(d, lower - bead_top)
            d = np.maximum(d, bead_bottom - upper)
            np.minimum(best, d, out=best)
        return best


@dataclass(frozen=True)
class TorchModel:


    standoff_mm: float = 6.0
    nozzle_diameter_mm: float = 16.0
    nozzle_length_mm: float = 25.0
    body_diameter_mm: float = 35.0
    body_length_mm: float = 80.0
    sphere_step_mm: float = 4.0
    samples: tuple[tuple[float, float], ...] | None = None


    rings: tuple[tuple[float, float, float], ...] | None = None


    stackup: TorchStackup | None = None

    @classmethod
    def yaskawa_accufast_45(
        cls, sphere_step_mm: float = 6.0, *, stackup: TorchStackup | None = None,
    ) -> "TorchModel":


        u = np.array([1.0, 0.0, -1.0]) / np.sqrt(2.0)
        rings: list[tuple[float, float, float]] = []
        for p0, p1, r in YASKAWA_45_CAPSULES:
            a, b = np.asarray(p0) - YASKAWA_45_TCP, np.asarray(p1) - YASKAWA_45_TCP
            length = float(np.linalg.norm(b - a))
            n = max(2, int(length / sphere_step_mm) + 1)
            for t in np.linspace(0.0, 1.0, n):
                p = a + t * (b - a)
                axial = float(p @ u)
                lateral = float(np.linalg.norm(p - axial * u))
                if axial > 0:
                    rings.append((axial, r, lateral))
        rings.sort()


        pts = tuple((axial, r + lateral) for axial, r, lateral in rings)
        return cls(sphere_step_mm=sphere_step_mm, samples=pts, rings=tuple(rings),
                   stackup=stackup if stackup is not None else TorchStackup.from_profile())

    def envelope(self) -> np.ndarray:


        if self.rings is not None:
            return np.array(self.rings, float)
        s = self.spheres()
        return np.column_stack([s[:, 0], s[:, 1], np.zeros(len(s))])

    def spheres(self) -> np.ndarray:


        if self.samples is not None:
            return np.array(self.samples, float)
        out: list[tuple[float, float]] = []
        r0, r1 = self.nozzle_diameter_mm / 2, self.body_diameter_mm / 2
        z = self.standoff_mm
        top_nozzle = z + self.nozzle_length_mm
        while z < top_nozzle:
            t = (z - self.standoff_mm) / self.nozzle_length_mm
            out.append((z, r0 + t * (r1 - r0)))
            z += self.sphere_step_mm
        top_body = top_nozzle + self.body_length_mm
        while z <= top_body:
            out.append((z, r1))
            z += self.sphere_step_mm
        return np.array(out)


@dataclass(frozen=True)
class Violation:
    move_index: int
    gcode_line: int
    point: tuple[float, float, float]
    obstacle: str
    clearance_mm: float

    def __post_init__(self) -> None:

        if abs(self.clearance_mm) < _CLEARANCE_EPS_MM:
            object.__setattr__(self, "clearance_mm", 0.0)


def _check_box(name: str, lo: np.ndarray, hi: np.ndarray) -> None:


    if lo.shape != (3,) or hi.shape != (3,):
        raise ValueError(f"box {name!r}: min and max are 3 numbers each")
    if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        raise ValueError(f"box {name!r}: min/max carry a non-finite value "
                         f"({lo.tolist()}, {hi.tolist()})")
    if np.any(hi <= lo):
        raise ValueError(
            f"box {name!r} has min {lo.tolist()} >= max {hi.tolist()} on at "
            "least one axis, so it encloses nothing and no torch position can "
            "ever violate it -- it would be counted as an obstacle and checked "
            "against nothing. Swap the corners, or give it a real size"
        )


def _check_capsule(name: str, p0: np.ndarray, p1: np.ndarray, radius: float) -> None:

    if p0.shape != (3,) or p1.shape != (3,):
        raise ValueError(f"capsule {name!r}: p0 and p1 are 3 numbers each")
    if not (np.all(np.isfinite(p0)) and np.all(np.isfinite(p1))
            and np.isfinite(radius)):
        raise ValueError(f"capsule {name!r}: p0/p1/radius carry a non-finite value")
    if radius < 0:
        raise ValueError(f"capsule {name!r}: radius {radius:g} is negative")
    if radius == 0 and float(np.linalg.norm(p1 - p0)) == 0.0:
        raise ValueError(
            f"capsule {name!r} is a single point of zero radius, so nothing can "
            "collide with it. Give it a length, a radius, or leave it out"
        )


@dataclass
class Scene:


    boxes: list[tuple[str, np.ndarray, np.ndarray]] = field(default_factory=list)
    capsules: list[tuple[str, np.ndarray, np.ndarray, float]] = field(default_factory=list)
    part_fixed: tuple[str, ...] | None = None
    substrates: list = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Scene":
        data = json.loads(Path(path).read_text(encoding="utf-8"))


        if "boxes" not in data and "capsules" not in data:
            raise ValueError(
                f"{Path(path).name} declares no recognised geometry: found "
                f"{sorted(data)}, expected 'boxes' and/or 'capsules'. An "
                "unreadable scene is not an empty cell -- fix the key rather "
                "than emitting against nothing"
            )
        scene = cls()
        for i, b in enumerate(data.get("boxes", [])):
            name = b.get("name", f"box{i}")
            lo, hi = np.asarray(b["min"], float), np.asarray(b["max"], float)
            _check_box(name, lo, hi)
            scene.boxes.append((name, lo, hi))
        for i, c in enumerate(data.get("capsules", [])):
            name = c.get("name", f"capsule{i}")
            p0, p1 = np.asarray(c["p0"], float), np.asarray(c["p1"], float)
            radius = float(c["radius"])
            _check_capsule(name, p0, p1, radius)
            scene.capsules.append((name, p0, p1, radius))
        declared = data.get("part_fixed")
        if declared is not None:
            scene.part_fixed = tuple(str(n) for n in declared)
        return scene


LC_PART_FIXED = ("charles_platter", "manpro_training_block", "platter_gusset",
                 "faceplate_168941-1")
LC_ROBOT_FIXED = ("mh185_headstock", "positioner_stand", "robot_pedestal",
                  "robot_pedestal_top_plate", "robot_pedestal_foot", "floor", "reamer_yair_185091",
                  "reamer_base", "reamer_foot", "reamer_post", "reamer_mount",
                  "cell_base_rear_rail", "cell_base_front_rail", "cell_base_left_rail",
                  "cell_base_right_rail", "cell_base_station_crossmember",
                  *(f"cell_base_anchor_{i}" for i in range(8)))


def _part_fixed_names(scene: Scene) -> frozenset[str]:
    if scene.part_fixed is not None:
        return frozenset(scene.part_fixed)
    names = {n for n, *_ in scene.boxes} | {n for n, *_ in scene.capsules} | {s.name for s in scene.substrates}
    unknown = sorted(names - set(LC_PART_FIXED) - set(LC_ROBOT_FIXED))
    if unknown:
        raise ValueError(
            "a station angle was given, but these obstacles never said which "
            f"frame they are in: {unknown}. Set Scene.part_fixed to the names "
            "bolted to the platter (they turn with the station); everything "
            "else is taken as fixed to the robot's world."
        )
    return frozenset(LC_PART_FIXED)


def _station_frame(station_transform):


    if station_transform is None:
        return None
    T = np.asarray(station_transform, float)
    if T.shape != (4, 4):
        raise ValueError(
            "station_transform is a 4x4 part-frame matrix "
            "(station.part_frame_transform builds it)"
        )
    R = T[:3, :3]
    return R, T[:3, 3], R.T @ np.array([0.0, 0.0, 1.0])


def _point_segment_dist(points: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:

    ab = b - a
    length_sq = np.maximum((ab * ab).sum(axis=1), 1e-12)
    ap = points[:, None, :] - a[None, :, :]
    t = np.clip((ap * ab[None, :, :]).sum(axis=2) / length_sq[None, :], 0.0, 1.0)
    closest = a[None, :, :] + t[:, :, None] * ab[None, :, :]
    return np.linalg.norm(points[:, None, :] - closest, axis=2)


def _point_box_dist(points: np.ndarray, bmin: np.ndarray, bmax: np.ndarray) -> np.ndarray:


    q = np.maximum(bmin - points, points - bmax)
    outside = np.linalg.norm(np.maximum(q, 0.0), axis=1)
    inside = np.minimum(q.max(axis=1), 0.0)
    return outside + inside


def _sample_params(p0: np.ndarray, p1: np.ndarray, step: float) -> np.ndarray:
    n = max(2, int(np.ceil(np.linalg.norm(p1 - p0) / step)) + 1)
    return np.linspace(0.0, 1.0, n)


def _sample_segment(p0: np.ndarray, p1: np.ndarray, step: float) -> np.ndarray:
    t = _sample_params(p0, p1, step)
    return p0[None, :] + t[:, None] * (p1 - p0)[None, :]


def bead_slab(prev, target, bead_height_mm: float):


    lift = np.array([0.0, 0.0, bead_height_mm / 2])
    a, b = np.asarray(prev, float) + lift, np.asarray(target, float) + lift
    lay = (float(min(prev[2], target[2])), float(max(prev[2], target[2])))
    return a, b, lay[1] + bead_height_mm, lay


def weld_zone(near, sample_z, start_z, top, lay_lo, lay_hi):


    eps = _CLEARANCE_EPS_MM
    sample_z = np.asarray(sample_z, float)[:, None]
    lay_lo, lay_hi = np.asarray(lay_lo, float)[None, :], np.asarray(lay_hi, float)[None, :]
    under = np.asarray(top, float)[None, :] <= sample_z + eps
    same_pass = (start_z >= lay_lo - eps) & (start_z <= lay_hi + eps) & (sample_z >= lay_lo - eps)
    return near & (under | same_pass)


def _work_surface_dist(points, bmin, bmax):


    d = _point_box_dist(points, bmin, bmax)
    inside_xy = np.all((points[:, :2] >= bmin[:2]) &
                       (points[:, :2] <= bmax[:2]), axis=1)
    d[inside_xy] = points[inside_xy, 2] - bmax[2]
    return d


class _Rows:


    def __init__(self, width: int | None, dtype=float):
        self._buf = np.empty((64,) if width is None else (64, width), dtype)
        self._n = 0

    def extend(self, rows) -> None:
        rows = np.asarray(rows, self._buf.dtype)
        n = self._n + len(rows)
        if n > len(self._buf):
            grown = np.empty((max(2 * len(self._buf), n),) + self._buf.shape[1:], self._buf.dtype)
            grown[:self._n] = self._buf[:self._n]
            self._buf = grown
        self._buf[self._n:n] = rows
        self._n = n

    def __len__(self) -> int:
        return self._n

    @property
    def view(self) -> np.ndarray:
        return self._buf[:self._n]


def _box_dist_many(centers: np.ndarray, bmin: np.ndarray, bmax: np.ndarray) -> np.ndarray:


    q = np.maximum(bmin[None, :, None, :] - centers, centers - bmax[None, :, None, :])
    o = np.maximum(q, 0.0)
    outside = np.sqrt(np.add.reduce(o * o, axis=-1))
    inside = np.minimum(q.max(axis=-1), 0.0)
    return outside + inside


_BOX_BLOCK = 200_000


def check(
    toolpath: Toolpath,
    torch: TorchModel | None = None,
    scene: Scene | None = None,
    bead_width_mm: float = 9.0,
    bead_height_mm: float = 3.0,
    margin_mm: float = 3.0,
    exclusion_mm: float = 12.0,
    sample_mm: float = 4.0,
    contact_ok: tuple[str, ...] = (),
    station_transform: np.ndarray | None = None,
    entry_wire_drop_mm: float | None = None,
    entry_wire_through_move: int | None = None,
) -> list[Violation]:


    torch = torch or TorchModel()
    scene = scene or Scene()
    spheres = torch.spheres()
    violations: list[Violation] = []
    st = _station_frame(station_transform)
    part_fixed = _part_fixed_names(scene) if st is not None else frozenset()


    bead_pts = _Rows(3)
    bead_owner = _Rows(None, int)
    bead_top = _Rows(None)
    bead_a = _Rows(3)
    bead_b = _Rows(3)
    flat_bead_a = _Rows(3)
    flat_bead_b = _Rows(3)
    flat_bead_top = _Rows(None)
    flat_bead_lay = _Rows(2)

    solid = [(name, bmin, bmax) for name, bmin, bmax in scene.boxes if name not in contact_ok]
    solid_names = [name for name, _, _ in solid]
    solid_min = np.array([bmin for _, bmin, _ in solid], float).reshape(-1, 3)
    solid_max = np.array([bmax for _, _, bmax in solid], float).reshape(-1, 3)
    solid_part_fixed = np.array([name in part_fixed for name in solid_names], bool)
    bead_line: list[int] = []
    bead_r = bead_width_mm / 2


    env = torch.envelope()


    reach_xy = float(np.max(env[:, 1] + env[:, 2])) + bead_r + margin_mm
    reach_lo = float(np.min(env[:, 0] - env[:, 1])) - bead_r - margin_mm
    reach_hi = float(np.max(env[:, 0] + env[:, 1])) + bead_r + margin_mm
    if torch.stackup is not None:
        stack = torch.stackup
        end = stack.nozzle_body_end_z_mm
        if end is not None and stack.nozzle_diameter_mm is not None:
            reach_xy = max(reach_xy, stack.nozzle_diameter_mm / 2 + bead_r + margin_mm)
            reach_lo = min(reach_lo, stack.contact_tip_end_z_mm - bead_r - margin_mm)
            reach_hi = max(reach_hi, end + bead_r + margin_mm)

    prev = None
    flat_prev = None
    metal_prev = None
    wire_prev = None
    flat_wire_prev = None
    for i, move in enumerate(toolpath.moves):
        target = np.asarray(move.target, float)


        lift = getattr(move, "ctwd_lift", 0.0)
        metal_target = target - np.array([0.0, 0.0, lift]) if lift else target
        flat_target = metal_target - np.array([0.0, 0.0, getattr(move, "seat", 0.0)])
        if entry_wire_through_move is not None and i <= entry_wire_through_move:
            wire_drop = 0.0 if entry_wire_drop_mm is None else entry_wire_drop_mm
        else:
            wire_drop = lift
        wire_target = target - np.array([0.0, 0.0, wire_drop]) if wire_drop else target
        flat_wire_target = wire_target - np.array(
            [0.0, 0.0, getattr(move, "seat", 0.0)])
        if prev is None:
            violations.extend(replace(v, move_index=i, gcode_line=move.line_no)
                              for v in check_point(target, torch, scene, margin_mm,
                                                   contact_ok, station_transform,
                                                   wire_drop_mm=wire_drop))
        if prev is not None:
            t = _sample_params(prev, target, sample_mm)
            samples = prev[None, :] + t[:, None] * (target - prev)[None, :]
            flat_samples = flat_prev[None, :] + t[:, None] * (flat_target - flat_prev)[None, :]


            if entry_wire_through_move is not None and i > entry_wire_through_move:
                segment_wire_prev = metal_prev
                segment_flat_wire_prev = flat_prev
            else:
                segment_wire_prev = wire_prev
                segment_flat_wire_prev = flat_wire_prev
            flat_wire_samples = (segment_flat_wire_prev[None, :] + t[:, None]
                                 * (flat_wire_target - segment_flat_wire_prev)[None, :])
            samples_w = samples if st is None else samples @ st[0].T + st[1]
            wire_samples = (segment_wire_prev[None, :] + t[:, None]
                            * (wire_target - segment_wire_prev)[None, :])
            wire_w = wire_samples if st is None else wire_samples @ st[0].T + st[1]
            if len(bead_pts):
                pts = bead_pts.view
                owner = bead_owner.view


                pts_w = pts if st is None else pts @ st[0].T + st[1]


                if st is None:
                    ceiling = bead_top.view
                    floor = np.minimum(bead_a.view[:, 2],
                                       bead_b.view[:, 2]) - bead_height_mm / 2
                else:
                    aa_w = bead_a.view @ st[0].T + st[1]
                    bb_w = bead_b.view @ st[0].T + st[1]
                    extent = bead_r * np.linalg.norm(st[0][2, :2]) + bead_height_mm / 2 * abs(st[0][2, 2])
                    ceiling = np.maximum(aa_w[:, 2], bb_w[:, 2]) + extent
                    floor = np.minimum(aa_w[:, 2], bb_w[:, 2]) - extent
                dx = pts_w[None, :, 0] - samples_w[:, None, 0]
                dy = pts_w[None, :, 1] - samples_w[:, None, 1]
                rho = np.hypot(dx, dy)
                dz = pts_w[None, :, 2] - samples_w[:, None, 2]


                seg = _point_segment_dist(
                    flat_samples, flat_bead_a.view, flat_bead_b.view)
                near = seg < exclusion_mm
                tip_height = flat_wire_samples[:, None, 2] - flat_bead_top.view[None, :]
                lay = flat_bead_lay.view


                vertical = np.linalg.norm(target[:2] - prev[:2]) < 1e-6
                in_zone = weld_zone(near, flat_samples[:, 2], float(flat_prev[2]),
                                    flat_bead_top.view, lay[:, 0], lay[:, 1])
                in_zone &= bool(move.deposit or vertical)
                excluded = in_zone[:, owner]


                footprint = _point_segment_dist(
                    samples[:, :2], bead_a.view[:, :2], bead_b.view[:, :2])
                tip_d = np.maximum(footprint - bead_r, tip_height)
                tip_d = np.where(in_zone, np.inf, tip_d)
                m, b = np.unravel_index(int(np.argmin(tip_d)), tip_d.shape)

                bead_hit = (float(tip_d[m, b]), int(m), int(b))

                cand = np.nonzero(
                    ~excluded
                    & (rho <= reach_xy)
                    & (dz >= reach_lo)
                    & (dz <= reach_hi)
                )
                if cand[0].size:
                    rho_c, dz_c = rho[cand], dz[cand]


                    if torch.stackup is not None:
                        tcp_z = samples_w[cand[0], 2]
                        dbest = torch.stackup.bead_clearance(
                            rho_c, dz_c, env, bead_r,
                            floor[owner[cand[1]]] - tcp_z,
                            ceiling[owner[cand[1]]] - tcp_z,
                        )
                    else:
                        dbest = np.full(rho_c.shape, np.inf)
                        for axial, tube_r, lat in env:
                            d = np.hypot(rho_c - lat, dz_c - axial) - tube_r - bead_r
                            vertical_gap = samples_w[cand[0], 2] + axial - tube_r - ceiling[owner[cand[1]]]
                            d = np.maximum(d, vertical_gap)
                            np.minimum(dbest, d, out=dbest)
                    k = int(np.argmin(dbest))
                    if dbest[k] < bead_hit[0]:
                        m, p = int(cand[0][k]), int(cand[1][k])
                        bead_hit = (float(dbest[k]), m, int(owner[p]))
                clearance, m, b = bead_hit
                if clearance < margin_mm:
                    violations.append(
                        Violation(
                            move_index=i,
                            gcode_line=move.line_no,
                            point=tuple(float(c) for c in np.round(samples[m], 2)),


                            obstacle=f"bead(line {bead_line[b]})",
                            clearance_mm=clearance,
                        )
                    )


            worst_by_obstacle: dict[str, tuple[np.ndarray, float]] = {}

            def note(name: str, pt: np.ndarray, d: float, hits=worst_by_obstacle) -> None:
                prev_hit = hits.get(name)
                if prev_hit is None or d < prev_hit[1]:
                    hits[name] = (pt, d)


            for name, bmin, bmax in scene.boxes:
                if name not in contact_ok:
                    continue
                tip = wire_samples if st is None or name in part_fixed else wire_w
                d = _work_surface_dist(tip, bmin, bmax)
                m = int(np.argmin(d))
                if d[m] < margin_mm:
                    note(name, samples[m], float(d[m]))

            for substrate in scene.substrates:
                if substrate.name in contact_ok:
                    tip = wire_samples if st is None or substrate.name in part_fixed else wire_w
                    d = substrate.distance(tip, contact=True)
                    m = int(np.argmin(d))
                    if d[m] < margin_mm:
                        note(substrate.name, samples[m], float(d[m]))


            if solid_names:
                count = len(samples)
                lift = np.zeros((len(spheres), 3))
                lift[:, 2] = spheres[:, 0]
                if st is None:
                    centers_all = np.broadcast_to(
                        (samples[None, :, :] + lift[:, None, :])[:, None],
                        (len(spheres), len(solid_names), count, 3))
                else:
                    leaning = samples[None, :, :] + (spheres[:, 0, None] * st[2][None, :])[:, None, :]
                    upright = samples_w[None, :, :] + lift[:, None, :]
                    centers_all = np.where(solid_part_fixed[None, :, None, None],
                                           leaning[:, None], upright[:, None])
                box_at = np.empty((len(spheres), len(solid_names)), int)
                box_d = np.empty((len(spheres), len(solid_names)))
                step = max(1, _BOX_BLOCK // (len(solid_names) * count))
                for k0 in range(0, len(spheres), step):
                    d = (_box_dist_many(centers_all[k0:k0 + step], solid_min, solid_max)
                         - spheres[k0:k0 + step, 1, None, None])
                    at = d.argmin(axis=2)
                    box_at[k0:k0 + step] = at
                    box_d[k0:k0 + step] = np.take_along_axis(d, at[:, :, None], axis=2)[:, :, 0]
                box_hit = box_d < margin_mm
            for k, (zoff, radius) in enumerate(spheres):
                if st is None:
                    centers = centers_w = samples + np.array([0.0, 0.0, zoff])
                else:
                    centers = samples + zoff * st[2]
                    centers_w = samples_w + np.array([0.0, 0.0, zoff])
                if solid_names:
                    for j in np.flatnonzero(box_hit[k]):
                        note(solid_names[j], samples[int(box_at[k, j])], float(box_d[k, j]))
                for name, p0, p1, r in scene.capsules:
                    c = centers if name in part_fixed else centers_w
                    d = _point_segment_dist(c, p0[None], p1[None])[:, 0] - r - radius
                    m = int(np.argmin(d))
                    if d[m] < margin_mm:
                        note(name, samples[m], float(d[m]))
                for substrate in scene.substrates:
                    if substrate.name not in contact_ok:
                        c = centers if substrate.name in part_fixed else centers_w
                        d = substrate.distance(c) - radius
                        m = int(np.argmin(d))
                        if d[m] < margin_mm:
                            note(substrate.name, samples[m], float(d[m]))

            for name, (pt, d) in sorted(worst_by_obstacle.items(), key=lambda kv: kv[1][1]):
                violations.append(
                    Violation(
                        move_index=i,
                        gcode_line=move.line_no,
                        point=tuple(float(c) for c in np.round(pt, 2)),
                        obstacle=name,
                        clearance_mm=float(d),
                    )
                )
        if move.deposit and prev is not None:
            a0, b0, top, _ = bead_slab(metal_prev, metal_target, bead_height_mm)
            flat_a, flat_b, flat_top, flat_lay = bead_slab(flat_prev, flat_target, bead_height_mm)
            axis_pts = _sample_segment(a0, b0, sample_mm)
            bead_a.extend(a0[None])
            bead_b.extend(b0[None])
            bead_pts.extend(axis_pts)
            bead_owner.extend(np.full(len(axis_pts), len(bead_line), dtype=int))
            bead_top.extend([top])
            flat_bead_a.extend(flat_a[None])
            flat_bead_b.extend(flat_b[None])
            flat_bead_top.extend([flat_top])
            flat_bead_lay.extend([flat_lay])
            bead_line.append(move.line_no)
        prev = target
        metal_prev = metal_target
        flat_prev = flat_target
        wire_prev = wire_target
        flat_wire_prev = flat_wire_target
    return violations


class ClearanceError(RuntimeError):
    pass


class WireHangError(ClearanceError):
    pass


CONTACT_OK = ("charles_platter", "bead_test_coupon")


def blocking(
    violations: list[Violation],
    *,
    contact_ok: tuple[str, ...] = CONTACT_OK,
    max_penetration_mm: float = 0.0,
) -> list[Violation]:


    out: list[Violation] = []
    for v in violations:
        forgiving = v.obstacle in contact_ok or v.obstacle.startswith("bead(")
        if not forgiving or v.clearance_mm < -max_penetration_mm:
            out.append(v)
    return out


def check_point(
    point: tuple[float, float, float] | np.ndarray,
    torch: TorchModel,
    scene: Scene,
    margin_mm: float = 3.0,
    contact_ok: tuple[str, ...] = (),
    station_transform: np.ndarray | None = None,
    *,
    wire_drop_mm: float = 0.0,
) -> list[Violation]:


    p = np.asarray(point, float).reshape(1, 3)
    st = _station_frame(station_transform)
    part_fixed = _part_fixed_names(scene) if st is not None else frozenset()
    p_w = p if st is None else p @ st[0].T + st[1]
    wire, wire_w = p, p_w
    if wire_drop_mm:
        wire = p - np.array([0.0, 0.0, wire_drop_mm])
        wire_w = wire if st is None else wire @ st[0].T + st[1]
    out: list[Violation] = []
    for name, bmin, bmax in scene.boxes:
        if name not in contact_ok:
            continue
        tip = wire if st is None or name in part_fixed else wire_w
        d = float(_work_surface_dist(tip, bmin, bmax)[0])
        if d < margin_mm:
            out.append(Violation(-1, -1, tuple(float(c) for c in p[0]), name, d))
    for substrate in scene.substrates:
        if substrate.name in contact_ok:
            tip = wire if st is None or substrate.name in part_fixed else wire_w
            d = float(substrate.distance(tip, contact=True)[0])
            if d < margin_mm:
                out.append(Violation(-1, -1, tuple(float(c) for c in p[0]), substrate.name, d))
    for zoff, radius in torch.spheres():
        if st is None:
            centers = centers_w = p + np.array([0.0, 0.0, zoff])
        else:
            centers = p + zoff * st[2]
            centers_w = p_w + np.array([0.0, 0.0, zoff])
        for name, bmin, bmax in scene.boxes:
            if name in contact_ok:
                continue
            c = centers if name in part_fixed else centers_w
            d = float(_point_box_dist(c, bmin, bmax)[0]) - radius
            if d < margin_mm:
                out.append(Violation(-1, -1, tuple(float(c) for c in p[0]), name, d))
        for name, p0, p1, r in scene.capsules:
            c = centers if name in part_fixed else centers_w
            d = float(_point_segment_dist(c, p0[None], p1[None])[0, 0]) - r - radius
            if d < margin_mm:
                out.append(Violation(-1, -1, tuple(float(c) for c in p[0]), name, d))
        for substrate in scene.substrates:
            if substrate.name not in contact_ok:
                c = centers if substrate.name in part_fixed else centers_w
                d = float(substrate.distance(c)[0]) - radius
                if d < margin_mm:
                    out.append(Violation(-1, -1, tuple(float(c) for c in p[0]), substrate.name, d))
    return out


def plot(
    toolpath: Toolpath,
    violations: list[Violation],
    path: str | Path,
) -> Path:


    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import cm

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(projection="3d")
    n_layers = max(1, len(toolpath.layers()))
    prev = None
    for move in toolpath.moves:
        if prev is not None:
            xs, ys, zs = zip(prev, move.target)
            if move.deposit:
                color = cm.viridis(move.layer / n_layers)
                ax.plot(xs, ys, zs, color=color, linewidth=2)
            else:
                ax.plot(xs, ys, zs, color="0.6", linewidth=0.8, linestyle="--")
        prev = move.target
    if violations:
        vx, vy, vz = zip(*(v.point for v in violations))
        ax.scatter(vx, vy, vz, color="red", marker="x", s=80, linewidths=2, label="violation")
        ax.legend()
    (x0, x1), (y0, y1), (z0, z1) = toolpath.bbox()
    ax.set_box_aspect((max(x1 - x0, 1), max(y1 - y0, 1), max(z1 - z0, 1)))
    ax.set_xlabel("X mm")
    ax.set_ylabel("Y mm")
    ax.set_zlabel("Z mm")
    title = "clean" if not violations else f"{len(violations)} clearance violation(s)"
    ax.set_title(f"waam-pipeline clearance check: {title}")
    out = Path(path)
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out


def point_guard(torch: TorchModel | None, scene: Scene | None,
                margin_mm: float = DEFAULT_MARGIN_MM,
                station_transform: np.ndarray | None = None,
                *, contact_ok: tuple[str, ...] = CONTACT_OK):


    if scene is None or torch is None:
        return None

    def guard(xyz, where: str, wire_drop_mm: float = 0.0) -> None:
        def hits_at(drop):
            return blocking(check_point(xyz, torch, scene, margin_mm,
                                        contact_ok=contact_ok,
                                        station_transform=station_transform,
                                        wire_drop_mm=drop),
                            contact_ok=contact_ok)

        hits = hits_at(0.0)
        if not hits and wire_drop_mm:
            hits = hits_at(wire_drop_mm)
            if hits:
                w = min(hits, key=lambda v: v.clearance_mm)
                raise WireHangError(
                    f"{where}: a wire hanging {wire_drop_mm:.2f} mm longer than the default "
                    f"comes {w.clearance_mm:.1f} mm to {w.obstacle}. Cut the wire (run "
                    "CLEAN_R1) before this job, or add travel lift")
        if hits:
            w = min(hits, key=lambda v: v.clearance_mm)
            raise ClearanceError(
                f"{where}: {w.clearance_mm:.1f} mm to {w.obstacle} at "
                f"{tuple(round(c, 1) for c in w.point)}"
            )

    return guard
