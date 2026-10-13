from __future__ import annotations

from dataclasses import dataclass, field
import re

import numpy as np

from . import cell_lc, station, touch
from .backends import jbi, jbi_read
from .robots import ar1440

SETUP_BASE = 40
TOUCH_BASE = 44
ROLES = ("top", "top", "top", "top", "side", "side", "end", "end")
STANDOFF_MM = 10.0
OVERTRAVEL_MM = touch.SEARCH_OVERTRAVEL_MM
SIDE_DEPTH_MM = (2.0, 4.0)
DEFAULT_CLEAR_MM = 50.0
MAX_TOUCH_SHIFT_MM = 5.0
MAX_TOP_SHIFT_MM = 1.5


MAX_SETUP_PLANE_MM = 1.0
MAX_TOP_OUTLIER_MM = 0.8
MAX_TILT_DEG = 0.3
MAX_YAW_DEG = 2.0
MAX_RESIDUAL_MM = 0.3
MAX_JOINT_CHANGE_DEG = 3.0
LINE_TOL_MM = 0.5
STATION_PULSES_PER_DEG = 4096 * station.GEAR_RATIO / 360.0
PROFILE_VERSION = 1


@dataclass(frozen=True)
class DatumProbe(touch.Probe):
    role: str = "top"
    normal: np.ndarray | None = None
    via: np.ndarray | None = None


@dataclass(frozen=True)
class BlockDatum:
    contacts: np.ndarray
    top_normal: np.ndarray
    top_point: np.ndarray
    side_in: np.ndarray
    end_in: np.ndarray
    clear_z: float
    depths_mm: np.ndarray


def _records(records, base, what):
    if not isinstance(records, list) or len(records) != len(ROLES):
        raise ValueError(f"{what}: expected exactly 8 pulse records")
    out = []
    for i, record in enumerate(records):
        if (not isinstance(record, list) or len(record) != 6
                or any(type(v) is not int for v in record)):
            raise ValueError(f"P{base + i:03d}: enter six whole PULSE values in S, L, U, R, B, T order")
        out.append(list(record))
    return out


def _points(records, base, what):
    points = []
    for i, record in enumerate(_records(records, base, what)):
        try:
            points.append(cell_lc.tcp_from_pulses(record))
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"P{base + i:03d}: {exc}") from None
    return np.asarray(points)


def _distance(value, name, low, high):
    if type(value) not in (int, float) or not np.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number from {low:g} to {high:g} mm")
    return float(value)


def _held_out(values):


    worst = 0.0
    for i in range(4):
        keep = [k for k in range(4) if k != i]
        design = np.column_stack([np.ones(3), values[keep, :2]])
        if np.linalg.matrix_rank(design) < 3:
            continue
        coefficients = np.linalg.lstsq(design, values[keep, 2], rcond=None)[0]
        worst = max(worst, abs(float(values[i, 2] - np.r_[1.0, values[i, :2]] @ coefficients)))
    return worst


def _top_plane(points, base):
    top = touch.fit_plane(points)
    miss = _held_out(points)
    if miss > MAX_SETUP_PLANE_MM:
        raise ValueError(f"P{base:03d}-P{base + 3:03d}: the four top touches are not on one flat face "
                         f"(top plane residual {miss:.2f} mm exceeds {MAX_SETUP_PLANE_MM:g} mm). "
                         "One hit a burr or spatter; touch the top face clear of both.")
    return top


def _top_outliers(reference, displacements, labels):

    miss = _held_out(np.column_stack([reference[:4, :2], displacements[:4]]))
    if miss > MAX_TOP_OUTLIER_MM:
        raise ValueError(f"{labels[0]}-{labels[3]}: the four top touches disagree by {miss:.2f} mm, more "
                         "than a rigid move allows. One hit spatter or a burr: brush the spots and run again.")


