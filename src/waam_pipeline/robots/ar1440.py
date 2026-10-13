from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

MM = 1.0


@dataclass(frozen=True)
class Joint:
    name: str
    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    lower_deg: float
    upper_deg: float
    speed_deg_s: float


AR1440_MANUAL_L_LIMIT_SWITCH_UPPER = 95.0
AR1440_JOINTS: tuple[Joint, ...] = (
    Joint("joint_1_s", (0.0, 0.0, 0.0), (0, 0, 1), -170.0, 170.0, 260.0),
    Joint("joint_2_l", (155.0, 0.0, 450.0), (0, 1, 0), -90.0, 155.0, 230.0),
    Joint("joint_3_u", (0.0, 0.0, 614.0), (0, -1, 0), -85.0, 140.625, 260.0),
    Joint("joint_4_r", (0.0, 0.0, 200.0), (-1, 0, 0), -150.0, 150.0, 470.0),
    Joint("joint_5_b", (640.0, 0.0, 0.0), (0, -1, 0), -135.0, 90.0, 470.0),
    Joint("joint_6_t", (100.0, 0.0, 0.0), (-1, 0, 0), -210.0, 210.0, 700.0),
)


PULSES_PER_DEG = (1435.354, 1300.322, 1422.222, 969.956, 980.240, 454.756)


V_TOKEN_MM_PER_MIN = 60.0


AIR_MM_MIN = 19998.0

WRIST_JOINT = "joint_5_b"


