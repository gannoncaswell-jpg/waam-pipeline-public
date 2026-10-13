from __future__ import annotations

from pathlib import Path

import numpy as np


from .ar1440 import Joint, _rot, _validate_pose

MM = 1.0


HC10DTP_JOINTS: tuple[Joint, ...] = (
    Joint("joint_1_s", (0.0, 0.0, 0.0), (0, 0, 1), -210.0, 210.0, 130.0),
    Joint("joint_2_l", (0.0, 0.0, 275.0), (0, 1, 0), -180.0, 180.0, 130.0),
    Joint("joint_3_u", (0.0, 0.0, 700.0), (0, -1, 0), -290.0, 290.0, 180.0),
    Joint("joint_4_r", (500.0, 0.0, 0.0), (-1, 0, 0), -210.0, 210.0, 180.0),
    Joint("joint_5_b", (0.0, 162.0, 0.0), (0, -1, 0), -180.0, 180.0, 250.0),
    Joint("joint_6_t", (170.0, 0.0, 0.0), (-1, 0, 0), -210.0, 210.0, 250.0),
)


PULSES_PER_DEG = (2671.305, 2671.306, 2347.417, 1619.148, 1619.150, 1619.148)


V_TOKEN_MM_PER_MIN = 60.0

AIR_MM_MIN = 3333.0


_FLANGE_FROM_T = 170.0


WRIST_JOINT = "joint_4_r"

_LO = np.array([j.lower_deg for j in HC10DTP_JOINTS])
_HI = np.array([j.upper_deg for j in HC10DTP_JOINTS])
_ORIGINS = np.array([j.origin for j in HC10DTP_JOINTS], float)


def _axis_column(joint: Joint) -> tuple[int, float]:
    nonzero = [i for i, v in enumerate(joint.axis) if v]
    if len(nonzero) != 1 or abs(joint.axis[nonzero[0]]) != 1:
        raise ValueError(f"{joint.name}: axis {joint.axis} is not a signed basis vector")
    return nonzero[0], float(np.sign(joint.axis[nonzero[0]]))


_AXIS_COL: tuple[tuple[int, float], ...] = tuple(_axis_column(j) for j in HC10DTP_JOINTS)


def _axis_rot(col: int, deg: float) -> np.ndarray:
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    m = np.zeros((3, 3))
    if col == 0:
        m[0, 0], m[1, 1], m[1, 2], m[2, 1], m[2, 2] = 1.0, c, -s, s, c
    elif col == 1:
        m[1, 1], m[0, 0], m[0, 2], m[2, 0], m[2, 2] = 1.0, c, s, -s, c
    else:
        m[2, 2], m[0, 0], m[0, 1], m[1, 0], m[1, 1] = 1.0, c, -s, s, c
    return m


def _chain(angles_deg) -> tuple[np.ndarray, np.ndarray]:


    rots = np.empty((len(HC10DTP_JOINTS), 3, 3))
    pos = np.empty((len(HC10DTP_JOINTS), 3))
    r = np.eye(3)
    p = np.zeros(3)
    for i, ((col, sign), angle) in enumerate(zip(_AXIS_COL, angles_deg)):
        p = p + r @ _ORIGINS[i]
        r = r @ _axis_rot(col, sign * angle)
        rots[i] = r
        pos[i] = p
    return rots, pos


def fk(angles_deg: list[float] | np.ndarray) -> dict[str, np.ndarray]:


    if len(angles_deg) != len(HC10DTP_JOINTS):
        raise ValueError(f"expected {len(HC10DTP_JOINTS)} joint angles")
    for joint, angle in zip(HC10DTP_JOINTS, angles_deg):
        if not (joint.lower_deg - 1e-9 <= angle <= joint.upper_deg + 1e-9):
            raise ValueError(
                f"{joint.name}: {angle} deg outside [{joint.lower_deg}, {joint.upper_deg}]"
            )
    rots, pos = _chain(angles_deg)
    frames: dict[str, np.ndarray] = {}
    for joint, r, p in zip(HC10DTP_JOINTS, rots, pos):
        t = np.eye(4)
        t[:3, :3], t[:3, 3] = r, p
        frames[joint.name] = t
    frames["flange"] = frames[HC10DTP_JOINTS[-1].name]
    return frames


def wrist_center(angles_deg: list[float] | np.ndarray) -> np.ndarray:


    return fk(angles_deg)[WRIST_JOINT][:3, 3]


_L_ORIGIN = np.array([0.0, 0.0, 275.0])
_LOWER_ARM = 700.0
_UPPER_ARM = 500.0
_WRIST_OFFSET = 162.0
_ARM_MAX = _LOWER_ARM + _UPPER_ARM


