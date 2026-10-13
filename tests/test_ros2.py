import contextlib
import importlib.util
import io
import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.emit import PlatterState
from waam_pipeline.backends.jbi import PULSES_PER_DEG, v_token
from waam_pipeline.backends.ros2 import (
    AR1440_MODEL, HC10_MODEL, JOINT_NAMES, MAX_POINTS_PER_TRAJECTORY,
    MIN_POINTS_PER_TRAJECTORY, MODE_QUEUE, ArcIo, IoPoint, IoStep, RobotModel,
    Ros2Refused, Wait, build_ros2_job,
)
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, emit_job, emit_ros2
from waam_pipeline.robots.ar1440 import AR1440_JOINTS


from waam_pipeline.plan import HC_PARK_Q, MeasureStop
from waam_pipeline.robots.hc10 import ARM_JOINT_NAMES
from waam_pipeline.slicer import wall
from waam_pipeline.toolpath import Move, Toolpath

ORIGIN_X, ORIGIN_Y, SUBSTRATE_MM = 1014.0, -180.0, 6.35


def runner_module():


    spec = importlib.util.spec_from_file_location(
        "waam_ros2_runner", ROOT / "ros2" / "waam_ros2_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def long_bead(points=400, step_mm=0.5):

    tp = Toolpath()
    tp.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
    for i in range(1, points):
        tp.moves.append(Move(i * step_mm, 0.0, 5.0, 500.0, True, 0, i + 1))
    return tp


def postures(job):


    out = []
    for seg in job.segments:
        for p in seg.points:
            q = tuple(round(v, 12) for v in p.positions_rad)
            if not out or out[-1] != q:
                out.append(q)
    return out


_P = PlatterState(clear=True, training_block_on=True, attested_by="test")


def wall_setup(layers=6, layer_h=2.0, length=150.0):


    tp = wall(length, layers, layer_h)
    origin = cell_lc.snap_toolpath_to_platter(tp, ORIGIN_X, ORIGIN_Y,
                                             substrate_mm=SUBSTRATE_MM)
    torch = TorchModel.yaskawa_accufast_45()
    scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=True)
    return tp, origin, scene, torch


