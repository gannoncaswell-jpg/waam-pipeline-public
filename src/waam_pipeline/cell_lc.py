from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .clearance import LC_PART_FIXED, Scene
from .robots import ar1440
from .robots.ar1440 import AR1440_JOINTS, _rot


AXIS_Z = 120.2


FACEPLATE_X = 967.8


FACEPLATE_Y = -641.4
FLOOR_Z = -860.4
CHARLES_PLATE = (676.0, 414.3, 6.35)
FRAME_CENTER = np.array([450.0, -300.0])


CLEANER_MODEL = "Yaskawa_Reamer"
CLEANER_SOURCE_SHA256 = "ad57261b15b8a66f90a5926c7d6058ea383403856b39471a1575bb6249d950f1"
CLEANER_SOURCE_BORE = np.array([-305.09635, -761.98419, 1407.74585])
CLEANER_SOURCE_SPRAY = np.array([-305.09635, -682.70419, 1407.74585])
CLEANER_SOURCE_MOUNT = np.array([-428.19635, -761.98419, 1099.04590])


CLEANER_BORE_POINT = np.array([151.7, -620.1, 540.0])
CLEANER_YAW_DEG = 90.0
CLEANER_POST_SECTION = (100.0, 104.0)
CLEANER_BASE_SECTION = (400.0, 360.0)
CLEANER_CLEARANCE_PAD_MM = 5.0


PLATTER_FACE_Z_MODEL = 165.8


MAX_FACE_OFFSET_MM = 50.0

_CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "cell-calibration.json"


def _load_calibration() -> dict:

    if not _CALIBRATION_PATH.exists():
        return {}
    data = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    off = float(data.get("platter_face_offset_mm", 0.0))
    if not -MAX_FACE_OFFSET_MM <= off <= MAX_FACE_OFFSET_MM:
        raise ValueError(
            f"{_CALIBRATION_PATH}: platter_face_offset_mm={off} is outside "
            f"+-{MAX_FACE_OFFSET_MM} mm. Check the sign and units in the calibration."
        )
    return data


def _calibration_mtime_ns() -> int | None:
    try:
        return _CALIBRATION_PATH.stat().st_mtime_ns
    except OSError:
        return None


_CALIBRATION_MTIME_NS = _calibration_mtime_ns()

CALIBRATION = _load_calibration()


def calibration_stale_reason() -> str | None:


    now = _calibration_mtime_ns()
    if now == _CALIBRATION_MTIME_NS:
        return None
    if _CALIBRATION_MTIME_NS is None:
        what = f"{_CALIBRATION_PATH} appeared after this process imported cell_lc"
    elif now is None:
        what = f"{_CALIBRATION_PATH} was removed after this process loaded it"
    else:
        what = f"{_CALIBRATION_PATH} was written after this process loaded it"
    return (
        f"{what}. Every calibrated value in cell_lc (PLATTER_FACE_Z, the "
        "training block, DEFAULT_PART_ORIGIN) is frozen at import, so this "
        "job would be built from the OLD numbers and stamped with them. "
        "RESTART the process (or the server) and emit again."
    )
PLATTER_FACE_OFFSET_MM = float(CALIBRATION.get("platter_face_offset_mm", 0.0))
PLATTER_FACE_Z = PLATTER_FACE_Z_MODEL + PLATTER_FACE_OFFSET_MM


TRAINING_BLOCK_DEFAULT = (250.0, 150.0, 40.0)
TRAINING_BLOCK_ALONG_PLATE_DEFAULT_MM = 202.8


def _load_training_block() -> tuple[tuple[float, float, float], float, bool]:


    plate_l, plate_w, _ = CHARLES_PLATE
    fb = CALIBRATION.get("training_block") or {}
    dims = (
        float(fb.get("length_mm", TRAINING_BLOCK_DEFAULT[0])),
        float(fb.get("width_mm", TRAINING_BLOCK_DEFAULT[1])),
        float(fb.get("height_mm", TRAINING_BLOCK_DEFAULT[2])),
    )
    along = float(fb.get("along_plate_mm", TRAINING_BLOCK_ALONG_PLATE_DEFAULT_MM))
    for name, got, limit in (
        ("length_mm", dims[0], plate_l), ("width_mm", dims[1], plate_w),
        ("height_mm", dims[2], 500.0),
    ):
        if not 0.0 < got <= limit:
            raise ValueError(
                f"{_CALIBRATION_PATH}: training_block.{name}={got} is not in "
                f"0..{limit} mm. Enter the measured dimension in millimetres."
            )
    if not 0.0 <= along <= plate_l:
        raise ValueError(
            f"{_CALIBRATION_PATH}: training_block.along_plate_mm={along} is "
            f"off the {plate_l} mm plate. This is the distance in MILLIMETRES "
            "from the faceplate plane to the block centre; it was a fraction "
            "of the plate length before 2026-09-04."
        )


    on = fb.get("on_platter", True)
    if not isinstance(on, bool):
        raise ValueError(
            f"{_CALIBRATION_PATH}: training_block.on_platter={on!r} must be "
            "true or false (a JSON boolean), nothing else."
        )
    return dims, along, on


