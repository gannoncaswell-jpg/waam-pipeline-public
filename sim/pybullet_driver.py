from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waam_pipeline import cell_hc, cell_lc, station


from waam_pipeline.clearance import (
    CONTACT_OK,
    Scene,
    Violation,
    _part_fixed_names,
    _point_segment_dist,
    _sample_params,
    _sample_segment,
    bead_slab,
    blocking,
    weld_zone,
)
from waam_pipeline.robots import ar1440, hc10

_M = 1e-3

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URDF = REPO_ROOT / "robots" / "ar1440.urdf"
HC_DEFAULT_URDF = REPO_ROOT / "robots" / "hc10dtp.urdf"

WSL2_HINT = (
    "PyBullet ships no Windows wheel (3.2.7 = 10 manylinux files + an MSVC-only "
    "sdist). Run this under WSL2: docs/wsl2-setup.md, then "
    "`bash tools/wsl2-setup.sh`."
)

def _arm_link_names(arm_joint_names) -> tuple[str, ...]:


    return ("base_link",) + tuple(
        f"link_{i + 1}_{name[-1]}" for i, name in enumerate(arm_joint_names))


ARM_LINK_NAMES: tuple[str, ...] = _arm_link_names(ar1440.ARM_JOINT_NAMES)


HC_ARM_LINK_NAMES: tuple[str, ...] = _arm_link_names(hc10.ARM_JOINT_NAMES)


TORCH_SHAPE_NAMES: tuple[str, ...] = ("torch/body", "torch/neck", "torch/gooseneck")


HC_TORCH_SHAPE_NAMES: tuple[str, ...] = (
    "torch/hc_tool0_mount", "torch/hc_tool0_feeder",
    "torch/hc_tool0_neck", "torch/hc_tool0_tip",
)


def _is_torch_link(link: str) -> bool:


    return link.startswith("torch/")


DEFAULT_IGNORE_PAIRS: tuple[tuple[str, str], ...] = (
    ("base_link", "robot_pedestal"),
    ("base_link", "floor"),
    ("link_1_s", "robot_pedestal"),
    ("base_link", "robot_pedestal_top_plate"),
    ("link_1_s", "robot_pedestal_top_plate"),
)


def _import_pybullet():
    import pybullet

    return pybullet


try:
    _pb = _import_pybullet()
    HAVE_PYBULLET = True
    PYBULLET_IMPORT_ERROR: str | None = None
except Exception as exc:
    _pb = None
    HAVE_PYBULLET = False
    PYBULLET_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def require_pybullet():

    if not HAVE_PYBULLET:
        raise ImportError(
            f"sim.pybullet_driver needs pybullet and cannot import it "
            f"({PYBULLET_IMPORT_ERROR}). {WSL2_HINT}"
        )
    return _pb


def quat_from_matrix(R) -> tuple[float, float, float, float]:


    m = np.asarray(R, float)
    if m.shape != (3, 3):
        raise ValueError("quat_from_matrix wants a 3x3 rotation")
    t = float(np.trace(m))
    if t > 0.0:
        s = math.sqrt(t + 1.0) * 2.0
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] >= m[1, 1] and m[0, 0] >= m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] >= m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    n = math.sqrt(x * x + y * y + z * z + w * w)
    return (x / n, y / n, z / n, w / n)


def matrix_from_quat(q) -> np.ndarray:


    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        raise ValueError("zero quaternion")
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def _axis_to_z(direction) -> tuple[float, float, float, float]:


    d = np.asarray(direction, float)
    n = float(np.linalg.norm(d))
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    d = d / n
    z = np.array([0.0, 0.0, 1.0])
    c = float(np.dot(z, d))
    if c > 1.0 - 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    if c < -1.0 + 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    v = np.cross(z, d)
    s = math.sqrt((1.0 + c) * 2.0)
    q = np.array([v[0] / s, v[1] / s, v[2] / s, s / 2.0])
    return tuple(float(x) for x in q / np.linalg.norm(q))


def capsule_frame(p0_mm, p1_mm) -> tuple[tuple[float, float, float],
                                         tuple[float, float, float, float],
                                         float]:


    a = np.asarray(p0_mm, float)
    b = np.asarray(p1_mm, float)
    d = b - a
    length = float(np.linalg.norm(d))
    centre = tuple(float(v) * _M for v in (a + b) / 2.0)
    return centre, _axis_to_z(d), length * _M


@dataclass(frozen=True)
class BodySpec:


    name: str
    kind: str
    position: tuple[float, float, float]
    orientation: tuple[float, float, float, float]
    half_extents: tuple[float, float, float] | None = None
    radius: float | None = None
    length: float | None = None
    part_fixed: bool = False
    vertices: tuple | None = None
    indices: tuple[int, ...] | None = None


def _station_placement(station_deg: float, sense: int):


    if abs(float(station_deg)) < 1e-12:
        return None
    T = station.station_transform(float(station_deg), sense)
    return T[:3, :3], T[:3, 3]


def _to_world_mm(points_part_mm, origin_robot_mm, place) -> np.ndarray:

    p = np.atleast_2d(np.asarray(points_part_mm, float)) + np.asarray(origin_robot_mm, float)
    if place is not None:
        p = p @ place[0].T + place[1]
    return p