class OneJob(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.tp, cls.origin, cls.scene, cls.torch = wall_setup()
        cls.job, cls.summary = emit_ros2(
            cls.tp, cell_lc.pulse_solver(cls.origin), bead_height_mm=2.0, name="WALLTEST", platter=_P, scene=cls.scene, torch=cls.torch)

    def test_ros_poses_match_the_jbi_pulse_records(self):


        text, _ = emit_job(self.tp, cell_lc.pulse_solver(self.origin), bead_height_mm=2.0, name="WALLTEST", platter=_P, arc_file=12,
                           scene=self.scene, torch=self.torch,
                           date="2026/01/01 00:00")


        jbi_deg, seen_j = [], set()
        for ln in text.splitlines():
            if not re.match(r"^C\d{5}=", ln):
                continue
            deg = [int(v) / r for v, r in zip(ln.split("=", 1)[1].split(","), PULSES_PER_DEG)]
            key = tuple(round(d, 9) for d in deg)
            if key not in seen_j:
                seen_j.add(key)
                jbi_deg.append(deg)
        ros_deg, seen = [], set()
        for seg in self.job.segments:
            for p in seg.points:
                q = tuple(round(math.degrees(a), 9) for a in p.positions_rad)
                if q not in seen:
                    seen.add(q)
                    ros_deg.append(q)
        self.assertEqual(len(jbi_deg), len(ros_deg))
        for i, (a, b) in enumerate(zip(jbi_deg, ros_deg)):
            for axis, (x, y) in enumerate(zip(a, b)):
                self.assertAlmostEqual(
                    x, y, places=2,
                    msg=f"posture {i} axis {axis}: .JBI {x} vs ROS {y}")

    def test_the_arc_switches_in_the_same_places(self):
        text, _ = emit_job(self.tp, cell_lc.pulse_solver(self.origin), bead_height_mm=2.0, name="WALLTEST", platter=_P, arc_file=12,
                           scene=self.scene, torch=self.torch,
                           date="2026/01/01 00:00")
        jbi_arcs = [ln.split()[0] for ln in text.splitlines()
                    if ln.startswith(("ARCON", "ARCOF"))]
        ros_arcs = ["ARCON" if s.arc == "on" else "ARCOF"
                    for s in self.job.steps if isinstance(s, IoStep)]
        self.assertEqual(jbi_arcs, ros_arcs)

    def test_the_dwells_survive(self):


        waits = [w.seconds for w in self.job.steps if isinstance(w, Wait)]
        self.assertEqual(waits[0], 2.0)
        self.assertEqual(waits[1:], [30.0] * (len(waits) - 1))

    def test_workbench_operator_stops_are_refused_by_setting_name(self):
        for kwargs, setting in (({"temp_check_every": 1}, "Check temperature every N layers"),
                                ({"measure": MeasureStop(1)}, "Measure stop every N layers")):
            with self.subTest(setting=setting), self.assertRaises(EmitRefused) as refused:
                emit_ros2(
                    self.tp, cell_lc.pulse_solver(self.origin), bead_height_mm=2.0,
                    name="STOP", platter=_P, scene=self.scene, torch=self.torch, **kwargs,
                )
            self.assertIn(setting, refused.exception.detail)

    def test_explicitly_disabled_workbench_stops_are_byte_identical(self):
        job, _ = emit_ros2(
            self.tp, cell_lc.pulse_solver(self.origin), bead_height_mm=2.0,
            name="WALLTEST", platter=_P, scene=self.scene, torch=self.torch,
            temp_check_every=0, bead_dwell_s=0.0, measure=None,
        )
        self.assertEqual(job.to_json(), self.job.to_json())

    def test_weld_moves_run_at_the_speed_the_toolpath_was_planned_for(self):


        planned_mm_min = wall(150.0, 6, 2.0).moves[1].feed
        self.assertEqual(planned_mm_min, 200.0)
        token = v_token(planned_mm_min, 60.0)
        self.assertEqual(token, "3.3")
        weld = [s for s in self.job.segments if s.welding]
        self.assertTrue(weld)
        for seg in weld:
            self.assertAlmostEqual(seg.duration_s, 150.0 / float(token), places=2)

    def test_summary_counts_what_the_operator_needs(self):
        self.assertEqual(self.summary["name"], "WALLTEST")
        self.assertFalse(self.summary["welding"])
        self.assertEqual(self.summary["arc_switches"], 12)
        self.assertGreater(self.summary["deposit_mm"], 0)


class MotoRos2Rules(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.tp, cls.origin, cls.scene, cls.torch = wall_setup()
        cls.job, _ = emit_ros2(cls.tp, cell_lc.pulse_solver(cls.origin), bead_height_mm=2.0, name="RULES", platter=_P,
                               scene=cls.scene, torch=cls.torch)

    def test_every_goal_starts_where_the_robot_is_and_ends_stopped(self):


        prev_end = None
        for seg in self.job.segments:
            self.assertEqual(seg.points[0].time_from_start_s, 0.0)
            self.assertEqual(set(seg.points[0].velocities_rad_s), {0.0})
            self.assertEqual(set(seg.points[-1].velocities_rad_s), {0.0})
            if prev_end is not None:
                for a, b in zip(prev_end, seg.points[0].positions_rad):
                    self.assertAlmostEqual(a, b, places=9)
            prev_end = seg.points[-1].positions_rad

    def test_time_advances_in_whole_milliseconds(self):


        for seg in self.job.segments:
            last = -1
            for k, p in enumerate(seg.points):
                ms = int(round(p.time_from_start_s * 1000))
                if k:
                    self.assertGreater(ms, last, f"{seg.label} point {k}")
                    self.assertGreater(ms, 0)
                last = ms

    def test_goal_sizes_are_legal(self):
        for seg in self.job.segments:
            self.assertGreaterEqual(len(seg.points), MIN_POINTS_PER_TRAJECTORY)
            self.assertLessEqual(len(seg.points), MAX_POINTS_PER_TRAJECTORY)

    def test_no_point_exceeds_a_datasheet_axis_speed(self):
        for seg in self.job.segments:
            for p in seg.points:
                for v, j in zip(p.velocities_rad_s, AR1440_JOINTS):
                    self.assertLessEqual(abs(math.degrees(v)), j.speed_deg_s)

    def test_joint_names_are_the_urdf_names(self):


        urdf = (ROOT / "robots" / "ar1440.urdf").read_text(encoding="utf-8")
        names = re.findall(r'<joint name="(joint_\d_\w)"', urdf)
        self.assertEqual(list(JOINT_NAMES), names)

    def test_a_long_welding_pass_is_refused_not_silently_stopped(self):


        tp = long_bead()
        _, origin, scene, torch = wall_setup()
        with self.assertRaises(Ros2Refused) as ctx:
            build_ros2_job(tp, cell_lc.pulse_solver(origin), name="LONG")
        self.assertEqual(ctx.exception.reason, "trajectory")
        self.assertIn("WELDING run", ctx.exception.detail)
        self.assertIn("point_queue=True", ctx.exception.detail)


        job = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="LONG", allow_weld_splits=True)
        self.assertTrue(any("stops between them" in n for n in job.notes))
        for seg in job.segments:
            self.assertLessEqual(len(seg.points), MAX_POINTS_PER_TRAJECTORY)

    def test_a_weld_too_fast_for_an_axis_is_refused_not_slowed(self):


        tp, origin, scene, torch = wall_setup()


        fast = wall(150.0, 6, 2.0, feed_mm_min=90000.0)
        fast_origin = cell_lc.snap_toolpath_to_platter(fast, ORIGIN_X, ORIGIN_Y,
                                                       substrate_mm=SUBSTRATE_MM)
        with self.assertRaises(EmitRefused) as ctx:
            emit_ros2(fast, cell_lc.pulse_solver(fast_origin), bead_height_mm=2.0, name="FAST", platter=_P,
                      scene=scene, torch=torch, joint_speed_frac=0.05)
        self.assertEqual(ctx.exception.reason, "speed")
        self.assertIn("deposition per millimetre", ctx.exception.detail)


        job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="SLOW", platter=_P,
                           scene=scene, torch=torch, air_v="1500",
                           joint_speed_frac=0.05)
        self.assertTrue(any(s.stretched for s in job.segments))


