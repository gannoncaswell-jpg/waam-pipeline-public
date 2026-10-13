import json
import math
from pathlib import Path
import subprocess
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from waam_pipeline import ornl, server
from waam_pipeline.correction import rotate_part
from waam_pipeline.slicer import parse_stl
from examples.ornl_acceptance import block, ring, crosses_hole

FIXTURE = Path(__file__).parent / "fixtures" / "ornl" / "block.gcode"


class TestOrnlProfile(unittest.TestCase):
    def test_units_and_all_regions_share_the_cell_bead(self):
        options=ornl.Options(2.9,8.6,175,contours=3,stepover_percent=70,angle_rotation_deg=90)
        settings=ornl.settings(options)["settings"][0]
        self.assertEqual(settings["layer_height"],2900)
        self.assertAlmostEqual(settings["infill_line_spacing"],6020)
        self.assertAlmostEqual(settings["inset_overlap_distance"],2580)
        self.assertAlmostEqual(settings["infill_angle_rotation"],math.pi/2)
        for role in ("perimeter","inset","infill"):
            self.assertEqual(settings[role+"_width"],8600)
            self.assertAlmostEqual(settings[role+"_speed"],175000/60)

    def test_invalid_options_are_refused(self):
        for change in ({"contours":2.5},{"contours":True},{"stepover_percent":float("nan")},
                       {"feed_mm_min":0},{"pattern":"random"},{"travel_lift_mm":-1}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                ornl.Options(**(dict(layer_height_mm=2,bead_width_mm=6,feed_mm_min=180) | change))

    def test_solid_mesh_and_binary_round_trip(self):
        for tri in (block(),ring()):
            checked=ornl.validate_mesh(tri,ornl.Options(2,6,180))
            np.testing.assert_allclose(parse_stl(ornl.stl_bytes(checked)),tri)

    def test_open_inconsistent_and_nonfinite_meshes_refused(self):
        reversed_face=block().copy()
        reversed_face[0]=reversed_face[0,::-1]
        nonfinite=block().copy();nonfinite[0,0,0]=np.nan
        for tri in (block()[:-1],reversed_face,nonfinite):
            with self.subTest(),self.assertRaises(ValueError):
                ornl.validate_mesh(tri,ornl.Options(2,6,180))

    def test_hole_oracle_detects_a_crossing_with_endpoints_outside(self):
        self.assertTrue(crosses_hole((-20,0,2),(20,0,2)))
        self.assertFalse(crosses_hole((-20,20,2),(20,20,2)))


class TestOrnlOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text=FIXTURE.read_text(encoding="utf-8")
        cls.options=ornl.Options(2,6,180)

    def test_real_engine_fixture_preserves_geometry_layers_and_regions(self):
        tp=ornl.parse_output(self.text,self.options)
        self.assertEqual(len(tp.layers()),3)
        self.assertFalse(tp.moves[0].deposit)
        self.assertEqual({m.region for m in tp.moves if m.deposit},{"perimeter","inset","infill"})
        self.assertEqual({m.z for m in tp.moves if m.deposit},{2,4,6})
        self.assertAlmostEqual(tp.deposit_length(),1424.5,places=3)
        self.assertEqual(sum(event=="ARCON" for event,_ in tp.arc_events()),9)
        self.assertEqual(tp.unhandled,[])

    def test_rejects_unmodelled_and_ambiguous_commands(self):
        anchor="G0 X27.0000 Y17.0000 Z7.0000"
        self.assertIn(anchor,self.text)
        for replacement in ("G28", "G2 X27 Y17 I5 J3", "G0 X=27 Y=17 Z=7", "G0 X27 X28 Y17 Z7",
                            "G0 Xnan Y17 Z7", "G0 X27 Y17 Z7 A0", "M1002", "G91"):
            with self.subTest(replacement=replacement),self.assertRaises(ValueError):
                ornl.parse_output(self.text.replace(anchor,replacement,1),self.options)

    def test_rejects_truncation_wrong_feed_and_layer_discontinuity(self):
        changes=[self.text.split("M02")[0],self.text.replace("F180","F360",1),
                 self.text.replace("(BEGINNING LAYER: 2)","(BEGINNING LAYER: 4)",1),
                 self.text.replace("Z2.0000","Z2.5000",1),
                 self.text.replace("M3 S1","M3 S2",1),
                 self.text.replace("M3 S1","",1),
                 self.text.replace("(PERIMETER)","(SKELETON)",1)]
        for change in changes:
            with self.subTest(),self.assertRaises(ValueError):
                ornl.parse_output(change,self.options)

    def test_placement_preserves_roles_and_lengths(self):
        tp=ornl.parse_output(self.text,self.options)
        rotated,_=rotate_part(tp,block(),37)
        self.assertAlmostEqual(rotated.deposit_length(),tp.deposit_length())
        self.assertEqual([m.region for m in rotated.moves],[m.region for m in tp.moves])

    def test_output_must_belong_to_the_current_mesh_and_profile(self):
        with self.assertRaisesRegex(ValueError,"does not belong"):
            ornl.parse_output(self.text,self.options,expected_job="0"*64)

    def test_semantic_comments_are_validated_everywhere(self):
        anchor = "G0 X27.0000 Y17.0000 Z7.0000 (TRAVEL)"
        changes = [
            self.text.replace("(BEGINNING LAYER: 2)", "(BEGINNING LAYER: garbage)"),
            self.text.replace("(LAYER COUNT: 3)", "(LAYER COUNT: garbage)"),
            self.text.replace(anchor, anchor.replace("(TRAVEL)", "(PERIMETER)")),
            self.text.replace("(PERIMETER)", "(PERIMETER) (SPIRAL)", 1),
            self.text.replace("(PERIMETER)", "(PERIMETER) (INSET)", 1),
            self.text.replace(anchor, anchor + " (BEGINNING LAYER: 2)"),
            self.text.replace(anchor, "(SPIRAL)\n" + anchor),
            self.text.replace(anchor, anchor.replace("(TRAVEL)", "(TRAVEL")),
            self.text + "\n(BEGINNING LAYER: garbage)\n",
        ]
        for text in changes:
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                ornl.parse_output(text, self.options)

    def test_edited_count_and_matching_fingerprint_cannot_hide_truncation(self):
        import re
        job = re.search(r"\(WAAM_JOB ([0-9a-f]{64})\)", self.text)[1]
        truncated = self.text.split("(BEGINNING LAYER: 2)")[0] + "M02\n"
        for text in (truncated, truncated.replace("(LAYER COUNT: 3)", "(LAYER COUNT: 1)")):
            with self.subTest(), self.assertRaisesRegex(ValueError, "incomplete"):
                ornl.parse_output(text, self.options, expected_job=job, expected_height_mm=6)
        for height in (5.1, 6, 6.9):
            ornl.parse_output(self.text, self.options, expected_job=job, expected_height_mm=height)


class TestOrnlEngineFailure(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = mock.patch.object(ornl, "_engine_data_root", return_value=Path(self.directory.name))
        root.start()
        self.addCleanup(root.stop)

    def test_missing_engine_is_actionable(self):
        with mock.patch.object(ornl,"executable",return_value=None):
            with self.assertRaisesRegex(ValueError,"not configured"):
                ornl.slice_mesh(block(),ornl.Options(2,6,180))

    def test_success_exit_without_output_is_not_a_successful_slice(self):
        with mock.patch.object(ornl,"executable",return_value=Path("engine")),\
             mock.patch.object(ornl,"_run",return_value="version=2.1.0 "):
            with self.assertRaisesRegex(ValueError,"did not create"):
                ornl.slice_mesh(block(),ornl.Options(2,6,180))

    def test_slice_checks_mesh_extent_before_persisting_artifacts(self):
        import re
        def run(command, directory, env, timeout):
            if "--version" in command:
                return "version=2.1.0 "
            profile = json.loads((directory / "profile.s2c").read_text())
            job = re.search(r"WAAM_JOB ([0-9a-f]{64})", profile["settings"][0]["start_code"])[1]
            text = FIXTURE.read_text().split("(BEGINNING LAYER: 2)")[0] + "M02\n"
            text = re.sub(r"WAAM_JOB [0-9a-f]{64}", "WAAM_JOB " + job, text)
            text = text.replace("(LAYER COUNT: 3)", "(LAYER COUNT: 1)")
            (directory / "result.gcode").write_text(text)
            return ""
        artifacts = Path(self.directory.name) / "artifacts"
        with mock.patch.object(ornl, "executable", return_value=Path("engine")), \
             mock.patch.object(ornl, "_run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "incomplete build extent"):
                ornl.slice_mesh(block(), ornl.Options(2, 6, 180), artifacts=artifacts)
        self.assertFalse(artifacts.exists())

    def test_timeout_is_reported_and_uses_no_shell(self):
        with mock.patch.object(subprocess,"Popen", wraps=subprocess.Popen) as run:
            with self.assertRaisesRegex(ValueError,"exceeded"):
                ornl._run([sys.executable, "-c", "import time; time.sleep(30)"],
                          self.directory.name, os.environ.copy(), .1)
            self.assertFalse(run.call_args.kwargs.get("shell",False))

    def test_generation_limit_kills_noisy_or_disk_writing_children(self):
        writers = ["os.write(1, b'x'*2048)", "os.write(2, b'x'*2048)",
                   "f.write(b'x'*2048); f.flush()"]
        for writer in writers:
            with self.subTest(writer=writer), tempfile.TemporaryDirectory() as directory:
                script = ("import os, time\nf=open('result.gcode', 'wb')\n"
                          "for i in range(100):\n " + writer + "\n time.sleep(.02)\n"
                          "open('finished', 'w').close()\n")
                with mock.patch.object(ornl, "MAX_OUTPUT_BYTES", 4096):
                    with self.assertRaisesRegex(ValueError, "size limit during generation"):
                        ornl._run([sys.executable, "-c", script], directory, os.environ.copy(), 10)
                self.assertFalse((Path(directory) / "finished").exists())

    @unittest.skipUnless(os.name == "nt", "Windows process termination semantics")
    def test_generation_limit_waits_for_windows_process_termination(self):
        import _winapi

        original_popen = subprocess.Popen
        children = []
        def start(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        script = ("import os, time\nf=open('result.gcode', 'wb')\n"
                  "for i in range(100):\n os.write(2, b'x'*2048)\n time.sleep(.02)\n")
        with mock.patch.object(subprocess, "Popen", side_effect=start), \
             mock.patch.object(ornl, "MAX_OUTPUT_BYTES", 4096):


            for iteration in range(20):
                with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(ValueError, "size limit during generation"):
                        ornl._run([sys.executable, "-c", script], directory, os.environ.copy(), 10)
                    child = children[-1]
                    try:
                        self.assertEqual(_winapi.WaitForSingleObject(child._handle, 0), _winapi.WAIT_OBJECT_0,
                                         "_run returned before the child released its resources")
                        self.assertTrue(child.stdout.closed)
                    finally:


                        self.assertEqual(_winapi.WaitForSingleObject(child._handle, 10000), _winapi.WAIT_OBJECT_0)

    def test_final_output_burst_and_combined_budget_are_checked(self):
        with mock.patch.object(ornl, "MAX_OUTPUT_BYTES", 4096):
            with self.assertRaisesRegex(ValueError, "size limit during generation"):
                ornl._run([sys.executable, "-c",
                           "import os; open('result.gcode','wb').write(b'x'*3000); os.write(1,b'x'*3000)"],
                          self.directory.name, os.environ.copy(), 10)

    def test_process_lock_refuses_overlapping_invocations(self):
        with ornl._engine_lock():
            with mock.patch.object(tempfile, "gettempdir", return_value="different-temp"), \
                 self.assertRaisesRegex(ValueError,"another part"):
                with ornl._engine_lock():
                    self.fail("Second engine process acquired the shared scratch lock")

    def test_process_lock_is_shared_with_a_second_server(self):
        script = ("from pathlib import Path; from waam_pipeline import ornl; "
                  f"ornl._engine_data_root=lambda: Path({self.directory.name!r}); "
                  "\nwith ornl._engine_lock(): pass")
        env = dict(os.environ, PYTHONPATH=str(Path(ornl.__file__).parents[1]),
                   TEMP=str(Path(self.directory.name) / "other-temp"))
        with ornl._engine_lock():
            result = subprocess.run([sys.executable, "-c", script], env=env,
                                    capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"another part", result.stderr)


class TestOrnlServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text=FIXTURE.read_text(encoding="utf-8")
        cls.options=ornl.Options(2,6,180)
        cls.params={"kind":"stl","units":"mm","slicer":"ornl","lh":"2","bead":"6", "travel":"180","cell":"0","repair":"0"}
        cls.raw=ornl.stl_bytes(block())
        cls.meta={"engine":"ornl","version":"2.1.0","options":ornl.asdict(cls.options),
                  "spacing_mm":4.5,"region_length_mm":{"perimeter":528,"inset":420,"infill":476.5},
                  "build_height_mm":6,"input_height_mm":6}

    def engine_result(self,*args,**kwargs):
        return ornl.parse_output(self.text,self.options),self.meta

    def test_dispatch_placement_and_request_replay(self):
        with mock.patch.object(ornl,"slice_mesh",side_effect=self.engine_result) as engine:
            result=server.run_slice(self.params,self.raw,workspace="ornl-test")
            self.assertEqual(result["slicing"]["engine"],"ornl")
            self.assertEqual(result["state"]["toolpath"]["moves"][2]["region"],"perimeter")
            request=server._state("ornl-test")["request"]
            self.assertEqual(request["params"]["slicer"],"ornl")
            self.assertEqual(request["params"]["stepover"],"75.0")
            placed=server.run_placement({"yaw":"45"},workspace="ornl-test")
            self.assertEqual(placed["slicing"],self.meta)
            self.assertEqual(engine.call_count,1)
            server.run_slice(request["params"],request["stl"],workspace="ornl-test")
            self.assertEqual(engine.call_count,2)

    def test_failure_preserves_previous_job(self):
        with mock.patch.object(ornl,"slice_mesh",side_effect=self.engine_result):
            server.run_slice(self.params,self.raw,workspace="ornl-failure")
        original=server._state("ornl-failure")["toolpath"]
        with mock.patch.object(ornl,"slice_mesh",side_effect=ValueError("ORNL timed out")):
            with self.assertRaises(ValueError):
                server.run_slice(self.params,self.raw,workspace="ornl-failure")
        self.assertIs(server._state("ornl-failure")["toolpath"],original)

    def test_hidden_builtin_seam_does_not_block_ornl(self):
        with mock.patch.object(ornl, "slice_mesh", side_effect=self.engine_result):
            result = server.run_slice(dict(self.params, seam=""), self.raw, workspace="ornl-no-seam")
        self.assertTrue(result["ok"])
        self.assertEqual(result["slicing"]["engine"], "ornl")

    def test_unknown_engine_noninteger_contours_and_demo_refused(self):
        for change in ({"slicer":"other"},{"contours":"2.5"},{"kind":"wall"}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                server.run_slice(dict(self.params,**change),self.raw)
