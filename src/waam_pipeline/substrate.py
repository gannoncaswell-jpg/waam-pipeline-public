from __future__ import annotations

from dataclasses import dataclass, replace
import json

import numpy as np

from .touch import fit_plane


MAX_RESIDUAL_MM = 0.5
MAX_TILT_DEG = 5.0


V1_MAX_TORCH_TILT_DEG = 3.0


@dataclass(frozen=True)
class Substrate:
    points: np.ndarray
    origin: np.ndarray
    basis: np.ndarray
    corners: np.ndarray
    thickness: float
    residual: float
    tilt: float
    name: str = "bead_test_coupon"
    torch_tilt_deg: tuple[float, ...] = ()
    gap_direction: str | None = None
    tool0_tip_offset_mm: float | None = None
    physical_shift_robot: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @classmethod
    def fit(cls, points, thickness):
        p = np.asarray(points, dtype=float)
        if p.shape != (4, 3) or not np.isfinite(p).all() or np.abs(p).max() > 10000:
            raise ValueError("Substrate: enter four finite corner points in robot-base millimetres")
        if not np.isfinite(thickness) or not 0 < thickness <= 100:
            raise ValueError("Plate thickness (model only): enter more than 0 and at most 100 mm. It draws the plate below the measured top; it does not set the torch height.")
        fit = fit_plane(p)
        if fit.residual_mm > MAX_RESIDUAL_MM:
            raise ValueError(f"Corners differ from the fitted plane by {fit.residual_mm:.2f} mm (limit 0.5 mm). Recheck the touches and plate flatness.")
        if fit.tilt_deg > MAX_TILT_DEG:
            raise ValueError("Substrate tilt exceeds 5 degrees. Level the plate for the vertical-torch bead test.")
        u = p[1] - p[0]
        u -= np.dot(u, fit.normal) * fit.normal
        if np.linalg.norm(u) < 1:
            raise ValueError("Corners 1 and 2 must be distinct adjacent corners")
        u /= np.linalg.norm(u)
        basis = np.column_stack((u, np.cross(fit.normal, u), fit.normal))
        uv = ((p - fit.point) @ basis)[:, :2]
        edges = np.roll(uv, -1, axis=0) - uv
        turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
        if np.min(np.linalg.norm(edges, axis=1)) < 1 or not (np.all(turns > 1) or np.all(turns < -1)):
            raise ValueError("Enter corners 1, 2, 3, 4 around the perimeter, without crossing or repeating a corner")
        if np.all(turns < 0):
            uv = uv[::-1].copy()
        return cls(p.copy(), fit.point.copy(), basis, uv, float(thickness), fit.residual_mm, fit.tilt_deg)

    def shifted(self, offset):
        return replace(self, origin=self.origin - np.asarray(offset, float))

    def edge_distances(self, xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        edge = np.roll(self.corners, -1, axis=0) - self.corners
        delta = xy[:, None, :] - self.corners
        return (edge[:, 0] * delta[:, :, 1] - edge[:, 1] * delta[:, :, 0]) / np.linalg.norm(edge, axis=1)

    def datum(self, x, y):

        x, y = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float))
        normal = self.basis[:, 2]
        return self.origin[2] - ((x - self.origin[0]) * normal[0]
                                 + (y - self.origin[1]) * normal[1]) / normal[2]

    def distance(self, points, *, contact=False):


        origin = self.origin if contact else self.origin + self.physical_shift_robot
        q = (np.asarray(points, float).reshape(-1, 3) - origin) @ self.basis
        sides = self.edge_distances(q[:, :2])
        inside = np.all(sides >= -1e-8, axis=1)
        a, b = self.corners, np.roll(self.corners, -1, axis=0)
        ab = b - a
        t = np.clip(((q[:, None, :2] - a) * ab).sum(2) / (ab * ab).sum(1), 0, 1)
        horizontal = np.linalg.norm(q[:, None, :2] - a - t[:, :, None] * ab, axis=2).min(1)
        horizontal[inside] = -sides[inside].min(1)
        vertical = np.maximum(q[:, 2], -self.thickness - q[:, 2])
        d = np.hypot(np.maximum(horizontal, 0), np.maximum(vertical, 0)) + np.minimum(np.maximum(horizontal, vertical), 0)
        if contact:
            d[inside] = q[inside, 2]
        d[np.abs(d) < 1e-6] = 0
        return d

    def mesh(self):
        local = np.array([[x, y, z] for z in (0, -self.thickness) for x, y in self.corners])
        vertices = local @ self.basis.T + self.origin + self.physical_shift_robot
        faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6)]
        for i in range(4):
            j = (i + 1) % 4
            faces.extend(((i, i + 4, j + 4), (i, j + 4, j)))
        return vertices[np.array(faces)]

    def place(self, toolpath, *, ox=0, oy=0, bottom=0, gap=0, bead_width=8.6, edge_margin=5):
        points = np.asarray(toolpath.deposit_points())
        pivot = (points[:, :2].min(0) + points[:, :2].max(0)) / 2
        xy = points[:, :2] - pivot + [ox, oy]
        required = bead_width / (2 * self.basis[2, 2]) + edge_margin
        if np.min(self.edge_distances(xy)) < required - 1e-6:
            raise ValueError("The bead footprint crosses the substrate edge margin. Move or rotate it, or shorten the bead.")
        moves = []
        for move in toolpath.moves:
            local = np.array([move.x - pivot[0] + ox, move.y - pivot[1] + oy])
            p = self.basis[:, :2] @ local + [0, 0, move.z - bottom + gap]
            moves.append(replace(move, x=float(p[0]), y=float(p[1]), z=float(p[2]),
                                 seat=move.seat + float(self.basis[2, :2] @ local)))
        return replace(toolpath, moves=moves)

    def setup_metadata(self, platter_z, platter_center_xy):


        reference_z = float(platter_z)
        reference_xy = np.asarray(platter_center_xy, float)
        if (not np.isfinite(reference_z) or reference_xy.shape != (2,)
                or not np.isfinite(reference_xy).all()):
            raise ValueError("Surface setup needs a finite platter command height and XY center")
        command_corners = (np.column_stack((self.corners, np.zeros(len(self.corners))))
                           @ self.basis.T + self.origin)
        physical_shift = np.asarray(self.physical_shift_robot, float)
        relative_xy = command_corners[:, :2] + physical_shift[:2] - reference_xy
        heights = command_corners[:, 2] - reference_z
        return {
            "reference": "registered_platter_at_s1_home",
            "reference_command_z_robot_mm": reference_z,
            "reference_center_xy_robot_mm": reference_xy.tolist(),
            "center_xy_from_platter_center_mm":
                (self.origin[:2] + physical_shift[:2] - reference_xy).tolist(),
            "center_height_above_platter_mm": float(self.origin[2] - reference_z),
            "height_range_above_platter_mm": [float(heights.min()), float(heights.max())],
            "height_variation_mm": float(np.ptp(heights)),
            "bounds_xy_from_platter_center_mm": {
                "min": relative_xy.min(axis=0).tolist(),
                "max": relative_xy.max(axis=0).tolist(),
            },
        }

    def metadata(self):
        data = {
            "points_robot_mm": self.points.tolist(), "origin_robot_mm": self.origin.tolist(),
            "basis": self.basis.T.tolist(), "corners_uv_mm": self.corners.tolist(),
            "thickness_mm": self.thickness, "max_residual_mm": self.residual,
            "tilt_deg": self.tilt,
            "size_mm": np.ptp(self.corners, axis=0).tolist(),
        }
        if self.gap_direction is not None:
            data.update(torch_tilt_deg=list(self.torch_tilt_deg),
                        gap_direction=self.gap_direction,
                        tool0_tip_offset_mm=self.tool0_tip_offset_mm,
                        physical_shift_robot_mm=list(self.physical_shift_robot),
                        command_origin_robot_mm=self.origin.tolist(),
                        physical_origin_robot_mm=(self.origin + self.physical_shift_robot).tolist(),
                        command_points_robot_mm=self.points.tolist(),
                        points_robot_mm=(self.points + self.physical_shift_robot).tolist())
        return data