class PointSpacingUnderTheAssumedPeriodIsSaidOutLoud(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        _, cls.origin, _, _ = wall_setup()

    def bead(self, step_mm, points=400):
        tp = Toolpath()
        tp.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
        for i in range(1, points):
            tp.moves.append(Move(i * step_mm, 0.0, 5.0, 500.0, True, 0, i + 1))
        return build_ros2_job(tp, cell_lc.pulse_solver(self.origin),
                              name="SPACING", point_queue=True)

    def note(self, job):
        return next((n for n in job.notes
                     if n.startswith("MAY RUN SLOWER THAN COMMANDED")), None)

    def test_a_bead_spaced_under_the_assumed_period_carries_the_note(self):


        note = self.note(self.bead(0.05))
        self.assertIsNotNone(note, "a 0.05 mm-spaced bead ran with no warning")
        self.assertIn("3.60 ms", note)
        self.assertIn("UNVERIFIED", note)
        self.assertIn("MORE METAL PER MILLIMETRE", note)

    def test_a_normally_spaced_bead_does_not(self):


        self.assertIsNone(self.note(self.bead(0.5)))

    def test_the_floor_still_refuses_what_it_always_refused(self):


        with self.assertRaises(Ros2Refused) as ctx:
            self.bead(0.02)
        self.assertEqual(ctx.exception.reason, "trajectory")
        self.assertIn("ms floor", ctx.exception.detail)


class ReportedSpeedIsTheSpeedTheJobCommands(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        tp, cls.origin, _, _ = wall_setup()
        cls.job = build_ros2_job(tp, cell_lc.pulse_solver(cls.origin),
                                 name="DERATED", air_v="1500",
                                 joint_speed_frac=0.05)

    def test_the_case_really_does_stretch(self):


        self.assertTrue(any(s.stretched for s in self.job.segments),
                        "no move was stretched, so this case cannot tell the "
                        "two ways of computing the stat apart")

    def test_the_stat_is_within_the_configured_derate(self):


        self.assertLessEqual(self.job.stats["peak_axis_speed_frac"],
                             0.05 + 1e-9)

    def test_the_stat_matches_the_points_that_were_actually_emitted(self):


        d = json.loads(self.job.to_json())
        worst = 0.0
        for step in d["steps"]:
            if step["kind"] != "trajectory":
                continue
            for a, b in zip(step["points"], step["points"][1:]):
                dt = b["time_from_start"] - a["time_from_start"]
                if dt <= 0:
                    continue
                for pa, pb, j in zip(a["positions"], b["positions"],
                                     AR1440_JOINTS):
                    rate = abs(math.degrees(pb - pa)) / dt
                    worst = max(worst, rate / j.speed_deg_s)
        self.assertAlmostEqual(d["stats"]["peak_axis_speed_frac"], worst,
                               places=3)


class PointQueueEmission(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.tp = long_bead()
        _, cls.origin, cls.scene, cls.torch = wall_setup()

    def build(self, **kw):
        return build_ros2_job(self.tp, cell_lc.pulse_solver(self.origin),
                              name="LONG", **kw)

    def test_the_long_bead_survives_whole(self):
        job = self.build(point_queue=True)
        weld = [s for s in job.segments if s.welding]
        self.assertEqual(len(weld), 1)
        self.assertEqual(weld[0].mode, MODE_QUEUE)
        self.assertGreater(len(weld[0].points), MAX_POINTS_PER_TRAJECTORY)
        self.assertTrue(any("STREAMED" in n for n in job.notes))

    def test_streaming_changes_delivery_and_not_motion(self):


        split = self.build(allow_weld_splits=True)
        queued = self.build(point_queue=True)
        self.assertEqual(postures(split), postures(queued))
        self.assertAlmostEqual(sum(s.duration_s for s in split.segments),
                               sum(s.duration_s for s in queued.segments),
                               places=6)
        self.assertGreater(len(split.segments), len(queued.segments))

    def test_a_streamed_run_still_obeys_every_other_rule(self):


        job = self.build(point_queue=True)
        seg = next(s for s in job.segments if s.queued)
        self.assertEqual(seg.points[0].time_from_start_s, 0.0)
        self.assertEqual(set(seg.points[0].velocities_rad_s), {0.0})
        self.assertEqual(set(seg.points[-1].velocities_rad_s), {0.0})
        last = -1
        for k, p in enumerate(seg.points):
            self.assertEqual(len(p.positions_rad), len(JOINT_NAMES))
            self.assertEqual(len(p.velocities_rad_s), len(JOINT_NAMES))
            ms = int(round(p.time_from_start_s * 1000))
            if k:
                self.assertGreater(ms, last)
            last = ms


        prev = None
        for s in job.segments:
            if prev is not None:
                for a, b in zip(prev, s.points[0].positions_rad):
                    self.assertAlmostEqual(a, b, places=9)
            prev = s.points[-1].positions_rad

    def test_a_streamed_job_is_format_version_2(self):


        self.assertEqual(self.build(point_queue=True).format_version, 2)
        self.assertEqual(self.build(allow_weld_splits=True).format_version, 1)
        d = json.loads(self.build(point_queue=True).to_json())
        modes = [s["mode"] for s in d["steps"] if s["kind"] == "trajectory"]
        self.assertIn(MODE_QUEUE, modes)
        self.assertEqual(d["version"], 2)

    def test_point_queue_does_not_open_a_second_door_for_short_jobs(self):


        tp, origin, scene, torch = wall_setup()
        plain, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="W", platter=_P,
                             scene=scene, torch=torch)
        streamed = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="W",
                                  point_queue=True)
        self.assertEqual([s.mode for s in streamed.segments],
                         ["goal"] * len(plain.segments))
        self.assertEqual(streamed.format_version, 1)

    def test_goal_yaml_refuses_to_export_a_streamed_segment(self):


        job = self.build(point_queue=True)
        index = next(i for i, s in enumerate(job.segments) if s.queued)
        with self.assertRaises(Ros2Refused) as ctx:
            job.goal_yaml(index)
        self.assertIn("STREAMED", ctx.exception.detail)


class PointQueueFeeding(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.mod = runner_module()
        _, origin, _, _ = wall_setup()
        cls.job = json.loads(
            build_ros2_job(long_bead(), cell_lc.pulse_solver(origin),
                           name="LONG", point_queue=True).to_json())
        cls.codes = cls.mod.QueueCodes(cls.mod.SimulatedPointQueue.Codes)

    def flat_points(self, n=40, dt=0.05, joints=6):
        return [{"positions": [0.0] * joints, "velocities": [0.0] * joints,
                 "time_from_start": i * dt} for i in range(n)]

    def feeder(self, points, **kw):
        kw.setdefault("submit", lambda p: self.mod.QueueReply(self.codes.SUCCESS))
        kw.setdefault("codes", self.codes)
        kw.setdefault("label", "test")
        return self.mod.PointQueueFeeder(points, **kw)


    def test_a_healthy_pass_is_paced_by_busy_and_never_runs_dry(self):
        lines, underrun = self.mod.simulate_queue_segments(self.job)
        self.assertTrue(lines)
        self.assertFalse(underrun)
        self.assertTrue(any("BUSY replies answered" in ln for ln in lines))


        fed = [ln for ln in lines if "BUSY replies answered" in ln]
        self.assertTrue(any("0 BUSY" not in ln for ln in fed))

    def test_busy_is_answered_by_retrying_the_same_point(self):


        clock = [0.0]
        seen = []
        busy_left = [3]

        def submit(p):
            if busy_left[0]:
                busy_left[0] -= 1
                return self.mod.QueueReply(self.codes.BUSY, "Robot is busy.")
            busy_left[0] = 3
            seen.append(p["time_from_start"])
            return self.mod.QueueReply(self.codes.SUCCESS)

        pts = self.flat_points(n=6)
        f = self.feeder(pts, submit=submit, now=lambda: clock[0],
                        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                        in_motion=lambda: True)
        report = f.feed()
        self.assertEqual(seen, [p["time_from_start"] for p in pts])
        self.assertEqual(report["busy_replies"], 3 * len(pts))


    def test_a_stalled_feeder_is_caught_as_an_underrun(self):


        lines, underrun = self.mod.simulate_queue_segments(self.job, stall_s=2.0)
        self.assertTrue(underrun)
        self.assertTrue(any("UNDERRUN CAUGHT" in ln for ln in lines))
        self.assertTrue(any("stopped 1 time(s)" in ln for ln in lines))

    def test_the_arm_stopping_is_an_underrun_even_with_points_left(self):
        moving = [True]
        pts = self.flat_points(n=20)

        def submit(p):
            if p["time_from_start"] > 0.4:
                moving[0] = False
            return self.mod.QueueReply(self.codes.SUCCESS)

        f = self.feeder(pts, submit=submit, now=lambda: 0.0,
                        sleep=lambda s: None, in_motion=lambda: moving[0])
        with self.assertRaises(self.mod.QueueUnderrun) as ctx:
            f.feed()
        self.assertIn("STOPPED", str(ctx.exception))

    def test_a_thin_cushion_alone_does_not_abort_a_moving_arm(self):


        clock = [0.0]
        pts = self.flat_points(n=12, dt=0.5)

        def submit(p):
            clock[0] += 0.9
            return self.mod.QueueReply(self.codes.SUCCESS)

        warnings = []
        f = self.feeder(pts, submit=submit, now=lambda: clock[0],
                        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                        in_motion=lambda: True, log=warnings.append)
        report = f.feed()
        self.assertEqual(report["points"], len(pts))
        self.assertGreater(report["thin_lead"], 0)
        self.assertTrue(any("barely keeping up" in w for w in warnings))

    def test_without_robot_status_an_empty_cushion_is_the_only_signal_left(self):
        clock = [0.0]
        pts = self.flat_points(n=12, dt=0.5)

        def submit(p):
            clock[0] += 0.7
            return self.mod.QueueReply(self.codes.SUCCESS)

        f = self.feeder(pts, submit=submit, now=lambda: clock[0],
                        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                        in_motion=lambda: None)
        with self.assertRaises(self.mod.QueueUnderrun) as ctx:
            f.feed()
        self.assertIn("robot_status", str(ctx.exception))

    def test_anything_that_is_not_success_or_busy_stops_the_pass(self):


        f = self.feeder(
            self.flat_points(n=5),
            submit=lambda p: self.mod.QueueReply(self.codes.WRONG_MODE, "nope"),
            now=lambda: 0.0, sleep=lambda s: None, in_motion=lambda: True)
        with self.assertRaises(self.mod.QueueRejected) as ctx:
            f.feed()
        self.assertIn("WRONG_MODE", str(ctx.exception))

    def test_a_point_that_omits_an_axis_is_rejected_by_the_model_too(self):


        sim = self.mod.SimulatedPointQueue(n_joints=7, total_points=5)
        f = self.feeder(self.flat_points(n=5, joints=6), submit=sim.submit,
                        codes=sim.codes, now=sim.now, sleep=sim.sleep,
                        in_motion=sim.in_motion)
        with self.assertRaises(self.mod.QueueRejected) as ctx:
            f.feed()
        self.assertIn("INVALID_JOINT_LIST", str(ctx.exception))

    def test_a_controller_that_stops_consuming_is_given_up_on(self):

        clock = [0.0]
        f = self.feeder(
            self.flat_points(n=5),
            submit=lambda p: self.mod.QueueReply(self.codes.BUSY, "busy"),
            now=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + s),
            in_motion=lambda: True)
        with self.assertRaises(self.mod.QueueStalled):
            f.feed()
        self.assertGreater(clock[0], self.mod.BUSY_GIVE_UP_S)

    def test_the_result_codes_are_resolved_and_never_invented(self):


        class Partial:
            SUCCESS = 0
            BUSY = 1

        with self.assertRaises(RuntimeError) as ctx:
            self.mod.QueueCodes(Partial)
        self.assertIn("WRONG_MODE", str(ctx.exception))
        self.assertEqual(self.codes.name(self.codes.BUSY), "BUSY")


    def test_the_runner_runs_the_simulation_in_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(json.dumps(self.job), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.mod.main([str(p), "--dry-run"]), 0)
            text = out.getvalue()
        self.assertIn("QUEUE", text)
        self.assertIn("BUSY replies answered", text)

    def test_the_dry_run_fails_when_an_injected_stall_goes_unnoticed(self):

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(json.dumps(self.job), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                caught = self.mod.main([str(p), "--dry-run", "--sim-stall", "2.0"])
                missed = self.mod.main([str(p), "--dry-run",
                                        "--sim-stall", "0.001"])
        self.assertEqual(caught, 0)
        self.assertEqual(missed, 1)

    def test_an_over_long_goal_segment_is_refused_before_anything_moves(self):


        bad = json.loads(json.dumps(self.job))
        bad["version"] = 1
        for step in bad["steps"]:
            if step.get("mode") == MODE_QUEUE:
                step["mode"] = "goal"
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                self.mod.load_job(p)
        self.assertIn("point_queue=True", str(ctx.exception))

    def test_a_version_1_job_still_loads(self):
        tp, origin, scene, torch = wall_setup(layers=2)
        job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="OLD", platter=_P,
                           scene=scene, torch=torch)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(job.to_json(), encoding="utf-8")
            self.assertEqual(self.mod.load_job(p)["version"], 1)


class HeldAxes(unittest.TestCase):


    def test_goal_yaml_refuses_to_guess_a_held_axis(self):
        tp, origin, scene, torch = wall_setup()
        job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="HOLD", platter=_P, scene=scene, torch=torch,
                           hold_axes=("s1_axis_1",))
        with self.assertRaises(Ros2Refused) as ctx:
            job.goal_yaml(0)
        self.assertIn("s1_axis_1", ctx.exception.detail)
        text = job.goal_yaml(0, hold_rad={"s1_axis_1": 0.25})
        self.assertIn("s1_axis_1", text)
        self.assertTrue(any("holds them at their current position" in n
                            for n in job.notes))

    def test_the_held_axis_lands_in_every_row(self):
        tp, origin, scene, torch = wall_setup()
        job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="HOLD", platter=_P, scene=scene, torch=torch,
                           hold_axes=("s1_axis_1",))
        text = job.goal_yaml(0, hold_rad={"s1_axis_1": 0.25})
        rows = [ln for ln in text.splitlines() if ln.strip().startswith("- positions:")]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.count(","), len(JOINT_NAMES))
            self.assertIn("0.250000", row)


