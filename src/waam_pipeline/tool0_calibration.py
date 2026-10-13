from __future__ import annotations

from copy import deepcopy

import numpy as np

from . import cell_lc
from .robots import ar1440


def fit_tip_offset(data):


    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or data.get("frame") != "lc50s_tool0_pulse"):
        raise ValueError("Calibration requires version 1 LC50S TOOL 0 PULSE records")

    extension = data.get("wire_extension_mm")
    uncertainty = data.get("position_uncertainty_mm")
    limit = data.get("max_offset_uncertainty_mm", 1.0)
    for name, value, allow_zero in (
        ("wire_extension_mm", extension, True),
        ("position_uncertainty_mm", uncertainty, False),
        ("max_offset_uncertainty_mm", limit, False),
    ):
        if (type(value) not in (int, float) or not np.isfinite(value)
                or value > 100 or value < 0 or (not allow_zero and value == 0)):
            qualifier = "nonnegative" if allow_zero else "positive"
            raise ValueError(f"{name} must be a measured or explicitly chosen finite {qualifier} value of at most 100 mm")

    poses = data.get("poses")
    if not isinstance(poses, list) or not 3 <= len(poses) <= 100:
        raise ValueError("Use 3 to 100 poses of the same fixed physical endpoint with different torch directions")
    axis = cell_lc._rot((0, 1, 0), -cell_lc.FLANGE_PITCH_DEG) @ [0, 0, -1]
    points, directions, rotations = [], [], []
    for i, pulses in enumerate(poses, 1):
        if (not isinstance(pulses, list) or len(pulses) != 6
                or any(type(v) not in (int, float) or not np.isfinite(v)
                       or float(v) != int(v) for v in pulses)):
            raise ValueError(f"Pose {i}: enter six whole S, L, U, R, B, T pulse values")
        try:
            flange = ar1440.fk(np.asarray(pulses, float) / ar1440.PULSES_PER_DEG)["flange"]
        except ValueError as exc:
            raise ValueError(f"Pose {i}: {exc}") from None
        points.append(flange[:3, 3] + flange[:3, :3] @ cell_lc.TOOL_TCP)
        directions.append(flange[:3, :3] @ axis)
        rotations.append(flange[:3, :3])
    points, directions = np.asarray(points), np.asarray(directions)
    centered_points = points - points.mean(axis=0)
    centered_directions = directions - directions.mean(axis=0)
    strength = float(np.linalg.norm(centered_directions))
    if strength <= 1e-10:
        raise ValueError("The torch directions are identical; these captures cannot determine the contact-tip offset")


    fit_uncertainty = float(np.sqrt(len(points)) * uncertainty / strength)
    if fit_uncertainty > limit:
        raise ValueError(
            f"Torch directions vary too little: the declared position uncertainty gives "
            f"{fit_uncertainty:.2f} mm offset uncertainty (allowed {limit:g} mm). "
            "Use more varied orientations at the same fixed endpoint."
        )


    rotations = np.asarray(rotations)
    rotation_design = (rotations - rotations.mean(axis=0)).reshape(-1, 3)
    endpoint_offset, _, rank, singular = np.linalg.lstsq(
        rotation_design, -centered_points.reshape(-1), rcond=None)
    if rank < 3 or singular[-1] <= 1e-10:
        raise ValueError("The poses do not constrain all three tool-offset components; vary the torch tilt in at least two directions")
    vector_uncertainty = float(np.sqrt(len(points)) * uncertainty / singular[-1])
    if vector_uncertainty > limit:
        raise ValueError(
            f"The poses cannot exclude a hidden lateral tool offset: the declared "
            f"position uncertainty gives {vector_uncertainty:.2f} mm vector uncertainty "
            f"(allowed {limit:g} mm). Vary the torch tilt in at least two directions."
        )
    lateral_offset = endpoint_offset - float(endpoint_offset @ axis) * axis
    lateral = float(np.linalg.norm(lateral_offset))
    if lateral > vector_uncertainty:
        raise ValueError(
            f"The pivot fit finds a {lateral:.3f} mm lateral contact-tip offset "
            f"beyond the declared {vector_uncertainty:.3f} mm uncertainty; "
            "an axial offset alone cannot reconcile this tool."
        )
    alpha = float(np.sum(centered_directions * centered_points) / strength**2)
    offset = alpha + float(extension)
    endpoints = points - alpha * directions
    fixed_point = endpoints.mean(axis=0)
    errors = np.linalg.norm(endpoints - fixed_point, axis=1)
    maximum, rms = float(errors.max()), float(np.sqrt(np.mean(errors**2)))
    if maximum > 0.5:
        raise ValueError(
            f"The reconstructed endpoint moves by up to {maximum:.3f} mm (limit 0.5 mm). "
            "Recheck that every pose uses the same physical point and wire extension."
        )
    if rms > uncertainty:
        raise ValueError(
            f"The endpoint RMS residual is {rms:.3f} mm, larger than the declared "
            f"{uncertainty:g} mm position uncertainty. Recheck the captures and uncertainty budget."
        )
    if not -100 <= offset <= 100:
        raise ValueError(f"The fitted contact-tip-to-TOOL-0 offset is {offset:.3f} mm, outside the supported -100 to 100 mm range")
    spread = float(np.degrees(np.arccos(np.clip((directions @ directions.T).min(), -1, 1))))
    return {
        "version": 1,
        "frame": "lc50s_tool0_pulse",
        "method": "fixed_endpoint_axial_contact_tip_offset",
        "tool0_tip_offset_mm": offset,
        "fixed_endpoint_robot_mm": fixed_point.tolist(),
        "max_residual_mm": maximum,
        "rms_residual_mm": rms,
        "axis_spread_deg": spread,
        "axis_fit_singular_value": strength,
        "offset_fit_uncertainty_mm": fit_uncertainty,
        "full_vector_fit_uncertainty_mm": vector_uncertainty,
        "full_vector_fit_singular_values": singular.tolist(),
        "lateral_offset_estimate_mm": lateral,
        "max_offset_uncertainty_mm": float(limit),
        "uncertainty_scope": (
            "Conditional bound from the supplied per-capture position uncertainty. "
            "It excludes uncertainty in the measured wire extension and the TOOL 0 axis model."
        ),
        "source": deepcopy(data),
        "model": {
            "tool_tcp_flange_mm": cell_lc.TOOL_TCP.tolist(),
            "wire_axis_flange": axis.tolist(),
            "pulses_per_degree": list(ar1440.PULSES_PER_DEG),
        },
    }
