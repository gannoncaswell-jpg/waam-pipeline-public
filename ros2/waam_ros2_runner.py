#!/usr/bin/env python3
'Run a waam-pipeline ROS 2 job against MotoROS2. UNPROVEN ON HARDWARE.'


from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path


JOINT_TOLERANCE_DEG = 0.5


JOB_FORMAT_VERSIONS = (1, 2)


MAX_POINTS_PER_GOAL = 200

MODE_GOAL = "goal"
MODE_QUEUE = "queue"


CLEARANCE_GUARDED = "yes (guarded)"


def clearance_of(job: dict) -> str:
    return str(job.get("clearance") or "NO - UNCHECKED")


YRC1000_WRITABLE_BIT_RANGES = (
    (10010, 15127),
    (27010, 29567),
    (60010, 60647),
)


def io_write_errors(address: int, value: int) -> list[str]:


    e: list[str] = []
    if value not in (0, 1):
        e.append(f"{address}: value must be 0 or 1, got {value!r}")
    if address % 10 > 7:
        e.append(f"{address}: last digit may not be 8 or 9 -- the "
                 "controller's I/O is numbered in octets")
    if not any(lo <= address <= hi for lo, hi in YRC1000_WRITABLE_BIT_RANGES):
        e.append(f"{address}: outside every address range MotoROS2 will write "
                 "on a YRC1000 (10010-15127 general out, 27010-29567 network "
                 "in, 60010-60647 interface panel)")
    return e


def load_job(path: Path) -> dict:
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("format") != "waam-pipeline/ros2-job":
        raise SystemExit(f"{path}: not a waam-pipeline ROS 2 job file")
    if job.get("version") not in JOB_FORMAT_VERSIONS:
        raise SystemExit(f"{path}: job format version {job.get('version')}, "
                         "this runner speaks version(s) "
                         f"{', '.join(str(v) for v in JOB_FORMAT_VERSIONS)}")
    for step in job.get("steps", ()):
        if step.get("kind") != "trajectory":
            continue
        mode = step.get("mode", MODE_GOAL)
        if mode not in (MODE_GOAL, MODE_QUEUE):
            raise SystemExit(f"{path}: segment {step.get('label')!r} asks for "
                             f"delivery mode {mode!r}, which this runner does "
                             "not know how to perform")
        if mode == MODE_GOAL and len(step["points"]) > MAX_POINTS_PER_GOAL:
            raise SystemExit(
                f"{path}: segment {step.get('label')!r} carries "
                f"{len(step['points'])} points as a single GOAL, and MotoROS2's "
                f"action server takes {MAX_POINTS_PER_GOAL} "
                "(MAX_NUMBER_OF_POINTS_PER_TRAJECTORY). Re-emit it with "
                "point_queue=True so it is streamed instead.")


    n_writes = 0
    for step in job.get("steps", ()):
        if step.get("kind") != "io":
            continue
        for w in step.get("writes") or ():
            n_writes += 1
            for err in io_write_errors(int(w["address"]), int(w["value"])):
                raise SystemExit(f"{path}: arc step {step.get('label')!r}: "
                                 f"{err}")
    if n_writes and not job.get("welding"):
        raise SystemExit(
            f"{path}: this job declares \"welding\": false but carries "
            f"{n_writes} arc I/O write(s). One of the two is a lie, and the "
            "writes are the half that reaches the power source. Refusing. If "
            "you want to rehearse a welding job without the arc, re-emit it "
            "unedited and run it with --dry-arc.")
    if job.get("welding") and not n_writes:
        raise SystemExit(
            f"{path}: this job declares \"welding\": true but carries no arc "
            "I/O writes at all, so nothing in it would ever strike or stop an "
            "arc. Re-emit it with a verified ArcIo map "
            "(waam_pipeline.backends.ros2.ArcIo).")
    return job