def _rot(axis: tuple[float, float, float], deg: float) -> np.ndarray:
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    x, y, z = a
    return np.array(
        [
            [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
            [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
            [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
        ]
    )


def fk(angles_deg: list[float] | np.ndarray) -> dict[str, np.ndarray]:


    if len(angles_deg) != len(AR1440_JOINTS):
        raise ValueError(f"expected {len(AR1440_JOINTS)} joint angles")
    frames: dict[str, np.ndarray] = {}
    T = np.eye(4)
    for joint, angle in zip(AR1440_JOINTS, angles_deg):
        if not (joint.lower_deg - 1e-9 <= angle <= joint.upper_deg + 1e-9):
            raise ValueError(
                f"{joint.name}: {angle} deg outside [{joint.lower_deg}, {joint.upper_deg}]"
            )
        step = np.eye(4)
        step[:3, 3] = joint.origin
        step[:3, :3] = _rot(joint.axis, angle)
        T = T @ step
        frames[joint.name] = T.copy()
    frames["flange"] = frames[AR1440_JOINTS[-1].name]
    return frames


def wrist_center(angles_deg: list[float] | np.ndarray) -> np.ndarray:

    return fk(angles_deg)[WRIST_JOINT][:3, 3]


_A_LOWER = 614.0
_ELBOW_VEC = np.array([640.0, 0.0, 200.0])
_C_UPPER = float(np.linalg.norm(_ELBOW_VEC))
_PHI = np.degrees(np.arctan2(640.0, 200.0))
_SHOULDER = np.array([155.0, 0.0, 450.0])
_FLANGE_OFFSET = np.array([100.0, 0.0, 0.0])


def _within_limits(q: list[float]) -> bool:
    return all(
        j.lower_deg - 1e-9 <= a <= j.upper_deg + 1e-9 for j, a in zip(AR1440_JOINTS, q)
    )


def _validate_pose(pose) -> np.ndarray:

    target = np.asarray(pose, float)
    if target.shape != (4, 4) or not np.isfinite(target).all():
        raise ValueError("pose must be a finite 4x4 rigid transform")
    r = target[:3, :3]
    if (not np.allclose(target[3], [0, 0, 0, 1], atol=1e-9, rtol=0)
            or not np.allclose(r.T @ r, np.eye(3), atol=1e-6, rtol=0)
            or not np.isclose(np.linalg.det(r), 1, atol=1e-6, rtol=0)):
        raise ValueError("pose must contain a proper rotation and homogeneous last row")
    return target


def ik(
    pose: np.ndarray,
    all_solutions: bool = False,
    tol_mm: float = 0.5,
    *,
    tol_deg: float = 0.001,
) -> list[float] | list[list[float]] | None:


    target = _validate_pose(pose)
    if not np.isfinite([tol_mm, tol_deg]).all() or min(tol_mm, tol_deg) <= 0:
        raise ValueError("IK tolerances must be finite and positive")
    w = target[:3, 3] - target[:3, :3] @ _FLANGE_OFFSET

    base_s = np.degrees(np.arctan2(w[1], w[0]))
    solutions: list[list[float]] = []
    shoulder_branches = []
    for flip in (0.0, 180.0):
        s = base_s + flip
        s = (s + 180.0) % 360.0 - 180.0
        shoulder_branches.append(s)

    for s_deg, elbow in [(s, e) for s in shoulder_branches for e in (+1, -1)]:

        w_local = _rot((0, 0, 1), -s_deg) @ w
        wr = float(w_local[0]) - _SHOULDER[0]
        wz = float(w_local[2]) - _SHOULDER[2]
        d_sq = wr * wr + wz * wz
        d = np.sqrt(d_sq)
        if d < 1e-9 or d > _A_LOWER + _C_UPPER or d < abs(_A_LOWER - _C_UPPER):
            continue

        alpha = np.degrees(np.arctan2(wr, wz))
        cos_psi = np.clip((_A_LOWER**2 + d_sq - _C_UPPER**2) / (2 * _A_LOWER * d), -1, 1)
        psi = np.degrees(np.arccos(cos_psi))
        l_deg = alpha - elbow * psi


        l_deg = (l_deg + 180.0) % 360.0 - 180.0
        du = np.array(
            [_A_LOWER * np.sin(np.radians(l_deg)), _A_LOWER * np.cos(np.radians(l_deg))]
        )
        theta_c = np.degrees(np.arctan2(wr - du[0], wz - du[1]))
        u_deg = theta_c - l_deg - _PHI
        u_deg = (u_deg + 180.0) % 360.0 - 180.0


        r03 = _rot((0, 0, 1), s_deg) @ _rot((0, 1, 0), l_deg + u_deg)
        m = r03.T @ target[:3, :3]
        sb = float(np.hypot(m[0, 1], m[0, 2]))
        for wrist in (+1, -1):
            if sb < 1e-9:
                b_deg = 0.0 if m[0, 0] > 0 else 180.0
                r_deg = np.degrees(np.arctan2(m[2, 1], m[1, 1]))
                t_deg = 0.0
                if b_deg == 0.0:


                    rj, tj = AR1440_JOINTS[3], AR1440_JOINTS[5]
                    low = max(-rj.upper_deg, r_deg + tj.lower_deg)
                    high = min(-rj.lower_deg, r_deg + tj.upper_deg)
                    if low > high:
                        continue
                    total = r_deg
                    r_deg = float(np.clip(total, low, high))
                    t_deg = total - r_deg
                if wrist < 0:
                    continue
            else:
                b_deg = np.degrees(np.arctan2(wrist * sb, m[0, 0]))
                t_deg = np.degrees(np.arctan2(wrist * m[0, 1], wrist * m[0, 2]))
                r_deg = np.degrees(np.arctan2(wrist * m[1, 0], -wrist * m[2, 0]))


            for r_v in (r_deg, r_deg + 360.0, r_deg - 360.0):
                for t_v in (t_deg, t_deg + 360.0, t_deg - 360.0):
                    q = [s_deg, l_deg, -u_deg, -r_v, -b_deg, -t_v]
                    if not _within_limits(q):
                        continue
                    back = fk(q)["flange"]
                    err = np.linalg.norm(back[:3, 3] - target[:3, 3])
                    cos_angle = (np.trace(back[:3, :3].T @ target[:3, :3]) - 1) / 2
                    angle = np.degrees(np.arccos(np.clip(cos_angle, -1, 1)))
                    if err <= tol_mm and angle <= tol_deg:
                        solutions.append([float(v) for v in q])

    if not solutions:
        return None
    solutions.sort(key=lambda q: abs(q[3]) + abs(q[5]))
    return solutions if all_solutions else solutions[0]


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
    return ik(pose) is not None


_LINK_RADII = {
    "joint_1_s": 95.0,
    "joint_2_l": 80.0,
    "joint_3_u": 70.0,
    "joint_4_r": 55.0,
    "joint_5_b": 45.0,
    "joint_6_t": 35.0,
}


_MESH_LINKS = {
    "base_link": ("gp12_base_link.stl", (0.0, 0.0, 0.0)),
    "link_1_s": ("gp12_link_1_s.stl", (0.0, 0.0, 0.450)),
    "link_2_l": ("gp12_link_2_l.stl", (0.0, 0.0, 0.0)),
    "link_3_u": ("gp12_link_3_u.stl", (0.0, 0.0, 0.0)),
    "link_4_r": ("gp12_link_4_r.stl", (0.640, 0.0, 0.0)),
    "link_5_b": ("gp12_link_5_b.stl", (0.0, 0.0, 0.0)),
    "link_6_t": ("gp12_link_6_t.stl", (-0.100, 0.0, 0.0)),
}


def _mesh_geom(link: str, mesh_dir: str) -> str:


    entry = _MESH_LINKS.get(link)
    if entry is None:
        return ""
    name, (ox, oy, oz) = entry
    parts = []
    for tag, sub in (("visual", "visual"), ("collision", "collision")):
        parts.append(
            f'<{tag}><origin xyz="{ox:.4f} {oy:.4f} {oz:.4f}"/>'
            f'<geometry><mesh filename="{mesh_dir}/{sub}/{name}"/></geometry>'
            + ('<material name="yaskawa_blue"><color rgba="0.25 0.4 0.75 1"/></material>'
               if tag == "visual" else "")
            + f"</{tag}>"
        )
    return "".join(parts)


def write_urdf(
    path: str,
    mesh_dir: str | None = None,
    *,
    station: bool = True,
    station_sense: int | None = None,
    platter: str = "gen2",
) -> str:


    m = 1e-3
    if mesh_dir is None:
        beside = Path(path).resolve().parent / "meshes" / "ar1440"
        mesh_dir = "meshes/ar1440" if beside.is_dir() else ""
    lines = ['<?xml version="1.0"?>', '<robot name="motoman_ar1440">']
    base_geom = (
        _mesh_geom("base_link", mesh_dir)
        if mesh_dir
        else '<visual><geometry><box size="0.3 0.3 0.15"/></geometry>'
        '<origin xyz="0 0 0.075"/></visual><collision><geometry>'
        '<box size="0.3 0.3 0.15"/></geometry><origin xyz="0 0 0.075"/></collision>'
    )
    lines.append(f'  <link name="base_link">{base_geom}</link>')
    parent = "base_link"
    for i, joint in enumerate(AR1440_JOINTS):
        link = f"link_{i + 1}_{joint.name[-1]}"

        nxt = AR1440_JOINTS[i + 1].origin if i + 1 < len(AR1440_JOINTS) else (60.0, 0.0, 0.0)
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
    if station:


        from .. import station as station_mod

        chosen = {} if station_sense is None else {"sense": station_sense}
        lines.append(station_mod.urdf_fragment(
            parent_link="base_link", platter=platter, **chosen))
    lines.append("</robot>")
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


ARM_JOINT_NAMES: tuple[str, ...] = tuple(j.name for j in AR1440_JOINTS)


def urdf_joint_positions(q_deg=None, station_deg=None) -> dict[str, float]:


    out: dict[str, float] = {}
    if q_deg is not None:
        q = [float(v) for v in q_deg]
        if len(q) != 6:
            raise ValueError("q_deg is six pendant-convention degrees, S L U R B T")
        out.update({n: float(np.radians(v)) for n, v in zip(ARM_JOINT_NAMES, q)})
    if station_deg is not None:
        from .. import station as station_mod

        out[station_mod.STATION_JOINT_NAME] = float(np.radians(float(station_deg)))
    return out


def _default_urdf_path() -> Path:
    return Path(__file__).resolve().parents[3] / "robots" / "ar1440.urdf"


if __name__ == "__main__":


    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_urdf_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_urdf(str(target))
    text = target.read_text(encoding="utf-8")
    print(f"wrote {target}")
    print(f"  arm geometry: {'vendored meshes' if '<mesh' in text else 'PRIMITIVES'}")
    print(f"  station branch: {'yes' if 'joint_s1_station' in text else 'no'}")