def datum_from_setup(records, *, clear_mm=DEFAULT_CLEAR_MM) -> BlockDatum:

    clear = _distance(clear_mm, "clear_mm", 20, 200)
    points = _points(records, SETUP_BASE, "setup P040-P047")
    try:
        top = _top_plane(points[:4], SETUP_BASE)
    except ValueError as exc:
        raise ValueError(f"P040-P043 top plane: {exc}") from None
    if top.tilt_deg > 3.0:
        raise ValueError(f"P040-P043: top plane tilt {top.tilt_deg:.2f} deg exceeds 3 deg")

    def inward(first, spacing, label):
        delta = points[first + 1, :2] - points[first, :2]
        length = float(np.linalg.norm(delta))
        if length < spacing:
            raise ValueError(f"{label}: contacts need at least {spacing:g} mm horizontal spacing")
        normal = np.array([-delta[1], delta[0], 0.0]) / length
        if normal @ (top.point - points[first]) < 0:
            normal = -normal
        return normal

    side = inward(4, 20, "P044-P045 side")
    end = inward(6, 10, "P046-P047 end")
    angle = float(np.degrees(np.arccos(np.clip(side @ end, -1, 1))))
    if not 75 <= angle <= 105:
        raise ValueError(f"Side/end faces must be perpendicular within 15 deg (angle {angle:.2f} deg)")
    for i, point in enumerate(points[:4]):
        for normal, first, face in ((side, 4, "side"), (end, 6, "end")):
            if (point - points[first]) @ normal < 8:
                raise ValueError(f"P{SETUP_BASE + i:03d}: top contact must be at least 8 mm inside the {face} face")
    depths = (top.point - points[4:]) @ top.normal
    for i, depth in enumerate(depths, 4):
        if not SIDE_DEPTH_MM[0] <= depth <= SIDE_DEPTH_MM[1]:
            raise ValueError(f"P{SETUP_BASE + i:03d}: depth {depth:.2f} mm; "
                             "touch the side/end faces 2-4 mm below the top edge")
    clear_z = max(top.height_at(p[0], p[1]) for p in points) + clear
    return BlockDatum(points, top.normal, top.point, side, end, float(clear_z), depths)


def touch_plan(datum, *, standoff_mm=STANDOFF_MM, overtravel_mm=OVERTRAVEL_MM) -> list[DatumProbe]:
    standoff = _distance(standoff_mm, "standoff_mm", 5, 25)
    if (type(overtravel_mm) not in (int, float) or not np.isfinite(overtravel_mm)
            or not 0 < overtravel_mm < 8.9):
        raise ValueError("overtravel_mm must be positive and under the 8.9 mm CLEAN_R1 stick-out")
    probes = []
    names = ("top1", "top2", "top3", "top4", "side1", "side2", "end1", "end2")
    for name, role, contact in zip(names, ROLES, datum.contacts):
        d = np.array([0.0, 0.0, -1.0]) if role == "top" else getattr(datum, f"{role}_in")
        normal = datum.top_normal if role == "top" else d
        if role != "top":
            normal = normal - (normal @ datum.top_normal) * datum.top_normal
            normal = normal / np.linalg.norm(normal)
        start = contact - standoff * d
        if datum.clear_z < start[2] + 5.0:
            raise ValueError(f"{name}: the clear height must sit at least 5 mm above its approach point")
        probes.append(DatumProbe(name=name, start=start, target=contact + overtravel_mm * d,
                                 expected=contact.copy(), role=role, normal=normal.copy(),
                                 via=np.array([start[0], start[1], datum.clear_z])))
    return probes


def setup_summary(datum, plan) -> dict:
    return {"clear_z": float(datum.clear_z), "depths_mm": datum.depths_mm.tolist(),
            "face_angle_deg": float(np.degrees(np.arccos(np.clip(datum.side_in @ datum.end_in, -1, 1)))),
            "top_tilt_deg": float(np.degrees(np.arccos(np.clip(datum.top_normal[2], -1, 1)))),
            "probes": [{"name": p.name, "role": p.role, "start": p.start.tolist(),
                        "via": p.via.tolist(), "expected": p.expected.tolist()} for p in plan]}


@dataclass(frozen=True)
class BlockShift:
    transform: np.ndarray
    translation_mm: np.ndarray
    rotation_deg: np.ndarray
    displacements_mm: np.ndarray
    residuals_mm: np.ndarray
    labels: tuple[str, ...]
    center_mm: np.ndarray = field(default_factory=lambda: np.zeros(3))

    @property
    def center_shift_mm(self) -> np.ndarray:


        c = np.asarray(self.center_mm, float)
        return self.transform[:3, :3] @ c + self.transform[:3, 3] - c

    def metadata(self) -> dict:
        return {"transform": self.transform.tolist(), "translation_mm": self.translation_mm.tolist(),
                "rotation_deg": self.rotation_deg.tolist(), "center_mm": np.asarray(self.center_mm).tolist(),
                "center_shift_mm": self.center_shift_mm.tolist(),
                "displacements_mm": self.displacements_mm.tolist(),
                "residuals_mm": self.residuals_mm.tolist(), "labels": list(self.labels)}


