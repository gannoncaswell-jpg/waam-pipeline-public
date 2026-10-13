from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import touch
from .clearance import Scene, TorchModel
from .plan import HC_PARK_Q
from .robots import hc10
from .slicer import load_stl


ROOT = Path(__file__).resolve().parents[2]
MESH_PATH = ROOT / "hardware" / "cell_models" / "meshes" / "aw_hc_display.npz"
TORCH_MESH_PATH = ROOT / "hardware" / "cell_models" / "meshes" / "aw_hc_torch.npz"
TORCH_KEY = "Yaskawa_Torch"
PROFILE_ID = "hc10"
LABEL = "AW-HC · HC10DTP preview"
EXPORT_REASON = (
    "HC10DTP preview only. Clearance and robot exports require calibrated "
    "cell, tool and controller profiles."
)


TABLE_Z_MODEL = 967.2326049804688
TABLE_XY_MIN = np.array([-609.6063232421875, -1219.1842041015625])
TABLE_XY_MAX = np.array([609.5936279296875, 1219.2158203125])

ROBOT_BASE = np.array([-508.006, 0.016, 1242.23 - 275.0])


TORCH_NATIVE_TO_FLANGE = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


TORCH_TCP_NATIVE_MM = np.array([-2.87, 0.0, 298.338])
TORCH_TCP_NATIVE_RY_DEG = -46.5


TORCH_TCP_FLANGE_MM = TORCH_TCP_NATIVE_MM @ TORCH_NATIVE_TO_FLANGE.T


HC_TOOL0_TCP_NATIVE_MM = np.array([-2.758, -2.040, 304.681])
HC_TOOL0_TCP_RY_DEG = -44.0


HC_TOOL0_NATIVE_TO_FLANGE = np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])
HC_TOOL0_TCP_FLANGE_MM = HC_TOOL0_NATIVE_TO_FLANGE @ HC_TOOL0_TCP_NATIVE_MM


_CALIBRATION_PATH = ROOT / "docs" / "cell-hc-calibration.json"
MAX_TABLE_OFFSET_MM = 50.0
_MAX_FIT_DRIFT_MM = 0.2
_MAX_FIT_TILT_DRIFT_DEG = 0.05


@dataclass(frozen=True)
class HcTablePlane:


    point: np.ndarray
    normal: np.ndarray
    tilt_deg: float
    residual_mm: float

    @property
    def offset_mm(self) -> float:
        return float(self.point[2] - TABLE_Z_MODEL)