def scene_body_specs(
    scene: Scene,
    part_origin_robot,
    *,
    station_deg: float = 0.0,
    sense: int = station.STATION_SENSE,
) -> list[BodySpec]:


    o = np.asarray(part_origin_robot, float)
    place = _station_placement(station_deg, sense)
    part_fixed = _part_fixed_names(scene) if place is not None else frozenset()
    rot = np.eye(3) if place is None else place[0]

    out: list[BodySpec] = []
    for name, bmin, bmax in scene.boxes:
        fixed = name in part_fixed
        centre = (np.asarray(bmin, float) + np.asarray(bmax, float)) / 2.0
        half = (np.asarray(bmax, float) - np.asarray(bmin, float)) / 2.0
        w = _to_world_mm(centre, o, place if fixed else None)[0]
        out.append(BodySpec(
            name=name,
            kind="box",
            position=tuple(float(v) * _M for v in w),
            orientation=quat_from_matrix(rot if fixed else np.eye(3)),
            half_extents=tuple(float(v) * _M for v in half),
            part_fixed=fixed,
        ))
    for name, p0, p1, radius in scene.capsules:
        fixed = name in part_fixed
        w = _to_world_mm(np.vstack([p0, p1]), o, place if fixed else None)
        centre, quat, length = capsule_frame(w[0], w[1])
        out.append(BodySpec(
            name=name,
            kind="capsule",
            position=centre,
            orientation=quat,
            radius=float(radius) * _M,
            length=length,
            part_fixed=fixed,
        ))
    for substrate in scene.substrates:
        fixed = substrate.name in part_fixed
        world_origin = _to_world_mm(substrate.origin, o, place if fixed else None)[0]
        vertices, indices = np.unique(substrate.mesh().reshape(-1, 3) - substrate.origin,
                                      axis=0, return_inverse=True)
        vertices *= _M
        out.append(BodySpec(
            name=substrate.name, kind="convex",
            position=tuple(world_origin * _M),
            orientation=quat_from_matrix(rot if fixed else np.eye(3)),
            vertices=tuple(map(tuple, vertices)), indices=tuple(map(int, indices)), part_fixed=fixed,
        ))
    return out


@dataclass(frozen=True)
class BeadSpec:


    move_index: int
    line_no: int
    p0: tuple[float, float, float]
    p1: tuple[float, float, float]
    radius_mm: float
    top_z_mm: float
    lay_z_mm: tuple[float, float]
    flat_p0: tuple[float, float, float] | None = None
    flat_p1: tuple[float, float, float] | None = None
    flat_top_z_mm: float | None = None
    flat_lay_z_mm: tuple[float, float] | None = None

    @property
    def obstacle(self) -> str:

        return f"bead(line {self.line_no})"


def bead_specs(toolpath, *, bead_width_mm: float = 9.0,
               bead_height_mm: float = 3.0) -> list[BeadSpec]:


    out: list[BeadSpec] = []
    prev = None
    flat_prev = None
    for i, move in enumerate(toolpath.moves):

        target = np.asarray(move.target, float) - np.array([0.0, 0.0, getattr(move, "ctwd_lift", 0.0)])
        flat_target = target - np.array([0.0, 0.0, getattr(move, "seat", 0.0)])
        if move.deposit and prev is not None:
            a, b, top, lay = bead_slab(prev, target, float(bead_height_mm))
            fa, fb, ftop, flay = bead_slab(flat_prev, flat_target, float(bead_height_mm))
            flat = (tuple(float(v) for v in fa), tuple(float(v) for v in fb), ftop, flay)
            out.append(BeadSpec(
                move_index=i,
                line_no=move.line_no,
                p0=tuple(float(v) for v in a),
                p1=tuple(float(v) for v in b),
                radius_mm=float(bead_width_mm) / 2.0,
                top_z_mm=top,
                lay_z_mm=lay,
                flat_p0=flat[0],
                flat_p1=flat[1],
                flat_top_z_mm=flat[2],
                flat_lay_z_mm=flat[3],
            ))
        prev = target
        flat_prev = flat_target
    return out


def excluded_beads(sample_part_mm, beads: list[BeadSpec], exclusion_mm: float,
                   start_z_mm: float) -> np.ndarray:


    if not beads:
        return np.zeros(0, dtype=bool)
    s = np.asarray(sample_part_mm, float).reshape(1, 3)
    a = np.array([b.p0 if b.flat_p0 is None else b.flat_p0 for b in beads], float)
    b = np.array([b.p1 if b.flat_p1 is None else b.flat_p1 for b in beads], float)
    near = _point_segment_dist(s, a, b) < float(exclusion_mm)
    lay = np.array([bd.lay_z_mm if bd.flat_lay_z_mm is None else bd.flat_lay_z_mm
                    for bd in beads], float)
    top = np.array([bd.top_z_mm if bd.flat_top_z_mm is None else bd.flat_top_z_mm
                    for bd in beads], float)
    return weld_zone(near, s[:, 2], float(start_z_mm), top, lay[:, 0], lay[:, 1])[0]


def _yaskawa_to_flange(p, to_flange=None) -> np.ndarray:


    m = cell_lc.YASKAWA_FLANGE_TO_OURS if to_flange is None else to_flange

    return np.asarray(m, float) @ np.asarray(p, float) + 0.0


def torch_capsules_flange(capsules=None, to_flange=None) -> tuple[tuple[np.ndarray, np.ndarray, float], ...]:


    src = cell_lc.TOOL_INTERFERE_CAPSULES if capsules is None else capsules
    return tuple((_yaskawa_to_flange(a, to_flange), _yaskawa_to_flange(b, to_flange), float(r))
                 for a, b, r in src)