def _rotation(rotation_deg):
    vector = np.radians(rotation_deg)
    theta = float(np.linalg.norm(vector))
    x, y, z = vector
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    a = np.sinc(theta / np.pi)
    b = 0.5 * np.sinc(theta / (2 * np.pi)) ** 2
    return np.eye(3) + a * skew + b * (skew @ skew)


def check_touches(plan, records, *, base=TOUCH_BASE) -> np.ndarray:

    plan = list(plan)
    if len(plan) != len(ROLES):
        raise ValueError("The block fit needs exactly 8 probes in T1,T2,T3,T4,S1,S2,E1,E2 order")
    if type(base) is not int or not 0 <= base <= 248:
        raise ValueError("The touch result base must be a whole number from 0 to 248")
    points = _points(records, base, "new touches")
    for i, (probe, point) in enumerate(zip(plan, points)):
        direction = probe.direction
        delta = point - probe.start
        along = float(delta @ direction)
        off = float(np.linalg.norm(delta - along * direction))
        if off > LINE_TOL_MM or not -0.5 <= along <= probe.travel_mm + 0.5:
            raise ValueError(f"P{base + i:03d} new touch: not from this touch job "
                             "(off the search line or outside its window)")
    return points


def fit_shift(plan, reference_records, new_records, *, base=TOUCH_BASE) -> BlockShift:

    plan = list(plan)
    try:
        reference = check_touches(plan, reference_records, base=base)
    except ValueError as exc:
        message = str(exc).replace("new touches:", "reference:", 1).replace(" new touch:", " reference:", 1)
        raise ValueError(message) from None
    measured = check_touches(plan, new_records, base=base)
    labels = tuple(f"P{base + i:03d}" for i in range(len(plan)))
    directions = np.array([p.direction for p in plan])
    normals = np.asarray([p.normal for p in plan], float)
    if (normals.shape != (len(ROLES), 3) or not np.isfinite(normals).all()
            or not np.allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-6)):
        raise ValueError("Each datum probe needs a unit face normal")
    displacements = np.sum((measured - reference) * directions, axis=1)
    for i, probe in enumerate(plan):
        if probe.role == "top" and abs(displacements[i]) > MAX_TOP_SHIFT_MM:
            raise ValueError(f"{labels[i]}: the block top moved {-displacements[i]:+.2f} mm; "
                             "a block seated flat cannot rise or sink that much. "
                             "Check for debris under it and re-seat it.")
    worst = int(np.argmax(np.abs(displacements)))
    if abs(displacements[worst]) > MAX_TOUCH_SHIFT_MM:
        raise ValueError(f"{labels[worst]}: touch moved {displacements[worst]:+.2f} mm; limit is 5 mm")
    _top_outliers(reference, displacements, labels)

    def predict(parameters):
        rotation = _rotation(parameters[3:])
        moved_normals = normals @ rotation.T
        denominators = np.sum(directions * moved_normals, axis=1)
        if np.any(np.abs(denominators) < 1e-6):
            raise ValueError("Block fit has a face parallel to its search line")
        moved = reference @ (rotation - np.eye(3)).T + parameters[:3]
        return np.sum(moved * moved_normals, axis=1) / denominators

    parameters = np.zeros(6)

    steps = np.array([1e-4] * 3 + [1e-5] * 3)
    for _ in range(20):
        predicted = predict(parameters)
        jacobian = np.column_stack([
            (predict(parameters + step) - predict(parameters - step)) / (2 * steps[i])
            for i, step in enumerate(np.diag(steps))
        ])
        singular = np.linalg.svd(jacobian, compute_uv=False)
        if singular[0] == 0 or singular[-1] / singular[0] < 1e-4:
            raise ValueError("Block touch layout is rank-deficient; spread the contacts farther apart")
        delta = np.linalg.lstsq(jacobian, displacements - predicted, rcond=None)[0]
        parameters += delta
        old_cost = float(np.sum((displacements - predicted) ** 2))
        new_cost = float(np.sum((displacements - predict(parameters)) ** 2))
        if np.linalg.norm(delta) < 1e-7 or abs(new_cost - old_cost) < 1e-12 * max(1.0, old_cost):
            break
    else:
        raise ValueError("Block fit did not converge; recheck the touch records")
    residuals = displacements - predict(parameters)
    worst = int(np.argmax(np.abs(residuals)))
    if abs(residuals[worst]) > MAX_RESIDUAL_MM:
        raise ValueError(f"{labels[worst]}: fit residual {residuals[worst]:+.2f} mm exceeds 0.3 mm")
    rotation = _rotation(parameters[3:])
    normal = normals[0]
    tilt = float(np.degrees(np.arccos(np.clip((rotation @ normal) @ normal, -1, 1))))
    if tilt > MAX_TILT_DEG:
        raise ValueError("block not seated flat, or S1 not at 0: top normal tilt exceeds 0.3 deg")
    if abs(parameters[3:] @ normal) > MAX_YAW_DEG:
        raise ValueError("Block yaw about the top normal exceeds 2 deg; re-seat it closer")
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = parameters[:3]
    return BlockShift(transform, parameters[:3].copy(), parameters[3:].copy(), displacements, residuals, labels,
                      center_mm=reference.mean(axis=0))