def _normal_gap_projection(contact_tips, gaps):


    tips = np.asarray(contact_tips, float)
    gaps = np.asarray(gaps, float)
    centered = tips - tips.mean(axis=0)
    measured = gaps - gaps.mean()
    slopes = np.zeros(2)
    for _ in range(80):
        direction = np.r_[slopes, 1.0]
        length = np.linalg.norm(direction)
        normal = direction / length
        residual = centered @ normal - measured
        derivative = (np.eye(3) - np.outer(normal, normal))[:, :2] / length
        jacobian = centered @ derivative
        step, _, rank, _ = np.linalg.lstsq(jacobian, -residual, rcond=None)
        if rank < 2:
            raise ValueError("The contact-tip readings do not span a substrate plane")
        if np.linalg.norm(step) < 1e-9:
            return tips - gaps[:, None] * normal
        objective = float(residual @ residual)
        scale = 1.0
        for _ in range(32):
            trial = slopes + scale * step
            trial_normal = np.r_[trial, 1.0]
            trial_normal /= np.linalg.norm(trial_normal)
            trial_residual = centered @ trial_normal - measured
            if float(trial_residual @ trial_residual) < objective:
                slopes = trial
                break
            scale *= 0.5
        else:

            if np.linalg.norm(jacobian.T @ residual) <= 1e-8:
                return tips - gaps[:, None] * normal
            raise ValueError("The perpendicular contact-tip measurements did not converge to a plane; recheck the readings")
    raise ValueError("The perpendicular contact-tip measurements did not converge to a plane; recheck the readings")