@dataclass(frozen=True)
class UrdfReport:


    path: Path
    root_link: str
    links: tuple[str, ...]
    joints: tuple[tuple[str, str, str, str], ...]
    mesh_files: tuple[str, ...]
    missing_meshes: tuple[str, ...]
    inertials: int

    @property
    def joint_types(self) -> dict[str, str]:
        return {n: t for n, t, _p, _c in self.joints}

    def problems(
        self,
        *,
        arm_link_names: tuple[str, ...] = ARM_LINK_NAMES,
        arm_joint_names: tuple[str, ...] | None = None,
        requires_station: bool = True,
    ) -> list[str]:


        if arm_joint_names is None:
            arm_joint_names = ar1440.ARM_JOINT_NAMES
        out: list[str] = []
        for name in arm_link_names:
            if name not in self.links:
                out.append(f"missing arm link {name}")
        if requires_station:
            for name in station.STATION_LINK_NAMES:
                if name not in self.links:
                    out.append(f"missing station link {name}")
        types = self.joint_types
        for name in arm_joint_names:
            if types.get(name) != "revolute":
                out.append(f"{name} is {types.get(name)!r}, expected revolute")
        if requires_station and station.STATION_JOINT_NAME not in types:
            out.append(f"missing station joint {station.STATION_JOINT_NAME}")
        if not self.mesh_files:
            out.append("no <mesh> elements: this is the primitive-geometry URDF, "
                       "not the one an interference check wants "
                       "(PYTHONPATH=src python -m waam_pipeline.robots.ar1440)")
        if self.missing_meshes:
            out.append(f"mesh files do not resolve on disk: {list(self.missing_meshes)}")
        return out


def inspect_urdf(path=None) -> UrdfReport:


    p = Path(DEFAULT_URDF if path is None else path)

    root = ET.parse(p).getroot()  # nosec B314
    links = tuple(el.get("name", "") for el in root.findall("link"))
    joints = tuple(
        (
            el.get("name", ""),
            el.get("type", ""),
            (el.find("parent").get("link", "") if el.find("parent") is not None else ""),
            (el.find("child").get("link", "") if el.find("child") is not None else ""),
        )
        for el in root.findall("joint")
    )
    children = {c for _n, _t, _p, c in joints}
    roots = [n for n in links if n not in children]
    meshes = tuple(m.get("filename", "") for m in root.iter("mesh"))
    missing = tuple(sorted({f for f in meshes if not (p.parent / f).is_file()}))
    return UrdfReport(
        path=p,
        root_link=roots[0] if roots else "",
        links=links,
        joints=joints,
        mesh_files=meshes,
        missing_meshes=missing,
        inertials=len(list(root.iter("inertial"))),
    )


def link_bound_radii(collision_dir, mesh_links=None) -> dict[str, float]:


    from waam_pipeline.slicer import load_stl

    root = Path(collision_dir)
    links = ar1440._MESH_LINKS if mesh_links is None else mesh_links
    out: dict[str, float] = {}
    for link, (stl_name, off) in links.items():
        path = root / stl_name
        if not path.is_file():
            out[link] = float("inf")
            continue
        v = load_stl(path).reshape(-1, 3) * 1000.0 + np.asarray(off, float) * 1000.0
        out[link] = float(np.linalg.norm(v, axis=1).max())
    return out


def map_joint_indices(joint_rows, wanted) -> dict[str, int]:


    found: dict[str, int] = {}
    for row in joint_rows:
        name = row[1]
        if isinstance(name, (bytes, bytearray)):
            name = name.decode("utf-8")
        found[str(name)] = int(row[0])
    missing = [n for n in wanted if n not in found]
    if missing:
        raise KeyError(
            f"URDF has no joint(s) {missing}; it has {sorted(found)}. "
            "Regenerate with `PYTHONPATH=src python -m waam_pipeline.robots.ar1440`."
        )
    return {n: found[n] for n in wanted}


def map_link_indices(joint_rows, root_link: str) -> dict[int, str]:


    out: dict[int, str] = {-1: root_link}
    for row in joint_rows:
        name = row[12]
        if isinstance(name, (bytes, bytearray)):
            name = name.decode("utf-8")
        out[int(row[0])] = str(name)
    return out


@dataclass(frozen=True)
class LinkHit:


    violation: Violation
    link: str
    station_deg: float = 0.0

    @property
    def is_torch(self) -> bool:
        return _is_torch_link(self.link)

    def describe(self) -> str:
        v = self.violation
        return (f"move {v.move_index} (L{v.gcode_line}): {self.link} "
                f"{v.clearance_mm:+.2f} mm to {v.obstacle} at "
                f"{tuple(round(c, 1) for c in v.point)}")


@dataclass
class SimResult:


    violations: list[Violation] = field(default_factory=list)
    hits: list[LinkHit] = field(default_factory=list)
    unreachable: list[tuple[int, tuple[float, float, float]]] = field(default_factory=list)
    waypoints: int = 0
    poses: int = 0
    beads: int = 0
    station_deg: float = 0.0

    def arm_hits(self) -> list[LinkHit]:

        return [h for h in self.hits if not h.is_torch]

    def blocking(self, *, contact_ok: tuple[str, ...] = CONTACT_OK,
                 max_penetration_mm: float = 0.0) -> list[Violation]:


        return blocking(self.violations, contact_ok=contact_ok,
                        max_penetration_mm=max_penetration_mm)

    def worst(self) -> Violation | None:
        return min(self.violations, key=lambda v: v.clearance_mm, default=None)