TRAINING_BLOCK, TRAINING_BLOCK_ALONG_PLATE_MM, TRAINING_BLOCK_ON_PLATTER = (
    _load_training_block()
)
GUSSET_DIA = 320.0
GUSSET_THK = 9.5

DEFAULT_PART_ORIGIN = np.array([1014.0, -300.0, PLATTER_FACE_Z])


YASKAWA_FLANGE_TO_OURS = np.array([[0.0, 0.0, 1.0], [0.0, -1.0, 0.0], [1.0, 0.0, 0.0]])


TOOL_TCP = YASKAWA_FLANGE_TO_OURS @ np.array([-110.247, 0.324, 351.972])


TOOL_MASS_KG = 3.800
TOOL_COG_YASKAWA = np.array([-5.0, 0.0, 101.0])


TOOL_INTERFERE_CAPSULES = (
    ((0.0, 0.0, 0.0), (0.0, 0.0, 154.0), 50.0),
    ((0.0, 0.0, 154.0), (0.0, 0.0, 261.0), 20.0),
    ((0.0, 0.0, 261.0), (-90.0, 0.0, 351.0), 20.0),
)
FLANGE_PITCH_DEG = 45.0


FEEDER_U_ARM_ROTATION = np.diag([-1.0, -1.0, 1.0])
FEEDER_U_ARM_MOUNTS = {
    "upper": np.array([-225.8125, 61.73, 47.15]),
    "lower": np.array([-225.8125, 73.93, 80.13]),
}
FEEDER_MOUNT_HOLE_SET = "lower"
FEEDER_U_ARM_TRANSLATION = FEEDER_U_ARM_MOUNTS[FEEDER_MOUNT_HOLE_SET]


def feeder_points_in_u(points) -> np.ndarray:

    return np.asarray(points, float) @ FEEDER_U_ARM_ROTATION.T + FEEDER_U_ARM_TRANSLATION


SPOOL_S_BASE_TRANSLATION = np.array([-330.0, 0.0, 480.0])
ARM_SPOOL_DIA = 200.0
ARM_SPOOL_WIDTH = 55.0
ARM_SPOOL_BORE = 35.2
ARM_SPOOL_FLANGE_THK = 4.3
ARM_SPOOL_BARREL_DIA = 109.4
ARM_SPOOL_WOUND_DIA = 191.5


def tcp_from_pulses(pulses) -> np.ndarray:


    q = [float(p) / r for p, r in zip(pulses, ar1440.PULSES_PER_DEG)]
    if len(q) != len(ar1440.PULSES_PER_DEG):
        raise ValueError(f"expected {len(ar1440.PULSES_PER_DEG)} pulse values, got {len(q)}")
    f = ar1440.fk(q)["flange"]
    return f[:3, 3] + f[:3, :3] @ TOOL_TCP


def offset_from_touch_off(
    pulses,
    *,
    substrate_mm: float = 0.0,
    stickout_correction_mm: float = 0.0,
    touch_stickout_mm: float | None = None,
    weld_stickout_mm: float | None = None,
) -> float:


    if touch_stickout_mm is not None or weld_stickout_mm is not None:
        if stickout_correction_mm:
            raise ValueError(
                "give stickout_correction_mm OR the touch/weld pair, not both"
            )
        stickout_correction_mm = (
            float(touch_stickout_mm or 0.0) - float(weld_stickout_mm or 0.0)
        )
    z = float(tcp_from_pulses(pulses)[2])
    return z - stickout_correction_mm - substrate_mm - PLATTER_FACE_Z_MODEL


MIN_TOUCH_SPREAD_MM = 40.0


MAX_SURFACE_TILT_DEG = 5.0


class NoFixtureRecorded(ValueError):
    pass


@dataclass(frozen=True)
class WorkSurface:


    name: str
    points: np.ndarray
    centroid: np.ndarray
    normal: np.ndarray
    residual_mm: float
    tilt_deg: float

    def z_at(self, x: float, y: float) -> float:

        n, c = self.normal, self.centroid
        if abs(n[2]) < 1e-9:
            raise ValueError(f"{self.name}: surface is vertical, no z(x, y)")
        return float(c[2] - (n[0] * (x - c[0]) + n[1] * (y - c[1])) / n[2])

    def deviation_over(self, xy: np.ndarray) -> float:


        zs = [self.z_at(float(x), float(y)) for x, y in np.asarray(xy, float)]
        return float(max(zs) - min(zs)) if zs else 0.0

    def summary(self) -> str:
        return (
            f"{self.name}: {len(self.points)} touches, "
            f"z {self.z_at(float(self.centroid[0]), float(self.centroid[1])):.2f} mm "
            f"at the centroid, tilt {self.tilt_deg:.3f} deg, "
            f"flatness residual {self.residual_mm:.3f} mm"
        )


