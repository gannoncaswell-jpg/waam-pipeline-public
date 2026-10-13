import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.backends.ros2 import (
    YRC1000_WRITABLE_BIT_RANGES, ArcIo, IoPoint,
)
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import PlatterState, emit_ros2
from waam_pipeline.slicer import wall
from waam_pipeline.toolpath import Move, Toolpath

ORIGIN_X, ORIGIN_Y, SUBSTRATE_MM = 1014.0, -180.0, 6.35


_P = PlatterState(clear=True, training_block_on=True, attested_by="test")


ARC_IO = ArcIo(verified=True, source="test", reviewed_by=("a", "b"),
               on=(IoPoint(27010, 1, "WELD START"),),
               off=(IoPoint(27010, 0, "WELD START"),))


def runner_module():


    spec = importlib.util.spec_from_file_location(
        "waam_ros2_runner_gates", ROOT / "ros2" / "waam_ros2_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def welding_job(*, point_queue=False, long_pass=False):

    if long_pass:
        tp = Toolpath()
        tp.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
        for i in range(1, 400):
            tp.moves.append(Move(i * 0.5, 0.0, 5.0, 500.0, True, 0, i + 1))
    else:
        tp = wall(150.0, 2, 2.0)
    origin = cell_lc.snap_toolpath_to_platter(tp, ORIGIN_X, ORIGIN_Y,
                                             substrate_mm=SUBSTRATE_MM)
    scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=True)
    job, _ = emit_ros2(tp, cell_lc.pulse_solver(origin), bead_height_mm=2.0, name="GATE",
                       platter=_P, scene=scene,
                       torch=TorchModel.yaskawa_accufast_45(),
                       weld=True, arc_io=ARC_IO, point_queue=point_queue)
    return json.loads(job.to_json())


class _FakeRequest:


    def __init__(self):
        self.address = None
        self.value = None


class _FakeIoService:


    Request = _FakeRequest


class _Reply:
    success = True
    message = ""


def bare_runner(mod, **attrs):


    r = object.__new__(mod.Runner)
    r.weld = False
    r.dry_arc = False
    r.arc_lit = False
    r.WriteSingleIO = _FakeIoService
    for k, v in attrs.items():
        setattr(r, k, v)
    return r


