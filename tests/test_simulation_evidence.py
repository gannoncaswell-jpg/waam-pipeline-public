import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from waam_pipeline import arcparams, station
from waam_pipeline.robots import ar1440, hc10
from sim.kinematics_audit import audit_model


class TestKinematicCounterexamples(unittest.TestCase):
    def test_all_six_rates_remain_consistent_with_recorded_controller_ratios(self):


        ratios = [1640/13, 800/7, 125, 341/4, 1120/13, 10192/255]
        expected = np.asarray(ratios) * 4096 / 360
        np.testing.assert_allclose(ar1440.PULSES_PER_DEG, expected, rtol=5e-6, atol=0)

    def test_straight_wrist_split_stays_inside_both_joint_limits(self):
        for r in (-150, -140, 0, 140, 150):
            for t in (-210, -180, -40, 0, 40, 180, 210):
                with self.subTest(r=r, t=t):
                    target = ar1440.fk([0, 0, 0, r, 0, t])["flange"]
                    q = ar1440.ik(target)
                    self.assertIsNotNone(q)
                    np.testing.assert_allclose(ar1440.fk(q)["flange"], target, atol=1e-7)

    def test_non_rigid_or_nonfinite_poses_are_not_robot_poses(self):
        for model in (ar1440, hc10):
            valid = model.fk([0, 10, -20, 30, 40, 50])["flange"]
            for kind in ("nan", "scaled", "reflected", "last_row"):
                bad = valid.copy()
                if kind == "nan": bad[0, 0] = np.nan
                if kind == "scaled": bad[:3, :3] *= 2
                if kind == "reflected": bad[:3, 0] *= -1
                if kind == "last_row": bad[3, 0] = 1
                with self.subTest(model=model.__name__, kind=kind):
                    with self.assertRaises((ValueError, hc10.IkRefused)):
                        model.ik(bad)


