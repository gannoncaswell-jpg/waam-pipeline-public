from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..plan import HC_PARK_Q, PARK_Q, MeasureStop, build_plan
from ..robots.ar1440 import AR1440_JOINTS, Joint, PULSES_PER_DEG
from ..robots.ar1440 import AIR_MM_MIN as AR1440_AIR_MM_MIN
from ..robots.ar1440 import V_TOKEN_MM_PER_MIN as AR1440_V_UNIT_MM_PER_MIN
from ..robots.hc10 import HC10DTP_JOINTS
from ..robots.hc10 import PULSES_PER_DEG as HC10_PULSES_PER_DEG
from ..robots.hc10 import AIR_MM_MIN as HC10_AIR_MM_MIN
from ..robots.hc10 import V_TOKEN_MM_PER_MIN as HC10_V_UNIT_MM_PER_MIN
from ..toolpath import Toolpath
from .jbi import _V_TOKEN_RANGE, DEFAULT_WELD_MM_MIN, _git_sha, v_token


JOINT_NAMES: tuple[str, ...] = tuple(j.name for j in AR1440_JOINTS)


MAX_POINTS_PER_TRAJECTORY = 200
MIN_POINTS_PER_TRAJECTORY = 2


MODE_GOAL = "goal"
MODE_QUEUE = "queue"
JOB_FORMAT_VERSION_GOALS = 1
JOB_FORMAT_VERSION_POINT_QUEUE = 2


MIN_POINT_DT_S = 0.002


ASSUMED_INTERPOLATION_PERIOD_S = 0.004


START_MAX_PULSE_DEVIATION = 30
START_TOLERANCE_SAFETY = 0.8


@dataclass(frozen=True)
class RobotModel:


    name: str
    joints: tuple[Joint, ...]
    park_q: tuple[float, ...]
    v_unit_mm_per_min: float
    air_mm_min: float
    pulses_per_deg: tuple[float, ...] | None = None

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(j.name for j in self.joints)

    def start_tolerance_deg(self) -> tuple[float, ...]:


        if self.pulses_per_deg is None:
            return ()
        return tuple(
            round(START_MAX_PULSE_DEVIATION / rate * START_TOLERANCE_SAFETY, 6)
            for rate in self.pulses_per_deg
        )


AR1440_MODEL = RobotModel(name="AR1440", joints=AR1440_JOINTS, park_q=PARK_Q,
                          v_unit_mm_per_min=AR1440_V_UNIT_MM_PER_MIN, air_mm_min=AR1440_AIR_MM_MIN,
                          pulses_per_deg=PULSES_PER_DEG)


HC10_MODEL = RobotModel(name="HC10", joints=HC10DTP_JOINTS, park_q=HC_PARK_Q,
                        v_unit_mm_per_min=HC10_V_UNIT_MM_PER_MIN, air_mm_min=HC10_AIR_MM_MIN,
                        pulses_per_deg=HC10_PULSES_PER_DEG)


START_TOLERANCE_DEG: tuple[float, ...] = AR1440_MODEL.start_tolerance_deg()


YRC1000_WRITABLE_BIT_RANGES = (
    (10010, 15127),
    (27010, 29567),
    (60010, 60647),
)


class Ros2Refused(RuntimeError):


    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class IoPoint:


    address: int
    value: int
    name: str = ""

    def errors(self) -> list[str]:
        e: list[str] = []
        if self.value not in (0, 1):
            e.append(f"{self.address}: value must be 0 or 1, got {self.value!r}")


        if self.address % 10 > 7:
            e.append(f"{self.address}: last digit may not be 8 or 9")
        if not any(lo <= self.address <= hi for lo, hi in YRC1000_WRITABLE_BIT_RANGES):
            e.append(
                f"{self.address}: outside every address range MotoROS2 will "
                "write on a YRC1000 (10010-15127 general out, 27010-29567 "
                "network in, 60010-60647 interface panel)"
            )
        return e


