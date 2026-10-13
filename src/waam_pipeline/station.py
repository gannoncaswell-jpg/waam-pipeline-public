from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import cell_lc


STATION_SENSE = +1


MAX_SPEED_RPM = 12.4
MAX_SPEED_DEG_S = MAX_SPEED_RPM * 6.0
RATED_INERTIA_KG_M2 = 47.0
HOLDING_TORQUE_NM = 268.0


GEAR_RATIO = 161


SOFT_LIMIT_DEG = 360.0


STATION_JOINT_NAME = "joint_s1_station"
STATION_LINK_NAMES = ("station_base", "station_faceplate", "station_platter")


def axis_origin() -> np.ndarray:

    return np.array([cell_lc.FACEPLATE_X, 0.0, cell_lc.AXIS_Z], float)


def axis_direction(sense: int = STATION_SENSE) -> np.ndarray:

    if sense not in (1, -1):
        raise ValueError("sense must be +1 or -1")
    return np.array([0.0, float(sense), 0.0])


def rotation(theta_deg: float, sense: int = STATION_SENSE) -> np.ndarray:

    return cell_lc._rot(tuple(axis_direction(sense)), float(theta_deg))


def station_transform(theta_deg: float, sense: int = STATION_SENSE) -> np.ndarray:


    R = rotation(theta_deg, sense)
    o = axis_origin()
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = o - R @ o
    return T


def apply(points, theta_deg: float, sense: int = STATION_SENSE) -> np.ndarray:

    P = np.atleast_2d(np.asarray(points, float))
    if P.ndim != 2 or P.shape[1] != 3 or not np.isfinite(P).all():
        raise ValueError("points must be finite (N, 3) robot-frame millimetres")
    T = station_transform(theta_deg, sense)
    out = P @ T[:3, :3].T + T[:3, 3]
    return out.reshape(np.shape(points))


def _platter_transform(
    station_deg: float = 0.0, *, sense: int = STATION_SENSE,
) -> np.ndarray:

    angle = float(station_deg)
    if not np.isfinite(angle) or abs(angle) > SOFT_LIMIT_DEG:
        raise ValueError(
            f"station_deg must be finite and within +/-{SOFT_LIMIT_DEG:g} deg, "
            f"got {station_deg}"
        )
    return station_transform(angle, sense)


def platter_work_plane(
    station_deg: float = 0.0, *, sense: int = STATION_SENSE,
) -> tuple[np.ndarray, np.ndarray]:


    pose = _platter_transform(station_deg, sense=sense)
    point = cell_lc.DEFAULT_PART_ORIGIN.copy()
    point[2] = cell_lc.PLATTER_FACE_Z
    return pose[:3, :3] @ point + pose[:3, 3], pose[:3, 2].copy()


def part_frame_transform(
    theta_deg: float,
    part_origin_robot=None,
    *,
    sense: int = STATION_SENSE,
) -> np.ndarray:


    o = (cell_lc.DEFAULT_PART_ORIGIN if part_origin_robot is None
         else np.asarray(part_origin_robot, float))
    R = rotation(theta_deg, sense)
    a = axis_origin() - o
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = a - R @ a
    return T


def surface_normal_after(normal, theta_deg: float, sense: int = STATION_SENSE) -> np.ndarray:

    n = np.asarray(normal, float)
    mag = np.linalg.norm(n)
    if mag < 1e-9:
        raise ValueError("normal has no direction")
    return rotation(theta_deg, sense) @ (n / mag)


def downhand_error_deg(normal, theta_deg: float, sense: int = STATION_SENSE) -> float:


    n = surface_normal_after(normal, theta_deg, sense)
    return float(np.degrees(np.arccos(np.clip(n[2], -1.0, 1.0))))