def fit_hc_table(touch_offs: list[dict]) -> HcTablePlane:

    try:
        pulses = np.asarray([r["pulses_SLURBT"] for r in touch_offs], dtype=float)
        distances = np.asarray([r["vertical_distance_mm"] for r in touch_offs], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("HC table touch-offs need six pulses and a vertical distance") from exc
    if (pulses.ndim != 2 or pulses.shape[1] != 6 or len(pulses) < 3
            or distances.shape != (len(pulses),)
            or not np.isfinite(pulses).all() or not np.isfinite(distances).all()
            or np.any(pulses != np.round(pulses)) or np.any(distances < 0)):
        raise ValueError("HC table needs at least three finite pulse records and nonnegative distances")

    points = []
    for pulse, distance in zip(pulses, distances):
        flange = hc10.fk(pulse / hc10.PULSES_PER_DEG)["flange"]
        tcp = flange[:3, 3] + flange[:3, :3] @ HC_TOOL0_TCP_FLANGE_MM
        surface = tcp + ROBOT_BASE
        surface[2] -= distance
        points.append(surface)
    points = np.asarray(points)
    centroid = points.mean(axis=0)
    centred = points - centroid
    _, sv, vh = np.linalg.svd(centred, full_matrices=False)
    if sv[1] < 1e-6:
        raise ValueError("HC table touch-offs are collinear; spread them across the surface")
    normal = vh[-1] / np.linalg.norm(vh[-1])
    if normal[2] < 0:
        normal = -normal
    if normal[2] < 1e-9:
        raise ValueError("HC table touch-offs describe a vertical surface")
    tilt = float(np.degrees(np.arccos(np.clip(normal[2], -1.0, 1.0))))
    residual = float(np.max(np.abs(centred @ normal)))
    centroid.setflags(write=False)
    normal.setflags(write=False)
    plane = HcTablePlane(centroid, normal, tilt, residual)
    if not -MAX_TABLE_OFFSET_MM <= plane.offset_mm <= MAX_TABLE_OFFSET_MM:
        raise ValueError(f"HC table offset {plane.offset_mm:.3f} is outside +-"
                         f"{MAX_TABLE_OFFSET_MM} mm; check the sign and units")
    return plane


def _load_calibration(path: Path | None = None) -> tuple[float, HcTablePlane | None]:

    path = _CALIBRATION_PATH if path is None else Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0.0, None
    if (not isinstance(data, dict) or data.get("tool") != 0
            or data.get("coordinates") != "PULSE"):
        raise ValueError(f"{path}: HC table calibration requires TOOL 0 PULSE records")
    plane = fit_hc_table(data.get("touch_offs"))
    if "_fitted" in data:
        for key, expected, tolerance in (
                ("offset_mm", plane.offset_mm, _MAX_FIT_DRIFT_MM),
                ("centroid_mm", plane.point, _MAX_FIT_DRIFT_MM),
                ("residual_mm", plane.residual_mm, _MAX_FIT_DRIFT_MM),
                ("tilt_deg", plane.tilt_deg, _MAX_FIT_TILT_DRIFT_DEG)):
            try:
                recorded = np.asarray(data["_fitted"][key], dtype=float)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}: invalid _fitted.{key}") from exc
            if recorded.shape != np.shape(expected) or not np.isfinite(recorded).all():
                raise ValueError(f"{path}: invalid _fitted.{key}")
            if np.linalg.norm(recorded - expected) > tolerance:
                raise ValueError(f"{path}: _fitted.{key} disagrees with raw touch-offs; re-fit the record")
    return plane.offset_mm, plane


HC_TABLE_OFFSET_MM, HC_TABLE_PLANE = _load_calibration()
TABLE_Z = TABLE_Z_MODEL + HC_TABLE_OFFSET_MM
DEFAULT_PART_ORIGIN = np.array([250.0, 300.0, TABLE_Z])


def hc_work_surface_z(substrate_mm: float = 0.0) -> float:

    substrate_mm = float(substrate_mm)
    if not np.isfinite(substrate_mm) or substrate_mm < 0:
        raise ValueError("HC substrate thickness must be finite and nonnegative")
    return TABLE_Z + substrate_mm


CLEANERS = {"none": None, "yaskawa": "Yaskawa_Reamer",
            "tregaskiss": "Tregaskiss_Reamer", "binzel": "Binzel_Reamer"}

DEFAULT_CLEANER = "yaskawa"
FURNITURE = {"hc_table": "Table", "hc_back_panel": "BackPanel",
             "hc_balancer": "Balancer", "hc_pendant_mount": "Pendant_Bracket",
             "hc_pendant": "Pendant", "hc_fume_extraction": "Fume__Extraction"}


def profile(cleaner: str = DEFAULT_CLEANER) -> dict:
    if cleaner not in CLEANERS:
        raise ValueError("HC cleaner: choose none, yaskawa, tregaskiss or binzel")
    return {"id": PROFILE_ID, "planning_only": True, "robot_motion": "toolpath_shadow",
            "cleaner": cleaner, "source": "AW-HC.zip / AW-HC.vcl",
            "note": "Motion preview uses nominal cell geometry. Installed alignment and tooling require verification.",
            "export_reason": EXPORT_REASON}