class ArcIsGated(unittest.TestCase):


    def test_weld_without_a_map_is_refused(self):
        tp, origin, scene, torch = wall_setup()
        with self.assertRaises(EmitRefused) as ctx:
            emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="W", platter=_P, scene=scene, torch=torch, weld=True)
        self.assertEqual(ctx.exception.reason, "arc-io")
        self.assertIn("CIOPRG.LST", ctx.exception.detail)

    def test_an_unverified_map_is_refused(self):
        tp, origin, scene, torch = wall_setup()
        io = ArcIo(verified=False,
                   on=(IoPoint(27010, 1, "WELD START"),),
                   off=(IoPoint(27010, 0, "WELD START"),))
        with self.assertRaises(EmitRefused) as ctx:
            emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="W", platter=_P, scene=scene, torch=torch,
                      weld=True, arc_io=io)
        self.assertIn("verified", ctx.exception.detail)

    def test_the_shipped_template_is_not_armed(self):
        io = ArcIo.load(ROOT / "examples" / "arc_io_lc50s.json")
        self.assertFalse(io.verified)
        self.assertTrue(io.validation_errors())

    def test_the_hc10_template_is_not_armed_either(self):


        io = ArcIo.load(ROOT / "examples" / "arc_io_hc10.json")
        self.assertFalse(io.verified)
        self.assertTrue(io.validation_errors())
        self.assertEqual(io.on, ())
        self.assertEqual(io.off, ())

    def test_the_string_false_does_not_verify_the_map(self):


        raw = json.loads((ROOT / "examples" / "arc_io_lc50s.json").read_text("utf-8"))
        raw.update(verified="false", source="CIOPRG.LST p3",
                   reviewed_by=["a", "b"],
                   on=[{"address": 27010, "value": 1}],
                   off=[{"address": 27010, "value": 0}])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "arc_io.json"
            p.write_text(json.dumps(raw), encoding="utf-8")
            io = ArcIo.load(p)
        self.assertIs(io.verified, False)

        tp, origin, scene, torch = wall_setup()
        with self.assertRaises(EmitRefused) as ctx:
            emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="W", platter=_P,
                      scene=scene, torch=torch, weld=True, arc_io=io)
        self.assertEqual(ctx.exception.reason, "arc-io")
        self.assertIn("verified", ctx.exception.detail)

    def test_verified_needs_two_reviewers(self):
        io = ArcIo(verified=True, source="CIOPRG.LST p3", reviewed_by=("a",),
                   on=(IoPoint(27010, 1),), off=(IoPoint(27010, 0),))
        self.assertIn("two reviewers", "; ".join(io.validation_errors()))

    def test_an_off_that_does_not_undo_on_is_refused(self):


        io = ArcIo(verified=True,
                   on=(IoPoint(27010, 1), IoPoint(27011, 1)),
                   off=(IoPoint(27010, 0),))
        errs = "; ".join(io.validation_errors())
        self.assertIn("27011", errs)
        self.assertIn("not be extinguished", errs)

    def test_addresses_outside_what_motoros2_writes_are_refused(self):


        self.assertTrue(IoPoint(20010, 1).errors())
        self.assertTrue(IoPoint(37010, 1).errors())
        self.assertTrue(IoPoint(27018, 1).errors())
        self.assertFalse(IoPoint(27010, 1).errors())
        self.assertFalse(IoPoint(10010, 0).errors())
        self.assertFalse(IoPoint(60010, 1).errors())

    def test_a_verified_map_arms_the_arc_steps(self):
        tp, origin, scene, torch = wall_setup()
        io = ArcIo(verified=True, source="CIOPRG.LST p3", reviewed_by=("a", "b"),
                   on=(IoPoint(27010, 1, "WELD START"),),
                   off=(IoPoint(27010, 0, "WELD START"),))
        job, summary = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="W", platter=_P, scene=scene, torch=torch,
                                 weld=True, arc_io=io)
        self.assertTrue(summary["welding"])
        ios = [s for s in job.steps if isinstance(s, IoStep)]
        self.assertTrue(all(s.writes for s in ios))
        self.assertEqual(ios[0].writes[0].value, 1)
        self.assertEqual(ios[1].writes[0].value, 0)