@dataclass(frozen=True)
class ArcIo:


    verified: bool = False
    controller: str = "YRC1000"
    on: tuple[IoPoint, ...] = ()
    off: tuple[IoPoint, ...] = ()
    source: str = ""
    reviewed_by: tuple[str, ...] = ()
    settle_s: float = 0.0

    def validation_errors(self) -> list[str]:
        e: list[str] = []
        if self.controller.upper() != "YRC1000":
            e.append(f"controller {self.controller!r}: only YRC1000 address "
                     "ranges are transcribed here")
        if not self.on:
            e.append("`on` lists no writes, so nothing would strike the arc")
        if not self.off:
            e.append("`off` lists no writes, so nothing would STOP the arc")
        for p in self.on + self.off:
            e.extend(p.errors())


        unreturned = {p.address for p in self.on} - {p.address for p in self.off}
        if unreturned:
            e.append(
                "these addresses are set by `on` and never written by `off`, "
                "so the arc would not be extinguished: "
                + ", ".join(str(a) for a in sorted(unreturned))
            )
        for p in self.off:
            same = [q for q in self.on if q.address == p.address]
            if same and same[0].value == p.value:
                e.append(f"{p.address}: `off` writes the same value as `on` "
                         f"({p.value}), so it changes nothing")
        if self.settle_s < 0 or self.settle_s > 10:
            e.append(f"settle_s must be 0..10 s, got {self.settle_s}")


        if self.verified is True and len(self.reviewed_by) < 2:
            e.append(
                "verified=true needs at least two reviewers in reviewed_by "
                f"(got {list(self.reviewed_by)}): the gate is a TWO-person "
                "check of these addresses against the controller's own I/O names"
            )
        return e

    @classmethod
    def load(cls, path: str | Path) -> "ArcIo":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}

        def points(key: str) -> tuple[IoPoint, ...]:
            out = []
            for item in raw.get(key) or ():
                out.append(IoPoint(address=int(item["address"]),
                                   value=int(item["value"]),
                                   name=str(item.get("name", ""))))
            return tuple(out)

        return cls(


            verified=raw.get("verified") is True,
            controller=str(raw.get("controller", "YRC1000")),
            on=points("on"),
            off=points("off"),
            source=str(raw.get("source", "")),
            reviewed_by=tuple(str(r) for r in (raw.get("reviewed_by") or ())),
            settle_s=float(raw.get("settle_s", 0.0)),
        )


@dataclass(frozen=True)
class TrajPoint:
    positions_rad: tuple[float, ...]
    velocities_rad_s: tuple[float, ...]
    time_from_start_s: float


@dataclass(frozen=True)
class Segment:


    label: str
    points: tuple[TrajPoint, ...]
    welding: bool
    stretched: tuple[str, ...] = ()
    mode: str = MODE_GOAL

    @property
    def duration_s(self) -> float:
        return self.points[-1].time_from_start_s

    @property
    def queued(self) -> bool:
        return self.mode == MODE_QUEUE


@dataclass(frozen=True)
class Wait:
    seconds: float
    label: str


@dataclass(frozen=True)
class IoStep:


    writes: tuple[IoPoint, ...]
    arc: str
    label: str


CLEARANCE_GUARDED = "yes (guarded)"
CLEARANCE_UNCHECKED = "NO - UNCHECKED"


