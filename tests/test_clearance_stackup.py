from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import clearance as c


def path(*points):

    return SimpleNamespace(moves=[
        SimpleNamespace(target=xyz, deposit=deposit, line_no=i + 1)
        for i, (xyz, deposit) in enumerate(points)
    ])


def square():
    points = [((0, 0, 0), False)]
    for layer in range(12):
        z = layer * 3.0
        if layer:
            points.append(((0, 0, z), False))
        points.extend((xyz, True) for xyz in (
            (20, 0, z), (20, 20, z), (0, 20, z), (0, 0, z)
        ))
    return path(*points)


def encoded(hits):
    return json.dumps([asdict(hit) for hit in hits], sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class StackupRegression(unittest.TestCase):
    def setUp(self):
        self.torch = c.TorchModel.yaskawa_accufast_45()
        self.legacy = replace(self.torch, stackup=None)

    def test_profile_and_unknown_dimensions(self):
        profile = json.loads((ROOT / "examples" / "cell_lc50s.json").read_text())
        self.assertEqual(profile["ctwd_mm"], 9.525)
        stack = self.torch.stackup
        self.assertEqual(stack.ctwd_mm, profile["ctwd_mm"])
        self.assertEqual(stack.contact_tip_end_z_mm, profile["ctwd_mm"])
        self.assertIsNone(stack.contact_tip_protrusion_mm)
        self.assertIsNone(stack.nozzle_face_z_mm)
        self.assertIsNone(stack.nozzle_body_end_z_mm)
        self.assertIsNone(stack.nozzle_diameter_mm)
        self.assertIsNone(stack.nozzle_length_mm)

    def test_controller_geometry_bytes(self):
        tcp = np.array([-110.247, 0.324, 351.972])
        capsules = (
            ((0.0, 0.0, 0.0), (0.0, 0.0, 154.0), 50.0),
            ((0.0, 0.0, 154.0), (0.0, 0.0, 261.0), 20.0),
            ((0.0, 0.0, 261.0), (-90.0, 0.0, 351.0), 20.0),
        )
        self.assertEqual(c.YASKAWA_45_TCP.tobytes(), tcp.tobytes())
        self.assertEqual(c.YASKAWA_45_CAPSULES, capsules)

        u = np.array([1.0, 0.0, -1.0]) / np.sqrt(2.0)
        rings = []
        for p0, p1, r in capsules:
            a, b = np.asarray(p0) - tcp, np.asarray(p1) - tcp
            n = max(2, int(float(np.linalg.norm(b - a)) / 6.0) + 1)
            for t in np.linspace(0.0, 1.0, n):
                p = a + t * (b - a)
                axial = float(p @ u)
                lateral = float(np.linalg.norm(p - axial * u))
                if axial > 0:
                    rings.append((axial, r, lateral))
        rings.sort()
        spheres = np.array([(axial, r + lateral) for axial, r, lateral in rings])
        self.assertEqual(self.torch.envelope().tobytes(), np.array(rings).tobytes())
        self.assertEqual(self.torch.spheres().tobytes(), spheres.tobytes())

    def assert_artifact_removed(self, toolpath, *, top_above_tcp=0.0, **kwargs):


        old = [v for v in c.check(toolpath, self.legacy, **kwargs)
               if v.obstacle.startswith("bead(")]
        env = self.legacy.envelope()
        artifact = float(np.min(env[:, 0] - env[:, 1]))
        self.assertAlmostEqual(artifact, -5.0, delta=0.01)
        self.assertTrue(any(abs(v.clearance_mm - (artifact - top_above_tcp)) < 1e-9 for v in old))
        new = c.check(toolpath, self.torch, **kwargs)
        self.assertFalse([v for v in new if v.obstacle.startswith("bead(")])
        self.assertFalse(c.blocking(new))

    def test_flat_bead_on_plate(self):
        scene = c.Scene(boxes=[("charles_platter", np.array([-100., -100., -6.]),
                                np.array([100., 100., 0.]))])
        flat = path(((0, 0, 0), False), ((40, 0, 0), True),
                    ((40, 20, 0), False), ((0, 20, 0), True))


        hits = c.check(flat, self.torch, scene, contact_ok=c.CONTACT_OK)
        ploughs = [v for v in c.blocking(hits) if v.obstacle.startswith("bead(")]
        self.assertEqual([(v.move_index, v.obstacle) for v in ploughs], [(2, "bead(line 2)")])
        self.assertAlmostEqual(ploughs[0].clearance_mm, -3.0)
        self.assertEqual(ploughs, c.blocking(hits))


        lifted = path(((0, 0, 0), False), ((40, 0, 0), True),
                      ((40, 0, 40), False), ((40, 20, 40), False),
                      ((40, 20, 0), False), ((0, 20, 0), True))
        self.assert_artifact_removed(lifted, top_above_tcp=3.0, scene=scene,
                                     contact_ok=c.CONTACT_OK)

    def test_twelve_layer_closed_square(self):
        self.assert_artifact_removed(square())

    def test_genuine_low_tooling_and_body_strikes_block(self):


        env = self.torch.envelope()
        for axial, radius, lateral in (env[0], env[-1]):
            with self.subTest(axial=axial):
                top = axial + self.torch.stackup.ctwd_mm
                tcp_z = top - axial
                strike = path(((0, -20, top), False), ((0, 20, top), True),
                              ((lateral, 0, tcp_z), False))
                hits = [v for v in c.blocking(c.check(strike, self.torch))
                        if v.obstacle.startswith("bead(")]
                self.assertTrue(hits)
                self.assertLess(min(v.clearance_mm for v in hits), 0)

    def test_scene_findings_bytes_with_and_without_stackup(self):
        scene = c.Scene(
            boxes=[("fixture", np.array([-30., -30., -10.]), np.array([30., 30., 60.])),
                   ("station", np.array([20., -40., 0.]), np.array([80., 40., 120.]))],
            capsules=[("reamer", np.array([0., 0., 0.]), np.array([0., 0., 250.]), 40.)],
            part_fixed=("fixture",),
        )
        tilted = np.eye(4)
        angle = np.deg2rad(45)
        tilted[:3, :3] = [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)],
                          [0, np.sin(angle), np.cos(angle)]]
        tilted[:3, 3] = [10, 20, 30]
        for transform in (None, np.eye(4), tilted):
            kwargs = dict(station_transform=transform)
            old_point = c.check_point((0, 0, 0), self.legacy, scene, **kwargs)
            new_point = c.check_point((0, 0, 0), self.torch, scene, **kwargs)
            self.assertTrue(old_point)
            self.assertEqual(encoded(new_point), encoded(old_point))
            cell_hits = lambda model, options=kwargs: [v for v in c.check(square(), model, scene, **options)
                                       if not v.obstacle.startswith("bead(")]
            old, new = cell_hits(self.legacy), cell_hits(self.torch)
            self.assertTrue(old)
            self.assertEqual(encoded(new), encoded(old))
            self.assertEqual(encoded(c.blocking(new)), encoded(c.blocking(old)))

    def test_complete_nozzle_and_partial_measurement_fallback(self):

        stack = c.TorchStackup(ctwd_mm=10, contact_tip_protrusion_mm=2,
                               nozzle_diameter_mm=16, nozzle_length_mm=25)
        self.assertEqual(stack.nozzle_face_z_mm, 12)
        self.assertEqual(stack.nozzle_body_end_z_mm, 37)


        torch = c.TorchModel(samples=((1., 1.),), stackup=stack)
        strike = path(((7, -20, 20), False), ((7, 20, 20), True), ((0, 0, 0), False))
        self.assertTrue(c.blocking(c.check(strike, torch)))
        partial = replace(self.torch, stackup=replace(self.torch.stackup,
                                                    nozzle_diameter_mm=1))
        self.assertEqual(encoded(c.check(square(), partial)),
                         encoded(c.check(square(), self.torch)))

    def test_contact_tip_and_nozzle_faces_are_distinct(self):
        stack = c.TorchStackup(ctwd_mm=10, contact_tip_protrusion_mm=2,
                               nozzle_diameter_mm=16, nozzle_length_mm=25)


        distance = stack.bead_clearance(
            np.array([0.]), np.array([10.5]), np.array([[11., 2., 0.]]),
            0.5, np.array([10.]), np.array([11.]),
        )
        self.assertLess(distance[0], 0)

        at_work = stack.bead_clearance(
            np.array([0.]), np.array([-0.5]), np.array([[11., 2., 0.]]),
            0.5, np.array([-1.]), np.array([0.]),
        )
        self.assertGreaterEqual(at_work[0], stack.ctwd_mm)

    def test_invalid_dimensions(self):
        for name in ("ctwd_mm", "contact_tip_protrusion_mm",
                     "nozzle_diameter_mm", "nozzle_length_mm"):
            for value in (-1, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    c.TorchStackup(**{"ctwd_mm": 10, name: value})


if __name__ == "__main__":
    unittest.main(verbosity=2)