def best_downhand_angle(
    normal,
    *,
    sense: int = STATION_SENSE,
    limits_deg: tuple[float, float] = (-180.0, 180.0),
    step_deg: float = 0.5,
) -> tuple[float, float]:


    lo, hi = float(limits_deg[0]), float(limits_deg[1])
    if hi < lo:
        raise ValueError("limits_deg must be (low, high)")
    grid = np.arange(lo, hi + step_deg, step_deg)
    if len(grid) == 0:
        grid = np.array([lo])
    errs = np.array([downhand_error_deg(normal, float(t), sense) for t in grid])
    i = int(np.argmin(errs))
    best = float(grid[i])

    for width in (step_deg, step_deg / 5.0):
        fine = np.linspace(max(lo, best - width), min(hi, best + width), 21)
        fe = np.array([downhand_error_deg(normal, float(t), sense) for t in fine])
        j = int(np.argmin(fe))
        best = float(fine[j])
    return best, downhand_error_deg(normal, best, sense)


@dataclass(frozen=True)
class StationMove:


    move_index: int
    theta_deg: float
    downhand_error_deg: float
    rate_deg_s: float = 0.0
    clamped: bool = False


def plan_downhand(
    normals,
    *,
    sense: int = STATION_SENSE,
    limits_deg: tuple[float, float] = (-180.0, 180.0),
    max_step_deg: float | None = None,
    move_seconds: float | None = None,
) -> list[StationMove]:


    out: list[StationMove] = []
    prev: float | None = None
    for i, n in enumerate(normals):
        ideal, _ = best_downhand_angle(n, sense=sense, limits_deg=limits_deg)
        theta = ideal
        clamped = False
        if prev is not None and max_step_deg is not None:
            lo, hi = prev - max_step_deg, prev + max_step_deg
            if not (lo <= theta <= hi):
                theta = float(np.clip(theta, lo, hi))
                clamped = True
        theta = float(np.clip(theta, limits_deg[0], limits_deg[1]))
        rate = 0.0
        if prev is not None and move_seconds:
            rate = abs(theta - prev) / float(move_seconds)
        out.append(
            StationMove(
                move_index=i,
                theta_deg=theta,
                downhand_error_deg=downhand_error_deg(n, theta, sense),
                rate_deg_s=rate,
                clamped=clamped,
            )
        )
        prev = theta
    return out


def speed_violations(plan: list[StationMove], *, limit_deg_s: float = MAX_SPEED_DEG_S) -> list[str]:

    return [
        f"move {m.move_index}: station {m.rate_deg_s:.1f} deg/s exceeds "
        f"{limit_deg_s:.0f} deg/s"
        for m in plan
        if m.rate_deg_s > limit_deg_s
    ]


@dataclass(frozen=True)
class AxisFit:


    origin: np.ndarray
    direction: np.ndarray
    radius_mm: float
    residual_mm: float
    sense: int | None = None
    sense_residual_deg: float | None = None

    def agrees_with_model(self, origin, direction, tol_mm: float = 25.0) -> bool:

        o = np.asarray(origin, float)
        d = np.asarray(direction, float)
        d = d / np.linalg.norm(d)
        if abs(float(np.dot(d, self.direction))) < 0.999:
            return False
        v = self.origin - o
        return float(np.linalg.norm(v - np.dot(v, d) * d)) <= tol_mm