class ArcIoIsGatedByIntentNotByTheFile(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.mod = runner_module()
        cls.job = welding_job()

    def write_job(self, td, job) -> Path:
        p = Path(td) / "job.ros2.json"
        p.write_text(json.dumps(job, indent=2), encoding="utf-8")
        return p

    def test_the_emitted_welding_job_still_loads(self):


        with tempfile.TemporaryDirectory() as td:
            loaded = self.mod.load_job(self.write_job(td, self.job))
        self.assertTrue(loaded["welding"])
        writes = [w for s in loaded["steps"] if s["kind"] == "io"
                  for w in s["writes"]]
        self.assertTrue(writes)

    def test_a_file_that_declares_no_welding_but_carries_arc_writes_is_refused(self):


        tampered = dict(self.job, welding=False)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as ctx:
                self.mod.load_job(self.write_job(td, tampered))
        msg = str(ctx.exception)
        self.assertIn("welding", msg)
        self.assertIn("--dry-arc", msg)

    def test_a_file_that_declares_welding_with_no_arc_writes_is_refused(self):


        stripped = json.loads(json.dumps(self.job))
        for step in stripped["steps"]:
            if step["kind"] == "io":
                step["writes"] = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as ctx:
                self.mod.load_job(self.write_job(td, stripped))
        self.assertIn("no arc I/O writes", str(ctx.exception))

    def test_an_address_the_emitter_would_reject_is_refused_at_load(self):


        bad = json.loads(json.dumps(self.job))
        for step in bad["steps"]:
            if step["kind"] == "io" and step["writes"]:
                step["writes"][0]["address"] = 10018
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as ctx:
                self.mod.load_job(self.write_job(td, bad))
        self.assertIn("10018", str(ctx.exception))

    def test_an_address_outside_the_writable_ranges_is_refused_at_load(self):


        bad = json.loads(json.dumps(self.job))
        for step in bad["steps"]:
            if step["kind"] == "io" and step["writes"]:
                step["writes"][0]["address"] = 37010
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as ctx:
                self.mod.load_job(self.write_job(td, bad))
        self.assertIn("37010", str(ctx.exception))

    def test_the_runners_address_rules_are_the_emitters_address_rules(self):


        self.assertEqual(self.mod.YRC1000_WRITABLE_BIT_RANGES,
                         YRC1000_WRITABLE_BIT_RANGES)
        for address in (10010, 10018, 15127, 15128, 20010, 27010, 29567,
                        37010, 60010, 60647, 60648):
            for value in (0, 1):
                self.assertEqual(
                    bool(self.mod.io_write_errors(address, value)),
                    bool(IoPoint(address, value).errors()),
                    f"the runner and the emitter disagree about {address}")

    def test_do_io_refuses_to_write_without_weld_and_writes_nothing(self):


        step = next(s for s in self.job["steps"]
                    if s["kind"] == "io" and s["writes"])
        calls = []
        r = bare_runner(self.mod, weld=False,
                        call=lambda *a, **k: calls.append(a))
        with self.assertRaises(RuntimeError) as ctx:
            r.do_io(step)
        self.assertIn("--weld", str(ctx.exception))
        self.assertEqual(calls, [], "a write reached the controller anyway")
        self.assertFalse(r.arc_lit)

    def test_dry_arc_suppresses_the_writes_instead_of_refusing_them(self):


        step = next(s for s in self.job["steps"]
                    if s["kind"] == "io" and s["writes"])
        calls = []
        r = bare_runner(self.mod, weld=False, dry_arc=True,
                        call=lambda *a, **k: calls.append(a))
        out = io.StringIO()
        with redirect_stdout(out):
            r.do_io(step)
        self.assertIn("SUPPRESSED", out.getvalue())
        self.assertEqual(calls, [])
        self.assertFalse(r.arc_lit)

    def test_with_weld_the_writes_do_go_out(self):


        step = next(s for s in self.job["steps"]
                    if s["kind"] == "io" and s["writes"])
        sent = []

        def call(client, request, what, timeout=10.0):
            sent.append((request.address, request.value))
            return _Reply()

        r = bare_runner(self.mod, weld=True, call=call, write_io=object())
        with redirect_stdout(io.StringIO()):
            r.do_io(step)
        self.assertEqual(sent, [(w["address"], w["value"])
                                for w in step["writes"]])
        self.assertTrue(r.arc_lit)

    def test_weld_and_dry_arc_together_are_refused(self):

        with tempfile.TemporaryDirectory() as td:
            p = self.write_job(td, self.job)
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main([str(p), "--weld", "--dry-arc"])
        self.assertIn("contradict", str(ctx.exception))

    def test_a_welding_job_without_either_flag_is_still_refused(self):


        with tempfile.TemporaryDirectory() as td:
            p = self.write_job(td, self.job)
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main([str(p)])
        self.assertIn("--dry-arc", str(ctx.exception))


class _Tri:
    def __init__(self, val):
        self.val = val


class _Status:


    def __init__(self):
        self.e_stopped = _Tri(0)
        self.in_error = _Tri(0)
        self.motion_possible = _Tri(1)


class _JointState:
    def __init__(self, name, position):
        self.name = list(name)
        self.position = list(position)


class TheStartCheckMatchesTheControllersBudget(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.mod = runner_module()
        cls.job = welding_job()

    def runner_at(self, offsets_deg):

        import math
        first = next(s["points"][0]["positions"] for s in self.job["steps"]
                     if s["kind"] == "trajectory")
        names = self.job["joint_names"]
        pos = [p + math.radians(d) for p, d in zip(first, offsets_deg)]
        r = bare_runner(self.mod, job=self.job, auto_hold=True,
                        joint_state=_JointState(names, pos),
                        robot_status=_Status())
        r.wait_for = lambda *a, **k: None
        return r

    def test_the_robot_at_park_passes(self):


        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner_at([0.0] * 6).preflight(), [])

    def test_three_tenths_of_a_degree_off_on_L_is_refused(self):


        r = self.runner_at([0.0, 0.3, 0.0, 0.0, 0.0, 0.0])
        with self.assertRaises(RuntimeError) as ctx:
            with redirect_stdout(io.StringIO()):
                r.preflight()
        msg = str(ctx.exception)
        self.assertIn("starting posture", msg)
        self.assertIn("joint_2_l", msg)

    def test_the_old_flat_tolerance_would_have_let_it_through(self):


        stripped = json.loads(json.dumps(self.job))
        stripped.pop("start_tolerance_deg", None)
        import math
        first = next(s["points"][0]["positions"] for s in stripped["steps"]
                     if s["kind"] == "trajectory")
        pos = [p + math.radians(d)
               for p, d in zip(first, [0.0, 0.3, 0.0, 0.0, 0.0, 0.0])]
        r = bare_runner(self.mod, job=stripped, auto_hold=True,
                        joint_state=_JointState(stripped["joint_names"], pos),
                        robot_status=_Status())
        r.wait_for = lambda *a, **k: None
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(r.preflight(), [])
        self.assertIn("start_tolerance_deg", err.getvalue())
        self.assertIn("0.5", err.getvalue())

    def test_the_emitted_tolerances_are_the_ones_the_runner_uses(self):


        self.assertEqual(len(self.job["start_tolerance_deg"]),
                         len(self.job["joint_names"]))


        tol = self.job["start_tolerance_deg"]
        just_inside = [t * 0.9 for t in tol]
        just_outside = [t * 1.1 for t in tol]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.runner_at(just_inside).preflight(), [])
        with self.assertRaises(RuntimeError):
            with redirect_stdout(io.StringIO()):
                self.runner_at(just_outside).preflight()