class TestPhysicalInputs(unittest.TestCase):
    def test_clearance_rounding_cannot_erase_penetration_before_gating(self):
        from waam_pipeline.clearance import Scene, TorchModel, CONTACT_OK, check_point, blocking
        from sim.pybullet_driver import _violation
        scene = Scene(boxes=[("charles_platter", np.array([-10.,-10.,-1.]),
                             np.array([10.,10.,0.]))])
        hits = check_point((0,0,-.004), TorchModel(), scene, contact_ok=CONTACT_OK)
        self.assertTrue(blocking(hits))
        self.assertAlmostEqual(hits[0].clearance_mm, -.004)
        v = _violation(0, 1, (0,0,0), "bead(line 1)", -.004)
        self.assertTrue(blocking([v]))
        self.assertFalse(blocking([v], max_penetration_mm=.005))

    def test_cylinder_slices_reproduce_analytic_polygon_perimeter_and_height(self):
        from test_server import cylinder
        from waam_pipeline.slicer import slice_mesh
        mesh = cylinder(radius=7., height=8., around=32)
        tp = slice_mesh(mesh, layer_height_mm=2., point_deviation_mm=0.)
        self.assertEqual(len(tp.layers()), 4)
        length = sum(np.linalg.norm(np.asarray(b.target)-a.target)
                     for a, b in zip(tp.moves, tp.moves[1:]) if b.deposit)
        self.assertAlmostEqual(length, 4*2*32*7*np.sin(np.pi/32), places=5)
        self.assertAlmostEqual(max(m.z for m in tp.moves), 8.)

    def test_ctwd_is_not_electrode_extension_or_a_current_only_classification(self):


        self.assertIn("not validated", arcparams.ctwd_warnings(12, 165)[0])
        for mode, extension in (("short-circuit", .5*25.4), ("pulsed-spray", 25.4)):
            notes = arcparams.ctwd_warnings(extension+3, 165, observed_mode=mode, arc_length_mm=3)
            self.assertFalse(any("above the" in n or "below the" in n for n in notes))
        with self.assertRaises(ValueError):
            arcparams.ctwd_warnings(12, 165, observed_mode="spray", arc_length_mm=13)

    def test_retention_loss_increases_required_wire_not_deposited_mass(self):


        kwargs = dict(layer_height_mm=2, bead_width_mm=7, travel_mm_min=400)
        ideal = arcparams.solve(**kwargs)
        actual = arcparams.solve(**kwargs, electrode_efficiency=.93)
        self.assertAlmostEqual(actual.wfs_m_min, ideal.wfs_m_min/.93)
        self.assertAlmostEqual(actual.deposition_kg_h, ideal.deposition_kg_h)
        self.assertAlmostEqual(actual.wire_consumption_kg_h*.93, actual.deposition_kg_h)
        self.assertAlmostEqual(arcparams.deposited_area_mm2(
            actual.wire_dia_mm, actual.wfs_m_min, 400, electrode_efficiency=.93), 14)
        self.assertAlmostEqual(arcparams.wfs_for_layer_height(
            2, 7, 400, electrode_efficiency=.93), actual.wfs_m_min)
        for eta in (0, -1, 1.01, np.nan, np.inf):
            with self.subTest(eta=eta), self.assertRaises(ValueError):
                arcparams.solve(**kwargs, electrode_efficiency=eta)

    def test_wire_and_heat_reject_nan_and_nonphysical_efficiency(self):
        sol = arcparams.solve(layer_height_mm=2, bead_width_mm=7, travel_mm_min=400)
        for x in (np.nan, np.inf, -1, 0):
            with self.subTest(wire=x), self.assertRaises(ValueError):
                arcparams.wire_area_mm2(x)
        for eta in (np.nan, np.inf, -0.1, 1.1):
            with self.subTest(heat_efficiency=eta), self.assertRaises(ValueError):
                sol.heat_input_kj_mm(20, 160, efficiency=eta)

    def test_melting_inverse_has_correct_linear_limit(self):

        for b in (1e-15, 1e-20, 1e-25):
            current = arcparams.current_for_wfs(4, 10, a=.02, b=b)
            self.assertAlmostEqual(current, 200, delta=1e-5)
            self.assertAlmostEqual(arcparams.melting_rate_m_min(current, 10, .02, b), 4)

    def test_station_axis_cannot_be_fitted_to_a_line_or_repeated_point(self):
        for pts in ([[0,0,0]]*3, [[0,0,0], [1,0,0], [2,0,0]],
                    [[0, 0]]*3, [[np.nan, 0, 0]]*3):
            with self.subTest(points=pts), self.assertRaises(ValueError):
                station.fit_station_axis(pts)

    def test_station_residual_includes_out_of_plane_error(self):
        theta = np.linspace(0, 2*np.pi, 8, endpoint=False)
        pts = np.column_stack([100*np.cos(theta), 100*np.sin(theta),
                               np.tile([-3., 3.], 4)])
        fit = station.fit_station_axis(pts)
        self.assertGreaterEqual(fit.residual_mm, 2.99)


class TestAuditFiniteValues(unittest.TestCase):


    def setUp(self):
        names = [j.name for j in ar1440.AR1440_JOINTS] + ["flange"]
        self.frames = {name: np.eye(4) for name in names}
        rows = []
        for i, name in enumerate(names):
            row = [None] * 13
            row[0], row[1], row[12] = i, name.encode(), name.encode()
            rows.append(row)
        self.state = [np.zeros(3), np.array([0., 0., 0., 1.]),
                      np.zeros(3), np.array([0., 0., 0., 1.]),
                      np.zeros(3), np.array([0., 0., 0., 1.])]
        self.engine = mock.Mock()
        self.engine.connect.return_value = 0
        self.engine.getNumJoints.return_value = len(rows)
        self.engine.getJointInfo.side_effect = lambda body, i, **kw: rows[i]
        self.engine.getLinkState.return_value = self.state
        self.engine.getMatrixFromQuaternion.return_value = np.eye(3).ravel()


        for patcher in (mock.patch.dict(sys.modules, pybullet=self.engine),
                        mock.patch.object(ar1440, "fk", return_value=self.frames)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_nonfinite_refused(self):
        self.engine.disconnect.reset_mock()
        with self.assertRaisesRegex(ValueError, "non-finite"):
            audit_model("ar1440", samples=1)
        self.engine.disconnect.assert_called_once_with(physicsClientId=0)

    def test_finite_fixture_passes(self):
        report = audit_model("ar1440", samples=1)
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(report["links"]), len(self.frames))
        self.engine.disconnect.assert_called_once_with(physicsClientId=0)

    def test_nonfinite_engine_states_are_refused(self):
        for field in range(len(self.state)):
            for value in (np.nan, np.inf, -np.inf):
                with self.subTest(field=field, value=value):
                    state = [v.copy() for v in self.state]
                    state[field][0] = value
                    self.engine.getLinkState.return_value = state
                    self.assert_nonfinite_refused()

    def test_nonfinite_fk_frames_are_refused(self):
        for component in ((0, 3), (0, 0), (3, 0)):
            for value in (np.nan, np.inf, -np.inf):
                with self.subTest(component=component, value=value):
                    frame = np.eye(4)
                    frame[component] = value
                    with mock.patch.dict(self.frames, flange=frame):
                        self.assert_nonfinite_refused()

    def test_nonfinite_converted_rotations_are_refused(self):
        for value in (np.nan, np.inf, -np.inf):
            with self.subTest(value=value):
                rotation = np.eye(3)
                rotation[0, 0] = value
                self.engine.getMatrixFromQuaternion.return_value = rotation.ravel()
                self.assert_nonfinite_refused()

    def test_nonfinite_computed_errors_are_refused(self):
        for function in ("np.linalg.norm", "rotation_error_deg"):
            for value in (np.nan, np.inf, -np.inf):
                with self.subTest(function=function, value=value):
                    with mock.patch("sim.kinematics_audit." + function,
                                    return_value=value):
                        self.assert_nonfinite_refused()

    def test_finite_positions_with_overflowing_error_are_refused(self):


        self.state[4][0] = 1e200
        with np.errstate(over="ignore", invalid="ignore"):
            self.assert_nonfinite_refused()