def place_origin(bounds, *, ox=0.0, oy=0.0, bottom=0.0, substrate=0.0, gap=0.0):


    bounds = np.asarray(bounds, dtype=float)
    values = np.asarray([ox, oy, bottom, substrate, gap], dtype=float)
    if bounds.shape != (2, 2) or not np.isfinite(bounds).all() or not np.isfinite(values).all():
        raise ValueError("AW-HC placement requires finite XY bounds and millimetre values")
    if np.any(bounds[1] < bounds[0]) or substrate < 0 or gap < 0:
        raise ValueError("Invalid AW-HC part bounds, substrate or gap")
    low, high = TABLE_XY_MIN - bounds[0], TABLE_XY_MAX - bounds[1]
    if np.any(low > high):
        raise ValueError("Part footprint does not fit the AW-HC table's nominal bounding rectangle")
    xy = np.clip(DEFAULT_PART_ORIGIN[:2] + [ox, oy], low, high)
    return np.array([*xy, hc_work_surface_z(substrate) + gap - bottom])


@lru_cache(maxsize=1)
def _furniture():
    with np.load(MESH_PATH, allow_pickle=False) as archive:
        meshes = {name: archive[name].copy() for name in archive.files}
    expected = set(FURNITURE.values()) | {"Curtian-Left", "Curtian-Right"} | (set(CLEANERS.values()) - {None})
    if set(meshes) != expected:
        raise ValueError("AW-HC display asset set is incomplete; rebuild the pinned vendor import")
    for name, mesh in meshes.items():
        if mesh.ndim != 3 or mesh.shape[1:] != (3, 3) or not np.isfinite(mesh).all():
            raise ValueError("Invalid AW-HC display mesh")
        if name == "Table":
            mesh = mesh.astype(float)
            mesh[:, :, 2] += HC_TABLE_OFFSET_MM
            meshes[name] = mesh
        mesh.setflags(write=False)
    return meshes


def scene_meshes(origin, *, cleaner=DEFAULT_CLEANER) -> dict:
    profile(cleaner)
    origin = np.asarray(origin, dtype=float)
    meshes = _furniture()
    def placed(mesh):
        return np.round(mesh.astype(float) - origin, 3).ravel().tolist()
    out = {name: placed(meshes[source]) for name, source in FURNITURE.items()}

    out["cell_walls"] = placed(np.concatenate([meshes["Curtian-Left"], meshes["Curtian-Right"]]))
    if CLEANERS[cleaner] is not None:
        out["hc_cleaner"] = placed(meshes[CLEANERS[cleaner]])
    return out


def reference_boxes(origin, *, cleaner=DEFAULT_CLEANER) -> list[dict]:

    profile(cleaner)
    origin = np.asarray(origin, dtype=float)
    meshes = _furniture()
    selected = dict(FURNITURE)
    selected.update(hc_curtain_left="Curtian-Left", hc_curtain_right="Curtian-Right")
    if CLEANERS[cleaner] is not None:
        selected["hc_cleaner"] = CLEANERS[cleaner]
    return [{"name": name, "min": (meshes[source].min(axis=(0, 1)) - origin).tolist(),
             "max": (meshes[source].max(axis=(0, 1)) - origin).tolist()}
            for name, source in selected.items()]


@lru_cache(maxsize=1)
def _link_meshes():
    out = {}
    for link, (filename, offset) in hc10._MESH_LINKS.items():


        visual = ROOT / "robots" / "meshes" / filename.replace("/collision/", "/visual/")
        path = visual if visual.exists() else ROOT / "robots" / "meshes" / filename
        triangles = load_stl(path) * 1000.0
        out[link] = {"tris": np.round(triangles, 3).ravel().tolist(),
                     "offset": (np.asarray(offset) * 1000.0).tolist()}
    return out


@lru_cache(maxsize=1)
def _torch_mesh():


    with np.load(TORCH_MESH_PATH, allow_pickle=False) as archive:
        mesh = archive[TORCH_KEY].copy()
    if mesh.ndim != 3 or mesh.shape[1:] != (3, 3) or not np.isfinite(mesh).all():
        raise ValueError("Invalid AW-HC torch display mesh")
    mesh.setflags(write=False)
    return mesh