def fit_work_surface(
    name: str,
    touch_pulses,
    *,
    substrate_mm: float = 0.0,
    stickout_correction_mm: float = 0.0,
) -> WorkSurface:


    pts = np.array([tcp_from_pulses(p) for p in touch_pulses], float)
    if len(pts) < 3:
        raise ValueError(
            f"{name}: a plane needs at least 3 touch points, got {len(pts)}. "
            "One point gives height only and cannot see tilt."
        )
    pts[:, 2] -= stickout_correction_mm + substrate_mm

    centroid = pts.mean(axis=0)
    centred = pts - centroid
    _, sv, vh = np.linalg.svd(centred)


    spread = float(sv[1]) / np.sqrt(len(pts))
    if spread < MIN_TOUCH_SPREAD_MM:
        raise ValueError(
            f"{name}: touch points span only ~{spread:.1f} mm across their "
            f"narrow axis (need {MIN_TOUCH_SPREAD_MM:.0f}). They are nearly "
            "collinear, so the fitted tilt is meaningless. Spread them out "
            "over the working area."
        )

    normal = vh[-1]
    if normal[2] < 0:
        normal = -normal
    normal = normal / np.linalg.norm(normal)

    residual = float(np.abs(centred @ normal).max())
    tilt = float(np.degrees(np.arccos(np.clip(normal[2], -1.0, 1.0))))
    if tilt > MAX_SURFACE_TILT_DEG:
        raise ValueError(
            f"{name}: fitted tilt {tilt:.2f} deg exceeds "
            f"{MAX_SURFACE_TILT_DEG} deg. Either the positioner is not flat, "
            "a touch was taken on something other than the work surface, or a "
            "pulse record was mistyped. Re-take the points."
        )
    return WorkSurface(name, pts, centroid, normal, residual, tilt)


def lowest_weld_z(toolpath) -> float:


    lows = [p[2] for p in toolpath.deposit_points()]
    if not lows:
        raise ValueError("toolpath has no deposit moves to seat on the work surface")
    return float(min(lows))


def snap_toolpath_to_surface(
    toolpath,
    x: float,
    y: float,
    surface: WorkSurface,
    *,
    substrate_mm: float = 0.0,
    gap_mm: float = 0.0,
    max_deviation_mm: float = 0.5,
) -> np.ndarray:


    if not np.isfinite(gap_mm) or gap_mm < 0.0:
        raise ValueError(f"gap_mm must be finite and >= 0, got {gap_mm}")
    reference = lowest_weld_z(toolpath)

    xy = np.array([[m.x + x, m.y + y] for m in toolpath.moves], float)
    deviation = surface.deviation_over(xy)
    if deviation > max_deviation_mm:
        raise ValueError(
            f"{surface.name}: the measured surface varies {deviation:.2f} mm "
            f"across this job's footprint (limit {max_deviation_mm} mm) because "
            f"it is tilted {surface.tilt_deg:.2f} deg. A flat job frame cannot "
            "follow it. Shim the fixture level, move the job to a smaller "
            "footprint, or raise max_deviation_mm if that error is acceptable "
            "against your layer height."
        )

    zc = surface.z_at(float(xy[:, 0].mean()), float(xy[:, 1].mean()))
    if substrate_mm < 0.0:
        raise ValueError(f"substrate_mm is a thickness, got {substrate_mm}")
    return np.array([x, y, zc + substrate_mm + gap_mm - reference], float)


def load_fixture(name: str | None = None) -> WorkSurface:


    fixtures = CALIBRATION.get("fixtures") or {}
    if not fixtures:
        raise NoFixtureRecorded(
            f"{_CALIBRATION_PATH}: no 'fixtures' recorded. Record at least three "
            "work-surface measurements using the calibration file instructions."
        )
    key = name or CALIBRATION.get("active_fixture")
    if key is None:
        raise NoFixtureRecorded(
            f"{_CALIBRATION_PATH}: no 'active_fixture' set; pick one of "
            f"{sorted(fixtures)}"
        )
    if key not in fixtures:
        raise ValueError(f"unknown fixture {key!r}; have {sorted(fixtures)}")
    f = {k: v for k, v in fixtures[key].items() if not k.startswith("_")}
    return fit_work_surface(
        key,
        f["touch_pulses"],
        substrate_mm=float(f.get("substrate_mm", 0.0)),
        stickout_correction_mm=float(f.get("stickout_correction_mm", 0.0)),
    )


def platter_work_surface_z(substrate_mm: float = 0.0) -> float:


    if substrate_mm < 0.0:
        raise ValueError(f"substrate_mm is a thickness, got {substrate_mm}")
    return PLATTER_FACE_Z + substrate_mm


def snap_origin_to_platter(
    x: float,
    y: float,
    *,
    substrate_mm: float = 0.0,
    first_deposit_z_mm: float = 0.0,
    gap_mm: float = 0.0,
) -> np.ndarray:


    return np.array(
        [x, y, platter_work_surface_z(substrate_mm) + gap_mm - first_deposit_z_mm],
        float,
    )


def snap_toolpath_to_platter(
    toolpath,
    x: float,
    y: float,
    *,
    substrate_mm: float = 0.0,
    gap_mm: float = 0.0,
) -> np.ndarray:


    if not np.isfinite(gap_mm) or gap_mm < 0.0:
        raise ValueError(f"gap_mm must be finite and >= 0, got {gap_mm}")
    return snap_origin_to_platter(
        x, y,
        substrate_mm=substrate_mm,
        first_deposit_z_mm=lowest_weld_z(toolpath),
        gap_mm=gap_mm,
    )


