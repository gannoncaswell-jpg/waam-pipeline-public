from __future__ import annotations

import json
from pathlib import Path

import numpy as np


FACEPLATE_DIA = 320.0
FACEPLATE_THK = 20.0


FACEPLATE_BOLT_SQUARE = 200.0
FACEPLATE_DOWEL_SPACING = 160.0
TOOLING_DIA = 490.0
AXIS_HEIGHT_ABOVE_BASE = 923.0


HEADSTOCK_LENGTH = 273.0


HOUSING_WIDTH = 300.0


MOTOSIM_AXIS_HEIGHT = 821.5
MOTOSIM_COLUMN_TOP = 655.0
AXIS_ABOVE_FOOT = MOTOSIM_AXIS_HEIGHT - MOTOSIM_COLUMN_TOP


HOUSING_TOP_ABOVE_AXIS = 168.0


MOTOR_DIA = 90.0


MOTOR_OFFSET_Z = 0.0


STAND_COLUMN = 200.0
STAND_BASE_ANCHORS = (300.0, 250.0)
STAND_DECK_Z = 138.0

GEAR_RATIO = 161
RATED_INERTIA_KGM2 = 47.0
HOLDING_TORQUE_NM = 268.0


PLATTER_L = 676.0
PLATTER_W = 414.3
PLATTER_T = 6.35
PLATTER_CORNER_R = 50.0
PLATTER_HOLE_DIA = 19.05
PLATTER_HOLE_SPACING = 203.2
GUSSET_DIA = 320.0


GUSSET_THK = 9.5
TRAINING_BLOCK = (250.0, 150.0, 40.0)


REAMER_STAND_H = 914.4

REAMER_HEAD = (281.9, 254.0, 315.0)

REAMER_POST = 175.0
REAMER_BASE = (243.2, 178.2, 15.0)


REAMER_HEAD_BASE = (243.2, 178.2, 15.0)


PEDESTAL_H = 860.4
PEDESTAL_SECTION = 360.0
PEDESTAL_TOP_PLATE = (420.0, 420.0, 20.0)
PEDESTAL_FOOT_PLATE = (460.0, 460.0, 20.0)
PEDESTAL_GUSSET_DROP = 120.0
PEDESTAL_GUSSET_THK = 10.0


BASE_RAIL_SECTION = (100.0, 80.0)
BASE_RAIL_BOTTOM = 18.0
BASE_ANCHOR_PITCH = 115.0
BASE_ANCHOR_HOLE_DIA = 12.6
BASE_ANCHOR_PAD = (65.0, 155.0, 10.0)


SPOOL_HOUSING_DIA = 240.0
SPOOL_HOUSING_WIDTH = 100.0
SPOOL_HOUSING_WALL = 5.0
SPOOL_SUPPORT_MOUNT = (-115.0, -26.0, 180.0)
WIRE_CONDUIT_RADIUS = 9.0


CELL_SYSTEM_WIDTH = 3123.0
CELL_FLOOR_LAYOUT = (2000.0, 2000.0)


CELL_L = 2140.0
CELL_W = 2140.0

FRAME_MEMBER = 45.0


OPENING_SILL = 1000.0
OPENING_HEIGHT = 1161.0
OPENING_MAX_WIDTH = 1779.0
CELL_HEIGHT = 2277.0


PANEL_THK = 6.0


DOOR_BAY_INSET = 0.0


FEEDER_BODY = (231.0, 191.0, 213.0)


FEEDER_MASS_KG = 6.0


K3572_NATIVE_BBOX_MIN = (-119.90578, -38.1, -130.31216)
K3572_NATIVE_BBOX_MAX = (117.24894, 263.525, 2.65684)
K3572_NATIVE_XY_CENTER = (-1.32842, 112.7125)
K3572_BRACKET_ENVELOPE = (301.625, 237.15472, 132.969)
K3572_TOP_SHEET_THK = 2.65684
K3572_BRACKET_DROP = 130.31216
K3572_STEP_SHA256 = "33E0DA1E95825324F587D5984AACFB9CEA9C699C3AB315C4376FA88D3B7416BD"


SPOOL_DIA = 300.0


SPOOL_WIDTH = 103.0
SPOOL_BORE = 51.5

SPOOL_FLANGE_THK = 8.0
SPOOL_BARREL_DIA = 160.0
SPOOL_WOUND_DIA = 280.0


HOOD_MOUTH_DIA = 300.0


HOOD_CONE_H = 250.0
HOOD_DUCT_DIA = 100.0
HOOD_DUCT_LEN = 250.0
HOOD_LIP = 20.0


YRC1000_BODY = (427.0, 598.0, 499.0)


YRC1000_OVER_DEPTH = 542.0


YRC1000_MASS_KG = 70.0
YRC1000_PLINTH = 100.0


YRC1000_EYE = (60.0, 60.0, 45.0)


R450_CASE = (355.0, 630.0, 571.0)


R450_MASS_KG = 68.0
R450_PANEL_RELIEF = 25.0
R450_SKID = 40.0


RACK_DEPTH = 800.0
RACK_WIDTH = 800.0
RACK_MEMBER = 40.0
RACK_SHELF_THK = 12.0
RACK_LOWER_DECK_Z = 60.0
RACK_CONTROLLER_DECK_Z = 850.0
DISCONNECT_BODY = (180.0, 380.0, 500.0)


WAAM_FIXTURE_SIZE = (508.0, 254.0, 50.8)
WAAM_FIXTURE_SOURCE_SHA256 = "04d13819c58d83fdb035daec5c381a34213e2a7343c0c64cee9f888f0f5f9356"


STEEL_DENSITY = 7850.0


def _quad(a, b, c, d) -> list[list]:

    return [[a, b, c], [a, c, d]]


def box_bounds(lo, hi) -> np.ndarray:

    x0, y0, z0 = (float(v) for v in lo)
    x1, y1, z1 = (float(v) for v in hi)
    v000, v100 = [x0, y0, z0], [x1, y0, z0]
    v110, v010 = [x1, y1, z0], [x0, y1, z0]
    v001, v101 = [x0, y0, z1], [x1, y0, z1]
    v111, v011 = [x1, y1, z1], [x0, y1, z1]
    tris: list[list] = []
    tris += _quad(v000, v010, v110, v100)
    tris += _quad(v001, v101, v111, v011)
    tris += _quad(v000, v100, v101, v001)
    tris += _quad(v010, v011, v111, v110)
    tris += _quad(v000, v001, v011, v010)
    tris += _quad(v100, v110, v111, v101)
    return np.array(tris, dtype=float)


def box(size, center=(0.0, 0.0, 0.0)) -> np.ndarray:

    s = np.asarray(size, float) / 2.0
    c = np.asarray(center, float)
    return box_bounds(c - s, c + s)


def cylinder(radius: float, height: float, base=(0.0, 0.0, 0.0),
             segments: int = 32, axis: str = "z") -> np.ndarray:

    ang = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
    ring = np.stack([radius * np.cos(ang), radius * np.sin(ang)], axis=1)
    tris: list[list] = []
    for i in range(segments):
        (ax, ay), (bx, by) = ring[i], ring[(i + 1) % segments]
        lo_a, lo_b = [ax, ay, 0.0], [bx, by, 0.0]
        hi_a, hi_b = [ax, ay, height], [bx, by, height]
        tris += _quad(lo_a, lo_b, hi_b, hi_a)
        tris.append([[0.0, 0.0, 0.0], lo_b, lo_a])
        tris.append([[0.0, 0.0, height], hi_a, hi_b])
    out = np.array(tris, dtype=float)
    return place(out, rotate=_axis_rotation(axis), translate=base)


def frustum(radius_lo: float, radius_hi: float, height: float,
            base=(0.0, 0.0, 0.0), segments: int = 24,
            axis: str = "z") -> np.ndarray:


    if radius_lo <= 0.0 or radius_hi <= 0.0:
        raise ValueError("frustum radii must both be positive")
    ang = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
    unit = np.stack([np.cos(ang), np.sin(ang)], axis=1)
    lo, hi = radius_lo * unit, radius_hi * unit
    tris: list[list] = []
    for i in range(segments):
        j = (i + 1) % segments
        lo_a, lo_b = [*lo[i], 0.0], [*lo[j], 0.0]
        hi_a, hi_b = [*hi[i], height], [*hi[j], height]
        tris += _quad(lo_a, lo_b, hi_b, hi_a)
        tris.append([[0.0, 0.0, 0.0], lo_b, lo_a])
        tris.append([[0.0, 0.0, height], hi_a, hi_b])
    out = np.array(tris, dtype=float)
    return place(out, rotate=_axis_rotation(axis), translate=base)


def _axis_rotation(axis: str) -> np.ndarray:

    if axis == "z":
        return np.eye(3)
    if axis == "x":
        return np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    if axis == "y":
        return np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])
    raise ValueError(f"axis must be x, y or z, got {axis!r}")


def place(triangles: np.ndarray, rotate: np.ndarray | None = None,
          translate=(0.0, 0.0, 0.0)) -> np.ndarray:

    out = np.asarray(triangles, float)
    if rotate is not None:
        r = np.asarray(rotate, float)
        if abs(np.linalg.det(r) - 1.0) > 1e-9:
            raise ValueError("rotate must be a proper rotation (det +1)")
        out = out @ r.T
    return out + np.asarray(translate, float)


def rounded_rect(length: float, width: float, radius: float,
                 arc_segments: int = 6) -> np.ndarray:

    hx, hy = length / 2.0, width / 2.0
    r = min(radius, hx, hy)
    centers = [(hx - r, hy - r), (-hx + r, hy - r),
               (-hx + r, -hy + r), (hx - r, -hy + r)]
    starts = [0.0, np.pi / 2, np.pi, 1.5 * np.pi]
    pts: list[list[float]] = []
    for (cx, cy), a0 in zip(centers, starts):
        for a in np.linspace(a0, a0 + np.pi / 2, arc_segments + 1):
            pts.append([cx + r * np.cos(a), cy + r * np.sin(a)])

    keep = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - keep[-1][0]) > 1e-9 or abs(p[1] - keep[-1][1]) > 1e-9:
            keep.append(p)
    return np.array(keep, dtype=float)