def from_request(raw, thickness):

    from . import cell_lc
    from .robots import ar1440

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise ValueError("Substrate registration must be a JSON record of at most 4 KB")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Substrate registration is not valid JSON") from None
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2, 3)
            or data.get("frame") != "lc50s_tool0_pulse"):
        raise ValueError("Substrate registration requires LC50S TOOL 0 PULSE records; pendant XYZ and user frames are not interchangeable")
    records = data.get("corners")
    if not isinstance(records, list) or len(records) != 4:
        raise ValueError("Substrate registration needs exactly four corner records")
    version = data["version"]
    direction = data.get("gap_direction")
    offset = data.get("tool0_tip_offset_mm")
    if version in (2, 3):
        if direction not in ("normal", "vertical", "torch"):
            raise ValueError("Choose whether the measured contact-tip gap is perpendicular to the substrate, vertical, or along the torch")
        if (type(offset) not in (int, float) or not np.isfinite(offset)
                or not -100 <= offset <= 100):
            raise ValueError("Enter the verified contact-tip-to-TOOL-0 axial offset from -100 to 100 mm; weld CTWD is not that calibration")
    points = []
    axes, gaps, tilts = [], [], []
    from .clearance import TorchStackup
    target = data.get("weld_ctwd_mm")
    expected = TorchStackup.from_profile().ctwd_mm
    if type(target) not in (float, int) or not np.isfinite(target) or abs(target - expected) > 1e-6:
        raise ValueError(f"Weld contact-tip distance must match the LC profile ({expected:g} mm). Reopen the registration form.")
    for i, corner in enumerate(records, 1):
        if not isinstance(corner, dict):
            raise ValueError(f"Corner {i}: enter a pulse record and measured contact-tip distance")
        record = corner.get("pulses")
        if (not isinstance(record, list) or len(record) != 6 or
                any(type(v) not in (float, int) or not np.isfinite(v) or float(v) != int(v) for v in record)):
            raise ValueError(f"Corner {i}: enter six whole PULSE values in S, L, U, R, B, T order")
        q = np.asarray(record, float) / ar1440.PULSES_PER_DEG
        try:
            pose = ar1440.fk(q)["flange"]
        except ValueError as exc:
            raise ValueError(f"Corner {i}: {exc}") from None
        wire_axis = cell_lc._rot((0, 1, 0), -cell_lc.FLANGE_PITCH_DEG) @ [0, 0, -1]
        wire_down = pose[:3, :3] @ wire_axis
        tilt = float(np.degrees(np.arccos(np.clip(-wire_down[2], -1.0, 1.0))))
        if version == 1 and tilt > V1_MAX_TORCH_TILT_DEG:
            raise ValueError(
                f"Corner {i}: TOOL 0 is {tilt:.1f} deg off vertical (v1 limit "
                f"{V1_MAX_TORCH_TILT_DEG:g} deg). Record the contact-tip offset "
                f"and register as version 2 to use a more tilted pose.")
        gap = corner.get("tip_distance_mm")
        if type(gap) not in (float, int) or not np.isfinite(gap) or not 0 <= gap <= 100:
            raise ValueError(f"Corner {i}: enter the measured contact-tip distance from 0 to 100 mm")
        point = cell_lc.tcp_from_pulses(record)
        if version == 1:
            point[2] += target - gap
        points.append(point)
        axes.append(wire_down)
        gaps.append(gap)
        tilts.append(tilt)
    if version == 1:
        return Substrate.fit(points, thickness)


    axes = np.asarray(axes)
    gaps = np.asarray(gaps)
    tips = np.asarray(points) - float(offset) * axes
    if direction == "normal":
        surface_points = _normal_gap_projection(tips, gaps)
    elif direction == "vertical":
        surface_points = tips - gaps[:, None] * [0, 0, 1]
    else:
        surface_points = tips + gaps[:, None] * axes

    datum_shift = np.array([0.0, 0.0, target - float(offset)])
    plate = Substrate.fit(surface_points + datum_shift, thickness)
    normal = plate.basis[:, 2]
    away = np.flatnonzero(axes @ normal >= -1e-8)
    if len(away):
        raise ValueError(f"Corner {int(away[0]) + 1}: the torch points away from the substrate surface")
    if direction == "normal":
        actual_gaps = (tips - (plate.origin - datum_shift)) @ normal
        if np.max(np.abs(actual_gaps - gaps)) > MAX_RESIDUAL_MM:
            raise ValueError("The perpendicular contact-tip measurements differ from the fitted plane by more than 0.5 mm")
    plate = replace(plate, torch_tilt_deg=tuple(tilts), gap_direction=direction,
                    tool0_tip_offset_mm=float(offset),
                    physical_shift_robot=tuple(-datum_shift))
    if version == 3:
        from . import sensed

        readings = data.get("sensed")
        if not isinstance(readings, dict):
            raise ValueError("Sensed substrate registration needs a base position and touch PULSE records")
        base = readings.get("base")
        if type(base) is not int or not 0 <= base <= 225:
            raise ValueError("Sensed substrate base position must be a whole number from 0 to 225")
        fit = readings.get("fit", "smooth")
        if fit not in ("smooth", "interpolate"):
            raise ValueError("Sensed fit must be smooth or interpolate")
        return sensed.fit_sensed(plate, readings.get("points"), base=base, fit=fit)
    return plate