class _ModeReply:


    class _Code:
        value = 1

    result_code = _Code()
    message = ""


class _Enum:
    READY = 1


class _Service:


    def __init__(self, name):
        self.name = name

    def wait_for_service(self, timeout_sec=10.0):
        return True


class _FirstQueuedPoint(Exception):
    pass


class _Req:
    def __init__(self):
        self.joint_names = []
        self.point = None
        self.address = None
        self.value = None


class _Msg:
    Request = _Req


def _fake_ros_messages():


    import types

    tm = types.ModuleType("trajectory_msgs")
    tm_msg = types.ModuleType("trajectory_msgs.msg")

    class JointTrajectoryPoint:
        def __init__(self):
            self.positions = []
            self.velocities = []
            self.time_from_start = None

    tm_msg.JointTrajectoryPoint = JointTrajectoryPoint
    tm.msg = tm_msg

    bi = types.ModuleType("builtin_interfaces")
    bi_msg = types.ModuleType("builtin_interfaces.msg")

    class Duration:
        def __init__(self, sec=0, nanosec=0):
            self.sec, self.nanosec = sec, nanosec

    bi_msg.Duration = Duration
    bi.msg = bi_msg
    return {"trajectory_msgs": tm, "trajectory_msgs.msg": tm_msg,
            "builtin_interfaces": bi, "builtin_interfaces.msg": bi_msg}


def queued_welding_job():


    from waam_pipeline.backends.ros2 import build_ros2_job
    from waam_pipeline.clearance import point_guard

    tp = Toolpath()
    tp.moves.append(Move(0.0, 0.0, 5.0, 3000.0, False, 0, 1))
    for i in range(1, 400):
        tp.moves.append(Move(i * 0.5, 0.0, 5.0, 500.0, True, 0, i + 1))
    origin = cell_lc.snap_toolpath_to_platter(tp, ORIGIN_X, ORIGIN_Y,
                                              substrate_mm=SUBSTRATE_MM)
    scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=True)
    torch = TorchModel.yaskawa_accufast_45()
    job = build_ros2_job(tp, cell_lc.pulse_solver(origin), name="QWELD",
                         guard=point_guard(torch, scene, 5.0),
                         weld=True, arc_io=ARC_IO, point_queue=True)
    return json.loads(job.to_json())