def snap_toolpath_to_platter_pose(
    toolpath,
    x: float,
    y: float,
    *,
    station_deg: float = 0.0,
    station_sense: int | None = None,
    substrate_mm: float = 0.0,
    gap_mm: float = 0.0,
) -> np.ndarray:


    from . import station

    chosen = {} if station_sense is None else {"sense": station_sense}
    pose = station._platter_transform(station_deg, **chosen)
    origin = snap_toolpath_to_platter(
        toolpath, x, y, substrate_mm=substrate_mm, gap_mm=gap_mm,
    )
    if not np.isfinite(origin).all():
        raise ValueError("platter seating origin must be finite")
    pose[:3, 3] += pose[:3, :3] @ origin
    return pose


LC_CELL_PROFILE = Path(__file__).resolve().parents[2] / "examples" / "cell_lc50s.json"


def arc_file(profile: str | Path | None = None) -> int:


    from . import frames

    path = Path(profile) if profile is not None else LC_CELL_PROFILE
    try:
        cfg = frames.load_cell_config(path)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(
            f"{path}: no usable cell profile, so there is no recorded ASF# to "
            f"emit with ({exc}). Fix the profile -- a fallback number would "
            "weld somebody else's schedule."
        ) from exc
    if not isinstance(cfg, frames.CellConfig) or str(cfg.robot) != "ar1440":
        raise ValueError(
            f"{path}: {getattr(cfg, 'name', '?')!r} is not the AR1440 cell "
            f"(robot {getattr(cfg, 'robot', '?')!r}), so its ASF# is not this "
            "cell's ASF#."
        )
    n = int(cfg.arc_file)

    if profile is None:
        from . import readiness

        seen, note = readiness.configured_arc_file()
        if seen is not None and seen != n:
            raise ValueError(
                f"ASF# disagreement: {path.name} says {n}, but the preflight "
                f"reports {seen} from {note}. One of them is what a human read "
                "and the other is what the robot would weld with. Reconcile "
                "them before emitting."
            )
    return n


def seat_job(
    toolpath,
    x: float,
    y: float,
    *,
    substrate_mm: float = 0.0,
    gap_mm: float = 0.0,
    fixture: str | None = None,
) -> tuple[np.ndarray, "WorkSurface | None"]:


    try:
        surface = load_fixture(fixture)
    except NoFixtureRecorded:
        origin = snap_toolpath_to_platter(
            toolpath, x, y, substrate_mm=substrate_mm, gap_mm=gap_mm)
        return origin, None
    origin = snap_toolpath_to_surface(
        toolpath, x, y, surface, substrate_mm=substrate_mm, gap_mm=gap_mm)
    return origin, surface


def calibration_summary() -> tuple[str, ...]:

    src = "docs/cell-calibration.json" if CALIBRATION else "UNCALIBRATED (model only)"
    return (
        f"face z {PLATTER_FACE_Z:.1f} = {PLATTER_FACE_Z_MODEL:.1f}"
        f"{PLATTER_FACE_OFFSET_MM:+.1f}",
        f"cal {src}",
    )


def lc_scene(
    part_origin_robot: np.ndarray | None = None,
    *,
    training_block: bool | None = None,
    station_deg: float = 0.0,
    station_sense: int | None = None,
) -> Scene:


    from . import station

    chosen = {} if station_sense is None else {"sense": station_sense}
    pose = station._platter_transform(station_deg, **chosen)
    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    sc = Scene()
    cm = _cell_models()

    def box(name, mn, mx):
        lo, hi = np.asarray(mn, float), np.asarray(mx, float)
        if station_deg != 0.0 and name in LC_PART_FIXED:
            center = pose[:3, :3] @ ((lo + hi) / 2) + pose[:3, 3]
            half = np.abs(pose[:3, :3]) @ ((hi - lo) / 2)
            lo, hi = center - half, center + half
        sc.boxes.append((name, lo - o, hi - o))

    L, W, T = CHARLES_PLATE
    fx, fy = FACEPLATE_X, FACEPLATE_Y

    box("charles_platter", (fx - W / 2, fy, PLATTER_FACE_Z - T),
        (fx + W / 2, fy + L, PLATTER_FACE_Z))


    if training_block is None:
        training_block = TRAINING_BLOCK_ON_PLATTER
    if training_block:
        fb_l, fb_w, fb_h = TRAINING_BLOCK
        fb_along = TRAINING_BLOCK_ALONG_PLATE_MM
        box("manpro_training_block",
            (fx - fb_w / 2, fy + fb_along - fb_l / 2, PLATTER_FACE_Z),
            (fx + fb_w / 2, fy + fb_along + fb_l / 2, PLATTER_FACE_Z + fb_h))

    box("platter_gusset",
        (fx - GUSSET_DIA / 2, fy, PLATTER_FACE_Z - T - GUSSET_DIA / 2),
        (fx + GUSSET_DIA / 2, fy + GUSSET_THK, PLATTER_FACE_Z - T))
    box("mh185_headstock", (fx - 170, fy - 360, AXIS_Z - 200),
        (fx + 170, fy - 20, AXIS_Z + 200))

    stand = np.concatenate([v for k, v in cm.mh185_headstock_parts().items()
                            if k.startswith("stand_")]).reshape(-1, 3)
    stand = stand @ np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]).T
    stand += [fx, fy, FLOOR_Z]
    box("positioner_stand", stand.min(0), stand.max(0))
    box("robot_pedestal", (-180, -180, FLOOR_Z), (180, 180, 0.0))

    px, py, pt = cm.PEDESTAL_TOP_PLATE
    box("robot_pedestal_top_plate", (-px / 2, -py / 2, -pt - cm.PEDESTAL_GUSSET_DROP),
        (px / 2, py / 2, 0.0))
    foot_x, foot_y, foot_t = cm.PEDESTAL_FOOT_PLATE
    box("robot_pedestal_foot", (-foot_x / 2, -foot_y / 2, FLOOR_Z),
        (foot_x / 2, foot_y / 2, FLOOR_Z + foot_t + 13))


    for name, tris in cm.cell_base_parts(FRAME_CENTER, FACEPLATE_X).items():
        lo, hi = cm.bbox(tris)
        box("cell_base_" + name, lo + [0, 0, FLOOR_Z], hi + [0, 0, FLOOR_Z])
    box("floor", (-1100, -2300, FLOOR_Z - 20), (1500, 700, FLOOR_Z))
    cleaner = cleaner_mesh()
    if cleaner is None:

        box("reamer_yair_185091", (-1, -787, FLOOR_Z), (281, -533, 500.0))
    else:

        box("reamer_yair_185091",
            cleaner.min(axis=(0, 1)) - CLEANER_CLEARANCE_PAD_MM,
            cleaner.max(axis=(0, 1)) + CLEANER_CLEARANCE_PAD_MM)
        for name, support in cleaner_support_meshes().items():
            box(name.replace("lc_cleaner_", "reamer_"),
                support.min(axis=(0, 1)) - CLEANER_CLEARANCE_PAD_MM,
                support.max(axis=(0, 1)) + CLEANER_CLEARANCE_PAD_MM)


    box("faceplate_168941-1", (fx - 160, fy - 12, AXIS_Z - 160),
        (fx + 160, fy, AXIS_Z + 160))
    return sc