def circle_loop(cx: float, cy: float, radius: float, segments: int = 16,
                clockwise: bool = False) -> np.ndarray:
    ang = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
    if clockwise:
        ang = ang[::-1]
    return np.stack([cx + radius * np.cos(ang), cy + radius * np.sin(ang)], axis=1)


def half_disc(radius: float, segments: int = 24) -> np.ndarray:

    ang = np.linspace(np.pi, 2 * np.pi, segments + 1)
    pts = np.stack([radius * np.cos(ang), radius * np.sin(ang)], axis=1)
    return pts


def _signed_area(loop: np.ndarray) -> float:
    x, y = loop[:, 0], loop[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _orient(loop: np.ndarray, ccw: bool) -> np.ndarray:
    return loop if (_signed_area(loop) > 0) == ccw else loop[::-1].copy()


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _proper_crossing(a, b, c, d, eps: float = 1e-9) -> bool:

    d1, d2 = _cross(a, b, c), _cross(a, b, d)
    d3, d4 = _cross(c, d, a), _cross(c, d, b)
    return (d1 * d2 < -eps) and (d3 * d4 < -eps)


def _point_in_loop(p, loop: np.ndarray) -> bool:
    inside = False
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        if (y0 > p[1]) != (y1 > p[1]):
            xx = x0 + (p[1] - y0) * (x1 - x0) / (y1 - y0)
            if xx > p[0]:
                inside = not inside
    return inside


def _bridge(poly: list, hole: list, others: list[np.ndarray]) -> list:

    best = None
    for i, p in enumerate(poly):
        for j, h in enumerate(hole):
            d = float(np.hypot(p[0] - h[0], p[1] - h[1]))
            if best is not None and d >= best[0]:
                continue
            if _bridge_blocked(p, h, poly, hole, others):
                continue
            best = (d, i, j)
    if best is None:
        raise ValueError("no visible bridge from outer loop to hole")
    _, i, j = best
    return list(poly[:i + 1]) + list(hole[j:]) + list(hole[:j + 1]) + list(poly[i:])


def _bridge_blocked(p, h, poly: list, hole: list, others: list[np.ndarray]) -> bool:
    loops = [np.array(poly, dtype=float), np.array(hole, dtype=float)] + list(others)
    for loop in loops:
        n = len(loop)
        for k in range(n):
            if _proper_crossing(p, h, loop[k], loop[(k + 1) % n]):
                return True
    mid = ((p[0] + h[0]) / 2.0, (p[1] + h[1]) / 2.0)
    if not _point_in_loop(mid, np.array(poly, dtype=float)):
        return True
    return any(_point_in_loop(mid, loop) for loop in [np.array(hole, dtype=float)] + list(others))


def _ear_clip(poly: np.ndarray) -> list[tuple[int, int, int]]:

    idx = list(range(len(poly)))
    tris: list[tuple[int, int, int]] = []
    while len(idx) > 3:
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = poly[i0], poly[i1], poly[i2]
            if _cross(a, b, c) <= 1e-9:
                continue
            blocked = False
            for j in idx:
                if j in (i0, i1, i2):
                    continue
                p = poly[j]
                if (_cross(a, b, p) > 1e-9 and _cross(b, c, p) > 1e-9
                        and _cross(c, a, p) > 1e-9):
                    blocked = True
                    break
            if blocked:
                continue
            tris.append((i0, i1, i2))
            idx.pop(k)
            break
        else:
            raise ValueError("ear clipping failed — polygon is not simple")
    tris.append((idx[0], idx[1], idx[2]))
    return tris


def _refine_cap(cap: list, max_edge: float) -> list:


    out: list = []
    todo = list(cap)
    while todo:
        a, b, c = todo.pop()
        lengths = (np.hypot(*(b - a)), np.hypot(*(c - b)), np.hypot(*(a - c)))
        worst = int(np.argmax(lengths))
        if lengths[worst] <= max_edge:
            out.append((a, b, c))
        elif worst == 0:
            m = (a + b) / 2.0
            todo += [(a, m, c), (m, b, c)]
        elif worst == 1:
            m = (b + c) / 2.0
            todo += [(a, b, m), (a, m, c)]
        else:
            m = (c + a) / 2.0
            todo += [(a, b, m), (b, c, m)]
    return out


def prism(outer, holes=(), z0: float = 0.0, z1: float = 1.0,
          cap_max_edge: float | None = None) -> np.ndarray:


    outer = _orient(np.asarray(outer, float), ccw=True)
    holes = [_orient(np.asarray(h, float), ccw=False) for h in holes]
    merged = list(outer)
    for n, hole in enumerate(holes):
        merged = _bridge(merged, list(hole), holes[n + 1:])
    flat = np.array(merged, dtype=float)
    cap = [(flat[i0], flat[i1], flat[i2]) for i0, i1, i2 in _ear_clip(flat)]
    if cap_max_edge is not None:
        cap = _refine_cap(cap, cap_max_edge)
    tris: list[list] = []
    for a, b, c in cap:
        tris.append([[a[0], a[1], z0], [c[0], c[1], z0], [b[0], b[1], z0]])
        tris.append([[a[0], a[1], z1], [b[0], b[1], z1], [c[0], c[1], z1]])
    for loop in [outer] + holes:
        n = len(loop)
        for i in range(n):
            a, b = loop[i], loop[(i + 1) % n]


            steps = 1 if cap_max_edge is None else max(
                1, int(np.ceil(float(np.hypot(*(b - a))) / cap_max_edge)))
            for k in range(steps):
                p = a + (b - a) * (k / steps)
                q = a + (b - a) * ((k + 1) / steps)
                tris += _quad([p[0], p[1], z0], [q[0], q[1], z0],
                              [q[0], q[1], z1], [p[0], p[1], z1])
    return np.array(tris, dtype=float)


def mh185_headstock_parts(
    axis_height: float = AXIS_HEIGHT_ABOVE_BASE,
    include_stand: bool = True,
    segments: int = 32,
) -> dict[str, np.ndarray]:


    z = float(axis_height)
    foot_z = z - AXIS_ABOVE_FOOT
    parts: dict[str, np.ndarray] = {}

    parts["faceplate"] = cylinder(FACEPLATE_DIA / 2, FACEPLATE_THK,
                                  base=(-FACEPLATE_THK, 0.0, z),
                                  segments=segments, axis="x")

    parts["front_hub"] = cylinder(120.0, 40.0 - FACEPLATE_THK,
                                  base=(-40.0, 0.0, z), segments=segments, axis="x")


    parts["casting"] = box_bounds((-175.0, -HOUSING_WIDTH / 2, foot_z),
                                  (-40.0, HOUSING_WIDTH / 2, z + HOUSING_TOP_ABOVE_AXIS))

    parts["foot_flange"] = box_bounds((-200.0, -170.0, foot_z),
                                      (-20.0, 170.0, foot_z + 25.0))


    parts["motor_adapter"] = box_bounds((-230.0, -70.0, z + MOTOR_OFFSET_Z - 70.0),
                                        (-175.0, 70.0, z + MOTOR_OFFSET_Z + 70.0))
    parts["motor"] = cylinder(MOTOR_DIA / 2, HEADSTOCK_LENGTH - 230.0,
                              base=(-HEADSTOCK_LENGTH, 0.0, z + MOTOR_OFFSET_Z),
                              segments=segments, axis="x")
    if include_stand:


        parts["stand_top_plate"] = box_bounds((-250.0, -130.0, foot_z - 21.0),
                                              (-10.0, 130.0, foot_z))
        parts["stand_column"] = box_bounds((-230.0, -STAND_COLUMN / 2, STAND_DECK_Z),
                                           (-30.0, STAND_COLUMN / 2, foot_z - 21.0))
        bx, by = STAND_BASE_ANCHORS[0] / 2 + 30.0, STAND_BASE_ANCHORS[1] / 2 + 30.0
        parts["stand_base_plate"] = box_bounds((-130.0 - bx, -by, STAND_DECK_Z - 20.0),
                                               (-130.0 + bx, by, STAND_DECK_Z))
        for i, x in enumerate((-130.0 - STAND_BASE_ANCHORS[0] / 2,
                               -130.0 + STAND_BASE_ANCHORS[0] / 2)):
            for j, y in enumerate((-STAND_BASE_ANCHORS[1] / 2, STAND_BASE_ANCHORS[1] / 2)):
                parts[f"stand_fastener_{i}_{j}"] = _display_fastener((x, y, STAND_DECK_Z))
    return parts


_MHT185_HSF_MESH = Path(__file__).resolve().parent / "meshes" / "mht185_hsf.npy"


def mh185_headstock(**kwargs) -> np.ndarray:


    if not kwargs and _MHT185_HSF_MESH.exists():
        hsf = np.load(_MHT185_HSF_MESH).astype(float)
        parts = mh185_headstock_parts()
        stand = [v for k, v in parts.items() if k.startswith("stand_")]
        return np.concatenate([hsf] + stand)
    return np.concatenate(list(mh185_headstock_parts(**kwargs).values()))


def faceplate_hole_pattern() -> dict[str, list[tuple[float, float]]]:


    h = FACEPLATE_BOLT_SQUARE / 2
    d = FACEPLATE_DOWEL_SPACING / 2
    return {
        "m12": [(h, 0.0), (-h, 0.0), (0.0, h), (0.0, -h)],
        "dowel": [(d, 0.0), (-d, 0.0)],
    }


def charles_platter_parts(
    length: float = PLATTER_L,
    width: float = PLATTER_W,
    thickness: float = PLATTER_T,
    corner_radius: float = PLATTER_CORNER_R,
    hole_dia: float = PLATTER_HOLE_DIA,
    hole_spacing: float = PLATTER_HOLE_SPACING,
    hole_x: float | None = None,
    include_holes: bool = True,
    include_gusset: bool = True,
    include_fixture: bool = True,
    segments: int = 16,
    cap_max_edge: float = 100.0,
) -> dict[str, np.ndarray]:


    parts: dict[str, np.ndarray] = {}
    hx = length / 2.0 if hole_x is None else float(hole_x)
    outer = rounded_rect(length, width, corner_radius)
    outer = outer + np.array([length / 2.0, 0.0])
    holes = []
    if include_holes:
        holes = [circle_loop(hx, s * hole_spacing / 2, hole_dia / 2, segments,
                             clockwise=True) for s in (+1, -1)]
    parts["plate"] = prism(outer, holes, z0=-thickness, z1=0.0,
                           cap_max_edge=cap_max_edge)
    if include_gusset:


        loop = half_disc(GUSSET_DIA / 2, segments=24)
        local = prism(loop, z0=0.0, z1=GUSSET_THK, cap_max_edge=cap_max_edge)


        spin = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        parts["gusset"] = place(local, rotate=spin, translate=(0.0, 0.0, -thickness))
    if include_fixture:
        fx, fy, fz = TRAINING_BLOCK


        cx = 0.3 * length
        rect = np.array([[cx - fx / 2, -fy / 2], [cx + fx / 2, -fy / 2],
                         [cx + fx / 2, fy / 2], [cx - fx / 2, fy / 2]])
        parts["training_block"] = prism(rect, z0=0.0, z1=fz,
                                       cap_max_edge=cap_max_edge)
    return parts


def charles_platter(**kwargs) -> np.ndarray:
    return np.concatenate(list(charles_platter_parts(**kwargs).values()))


def platter_mass_kg(length: float = PLATTER_L, width: float = PLATTER_W,
                    thickness: float = PLATTER_T) -> float:

    return length * width * thickness * 1e-9 * STEEL_DENSITY


def reamer_station_parts(
    stand_height: float = REAMER_STAND_H,
    head=REAMER_HEAD,
    post: float = REAMER_POST,
    base=REAMER_BASE,
    head_base=REAMER_HEAD_BASE,
) -> dict[str, np.ndarray]:


    bx, by, bz = base
    hx, hy, hz = head
    mx, my, mt = head_base
    if not 0 < mt < hz:
        raise ValueError("head mounting plate must fit within the head envelope")
    return {
        "base_plate": box((bx, by, bz), center=(0.0, 0.0, bz / 2)),
        "post": box_bounds((-post / 2, -post / 2, bz), (post / 2, post / 2, stand_height)),
        "head_mount_plate": box_bounds((-mx / 2, -my / 2, stand_height),
                                        (mx / 2, my / 2, stand_height + mt)),
        "head": box_bounds((-hx / 2, -hy / 2, stand_height + mt),
                            (hx / 2, hy / 2, stand_height + hz)),
    }


def reamer_station(**kwargs) -> np.ndarray:
    return np.concatenate(list(reamer_station_parts(**kwargs).values()))


def _display_fastener(base) -> np.ndarray:

    x, y, z = base
    return np.concatenate([cylinder(15.0, 3.0, base=(x, y, z), segments=12),
                           cylinder(12.0, 10.0, base=(x, y, z + 3), segments=6)])


def _triangular_web(run, drop, thickness) -> np.ndarray:

    tri = prism(np.array([[0., -drop], [run, 0.], [0., 0.]]),
                z0=-thickness / 2, z1=thickness / 2)
    return place(tri, rotate=np.array([[1., 0., 0.], [0., 0., -1.], [0., 1., 0.]]))


def robot_pedestal_parts(
    height: float = PEDESTAL_H,
    section: float = PEDESTAL_SECTION,
    top_plate=PEDESTAL_TOP_PLATE,
) -> dict[str, np.ndarray]:

    px, py, pz = top_plate
    s = section / 2.0
    fx, fy, ft = PEDESTAL_FOOT_PLATE
    parts = {
        "column": box_bounds((-s, -s, ft), (s, s, height - pz)),
        "top_plate": box((px, py, pz), center=(0.0, 0.0, height - pz / 2)),
        "foot_plate": box((fx, fy, ft), center=(0.0, 0.0, ft / 2)),
    }
    for i, angle in enumerate((0, np.pi / 2, np.pi, 3 * np.pi / 2)):
        c, sn = np.cos(angle), np.sin(angle)
        rot = np.array([[c, -sn, 0.], [sn, c, 0.], [0., 0., 1.]])
        run = (px if i % 2 == 0 else py) / 2 - s
        if run > 0:
            parts[f"gusset_{i}"] = place(_triangular_web(run, PEDESTAL_GUSSET_DROP,
                                                         PEDESTAL_GUSSET_THK),
                                         rotate=rot, translate=(s * c, s * sn, height - pz))
    for i, x in enumerate((-fx / 2 + 30, fx / 2 - 30)):
        for j, y in enumerate((-fy / 2 + 30, fy / 2 - 30)):
            parts[f"foot_fastener_{i}_{j}"] = _display_fastener((x, y, ft))
    return parts


def robot_pedestal(**kwargs) -> np.ndarray:
    return np.concatenate(list(robot_pedestal_parts(**kwargs).values()))


def cell_base_parts(center=(450., -300.), station_x=967.8) -> dict[str, np.ndarray]:


    cx, cy = center
    w, h = BASE_RAIL_SECTION
    x0, x1 = cx - CELL_W / 2 + w / 2, cx + CELL_W / 2 - w / 2
    y0, y1 = cy - CELL_L / 2 + w / 2, cy + CELL_L / 2 - w / 2
    z = BASE_RAIL_BOTTOM
    parts = {
        "rear_rail": box_bounds((x0, y0, z), (x0 + w, y1, z + h)),
        "front_rail": box_bounds((x1 - w, y0, z), (x1, y1, z + h)),
        "left_rail": box_bounds((x0 + w, y0, z), (x1 - w, y0 + w, z + h)),
        "right_rail": box_bounds((x0 + w, y1 - w, z), (x1 - w, y1, z + h)),
        "station_crossmember": box_bounds((station_x - 100, y0 + w, z),
                                            (station_x + 100, y1 - w, STAND_DECK_Z - 20)),
    }


    pad_locations = [(x, y) for x in (x0 + w / 2, x1 - w / 2)
                     for y in (y0 + w / 2, cy, y1 - w / 2)]
    pad_locations += [(cx, y0 + w / 2), (cx, y1 - w / 2)]
    px, py, pt = BASE_ANCHOR_PAD
    outer = np.array([[-px / 2, -py / 2], [px / 2, -py / 2],
                      [px / 2, py / 2], [-px / 2, py / 2]])
    holes = [circle_loop(0, sy * BASE_ANCHOR_PITCH / 2,
                         BASE_ANCHOR_HOLE_DIA / 2, 12, clockwise=True) for sy in (-1, 1)]
    pad = prism(outer, holes, z0=0, z1=pt)
    for i, (x, y) in enumerate(pad_locations):

        parts[f"anchor_{i}"] = np.concatenate([place(pad, translate=(x, y, 0)),
            cylinder(18, z - pt, base=(x, y, pt), segments=12)])
    return parts


def cell_base() -> np.ndarray:
    return np.concatenate(list(cell_base_parts().values()))


DECK_TOP_Z = 6 * 25.4
DECK_BOTTOM_Z = 3 * 25.4
DECK_END_LEN = 14 * 25.4
DECK_MID_LEN = 45.75 * 25.4
DECK_END_W = 49 * 25.4
DECK_MID_W = 46 * 25.4
DECK_NOTCH = (7.75 * 25.4, 3 * 25.4)


DECK_BACK_X = -PEDESTAL_SECTION / 2


def cell_deck_parts() -> dict[str, np.ndarray]:


    z0, z1, x0 = DECK_BOTTOM_Z, DECK_TOP_Z, DECK_BACK_X
    half_mid = DECK_MID_LEN / 2
    nw, nd = DECK_NOTCH
    parts = {
        f"deck_end_{side}": box_bounds(
            (x0, min(s * half_mid, s * (half_mid + DECK_END_LEN)), z0),
            (x0 + DECK_END_W, max(s * half_mid, s * (half_mid + DECK_END_LEN)), z1))
        for side, s in (("minus_y", -1), ("plus_y", 1))
    }
    parts["deck_middle"] = box_bounds((x0 + nd, -half_mid, z0), (x0 + DECK_MID_W, half_mid, z1))
    for side, s in (("minus_y", -1), ("plus_y", 1)):
        parts[f"deck_middle_edge_{side}"] = box_bounds(
            (x0, min(s * nw / 2, s * half_mid), z0), (x0 + nd, max(s * nw / 2, s * half_mid), z1))
    return parts


def cell_deck() -> np.ndarray:
    return np.concatenate(list(cell_deck_parts().values()))


def cell_frame_parts(
    length: float = CELL_L,
    width: float = CELL_W,
    member: float = FRAME_MEMBER,
    sill: float = OPENING_SILL,
    opening_height: float = OPENING_HEIGHT,
    max_bay: float = OPENING_MAX_WIDTH,
    height: float = CELL_HEIGHT,
) -> dict[str, np.ndarray]:


    m = member
    top = height
    hx, hy = length / 2.0 - m / 2.0, width / 2.0 - m / 2.0

    def _axis_positions(span: float, half: float) -> list[float]:
        bays = max(1, int(np.ceil((span - m) / (max_bay + m))))
        return [-half + i * (2 * half) / bays for i in range(bays + 1)]

    xs, ys = _axis_positions(length, hx), _axis_positions(width, hy)
    parts: dict[str, np.ndarray] = {}
    posts = [(x, y) for x in xs for y in (ys[0], ys[-1])]
    posts += [(x, y) for y in ys[1:-1] for x in (xs[0], xs[-1])]
    for n, (x, y) in enumerate(sorted(set(posts))):
        parts[f"post_{n}"] = box_bounds((x - m / 2, y - m / 2, 0.0),
                                        (x + m / 2, y + m / 2, top))
    levels = [(0.0, m), (sill - m, sill),
              (sill + opening_height, sill + opening_height + m)]
    if top > sill + opening_height + m:
        levels.append((top - m, top))
    for n, (z0, z1) in enumerate(levels):
        for side, y in (("front", ys[0]), ("back", ys[-1])):
            parts[f"rail_{side}_{n}"] = box_bounds((-hx - m / 2, y - m / 2, z0),
                                                   (hx + m / 2, y + m / 2, z1))
        for side, x in (("left", xs[0]), ("right", xs[-1])):
            parts[f"rail_{side}_{n}"] = box_bounds((x - m / 2, -hy + m / 2, z0),
                                                   (x + m / 2, hy - m / 2, z1))
    return parts


def cell_frame(**kwargs) -> np.ndarray:
    return np.concatenate(list(cell_frame_parts(**kwargs).values()))


def cell_frame_v2_parts(
    length: float = CELL_L,
    width: float = CELL_W,
    member: float = FRAME_MEMBER,
    sill: float = OPENING_SILL,
    opening_height: float = OPENING_HEIGHT,
    door_width: float = OPENING_MAX_WIDTH,
    height: float = CELL_HEIGHT,
    panel: float = PANEL_THK,
    door_offset: float = DOOR_BAY_INSET,
) -> dict[str, np.ndarray]:


    m, t = member, panel
    hx, hy = length / 2.0 - m / 2.0, width / 2.0 - m / 2.0
    head = sill + opening_height
    parts: dict[str, np.ndarray] = {}


    jamb = (door_offset - door_width / 2.0 - m / 2.0,
            door_offset + door_width / 2.0 + m / 2.0)

    def _split(a: float, b: float) -> list[float]:
        span = b - a
        n = max(1, int(np.ceil((span - m) / (OPENING_MAX_WIDTH + m))))
        return [a + i * span / n for i in range(n + 1)]


    fixed = sorted({-hx, hx, *jamb})
    front_xs = [x for a, b in zip(fixed, fixed[1:]) for x in _split(a, b)[:-1]]
    front_xs.append(hx)
    back_xs = _split(-hx, hx)
    side_ys = _split(-hy, hy)

    post_xy = {(x, -hy) for x in front_xs} | {(x, hy) for x in back_xs}
    post_xy |= {(x, y) for y in side_ys[1:-1] for x in (-hx, hx)}
    for n, (x, y) in enumerate(sorted(post_xy)):
        parts[f"post_{n}"] = box_bounds((x - m / 2, y - m / 2, 0.0),
                                        (x + m / 2, y + m / 2, height))


    levels = [(0.0, m), (sill - m, sill), (head, head + m)]
    if height - m > head + m + 1e-6:
        levels.append((height - m, height))
    for n, (z0, z1) in enumerate(levels):
        for side, y in (("front", -hy), ("back", hy)):
            parts[f"rail_{side}_{n}"] = box_bounds((-hx - m / 2, y - m / 2, z0),
                                                   (hx + m / 2, y + m / 2, z1))
        for side, x in (("left", -hx), ("right", hx)):
            parts[f"rail_{side}_{n}"] = box_bounds((x - m / 2, -hy + m / 2, z0),
                                                   (x + m / 2, hy - m / 2, z1))


    def _panel(name, u0, u1, z0, z1, along_x: bool, v: float):
        if u1 - u0 < 1.0 or z1 - z0 < 1.0:
            return
        lo = (u0, v - t / 2, z0) if along_x else (v - t / 2, u0, z0)
        hi = (u1, v + t / 2, z1) if along_x else (v + t / 2, u1, z1)
        parts[name] = box_bounds(lo, hi)

    bands = (("lower", m, sill - m), ("upper", sill, head),
             ("header", head + m, height - m))
    for label, z0, z1 in bands:


        for n, (a, b) in enumerate(zip(front_xs, front_xs[1:])):
            if label == "upper" and jamb[0] - 1e-6 <= a and b <= jamb[1] + 1e-6:
                continue
            _panel(f"panel_front_{label}_{n}", a + m / 2, b - m / 2, z0, z1,
                   True, -hy)
        for n, (a, b) in enumerate(zip(back_xs, back_xs[1:])):
            _panel(f"panel_back_{label}_{n}", a + m / 2, b - m / 2, z0, z1,
                   True, hy)
        for n, (a, b) in enumerate(zip(side_ys, side_ys[1:])):
            _panel(f"panel_left_{label}_{n}", a + m / 2, b - m / 2, z0, z1,
                   False, -hx)
            _panel(f"panel_right_{label}_{n}", a + m / 2, b - m / 2, z0, z1,
                   False, hx)
    return parts


def cell_frame_v2(**kwargs) -> np.ndarray:
    return np.concatenate(list(cell_frame_v2_parts(**kwargs).values()))


_K3572_STEP_MESH = Path(__file__).resolve().parent / "meshes" / "k3572_1_step.npy"
_K3560_STEP_PARTS = Path(__file__).resolve().parent / "meshes" / "k3560_1_step.npz"
_K3560_STEP_META = Path(__file__).resolve().parent / "meshes" / "k3560_1_step.json"


def k3572_1_bracket() -> np.ndarray:


    if not _K3572_STEP_MESH.exists():
        raise FileNotFoundError(
            f"{_K3572_STEP_MESH} missing — run import_k3572_step.py"
        )
    native = np.load(_K3572_STEP_MESH).astype(float)
    local = np.empty_like(native)
    center_x, center_y = K3572_NATIVE_XY_CENTER
    local[..., 0] = native[..., 1] - center_y
    local[..., 1] = -(native[..., 0] - center_x)
    local[..., 2] = native[..., 2]
    return local


def k3560_1_metadata() -> dict:

    return json.loads(_K3560_STEP_META.read_text(encoding="utf-8"))


def k3560_points_in_feeder(points) -> np.ndarray:

    mount = k3560_1_metadata()["registration"]
    return (np.asarray(points, float) @ np.asarray(mount["rotation"]).T
            + np.asarray(mount["translation_mm"]))


def autodrive_4r100_parts() -> dict[str, np.ndarray]:


    if not _K3560_STEP_PARTS.exists():
        raise FileNotFoundError(f"{_K3560_STEP_PARTS} missing — run import_k3560_step.py")
    parts = {"k3572_bracket": k3572_1_bracket()}
    metadata = k3560_1_metadata()
    rotation = np.asarray(metadata["registration"]["rotation"])
    translation = np.asarray(metadata["registration"]["translation_mm"])
    with np.load(_K3560_STEP_PARTS, allow_pickle=False) as source:
        for group in metadata["groups"]:
            key = group["array_key"]
            parts[key] = np.asarray(source[key], float) @ rotation.T + translation
    return parts


def autodrive_4r100_appearances() -> dict[str, dict]:

    return {group["array_key"]: group["appearance"]
            for group in k3560_1_metadata()["groups"]}


def autodrive_4r100() -> np.ndarray:
    return np.concatenate(list(autodrive_4r100_parts().values()))


_YASKAWA_TORCH_PARTS = Path(__file__).resolve().parent / "meshes" / "yaskawa_torches.npz"
_YASKAWA_TORCH_META = _YASKAWA_TORCH_PARTS.with_suffix(".json")


def yaskawa_torch_metadata() -> dict:

    return json.loads(_YASKAWA_TORCH_META.read_text(encoding="utf-8"))


def yaskawa_torch_parts(variant: str | None = None) -> dict[str, np.ndarray]:


    metadata = yaskawa_torch_metadata()
    model = metadata["variants"][variant or metadata["default_variant"]]
    registration = model["registration"]
    rotation = np.asarray(registration["rotation"], float)
    translation = np.asarray(registration["translation_mm"], float)
    with np.load(_YASKAWA_TORCH_PARTS, allow_pickle=False) as source:
        return {g["array_key"]: np.asarray(source[g["array_key"]], float) @ rotation.T + translation
                for g in model["groups"]}


def yaskawa_torch_appearances(variant: str | None = None) -> dict[str, dict]:

    metadata = yaskawa_torch_metadata()
    model = metadata["variants"][variant or metadata["default_variant"]]
    return {g["array_key"]: g["appearance"] for g in model["groups"] if g.get("appearance")}


def _annulus(outer_dia: float, bore_dia: float, x0: float, x1: float,
             segments: int) -> np.ndarray:

    loop = circle_loop(0.0, 0.0, outer_dia / 2, segments)
    hole = circle_loop(0.0, 0.0, bore_dia / 2, segments, clockwise=True)
    local = prism(loop, [hole], z0=x0, z1=x1)


    spin = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    return place(local, rotate=spin)


def wire_spool_parts(
    flange_dia: float = SPOOL_DIA,
    width: float = SPOOL_WIDTH,
    bore: float = SPOOL_BORE,
    flange_thk: float = SPOOL_FLANGE_THK,
    barrel_dia: float = SPOOL_BARREL_DIA,
    wound_dia: float = SPOOL_WOUND_DIA,
    full: bool = True,
    segments: int = 14,
) -> dict[str, np.ndarray]:


    parts = {
        "flange_inboard": _annulus(flange_dia, bore, 0.0, flange_thk, segments),
        "flange_outboard": _annulus(flange_dia, bore, width - flange_thk, width,
                                    segments),
        "barrel": _annulus(barrel_dia, bore, flange_thk, width - flange_thk,
                           segments),
    }
    if full:
        parts["wire_pack"] = _annulus(wound_dia, barrel_dia, flange_thk,
                                      width - flange_thk, segments)
    return parts


def wire_spool(**kwargs) -> np.ndarray:
    return np.concatenate(list(wire_spool_parts(**kwargs).values()))


def _shell_sector(outer, inner, x0, x1, start, stop) -> np.ndarray:

    steps = max(1, int(np.ceil((stop - start) / 10)))
    angles = np.radians(np.linspace(start, stop, steps + 1))
    outer_loop = np.c_[outer * np.cos(angles), outer * np.sin(angles)]
    inner_loop = np.c_[inner * np.cos(angles[::-1]), inner * np.sin(angles[::-1])]
    solid = prism(np.concatenate([outer_loop, inner_loop]), z0=x0, z1=x1)
    return place(solid, rotate=_axis_rotation("x"))


def spool_housing_parts() -> dict[str, np.ndarray]:


    r, wall = SPOOL_HOUSING_DIA / 2, SPOOL_HOUSING_WALL
    end = SPOOL_HOUSING_WIDTH - 12
    parts = {
        "back_cover": _annulus(2 * r, 36, -12, -7, 36),
        "back_band": _annulus(2 * r, 2 * (r - wall), -7, 10, 36),
        "front_band": _annulus(2 * r, 2 * (r - wall), 70, end, 36),
        "front_rim": _annulus(2 * r, 2 * (r - 15), end - wall, end, 36),
        "barrel_above_window": _shell_sector(r, r - wall, 10, 70, 0, 135),
        "barrel_below_window": _shell_sector(r, r - wall, 10, 70, 225, 360),
        "spindle": cylinder(14, 100, base=(-20, 0, 0), segments=16, axis="x"),
        "retainer": cylinder(22, 12, base=(60, 0, 0), segments=6, axis="x"),
        "outlet_guide": cylinder(14, 28, base=(28, 0, r - 5), segments=12),
    }
    return parts


def spool_housing() -> np.ndarray:
    return np.concatenate(list(spool_housing_parts().values()))


def spool_support_parts(hub) -> dict[str, np.ndarray]:


    hx, hy, hz = hub
    mx, my, mz = SPOOL_SUPPORT_MOUNT
    return {
        "mount_plate": box((70, 90, 16), center=(mx, my, mz)),
        "support_arm": box_bounds((min(hx - 24, mx), my - 16, mz),
                                    (max(hx + 24, mx), my + 16, mz + 24)),
        "support_upright": box_bounds((hx - 24, hy - 38, mz + 24),
                                         (hx + 24, hy - 14, hz + 24)),
        "hub_mount": box((64, 18, 64), center=(hx, hy - 14, hz)),
    }


def fume_hood_parts(
    mouth_dia: float = HOOD_MOUTH_DIA,
    cone_height: float = HOOD_CONE_H,
    duct_dia: float = HOOD_DUCT_DIA,
    duct_length: float = HOOD_DUCT_LEN,
    lip: float = HOOD_LIP,
    segments: int = 20,
) -> dict[str, np.ndarray]:


    wall = 2.0

    def shell(r0, r1, z0, z1):
        angles = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
        unit = np.stack([np.cos(angles), np.sin(angles)], axis=1)
        rings = [np.c_[unit * r, np.full(segments, z)]
                 for r, z in ((r0, z0), (r1, z1), (r0 - wall, z0), (r1 - wall, z1))]
        outer_lo, outer_hi, inner_lo, inner_hi = rings
        tris = []
        for i in range(segments):
            j = (i + 1) % segments
            tris += _quad(outer_lo[i], outer_lo[j], outer_hi[j], outer_hi[i])
            tris += _quad(inner_lo[i], inner_hi[i], inner_hi[j], inner_lo[j])
            tris += _quad(outer_lo[i], inner_lo[i], inner_lo[j], outer_lo[j])
            tris += _quad(outer_hi[i], outer_hi[j], inner_hi[j], inner_hi[i])
        return np.asarray(tris, dtype=float)

    return {
        "lip": shell(mouth_dia / 2, mouth_dia / 2, 0.0, lip),
        "cone": shell(mouth_dia / 2, duct_dia / 2, lip, lip + cone_height),
        "duct": shell(duct_dia / 2, duct_dia / 2, lip + cone_height,
                      lip + cone_height + duct_length),
    }


def fume_hood(**kwargs) -> np.ndarray:
    return np.concatenate(list(fume_hood_parts(**kwargs).values()))


def yrc1000_cabinet_parts(
    body=YRC1000_BODY,
    over_depth: float = YRC1000_OVER_DEPTH,
    plinth: float = YRC1000_PLINTH,
    eye=YRC1000_EYE,
) -> dict[str, np.ndarray]:


    dx, dy, dz = body
    ex, ey, ez = eye
    z0 = plinth
    parts = {
        "plinth": box_bounds((-dx / 2 + 20.0, -dy / 2 + 20.0, 0.0),
                             (dx / 2 - 20.0, dy / 2 - 20.0, plinth)),
        "body": box_bounds((-dx / 2, -dy / 2, z0), (dx / 2, dy / 2, z0 + dz)),

        "rear_duct": box_bounds((-dx / 2 - (over_depth - dx), -dy / 2 + 60.0, z0 + 60.0),
                                (-dx / 2, dy / 2 - 60.0, z0 + dz - 60.0)),
    }
    for n, (sx, sy) in enumerate(((1, 1), (1, -1), (-1, 1), (-1, -1))):
        cx = sx * (dx / 2 - ex / 2 - 30.0)
        cy = sy * (dy / 2 - ey / 2 - 30.0)
        parts[f"lift_eye_{n}"] = box((ex, ey, ez), center=(cx, cy, z0 + dz + ez / 2))
    return parts


_YRC1000_STEP_MESH = Path(__file__).resolve().parent / "meshes" / "yrc1000_step.npy"


def yrc1000_cabinet(**kwargs) -> np.ndarray:


    if not kwargs and _YRC1000_STEP_MESH.exists():
        step = np.load(_YRC1000_STEP_MESH).astype(float)
        tris = step[:, :, [2, 0, 1]] + np.array([-213.5, -299.0, 9.0])

        area2 = np.linalg.norm(
            np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
        return tris[area2 > 1e-6]
    return np.concatenate(list(yrc1000_cabinet_parts(**kwargs).values()))


def lincoln_r450_parts(
    case=R450_CASE,
    relief: float = R450_PANEL_RELIEF,
    skid: float = R450_SKID,
) -> dict[str, np.ndarray]:


    dx, dy, dz = case
    parts = {
        "skid": box_bounds((-dx / 2, -dy / 2, 0.0), (dx / 2, dy / 2, skid)),
        "case": box_bounds((-dx / 2, -dy / 2 + 15.0, skid),
                           (dx / 2 - relief, dy / 2 - 15.0, skid + dz - 60.0)),
        "front_panel": box_bounds((dx / 2 - relief, -dy / 2 + 60.0, skid + 60.0),
                                  (dx / 2, dy / 2 - 60.0, skid + dz - 140.0)),
        "top_cap": box_bounds((-dx / 2, -dy / 2, skid + dz - 60.0),
                              (dx / 2, dy / 2, skid + dz)),
    }
    return parts


def lincoln_r450(**kwargs) -> np.ndarray:
    return np.concatenate(list(lincoln_r450_parts(**kwargs).values()))


def equipment_rack_parts() -> dict[str, np.ndarray]:


    dx, dy, m = RACK_DEPTH, RACK_WIDTH, RACK_MEMBER
    parts = {}
    for ix, sx in enumerate((-1, 1)):
        for iy, sy in enumerate((-1, 1)):
            x, y = sx * (dx - m) / 2, sy * (dy - m) / 2
            parts[f"leg_{ix}_{iy}"] = box_bounds(
                (x - m / 2, y - m / 2, 0),
                (x + m / 2, y + m / 2, RACK_CONTROLLER_DECK_Z))
    for name, z in (("lower_deck", RACK_LOWER_DECK_Z),
                    ("controller_deck", RACK_CONTROLLER_DECK_Z)):
        parts[name] = box_bounds((-dx / 2, -dy / 2, z - RACK_SHELF_THK),
                                  (dx / 2, dy / 2, z))
    return parts


def equipment_rack() -> np.ndarray:
    return np.concatenate(list(equipment_rack_parts().values()))


def power_disconnect() -> np.ndarray:

    dx, dy, dz = DISCONNECT_BODY
    return box_bounds((-dx / 2, -dy / 2, 0), (dx / 2, dy / 2, dz))


def waam_fixture() -> np.ndarray:


    path = Path(__file__).resolve().parent / "meshes" / "waam_fixture.npy"
    return np.load(path, allow_pickle=False).astype(float)


MEASUREMENTS: list[dict[str, object]] = [

    {
        "id": "mh185-axis-above-foot",
        "component": "mh185_headstock",
        "parameter": "station axis height above the headstock foot underside",
        "current_value_mm": AXIS_ABOVE_FOOT,
        "basis": "MODEL",
        "how_to_measure": "Stand a rule on the stand top plate under the "
                          "headstock foot and measure vertically to the "
                          "faceplate centre (use the centre bore or the dowel "
                          "midpoint as the mark).",
        "priority": "L",
    },
    {
        "id": "mh185-stand-splice-z",
        "component": "mh185_headstock",
        "parameter": "height of the plate joint seen at ~820-826 on the stand "
                     "column (foot, or a column splice?)",
        "current_value_mm": "?",
        "basis": "LOG",
        "how_to_measure": "Hook the tape on the floor beside the positioner "
                          "column and read the height of the horizontal plate "
                          "seam, then note whether the seam is under the "
                          "headstock casting or below the top plate.",
        "priority": "H",
    },
    {
        "id": "mh185-column-overhang",
        "component": "mh185_headstock",
        "parameter": "faceplate face overhang past the front of the stand column",
        "current_value_mm": 30.0,
        "basis": "ASSUMED",
        "how_to_measure": "Hold a square against the faceplate face and "
                          "measure horizontally back along the station axis "
                          "to the front face of the stand column.",
        "priority": "M",
    },
    {
        "id": "mh185-stand-column",
        "component": "mh185_headstock",
        "parameter": "stand column section across the station axis",
        "current_value_mm": STAND_COLUMN,
        "basis": "SCALED",
        "how_to_measure": "Tape the positioner column across both faces at "
                          "chest height; record both directions if it is not "
                          "square.",
        "priority": "M",
    },
    {
        "id": "mh185-stand-deck-z",
        "component": "mh185_headstock",
        "parameter": "stand base plate height above the cell base rail",
        "current_value_mm": STAND_DECK_Z,
        "basis": "SCALED",
        "how_to_measure": "Rest a rule on the top of the cell's bottom "
                          "aluminium base rail beside the column and measure "
                          "up to the underside of the column base plate.",
        "priority": "M",
    },

    {
        "id": "platter-fixture-size",
        "component": "charles_platter",
        "parameter": "knurled-screw fixture block L x W x H",
        "current_value_mm": "%.0f x %.0f x %.0f" % TRAINING_BLOCK,
        "basis": "ASSUMED",
        "how_to_measure": "Tape the fixture plate on the platter in all three "
                          "directions, measuring the plate body only (not the "
                          "knurled screws).",
        "priority": "M",
    },
    {
        "id": "platter-fixture-pos",
        "component": "charles_platter",
        "parameter": "fixture block centre distance from the faceplate plane",
        "current_value_mm": 0.3 * PLATTER_L,
        "basis": "ASSUMED",
        "how_to_measure": "Hook the tape on the faceplate face and measure "
                          "along the platter centreline to the near edge of "
                          "the fixture plate.",
        "priority": "M",
    },
    {
        "id": "platter-hole-x",
        "component": "charles_platter",
        "parameter": "probe-datum hole centres, distance from the faceplate plane",
        "current_value_mm": PLATTER_L / 2,
        "basis": "ASSUMED",
        "how_to_measure": "Hook the tape on the faceplate face and measure "
                          "along the platter to the centre of either "
                          "3/4 in hole (they share one line).",
        "priority": "H",
    },
    {
        "id": "platter-corner-r",
        "component": "charles_platter",
        "parameter": "plate corner radius",
        "current_value_mm": PLATTER_CORNER_R,
        "basis": "ASSUMED",
        "how_to_measure": "Lay a radius gauge (or trace onto paper) at one "
                          "free-end corner of the platter.",
        "priority": "L",
    },
    {
        "id": "platter-gusset-dia",
        "component": "charles_platter",
        "parameter": "semicircular gusset diameter",
        "current_value_mm": GUSSET_DIA,
        "basis": "ASSUMED",
        "how_to_measure": "Tape across the flat top edge of the half-disc "
                          "gusset at the faceplate end — that chord is the "
                          "full diameter.",
        "priority": "M",
    },
    {
        "id": "platter-gusset-thk",
        "component": "charles_platter",
        "parameter": "gusset plate thickness",
        "current_value_mm": GUSSET_THK,
        "basis": "ASSUMED",
        "how_to_measure": "Caliper the gusset plate on its exposed curved "
                          "edge below the platter.",
        "priority": "L",
    },

    {
        "id": "reamer-post-section",
        "component": "reamer_station",
        "parameter": "stand post section (mid-height)",
        "current_value_mm": REAMER_POST,
        "basis": "PHOTO",
        "how_to_measure": "Tape across the reamer stand's tapered column at "
                          "half its height, in both directions.",
        "priority": "M",
    },
    {
        "id": "reamer-base-thk",
        "component": "reamer_station",
        "parameter": "stand floor-foot plate thickness",
        "current_value_mm": REAMER_BASE[2],
        "basis": "ASSUMED",
        "how_to_measure": "Caliper the stand's floor foot at a corner. The "
                          "booklet dimensions the head mounting plate instead; "
                          "the floor foot's width, depth and thickness are unverified.",
        "priority": "L",
    },
    {
        "id": "reamer-floor-foot-width",
        "component": "reamer_station",
        "parameter": "stand floor-foot width",
        "current_value_mm": REAMER_BASE[0],
        "basis": "ASSUMED",
        "how_to_measure": "Measure the floor foot across its first side; "
                          "identify this side in a photograph. The booklet's "
                          "243.2 mm belongs to the head plate, not this foot.",
        "priority": "L",
    },
    {
        "id": "reamer-floor-foot-depth",
        "component": "reamer_station",
        "parameter": "stand floor-foot depth",
        "current_value_mm": REAMER_BASE[1],
        "basis": "ASSUMED",
        "how_to_measure": "Measure the floor foot perpendicular to its first "
                          "side. The booklet's178.2 mm does not dimension this foot.",
        "priority": "L",
    },
    {
        "id": "reamer-head-base-thk",
        "component": "reamer_station",
        "parameter": "head mounting plate thickness",
        "current_value_mm": REAMER_HEAD_BASE[2],
        "basis": "ASSUMED",
        "how_to_measure": "Caliper the head mounting plate at an exposed edge "
                          "above the stand; the booklet dimensions its XY "
                          "footprint, not thickness or centering under the head.",
        "priority": "L",
    },

    {
        "id": "pedestal-section",
        "component": "robot_pedestal",
        "parameter": "pedestal column section",
        "current_value_mm": PEDESTAL_SECTION,
        "basis": "LOG",
        "how_to_measure": "Tape across the robot pedestal column at mid "
                          "height in both directions, clear of the cable "
                          "entry.",
        "priority": "M",
    },
    {
        "id": "pedestal-top-plate",
        "component": "robot_pedestal",
        "parameter": "pedestal top plate L x W x T",
        "current_value_mm": "%.0f x %.0f x %.0f" % PEDESTAL_TOP_PLATE,
        "basis": "ASSUMED",
        "how_to_measure": "Tape the plate the robot base bolts to, across "
                          "both directions, and caliper its edge thickness.",
        "priority": "M",
    },

    {
        "id": "frame-height",
        "component": "cell_frame_v2",
        "parameter": "fence overall height above the cell base",
        "current_value_mm": CELL_HEIGHT,
        "basis": "MANUAL",
        "how_to_measure": "Stand the tape on the floor against a corner post "
                          "and read the height of the top rail's upper "
                          "surface.",
        "priority": "M",
    },
    {
        "id": "frame-panel-thk",
        "component": "cell_frame_v2",
        "parameter": "infill panel slab thickness",
        "current_value_mm": PANEL_THK,
        "basis": "ASSUMED",
        "how_to_measure": "Caliper a fence panel at the door jamb where its "
                          "edge is exposed in the extrusion slot.",
        "priority": "L",
    },
    {
        "id": "frame-door-offset",
        "component": "cell_frame_v2",
        "parameter": "door opening centre offset along the front face",
        "current_value_mm": DOOR_BAY_INSET,
        "basis": "ASSUMED",
        "how_to_measure": "Tape from the outside face of the left front "
                          "corner post to the near jamb post of the door "
                          "opening.",
        "priority": "M",
    },
    {
        "id": "frame-bay-division",
        "component": "cell_frame_v2",
        "parameter": "intermediate post spacing on the back and side faces",
        "current_value_mm": "?",
        "basis": "ASSUMED",
        "how_to_measure": "Walk the fence and tape post-centre to post-centre "
                          "along the back face and each side, recording each "
                          "bay in order.",
        "priority": "M",
    },

    {
        "id": "arm-cluster-mass",
        "component": "autodrive_4r100",
        "parameter": "installed mass on the U-arm (feeder + K3572-1 bracket "
                     "only — the spool is base-mounted) plus the wrist/torch "
                     "mass, kg",
        "current_value_mm": "? (ceiling: 22 kg U-axis TOTAL incl. wrist)",
        "basis": "MANUAL",
        "how_to_measure": "Weigh the feeder and its bracket, and the torch "
                          "package at the wrist. HW1484060 7.1.1: U-axis max "
                          "22 kg INCLUDING wrist load (its example: 6 kg wrist "
                          "-> 16 kg on the upper arm). Also confirm ARM Control "
                          "load setting was performed on the pendant.",
        "priority": "H",
    },
    {
        "id": "feeder-bracket-pattern",
        "component": "cell_lc",
        "parameter": "K3572-1 lower staggered hole pairs on the rear U-arm pads",
        "current_value_mm": "lower staggered pairs (owner-confirmed)",
        "basis": "MODEL",
        "how_to_measure": "The owner confirms the lower pairs closer to the "
                          "corner bend. The rear pad axes have 48/78 mm spacing "
                          "in HW1484060 fig. 7-1. A close-up of the seating "
                          "surfaces can check the mesh registration; no bracket "
                          "removal or hole-set identification is needed.",
        "priority": "H",
    },
    {
        "id": "feeder-body-axes",
        "component": "autodrive_4r100",
        "parameter": "installed inlet/outlet orientation of the supplied K3560 CAD",
        "current_value_mm": "CAD fitting axis native Y; barbed negative-Y end used as inlet",
        "basis": "MODEL",
        "how_to_measure": "Photograph the wire-supply and torch connections. "
                          "Geometry is supplied by K3560-1 STEP; the functional "
                          "inlet label and installed body yaw need confirmation.",
        "priority": "L",
    },
    {
        "id": "feeder-bracket-envelope",
        "component": "autodrive_4r100",
        "parameter": "K3572-1 overall envelope: along-wire x across x total drop",
        "current_value_mm": "%.3f x %.3f x %.3f" % K3572_BRACKET_ENVELOPE,
        "basis": "MODEL",
        "how_to_measure": "Optional receiving check only: compare the outer "
                          "envelope with the hash-pinned vendor STEP; no lab "
                          "measurement is needed for the nominal model.",
        "priority": "L",
    },
    {
        "id": "feeder-bracket-offset",
        "component": "cell_lc",
        "parameter": "verify rear-pad seating with the confirmed lower hole set; "
                     "bracket shape is already supplied by CAD",
        "current_value_mm": "(-225.8125, 73.93, 80.13), Rz 180 (lower pairs)",
        "basis": "MODEL",
        "how_to_measure": "The rear pads and lower staggered hole set are "
                          "owner-confirmed. Placement is fitted to mesh bores, "
                          "not a vendor assembly transform. Check the seated "
                          "faces and sheet datum or supply the "
                          "bracket installation drawing if available.",
        "priority": "H",
    },
    {
        "id": "feeder-arm-station",
        "component": "cell_lc",
        "parameter": "feeder mount position along the upper arm, measured from "
                     "the U-axis joint face",
        "current_value_mm": "rear mounting face X=-75 / sheet centre X=-225.8125",
        "basis": "MODEL",
        "how_to_measure": "Measure from the U-axis datum plane to the seated "
                          "rear pad face along local X. Positive is toward "
                          "the wrist; rearward is negative. Mark both datums.",
        "priority": "H",
    },
    {
        "id": "feeder-body-on-bracket",
        "component": "autodrive_4r100",
        "parameter": "4R100 body X/Y slide position on the K3572 top sheet",
        "current_value_mm": "nominal two-hole CAD/mesh fit; installed pose unmeasured",
        "basis": "MODEL",
        "how_to_measure": "Tape from the bracket sheet edges to the feeder "
                          "case at two adjacent sides; the mounting slots "
                          "allow more than one installed position.",
        "priority": "M",
    },
    {
        "id": "spool-flange-dia",
        "component": "wire_spool",
        "parameter": "installed spool flange diameter; distinguish the live reel from the S300 catalog asset",
        "current_value_mm": "200 live display (provisional); 300 S300 catalog",
        "basis": "PHOTO",
        "how_to_measure": "Tape across the flange of the spool currently "
                          "hung on the base/S-axis support, through the hub "
                          "centre.",
        "priority": "L",
    },
    {
        "id": "spool-width",
        "component": "wire_spool",
        "parameter": "spool overall width across both flanges",
        "current_value_mm": "55 live display (provisional); 103 S300 catalog",
        "basis": "PHOTO",
        "how_to_measure": "Caliper the spool from flange outer face to flange "
                          "outer face at the rim.",
        "priority": "L",
    },
    {
        "id": "spool-mount-offset",
        "component": "cell_lc",
        "parameter": "BASE-mounted spool on bracket kit 183080-1 (BOM item "
                     "64): hub position relative to the robot base; housing "
                     "rotation with the S-axis confirmed by the user",
        "current_value_mm": "(-330, 0, 480) on link_1_s; z SCALED off print "
                            "sheet 2, rearward offset PHOTO",
        "basis": "SCALED",
        "how_to_measure": "Tape from the robot base mounting plane up to the "
                          "spool hub centre (arbor nut) and horizontally back "
                          "from the base centre; record the S-axis pose used. "
                          "The user has confirmed that the housing rotates "
                          "with S, so only the installed offsets remain open.",
        "priority": "H",
    },

    {
        "id": "hood-mouth-dia",
        "component": "fume_hood",
        "parameter": "extraction cone mouth diameter",
        "current_value_mm": HOOD_MOUTH_DIA,
        "basis": "ASSUMED",
        "how_to_measure": "Tape across the open mouth of the hood cone "
                          "through its centre (a helper holds the dumb end).",
        "priority": "L",
    },
    {
        "id": "hood-cone-height",
        "component": "fume_hood",
        "parameter": "cone taper length, mouth plane to duct collar",
        "current_value_mm": HOOD_CONE_H,
        "basis": "ASSUMED",
        "how_to_measure": "Hold a rule vertically at the cone axis and "
                          "measure from the mouth rim plane up to the collar "
                          "where the cone meets the round duct.",
        "priority": "M",
    },
    {
        "id": "hood-duct-dia",
        "component": "fume_hood",
        "parameter": "duct diameter above the cone",
        "current_value_mm": HOOD_DUCT_DIA,
        "basis": "ASSUMED",
        "how_to_measure": "Wrap a tape around the duct just above the cone "
                          "collar and divide the circumference by pi.",
        "priority": "L",
    },
    {
        "id": "hood-mouth-height",
        "component": "cell_lc",
        "parameter": "hood mouth plane height above the floor",
        "current_value_mm": "2100 (display assumption)",
        "basis": "ASSUMED",
        "how_to_measure": "Stand the tape on the floor directly under the "
                          "hood and read the height of the cone's mouth rim.",
        "priority": "L",
    },
    {
        "id": "hood-plan-position",
        "component": "cell_lc",
        "parameter": "hood axis position relative to the robot base centre "
                     "(forward and lateral)",
        "current_value_mm": "700 forward / 450 right (display assumption)",
        "basis": "ASSUMED",
        "how_to_measure": "Drop a plumb bob from the hood cone's centre and "
                          "tape from the mark on the floor to the robot "
                          "pedestal's front and right faces.",
        "priority": "L",
    },

    {
        "id": "yrc1000-plinth",
        "component": "yrc1000_cabinet",
        "parameter": "controller base/plinth height under the cabinet",
        "current_value_mm": YRC1000_PLINTH,
        "basis": "ASSUMED",
        "how_to_measure": "Measure from the supporting rack deck to the "
                          "controller cabinet's bottom skin; record the "
                          "rack deck height above the floor separately.",
        "priority": "L",
    },
    {
        "id": "yrc1000-position",
        "component": "cell_lc",
        "parameter": "controller cabinet position relative to the cell frame",
        "current_value_mm": "upper rack deck; relocated side confirmed, exact offsets provisional",
        "basis": "ASSUMED",
        "how_to_measure": "Tape from the outside face of the nearest fence "
                          "post to the nearest cabinet corner, and note which "
                          "way the door faces.",
        "priority": "L",
    },
    {
        "id": "r450-skid",
        "component": "lincoln_r450",
        "parameter": "base skid/foot height under the R450 case",
        "current_value_mm": R450_SKID,
        "basis": "ASSUMED",
        "how_to_measure": "Measure from the supporting rack deck to the "
                          "R450 case's bottom skin; record the lower deck "
                          "height above the floor separately.",
        "priority": "L",
    },
    {
        "id": "r450-position",
        "component": "cell_lc",
        "parameter": "Power Wave R450 position relative to the cell frame",
        "current_value_mm": "lower rack deck; relocated side confirmed, exact offsets provisional",
        "basis": "ASSUMED",
        "how_to_measure": "Tape from the outside face of the nearest fence "
                          "post to the nearest R450 case corner, and note "
                          "which way the control panel faces.",
        "priority": "L",
    },

    {
        "id": "cell-frame-fore-aft",
        "component": "cell_lc",
        "parameter": "robot S-axis to front and rear fence outside faces",
        "current_value_mm": "1520 forward / 620 rear (provisional outside faces)",
        "basis": "ASSUMED",
        "how_to_measure": "Measure from a plumb mark of the robot S-axis to "
                          "the front and rear outside frame faces; record "
                          "member thickness separately from the floor-layout datums.",
        "priority": "H",
    },
    {
        "id": "cell-frame-lateral",
        "component": "cell_lc",
        "parameter": "robot base centre to the LEFT and RIGHT fence rails",
        "current_value_mm": "770 left / 1370 right (provisional outside faces)",
        "basis": "ASSUMED",
        "how_to_measure": "Measure from a plumb mark of the robot S-axis to "
                          "the left and right outside frame faces; identify "
                          "outside faces separately from the floor-layout datums.",
        "priority": "H",
    },
    {
        "id": "platter-face-row",
        "component": "cell_lc",
        "parameter": "platter surface height and tilt; separate physical surface from the TCP command datum",
        "current_value_mm": "84.251 above axis is the command datum; raw single touch was 77.901",
        "basis": "PENDANT",
        "how_to_measure": "Collect additional well-spread surface measurements "
                          "with their raw robot pulses and measurement reference; "
                          "record actual wire extension or verified contact-tip-to-TCP "
                          "offset, and retain the existing single-point evidence.",
        "priority": "H",
    },
    {
        "id": "axis-above-robot-base",
        "component": "cell_lc",
        "parameter": "station axis height above the robot base plane",
        "current_value_mm": 62.6,
        "basis": "MANUAL",
        "how_to_measure": "Lay a straightedge across the robot base mounting "
                          "plane and measure vertically to the faceplate "
                          "centre — one direct tape, no shared floor datum "
                          "(this settles the print's 923 vs the manual's 936).",
        "priority": "H",
    },
    {
        "id": "station-axis-forward",
        "component": "cell_lc",
        "parameter": "station axis line, forward of the robot S axis",
        "current_value_mm": 1114.4,
        "basis": "SCALED",
        "how_to_measure": "Snap a chalk line on the floor through the robot "
                          "pedestal centre parallel to the front fence, then "
                          "tape from it to a plumb drop off the faceplate "
                          "centre.",
        "priority": "H",
    },
    {
        "id": "faceplate-plane-lateral",
        "component": "cell_lc",
        "parameter": "faceplate plane, right of the robot central axis",
        "current_value_mm": 641.4,
        "basis": "LOG",
        "how_to_measure": "Hold a square on the faceplate face and tape "
                          "horizontally along the station axis to the robot's "
                          "central (S) axis plumb line.",
        "priority": "H",
    },
    {
        "id": "reamer-stand-position",
        "component": "cell_lc",
        "parameter": "reamer stand base position relative to the robot base "
                     "centre (forward and lateral)",
        "current_value_mm": "140 fwd / 660 right",
        "basis": "ASSUMED",
        "how_to_measure": "Tape on the floor from the robot pedestal's front "
                          "face and right face to the two nearest corners of "
                          "the reamer stand's base plate.",
        "priority": "H",
    },
    {
        "id": "station-handedness",
        "component": "cell_lc",
        "parameter": "station S-zero direction (which way the axis points at "
                     "0 deg) and rotation sense",
        "current_value_mm": "?",
        "basis": "ASSUMED",
        "how_to_measure": "At the pendant, jog the station +10 deg from zero "
                          "and photograph the platter before and after; or "
                          "read the station calibration out of the dump.",
        "priority": "H",
    },


    {
        "id": "ctrl-option-function",
        "component": "controller",
        "parameter": "which optional functions are enabled — coordinated "
                     "motion (positioner rotation) and searching (touch sense)",
        "current_value_mm": "R1+S1 calibration records exist; option list and SRCH enablement unconfirmed",
        "basis": "PENDANT",
        "how_to_measure": "MAIN MENU > SYSTEM INFO > OPTION FUNCTION and "
                          "photograph the whole list; coordinated motion gates "
                          "every rotating job and searching gates touch sense.",
        "priority": "H",
    },
    {
        "id": "ctrl-station-pulses-per-deg",
        "component": "controller",
        "parameter": "station (EC) encoder pulses per degree — the one number "
                     "that unblocks emitting positioner rotation",
        "current_value_mm": "1831.82 predicted (4096 x 161 / 360), UNVERIFIED",
        "basis": "PENDANT",
        "how_to_measure": "Capture S1 pulse and angle readings at two distinct "
                          "known positions, using the same position for each "
                          "paired reading; differences determine scale without "
                          "assuming the encoder zero offset.",
        "priority": "H",
    },
    {
        "id": "ctrl-tool-file",
        "component": "controller",
        "parameter": "TOOL file entries (X Y Z Rx Ry Rz per tool number) — the "
                     "controller's own torch TCP",
        "current_value_mm": "TOOL 0 pendant: X -110.247 Y 0.324 Z 351.972; Rx 0 Ry -45 Rz 0",
        "basis": "PENDANT",
        "how_to_measure": "Confirm the captured TOOL 0 values and photograph "
                          "any other tools in use; record the tool number and "
                          "matching TCP values used for C1-C3 calibration.",
        "priority": "H",
    },
    {
        "id": "ctrl-arc-files-12",
        "component": "controller",
        "parameter": "contents of ASF#12 and AEF#12 (the arc start/end condition "
                     "files the generated wall job calls)",
        "current_value_mm": "? (referenced by WAAM-WALL1.JBI, contents unknown)",
        "basis": "PENDANT",
        "how_to_measure": "ARC WELDING > ARC START COND, open file 12 and "
                          "photograph every window, then the same for ARC END "
                          "COND 12; we are calling these without knowing them.",
        "priority": "H",
    },
    {
        "id": "ctrl-soft-limits",
        "component": "controller",
        "parameter": "soft limits actually set on this controller, all axes "
                     "plus the station",
        "current_value_mm": "S+-170 L-90/+155 U-85/+140 R+-150 B+90/-135 T+-210 "
                            "(US datasheet)",
        "basis": "PENDANT",
        "how_to_measure": "Photograph the axis range / soft limit screen; the "
                          "two datasheet editions disagree on the wrist, and "
                          "this settles which set the machine enforces.",
        "priority": "M",
    },
    {
        "id": "ctrl-station-calibration",
        "component": "controller",
        "parameter": "robot-to-station calibration (the C1-C3 three-point "
                     "result) defining where the positioner axis really is",
        "current_value_mm": "No. 01 R1+S1 TOOL 00: C1 robot pulses recorded; C2/C3 and all S1 positions needed",
        "basis": "PENDANT",
        "how_to_measure": "Read the existing C1, C2 and C3 records, capturing "
                          "all six R1 pulses plus the corresponding S1 position "
                          "and units for each, and confirm the matching tool "
                          "data without reteaching the calibration.",
        "priority": "M",
    },
    {
        "id": "ctrl-uframe-file",
        "component": "controller",
        "parameter": "USER FRAME entries in use (which UF# means what)",
        "current_value_mm": "? (jobs so far are TOOL 0 / no user frame)",
        "basis": "PENDANT",
        "how_to_measure": "ROBOT > USER COORDINATE, photograph each defined "
                          "frame number and its origin/orientation values.",
        "priority": "M",
    },
    {
        "id": "ctrl-version",
        "component": "controller",
        "parameter": "system software version and manipulator/controller model "
                     "and serial",
        "current_value_mm": "type YR-1-06VXH12-A01 from the nameplate",
        "basis": "PENDANT",
        "how_to_measure": "SYSTEM INFO > VERSION, photograph the screen; the "
                          "software floor decides whether MotoROS2 is even an "
                          "option later.",
        "priority": "M",
    },
]


def measurements_by_priority() -> dict[str, int]:

    out: dict[str, int] = {}
    for row in MEASUREMENTS:
        key = str(row["priority"])
        out[key] = out.get(key, 0) + 1
    return out


MEASUREMENTS.extend([
    {"id": "equipment-rack-registration", "component": "equipment_rack",
     "parameter": "rack footprint, deck heights and installed position",
     "current_value_mm": "800 x 800; deck tops 60 and 850; provisional",
     "basis": "ASSUMED", "priority": "M",
     "how_to_measure": "Tape the rack outside faces and both deck tops from the floor, then measure its corners relative to the robot base."},
    {"id": "disconnect-envelope", "component": "power_disconnect",
     "parameter": "disconnect enclosure body and mounting position",
     "current_value_mm": "180 x 380 x 500; provisional",
     "basis": "ASSUMED", "priority": "M",
     "how_to_measure": "Tape the disconnect enclosure in three directions and record its mounting height and offset from the rack."},
    {"id": "waam-fixture-registration", "component": "waam_fixture",
     "parameter": "CAD_FIXTURE_001 installed pose and clamped work surface",
     "current_value_mm": "508 x 254 x 50.8 CAD; installed pose unknown",
     "basis": "MODEL", "priority": "H",
     "how_to_measure": "Confirm whether the slotted WAAM fixture is installed, locate three mounting datums, and record touch-off points on the actual clamped work surface."},
    {"id": "frame-envelope-datums", "component": "cell_frame_v2",
     "parameter": "outside envelope versus 2000 x 2000 floor-layout datums",
     "current_value_mm": "2140 x 2140 simplified outside envelope",
     "basis": "MODEL", "priority": "M",
     "how_to_measure": "Tape the extrusion outside faces and projecting feet separately, and locate them relative to the 2000 mm floor-layout datums."},
    {"id": "cell-base-structure", "component": "cell_base",
     "parameter": "structural rail sections, crossmember and anchor-pad locations",
     "current_value_mm": "100 x 80 perimeter rails; 115 pitch / 12.6 bores per sheet 5; layout provisional",
     "basis": "ASSUMED", "priority": "M",
     "how_to_measure": "Measure the base rail sections, crossmember top and eight anchoring stations; distinguish the open floor from any local platform."},
    {"id": "cell-deck-edge", "component": "cell_deck",
     "parameter": "which pedestal face is flush with the deck's flat long edge",
     "current_value_mm": "column back face at x = -180 (the 360 mm column); outline, 6 in top and 3 in underside taped 2026-09-24",
     "basis": "ASSUMED", "priority": "L",
     "how_to_measure": "Tape from the deck's flat long edge to the centre of the robot's round base, perpendicular to the edge.",
     },
    {"id": "pedestal-connections", "component": "robot_pedestal",
     "parameter": "foot plate, gussets and fastener positions",
     "current_value_mm": "460 x 460 x 20 foot; 120 gusset drop; provisional",
     "basis": "PHOTO", "priority": "M",
     "how_to_measure": "Measure the pedestal foot plate, triangular webs and both bolt patterns without changing the recorded floor-to-mount height."},
    {"id": "spool-housing-support", "component": "spool_housing",
     "parameter": "housing, spindle, rotating support and bracket attachment",
     "current_value_mm": "240 diameter x 100 housing; S-axis rotation confirmed; shape provisional",
     "basis": "PHOTO", "priority": "H",
     "how_to_measure": "Measure the housing, window, spindle and support plates; record their mounting datums in the S-axis frame."},
    {"id": "wire-conduit-routing", "component": "autodrive_4r100",
     "parameter": "185712-1 flexible conduit ends, clips, diameter and slack",
     "current_value_mm": "18 OD display curve between S-link housing and U-link feeder; provisional",
     "basis": "ASSUMED", "priority": "H",
     "how_to_measure": "Record the conduit inlet/outlet and clamp locations, diameter and free length at known S/L/U poses; verify the path through the working range."},
])


GENERATORS = {
    "mh185_headstock": mh185_headstock,
    "charles_platter": charles_platter,
    "reamer_station": reamer_station,
    "robot_pedestal": robot_pedestal,
    "cell_base": cell_base,
    "cell_deck": cell_deck,
    "cell_frame": cell_frame,
    "cell_frame_v2": cell_frame_v2,
    "autodrive_4r100": autodrive_4r100,
    "wire_spool": wire_spool,
    "spool_housing": spool_housing,
    "fume_hood": fume_hood,
    "yrc1000_cabinet": yrc1000_cabinet,
    "lincoln_r450": lincoln_r450,
    "equipment_rack": equipment_rack,
    "power_disconnect": power_disconnect,
    "waam_fixture": waam_fixture,
}


def _write_stl():

    try:
        from waam_pipeline.slicer import write_stl
    except ImportError:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from waam_pipeline.slicer import write_stl
    return write_stl


def write_all(out_dir: str | Path) -> dict[str, Path]:

    write_stl = _write_stl()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, gen in GENERATORS.items():
        written[name] = write_stl(gen(), out / f"{name}.stl", name.encode()[:80])
    return written


def bbox(triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.asarray(triangles, float).reshape(-1, 3)
    return t.min(axis=0), t.max(axis=0)


if __name__ == "__main__":
    here = Path(__file__).parent
    for name, path in write_all(here / "stl").items():
        tris = GENERATORS[name]()
        lo, hi = bbox(tris)
        size = " x ".join(f"{v:.1f}" for v in (hi - lo))
        print(f"{name:18s} {len(tris):5d} tris  bbox {size} mm  -> {path.name}")
    pattern = faceplate_hole_pattern()
    print(f"\nfaceplate O{FACEPLATE_DIA:.0f} inside the O{TOOLING_DIA:.0f} tooling dia: "
          f"{len(pattern['m12'])}x M12 on a {FACEPLATE_BOLT_SQUARE:.0f} square, "
          f"{len(pattern['dowel'])}x O12 dowels {FACEPLATE_DOWEL_SPACING:.0f} apart")
    print(f"platter bare-plate mass ~{platter_mass_kg():.1f} kg "
          f"(MH185 holding torque {HOLDING_TORQUE_NM:.0f} N*m, "
          f"inertia budget {RATED_INERTIA_KGM2:.0f} kg*m^2, ratio {GEAR_RATIO})")
    counts = measurements_by_priority()
    tally = ", ".join(f"{k}={counts.get(k, 0)}" for k in ("H", "M", "L"))
    print(f"{len(MEASUREMENTS)} measurement rows ({tally}) -> "
          f"run gen_measurement_sheet.py for docs/measurement-sheet.csv")