def describe(job: dict) -> str:
    out = [
        f"job          {job['name']}",
        f"generator    {job.get('generator', '?')}",
        f"format       version {job.get('version')}",
        f"joints       {', '.join(job['joint_names'])}",
        f"welding      {'YES' if job['welding'] else 'no - air moves only'}",
        f"clearance    {clearance_of(job)}",
    ]
    for line in job.get("provenance", ()):
        out.append(f"cell         {line}")
    for n in job.get("notes", ()):
        out.append(f"note         {n}")
    stats = job.get("stats", {})
    if stats:
        out.append("stats        " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    out.append("")
    for i, step in enumerate(job["steps"]):
        if step["kind"] == "trajectory":
            span = step["points"][-1]["time_from_start"]
            kind = "QUEUE" if step.get("mode") == MODE_QUEUE else "TRAJ "
            out.append(f"{i:3d}  {kind} {len(step['points']):4d} pts  {span:6.2f} s  "
                       f"{'WELD' if step['welding'] else 'air '}  {step['label']}")
            for s in step.get("stretched", ()):
                out.append(f"          slowed for an axis limit: {s}")
        elif step["kind"] == "wait":
            out.append(f"{i:3d}  WAIT  {step['seconds']:.1f} s  {step['label']}")
        else:
            if step["writes"]:
                w = ", ".join(f"{x['address']}={x['value']}" for x in step["writes"])
            else:
                w = "no I/O behind it"
            out.append(f"{i:3d}  ARC   {step['arc'].upper():3s}  {w}  ({step['label']})")
    return "\n".join(out)


BUSY_BACKOFF_S = 0.002
BUSY_BACKOFF_MAX_S = 0.050
BUSY_GIVE_UP_S = 5.0
LEAD_WARN_S = 0.10
DRAIN_MARGIN_S = 5.0


def _duration(seconds: float):


    from builtin_interfaces.msg import Duration

    sec = int(seconds)
    nsec = int(round((seconds - sec) * 1e9))
    if nsec >= 1_000_000_000:
        sec, nsec = sec + 1, nsec - 1_000_000_000
    return Duration(sec=sec, nanosec=nsec)


class QueueUnderrun(RuntimeError):
    pass


class QueueRejected(RuntimeError):
    pass


class QueueStalled(RuntimeError):
    pass


class QueueCodes:


    NAMES = ("SUCCESS", "BUSY", "WRONG_MODE", "INVALID_JOINT_LIST",
             "UNABLE_TO_PROCESS_POINT")

    def __init__(self, source):
        missing = [n for n in self.NAMES if not hasattr(source, n)]
        if missing:
            raise RuntimeError(
                f"{source!r} does not define {', '.join(missing)}. This runner "
                "reads QueueResultEnum's values off the installed "
                "motoros2_interfaces rather than hard-coding them; a version "
                "that spells them differently needs a look, not a guess.")
        self._by_name = {n: getattr(source, n) for n in self.NAMES}
        for n, v in self._by_name.items():
            setattr(self, n, v)

    def name(self, value) -> str:
        for n, v in self._by_name.items():
            if v == value:
                return n
        return f"code {value}"


class QueueReply:


    __slots__ = ("code", "message")

    def __init__(self, code, message=""):
        self.code = code
        self.message = message


class PointQueueFeeder:


    def __init__(self, points, *, submit, codes, now=time.monotonic,
                 sleep=time.sleep, in_motion=None, label="", log=None):
        if len(points) < 2:
            raise ValueError(f"{label}: a streamed run needs at least two "
                             "points -- the first only initialises the queue")
        self.points = points
        self.submit = submit
        self.codes = codes
        self.now = now
        self.sleep = sleep
        self.in_motion = in_motion if in_motion is not None else (lambda: None)
        self.label = label
        self.log = log if log is not None else (lambda msg: None)
        self.started_at = None
        self.last_t = 0.0
        self.busy_replies = 0
        self.thin_lead = 0
        self.min_lead_s = None


        self.primed = False
        self.moving_seen = False


        self.saturated = False


    def lead_s(self) -> float:
        if self.started_at is None:
            return float("inf")
        return (self.started_at + self.last_t) - self.now()

    def _health(self, index: int) -> None:
        if self.started_at is None:
            return
        lead = self.lead_s()
        was_primed = self.primed
        if lead > LEAD_WARN_S:
            self.primed = True
        moving = self.in_motion()
        if moving:
            self.moving_seen = True
        if self.saturated and (self.min_lead_s is None or lead < self.min_lead_s):
            self.min_lead_s = lead
        if moving is False and self.moving_seen:
            raise QueueUnderrun(
                f"{self.label}: the arm STOPPED after point {index} of "
                f"{len(self.points)} -- robot_status.in_motion went false with "
                f"{len(self.points) - index} points still to feed. The queue "
                "ran dry, which puts a full stop inside the bead. Feed faster, "
                "reduce the point density, or run the pass slower.")
        if moving is None and was_primed and lead <= 0.0:
            raise QueueUnderrun(
                f"{self.label}: the controller has executed everything queued "
                f"({lead:.3f} s of cushion left) at point {index} of "
                f"{len(self.points)}, and no robot_status is being published to "
                "confirm the arm is still moving. Treating it as an underrun.")
        if was_primed and lead <= LEAD_WARN_S:
            self.thin_lead += 1
            if self.thin_lead == 1:
                self.log(f"    warning: only {lead * 1000:.0f} ms of queued "
                         f"motion left at point {index}; the feed is barely "
                         "keeping up")

    def feed(self) -> dict:
        for i, pt in enumerate(self.points):
            backoff = BUSY_BACKOFF_S
            busy_since = None
            while True:
                self._health(i)
                reply = self.submit(pt)
                if reply.code == self.codes.SUCCESS:
                    break
                if reply.code != self.codes.BUSY:
                    raise QueueRejected(
                        f"{self.label}: queue_traj_point refused point {i} of "
                        f"{len(self.points)} with "
                        f"{self.codes.name(reply.code)}: {reply.message}")
                self.busy_replies += 1
                self.saturated = True
                if busy_since is None:
                    busy_since = self.now()
                elif self.now() - busy_since > BUSY_GIVE_UP_S:
                    raise QueueStalled(
                        f"{self.label}: point {i} of {len(self.points)} has been "
                        f"BUSY for {BUSY_GIVE_UP_S:.0f} s. The controller is not "
                        "consuming points -- HOLD, an alarm, or motion stopped "
                        "under us.")
                self.sleep(backoff)
                backoff = min(backoff * 2, BUSY_BACKOFF_MAX_S)


            self.last_t = float(pt["time_from_start"])
            if i == 1:
                self.started_at = self.now()
        return self.report()

    def drain(self, *, margin_s: float = DRAIN_MARGIN_S) -> None:


        if self.started_at is None:
            return
        ends_at = self.started_at + self.last_t
        limit = ends_at + margin_s
        while self.now() < limit:
            if self.now() >= ends_at and self.in_motion() is not True:
                return
            self.sleep(0.01)
        raise RuntimeError(
            f"{self.label}: the arm was still moving {margin_s:.0f} s after the "
            "streamed run should have finished. Not changing motion mode under "
            "a robot that is still going.")

    def report(self) -> dict:
        return {
            "points": len(self.points),
            "busy_replies": self.busy_replies,
            "thin_lead": self.thin_lead,
            "min_lead_s": round(self.min_lead_s, 3)
            if self.min_lead_s is not None else None,
            "planned_s": round(self.last_t, 2),
        }


class SimulatedPointQueue:


    class Codes:
        SUCCESS = "SUCCESS"
        BUSY = "BUSY"
        WRONG_MODE = "WRONG_MODE"
        INVALID_JOINT_LIST = "INVALID_JOINT_LIST"
        UNABLE_TO_PROCESS_POINT = "UNABLE_TO_PROCESS_POINT"

    def __init__(self, *, n_joints: int, total_points: int,
                 interp_ms: float = 4.0, slots: int = 200,
                 stall_s: float = 0.0, stall_after: int | None = None):
        self.codes = QueueCodes(self.Codes)
        self.n_joints = n_joints
        self.total_points = total_points
        self.buffer_cap_s = slots * interp_ms / 1000.0
        self.stall_s = stall_s
        self.stall_after = stall_after
        self.t = 0.0
        self.accepted = 0
        self.busy_replies = 0
        self.last_t = None
        self.buffered_until = None
        self.stops = 0
        self.stopped_s = 0.0
        self._dry_at = None


    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


        if (self.stall_after is not None and self.stall_s > 0
                and self.accepted >= self.stall_after):
            self.t += self.stall_s
            self.stall_s = 0.0

    def _observe(self) -> None:


        if self.buffered_until is None or self._dry_at is not None:
            return
        if self.buffered_until <= self.t and self.accepted < self.total_points:
            self._dry_at = self.buffered_until
            self.stops += 1

    def in_motion(self):
        self._observe()
        if self.buffered_until is None:
            return None
        return self.buffered_until > self.t

    @property
    def stopped_total_s(self) -> float:
        if self._dry_at is None:
            return self.stopped_s
        return self.stopped_s + max(0.0, self.t - self._dry_at)


    def submit(self, point) -> QueueReply:
        self._observe()
        if (len(point["positions"]) != self.n_joints
                or len(point["velocities"]) != self.n_joints):


            return QueueReply(self.codes.INVALID_JOINT_LIST,
                              f"needs data for all {self.n_joints} joints")
        t_point = float(point["time_from_start"])
        if self.last_t is None:
            self.accepted += 1
            self.last_t = t_point
            return QueueReply(self.codes.SUCCESS, "")
        if (self.buffered_until is not None
                and self.buffered_until - self.t >= self.buffer_cap_s):


            self.busy_replies += 1
            return QueueReply(self.codes.BUSY, "Robot is busy.")
        if t_point <= self.last_t:


            return QueueReply(self.codes.UNABLE_TO_PROCESS_POINT,
                              f"time {t_point} does not advance on {self.last_t}")
        span = t_point - self.last_t
        if self.buffered_until is None or self.buffered_until < self.t:
            if self._dry_at is not None:
                self.stopped_s += self.t - self._dry_at
                self._dry_at = None
            start = self.t
        else:
            start = self.buffered_until
        self.buffered_until = start + span
        self.last_t = t_point
        self.accepted += 1
        return QueueReply(self.codes.SUCCESS, "")


def simulate_queue_segments(job: dict, *, stall_s: float = 0.0,
                            stall_after: int | None = None) -> tuple[list[str], bool]:


    lines: list[str] = []
    held = len(job.get("hold_axes") or ())
    n_joints = len(job["joint_names"]) + held
    seen_underrun = False
    for i, step in enumerate(job["steps"]):
        if step.get("kind") != "trajectory" or step.get("mode") != MODE_QUEUE:
            continue


        pts = [dict(p, positions=list(p["positions"]) + [0.0] * held,
                    velocities=list(p["velocities"]) + [0.0] * held)
               for p in step["points"]]
        after = stall_after
        if stall_s > 0 and after is None:
            after = len(pts) // 2
        sim = SimulatedPointQueue(n_joints=n_joints, total_points=len(pts),
                                  stall_s=stall_s, stall_after=after)
        feeder = PointQueueFeeder(
            pts, submit=sim.submit, codes=sim.codes, now=sim.now,
            sleep=sim.sleep, in_motion=sim.in_motion,
            label=f"step {i} ({step['label']})", log=lines.append)
        lines.append(f"simulating step {i}: {len(pts)} points, "
                     f"{pts[-1]['time_from_start']:.2f} s of motion, "
                     f"{sim.buffer_cap_s * 1000:.0f} ms of simulated buffer"
                     + (f", feeder stalled {stall_s:.2f} s after point {after}"
                        if stall_s > 0 else ""))
        try:
            report = feeder.feed()
            feeder.drain()
        except QueueUnderrun as exc:
            seen_underrun = True
            lines.append(f"    UNDERRUN CAUGHT: {exc}")
            lines.append(f"    (the simulated arm stopped {sim.stops} time(s), "
                         f"{sim.stopped_total_s:.2f} s in total)")
            continue
        lines.append(
            f"    fed {report['points']} points, {report['busy_replies']} BUSY "
            f"replies answered, smallest cushion {report['min_lead_s']} s")
        if sim.stops:
            lines.append(f"    !! the simulated arm stopped {sim.stops} time(s) "
                         "and the feeder did not notice -- that is a bug")
    return lines, seen_underrun


class Runner:
    def __init__(self, job: dict, *, weld: bool, auto_hold: bool,
                 goal_time_tolerance_s: float, dry_arc: bool = False):
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.node import Node

        from control_msgs.action import FollowJointTrajectory
        from industrial_msgs.msg import RobotStatus
        from motoros2_interfaces.msg import QueueResultEnum
        from motoros2_interfaces.srv import (
            QueueTrajPoint, StartPointQueueMode, StartTrajMode, WriteSingleIO,
        )
        from sensor_msgs.msg import JointState
        from std_srvs.srv import Trigger

        from industrial_msgs.msg import TriState
        from motoros2_interfaces.msg import MotionReadyEnum

        self.rclpy = rclpy
        self.FollowJointTrajectory = FollowJointTrajectory
        self.WriteSingleIO = WriteSingleIO
        self.QueueTrajPoint = QueueTrajPoint
        self.StartTrajMode = StartTrajMode
        self.StartPointQueueMode = StartPointQueueMode
        self.Trigger = Trigger
        self.MotionReadyEnum = MotionReadyEnum
        self.TriState = TriState
        self.queue_codes = QueueCodes(QueueResultEnum)
        self.job = job


        self.weld = weld
        self.dry_arc = dry_arc
        self.auto_hold = auto_hold
        self.goal_time_tolerance_s = goal_time_tolerance_s
        self.arc_lit = False
        self.joint_state = None
        self.robot_status = None
        self.mode = None
        self.queue_fed = False

        rclpy.init()
        self.node = Node("waam_ros2_runner")
        self.node.create_subscription(JointState, "joint_states",
                                      self._on_joints, 10)
        self.node.create_subscription(RobotStatus, "robot_status",
                                      self._on_status, 10)
        self.fjt = ActionClient(self.node, FollowJointTrajectory,
                                "follow_joint_trajectory")
        self.start_traj = self.node.create_client(StartTrajMode, "start_traj_mode")
        self.start_queue = self.node.create_client(StartPointQueueMode,
                                                   "start_point_queue_mode")
        self.stop_traj = self.node.create_client(Trigger, "stop_traj_mode")
        self.queue_point = self.node.create_client(QueueTrajPoint,
                                                   "queue_traj_point")
        self.write_io = self.node.create_client(WriteSingleIO, "write_single_io")


    def _on_joints(self, msg) -> None:
        self.joint_state = msg

    def _on_status(self, msg) -> None:
        self.robot_status = msg

    def spin(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.rclpy.spin_once(self.node, timeout_sec=0.05)

    def wait_for(self, what: str, predicate, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.rclpy.spin_once(self.node, timeout_sec=0.1)
            if predicate():
                return
        raise RuntimeError(f"timed out waiting for {what} after {timeout:.0f} s")

    def call(self, client, request, what: str, timeout: float = 10.0):
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f"{what}: service not available. Is the "
                               "micro-ROS Agent running and MotoROS2 connected?")
        return self.call_ready(client, request, what, timeout=timeout)

    def call_ready(self, client, request, what: str, timeout: float = 10.0):


        future = client.call_async(request)
        self.rclpy.spin_until_future_complete(self.node, future, timeout_sec=timeout)
        if future.result() is None:
            raise RuntimeError(f"{what}: no response within {timeout:.0f} s")
        return future.result()

    def spin_sleep(self, seconds: float) -> None:


        end = time.monotonic() + seconds
        self.rclpy.spin_once(self.node, timeout_sec=seconds)
        left = end - time.monotonic()
        if left > 0:
            time.sleep(left)

    def in_motion(self):


        st = self.robot_status
        val = getattr(getattr(st, "in_motion", None), "val", None)
        if val == self.TriState.TRUE:
            return True
        if val == self.TriState.FALSE:
            return False
        return None


    def preflight(self) -> list[str]:


        import math

        self.wait_for("joint_states", lambda: self.joint_state is not None, 15.0)
        self.wait_for("robot_status", lambda: self.robot_status is not None, 15.0)

        st = self.robot_status
        for field, want, why in (
            ("e_stopped", 0, "the e-stop is engaged"),
            ("in_error", 0, "the controller reports an error"),
            ("motion_possible", 1, "the controller says motion is not possible"),
        ):
            val = getattr(getattr(st, field, None), "val", None)
            if val is not None and val != want:
                raise RuntimeError(f"refusing to start: {why} "
                                   f"(robot_status.{field}={val})")

        have = list(self.joint_state.name)
        want = list(self.job["joint_names"])
        missing = [n for n in want if n not in have]
        if missing:
            raise RuntimeError(
                "the controller does not publish these joints: "
                f"{', '.join(missing)}. Its motoros2_config.yaml `joint_names` "
                "must match robots/ar1440.urdf exactly -- MotoROS2 matches "
                "goals to axes BY NAME, and its default names are "
                "joint_1..joint_6. See docs/ros2-setup.md."
            )


        extra = [n for n in have if n not in want]
        if extra and not self.auto_hold:
            raise RuntimeError(
                f"the controller also carries {', '.join(extra)} and MotoROS2 "
                "requires every configured axis in every goal. Re-run without "
                "--no-auto-hold to hold them where they are."
            )


        first = None
        for step in self.job["steps"]:
            if step["kind"] == "trajectory":
                first = step["points"][0]["positions"]
                break
        if first is None:
            raise RuntimeError("this job contains no motion")
        idx = {n: i for i, n in enumerate(have)}
        off = [
            (n, math.degrees(self.joint_state.position[idx[n]] - p))
            for n, p in zip(want, first)
        ]
        tol = self.job.get("start_tolerance_deg")
        if tol and len(tol) == len(want):
            tols = [float(t) for t in tol]
        else:
            tols = [JOINT_TOLERANCE_DEG] * len(want)
            print(
                "!! this job file carries no per-axis "
                "start_tolerance_deg, so the start check falls back to a flat "
                f"{JOINT_TOLERANCE_DEG} deg -- 8x to 24x looser than the "
                "30 pulses Ros_MotionControl_Init allows. A robot inside "
                "this tolerance and outside the controller's is accepted here "
                "and then rejected by MotoROS2 with "
                "INIT_TRAJ_INVALID_STARTING_POS. Re-emit the job to get the "
                "real per-axis numbers.",
                file=sys.stderr)
        bad = [(n, d, t) for (n, d), t in zip(off, tols) if abs(d) > t]
        if bad:
            raise RuntimeError(
                "the robot is not at this job's starting posture, and MotoROS2 "
                "refuses a goal whose first point is more than 30 pulses from "
                "the current position. Jog to park first. Off by: "
                + ", ".join(f"{n} {d:+.4f} deg (limit {t:.4f})"
                            for n, d, t in bad)
            )
        return extra


    def hold_values(self, extra: list[str]) -> list[float]:
        idx = {n: i for i, n in enumerate(self.joint_state.name)}
        return [float(self.joint_state.position[idx[n]]) for n in extra]

    def send_segment(self, step: dict, extra: list[str]) -> None:
        from builtin_interfaces.msg import Duration
        from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

        hold = self.hold_values(extra)
        goal = self.FollowJointTrajectory.Goal()
        traj = JointTrajectory()
        traj.joint_names = list(self.job["joint_names"]) + list(extra)
        for p in step["points"]:
            pt = JointTrajectoryPoint()
            pt.positions = [float(v) for v in p["positions"]] + hold
            pt.velocities = [float(v) for v in p["velocities"]] + [0.0] * len(extra)
            t = float(p["time_from_start"])
            pt.time_from_start = Duration(sec=int(t),
                                          nanosec=int(round((t % 1) * 1e9)))
            traj.points.append(pt)
        goal.trajectory = traj
        goal.goal_time_tolerance = Duration(
            sec=int(self.goal_time_tolerance_s),
            nanosec=int(round((self.goal_time_tolerance_s % 1) * 1e9)))

        if len(traj.points) > MAX_POINTS_PER_GOAL:
            raise RuntimeError(
                f"{step['label']}: {len(traj.points)} points in one goal, and "
                f"the action server takes {MAX_POINTS_PER_GOAL}. This one "
                "should have been streamed (point_queue=True).")
        if not self.fjt.wait_for_server(timeout_sec=10.0):
            raise RuntimeError("follow_joint_trajectory action server not found")
        send = self.fjt.send_goal_async(goal)
        self.rclpy.spin_until_future_complete(self.node, send, timeout_sec=10.0)
        handle = send.result()
        if handle is None or not handle.accepted:
            raise RuntimeError(f"{step['label']}: goal rejected by MotoROS2")
        self.goal_handle = handle
        result = handle.get_result_async()
        self.rclpy.spin_until_future_complete(
            self.node, result,
            timeout_sec=step["points"][-1]["time_from_start"] + 30.0)
        res = result.result()
        if res is None:
            raise RuntimeError(f"{step['label']}: no result (still moving?)")
        self.goal_handle = None
        code = res.result.error_code
        if code != 0:

            raise RuntimeError(
                f"{step['label']}: FollowJointTrajectory error_code {code} "
                f"({res.result.error_string}). A code of the form -ECCCCC "
                "carries a MotoROS2 MotionReadyEnum in its last five digits.")


    def ensure_mode(self, mode: str) -> None:


        if self.mode == mode:
            return


        if self.arc_lit:
            raise RuntimeError(
                f"refusing to switch motion mode ({self.mode} -> {mode}) with "
                "the ARC LIT. That transition stops the arm for as long as 45 "
                "s, which at the weld start is a burn-through. This is a bug "
                "in the look-ahead in run(), not something to retry.")
        if self.mode is not None:
            self.leave_mode()
        if mode == MODE_QUEUE:
            what, client = "start_point_queue_mode", self.start_queue
            request = self.StartPointQueueMode.Request()
        else:
            what, client = "start_traj_mode", self.start_traj
            request = self.StartTrajMode.Request()
        res = self.call(client, request, what)
        if res.result_code.value != self.MotionReadyEnum.READY:
            raise RuntimeError(f"{what} refused: {res.message} "
                               f"(code {res.result_code.value})")
        self.mode = mode
        self.queue_fed = False

    def prepare_mode(self, mode: str) -> None:


        if mode == MODE_QUEUE and self.mode == MODE_QUEUE and self.queue_fed:
            self.leave_mode()
        self.ensure_mode(mode)

    def leave_mode(self) -> None:


        if self.arc_lit:
            raise RuntimeError(
                "refusing to leave motion mode with the ARC LIT: this waits "
                "up to 30 s for the arm to stop and then stops the mode, all "
                "of it standing still over the work.")
        self.wait_for("the arm to come to a stop",
                      lambda: self.in_motion() is not True, 30.0)
        res = self.call(self.stop_traj, self.Trigger.Request(), "stop_traj_mode",
                        timeout=5.0)
        if not getattr(res, "success", True):
            raise RuntimeError(f"stop_traj_mode refused: "
                               f"{getattr(res, 'message', '')}")
        self.mode = None

    def stream_segment(self, step: dict, extra: list[str]) -> None:


        from trajectory_msgs.msg import JointTrajectoryPoint


        self.prepare_mode(MODE_QUEUE)
        self.queue_fed = True
        hold = self.hold_values(extra)
        names = list(self.job["joint_names"]) + list(extra)
        zeros = [0.0] * len(extra)
        if not self.queue_point.wait_for_service(timeout_sec=10.0):
            raise RuntimeError("queue_traj_point: service not available. Is "
                               "this MotoROS2 old enough to predate point-queue "
                               "mode? It arrived in 0.0.15.")

        def submit(p) -> QueueReply:
            req = self.QueueTrajPoint.Request()
            req.joint_names = list(names)
            pt = JointTrajectoryPoint()
            pt.positions = [float(v) for v in p["positions"]] + hold
            pt.velocities = [float(v) for v in p["velocities"]] + zeros
            pt.time_from_start = _duration(float(p["time_from_start"]))
            req.point = pt
            res = self.call_ready(self.queue_point, req, "queue_traj_point",
                                  timeout=5.0)
            return QueueReply(res.result_code.value, res.message)

        feeder = PointQueueFeeder(
            step["points"], submit=submit, codes=self.queue_codes,
            now=time.monotonic, sleep=self.spin_sleep,
            in_motion=self.in_motion, label=step["label"],
            log=lambda msg: print(msg, file=sys.stderr))
        report = feeder.feed()
        feeder.drain()
        print(f"    streamed {report['points']} points in "
              f"{report['planned_s']} s, {report['busy_replies']} BUSY replies "
              f"answered, smallest cushion {report['min_lead_s']} s")

    def do_io(self, step: dict) -> None:
        if not step["writes"]:
            print(f"    arc {step['arc']}: marker only, no I/O armed")
            return


        if not self.weld:
            if self.dry_arc:
                print(f"    arc {step['arc']}: SUPPRESSED by --dry-arc, "
                      f"{len(step['writes'])} write(s) not sent")
                return
            raise RuntimeError(
                f"{step['label']}: this step would write "
                f"{len(step['writes'])} arc I/O signal(s) and --weld was not "
                "given. Refusing rather than skipping them, because a job "
                "that expects an arc must not run as a mystery air move. Pass "
                "--weld to arm it, or --dry-arc to rehearse the motion with "
                "the arc suppressed.")
        for w in step["writes"]:
            req = self.WriteSingleIO.Request()
            req.address = int(w["address"])
            req.value = int(w["value"])
            res = self.call(self.write_io, req, f"write_single_io {w['address']}")
            if not getattr(res, "success", False):
                raise RuntimeError(
                    f"write_single_io {w['address']}={w['value']} failed: "
                    f"{getattr(res, 'message', '')}")
        self.arc_lit = step["arc"] == "on"

    def extinguish(self) -> None:

        if not self.arc_lit:
            return
        offs = [s for s in self.job["steps"]
                if s["kind"] == "io" and s["arc"] == "off" and s["writes"]]
        if not offs:
            print("!! THE ARC MAY STILL BE LIT and this job carries no arc-off "
                  "writes. Hit the e-stop.", file=sys.stderr)
            return
        print("!! extinguishing the arc", file=sys.stderr)
        for w in offs[0]["writes"]:
            try:
                req = self.WriteSingleIO.Request()
                req.address = int(w["address"])
                req.value = int(w["value"])
                self.call(self.write_io, req, "arc off", timeout=5.0)
            except Exception as exc:
                print(f"!! arc-off write {w['address']} failed: {exc}. "
                      "HIT THE E-STOP.", file=sys.stderr)
        self.arc_lit = False

    def run(self) -> int:
        extra = self.preflight()
        if extra:
            print(f"holding {', '.join(extra)} at its current position")


        first = next((s.get("mode", MODE_GOAL) for s in self.job["steps"]
                      if s["kind"] == "trajectory"), MODE_GOAL)
        self.ensure_mode(first)

        try:
            for i, step in enumerate(self.job["steps"]):
                print(f"[{i + 1}/{len(self.job['steps'])}] {step['kind']}: "
                      f"{step['label']}")
                if step["kind"] == "trajectory":
                    if step.get("mode", MODE_GOAL) == MODE_QUEUE:
                        self.stream_segment(step, extra)
                    else:
                        self.ensure_mode(MODE_GOAL)
                        self.send_segment(step, extra)
                elif step["kind"] == "wait":
                    self.spin(float(step["seconds"]))
                else:
                    if step["arc"] == "on":


                        nxt = next((t.get("mode", MODE_GOAL)
                                    for t in self.job["steps"][i + 1:]
                                    if t["kind"] == "trajectory"), None)
                        if nxt is not None:
                            self.prepare_mode(nxt)
                    self.do_io(step)
        finally:
            self.extinguish()
            try:
                if self.mode is not None:
                    self.leave_mode()
            except Exception as exc:
                print(f"stop_traj_mode: {exc}", file=sys.stderr)
        print("done")
        return 0

    def shutdown(self) -> None:
        try:
            self.node.destroy_node()
        finally:
            self.rclpy.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("job", type=Path, help="the .ros2.json emitted by emit_ros2")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the sequence and exit; needs no ROS")
    ap.add_argument("--weld", action="store_true",
                    help="arm the arc. Refused unless the job was built with a "
                         "verified arc I/O map.")
    ap.add_argument("--dry-arc", action="store_true",
                    help="run a WELDING job's motion with the arc suppressed: "
                         "no arc I/O is written. This exists so nobody has to "
                         "edit \"welding\": false into the job file to get a "
                         "rehearsal -- that edit leaves the arc writes in "
                         "place and disarms the checks around them, and "
                         "load_job now refuses such a file.")
    ap.add_argument("--unchecked", action="store_true",
                    help="run a job whose file does not record a clearance "
                         "check. An ungated job is byte-identical to a gated "
                         "one apart from that stamp, so this flag is the only "
                         "thing that makes running one a decision.")
    ap.add_argument("--no-auto-hold", action="store_true",
                    help="refuse, rather than hold, an axis the controller has "
                         "that this job does not command")
    ap.add_argument("--goal-time-tolerance", type=float, default=1.0,
                    metavar="S")
    ap.add_argument("--sim-stall", type=float, default=0.0, metavar="S",
                    help="in --dry-run, make the simulated feeder go away for "
                         "S seconds in the middle of a streamed pass. The "
                         "underrun MUST be caught; that is what this proves.")
    ap.add_argument("--sim-stall-at", type=int, default=None, metavar="N",
                    help="which accepted point --sim-stall interrupts "
                         "(default: halfway)")
    args = ap.parse_args(argv)

    job = load_job(args.job)
    if args.dry_run:
        print(describe(job))
        lines, underrun = simulate_queue_segments(
            job, stall_s=args.sim_stall, stall_after=args.sim_stall_at)
        if lines:
            print()
            print("\n".join(lines))
        if args.sim_stall > 0:
            if not lines:
                print("\n--sim-stall, but this job has no streamed segments to "
                      "stall. Emit it with point_queue=True.", file=sys.stderr)
                return 1
            if not underrun:
                print("\nthe simulated stall did NOT produce an underrun the "
                      "feeder could catch. Either it was too short to empty "
                      "the queue, or the detector is broken.", file=sys.stderr)
                return 1
            print("\nthe simulated underrun was CAUGHT, which is what "
                  "--sim-stall asks for")
        elif underrun:
            print("\nthe feeder underran its own simulated queue with no fault "
                  "injected. That is a bug in the pacing.", file=sys.stderr)
            return 1
        return 0

    if clearance_of(job) != CLEARANCE_GUARDED and not args.unchecked:
        raise SystemExit(
            f"this job records clearance {clearance_of(job)!r}: nothing "
            "checked the torch against the cell before it was written. It was "
            "built by backends.ros2.build_ros2_job directly, or by an emitter "
            "old enough not to stamp it -- either way its file is otherwise "
            "byte-identical to a gated one, which is why this is refused "
            "rather than warned about. Re-emit it through "
            "waam_pipeline.emit.emit_ros2, which runs the same check()/"
            "blocking() pass the .JBI path runs, or pass --unchecked if you "
            "mean it.")
    if args.weld and args.dry_arc:
        raise SystemExit(
            "--weld and --dry-arc contradict each other: one arms the arc and "
            "the other suppresses it. Pick the one you mean.")
    if args.weld and not job["welding"]:
        raise SystemExit(
            "--weld, but this job was built with weld=False: its arc steps are "
            "markers with no I/O behind them. Rebuild it with a verified arc "
            "I/O map (waam_pipeline.backends.ros2.ArcIo) -- the runner will not "
            "invent the addresses.")
    if job["welding"] and not (args.weld or args.dry_arc):
        raise SystemExit(
            "this job WILL STRIKE AN ARC and --weld was not given. Pass --weld "
            "deliberately, with the cell cleared and somebody watching -- or "
            "--dry-arc to rehearse the motion with the arc suppressed. Do NOT "
            "edit \"welding\": false into the file: that leaves the arc "
            "writes in it, and load_job refuses the result.")

    runner = Runner(job, weld=args.weld, auto_hold=not args.no_auto_hold,
                    goal_time_tolerance_s=args.goal_time_tolerance,
                    dry_arc=args.dry_arc)

    def on_signal(signum, frame):


        print("\ninterrupted", file=sys.stderr)
        handle = getattr(runner, "goal_handle", None)
        if handle is not None:
            try:
                handle.cancel_goal_async()
            except Exception:
                pass
        runner.extinguish()
        runner.shutdown()
        sys.exit(130)

    signal.signal(signal.SIGINT, on_signal)
    try:
        return runner.run()
    except Exception as exc:
        runner.extinguish()
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        runner.shutdown()


if __name__ == "__main__":
    sys.exit(main())