class NoModeChangeWhileTheArcIsLit(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.mod = runner_module()
        cls.job = queued_welding_job()

    def setUp(self):
        for name, module in _fake_ros_messages().items():
            sys.modules.setdefault(name, module)

    def drive(self, job):

        log = []
        mod = self.mod

        def call(client, request, what, timeout=10.0):
            log.append(what.split()[0])
            if what.startswith("write_single_io") or what == "arc":
                return _Reply()
            if what in ("start_traj_mode", "start_point_queue_mode",
                        "stop_traj_mode"):
                return _ModeReply() if what.startswith("start") else _Reply()
            raise AssertionError(f"unexpected service call {what!r}")

        def call_ready(client, request, what, timeout=10.0):
            log.append(what)
            if what == "queue_traj_point":
                raise _FirstQueuedPoint
            raise AssertionError(f"unexpected service call {what!r}")

        r = bare_runner(
            mod, job=job, weld=True, auto_hold=True,
            mode=None, queue_fed=False, goal_time_tolerance_s=1.0,
            MotionReadyEnum=_Enum, Trigger=_Msg, StartTrajMode=_Msg,
            StartPointQueueMode=_Msg, QueueTrajPoint=_Msg,
            queue_codes=mod.QueueCodes(mod.SimulatedPointQueue.Codes),
            start_traj=_Service("start_traj"),
            start_queue=_Service("start_queue"),
            stop_traj=_Service("stop_traj"),
            queue_point=_Service("queue_point"),
            write_io=_Service("write_io"),
            joint_state=_JointState(job["joint_names"], [0.0] * 6),
            call=call, call_ready=call_ready)
        r.preflight = lambda: []
        r.send_segment = lambda step, extra: log.append("send_goal")
        r.spin = lambda seconds: None
        r.spin_sleep = lambda seconds: None
        r.in_motion = lambda: True
        r.wait_for = lambda *a, **k: None
        with self.assertRaises(_FirstQueuedPoint):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                r.run()
        return log, r

    def test_the_job_has_the_step_order_the_defect_needs(self):

        kinds = [(s["kind"], s.get("mode", ""), s.get("arc", ""))
                 for s in self.job["steps"]]
        self.assertEqual(
            kinds,
            [("trajectory", "goal", ""), ("wait", "", ""),
             ("trajectory", "goal", ""), ("io", "", "on"),
             ("trajectory", "queue", ""), ("io", "", "off"),
             ("trajectory", "goal", "")])

    def test_no_mode_transition_lands_between_arc_on_and_the_first_point(self):


        log, _ = self.drive(self.job)
        self.assertIn("write_single_io", log)
        self.assertIn("queue_traj_point", log)
        arc_on = log.index("write_single_io")
        first_point = log.index("queue_traj_point")
        self.assertLess(arc_on, first_point)
        between = log[arc_on + 1:first_point]
        for forbidden in ("stop_traj_mode", "start_point_queue_mode",
                          "start_traj_mode"):
            self.assertNotIn(forbidden, between,
                             f"{forbidden} runs with the arc lit: {log}")

    def test_the_queue_mode_is_entered_before_the_arc_comes_on(self):


        log, _ = self.drive(self.job)
        self.assertIn("start_point_queue_mode", log)
        self.assertLess(log.index("start_point_queue_mode"),
                        log.index("write_single_io"))

    def test_a_transition_with_the_arc_lit_is_refused_outright(self):


        _, r = self.drive(self.job)
        r.arc_lit = True
        r.mode = "goal"
        with self.assertRaises(RuntimeError) as ctx:
            r.ensure_mode("queue")
        self.assertIn("ARC LIT", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx2:
            r.leave_mode()
        self.assertIn("ARC LIT", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