_MOTION = re.compile(r"(MOVJ|MOVL|MOVC|MOVS) C(\d{5})( EC(\d{5}))?( .*)?")
_UNSAFE = re.compile(
    r"\b(?:SRCH|UNTIL|IMOV\w*|SMOV[LCJ]\w*|SFTON|SFTOF|MSHIFT|SREFP)\b|\+MOV"
    r"|\b(?:PX|LP|BP|EX|P)(?:\d|\[|\()|\b(?:UF#|BF|RF|TF)(?![A-Z_])",
    re.IGNORECASE,
)


def _source_job(source_text):
    if not isinstance(source_text, str) or "\n" not in source_text:
        raise ValueError("source_job must contain JBI text with a NOP..END instruction block")
    try:
        source_text.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("source_job text must be ASCII") from None
    job = jbi_read.read_jbi(source_text)
    jbi._check_job_name(job.name, what="source job name")
    if job.pos_type != "PULSE":
        raise ValueError("Only ///POSTYPE PULSE class jobs can be re-indexed")
    if job.groups not in (["RB1"], ["RB1", "ST1"]):
        raise ValueError("Class job groups must be exactly RB1 or RB1,ST1")
    records = {}
    counts = {"C": 0, "EC": 0}
    for line in job.header_lines:
        stripped = line.strip()
        if stripped.split(None, 1)[0] == "///LVARS":
            raise ValueError("Local variable declarations (///LVARS) are not supported in a class job")
        if stripped.startswith("/"):
            continue
        match = re.fullmatch(r"(C|EC)(\d{5})=([+-]?\d+(?:,[+-]?\d+)*)", stripped)
        if match is None:
            raise ValueError(f"Unsupported position record: {stripped}")
        kind, index, values = match.groups()
        key = kind + index
        if key in records:
            raise ValueError(f"{key}: duplicate position record")
        pulses = [int(v) for v in values.split(",")]
        if len(pulses) != (6 if kind == "C" else 1):
            raise ValueError(f"{key}: expected {'six' if kind == 'C' else 'one'} integer pulse values")
        records[key] = pulses
        counts[kind] += 1
    if not job.uses_station_axis and counts["EC"]:
        raise ValueError("EC records require group ST1")
    expected_npos = (counts["C"], 0, counts["EC"], 0, 0, 0)
    if job.npos != expected_npos:
        raise ValueError("///NPOS must match the C and EC record counts with all other counts zero")
    for directive in ("//NAME", "///POSTYPE", "///NPOS", "///DATE"):
        if sum(line.split(None, 1)[0] == directive for line in job.header_lines) != 1:
            raise ValueError(f"Class job needs exactly one {directive} record")
    uses, calls = {}, []
    arc_on, previous_ec = False, None
    for line in job.instructions:
        if line.startswith("'"):
            continue
        code = re.sub(r'"[^"\n]*"', '""', line)
        for call in re.finditer(r"\b(CALL|JUMP)\s+JOB:([^\s]*)", code, re.IGNORECASE):
            kind, target = call.groups()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", target) is None:
                raise ValueError(f"CALL/JUMP JOB: target must be a plain static name: {line}")
            entry = {"kind": kind.upper(), "job": target}
            if entry not in calls:
                calls.append(entry)
        scan = re.sub(r"\bJOB:[A-Za-z0-9_-]+", "JOB:", code)
        if _UNSAFE.search(scan):
            raise ValueError(f"Unsupported search, variable, frame, or shifted motion: {line}")
        arc = re.match(r"\s*(ARCON|ARCOF)\b", code, re.IGNORECASE)
        if arc is not None:
            arc_on = arc[1].upper() == "ARCON"
        if re.search(r"\b(?:MOV\w*|SMOV\w*|IMOV\w*)\b", scan, re.IGNORECASE):
            motion = _MOTION.fullmatch(line)
            if motion is None:
                raise ValueError(f"Unsupported motion: {line}")
            c = "C" + motion[2]
            ec = None if motion[4] is None else "EC" + motion[4]
            if c not in records or (ec is not None and ec not in records):
                raise ValueError(f"Motion references a missing C/EC record: {line}")
            if (ec is not None) != job.uses_station_axis:
                raise ValueError(f"Every motion must carry EC exactly when ST1 is in the groups: {line}")
            if c in uses:
                raise ValueError(f"{c}: each C record must be used by exactly one motion line")
            station_pulses = None if ec is None else records[ec][0]
            if arc_on and station_pulses != previous_ec:
                raise ValueError(f"S1 moves during a weld at {line}: "
                                 "an endpoint correction is not enough there; re-teach that weld")
            previous_ec = station_pulses
            uses[c] = ec
    if set(uses) != {key for key in records if key.startswith("C")}:
        raise ValueError("Each C record must be used by exactly one motion line")
    return job, records, uses, calls