@dataclass(frozen=True)
class Comparison:


    agreed: tuple[tuple[int, str, float, float], ...]
    tier1_only: tuple[Violation, ...]
    tier2_only: tuple[Violation, ...]
    verdict_agrees: bool
    tier1_blocking: int
    tier2_blocking: int

    def summary(self) -> str:
        head = "AGREE" if self.verdict_agrees else "DISAGREE"
        return (f"{head}: tier1 {self.tier1_blocking} blocking, "
                f"tier2 {self.tier2_blocking} blocking; "
                f"{len(self.agreed)} shared, {len(self.tier1_only)} tier1-only, "
                f"{len(self.tier2_only)} tier2-only")

    def notes(self) -> list[str]:

        out: list[str] = []
        if self.tier1_only:
            out.append(
                "tier1-only findings are usually the SWEPT torch envelope: tier 1 "
                "does not know the wrist yaw, so it checks a rotationally "
                "symmetric superset (max lateral reach ~221 mm) of a torch that "
                "tier 2 places with the yaw the IK actually solved. Tier 1 "
                "can therefore report more proximity. This is a possible cause, "
                "not proof that a particular tier-2 miss is harmless.")
        if self.tier2_only:
            out.append(
                "tier2-only findings may involve an ARM link absent from tier 1, "
                "convex-hull geometry, different bead/torch proxies, or query "
                "sampling. `SimResult.arm_hits()` identifies arm findings; vertex "
                "counts alone cannot diagnose hull inflation. Inspect each case.")
        return out


def compare(tier1: list[Violation], tier2: list[Violation], *,
            contact_ok: tuple[str, ...] = CONTACT_OK) -> Comparison:


    a = {(v.move_index, v.obstacle): v for v in tier1}
    b = {(v.move_index, v.obstacle): v for v in tier2}
    shared = sorted(set(a) & set(b))
    return Comparison(
        agreed=tuple((m, o, a[(m, o)].clearance_mm, b[(m, o)].clearance_mm)
                     for m, o in shared),
        tier1_only=tuple(a[k] for k in sorted(set(a) - set(b))),
        tier2_only=tuple(b[k] for k in sorted(set(b) - set(a))),
        verdict_agrees=(bool(blocking(tier1, contact_ok=contact_ok))
                        == bool(blocking(tier2, contact_ok=contact_ok))),
        tier1_blocking=len(blocking(tier1, contact_ok=contact_ok)),
        tier2_blocking=len(blocking(tier2, contact_ok=contact_ok)),
    )


@dataclass(frozen=True)
class ShapeReport:


    link: str
    stl: str
    stl_vertices: int
    sim_vertices: int | None
    verdict: str


def _hc10_pose(q_deg=None, station_deg=None) -> dict[str, float]:


    if station_deg is not None and abs(float(station_deg)) > 1e-9:
        raise ValueError(
            "the HC10 binding has no station (requires_station=False): "
            f"station_deg must be 0, got {station_deg!r}")
    return {} if q_deg is None else hc10.urdf_joint_positions(q_deg)


def hc_torch_capsules_flange() -> tuple[tuple[np.ndarray, np.ndarray, float], ...]:


    return torch_capsules_flange(cell_hc.HC_TOOL_INTERFERE_CAPSULES,
                                 cell_hc.HC_TOOL0_NATIVE_TO_FLANGE)


@dataclass(frozen=True)
class CellBinding:


    name: str
    urdf_path: Path
    requires_station: bool
    arm_link_names: tuple[str, ...]
    arm_joint_names: tuple[str, ...]
    pose_fn: Callable[..., dict[str, float]]
    fk_fn: Callable[..., dict[str, np.ndarray]]
    torch_capsules_fn: Callable[[], tuple[tuple[np.ndarray, np.ndarray, float], ...]]
    torch_shape_names: tuple[str, ...]
    default_scene_fn: Callable[[np.ndarray], Scene]
    default_origin: np.ndarray
    robot_base_offset: np.ndarray
    make_solve: Callable[[np.ndarray], Callable[..., list[float]]]
    ignore_pairs: tuple[tuple[str, str], ...]
    mesh_links: dict[str, tuple[str, tuple[float, float, float]]]
    mesh_collision_dir: Callable[[Path], Path]
    uncalibrated: bool = False

    def native_origin(self, origin_robot_frame) -> np.ndarray:


        return np.asarray(origin_robot_frame, float) + self.robot_base_offset


AR1440_LC_BINDING = CellBinding(
    name="ar1440/lc50s",
    urdf_path=DEFAULT_URDF,
    requires_station=True,
    arm_link_names=ARM_LINK_NAMES,
    arm_joint_names=ar1440.ARM_JOINT_NAMES,
    pose_fn=ar1440.urdf_joint_positions,
    fk_fn=ar1440.fk,
    torch_capsules_fn=torch_capsules_flange,
    torch_shape_names=TORCH_SHAPE_NAMES,
    default_scene_fn=cell_lc.lc_scene,
    default_origin=cell_lc.DEFAULT_PART_ORIGIN,
    robot_base_offset=np.zeros(3),
    make_solve=cell_lc.pulse_solver,
    ignore_pairs=DEFAULT_IGNORE_PAIRS,
    mesh_links=ar1440._MESH_LINKS,
    mesh_collision_dir=lambda urdf_path: urdf_path.parent / "meshes" / "ar1440" / "collision",
    uncalibrated=False,
)