def _hc10_park_toolpath(length_mm=20.0):


    tp = Toolpath()
    tp.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
    tp.moves.append(Move(length_mm, 0.0, 5.0, 500.0, True, 0, 2))
    return tp


def _hc10_constant_solve(xyz):


    return HC_PARK_Q


class AnHC10JobCarriesHC10sOwnNumbers(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.job = build_ros2_job(_hc10_park_toolpath(), _hc10_constant_solve,
                                 name="HC10AIR", robot=HC10_MODEL)

    def test_emits_the_hc10_joint_names(self):


        self.assertEqual(self.job.joint_names, HC10_MODEL.joint_names)
        self.assertEqual(self.job.joint_names, ARM_JOINT_NAMES)

    def test_opens_and_closes_at_hc10_park_not_ar1440_park(self):
        opens = self.job.segments[0].points[0].positions_rad
        closes = self.job.segments[-1].points[-1].positions_rad
        want = tuple(math.radians(v) for v in HC_PARK_Q)
        for a, b in zip(opens, want):
            self.assertAlmostEqual(a, b, places=9)
        for a, b in zip(closes, want):
            self.assertAlmostEqual(a, b, places=9)

    def test_it_carries_the_hc10s_own_per_axis_start_tolerance(self):


        self.assertIsNotNone(HC10_MODEL.pulses_per_deg)
        tol = HC10_MODEL.start_tolerance_deg()
        self.assertEqual(len(tol), 6)
        self.assertTrue(all(0.0 < t < 0.5 for t in tol), tol)
        self.assertEqual(self.job.start_tolerance_deg, tol)
        d = json.loads(self.job.to_json())
        self.assertEqual(d["start_tolerance_deg"], list(tol))
        self.assertEqual([n for n in self.job.notes
                          if "start_tolerance_deg" in n], [])

    def test_the_park_is_the_taught_record_not_a_nominal_posture(self):


        from waam_pipeline.backends.jbi import HC_PARK_PULSES
        from waam_pipeline.robots.hc10 import PULSES_PER_DEG as HC_RATES
        self.assertEqual(
            tuple(int(round(a * r)) for a, r in zip(HC10_MODEL.park_q, HC_RATES)),
            HC_PARK_PULSES)
        self.assertNotEqual(HC10_MODEL.park_q, (0.0, 0.0, 200.0, 0.0, 0.0, 0.0))

    def test_the_ar1440_default_still_carries_real_numbers(self):


        self.assertIsNotNone(AR1440_MODEL.pulses_per_deg)
        tol = AR1440_MODEL.start_tolerance_deg()
        self.assertEqual(len(tol), 6)
        self.assertTrue(all(t > 0.0 for t in tol))


class HC10SpeedLimitsAreHC10sNotAR1440s(unittest.TestCase):


    K_DEG_PER_MM = 20.0
    MOVE_LENGTH_MM = 1.0


    WELD_V_AR1440 = "10"
    WELD_V_HC10 = "10"
    NEEDED_RATE_DEG_S = K_DEG_PER_MM * 10.0

    @classmethod
    def solve(cls, xyz):
        x, _, _ = xyz
        q = list(HC_PARK_Q)
        q[0] = HC_PARK_Q[0] + cls.K_DEG_PER_MM * x
        return tuple(q)

    def test_hc10_refuses_a_rate_its_own_s_axis_cannot_hold(self):
        with self.assertRaises(Ros2Refused) as ctx:
            build_ros2_job(_hc10_park_toolpath(length_mm=self.MOVE_LENGTH_MM),
                           self.solve, name="HCFAST", weld_v=self.WELD_V_HC10,
                           robot=HC10_MODEL)
        self.assertEqual(ctx.exception.reason, "speed")
        self.assertIn("axis rate limit", ctx.exception.detail)

    def test_the_identical_job_builds_clean_on_the_ar1440(self):


        job = build_ros2_job(_hc10_park_toolpath(length_mm=self.MOVE_LENGTH_MM),
                             self.solve, name="ARFAST",
                             weld_v=self.WELD_V_AR1440)
        self.assertEqual(job.joint_names, AR1440_MODEL.joint_names)
        self.assertGreater(job.stats.get("segments", 0), 0)


class SameGate(unittest.TestCase):


    def test_a_fouling_job_is_refused_the_same_way(self):
        tp = wall(150.0, 6, 2.0)


        origin = cell_lc.snap_toolpath_to_platter(tp, ORIGIN_X, -300.0,
                                                  substrate_mm=SUBSTRATE_MM)
        torch = TorchModel.yaskawa_accufast_45()
        scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=True)
        solve = cell_lc.pulse_solver(origin)
        with self.assertRaises(EmitRefused) as ros:
            emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="FOUL", platter=_P, scene=scene, torch=torch)
        with self.assertRaises(EmitRefused) as jbi:
            emit_job(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="FOUL", platter=_P, arc_file=12,
                     scene=scene, torch=torch, date="2026/01/01 00:00")
        self.assertEqual(ros.exception.reason, "clearance")
        self.assertEqual(ros.exception.detail, jbi.exception.detail)

    def test_the_invented_positions_are_checked_too(self):


        seen = []
        tp, origin, scene, torch = wall_setup()
        build_ros2_job(tp, cell_lc.pulse_solver(origin), name="G",
                       guard=lambda xyz, where: seen.append(where))
        self.assertTrue(any(w.startswith("hover before layer") for w in seen))
        self.assertIn("final retreat", seen)


        tp2 = Toolpath()
        tp2.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
        tp2.moves.append(Move(50.0, 0.0, 5.0, 500.0, True, 0, 2))
        tp2.moves.append(Move(50.0, 0.0, 7.0, 3000.0, False, 0, 3))
        tp2.moves.append(Move(0.0, 0.0, 7.0, 500.0, True, 1, 4))
        seen2 = []
        build_ros2_job(tp2, cell_lc.pulse_solver(origin), name="G2",
                       guard=lambda xyz, where: seen2.append(where))
        self.assertTrue(any(w.startswith("descend to layer") for w in seen2))

    def test_a_first_move_that_deposits_is_refused_here_too(self):
        tp = Toolpath()
        tp.moves.append(Move(10.0, 0.0, 5.0, 500.0, True, 0, 1))
        _, origin, _, _ = wall_setup()
        with self.assertRaises(ValueError) as ctx:
            build_ros2_job(tp, cell_lc.pulse_solver(origin), name="BAD")
        self.assertIn("first move deposits", str(ctx.exception))


        _, origin2, scene, torch = wall_setup()
        with self.assertRaises(EmitRefused) as ctx2:
            emit_ros2(tp, cell_lc.pulse_solver(origin2), bead_height_mm=2.0, name="BAD", platter=_P,
                      scene=scene, torch=torch)
        self.assertEqual(ctx2.exception.reason, "argument")