def source_summary(text) -> dict:
    job, records, _, calls = _source_job(text)
    return {"name": job.name, "groups": list(job.groups),
            "points": sum(key.startswith("C") for key in records),
            "station_records": sum(key.startswith("EC") for key in records), "calls": calls}


def reindex_job(source_text, shift, *, name, date, folder=None) -> tuple[str, dict]:

    job, records, uses, calls = _source_job(source_text)
    name = jbi._check_job_name(name)
    if name.upper() == job.name.upper():
        raise ValueError(f"{name} would replace the class job; choose a new name")
    date = jbi._inform_date(date)
    folders = jbi._folder_lines(folder)
    rates = np.asarray(ar1440.PULSES_PER_DEG)
    replacements, points = {}, []
    for key, ec in uses.items():
        old = records[key]
        q = np.asarray(old) / rates
        theta = 0.0 if ec is None else records[ec][0] / STATION_PULSES_PER_DEG
        frame = station.station_transform(theta)
        correction = frame @ shift.transform @ np.linalg.inv(frame)
        if np.allclose(correction, np.eye(4), rtol=0, atol=1e-9):
            new, change = old, 0.0
        else:
            if abs(q[4]) < 1.0:
                raise ValueError(f"{key} is at a wrist singularity (B ~ 0 deg); re-teach it by hand.")
            target = correction @ ar1440.fk(q)["flange"]
            solutions = ar1440.ik(target, all_solutions=True, tol_mm=0.01)
            if not solutions:
                raise ValueError(f"{key}: corrected pose is unreachable")
            nearest = np.asarray(min(solutions, key=lambda s: float(np.abs(np.asarray(s) - q).sum())))
            change = float(np.abs(nearest - q).max())
            if change > MAX_JOINT_CHANGE_DEG:
                raise ValueError(f"{key}: joint change {change:.3f} deg exceeds 3 deg")
            new = np.rint(nearest * rates).astype(int).tolist()
            try:
                actual = ar1440.fk(np.asarray(new) / rates)["flange"]
            except ValueError as exc:
                raise ValueError(f"{key}: rounded pulse pose: {exc}") from None
            error_mm = float(np.linalg.norm(actual[:3, 3] - target[:3, 3]))
            error_deg = float(np.degrees(np.arccos(np.clip(
                (np.trace(actual[:3, :3].T @ target[:3, :3]) - 1) / 2, -1, 1))))
            if error_mm > 0.02 or error_deg > 0.01:
                raise ValueError(f"{key}: rounded pulse pose misses target by {error_mm:.4f} mm / {error_deg:.4f} deg")
            replacements[key] = ",".join(str(v) for v in new)
        before = cell_lc.tcp_from_pulses(old)
        after = cell_lc.tcp_from_pulses(new)
        points.append({"record": key, "station_deg": float(theta), "tcp_before": before.tolist(),
                       "tcp_after": after.tolist(), "moved_mm": float(np.linalg.norm(after - before)),
                       "max_joint_change_deg": change})
    header = []
    for line in job.header_lines:
        stripped = line.strip()
        directive = stripped.split(None, 1)[0]
        if directive == "//NAME":
            header.extend([f"//NAME {name}", *folders])
        elif directive == "///FOLDERNAME":
            continue
        elif directive == "///DATE":
            header.append(f"///DATE {date}")
        elif (key := stripped.partition("=")[0]) in replacements:
            header.append(f"{key}={replacements[key]}")
        else:
            header.append(line)
    comments = [jbi._comment(c) for c in (
        "REINDEXED COPY - REVIEW REQ", f"from {job.name}",
        f"shift {np.linalg.norm(shift.center_shift_mm):.2f} mm {np.linalg.norm(shift.rotation_deg):.2f} deg",
        f"waam-pipeline {jbi._git_sha()}", "S1 records copied verbatim",
    )]
    raw = source_text.splitlines()
    begin = next(i for i, line in enumerate(raw) if line.strip() == "NOP")
    end = next(i for i in range(begin, len(raw)) if raw[i].strip() == "END")
    if any(line.strip() for line in raw[end + 1:]):
        raise ValueError("Class job has unexpected data after END")
    instructions = raw[begin:end + 1]
    output = "\r\n".join(header + instructions[:1] + comments + instructions[1:]) + "\r\n"
    output.encode("ascii")
    check = jbi_read.read_jbi(output)
    if (check.name != name or check.npos != job.npos
            or {k: v for k, v in check.positions.items() if k.startswith("EC")}
            != {k: v for k, v in job.positions.items() if k.startswith("EC")}
            or check.instructions[:1] + check.instructions[1 + len(comments):] != job.instructions):
        raise ValueError("Re-indexed job failed its round-trip invariants")
    return output, {"source": job.name, "name": name, "calls": calls, "shift": shift.metadata(), "points": points}