def reference_robot(origin, *, include_meshes=True) -> dict:


    origin = np.asarray(origin, dtype=float)
    q = [0.0] * 6
    frames = hc10.fk(q)
    points = [np.array([0.0, 0.0, -ROBOT_BASE[2]]), np.zeros(3)]
    points += [frames[j.name][:3, 3] for j in hc10.HC10DTP_JOINTS[1:]]
    points.append(frames["flange"][:3, 3])
    flange = frames["flange"]
    flange_rot, flange_pos = flange[:3, :3], flange[:3, 3]


    native = _torch_mesh().astype(float)
    world = native @ TORCH_NATIVE_TO_FLANGE.T @ flange_rot.T + flange_pos
    torch_mesh = np.round(world + ROBOT_BASE - origin, 3).tolist()

    tcp_flange_frame = TORCH_TCP_NATIVE_MM @ TORCH_NATIVE_TO_FLANGE.T
    tcp_world = tcp_flange_frame @ flange_rot.T + flange_pos
    tcp_marker = (tcp_world + ROBOT_BASE - origin).tolist()

    return {
        "skeleton": [(np.asarray(points) + ROBOT_BASE - origin).tolist()],
        "joints": [q], "chain": [{"origin": list(j.origin), "axis": list(j.axis)}
                                  for j in hc10.HC10DTP_JOINTS],
        "meshes": _link_meshes() if include_meshes else None,
        "torch_mesh": torch_mesh,
        "tcp_marker": tcp_marker,
        "base_offset": (ROBOT_BASE - origin).tolist(),
        "ik_checked": False, "mode": "reference_pose", "unreachable": None,
        "tool_display": ("Yaskawa torch and TCP marker use nominal CAD dimensions. "
                          "Tool calibration and path IK are pending."),
        "note": ("Source zero pose; not robot motion or a reachability result. Torch and "
                  "TCP marker are vendor display geometry, not a calibrated tool."),
    }


