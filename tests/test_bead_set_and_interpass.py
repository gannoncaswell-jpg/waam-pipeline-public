import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, demos, emit, server
from waam_pipeline.backends.jbi import emit_jbi_pulse
from waam_pipeline.emit import EmitRefused, _interpass_gate
from waam_pipeline.plan import build_plan
from waam_pipeline.slicer import wall
from waam_pipeline.substrate import from_request
from tests._support import IsolatedServerCase
from tests.test_sense_server import registration

SOLVE = cell_lc.pulse_solver(np.array(cell_lc.DEFAULT_PART_ORIGIN, float))


def dwells(steps):
    return [s for s in steps if s.kind == "dwell" and s.role == "interpass"]


class TestBeadSet(unittest.TestCase):
    def test_one_bead_is_unchanged(self):
        self.assertEqual(demos.demo_bead(101.6, 2.0, 325.0).moves,
                         wall(101.6, 1, 2.0, 325.0).moves)

    def test_four_beads_are_parallel_passes_that_lift_out_of_each_crater(self):
        tp = demos.demo_bead(101.6, 2.5, 325.0, count=4, pitch_mm=25.0)
        self.assertEqual(len(tp.layers()), 4)
        self.assertEqual([e[0] for e in tp.arc_events()], ["ARCON", "ARCOFF"] * 4)
        for k, layer in enumerate(tp.layers()):
            start, bead, lift = layer
            self.assertEqual((start.x, start.y, start.z, start.deposit), (0.0, 25.0 * k, 2.5, False))
            self.assertEqual((bead.x, bead.y, bead.z, bead.feed), (101.6, 25.0 * k, 2.5, 325.0))
            self.assertEqual((lift.x, lift.y, lift.deposit), (bead.x, bead.y, False))
            self.assertAlmostEqual(lift.z - bead.z, demos.BEAD_LIFT_MM)
        self.assertAlmostEqual(tp.deposit_length(), 4 * 101.6)

    def test_bad_counts_and_spacing_are_refused(self):
        for kwargs in (dict(count=0), dict(count=2.0), dict(count=3, pitch_mm=0.0),
                       dict(count=3, pitch_mm=float("nan"))):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                demos.demo_bead(**kwargs)

    def test_sized_wall_and_every_demo_take_the_weld_speed(self):
        tp = demos.build("wall", layer_height_mm=2.0, length_mm=80.0, layers=3)
        self.assertEqual(len(tp.layers()), 3)
        self.assertAlmostEqual(tp.deposit_length(), 240.0)
        for key in demos.DEMOS:
            fed = demos.with_feed(demos.build(key), 312.5)
            self.assertEqual({m.feed for m in fed.moves if m.deposit}, {312.5}, key)