def _keys(obj, required, optional, what):
    if not isinstance(obj, dict):
        raise ValueError(f"{what} must be an object")
    unknown = set(obj) - required - optional
    missing = required - set(obj)
    if unknown or missing:
        raise ValueError(f"{what}: unknown keys {sorted(unknown, key=str)}; missing keys {sorted(missing)}")


def validate_profile(obj) -> dict:

    _keys(obj, {"version", "kind", "setup", "clear_mm", "reference", "source_job"},
          {"notes", "saved_at"}, "profile")
    if type(obj["version"]) is not int or obj["version"] != PROFILE_VERSION:
        raise ValueError(f"profile version must be {PROFILE_VERSION}")
    if obj["kind"] != "manpro-reindex":
        raise ValueError("profile kind must be manpro-reindex")
    out = {"version": PROFILE_VERSION, "kind": "manpro-reindex"}
    for key, base in (("setup", SETUP_BASE), ("reference", TOUCH_BASE)):
        value = obj[key]
        if key == "reference" and value is None:
            out[key] = None
        else:
            _keys(value, {"records"}, set(), key)
            out[key] = {"records": _records(value["records"], base, key)}
    out["clear_mm"] = _distance(obj["clear_mm"], "clear_mm", 20, 200)
    source = obj["source_job"]
    out["source_job"] = None
    if source is not None:
        _keys(source, {"name", "text"}, set(), "source_job")
        if not isinstance(source["name"], str):
            raise ValueError("source_job name must be a string")
        text = source["text"]
        if not isinstance(text, str) or len(text) > 262144 or not text.isascii():
            raise ValueError("source_job text must be ASCII and at most 262144 characters")
        out["source_job"] = dict(source)
    for key in ("notes", "saved_at"):
        if key in obj:
            if not isinstance(obj[key], str) or (key == "notes" and len(obj[key]) > 500):
                raise ValueError(f"{key} must be a string" + (" of at most 500 characters" if key == "notes" else ""))
            out[key] = obj[key]
    return out


def plan_from_profile(profile) -> tuple[BlockDatum, list[DatumProbe]]:
    profile = validate_profile(profile)
    datum = datum_from_setup(profile["setup"]["records"], clear_mm=profile["clear_mm"])
    return datum, touch_plan(datum)


def reindex_from_profile(profile, new_records, *, name, date, folder=None) -> tuple[str, dict]:
    profile = validate_profile(profile)
    if profile["reference"] is None or profile["source_job"] is None:
        raise ValueError("Re-indexing requires both reference touches and a source_job in the profile")
    _, plan = plan_from_profile(profile)
    shift = fit_shift(plan, profile["reference"]["records"], new_records)
    return reindex_job(profile["source_job"]["text"], shift, name=name, date=date, folder=folder)