def _hc_flange_pose(tcp_robot: np.ndarray, approach=(0.0, 0.0, -1.0)) -> np.ndarray:


    a = np.asarray(approach, float)
    a = a / np.linalg.norm(a)
    ref = np.array([0.0, 0.0, 1.0]) if abs(a[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    y = np.cross(a, ref)
    y = y / np.linalg.norm(y)
    z = np.cross(a, y)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2] = a, y, z
    T[:3, 3] = np.asarray(tcp_robot, float) - T[:3, :3] @ HC_TOOL0_TCP_FLANGE_MM
    return T


def hc_solver(part_origin_robot: np.ndarray | None = None):


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    state: dict = {"q": None}

    def solve(part_xyz, approach=(0.0, 0.0, -1.0)) -> list[float]:
        tcp_table = np.asarray(part_xyz, float) + o
        tcp_base = tcp_table - ROBOT_BASE
        pose = _hc_flange_pose(tcp_base, approach)
        seed = state["q"] if state["q"] is not None else list(HC_PARK_Q)
        q = hc10.ik(pose, seed=seed, strict=False)
        if q is None:
            raise ValueError(f"unreachable TCP (HC10 base frame): {np.round(tcp_base, 1)}")
        state["q"] = q
        return q

    return solve


def hc_probe_plan(
    face_z: float = TABLE_Z,
    *,
    centre_xy=(float(DEFAULT_PART_ORIGIN[0]), float(DEFAULT_PART_ORIGIN[1])),
    half_span_mm: float = 150.0,
    approach_mm: float = 25.0,
    overtravel_mm: float = 12.0,
    solve=None,
) -> list[touch.Probe]:


    cx, cy = float(centre_xy[0]), float(centre_xy[1])
    h = float(half_span_mm)
    plan: list[touch.Probe] = []
    for iy, dy in enumerate((-h, 0.0, h)):
        for ix, dx in enumerate((-h, 0.0, h)):
            expected = np.array([cx + dx, cy + dy, float(face_z)])
            pr = touch.Probe(
                name=f"tbl_{ix}{iy}",
                start=expected + np.array([0.0, 0.0, approach_mm]),
                target=expected - np.array([0.0, 0.0, overtravel_mm]),
                expected=expected,
            )
            if solve is not None:
                try:
                    solve(pr.start)
                    solve(pr.expected)
                except ValueError:
                    continue
            plan.append(pr)
    return plan


def hc_robot_shadow(toolpath, part_origin_robot: np.ndarray | None = None, *,
                    include_meshes=True) -> dict:


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    skeletons: list[list[list[float]] | None] = []
    joints: list[list[float] | None] = []
    misses = 0
    solve = hc_solver(o)
    for m in toolpath.moves:
        try:
            q = solve((m.x, m.y, m.z))
        except ValueError:
            skeletons.append(None)
            joints.append(None)
            misses += 1
            continue
        f = hc10.fk(q)
        pts_base = [
            np.array([0.0, 0.0, -ROBOT_BASE[2]]),
            np.zeros(3),
            f["joint_2_l"][:3, 3],
            f["joint_3_u"][:3, 3],
            f["joint_4_r"][:3, 3],
            f["joint_5_b"][:3, 3],
            f["flange"][:3, 3],
        ]
        tcp_table = np.array([m.x, m.y, m.z]) + o
        pts_table = [p + ROBOT_BASE for p in pts_base] + [tcp_table]
        skeletons.append([[round(float(c), 1) for c in (p - o)] for p in pts_table])
        joints.append([round(float(a), 2) for a in q])
    return {
        "skeleton": skeletons,
        "joints": joints,
        "unreachable": misses,
        "ik_checked": True,
        "mode": "toolpath_shadow",
        "chain": [{"origin": list(j.origin), "axis": list(j.axis)} for j in hc10.HC10DTP_JOINTS],
        "base_offset": (ROBOT_BASE - o).tolist(),
        "meshes": _link_meshes() if include_meshes else None,

        "torch_mesh_flange": np.round(_torch_mesh() @ TORCH_NATIVE_TO_FLANGE.T, 3).tolist(),
        "tcp_flange": HC_TOOL0_TCP_FLANGE_MM.tolist(),
        "tool_display": "Controller Tool 0 TCP. Torch shape from vendor CAD.",
        "note": ("Nominal cell geometry. Reachability preview only; cell survey "
                 "and HC10 pulse calibration are pending."),
    }


def hc_scene(part_origin_robot: np.ndarray | None = None) -> Scene:


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    meshes = _furniture()
    sc = Scene(part_fixed=())

    def box(name, source):
        tris = meshes[source]
        sc.boxes.append((name, tris.min(axis=(0, 1)) - o, tris.max(axis=(0, 1)) - o))

    for name in ("hc_table", "hc_back_panel", "hc_balancer"):
        box(name, FURNITURE[name])
    cleaner = CLEANERS[DEFAULT_CLEANER]
    if cleaner is not None:
        box("hc_cleaner", cleaner)
    return sc


HC_TOOL_INTERFERE_CAPSULES: tuple[tuple[tuple[float, float, float], tuple[float, float, float], float], ...] = (
    ((0.0, 0.0, 0.0), (0.0, 0.0, 25.0), 40.0),
    ((0.0, 5.0, 35.0), (150.0, 5.0, 110.0), 35.0),
    ((210.0, 0.0, -30.0), (100.0, 0.0, 200.0), 25.0),
    ((100.0, 0.0, 205.0), (15.0, 0.0, 290.0), 15.0),
)


def hc_torch_controller_tool0(sphere_step_mm: float = 6.0) -> TorchModel:


    ry = np.radians(HC_TOOL0_TCP_RY_DEG)
    u = np.array([-np.sin(ry), 0.0, -np.cos(ry)])
    rings: list[tuple[float, float, float]] = []
    for p0, p1, r in HC_TOOL_INTERFERE_CAPSULES:
        a = np.asarray(p0, float) - HC_TOOL0_TCP_NATIVE_MM
        b = np.asarray(p1, float) - HC_TOOL0_TCP_NATIVE_MM
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
    return TorchModel(sphere_step_mm=sphere_step_mm, samples=pts, rings=tuple(rings))


HC_PYBULLET_IGNORE_PAIRS: tuple[tuple[str, str], ...] = (
    ("base_link", "hc_table"),
)