class Artefact(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        tp, origin, scene, torch = wall_setup(layers=2)
        cls.job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="ART", platter=_P, scene=scene, torch=torch)

    def test_json_round_trips(self):
        d = json.loads(self.job.to_json())
        self.assertEqual(d["format"], "waam-pipeline/ros2-job")
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["joint_names"], list(JOINT_NAMES))
        self.assertEqual(sum(1 for s in d["steps"] if s["kind"] == "trajectory"),
                         len(self.job.segments))

    def test_the_start_tolerance_is_one_the_controller_would_accept(self):


        d = json.loads(self.job.to_json())
        tol = d["start_tolerance_deg"]
        self.assertEqual(len(tol), len(d["joint_names"]))
        for name, t in zip(d["joint_names"], tol):
            self.assertGreater(t, 0.0, name)
            self.assertLess(t, 0.07, f"{name}: {t} deg is looser than the "
                                     "30 pulses MotoROS2 allows")

    def test_goal_yaml_is_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        d = yaml.safe_load(self.job.goal_yaml(0))
        self.assertEqual(d["trajectory"]["joint_names"], list(JOINT_NAMES))
        pts = d["trajectory"]["points"]
        self.assertEqual(pts[0]["time_from_start"], {"sec": 0, "nanosec": 0})
        for p in pts:
            self.assertEqual(len(p["positions"]), len(JOINT_NAMES))
            self.assertEqual(len(p["velocities"]), len(JOINT_NAMES))

    def test_an_ungated_job_is_not_byte_identical_to_a_gated_one(self):


        tp, origin, scene, torch = wall_setup(layers=2)
        gated, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="ART",
                             platter=_P, scene=scene, torch=torch)


        ungated = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="ART",
                                 weld_v="3.3")
        self.assertNotEqual(gated.to_json(), ungated.to_json())
        self.assertEqual(json.loads(gated.to_json())["clearance"],
                         "yes (guarded)")
        self.assertEqual(json.loads(ungated.to_json())["clearance"],
                         "NO - UNCHECKED")


        a = json.loads(gated.to_json())
        b = json.loads(ungated.to_json())
        for k in ("steps", "joint_names", "stats", "welding"):
            self.assertEqual(a[k], b[k], k)

    def test_an_empty_point_guard_cannot_earn_the_guarded_stamp(self):


        from waam_pipeline.clearance import point_guard
        self.assertIsNone(point_guard(None, None))
        tp, origin, _, _ = wall_setup(layers=2)
        job = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="ART",
                             guard=point_guard(None, None))
        self.assertEqual(json.loads(job.to_json())["clearance"],
                         "NO - UNCHECKED")

    def test_the_runner_refuses_an_unchecked_job(self):


        mod = runner_module()
        tp, origin, _, _ = wall_setup(layers=2)
        ungated = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="ART")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(ungated.to_json(), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                mod.main([str(p)])
            self.assertIn("UNCHECKED", str(ctx.exception))


            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(mod.main([str(p), "--dry-run"]), 0)
            self.assertIn("NO - UNCHECKED", out.getvalue())

    def test_the_runner_reads_what_the_emitter_writes(self):


        mod = runner_module()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(self.job.to_json(), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(mod.main([str(p), "--dry-run"]), 0)
            self.assertIn("TRAJ", out.getvalue())
            loaded = mod.load_job(p)
        text = mod.describe(loaded)
        self.assertIn("air moves only", text)
        self.assertIn("TRAJ", text)

    def test_the_runner_refuses_to_weld_an_unarmed_job(self):
        mod = runner_module()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "job.ros2.json"
            p.write_text(self.job.to_json(), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                mod.main([str(p), "--weld"])
        self.assertIn("weld=False", str(ctx.exception))


class ControllerConfig(unittest.TestCase):
    def test_every_repo_path_ros2_py_points_at_exists(self):


        src = ROOT / "src" / "waam_pipeline" / "backends" / "ros2.py"
        text = src.read_text(encoding="utf-8")
        prefixes = ("docs/", "ros2/", "robots/", "tests/", "examples/", "sim/")
        missing = []
        for tok in re.findall(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+", text):
            tok = tok.rstrip(".")
            if tok.startswith(prefixes) and not (ROOT / tok).exists():
                missing.append(tok)
        self.assertEqual(missing, [], "ros2.py points at files that are not "
                                      "in this repo")

    def test_the_shipped_config_names_the_urdf_joints(self):


        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        cfg = yaml.safe_load((ROOT / "ros2" / "motoros2_config.yaml")
                             .read_text(encoding="utf-8"))
        self.assertEqual(cfg["joint_names"][0], list(JOINT_NAMES))
        self.assertIn("agent_ip_address", cfg)

    def test_the_hc10_config_names_the_hc10_urdf_joints(self):


        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        cfg = yaml.safe_load((ROOT / "ros2" / "motoros2_config_hc10.yaml")
                             .read_text(encoding="utf-8"))
        self.assertEqual(cfg["joint_names"], [list(HC10_MODEL.joint_names)])
        self.assertEqual(cfg["joint_names"][0], list(ARM_JOINT_NAMES))
        self.assertIn("agent_ip_address", cfg)
        self.assertNotEqual(cfg["node_name"], "motoros2_lc50s")

    def test_the_two_configs_do_not_share_a_node_name_or_agent_ip(self):


        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        lc = yaml.safe_load((ROOT / "ros2" / "motoros2_config.yaml")
                            .read_text(encoding="utf-8"))
        hc = yaml.safe_load((ROOT / "ros2" / "motoros2_config_hc10.yaml")
                            .read_text(encoding="utf-8"))
        self.assertNotEqual(lc["node_name"], hc["node_name"])
        self.assertNotEqual(lc["agent_ip_address"], hc["agent_ip_address"])


if __name__ == "__main__":
    unittest.main()
