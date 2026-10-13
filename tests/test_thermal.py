from dataclasses import asdict, replace
import math
import types
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline.interpass import Profile, Response, _passes, illustrative_profile, simulate
from waam_pipeline.slicer import wall
from waam_pipeline.thermal import Enthalpy, GoldakHistory, GoldakSettings, Property, goldak_density


def profile():
    p = illustrative_profile(width_mm=4, height_mm=2, travel_mm_min=600, substrate_mm=2)
    g = GoldakSettings(2, 2, 2, 4, 1/3, 8000, 1400, 1500, 200000,
                       (Property(20, 40, 500), Property(4000, 40, 500)),
                       10, 8, 1, 0.02, 0, 0, 0)
    return replace(p, heat_model="goldak", goldak=g, sample_mm=1, net_power_w=200,
                   max_pause_s=2, pause_resolution_s=0.25)


def history(p=None, layers=2):
    p = p or profile()
    return GoldakHistory(p, _passes(wall(4, layers, p.layer_height_mm), p), 0)


class TestSource(unittest.TestCase):
    def test_half_space_integral_and_front_rear_partition(self):
        g = profile().goldak
        nodes, weights = np.polynomial.legendre.leggauss(48)
        power = 123.0
        for lo, hi, fraction in ((0, 4*g.front_mm, g.front_fraction),
                                 (-4*g.rear_mm, 0, 1-g.front_fraction)):
            intervals = ((lo, hi), (-4*g.lateral_mm, 4*g.lateral_mm), (-4*g.depth_mm, 0))
            axes = [(a+b)/2 + nodes*(b-a)/2 for a, b in intervals]
            ws = [weights*(b-a)/2 for a, b in intervals]
            xyz = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1)
            w = np.prod(np.stack(np.meshgrid(*ws, indexing="ij"), axis=-1), axis=-1)
            actual = np.sum(goldak_density(xyz, (0, 0, 0), (1, 0), g, power) * w)
            self.assertAlmostEqual(actual, power*fraction, places=7)

    def test_rotation_reversal_and_air_half_space(self):
        g = profile().goldak
        points = np.array([[1., .3, -1], [-2, 1, -.5], [0, 0, 1]])
        a = goldak_density(points, (0, 0, 0), (1, 0), g, 100)
        rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        b = goldak_density(points @ rotation.T + [30, 40, 50], (30, 40, 50), (0, 1), g, 100)
        np.testing.assert_allclose(a, b)
        c = goldak_density(points * [-1, -1, 1], (0, 0, 0), (-1, 0), g, 100)
        np.testing.assert_allclose(a, c)
        self.assertEqual(a[-1], 0)


class TestEnthalpy(unittest.TestCase):
    def test_roundtrip_across_sloped_properties_and_phase_interval(self):
        g = replace(profile().goldak, properties=(Property(20, 40, 500), Property(1000, 20, 900),
                                                 Property(2000, 30, 600), Property(4000, 30, 800)))
        m = Enthalpy(g, 25)
        temperatures = np.r_[np.linspace(20, 4000, 1001), 1400, 1500, 25]
        np.testing.assert_allclose(m.temperature(m.energy(temperatures)), temperatures, atol=1e-10)
        no_latent = Enthalpy(replace(g, latent_heat_j_kg=0), 25)
        self.assertAlmostEqual(float(m.energy(1600) - no_latent.energy(1600)),
                               g.latent_heat_j_kg * g.density_kg_m3 / 1e9)

        self.assertAlmostEqual(float(m.temperature(m.energy(1300) + m.energy(1600) - m.energy(1300))), 1600)
        for t in (0, 4001, float("nan")):
            with self.assertRaises(ValueError):
                m.energy(t)
        with self.assertRaises(ValueError):
            m.temperature(m.energy(4000) + 1)