class TestInterpassCadence(unittest.TestCase):
    tp = wall(60.0, 7, 2.0)

    def test_every_layer_is_the_default(self):
        steps = build_plan(self.tp, SOLVE, interpass_timer_s=30.0)
        self.assertEqual([s.layer for s in dwells(steps)], [1, 2, 3, 4, 5, 6])
        self.assertEqual(build_plan(self.tp, SOLVE), build_plan(self.tp, SOLVE, layers_per_interpass=1))

    def test_every_n_layers_dwells_after_each_run_of_n(self):
        for every, before in ((2, [2, 4, 6]), (3, [3, 6]), (6, [6])):
            with self.subTest(every=every):
                steps = build_plan(self.tp, SOLVE, interpass_timer_s=45.0, layers_per_interpass=every)
                self.assertEqual([s.layer for s in dwells(steps)], before)
                self.assertEqual({s.seconds for s in dwells(steps)}, {45.0})

                self.assertEqual(sum(s.role == "hover" for s in steps), 6)

    def test_a_staged_print_counts_its_own_layers(self):
        steps = build_plan(self.tp.layer_range(3, 6), SOLVE, layers_per_interpass=2)
        self.assertEqual([s.layer for s in dwells(steps)], [5])

    def test_bad_cadence_is_refused(self):
        for every in (0, -1, 1.5, "2", True):
            with self.subTest(every=every), self.assertRaises(ValueError):
                build_plan(self.tp, SOLVE, layers_per_interpass=every)

    def test_gate_refuses_a_cadence_that_never_pauses(self):
        _interpass_gate(self.tp, 30.0, False, 6)
        with self.assertRaisesRegex(EmitRefused, "never pauses"):
            _interpass_gate(self.tp, 30.0, False, 7)
        _interpass_gate(self.tp, 30.0, True, 7)
        _interpass_gate(wall(60.0, 1, 2.0), 30.0, False, 9)
        for bad in (float("inf"), float("nan"), 0.5):
            with self.subTest(seconds=bad), self.assertRaisesRegex(EmitRefused, "interpass"):
                _interpass_gate(self.tp, bad, False, 1)
        with self.assertRaisesRegex(EmitRefused, "whole number"):
            _interpass_gate(self.tp, 30.0, False, 0)

    def test_a_staged_range_is_judged_as_the_job_it_emits(self):
        from waam_pipeline import cell_lc as lc
        from waam_pipeline.clearance import TorchModel
        from waam_pipeline.emit import PlatterState, emit_job

        tp = wall(80.0, 7, 2.5, 325.0)
        origin = lc.snap_origin_to_platter(1014.0, -180.0, substrate_mm=6.35, first_deposit_z_mm=2.5)
        kwargs = dict(name="STAGED", arc_file=81, scene=lc.lc_scene(origin, training_block=False),
                      torch=TorchModel.yaskawa_accufast_45(), bead_height_mm=2.5, bead_width_mm=8.0,
                      platter=PlatterState(clear=True, attested_by="t"), date="2026/09/29 12:00",
                      layers_per_interpass=3)
        solve = lc.pulse_solver(origin)
        text, _ = emit_job(tp, solve, **kwargs)
        self.assertEqual(text.count("TIMER T=30.00"), 2)
        text, _ = emit_job(tp, solve, layers=(0, 3), **kwargs)
        self.assertEqual(text.count("TIMER T=30.00"), 1)
        with self.assertRaisesRegex(EmitRefused, "never pauses"):
            emit_job(tp, solve, layers=(0, 2), **kwargs)

    def test_timer_outside_the_controller_range_is_refused(self):
        kwargs = dict(name="T", date="2026/09/29 12:00", arc_file=81)
        text = emit_jbi_pulse(self.tp, SOLVE, interpass_timer_s=655.0, layers_per_interpass=3, **kwargs)
        self.assertEqual(text.count("TIMER T=655.00"), 2)
        text = emit_jbi_pulse(self.tp, SOLVE, interpass_timer_s=1000.0, layers_per_interpass=3, **kwargs)
        self.assertEqual(text.count("TIMER T=500.00\r\nTIMER T=500.00\r\n"), 2)
        for seconds in (3600.01, 5000.0):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "655.35"):
                emit_jbi_pulse(self.tp, SOLVE, interpass_timer_s=seconds, **kwargs)