@dataclass(frozen=True)
class Ros2Job:
    name: str
    joint_names: tuple[str, ...]
    steps: tuple[object, ...]
    hold_axes: tuple[str, ...]
    welding: bool
    notes: tuple[str, ...] = ()
    stats: dict = field(default_factory=dict)
    clearance: str = CLEARANCE_UNCHECKED
    provenance: tuple[str, ...] = ()


    start_tolerance_deg: tuple[float, ...] = ()

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.steps if isinstance(s, Segment))

    @property
    def format_version(self) -> int:


        if any(s.queued for s in self.segments):
            return JOB_FORMAT_VERSION_POINT_QUEUE
        return JOB_FORMAT_VERSION_GOALS

    def to_dict(self) -> dict:
        def enc(s):
            if isinstance(s, Segment):
                return {
                    "kind": "trajectory", "label": s.label, "mode": s.mode,
                    "welding": s.welding, "stretched": list(s.stretched),
                    "points": [
                        {"positions": list(p.positions_rad),
                         "velocities": list(p.velocities_rad_s),
                         "time_from_start": round(p.time_from_start_s, 6)}
                        for p in s.points
                    ],
                }
            if isinstance(s, Wait):
                return {"kind": "wait", "label": s.label, "seconds": s.seconds}
            return {
                "kind": "io", "label": s.label, "arc": s.arc,
                "writes": [{"address": w.address, "value": w.value,
                            "name": w.name} for w in s.writes],
            }

        return {
            "format": "waam-pipeline/ros2-job",
            "version": self.format_version,
            "name": self.name,
            "generator": f"waam-pipeline {_git_sha()}",
            "joint_names": list(self.joint_names),


            "start_tolerance_deg": list(self.start_tolerance_deg),
            "hold_axes": list(self.hold_axes),
            "welding": self.welding,


            "clearance": self.clearance,
            "provenance": list(self.provenance),
            "notes": list(self.notes),
            "stats": self.stats,
            "steps": [enc(s) for s in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    def goal_yaml(self, index: int, *, hold_rad: dict[str, float] | None = None,
                  goal_time_tolerance_s: float = 1.0,
                  goal_tolerance_deg: float = 0.5) -> str:


        seg = self.segments[index]
        if seg.queued:
            raise Ros2Refused(
                "argument",
                f"segment {index + 1} ({seg.label}) is {len(seg.points)} points "
                "and is meant to be STREAMED through queue_traj_point. There is "
                "no FollowJointTrajectory goal that carries it -- the action "
                f"server takes {MAX_POINTS_PER_TRAJECTORY} points "
                "(MAX_NUMBER_OF_POINTS_PER_TRAJECTORY) and would reject this "
                "one. Run it with ros2/waam_ros2_runner.py, which feeds the "
                "queue.",
            )
        names = list(self.joint_names)
        extra = []
        for axis in self.hold_axes:
            if not hold_rad or axis not in hold_rad:
                raise Ros2Refused(
                    "argument",
                    f"this controller also carries {axis}, and MotoROS2 refuses "
                    "a goal that does not list every configured axis. Pass its "
                    "current position in hold_rad (read it off /joint_states), "
                    "or use the runner, which reads it for you.",
                )
            names.append(axis)
            extra.append(float(hold_rad[axis]))

        def row(vals) -> str:
            return "[" + ", ".join(f"{v:.6f}" for v in vals) + "]"

        tol = math.radians(goal_tolerance_deg)
        out = [
            f"# {self.name} - segment {index + 1}/{len(self.segments)}: {seg.label}",
            f"# {'WELDING' if seg.welding else 'air move'}, "
            f"{len(seg.points)} points, {seg.duration_s:.1f} s",
            "trajectory:",
            "  header:",
            "    stamp: {sec: 0, nanosec: 0}",
            "  joint_names: [" + ", ".join(names) + "]",
            "  points:",
        ]
        for p in seg.points:
            sec = int(p.time_from_start_s)
            nsec = int(round((p.time_from_start_s - sec) * 1e9))
            if nsec == 1_000_000_000:
                sec, nsec = sec + 1, 0
            out += [
                "  - positions: " + row(tuple(p.positions_rad) + tuple(extra)),
                "    velocities: " + row(tuple(p.velocities_rad_s)
                                         + tuple(0.0 for _ in extra)),
                f"    time_from_start: {{sec: {sec}, nanosec: {nsec}}}",
            ]
        out.append("goal_tolerance:")
        for n in names:
            out.append(f"- {{name: {n}, position: {tol:.6f}}}")
        out.append(f"goal_time_tolerance: {{sec: {int(goal_time_tolerance_s)}, "
                   f"nanosec: {int(round((goal_time_tolerance_s % 1) * 1e9))}}}")
        return "\n".join(out) + "\n"


def _speed_mm_s(token: str, unit_mm_per_min: float) -> float:


    try:
        v = float(token)
    except (TypeError, ValueError) as exc:
        raise Ros2Refused("argument", f"speed {token!r} is not a number") from exc
    lo, hi = _V_TOKEN_RANGE
    if not lo <= v <= hi:
        raise Ros2Refused(
            "argument",
            f"V={v} is outside the INFORM range {lo:g}..{hi:g} mm/s, so the pendant "
            "job and the ROS job could not be running the same speed",
        )
    return v * unit_mm_per_min / 60.0


def _joint_dt(q0, q1, frac: float, model: RobotModel) -> float:

    worst = 0.0
    for a, b, j in zip(q0, q1, model.joints):
        worst = max(worst, abs(b - a) / (j.speed_deg_s * frac))
    return worst


def _joint_speed_check(q0, q1, dt: float, frac: float, model: RobotModel) -> float:


    need = dt
    for a, b, j in zip(q0, q1, model.joints):
        limit = j.speed_deg_s * frac
        rate = abs(b - a) / dt if dt > 0 else 0.0
        if rate > limit:
            need = max(need, abs(b - a) / limit)
    return need


def _tightest_point_dt(segments) -> float | None:


    tightest = None
    for seg in segments:
        for a, b in zip(seg.points, seg.points[1:]):
            dt = b.time_from_start_s - a.time_from_start_s
            if dt > 0 and (tightest is None or dt < tightest):
                tightest = dt
    return tightest


def _peak_axis_speed_frac(segments, model: RobotModel) -> float:


    worst = 0.0
    for seg in segments:
        for a, b in zip(seg.points, seg.points[1:]):
            dt = b.time_from_start_s - a.time_from_start_s
            if dt <= 0:
                continue
            for pa, pb, j in zip(a.positions_rad, b.positions_rad,
                                 model.joints):
                rate = abs(math.degrees(pb - pa)) / dt
                worst = max(worst, rate / j.speed_deg_s)
    return worst


def build_ros2_job(
    toolpath: Toolpath,
    solve,
    *,
    name: str,
    guard=None,
    arc_io: ArcIo | None = None,
    weld: bool = False,
    weld_v: str | None = None,
    air_v: str | None = None,
    vj_park: float = 50.0,
    vj_approach: float = 25.0,
    hover_mm: float = 50.0,
    interpass_timer_s: float = 30.0,
    layers_per_interpass: int = 1,
    temp_check_every: int = 0,
    bead_dwell_s: float = 0.0,
    measure: MeasureStop | None = None,
    joint_speed_frac: float = 1.0,
    hold_axes: tuple[str, ...] = (),
    allow_weld_splits: bool = False,
    point_queue: bool = False,
    use_ir_feed: bool = False,
    provenance: tuple[str, ...] = (),
    robot: RobotModel = AR1440_MODEL,
) -> Ros2Job:


    if not 0.05 <= joint_speed_frac <= 1.0:
        raise Ros2Refused("argument",
                          f"joint_speed_frac must be 0.05..1.0, got {joint_speed_frac}")
    weld_v = weld_v if weld_v is not None else v_token(DEFAULT_WELD_MM_MIN,
                                                       robot.v_unit_mm_per_min)
    air_v = air_v if air_v is not None else v_token(robot.air_mm_min, robot.v_unit_mm_per_min)
    weld_mm_s = _speed_mm_s(weld_v, robot.v_unit_mm_per_min)
    air_mm_s = _speed_mm_s(air_v, robot.v_unit_mm_per_min)

    if weld:
        if arc_io is None:
            raise Ros2Refused(
                "arc-io",
                "weld=True needs an arc I/O map. Under MotoROS2 the controller "
                "is in trajectory mode running INIT_ROS, so ARCON ASF#(n) is "
                "not available and the arc can only be struck by writing the "
                "signals that macro writes. Capture them from CIOPRG.LST, "
                "review them with a second person, and pass ArcIo.load(...).",
            )
        problems = arc_io.validation_errors()
        if problems:
            raise Ros2Refused("arc-io", "; ".join(problems))
        if arc_io.verified is not True:
            raise Ros2Refused(
                "arc-io",
                "the arc I/O map is not marked verified. Set verified=true only "
                "after two people have checked the addresses against the "
                "controller's own I/O names.",
            )

    steps = build_plan(toolpath, solve, hover_mm=hover_mm,
                       interpass_timer_s=interpass_timer_s,
                       layers_per_interpass=layers_per_interpass, guard=guard,
                       park_q=robot.park_q, temp_check_every=temp_check_every,
                       bead_dwell_s=bead_dwell_s, measure=measure)
    for st in steps:
        if st.kind in ("call", "pause", "message"):
            setting = {
                "temp_check": "Check temperature every N layers",
                "measure_reading": "Measure stop every N layers",
                "measure_pause": "Measure stop every N layers",
                "clean": "Measure stop every N layers",
                "pre_weld": "Measure stop every N layers",
            }.get(st.role, st.role)
            raise Ros2Refused(
                "argument", f"plan step kind {st.kind!r} ({st.role}) has no "
                "ROS 2 rendering: MotoROS2 cannot call jobs or perform "
                "pendant messages/operator pauses. Turn off the workbench "
                f'setting "{setting}" for ROS 2 export',
            )

    out: list[object] = []
    notes: list[str] = []

    pending: list[tuple[tuple[float, ...], float, str]] = []
    seg_start: tuple[float, ...] | None = None
    q_cur: tuple[float, ...] | None = None
    xyz_cur: tuple[float, float, float] | None = None
    arc_lit = False
    stretched: list[str] = []
    dropped = 0

    def flush() -> None:


        nonlocal pending, stretched, seg_start
        if not pending:
            seg_start = q_cur
            return
        seg = _segment(f"{pending[0][2]} -> {pending[-1][2]}", seg_start, pending,
                       welding=arc_lit, stretched=tuple(stretched))
        if len(seg.points) > MAX_POINTS_PER_TRAJECTORY and point_queue:


            out.append(Segment(label=seg.label, points=seg.points,
                               welding=seg.welding, stretched=seg.stretched,
                               mode=MODE_QUEUE))
            notes.append(
                f"{seg.label}: {len(seg.points)} points STREAMED through "
                "MotoROS2 point-queue mode (start_point_queue_mode + "
                f"queue_traj_point) - too long for the {MAX_POINTS_PER_TRAJECTORY}"
                "-point goal buffer, and the arm does not stop inside it"
            )
            pending = []
            stretched = []
            seg_start = q_cur
            return
        if (len(seg.points) > MAX_POINTS_PER_TRAJECTORY and seg.welding
                and not allow_weld_splits):
            raise Ros2Refused(
                "trajectory",
                f"{seg.label}: {len(seg.points)} points in one WELDING run, and "
                f"MotoROS2 takes {MAX_POINTS_PER_TRAJECTORY} per goal "
                "(MAX_NUMBER_OF_POINTS_PER_TRAJECTORY). Splitting it stops the "
                "robot mid-bead, so either stream it (point_queue=True, which "
                "sends it through queue_traj_point and does not stop), reduce "
                "the point count (slicer point reduction), or split the pass in "
                "the toolpath. allow_weld_splits=True accepts the stop.",
            )
        pieces = _split(seg)
        if len(pieces) > 1:
            notes.append(f"{seg.label}: sent as {len(pieces)} goals "
                         f"({MAX_POINTS_PER_TRAJECTORY}-point limit) - the robot "
                         "stops between them")
        out.extend(pieces)
        pending = []
        stretched = []
        seg_start = q_cur

    for st in steps:
        if st.kind in ("joint", "linear"):
            q = tuple(st.q)
            if q_cur is None:


                q_cur = seg_start = q
                xyz_cur = st.xyz
                continue
            if st.kind == "joint":
                vj = vj_park if st.role == "park" else vj_approach
                dt = _joint_dt(q_cur, q, joint_speed_frac * vj / 100.0, robot)
            else:
                speed = weld_mm_s if st.role == "weld" else air_mm_s
                if use_ir_feed and st.role == "weld" and st.feed_mm_min:


                    speed = float(st.feed_mm_min) / 60.0
                if xyz_cur is None or st.xyz is None:
                    dt = _joint_dt(q_cur, q, joint_speed_frac, robot)
                else:
                    dist = math.dist(xyz_cur, st.xyz)
                    dt = dist / speed if speed > 0 else 0.0
            if dt < MIN_POINT_DT_S:
                if max((abs(a - b) for a, b in zip(q_cur, q)), default=0.0) < 1e-6:
                    dropped += 1
                    xyz_cur = st.xyz or xyz_cur
                    continue
                raise Ros2Refused(
                    "trajectory",
                    f"{st.label or st.role}: two points {dt * 1000:.2f} ms apart, "
                    f"below the {MIN_POINT_DT_S * 1000:.0f} ms floor. MotoROS2 "
                    "times points in whole milliseconds and refuses a "
                    "non-increasing time, so reduce the point density or the "
                    "speed rather than rounding them together.",
                )
            need = _joint_speed_check(q_cur, q, dt, joint_speed_frac, robot)
            if need > dt:
                if st.role == "weld":
                    raise Ros2Refused(
                        "speed",
                        f"{st.label}: welding at {speed * 60:.0f} mm/min needs "
                        f"{need / dt:.2f}x an axis rate limit. Slowing a weld "
                        "changes deposition per millimetre, so this is a "
                        "process decision, not something to fix here: lower the "
                        "weld speed deliberately, or move the part into a "
                        "posture the wrist can follow.",
                    )
                stretched.append(f"{st.label or st.role} x{need / dt:.2f}")
                dt = need
            pending.append((q, dt, st.role))
            q_cur = q
            xyz_cur = st.xyz or xyz_cur
        elif st.kind == "dwell":
            flush()
            out.append(Wait(seconds=float(st.seconds), label=st.role))
        elif st.kind in ("arc_on", "arc_off"):
            flush()
            on = st.kind == "arc_on"
            if weld:
                writes = arc_io.on if on else arc_io.off
                out.append(IoStep(writes=tuple(writes), arc="on" if on else "off",
                                  label=f"arc {'on' if on else 'off'} "
                                        f"(move {st.move_index})"))
                if on and arc_io.settle_s > 0:
                    out.append(Wait(seconds=arc_io.settle_s, label="arc settle"))
            else:
                out.append(IoStep(writes=(), arc="on" if on else "off",
                                  label=f"arc {'on' if on else 'off'} "
                                        f"(move {st.move_index}) - NOT ARMED"))
            arc_lit = on
        else:


            raise Ros2Refused("argument", f"plan step kind {st.kind!r} ({st.role}) "
                                          "has no ROS 2 rendering")

    flush()

    if dropped:
        notes.append(f"{dropped} repeated point(s) dropped: a trajectory may "
                     "not hold two points at the same time")
    tight = _tightest_point_dt([s for s in out if isinstance(s, Segment)])
    if tight is not None and tight < ASSUMED_INTERPOLATION_PERIOD_S:
        notes.append(
            f"MAY RUN SLOWER THAN COMMANDED: the closest two points are "
            f"{tight * 1000:.2f} ms apart, under an ASSUMED (and UNVERIFIED, "
            "see MIN_POINT_DT_S) controller interpolation period of "
            f"{ASSUMED_INTERPOLATION_PERIOD_S * 1000:.0f} ms. A gap the "
            "interpolator cannot honour is executed at whatever it can do, "
            "which is slower -- and slower travel is MORE METAL PER "
            "MILLIMETRE, so the bead would be thicker than this job's "
            "stats say. Read the interpolation period off the controller, or "
            "reduce the point density, before believing motion_s."
        )
    if any(isinstance(s, Segment) and s.queued for s in out):
        notes.append(
            "this job needs POINT-QUEUE MODE and so is format version 2. The "
            "runner switches motion modes around each streamed run "
            "(stop_traj_mode, then start_point_queue_mode - MotoROS2 refuses "
            "to start a second motion mode while another is active) and paces "
            "the feed against the queue's BUSY replies. An underrun stops the "
            "arm inside the bead, so the runner treats it as a failure."
        )
    if not weld:
        notes.append("AIR MOVES ONLY - the arc is never struck. Every arc step "
                     "is a marker with no I/O behind it.")
    if hold_axes:
        notes.append("this controller carries axes this job does not command "
                     f"({', '.join(hold_axes)}); the runner holds them at their "
                     "current position because MotoROS2 refuses a goal that "
                     "omits a configured axis")
    start_tolerance_deg = robot.start_tolerance_deg()
    if robot.pulses_per_deg is None:


        notes.append(
            f"{robot.name}: no per-axis start_tolerance_deg -- this robot "
            "model carries no confirmed pulses_per_deg (see the hc10 module "
            "docstring), so MotoROS2's 30-pulse start budget cannot be "
            "converted to degrees per axis without inventing a scale. "
            "ros2/waam_ros2_runner.py falls back to a flat JOINT_TOLERANCE_DEG "
            "(0.5 deg) and warns when it does -- looser than any confirmed "
            "axis budget. Re-emit once a pulse dump lands."
        )

    job = Ros2Job(
        name=name,
        joint_names=robot.joint_names,
        steps=tuple(out),
        hold_axes=tuple(hold_axes),
        welding=bool(weld),
        clearance=(CLEARANCE_GUARDED if guard is not None
                   else CLEARANCE_UNCHECKED),
        provenance=tuple(provenance),
        notes=tuple(notes),
        start_tolerance_deg=start_tolerance_deg,
        stats={
            "segments": sum(1 for s in out if isinstance(s, Segment)),
            "points": sum(len(s.points) for s in out if isinstance(s, Segment)),
            "queued_segments": sum(1 for s in out
                                   if isinstance(s, Segment) and s.queued),
            "queued_points": sum(len(s.points) for s in out
                                 if isinstance(s, Segment) and s.queued),
            "motion_s": round(sum(s.duration_s for s in out
                                  if isinstance(s, Segment)), 1),
            "wait_s": round(sum(s.seconds for s in out if isinstance(s, Wait)), 1),


            "peak_axis_speed_frac": round(_peak_axis_speed_frac(
                [s for s in out if isinstance(s, Segment)], robot), 3),
            "arc_switches": sum(1 for s in out if isinstance(s, IoStep)),
        },
    )
    _validate(job, robot)
    return job


def _segment(label: str, q_start, pending, *, welding: bool,
             stretched: tuple[str, ...]) -> Segment:


    qs = [tuple(q_start)] + [q for q, _, _ in pending]
    times = [0.0]
    for _, dt, _ in pending:
        times.append(times[-1] + dt)

    vels: list[tuple[float, ...]] = [tuple(0.0 for _ in qs[0])]
    for k in range(1, len(qs) - 1):
        span = times[k + 1] - times[k - 1]
        vels.append(tuple(
            math.radians(b - a) / span for a, b in zip(qs[k - 1], qs[k + 1])
        ))
    if len(qs) > 1:
        vels.append(tuple(0.0 for _ in qs[0]))

    points = tuple(
        TrajPoint(positions_rad=tuple(math.radians(a) for a in q),
                  velocities_rad_s=v, time_from_start_s=t)
        for q, v, t in zip(qs, vels, times)
    )
    return Segment(label=label, points=points, welding=welding,
                   stretched=stretched)


def _split(seg: Segment) -> list[Segment]:


    if len(seg.points) <= MAX_POINTS_PER_TRAJECTORY:
        return [seg]
    out: list[Segment] = []
    i = 0
    n = len(seg.points)
    while i < n - 1:
        chunk = seg.points[i:i + MAX_POINTS_PER_TRAJECTORY]
        t0 = chunk[0].time_from_start_s
        zero = tuple(0.0 for _ in chunk[0].positions_rad)
        pts = []
        for k, p in enumerate(chunk):
            last = k == len(chunk) - 1
            first = k == 0
            pts.append(TrajPoint(
                positions_rad=p.positions_rad,
                velocities_rad_s=zero if (first or last) else p.velocities_rad_s,
                time_from_start_s=p.time_from_start_s - t0,
            ))
        out.append(Segment(label=f"{seg.label} [{len(out) + 1}]",
                           points=tuple(pts), welding=seg.welding,
                           stretched=seg.stretched))
        i += MAX_POINTS_PER_TRAJECTORY - 1
    return out


def _validate(job: Ros2Job, model: RobotModel) -> None:


    n = len(job.joint_names)
    for seg in job.segments:
        if seg.mode not in (MODE_GOAL, MODE_QUEUE):
            raise Ros2Refused("trajectory",
                              f"{seg.label}: unknown delivery mode {seg.mode!r}")
        if len(seg.points) < MIN_POINTS_PER_TRAJECTORY:
            raise Ros2Refused("trajectory",
                              f"{seg.label}: {len(seg.points)} point(s); "
                              f"MotoROS2 needs at least {MIN_POINTS_PER_TRAJECTORY}")
        if len(seg.points) > MAX_POINTS_PER_TRAJECTORY and not seg.queued:
            raise Ros2Refused("trajectory",
                              f"{seg.label}: {len(seg.points)} points exceeds "
                              f"{MAX_POINTS_PER_TRAJECTORY}")
        if seg.points[0].time_from_start_s != 0.0:
            raise Ros2Refused("trajectory",
                              f"{seg.label}: first point is not at t=0")
        if any(v != 0.0 for v in seg.points[0].velocities_rad_s):
            raise Ros2Refused("trajectory",
                              f"{seg.label}: the first point must be at rest")
        if any(v != 0.0 for v in seg.points[-1].velocities_rad_s):
            raise Ros2Refused("trajectory",
                              f"{seg.label}: MotoROS2 refuses a final point "
                              "with a non-zero velocity")
        last_ms = -1
        for k, p in enumerate(seg.points):
            if len(p.positions_rad) != n or len(p.velocities_rad_s) != n:
                raise Ros2Refused(
                    "trajectory",
                    f"{seg.label} point {k}: needs {n} positions and "
                    f"{n} velocities")
            ms = int(round(p.time_from_start_s * 1000))
            if ms <= last_ms and k:
                raise Ros2Refused(
                    "trajectory",
                    f"{seg.label} point {k}: time_from_start {ms} ms does not "
                    "advance on the previous point")
            last_ms = ms
        for k, p in enumerate(seg.points):
            for v, j in zip(p.velocities_rad_s, model.joints):
                if abs(math.degrees(v)) > j.speed_deg_s + 1e-9:
                    raise Ros2Refused(
                        "speed",
                        f"{seg.label} point {k}: {j.name} at "
                        f"{abs(math.degrees(v)):.0f} deg/s exceeds its rated "
                        f"{j.speed_deg_s:.0f} deg/s")