class TestConduction(unittest.TestCase):
    def test_nonuniform_thickness_conductance_uses_series_resistance(self):
        p = profile()
        p = replace(p, layer_height_mm=1.5, substrate_mm=1,
                    goldak=replace(p.goldak, cell_mm=1,
                                   properties=(Property(20, 1, 500), Property(4000, 10, 500))))
        h = history(p, layers=2)
        h.active[:] = False
        k_substrate = np.max(np.where(h.edges[2] < 0))
        h.active[np.ravel_multi_index((0, 0, k_substrate), h.shape)] = True
        h.active[np.ravel_multi_index((0, 0, k_substrate + 1), h.shape)] = True
        h._connect()
        source, sink = (np.ravel_multi_index((0, 0, k_substrate), h.shape),
                       np.ravel_multi_index((0, 0, k_substrate + 1), h.shape))
        temperatures = np.full(h.size, p.ambient_c)
        temperatures[sink] = 4000
        h.u = h.material.energy(temperatures)
        t, power, losses, _ = h._rates()
        self.assertAlmostEqual(float(losses.sum()), 0)
        self.assertAlmostEqual(float(power.sum()), 0)
        edge = np.where((h.i == source) & (h.j == sink))[0]
        self.assertEqual(len(edge), 1)
        idx = int(edge[0])
        t_source = t[source]
        t_sink = t[sink]
        area = h.interface_area[idx]
        di = h.interface_thickness_i[idx]
        dj = h.interface_thickness_j[idx]
        ki = h.material.conductivity(t_source)
        kj = h.material.conductivity(t_sink)
        expected = area / (di / (2 * ki) + dj / (2 * kj))
        expected_flux = expected * (t_sink - t_source)
        old = (2 * ki * kj / (ki + kj) * h.geometry[idx]
               * (t_sink - t_source))
        self.assertNotAlmostEqual(float(expected_flux), float(old), delta=abs(expected_flux) * 0.05)
        self.assertAlmostEqual(float(power[source]), float(expected_flux))
        self.assertAlmostEqual(float(power[sink]), -float(expected_flux))

    def test_source_step_limit_uses_actual_cell_spacing(self):
        p = profile()
        p = replace(p, goldak=replace(p.goldak, plate_length_mm=10.3, cell_mm=1))
        h = history(p, layers=1)
        start_steps = h.steps
        centres = []

        def spy_source(self, centre, direction):
            centres.append(np.array(centre))
            return np.full(self.size, 1e-12), 1.0

        original_source = h._source
        h._source = types.MethodType(spy_source, h)
        try:
            a = np.array([h.edges[0][1], h.points[0, 1], h.points[0, 2]])
            b = a + np.array([1.0, 0.0, 0.0])
            h.advance(0.1, (a, b))
        finally:
            h._source = original_source
        self.assertEqual(h.steps - start_steps, len(centres))
        moves = [np.linalg.norm(centres[i] - centres[i - 1]) for i in range(1, len(centres))]
        self.assertGreater(len(moves), 0)
        cell = np.diff(h.edges[0]).min()
        self.assertLessEqual(max(moves), cell / 2 * 1.000001)

    def test_unequal_conductivity_exchanges_equal_and_opposite_energy(self):
        p = profile()
        p = replace(p, goldak=replace(p.goldak, properties=(Property(20, 50, 500), Property(4000, 10, 800))))
        h = history(p)
        t = np.where(h.points[:, 0] < 2, 100., 900.)
        h.u[h.active] = h.material.energy(t[h.active])
        _, power, losses, _ = h._rates()
        self.assertAlmostEqual(float(power.sum()), 0, places=9)
        self.assertEqual(float(losses.sum()), 0)
        energy = float(np.sum(h.u*h.volume))
        h.advance(0.05)
        self.assertAlmostEqual(float(np.sum(h.u*h.volume)), energy, places=8)
        self.assertTrue(np.all(h.u[~h.active] == 0))

    def test_insulated_cosine_mode_matches_analytical_diffusion_and_refines(self):
        errors = []
        for cell in (1, 0.5):
            p = profile()
            p = replace(p, sample_mm=cell, goldak=replace(p.goldak, cell_mm=cell, max_step_s=0.0005))
            h = history(p, layers=1)
            x = h.points[:, 0] - h.edges[0][0]
            initial = 100 + 10*np.cos(math.pi*x/p.goldak.plate_length_mm)
            h.u[h.active] = h.material.energy(initial[h.active])
            h.advance(0.1)
            alpha = 40 / (8000*500) * 1e6
            expected = 100 + 10*np.cos(math.pi*x/10)*math.exp(-alpha*(math.pi/10)**2*.1)
            error = float(np.max(abs(h.material.temperature(h.u[h.active]) - expected[h.active])))
            errors.append(error)
        self.assertLess(errors[0], .01)
        self.assertLess(errors[1], errors[0]/3)

    def test_source_loss_and_storage_balance_with_activation_and_clone(self):
        p = profile()
        p = replace(p, goldak=replace(p.goldak, convection_w_m2_k=20, emissivity=.8, bottom_contact_w_m2_k=100))
        h = history(p)
        n = int(h.active.sum())
        h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 0, .1, 2)
        self.assertGreater(int(h.active.sum()), n)
        self.assertFalse(np.any(h.active[h.points[:, 2] > 2]))
        self.assertFalse(np.any(h.active[(h.points[:, 0] > 1) & (h.points[:, 2] > 0)]))
        self.assertAlmostEqual(h.input_j, 20, places=9)
        original = h.u.copy()
        clone = h.clone()
        clone.advance(.3)
        np.testing.assert_array_equal(h.u, original)
        self.assertIs(h.work, clone.work)
        self.assertGreater(clone.loss_j, h.loss_j)
        self.assertLess(float(np.sum(clone.u*clone.volume)), float(np.sum(h.u*h.volume)))
        self.assertAlmostEqual(clone.diagnostics()["balance_error_j"], 0, places=9)
        with self.assertRaisesRegex(ValueError, "backward"):
            clone.advance(0)

    def test_uniform_bottom_contact_is_counted_once(self):
        p = profile()
        p = replace(p, goldak=replace(p.goldak, convection_w_m2_k=10, bottom_contact_w_m2_k=100))
        h = history(p)
        h.u[h.active] = h.material.energy(125)
        _, _, losses, _ = h._rates()

        expected = (152*10 + 80*100)*1e-6*100
        self.assertAlmostEqual(float(losses.sum()), expected)

    def test_cold_idle_skip_and_shared_work_budget(self):
        h = history()
        h.advance(100)
        self.assertEqual(h.work.steps, 0)
        clone = h.clone()
        clone.work.cell_steps = 150_000_000
        with self.assertRaisesRegex(ValueError, "trial budget"):
            clone.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 100, .1, 2)
        self.assertFalse(h.has_heat())
        with self.assertRaisesRegex(ValueError, "24-hour"):
            h.advance(86401)