class TestServedJobs(IsolatedServerCase):


    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.raw = json.dumps(registration())

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "_STATES", {}).start()
        mock.patch.object(server, "_SHOW_CELL", True).start()
        mock.patch.object(server, "_HOSTED_ASSETS", False).start()
        mock.patch("waam_pipeline.backends.jbi._git_sha", return_value="test").start()

    def request(self, route, params, method="GET"):
        req = urllib.request.Request(self.base + route + "?" + urlencode(params),
                                     data=b"" if method == "POST" else None, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read()

    def slice(self, **params):
        code, body = self.request("/api/slice", dict(dict(kind="wall", fixture="0", lh="2.5", bead="8",
                                                          travel="325"), **params), method="POST")
        self.assertEqual(code, 200, body[:300])
        return json.loads(body)

    def jbi(self, **params):
        query = dict(dict(clear="1", block="0", by="t", arc="81", name="PRELIM",
                          gen=server._state("local").get("gen")), **params)
        return self.request("/api/jbi", query)

    def test_four_bead_plate_emits_one_job_with_four_welds_on_the_corner_plane(self):
        result = self.slice(shape="bead", bead_count="4", bead_pitch="25",
                            substrate_registration=self.raw)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["blocking"], 0)
        self.assertEqual(result["unreachable"], 0)
        state = server._state("local")
        tp, origin = state["toolpath"], np.asarray(state["emit"]["origin"])
        plate = from_request(self.raw, 6.35)
        welds = np.asarray([tp.moves[j].target for i, m in enumerate(tp.moves) if m.deposit
                            for j in (i - 1, i)]) + origin
        np.testing.assert_allclose(welds[:, 2], plate.datum(welds[:, 0], welds[:, 1]), atol=1e-6)
        params = result["state"]["meta"]["params"]
        self.assertEqual((params["bead_count"], params["bead_pitch"]), ("4", "25.0"))

        code, body = self.jbi(interpass="20", interpass_every="2")
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        self.assertEqual(text.count("ARCON ASF#(81)"), 4)
        self.assertEqual(text.count("TIMER T=20.00"), 1)
        self.assertIn("V=5.4", text)

    def test_placement_keeps_the_bead_set_settings(self):
        self.slice(shape="bead", bead_count="3", bead_pitch="30", substrate_registration=self.raw)
        result = server.run_placement({"oy": "5", "bead_count": "2"}, "local")
        self.assertEqual(len(server._state("local")["toolpath"].layers()), 2)
        self.assertEqual(result["state"]["meta"]["params"]["bead_count"], "2")

    def test_sized_wall_welds_at_the_travel_speed_with_the_chosen_cadence(self):
        result = self.slice(shape="wall", wall_length="90", wall_layers="5", substrate_registration=self.raw)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["layers"], 5)
        tp = server._state("local")["toolpath"]
        self.assertEqual({m.feed for m in tp.moves if m.deposit}, {325.0})
        self.assertAlmostEqual(tp.deposit_length(), 450.0, places=6)
        for every, timers in (("1", 4), ("2", 2), ("4", 1)):
            with self.subTest(every=every):
                code, body = self.jbi(interpass="40", interpass_every=every)
                self.assertEqual(code, 200, body[:300])
                self.assertEqual(body.decode("ascii").count("TIMER T=40.00"), timers)
        code, body = self.jbi(interpass="40", interpass_every="5")
        self.assertEqual(code, 409, body[:300])
        self.assertIn("never pauses", json.loads(body)["error"])

    def test_interpass_query_is_bounded(self):
        self.slice(shape="wall", wall_layers="3")
        for params in (dict(interpass="0"), dict(interpass="3601"), dict(interpass="nan"),
                       dict(interpass_every="0"), dict(interpass_every="1.5"), dict(interpass_every="x")):
            with self.subTest(**params):
                code, body = self.jbi(**params)
                self.assertEqual(code, 400, body[:300])
                self.assertIn("enter", json.loads(body)["error"])
        with mock.patch.object(emit, "_now", return_value="2026/09/29 12:00"):
            _, default = self.jbi()
            _, explicit = self.jbi(interpass="30", interpass_every="1")
        self.assertEqual(default, explicit)

    def test_long_dwell_temperature_check_and_bead_dwell_reach_the_job(self):
        self.slice(shape="wall", wall_layers="3")
        code, body = self.jbi(interpass="1000", temp_check_every="1", bead_dwell="15")
        self.assertEqual(code, 200, body[:300])
        text = body.decode("ascii")
        self.assertEqual(text.count("TIMER T=500.00\r\nTIMER T=500.00\r\n"
                                    'MSG "READ TEMP THEN PRESS START"\r\nPAUSE\r\nMSG ""\r\n'), 2)
        self.assertNotIn("TIMER T=15.00", text)
        for params in (dict(temp_check_every="1.5"), dict(bead_dwell="3601")):
            with self.subTest(**params):
                self.assertEqual(self.jbi(**params)[0], 400)

    def test_a_bead_set_refuses_layer_touches_that_would_stack_on_it(self):
        self.slice(shape="bead", bead_count="2", bead_pitch="12")
        code, body = self.request("/api/layer-sense-job", dict(layer="0", gen=server._state("local").get("gen")))
        self.assertEqual(code, 409, body[:300])
        self.assertIn("side by side", json.loads(body)["error"])
        touches = json.dumps([{"layer": 0, "spacing": 20, "points": [[0, 0, 0, 0, 0, 0]]}])
        result = self.slice(shape="bead", bead_count="2", bead_pitch="12", layer_sense=touches)
        self.assertFalse(result["ok"])
        self.assertIn("side by side", result["error"])

    def test_sizes_of_another_demo_are_ignored(self):
        result = self.slice(shape="bead", wall_layers="", wall_length="oops")
        self.assertTrue(result["ok"], result.get("error"))
        result = self.slice(shape="wall", wall_layers="3", bead_count="0", bead_pitch="")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["layers"], 3)
        self.assertNotIn("bead_count", result["state"]["meta"]["params"])

    def test_crowded_bead_set_and_bead_set_simulation_are_refused(self):
        for params, words in ((dict(bead_count="4", bead_pitch="8"), "Bead spacing"),
                              (dict(bead_count="2", thermal="illustrative"), "stacked wall")):
            with self.subTest(**params):
                result = self.slice(shape="bead", **params)
                self.assertFalse(result["ok"])
                self.assertIn(words, result["error"])


if __name__ == "__main__":
    unittest.main()