def _flange_pose(tcp_robot: np.ndarray) -> np.ndarray:

    bearing = float(np.degrees(np.arctan2(tcp_robot[1], tcp_robot[0])))
    R = _rot((0, 0, 1), bearing) @ _rot((0, 1, 0), FLANGE_PITCH_DEG)
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = tcp_robot - R @ TOOL_TCP
    return T


def pulse_solver(part_origin_robot: np.ndarray | None = None):


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    state: dict = {"q": None}

    def solve(part_xyz) -> list[float]:
        tcp = np.asarray(part_xyz, float) + o


        sols = ar1440.ik(_flange_pose(tcp), all_solutions=True)
        if not sols:
            raise ValueError(f"unreachable TCP (robot frame): {np.round(tcp, 1)}")
        if state["q"] is None:
            bearing = float(np.degrees(np.arctan2(tcp[1], tcp[0])))
            state["q"] = [bearing, 40.0, 10.0, 0.0, 50.0, 0.0]
        q = min(sols, key=lambda s: sum(abs(a - b) for a, b in zip(s, state["q"])))
        state["q"] = q
        return q

    return solve


def robot_shadow(toolpath, part_origin_robot: np.ndarray | None = None, *, include_meshes=True) -> dict:


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    skeletons: list[list[list[float]] | None] = []
    joints: list[list[float] | None] = []
    misses = 0
    solve = pulse_solver(o)
    for m in toolpath.moves:
        tcp = np.array([m.x, m.y, m.z]) + o
        try:
            q = solve((m.x, m.y, m.z))
        except ValueError:
            skeletons.append(None)
            joints.append(None)
            misses += 1
            continue
        f = ar1440.fk(q)
        pts_robot = [
            np.array([0.0, 0.0, FLOOR_Z]),
            np.zeros(3),
            f["joint_2_l"][:3, 3],
            f["joint_3_u"][:3, 3],
            f["joint_4_r"][:3, 3],
            f["joint_5_b"][:3, 3],
            f["flange"][:3, 3],
            tcp,
        ]
        skeletons.append([[round(float(c), 1) for c in (p - o)] for p in pts_robot])
        joints.append([round(float(a), 2) for a in q])
    return {
        "skeleton": skeletons,
        "joints": joints,
        "unreachable": misses,
        "chain": [
            {"origin": list(j.origin), "axis": list(j.axis)} for j in AR1440_JOINTS
        ],
        "base_offset": [float(-v) for v in o],
        "meshes": _decimated_link_meshes() if include_meshes else None,
        "conduits": wire_conduit_specs(),
        "tool_display": "YAIR 45° torch: shock clutch, no wire brake. Gold marker = calibrated TCP. Nozzle dimensions pending.",
        "note": "axis 967.8 fwd (R1+S1 swing cal 2026-09-08, was tape 1114.4) / faceplate 641.4 right; platter row +100 ASSUMED",
    }