@unittest.skipUnless(importlib.util.find_spec("pybullet"), "requires real PyBullet")
class TestEngineKinematics(unittest.TestCase):
    def test_engine_primitive_distances_against_closed_form_geometry(self):
        import pybullet as p
        from types import SimpleNamespace
        from sim.pybullet_driver import BodySpec, PyBulletDriver
        from waam_pipeline.clearance import _point_box_dist
        client = p.connect(p.DIRECT)
        try:
            sphere = p.createCollisionShape(p.GEOM_SPHERE, radius=.002, physicsClientId=client)
            probe = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=sphere, physicsClientId=client)
            specs = [BodySpec("box", "box", (0,0,0), (0,0,0,1), half_extents=(.01,.02,.03)),
                     BodySpec("capsule", "capsule", (0,0,0), (0,0,0,1), radius=.003, length=.020)]
            factory = SimpleNamespace(_p=p, client=client, gui=False)
            rng = np.random.default_rng(20260908)
            for kind, spec in enumerate(specs):


                body = PyBulletDriver._make(factory, spec)
                for xyz in rng.uniform(-40, 40, (200, 3)):
                    p.resetBasePositionAndOrientation(probe, xyz/1000, [0,0,0,1], physicsClientId=client)
                    rows = p.getClosestPoints(probe, body, 1., physicsClientId=client)
                    self.assertTrue(rows)
                    if kind == 0:
                        expected = _point_box_dist(xyz[None], np.array([-10,-20,-30]),
                                                   np.array([10,20,30]))[0] - 2
                    else:
                        expected = np.linalg.norm(xyz - [0,0,np.clip(xyz[2], -10, 10)]) - 5
                    self.assertAlmostEqual(rows[0][8]*1000, expected, delta=1e-5)
        finally:
            p.disconnect(physicsClientId=client)

    def test_both_models_match_all_links_over_seeded_workspace(self):
        for model in ("ar1440", "hc10dtp"):
            with self.subTest(model=model):
                report = audit_model(model, samples=250)
                self.assertTrue(report["passed"], report)

    def test_a_one_mm_joint_frame_mutation_is_detected(self):


        tree = ET.parse(ROOT / "robots/ar1440.urdf")
        for mesh in tree.findall(".//mesh"):
            mesh.set("filename", str((ROOT / "robots" / mesh.get("filename")).resolve()))
        origin = tree.find(".//joint[@name='joint_3_u']/origin")
        origin.set("xyz", "0 0 0.615")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mutated.urdf"
            tree.write(path)
            report = audit_model("ar1440", samples=10, urdf_path=path)
        self.assertFalse(report["passed"])
        self.assertGreater(report["links"]["joint_3_u"]["position_mm"], .99)