class TestProfileAndSchedule(unittest.TestCase):
    def test_legacy_profiles_and_explicit_goldak_settings(self):
        p = profile()
        p.validate()
        self.assertEqual(Profile.from_dict(asdict(p)), p)
        legacy = asdict(illustrative_profile())
        legacy.pop("heat_model"); legacy.pop("goldak")
        self.assertEqual(Profile.from_dict(legacy).heat_model, "gaussian")
        for invalid in (replace(p, goldak=None), replace(p, heat_model="gaussian"),
                        replace(p, sample_mm=2), replace(p, substrate_mm=0),
                        replace(p, goldak=replace(p.goldak, front_fraction=1)),
                        replace(p, goldak=replace(p.goldak, emissivity=float("nan"))),
                        replace(p, goldak=replace(p.goldak, properties=(Property(20, -1, 500), Property(4000, 1, 500))))):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                invalid.validate()

    def test_finite_domain_and_resource_refusals(self):
        p = profile()
        with self.assertRaisesRegex(ValueError, "footprint"):
            GoldakHistory(p, _passes(wall(100, 2, 2), p), 0)
        p = replace(p, goldak=replace(p.goldak, plate_length_mm=1e6))
        with self.assertRaisesRegex(ValueError, "25000"):
            history(p)

    def test_complete_pass_energy_and_deterministic_replay(self):
        p = profile()
        tp = wall(4, 2, 2)
        p = replace(p, max_width_error_mm=10, max_height_error_mm=10, min_support_reserve=1,
                    response=tuple(replace(r, width_mm=4, height_mm=2, support_reserve=3) for r in p.response))
        report = simulate(tp, p, policy="continuous")
        self.assertEqual(report["completed_passes"], 2)
        self.assertAlmostEqual(report["heat"]["input_j"], 200 * .8, places=8)
        self.assertAlmostEqual(report["heat"]["balance_error_j"], 0, places=8)
        self.assertGreater(report["passes"][1]["peak_interpass_c"], 25)
        self.assertEqual(report, simulate(tp, p, policy="continuous"))