def fit_station_axis(points, angles_deg=None) -> AxisFit:


    P = np.atleast_2d(np.asarray(points, float))
    if P.ndim != 2 or P.shape[1] != 3 or not np.isfinite(P).all():
        raise ValueError("touch points must be finite (N, 3) robot-frame millimetres")
    if len(P) < 3:
        raise ValueError("three touches of the same point are the minimum")


    centroid = P.mean(axis=0)
    _u, _s, vt = np.linalg.svd(P - centroid)
    if _s[1] <= max(1e-6, _s[0] * 1e-9):
        raise ValueError("touch points are repeated or collinear; station axis is not determined")
    normal = vt[2] / np.linalg.norm(vt[2])


    e1 = vt[0] / np.linalg.norm(vt[0])
    e2 = np.cross(normal, e1)
    uv = np.stack([(P - centroid) @ e1, (P - centroid) @ e2], axis=1)
    A = np.hstack([2.0 * uv, np.ones((len(uv), 1))])
    b = (uv ** 2).sum(axis=1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cu, cv = float(sol[0]), float(sol[1])
    radius = float(np.sqrt(max(0.0, sol[2] + cu * cu + cv * cv)))
    origin = centroid + cu * e1 + cv * e2

    radii = np.linalg.norm(P - origin - np.outer((P - origin) @ normal, normal), axis=1)


    plane_error = (P - origin) @ normal
    residual = float(np.max(np.hypot(radii - radius, plane_error)))

    sense = None
    sense_res = None
    if angles_deg is not None:
        angles = np.asarray(angles_deg, float)
        if angles.shape != (len(P),) or not np.isfinite(angles).all():
            raise ValueError("one finite station angle per touch point")

        rel = P - origin
        phi = np.degrees(np.arctan2(rel @ e2, rel @ e1))
        errs = {}
        for cand in (+1, -1):
            d_obs = np.diff(np.unwrap(np.radians(phi)))
            d_cmd = np.radians(np.diff(angles)) * cand
            errs[cand] = float(np.max(np.abs(np.degrees(d_obs - d_cmd))))
        sense = min(errs, key=errs.get)
        sense_res = errs[sense]


    return AxisFit(origin=origin, direction=normal, radius_mm=radius,
                   residual_mm=residual, sense=sense, sense_residual_deg=sense_res)


def pulses_to_degrees(pulses) -> list[float]:

    from .robots import ar1440

    p = list(pulses)
    if len(p) != 6:
        raise ValueError("six values, in pendant order S L U R B T")
    return [v / r for v, r in zip(p, ar1440.PULSES_PER_DEG)]


_PLATTER_CHOICES = ("gen2", "charles", "none")
_ALONG_AXIS_RPY = (-np.pi / 2.0, 0.0, 0.0)


def _platter_design():


    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "hardware" / "platter" / "platter.py"
    spec = importlib.util.spec_from_file_location("_waam_platter_design", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"gen-2 platter model not found: {path}")
    mod = importlib.util.module_from_spec(spec)


    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod.PlatterDesign()


def _solid(geometry: str, center_mm, rpy=(0.0, 0.0, 0.0)) -> str:

    cx, cy, cz = (float(v) / 1000.0 for v in center_mm)
    origin = (f'<origin xyz="{cx:.6f} {cy:.6f} {cz:.6f}" '
              f'rpy="{rpy[0]:.6f} {rpy[1]:.6f} {rpy[2]:.6f}"/>')
    return (
        f"<visual>{origin}<geometry>{geometry}</geometry>"
        '<material name="station_grey"><color rgba="0.55 0.55 0.58 1"/></material>'
        f"</visual><collision>{origin}<geometry>{geometry}</geometry></collision>"
    )


def _box_solid(bmin, bmax) -> str:
    lo, hi = np.asarray(bmin, float), np.asarray(bmax, float)
    size, center = hi - lo, (hi + lo) / 2.0
    geom = (f'<box size="{size[0] / 1000:.6f} {size[1] / 1000:.6f} '
            f'{size[2] / 1000:.6f}"/>')
    return _solid(geom, center)


def _disc_solid(diameter_mm: float, thickness_mm: float, near_face_mm: float) -> str:


    geom = (f'<cylinder radius="{diameter_mm / 2000.0:.6f}" '
            f'length="{thickness_mm / 1000.0:.6f}"/>')
    return _solid(geom, (0.0, near_face_mm + thickness_mm / 2.0, 0.0), _ALONG_AXIS_RPY)


def _disc_from_box(bmin, bmax) -> str:


    lo, hi = np.asarray(bmin, float), np.asarray(bmax, float)
    center = (hi + lo) / 2.0
    geom = (f'<cylinder radius="{(hi[0] - lo[0]) / 2000.0:.6f}" '
            f'length="{(hi[1] - lo[1]) / 1000.0:.6f}"/>')
    return _solid(geom, center, _ALONG_AXIS_RPY)


def urdf_fragment(
    parent_link: str = "base_link",
    *,
    sense: int = STATION_SENSE,
    platter: str = "gen2",
) -> str:


    if platter not in _PLATTER_CHOICES:
        raise ValueError(f"platter must be one of {_PLATTER_CHOICES}, got {platter!r}")
    s = int(sense)
    if s not in (1, -1):
        raise ValueError("sense must be +1 or -1")


    mount = np.array([cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y, cell_lc.AXIS_Z], float)
    boxes = {n: (mn, mx) for n, mn, mx in
             cell_lc.lc_scene(mount, training_block=False).boxes}

    base_geom = (_box_solid(*boxes["mh185_headstock"])
                 + _box_solid(*boxes["positioner_stand"]))
    faceplate_geom = _disc_from_box(*boxes["faceplate_168941-1"])
    if platter == "gen2":
        design = _platter_design()
        platter_geom = _disc_solid(design.diameter_mm, design.thickness_mm, 0.0)
    elif platter == "charles":
        platter_geom = (_box_solid(*boxes["charles_platter"])
                        + _box_solid(*boxes["platter_gusset"]))
    else:
        platter_geom = ""

    mx, my, mz = mount / 1000.0
    base, faceplate, plate = STATION_LINK_NAMES
    return "\n".join([
        "  <!-- MH185 headstock, control group S1: a SEPARATE BRANCH off",
        "       base_link, not a 7th arm joint. Nothing the arm does moves it.",
        f"       Axis = cell Y through x={cell_lc.FACEPLATE_X} z={cell_lc.AXIS_Z} mm,",
        "       from the R1+S1 ROBOT CALIBRATION swing. The rotation SENSE is",
        "       CONFIRMED +1 by the same C1/C2/C3 points and lives in <axis>",
        "       alone. -->",
        f'  <link name="{base}">{base_geom}</link>',
        '  <joint name="station_mount" type="fixed">'
        f'<parent link="{parent_link}"/><child link="{base}"/>'
        f'<origin xyz="{mx:.6f} {my:.6f} {mz:.6f}"/></joint>',
        f'  <link name="{faceplate}">{faceplate_geom}</link>',
        f'  <!-- "revolute" +-{SOFT_LIMIT_DEG:.0f} deg: the S1 soft limit read off',
        "       the controller's own CMOS image (2026-09-07), = +-(4096 x 161)",
        "       pulses: the same rows that close the 1831.8222 p/deg scale.",
        "       It was `continuous` while no document stated a limit; a tier-2",
        "       sweep could then pose past what the controller allows.",
        f"       effort = holding torque {HOLDING_TORQUE_NM:.0f} Nm; velocity =",
        f"       {MAX_SPEED_RPM} rpm = {MAX_SPEED_DEG_S:.1f} deg/s (Table 3-1). -->",
        f'  <joint name="{STATION_JOINT_NAME}" type="revolute">'
        f'<parent link="{base}"/><child link="{faceplate}"/>'
        f'<origin xyz="0 0 0"/><axis xyz="0 {s} 0"/>'
        f'<limit lower="{-np.radians(SOFT_LIMIT_DEG):.4f}" '
        f'upper="{np.radians(SOFT_LIMIT_DEG):.4f}" '
        f'effort="{HOLDING_TORQUE_NM:.1f}" '
        f'velocity="{np.radians(MAX_SPEED_DEG_S):.4f}"/></joint>',
        f'  <link name="{plate}">{platter_geom}</link>',
        '  <joint name="station_platter_fixed" type="fixed">'
        f'<parent link="{faceplate}"/><child link="{plate}"/>'
        '<origin xyz="0 0 0"/></joint>',
    ])