HC10_BINDING = CellBinding(
    name="hc10dtp/aw-hc",
    urdf_path=HC_DEFAULT_URDF,
    requires_station=False,
    arm_link_names=HC_ARM_LINK_NAMES,
    arm_joint_names=hc10.ARM_JOINT_NAMES,
    pose_fn=_hc10_pose,
    fk_fn=hc10.fk,
    torch_capsules_fn=hc_torch_capsules_flange,
    torch_shape_names=HC_TORCH_SHAPE_NAMES,
    default_scene_fn=cell_hc.hc_scene,
    default_origin=cell_hc.DEFAULT_PART_ORIGIN - cell_hc.ROBOT_BASE,
    robot_base_offset=cell_hc.ROBOT_BASE,
    make_solve=cell_hc.hc_solver,
    ignore_pairs=cell_hc.HC_PYBULLET_IGNORE_PAIRS,
    mesh_links=hc10._MESH_LINKS,
    mesh_collision_dir=lambda urdf_path: urdf_path.parent / "meshes",


    uncalibrated=True,
)

class PyBulletDriver:


    def __init__(
        self,
        *,
        binding: CellBinding = AR1440_LC_BINDING,
        urdf_path=None,
        scene: Scene | None = None,
        part_origin_robot=None,
        gui: bool = False,
        ignore_pairs: tuple[tuple[str, str], ...] | None = None,
        urdf_station_collision: bool = False,
        torch: bool = True,
        sense: int = station.STATION_SENSE,
    ):
        p = require_pybullet()
        self._p = p
        self.binding = binding
        self.urdf_path = Path(binding.urdf_path if urdf_path is None else urdf_path)
        self.report = inspect_urdf(self.urdf_path)
        problems = self.report.problems(
            arm_link_names=binding.arm_link_names,
            arm_joint_names=binding.arm_joint_names,
            requires_station=binding.requires_station,
        )
        if problems:
            raise ValueError(
                f"{self.urdf_path} is not fit to load: " + "; ".join(problems))
        if urdf_station_collision and not binding.requires_station:


            raise ValueError(
                f"{binding.name} has no station branch (requires_station=False); "
                "urdf_station_collision=True has nothing to query")


        self.origin = np.asarray(
            binding.default_origin if part_origin_robot is None
            else part_origin_robot, float)
        native_origin = binding.native_origin(self.origin)
        self.scene = binding.default_scene_fn(native_origin) if scene is None else scene
        self.ignore_pairs = frozenset(
            binding.ignore_pairs if ignore_pairs is None else ignore_pairs)
        self.sense = int(sense)
        self.gui = bool(gui)

        self.client = p.connect(p.GUI if gui else p.DIRECT)
        if self.client is not None and self.client < 0:
            raise RuntimeError(
                "pybullet.connect failed (GUI needs a display; under WSL2 that "
                "is WSLg - see docs/wsl2-setup.md section 8, and note the GUI is "
                "playback, never the check: DIRECT computes getClosestPoints "
                "identically)")


        p.setGravity(0, 0, 0, physicsClientId=self.client)


        p.setPhysicsEngineParameter(enableFileCaching=0, physicsClientId=self.client)
        p.setAdditionalSearchPath(str(self.urdf_path.parent), physicsClientId=self.client)


        self.body = p.loadURDF(str(self.urdf_path), useFixedBase=True,
                               physicsClientId=self.client)
        rows = [p.getJointInfo(self.body, i, physicsClientId=self.client)
                for i in range(p.getNumJoints(self.body, physicsClientId=self.client))]
        wanted_joints = tuple(binding.arm_joint_names)
        if binding.requires_station:
            wanted_joints += (station.STATION_JOINT_NAME,)
        self.joint_index = map_joint_indices(rows, wanted_joints)
        self.link_name = map_link_indices(rows, self.report.root_link)
        self.link_index = {v: k for k, v in self.link_name.items()}

        queried = list(binding.arm_link_names)
        if urdf_station_collision:
            queried += list(station.STATION_LINK_NAMES)
        self.queried_links = tuple(n for n in queried if n in self.link_index)
        self._queried_idx = frozenset(self.link_index[n] for n in self.queried_links)

        self._torch_bodies: dict[str, int] = {}
        self._torch_caps: tuple = ()
        if torch:
            self._build_torch()
        self._obstacles: dict[str, int] = {}
        self._obstacle_specs: dict[str, BodySpec] = {}
        self._bead_bodies: list[int] = []
        self._bead_list: list[BeadSpec] = []
        self._link_bounds: dict[str, float] | None = None
        self._placed_station_deg: float | None = None
        self.place_scene(station_deg=0.0)


    def __enter__(self) -> "PyBulletDriver":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "client", None) is not None:
            try:
                self._p.disconnect(physicsClientId=self.client)
            finally:
                self.client = None


    def _make(self, spec: BodySpec, rgba=(0.8, 0.35, 0.2, 0.6)) -> int:
        p = self._p
        if spec.kind == "box":
            col = p.createCollisionShape(p.GEOM_BOX, halfExtents=list(spec.half_extents),
                                         physicsClientId=self.client)
            vis = (p.createVisualShape(p.GEOM_BOX, halfExtents=list(spec.half_extents),
                                       rgbaColor=list(rgba), physicsClientId=self.client)
                   if self.gui else -1)
        elif spec.kind == "capsule":
            if spec.length <= 1e-9:
                col = p.createCollisionShape(p.GEOM_SPHERE, radius=spec.radius,
                                             physicsClientId=self.client)
                vis = (p.createVisualShape(p.GEOM_SPHERE, radius=spec.radius,
                                           rgbaColor=list(rgba), physicsClientId=self.client)
                       if self.gui else -1)
            else:


                col = p.createCollisionShape(p.GEOM_CAPSULE, radius=spec.radius,
                                             height=spec.length, physicsClientId=self.client)
                vis = (p.createVisualShape(p.GEOM_CAPSULE, radius=spec.radius,
                                           length=spec.length, rgbaColor=list(rgba),
                                           physicsClientId=self.client)
                       if self.gui else -1)
        elif spec.kind == "convex":
            col = p.createCollisionShape(p.GEOM_MESH, vertices=list(spec.vertices),
                                         physicsClientId=self.client)
            vis = (p.createVisualShape(p.GEOM_MESH, vertices=list(spec.vertices),
                                       indices=list(spec.indices),
                                       rgbaColor=list(rgba), physicsClientId=self.client)
                   if self.gui else -1)
        else:
            raise ValueError(f"unknown obstacle kind {spec.kind!r}")
        body = p.createMultiBody(
            baseMass=0.0,
            baseCollisionShapeIndex=col,
            baseVisualShapeIndex=vis,
            basePosition=list(spec.position),
            baseOrientation=list(spec.orientation),
            physicsClientId=self.client,
        )
        if spec.kind == "box":


            p.changeDynamics(body, -1, collisionMargin=0.0, physicsClientId=self.client)
        return body

    def place_scene(self, *, station_deg: float = 0.0) -> None:


        p = self._p
        specs = scene_body_specs(self.scene, self.origin,
                                 station_deg=station_deg, sense=self.sense)
        for spec in specs:
            if spec.name in self._obstacles:
                p.resetBasePositionAndOrientation(
                    self._obstacles[spec.name], list(spec.position),
                    list(spec.orientation), physicsClientId=self.client)
            else:
                self._obstacles[spec.name] = self._make(spec)
            self._obstacle_specs[spec.name] = spec


        place = _station_placement(station_deg, self.sense)
        for bead, body in zip(self._bead_list, self._bead_bodies):
            w = _to_world_mm(np.vstack([bead.p0, bead.p1]), self.origin, place)
            centre, quat, _ = capsule_frame(w[0], w[1])
            p.resetBasePositionAndOrientation(body, list(centre), list(quat),
                                               physicsClientId=self.client)
        self._placed_station_deg = float(station_deg)

    def _build_torch(self) -> None:


        p = self._p
        self._torch_caps = self.binding.torch_capsules_fn()
        names = self.binding.torch_shape_names
        if len(names) != len(self._torch_caps):
            raise ValueError(
                f"{self.binding.name}: torch_shape_names has {len(names)} "
                f"name(s) but torch_capsules_fn() returned {len(self._torch_caps)}")
        for name, (a, b, r) in zip(names, self._torch_caps):
            _c, _q, length = capsule_frame(a, b)
            if length <= 1e-9:
                col = p.createCollisionShape(p.GEOM_SPHERE, radius=r * _M,
                                             physicsClientId=self.client)
                vis = (p.createVisualShape(p.GEOM_SPHERE, radius=r * _M,
                                           rgbaColor=[0.9, 0.75, 0.1, 0.7],
                                           physicsClientId=self.client)
                       if self.gui else -1)
            else:
                col = p.createCollisionShape(p.GEOM_CAPSULE, radius=r * _M, height=length,
                                             physicsClientId=self.client)
                vis = (p.createVisualShape(p.GEOM_CAPSULE, radius=r * _M, length=length,
                                           rgbaColor=[0.9, 0.75, 0.1, 0.7],
                                           physicsClientId=self.client)
                       if self.gui else -1)
            self._torch_bodies[name] = p.createMultiBody(
                baseMass=0.0, baseCollisionShapeIndex=col, baseVisualShapeIndex=vis,
                basePosition=[0, 0, -10], physicsClientId=self.client)

    def add_bead(self, bead: BeadSpec, *, station_deg: float = 0.0) -> None:

        place = _station_placement(station_deg, self.sense)
        w = _to_world_mm(np.vstack([bead.p0, bead.p1]), self.origin, place)
        centre, quat, length = capsule_frame(w[0], w[1])
        spec = BodySpec(name=bead.obstacle, kind="capsule", position=centre,
                        orientation=quat, radius=bead.radius_mm * _M,
                        length=length, part_fixed=True)
        self._bead_bodies.append(self._make(spec, rgba=(0.9, 0.5, 0.2, 0.8)))
        self._bead_list.append(bead)

    def clear_beads(self) -> None:
        for b in self._bead_bodies:
            self._p.removeBody(b, physicsClientId=self.client)
        self._bead_bodies.clear()
        self._bead_list.clear()


    def pose(self, q_deg=None, station_deg=None) -> None:


        for name, radians in self.binding.pose_fn(q_deg, station_deg).items():
            self._p.resetJointState(self.body, self.joint_index[name], radians,
                                    physicsClientId=self.client)
        if q_deg is not None and self._torch_bodies:
            self._place_torch(q_deg)

    def _place_torch(self, q_deg) -> None:
        flange = self.binding.fk_fn(list(q_deg))["flange"]
        R, t = flange[:3, :3], flange[:3, 3]
        for name, (a, b, _r) in zip(self.binding.torch_shape_names, self._torch_caps):
            wa, wb = R @ a + t, R @ b + t
            centre, quat, _len = capsule_frame(wa, wb)
            self._p.resetBasePositionAndOrientation(
                self._torch_bodies[name], list(centre), list(quat),
                physicsClientId=self.client)


    def _closest(self, other: int, margin_mm: float) -> list[tuple[str, float]]:


        rows = self._p.getClosestPoints(self.body, other, margin_mm * _M,
                                        physicsClientId=self.client)
        out: list[tuple[str, float]] = []
        for row in rows:
            idx = int(row[3])
            if idx in self._queried_idx:
                out.append((self.link_name[idx], float(row[8]) * 1000.0))
        return out

    def _closest_torch(self, other: int, margin_mm: float) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for name, body in self._torch_bodies.items():
            rows = self._p.getClosestPoints(body, other, margin_mm * _M,
                                            physicsClientId=self.client)
            for row in rows:
                out.append((name, float(row[8]) * 1000.0))
        return out

    def link_bounds(self) -> dict[str, float]:


        if self._link_bounds is None:
            self._link_bounds = link_bound_radii(
                self.binding.mesh_collision_dir(self.urdf_path), self.binding.mesh_links)
        return self._link_bounds

    def _bead_reach(self, q_deg, margin_mm: float) -> tuple[np.ndarray, np.ndarray] | None:


        bounds = self.link_bounds()
        if any(not np.isfinite(r) for r in bounds.values()):
            return None
        frames = self.binding.fk_fn(list(q_deg))
        centres, radii = [], []
        for i, name in enumerate(self.binding.arm_link_names):
            if name not in bounds:
                return None
            T = np.eye(4) if i == 0 else frames[self.binding.arm_joint_names[i - 1]]
            centres.append(T[:3, 3])
            radii.append(bounds[name] + margin_mm)
        if self._torch_bodies:
            flange = frames["flange"]
            for a, b, r in self._torch_caps:
                centres.append(flange[:3, :3] @ ((a + b) / 2.0) + flange[:3, 3])
                radii.append(float(np.linalg.norm(b - a) / 2.0 + r) + margin_mm)
        return np.array(centres, float), np.array(radii, float)

    def _bead_targets(self, q_deg, margin_mm: float,
                      station_deg: float) -> list[tuple[str, int]]:


        pairs = [(b.obstacle, body)
                 for b, body in zip(self._bead_list, self._bead_bodies)]
        if not pairs:
            return []
        if any(n not in self.binding.arm_link_names for n in self.queried_links):
            return pairs
        reach = self._bead_reach(q_deg, margin_mm)
        if reach is None:
            return pairs
        centres, radii = reach
        place = _station_placement(station_deg, self.sense)
        a = _to_world_mm(np.array([b.p0 for b in self._bead_list]), self.origin, place)
        b = _to_world_mm(np.array([b.p1 for b in self._bead_list]), self.origin, place)
        bead_r = np.array([x.radius_mm for x in self._bead_list], float)
        d = _point_segment_dist(centres, a, b) - bead_r[None, :] - radii[:, None]
        keep = (d < 0.0).any(axis=0)
        return [pair for pair, k in zip(pairs, keep) if k]

    def contacts_at(self, q_deg, *, margin_mm: float = 3.0, station_deg: float = 0.0,
                    include_beads: bool = True) -> list[tuple[str, str, float]]:


        if self._placed_station_deg != float(station_deg):
            self.place_scene(station_deg=station_deg)
        self.pose(q_deg=q_deg, station_deg=station_deg)
        out: list[tuple[str, str, float]] = []
        targets = list(self._obstacles.items())
        if include_beads:
            targets += self._bead_targets(q_deg, margin_mm, station_deg)
        for name, body in targets:
            for link, mm in self._closest(body, margin_mm):
                if (link, name) not in self.ignore_pairs:
                    out.append((link, name, mm))
            for link, mm in self._closest_torch(body, margin_mm):
                out.append((link, name, mm))
        return out

    def inspect_collision_shapes(self) -> list[ShapeReport]:


        p = self._p
        from waam_pipeline.slicer import load_stl

        out: list[ShapeReport] = []
        collision_dir = self.binding.mesh_collision_dir(self.urdf_path)
        for link, (stl_name, _off) in self.binding.mesh_links.items():
            if link not in self.link_index:
                continue
            path = collision_dir / stl_name
            if not path.is_file():
                out.append(ShapeReport(link, str(path), 0, None, "STL MISSING"))
                continue
            tris = load_stl(path)
            n_stl = len({tuple(np.round(v, 6)) for v in tris.reshape(-1, 3)})
            try:
                data = p.getMeshData(self.body, self.link_index[link],
                                     flags=p.MESH_DATA_SIMULATION_MESH,
                                     physicsClientId=self.client)
                n_sim = int(data[0])
            except Exception:
                n_sim = None
            if n_sim is None:
                verdict = "UNKNOWN (getMeshData declined)"
            elif n_sim < n_stl:
                verdict = f"CONVEX HULL consistent ({n_sim} of {n_stl} vertices; inflation not measured)"
            else:
                verdict = f"VERTEX COUNT {n_sim} (topology and hull inflation not established)"
            out.append(ShapeReport(link, stl_name, n_stl, n_sim, verdict))
        return out


    def check(
        self,
        toolpath,
        *,
        bead_width_mm: float = 9.0,
        bead_height_mm: float = 3.0,
        margin_mm: float = 3.0,
        exclusion_mm: float = 12.0,
        sample_mm: float = 4.0,
        station_deg: float = 0.0,
        solve=None,
        bead_exclusion: str = "torch",
        progress=None,
    ) -> SimResult:


        if bead_exclusion not in ("torch", "all", "none"):
            raise ValueError("bead_exclusion is 'torch', 'all' or 'none'")
        if solve is None:
            if abs(float(station_deg)) > 1e-12:
                raise ValueError(
                    "station_deg != 0 needs a station-aware `solve`: "
                    f"{self.binding.name}'s default solver maps the part frame "
                    "to the robot frame by TRANSLATION ALONE, so it would drive "
                    "the arm to where the part used to be. The obstacle "
                    "placement here does handle the station (scene_body_specs); "
                    "the IK does not.")
            solve = self.binding.make_solve(self.binding.native_origin(self.origin))

        self.clear_beads()
        self.place_scene(station_deg=station_deg)
        result = SimResult(station_deg=float(station_deg))
        pending = bead_specs(toolpath, bead_width_mm=bead_width_mm,
                             bead_height_mm=bead_height_mm)
        by_move = {b.move_index: b for b in pending}


        worst: dict[tuple[int, str], tuple[np.ndarray, float, int]] = {}

        worst_link: dict[tuple[int, str, str], tuple[np.ndarray, float, int]] = {}

        prev = None
        flat_prev = None
        for i, move in enumerate(toolpath.moves):
            target = np.asarray(move.target, float)
            flat_target = target - np.array(
                [0.0, 0.0, getattr(move, "seat", 0.0) + getattr(move, "ctwd_lift", 0.0)])
            if prev is None:
                samples = target.reshape(1, 3)
                flat_samples = flat_target.reshape(1, 3)
            else:
                params = _sample_params(prev, target, sample_mm)
                samples = prev + params[:, None] * (target - prev)
                flat_samples = flat_prev + params[:, None] * (flat_target - flat_prev)
            vertical = prev is not None and np.linalg.norm(target[:2] - prev[:2]) < 1e-6
            for sample, flat_sample in zip(samples, flat_samples):
                result.waypoints += 1
                try:
                    q = solve(tuple(sample))
                except ValueError:
                    result.unreachable.append(
                        (i, tuple(float(round(c, 2)) for c in sample)))
                    continue
                result.poses += 1
                forgiven = set()
                if (bead_exclusion != "none" and self._bead_list
                        and (move.deposit or vertical)):
                    start_z = flat_sample[2] if prev is None else flat_prev[2]
                    mask = excluded_beads(
                        flat_sample, self._bead_list, exclusion_mm, start_z)
                    forgiven = {b.obstacle for b, m in zip(self._bead_list, mask) if m}
                for link, name, mm in self.contacts_at(
                        q, margin_mm=margin_mm, station_deg=station_deg):
                    if name in forgiven and (
                            bead_exclusion == "all"
                            or _is_torch_link(link)):
                        continue
                    key = (i, name)
                    if key not in worst or mm < worst[key][1]:
                        worst[key] = (sample, mm, move.line_no)
                    lkey = (i, name, link)
                    if lkey not in worst_link or mm < worst_link[lkey][1]:
                        worst_link[lkey] = (sample, mm, move.line_no)
                if progress is not None:
                    progress(i, len(toolpath.moves))
            if i in by_move:
                self.add_bead(by_move[i], station_deg=station_deg)
            prev = target
            flat_prev = flat_target

        result.beads = len(self._bead_list)
        for (move_index, name), (pt, mm, line) in sorted(worst.items()):
            result.violations.append(_violation(move_index, line, pt, name, mm))
        for (move_index, name, link), (pt, mm, line) in sorted(worst_link.items()):
            result.hits.append(LinkHit(
                violation=_violation(move_index, line, pt, name, mm),
                link=link, station_deg=float(station_deg)))
        result.violations.sort(key=lambda v: (v.move_index, v.clearance_mm))
        result.hits.sort(key=lambda h: h.violation.clearance_mm)
        return result


def _violation(move_index: int, line_no: int, point_part_mm, obstacle: str,
               clearance_mm: float) -> Violation:


    return Violation(
        move_index=int(move_index),
        gcode_line=int(line_no),
        point=tuple(float(c) for c in np.round(np.asarray(point_part_mm, float), 2)),
        obstacle=str(obstacle),
        clearance_mm=float(clearance_mm),
    )


def check(toolpath, *, binding: CellBinding = AR1440_LC_BINDING, urdf_path=None,
          scene=None, part_origin_robot=None,
          gui: bool = False, torch: bool = True,
          ignore_pairs: tuple[tuple[str, str], ...] | None = None,
          urdf_station_collision: bool = False, **kwargs) -> SimResult:


    with PyBulletDriver(binding=binding, urdf_path=urdf_path, scene=scene,
                        part_origin_robot=part_origin_robot, gui=gui, torch=torch,
                        ignore_pairs=ignore_pairs,
                        urdf_station_collision=urdf_station_collision) as drv:
        return drv.check(toolpath, **kwargs)