@lru_cache(maxsize=1)
def _decimated_link_meshes() -> dict | None:


    from pathlib import Path

    from .robots.ar1440 import _MESH_LINKS
    from .slicer import load_stl

    root = Path(__file__).resolve().parents[2] / "robots" / "meshes" / "ar1440"
    paths = {}
    for folder, suffix in (("visual", ""), ("decimated", "_dec")):
        candidates = {
            link: root / folder / name.replace(".stl", f"{suffix}.stl")
            for link, (name, _) in _MESH_LINKS.items()
        }
        if all(path.is_file() for path in candidates.values()):
            paths = candidates
            break
    if not paths:
        return None
    out = {}
    for link, (name, off) in _MESH_LINKS.items():
        tris = load_stl(paths[link]) * 1000.0
        flat = [round(float(v), 1) for v in tris.reshape(-1)]
        groups: list[dict] = []
        if link == "link_3_u":


            cmm = _cell_models()
            appearances = cmm.autodrive_4r100_appearances()
            for part_name, part_tris in cmm.autodrive_4r100_parts().items():
                start = len(flat) // 9
                placed = feeder_points_in_u(part_tris)


                flat += [round(float(v), 5) for v in placed.reshape(-1)]
                groups.append({
                    "start": start,
                    "end": len(flat) // 9,
                    "kind": "bracket" if part_name == "k3572_bracket" else "feeder",
                    "part": part_name,
                    **({"appearance": appearances[part_name]}
                       if part_name in appearances else {}),
                })
        if link == "link_1_s":


            flat = [round(float(v), 1) for v in
                    (tris + np.asarray(off) * 1000.0).reshape(-1)]
            cmm = _cell_models()
            sp = cmm.wire_spool(
                flange_dia=ARM_SPOOL_DIA,
                width=ARM_SPOOL_WIDTH,
                bore=ARM_SPOOL_BORE,
                flange_thk=ARM_SPOOL_FLANGE_THK,
                barrel_dia=ARM_SPOOL_BARREL_DIA,
                wound_dia=ARM_SPOOL_WOUND_DIA,
            )
            rz = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
            sp = sp.reshape(-1, 3) @ rz.T + SPOOL_S_BASE_TRANSLATION
            start = len(flat) // 9
            flat += [round(float(v), 1) for v in sp.reshape(-1)]
            groups.append({"start": start, "end": len(flat) // 9, "kind": "spool"})
            for part_name, part in cmm.spool_housing_parts().items():
                mounted = part.reshape(-1, 3) @ rz.T + SPOOL_S_BASE_TRANSLATION
                start = len(flat) // 9
                flat += [round(float(v), 1) for v in mounted.reshape(-1)]
                kind = "spool_hub" if part_name in ("spindle", "retainer") else "spool_housing"
                groups.append({"start": start, "end": len(flat) // 9, "kind": kind})
            for part in cmm.spool_support_parts(SPOOL_S_BASE_TRANSLATION).values():
                start = len(flat) // 9
                flat += [round(float(v), 1) for v in part.reshape(-1)]
                groups.append({"start": start, "end": len(flat) // 9, "kind": "spool_support"})
        if link == "link_6_t":


            cmm = _cell_models()
            appearances = cmm.yaskawa_torch_appearances()
            for part_name, part_tris in cmm.yaskawa_torch_parts().items():
                start = len(flat) // 9


                flat.extend(part_tris.reshape(-1).tolist())
                groups.append({"start": start, "end": len(flat) // 9,
                               "kind": "torch", "part": part_name,
                               **({"appearance": appearances[part_name]}
                                  if part_name in appearances else {})})
            start = len(flat) // 9

            marker = _cell_models().box((2.0, 2.0, 2.0), center=TOOL_TCP)
            flat += [round(float(v), 3) for v in marker.reshape(-1)]
            groups.append({"start": start, "end": len(flat) // 9, "kind": "tcp"})
            out[link] = {"tris": flat, "offset": [0.0, 0.0, 0.0], "extra": "torch"}


            disc = tris + np.array([off]) * 1000.0
            n_disc = disc.reshape(-1)
            flat[: len(n_disc)] = [round(float(v), 1) for v in n_disc]
        else:
            out[link] = {"tris": flat, "offset": ([0.0, 0.0, 0.0] if link == "link_1_s"
                                                 else [o_ * 1000.0 for o_ in off])}
            if link == "link_3_u":
                out[link]["extra"] = (
                    "feeder: K3572-1 bracket seated on rear U-arm pads; "
                    f"{FEEDER_MOUNT_HOLE_SET} staggered hole pairs owner-confirmed; "
                    "K3560-1 supplied CAD; nominal body fit; installed slide/yaw unconfirmed"
                )
            if link == "link_1_s":
                out[link]["extra"] = (
                    "spool housing/support: S-axis rotation user-confirmed; "
                    "housing, support and hub registration reconstructed"
                )
        if groups:
            out[link]["groups"] = groups
    return out


def wire_conduit_specs() -> list[dict]:


    cm = _cell_models()
    start = SPOOL_S_BASE_TRANSLATION + [0., 28., cm.SPOOL_HOUSING_DIA / 2 + 23.]
    inlet = cm.k3560_1_metadata()["inlet"]
    end_local = cm.k3560_points_in_feeder(inlet["point_native_mm"])


    approach_local = cm.k3560_points_in_feeder(
        np.asarray(inlet["point_native_mm"]) + 180 * np.asarray(inlet["outward_axis_native"]))
    end = feeder_points_in_u(end_local)
    approach = feeder_points_in_u(approach_local)
    return [{"name": "wire_supply", "radius": cm.WIRE_CONDUIT_RADIUS, "samples": 24,
             "controls": [{"link": 1, "point": start.tolist()},
                          {"link": 1, "point": (start + [-120., 0., 240.]).tolist()},
                          {"link": 3, "point": approach.tolist()},
                          {"link": 3, "point": end.tolist()}]}]


def wire_conduit_points(q_deg, spec=None) -> np.ndarray:

    from waam_pipeline.robots import ar1440
    spec = wire_conduit_specs()[0] if spec is None else spec
    fk = ar1440.fk(q_deg)
    frames = [np.eye(4)] + [fk[j.name] for j in AR1440_JOINTS]
    pts = np.array([(frames[c["link"]] @ np.r_[c["point"], 1.])[:3]
                    for c in spec["controls"]])
    t = np.linspace(0, 1, spec["samples"] + 1)[:, None]
    return (1-t)**3 * pts[0] + 3*(1-t)**2*t * pts[1] + 3*(1-t)*t**2 * pts[2] + t**3 * pts[3]


def wire_conduit_mesh(q_deg) -> np.ndarray:
    spec = wire_conduit_specs()[0]
    pts = wire_conduit_points(q_deg, spec)
    return np.concatenate([_tube(a, b, spec["radius"], spec["radius"], n=8)
                           for a, b in zip(pts, pts[1:])])


def _tube(p0, p1, r0, r1, n=12):

    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    a = p1 - p0
    a = a / np.linalg.norm(a)
    ref = np.array([0.0, 0.0, 1.0]) if abs(a[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(a, ref); u /= np.linalg.norm(u)
    v = np.cross(a, u)
    tris = []
    for i in range(n):
        t0, t1 = 2 * np.pi * i / n, 2 * np.pi * (i + 1) / n
        d0 = np.cos(t0) * u + np.sin(t0) * v
        d1 = np.cos(t1) * u + np.sin(t1) * v
        a0, a1 = p0 + r0 * d0, p0 + r0 * d1
        b0, b1 = p1 + r1 * d0, p1 + r1 * d1
        tris.append([a0, a1, b0])
        tris.append([a1, b1, b0])
    return np.array(tris)


def torch_mesh() -> np.ndarray:


    parts = []
    for a, b, radius in TOOL_INTERFERE_CAPSULES:

        p0, p1 = YASKAWA_FLANGE_TO_OURS @ np.asarray(a), YASKAWA_FLANGE_TO_OURS @ np.asarray(b)
        axis = (p1 - p0) / np.linalg.norm(p1 - p0)
        ref = np.array([0., 1., 0.])
        v = np.cross(axis, ref)
        v /= np.linalg.norm(v)
        w = np.cross(axis, v)
        theta = np.arange(16) * (2 * np.pi / 16)
        radial = np.cos(theta)[:, None] * v + np.sin(theta)[:, None] * w
        rings = []
        for center, angles in ((p0, np.linspace(-np.pi / 2, 0, 5)),
                               (p1, np.linspace(0, np.pi / 2, 5))):
            for angle in angles:
                rings.append(center + radius * np.sin(angle) * axis
                             + radius * np.cos(angle) * radial)
        faces = []
        for r0, r1 in zip(rings, rings[1:]):
            for i in range(16):
                j = (i + 1) % 16
                faces.extend(((r0[i], r0[j], r1[j]), (r0[i], r1[j], r1[i])))
        tris = np.asarray(faces)
        areas = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0],
                                        tris[:, 2] - tris[:, 0]), axis=1)
        parts.append(tris[areas > 1e-8])
    return np.concatenate(parts)


def _cell_models():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "hardware" / "cell_models"
    spec = importlib.util.spec_from_file_location("cell_models", root / "cell_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lc_scene_placements() -> dict:


    cm = _cell_models()

    R = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    rack_xy = np.array([
        FRAME_CENTER[0] - cm.CELL_W / 2 + cm.RACK_DEPTH / 2 + 70.0,
        FRAME_CENTER[1] + cm.CELL_L / 2 - cm.CELL_SYSTEM_WIDTH + cm.RACK_WIDTH / 2,
    ])
    return {
        "mh185_headstock": (R, np.array([FACEPLATE_X, FACEPLATE_Y, FLOOR_Z])),
        "charles_platter": (R, np.array([FACEPLATE_X, FACEPLATE_Y, PLATTER_FACE_Z])),

        "reamer_station": (np.eye(3), np.array([140.0, -660.0, FLOOR_Z])),
        "robot_pedestal": (np.eye(3), np.array([0.0, 0.0, FLOOR_Z])),
        "cell_base": (np.eye(3), np.array([0.0, 0.0, FLOOR_Z])),


        "fume_hood": (np.eye(3), np.array([700.0, -450.0, 1239.6])),
        "cell_frame_v2": (R, np.r_[FRAME_CENTER, FLOOR_Z]),
        "equipment_rack": (np.eye(3), np.r_[rack_xy, FLOOR_Z]),
        "yrc1000_cabinet": (np.eye(3), np.r_[rack_xy, FLOOR_Z + cm.RACK_CONTROLLER_DECK_Z]),
        "lincoln_r450": (np.eye(3), np.r_[rack_xy, FLOOR_Z + cm.RACK_LOWER_DECK_Z]),
        "power_disconnect": (np.eye(3), np.r_[rack_xy + [cm.RACK_DEPTH / 2 + cm.DISCONNECT_BODY[0] / 2, 0.0], FLOOR_Z + 150.0]),
    }


def _cleaner_head_native() -> np.ndarray | None:

    from . import cell_hc

    try:
        triangles = cell_hc._furniture().get(CLEANER_MODEL)
        manifest = json.loads(cell_hc.MESH_PATH.with_name("aw_hc_import.json").read_text())
        asset = manifest["assets"][CLEANER_MODEL]
        shells = asset["shells"]
        counts = [s["display_triangles"] for s in shells]


        if (triangles is None or asset["sha256"] != CLEANER_SOURCE_SHA256
                or [s["shell"] for s in shells] != list(range(25))
                or sum(counts) != len(triangles) or len(triangles) == 0):
            return None
        start = sum(counts[:2])
        end = start + counts[2]
        return np.concatenate((triangles[:start], triangles[end:])).astype(float)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _place_cleaner(points: np.ndarray, origin: np.ndarray | None = None) -> np.ndarray:

    rotation = _rot((0, 0, 1), CLEANER_YAW_DEG)
    o = np.zeros(3) if origin is None else np.asarray(origin, float)
    return (np.asarray(points, float) - CLEANER_SOURCE_BORE) @ rotation.T + CLEANER_BORE_POINT - o


def cleaner_mesh(origin: np.ndarray | None = None) -> np.ndarray | None:

    triangles = _cleaner_head_native()
    return None if triangles is None else np.round(_place_cleaner(triangles, origin), 1)


def cleaner_support_meshes(origin: np.ndarray | None = None) -> dict[str, np.ndarray]:


    cm = _cell_models()
    x, y, mount_z = CLEANER_SOURCE_MOUNT
    foot_z = mount_z - cm.REAMER_STAND_H
    floor_z = FLOOR_Z - CLEANER_BORE_POINT[2] + CLEANER_SOURCE_BORE[2]
    plate_t = cm.REAMER_BASE[2]


    plate_xy = (cm.REAMER_HEAD_BASE[1], cm.REAMER_HEAD_BASE[0])
    def slab(size_xy, bottom, top):
        return cm.box((*size_xy, top - bottom), (x, y, (bottom + top) / 2))
    supports = {
        "lc_cleaner_base": slab(CLEANER_BASE_SECTION, floor_z, foot_z),
        "lc_cleaner_foot": slab(plate_xy, foot_z, foot_z + plate_t),
        "lc_cleaner_post": slab(CLEANER_POST_SECTION, foot_z + plate_t, mount_z - plate_t),
        "lc_cleaner_mount": slab(plate_xy, mount_z - plate_t, mount_z),
    }
    return {name: np.round(_place_cleaner(tris, origin), 1) for name, tris in supports.items()}


def lc_scene_meshes(part_origin_robot: np.ndarray | None = None, *,
                    training_block: bool | None = None) -> dict | None:


    o = DEFAULT_PART_ORIGIN if part_origin_robot is None else np.asarray(part_origin_robot, float)
    cm = _cell_models()
    placements = lc_scene_placements()
    cleaner = cleaner_mesh(o)
    def place(tris, rot, pos):
        pts = np.asarray(tris, float).reshape(-1, 3) @ rot.T + pos - o
        return [round(float(v), 1) for v in pts.reshape(-1)]

    out = {}
    for name, (rot, pos) in placements.items():
        if name == "reamer_station" and cleaner is not None:
            continue
        if name == "cell_base":

            out[name] = place(cm.cell_deck(), rot, pos)
            continue
        if name == "charles_platter":
            out[name] = place(cm.charles_platter(include_fixture=False), rot, pos)
            present = TRAINING_BLOCK_ON_PLATTER if training_block is None else training_block
            if present:


                block = next(b for b in lc_scene(o, training_block=True).boxes
                             if b[0] == "manpro_training_block")
                lo, hi = block[1], block[2]
                rect = np.array([[lo[0], lo[1]], [hi[0], lo[1]],
                                 [hi[0], hi[1]], [lo[0], hi[1]]])
                out[block[0]] = place(cm.prism(rect, z0=lo[2], z1=hi[2]),
                                     np.eye(3), o)
            continue


        if name == "cell_frame_v2":
            parts = cm.cell_frame_v2_parts()
            frame = [v for k, v in parts.items() if "panel" not in k]


            panels = [v for k, v in parts.items() if "panel" in k and "lower" in k]
            walls = [v for k, v in parts.items() if "panel" in k and "lower" not in k]
            if frame:
                out["cell_frame_v2"] = place(np.concatenate(frame), rot, pos)
            if walls:
                out["cell_walls"] = place(np.concatenate(walls), rot, pos)
            if panels:
                out["cell_panels"] = place(np.concatenate(panels), rot, pos)
            continue
        out[name] = place(cm.GENERATORS[name](), rot, pos)
    if cleaner is not None:
        out["lc_cleaner"] = cleaner.ravel().tolist()
        out.update({name: tris.ravel().tolist() for name, tris in cleaner_support_meshes(o).items()})
    return out