class TestGoldakVisualization(unittest.TestCase):
    def test_viz_records_actual_endpoint_field_and_birth_metadata(self):
        p = profile()
        h = GoldakHistory(p, _passes(wall(4, 2, 2), p), 0, enable_viz=True)
        h.advance(.1)
        self.assertEqual(h.viz_payload()["snapshots"], [])
        h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), .1, .1, 2)
        payload = h.viz_payload()
        self.assertEqual(payload["shape"], list(h.shape))
        for actual, expected in zip(payload["edges"], h.edges):
            np.testing.assert_array_equal(actual, expected)
        self.assertEqual(len(payload["snapshots"]), h.steps)
        frame = payload["snapshots"][-1]
        self.assertAlmostEqual(frame["time_s"], h.time_s)
        self.assertEqual(frame["temperature_c"].dtype, np.float32)
        np.testing.assert_array_equal(frame["temperature_c"],
                                      h.material.temperature(h.u).astype(np.float32))
        ids = payload["deposited_cell_ids"]
        np.testing.assert_array_equal(ids, np.flatnonzero(h.active & (h.points[:, 2] > 0)))
        np.testing.assert_array_equal(payload["birth_time_s"][ids], np.float32(.1))
        self.assertTrue(np.isnan(payload["birth_time_s"][~h.active]).all())
        before = payload["birth_time_s"].copy()
        h.activate(np.array([0., 0, 2]), np.array([1., 0, 2]), .2)
        np.testing.assert_array_equal(h.viz_payload()["birth_time_s"], before)

    def test_viz_disabled_has_no_recording_buffers_or_calls(self):
        h = history()
        def unexpected(*args):
            self.fail("disabled visualization invoked recorder")
        h._record_viz_snapshot = unexpected
        self.assertFalse(hasattr(h, "_viz_snapshots"))
        self.assertFalse(hasattr(h.clone(), "_viz_birth_time"))
        h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 0, .1, 2)
        self.assertIsNone(h.viz_payload())

    def test_viz_downsamples_with_hard_binary_budget(self):
        p = profile()
        passes = _passes(wall(4, 1, 2), p)
        reference = GoldakHistory(p, passes, 0)
        metadata = 8 * reference.size + sum(e.nbytes for e in reference.edges) + 1024
        for capacity in (1, 2, 3, 8):
            with self.subTest(capacity=capacity):
                budget = metadata + capacity * (4 * reference.size + 8)
                h = GoldakHistory(p, passes, 0, enable_viz=True, viz_max_payload_bytes=budget)
                h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 0, .1, 2)
                h.advance(.5)
                payload = h.viz_payload()
                frames = payload["snapshots"]
                self.assertGreater(h.steps, capacity)
                self.assertLessEqual(len(frames), capacity)
                self.assertEqual([f["time_s"] for f in frames], sorted({f["time_s"] for f in frames}))
                self.assertAlmostEqual(frames[-1]["time_s"], .5)
                used = sum(e.nbytes for e in payload["edges"])
                used += payload["birth_time_s"].nbytes + payload["deposited_cell_ids"].nbytes
                used += sum(f["temperature_c"].nbytes + 8 for f in frames) + 1024
                self.assertLessEqual(used, budget)
        h = GoldakHistory(p, passes, 0, enable_viz=True, viz_max_payload_bytes=10**9)
        self.assertEqual(h._viz_max_payload_bytes, GoldakHistory.VIZ_MAX_PAYLOAD_BYTES)
        self.assertLess(h._viz_max_payload_bytes, 4_000_000)

    def test_viz_unavailable_when_grid_and_frame_exceed_budget(self):
        p = profile()
        for budget in (0, -1, 1000):
            with self.subTest(budget=budget):
                h = GoldakHistory(p, _passes(wall(4, 1, 2), p), 0,
                                  enable_viz=True, viz_max_payload_bytes=budget)
                h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 0, .1, 2)
                self.assertFalse(h.viz_payload()["available"])
                self.assertIn("budget", h.viz_payload()["reason"])
                self.assertEqual(h._viz_snapshots, [])
                self.assertIsNone(h._viz_birth_time)
                self.assertGreater(h.steps, 0)

    def test_viz_clone_records_stay_local_to_trial(self):
        p = profile()
        h = GoldakHistory(p, _passes(wall(4, 2, 2), p), 0, enable_viz=True)
        h.deposit(np.array([0., 0, 2]), np.array([1., 0, 2]), 0, .1, 2)
        baseline = h.viz_payload()
        candidate = h.clone()
        candidate._viz_snapshots[0][1][:] = -12345
        candidate.deposit(np.array([0., 0, 4]), np.array([1., 0, 4]), .1, .1, 2)
        unused_cooling = h.clone()
        unused_cooling.advance(.5)
        for _, frame in unused_cooling._viz_snapshots:
            frame[:] = -23456
        for actual, expected in zip(h.viz_payload()["snapshots"], baseline["snapshots"]):
            np.testing.assert_array_equal(actual["temperature_c"], expected["temperature_c"])
        np.testing.assert_array_equal(h.viz_payload()["birth_time_s"], baseline["birth_time_s"])
        self.assertGreater(len(candidate._viz_snapshots), len(h._viz_snapshots))

    def test_adaptive_cooling_preserves_only_accepted_material_and_energy(self):
        p = replace(profile(), net_power_w=400, max_pause_s=5,
                    response=(Response(25, 4, 2, 3), Response(800, 4, 2, 0)))
        tp = wall(4, 3, 2)
        continuous = simulate(tp, p, policy="continuous")
        adaptive = simulate(tp, p)
        self.assertEqual(continuous["completed_passes"], 1)
        self.assertEqual(continuous["passes"][-1]["reasons"], ["hot_support"])
        self.assertEqual(adaptive["completed_passes"], 3)
        self.assertGreater(adaptive["cooling_s"], 0)
        self.assertAlmostEqual(adaptive["heat"]["input_j"], 400 * 1.2, places=8)
        self.assertEqual(adaptive["heat"]["active_cells"], 256)
        self.assertGreater(adaptive["heat"]["trial_steps"], adaptive["heat"]["steps"])
        for row in adaptive["passes"]:
            self.assertEqual(row["start_move"], row["end_move"])
            self.assertFalse(row["reasons"])
        self.assertEqual(adaptive, simulate(tp, p))


if __name__ == "__main__":
    unittest.main()