P_POINT_REACH_MM = _ARM_MAX
FLANGE_REACH_MM = float(np.hypot(_ARM_MAX + _FLANGE_FROM_T, _WRIST_OFFSET))


_WRIST_SPAN_MM = float(np.hypot(_UPPER_ARM + _FLANGE_FROM_T, _WRIST_OFFSET))
ENVELOPE_MAX_MM = _LOWER_ARM + _WRIST_SPAN_MM
ENVELOPE_MIN_MM = abs(_LOWER_ARM - _WRIST_SPAN_MM)


def in_envelope(point_mm) -> bool:


    d = float(np.linalg.norm(np.asarray(point_mm, float) - _L_ORIGIN))
    return ENVELOPE_MIN_MM - 1e-9 <= d <= ENVELOPE_MAX_MM + 1e-9


class IkRefused(RuntimeError):


    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _within_limits(q) -> bool:
    return all(
        j.lower_deg - 1e-9 <= a <= j.upper_deg + 1e-9 for j, a in zip(HC10DTP_JOINTS, q)
    )


def _rotvec(r: np.ndarray) -> np.ndarray:


    t = r[0, 0] + r[1, 1] + r[2, 2]
    if t > 0.0:
        s = np.sqrt(t + 1.0) * 2.0
        w = 0.25 * s
        v = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]) / s
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = np.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2.0
        w = (r[2, 1] - r[1, 2]) / s
        v = np.array([0.25 * s, (r[0, 1] + r[1, 0]) / s, (r[0, 2] + r[2, 0]) / s])
    elif r[1, 1] > r[2, 2]:
        s = np.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2.0
        w = (r[0, 2] - r[2, 0]) / s
        v = np.array([(r[0, 1] + r[1, 0]) / s, 0.25 * s, (r[1, 2] + r[2, 1]) / s])
    else:
        s = np.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2.0
        w = (r[1, 0] - r[0, 1]) / s
        v = np.array([(r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s, 0.25 * s])
    if w < 0.0:
        w, v = -w, -v
    n = float(np.linalg.norm(v))
    if n < 1e-14:
        return np.zeros(3)
    return v / n * (2.0 * np.arctan2(n, w))


def _jacobian(rots: np.ndarray, pos: np.ndarray) -> np.ndarray:


    p_e = pos[-1]
    j = np.empty((6, 6))
    for i, (col, sign) in enumerate(_AXIS_COL):
        z = sign * rots[i][:, col]
        j[:3, i] = np.cross(z, p_e - pos[i])
        j[3:, i] = z
    return j


_ROT_WEIGHT_MM = 150.0
_MAX_ITER = 90
_STALL_ITERS = 12


def _residual(target: np.ndarray, q: np.ndarray):
    rots, pos = _chain(q)
    dp = target[:3, 3] - pos[-1]
    dw = _rotvec(target[:3, :3] @ rots[-1].T)
    cost = float(dp @ dp + (_ROT_WEIGHT_MM**2) * (dw @ dw))
    return cost, dp, dw, rots, pos


def _solve_from(target, q0, tol_mm, tol_rad):


    q = np.clip(np.asarray(q0, float), _LO, _HI)
    lam = 20.0
    stalled = 0
    cost, dp, dw, rots, pos = _residual(target, q)
    eye = np.eye(6)
    for _ in range(_MAX_ITER):
        if np.linalg.norm(dp) <= tol_mm and np.linalg.norm(dw) <= tol_rad:
            return q
        j = _jacobian(rots, pos)
        j[3:] *= _ROT_WEIGHT_MM
        e = np.concatenate([dp, dw * _ROT_WEIGHT_MM])
        try:
            dq = j.T @ np.linalg.solve(j @ j.T + (lam**2) * eye, e)
        except np.linalg.LinAlgError:
            return None
        q_new = np.clip(q + np.degrees(dq), _LO, _HI)
        cost_new, dp_new, dw_new, rots_new, pos_new = _residual(target, q_new)
        if cost_new < cost:
            stalled = 0 if cost_new < cost * (1.0 - 1e-4) else stalled + 1
            q, cost, dp, dw, rots, pos = q_new, cost_new, dp_new, dw_new, rots_new, pos_new
            lam = max(lam * 0.5, 1e-3)
        else:
            stalled += 1
            lam *= 4.0
            if lam > 1e7:
                break
        if stalled >= _STALL_ITERS:
            break
    if np.linalg.norm(dp) <= tol_mm and np.linalg.norm(dw) <= tol_rad:
        return q
    return None


def _seeds(target: np.ndarray, seed=None) -> list[list[float]]:


    out: list[list[float]] = []
    if seed is not None:
        out.append([float(v) for v in seed])
    p = target[:3, 3]
    aim = float(np.degrees(np.arctan2(p[1], p[0])))
    flip = aim - 180.0 if aim >= 0.0 else aim + 180.0
    for s in (aim, flip):
        for l_deg, u_deg in (
            (0.0, 0.0), (45.0, 45.0), (90.0, 0.0), (45.0, -45.0),
            (-45.0, 45.0), (135.0, -45.0), (90.0, 90.0), (0.0, 90.0),
        ):
            for b in (30.0, -30.0, 90.0, -90.0):
                out.append([s, l_deg, u_deg, 0.0, b, 0.0])
    rng = np.random.default_rng(20260904)
    for _ in range(12):
        out.append([float(v) for v in rng.uniform(_LO * 0.8, _HI * 0.8)])
    return out


def ik(
    pose: np.ndarray,
    all_solutions: bool = False,
    tol_mm: float = 0.01,
    tol_deg: float = 0.005,
    seed=None,
    strict: bool = True,
) -> list[float] | list[list[float]] | None:


    try:
        target = _validate_pose(pose)
    except ValueError as exc:
        raise IkRefused("argument", str(exc)) from exc
    if not np.isfinite([tol_mm, tol_deg]).all() or min(tol_mm, tol_deg) <= 0:
        raise IkRefused("argument", "IK tolerances must be finite and positive")

    tol_rad = float(np.radians(tol_deg))
    if not in_envelope(target[:3, 3]):
        d = float(np.linalg.norm(target[:3, 3] - _L_ORIGIN))
        if strict:
            raise IkRefused(
                "envelope",
                f"flange {d:.1f} mm from the L axis, outside the reachable shell "
                f"[{ENVELOPE_MIN_MM:.1f}, {ENVELOPE_MAX_MM:.1f}] mm",
            )
        return None

    found: list[list[float]] = []
    for q0 in _seeds(target, seed):
        q = _solve_from(target, q0, tol_mm, tol_rad)
        if q is None or not _within_limits(q):
            continue

        back = fk(q)["flange"]
        if np.linalg.norm(back[:3, 3] - target[:3, 3]) > tol_mm:
            continue
        if np.linalg.norm(_rotvec(target[:3, :3] @ back[:3, :3].T)) > tol_rad:
            continue
        sol = [float(v) for v in q]
        if not all_solutions:
            return sol
        if not any(np.allclose(sol, s, atol=1e-3) for s in found):
            found.append(sol)

    if found:
        found.sort(key=lambda q: abs(q[3]) + abs(q[5]))
        return found
    if strict:
        raise IkRefused(
            "orientation",
            "flange position is inside the envelope but no seed reached the "
            "requested orientation there (unattainable wrist pose, a joint "
            "limit, or a singular pocket)",
        )
    return None


def reachable(point_mm, approach=(0.0, 0.0, -1.0)) -> bool:


    a = np.asarray(approach, float)
    a = a / np.linalg.norm(a)
    ref = np.array([0.0, 0.0, 1.0]) if abs(a[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    y = np.cross(a, ref)
    y /= np.linalg.norm(y)
    z = np.cross(a, y)
    pose = np.eye(4)
    pose[:3, 0], pose[:3, 1], pose[:3, 2] = a, y, z
    pose[:3, 3] = point_mm
    return ik(pose, strict=False) is not None


_LINK_RADII = {
    "joint_1_s": 80.0,
    "joint_2_l": 70.0,
    "joint_3_u": 62.0,
    "joint_4_r": 52.0,
    "joint_5_b": 48.0,
    "joint_6_t": 40.0,
}


_MESH_LINKS = {
    "base_link": ("hc10/collision/base_link.stl", (0.0, 0.0, 0.0)),
    "link_1_s": ("hc10/collision/link_1_s.stl", (0.0, 0.0, 0.275)),
    "link_2_l": ("hc10/collision/link_2_l.stl", (0.0, 0.0, 0.0)),
    "link_3_u": ("hc10/collision/link_3_u.stl", (0.0, 0.0, 0.0)),
    "link_4_r": ("hc10/collision/link_4_r.stl", (0.0, 0.0, 0.0)),
    "link_5_b": ("hc10/collision/link_5_b.stl", (0.0, 0.0, 0.0)),
    "link_6_t": ("hc10dtp_b00/collision/link_6_t.stl", (-0.170, 0.0, 0.0)),
}


def _mesh_geom(link: str, mesh_dir: str) -> str:


    entry = _MESH_LINKS.get(link)
    if entry is None:
        return ""
    name, (ox, oy, oz) = entry
    parts = []
    for tag in ("visual", "collision"):
        parts.append(
            f'<{tag}><origin xyz="{ox:.4f} {oy:.4f} {oz:.4f}"/>'
            f'<geometry><mesh filename="{mesh_dir}/{name}"/></geometry>'
            + ('<material name="yaskawa_blue"><color rgba="0.25 0.4 0.75 1"/></material>'
               if tag == "visual" else "")
            + f"</{tag}>"
        )
    return "".join(parts)


def write_urdf(path: str, mesh_dir: str | None = None) -> str:


    m = 1e-3
    if mesh_dir is None:
        beside = Path(path).resolve().parent / "meshes"
        mesh_dir = "meshes" if beside.is_dir() else ""
    lines = ['<?xml version="1.0"?>', '<robot name="motoman_hc10dtp">']
    base_geom = (
        _mesh_geom("base_link", mesh_dir)
        if mesh_dir
        else '<visual><geometry><box size="0.26 0.26 0.10"/></geometry>'
        '<origin xyz="0 0 0.05"/></visual><collision><geometry>'
        '<box size="0.26 0.26 0.10"/></geometry><origin xyz="0 0 0.05"/></collision>'
    )
    lines.append(f'  <link name="base_link">{base_geom}</link>')
    parent = "base_link"
    for i, joint in enumerate(HC10DTP_JOINTS):
        link = f"link_{i + 1}_{joint.name[-1]}"

        nxt = (
            HC10DTP_JOINTS[i + 1].origin
            if i + 1 < len(HC10DTP_JOINTS)
            else (60.0, 0.0, 0.0)
        )
        length = float(np.linalg.norm(nxt))
        geom = ""
        if mesh_dir:
            geom = _mesh_geom(link, mesh_dir)
        elif length > 1.0:
            center = np.asarray(nxt) * m / 2
            direction = np.asarray(nxt, float) / length
            pitch = float(np.arccos(np.clip(direction[2], -1, 1)))
            yaw = float(np.arctan2(direction[1], direction[0]))
            radius = _LINK_RADII[joint.name] * m
            geom = (
                f'<visual><origin xyz="{center[0]:.4f} {center[1]:.4f} {center[2]:.4f}" '
                f'rpy="0 {pitch:.4f} {yaw:.4f}"/>'
                f'<geometry><cylinder radius="{radius:.3f}" length="{length * m:.4f}"/>'
                "</geometry></visual>"
                f'<collision><origin xyz="{center[0]:.4f} {center[1]:.4f} {center[2]:.4f}" '
                f'rpy="0 {pitch:.4f} {yaw:.4f}"/>'
                f'<geometry><cylinder radius="{radius:.3f}" length="{length * m:.4f}"/>'
                "</geometry></collision>"
            )
        lines.append(f'  <link name="{link}">{geom}</link>')
        ox, oy, oz = (v * m for v in joint.origin)
        lines.append(
            f'  <joint name="{joint.name}" type="revolute">'
            f'<parent link="{parent}"/><child link="{link}"/>'
            f'<origin xyz="{ox:.4f} {oy:.4f} {oz:.4f}"/>'
            f'<axis xyz="{joint.axis[0]} {joint.axis[1]} {joint.axis[2]}"/>'
            f'<limit lower="{np.radians(joint.lower_deg):.4f}" '
            f'upper="{np.radians(joint.upper_deg):.4f}" '
            f'velocity="{np.radians(joint.speed_deg_s):.4f}" effort="1000"/></joint>'
        )
        parent = link
    lines.append(
        '  <link name="flange"/>'
        '  <joint name="flange_fixed" type="fixed">'
        f'<parent link="{parent}"/><child link="flange"/><origin xyz="0 0 0"/></joint>'
    )
    lines.append("</robot>")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


ARM_JOINT_NAMES: tuple[str, ...] = tuple(j.name for j in HC10DTP_JOINTS)


def urdf_joint_positions(q_deg) -> dict[str, float]:


    q = [float(v) for v in q_deg]
    if len(q) != 6:
        raise ValueError("q_deg is six pendant-convention degrees, S L U R B T")
    return {n: float(np.radians(v)) for n, v in zip(ARM_JOINT_NAMES, q)}


def _default_urdf_path() -> Path:
    return Path(__file__).resolve().parents[3] / "robots" / "hc10dtp.urdf"


if __name__ == "__main__":


    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_urdf_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_urdf(str(target))
    text = target.read_text(encoding="utf-8")
    print(f"wrote {target}")
    print(f"  arm geometry: {'vendored meshes' if '<mesh' in text else 'PRIMITIVES'}")
