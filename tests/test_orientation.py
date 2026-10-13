import unittest
from unittest.mock import patch

import numpy as np
import trimesh

from examples.ornl_acceptance import block, ring
from waam_pipeline import cell_lc, ornl, server
from waam_pipeline.correction import rotate_part
from waam_pipeline.orientation import place_on_face, rest_faces
from waam_pipeline.slicer import slice_mesh, to_gcode, transform


class TestRestFaces(unittest.TestCase):
    def test_box_clusters_triangles_with_outward_normals_and_areas(self):
        tri = block(width=30, depth=18, height=6)
        faces = rest_faces(tri)
        self.assertEqual(len(faces), 6)
        self.assertEqual(sorted(f.area for f in faces), [108, 108, 180, 180, 540, 540])
        for face in faces:
            self.assertEqual(len(face.triangles), 2)
            self.assertAlmostEqual(np.linalg.norm(face.normal), 1)
            plane = tri[face.triangles[0], 0] @ face.normal
            self.assertLessEqual(float((tri @ face.normal).max()), plane + 1e-10)

    def test_inner_faces_are_not_rest_faces_and_degenerates_are_ignored(self):
        tri = ring()
        tri = np.concatenate((tri, np.zeros((2, 3, 3))))
        faces = rest_faces(tri)
        self.assertEqual(len(faces), 6)
        self.assertEqual(sorted(f.area for f in faces), [360, 360, 480, 480, 4416, 4416])

    def test_winding_and_triangle_order_do_not_choose_a_different_pose(self):
        tri = block()
        original, info = place_on_face(tri, 63, 25)
        order = np.random.default_rng(4).permutation(len(tri))
        shuffled = tri[order].copy()
        shuffled[::2] = shuffled[::2, ::-1]
        seated, second = place_on_face(shuffled, 63, 25)
        self.assertEqual(info, second)
        seated[::2] = seated[::2, ::-1]
        np.testing.assert_array_equal(seated, original[order])

    def test_near_normals_do_not_merge_non_coplanar_faces(self):

        tri = np.array([[[0, 0, 0], [10, 0, 0], [0, 10, 0]],
                        [[0, 0, 0], [-10, 0, 0.174550649], [0, 10, 0]]])
        faces = rest_faces(tri)
        self.assertEqual(len(faces), 2)
        self.assertTrue(all(len(face.triangles) == 1 for face in faces))

    def test_binary_stl_precision_retains_all_six_rotated_faces(self):
        for seed in range(20):
            tri = block(width=30, depth=18, height=6)
            for axis, angle in zip("xyz", np.random.default_rng(seed).uniform(-180, 180, 3)):
                tri = transform(tri, angle, axis, drop_to_z0=False)
            tri = tri.astype(np.float32).astype(float)
            with self.subTest(seed=seed):
                self.assertEqual(len(rest_faces(tri)), 6)
                seated, info = place_on_face(tri)
                contact = rest_faces(tri)[info["face"]].triangles
                self.assertEqual(len(contact), 2)
                np.testing.assert_array_equal(seated[contact, :, 2], 0)

    def test_roundoff_at_normal_hemisphere_boundary_still_forms_one_face(self):
        quad = np.array([[0, 0, 0], [1, 1 - 1e-8, 0], [1, 1, 1], [0, 0, 1.]])
        faces = rest_faces(quad[[[0, 1, 2], [0, 2, 3]]])
        self.assertEqual(len(faces), 1)
        self.assertEqual(len(faces[0].triangles), 2)

    def test_finely_faceted_round_mesh_refuses_tiny_contacts(self):
        tri = trimesh.creation.icosphere(subdivisions=3).triangles
        self.assertEqual(rest_faces(tri), [])
        with self.assertRaisesRegex(ValueError, "No supporting flat face"):
            place_on_face(tri, 70)

    def test_snap_is_a_proper_rotation_and_real_face_is_exactly_at_zero(self):
        tri = block(width=30, depth=18, height=6)
        for x, y in [(70, 0), (0, 80), (180, 0), (-180, 0), (63, 25), (360, -360)]:
            with self.subTest(x=x, y=y):
                seated, info = place_on_face(tri, x, y)
                rot = np.array(info["rotation"])
                np.testing.assert_allclose(rot.T @ rot, np.eye(3), atol=1e-14)
                self.assertAlmostEqual(np.linalg.det(rot), 1)
                np.testing.assert_allclose(rot @ info["normal"], [0, 0, -1], atol=1e-14)
                face = rest_faces(tri)[info["face"]]
                np.testing.assert_array_equal(seated[face.triangles, :, 2], 0)
                self.assertEqual(float(seated[..., 2].min()), 0)
                second, second_info = place_on_face(tri, x, y)
                self.assertEqual(info, second_info)
                self.assertEqual(seated.tobytes(), second.tobytes())

    def test_equal_angle_prefers_larger_face_and_lay_flat_handles_cad_tilt(self):
        tri = block(width=30, depth=18, height=6)
        seated, _ = place_on_face(tri, 45)
        np.testing.assert_array_equal(seated, tri)
        tilted = transform(tri, 23, "x", drop_to_z0=False)
        seated, info = place_on_face(tilted)
        np.testing.assert_allclose(seated, tri, atol=1e-13)
        self.assertAlmostEqual(info["snap_degrees"], 23)


class TestFacePlacementPipeline(unittest.TestCase):
    def setUp(self):
        self.ws = "place-on-face-test"
        self.tri = block(width=30, depth=18, height=6)
        self.raw = ornl.stl_bytes(self.tri)
        self.params = dict(kind="stl", units="mm", lh="3", seam="0", bead="3",
                           repair="0", fixture="0", sub="0")
        for context in (patch.object(server, "ROBOT_SHADOW_MAX_MOVES", 0),
                        patch.object(server, "_SHOW_CELL", True)):
            context.start()
            self.addCleanup(context.stop)
        self.addCleanup(server._STATES.pop, self.ws, None)

    def slice(self, **changes):
        return server.run_slice(dict(self.params, **changes), self.raw, self.ws)

    def state(self):
        return server._state(self.ws)

    @staticmethod
    def same_render(state):


        return {**state, "meta": {k: v for k, v in state["meta"].items() if k != "gen"}}

    def test_zero_orientation_matches_legacy_geometry_and_gcode_bytes(self):
        legacy = transform(self.tri, 0, "y")
        for yaw in (0, 37):

            expected, mesh = rotate_part(slice_mesh(legacy, 3, feed_mm_min=175.0,
                                                    seam_rotation_deg_per_layer=0), legacy, yaw)
            first = self.slice(yaw=str(yaw), ox="10", oy="-12")
            before = to_gcode(self.state()["toolpath"]).encode()
            self.assertEqual(before, to_gcode(expected).encode())
            self.assertEqual(self.state()["prepared"]["mesh"].tobytes(), legacy.tobytes())
            second = self.slice(yaw=str(yaw), ox="10", oy="-12", rotX="0", rotY="0", lay_flat="0")
            self.assertEqual(before, to_gcode(self.state()["toolpath"]).encode())
            self.assertEqual(self.same_render(first["state"]),
                             self.same_render(second["state"]))
            with patch.object(server, "slice_mesh", side_effect=AssertionError("Planar move resliced")):
                placed = server.run_placement(dict(yaw=str(yaw), ox="10", oy="-12",
                                                   rotX="0", rotY="0", lay_flat="0"), self.ws)
            self.assertEqual(before, to_gcode(self.state()["toolpath"]).encode())
            self.assertEqual(first["placement"], placed["placement"])

    def test_tilt_reslices_seated_mesh_and_clearance_uses_that_toolpath(self):
        self.slice(oz="7")
        with patch.object(server, "slice_mesh", wraps=slice_mesh) as slicer, \
                patch.object(server, "repair_travels", wraps=server.repair_travels) as clearance:
            result = server.run_placement(dict(rotX="70", oz="7"), self.ws)
        self.assertEqual(slicer.call_count, 1)
        seated = slicer.call_args.args[0]
        np.testing.assert_array_equal(np.ptp(seated, axis=(0, 1)), [30, 6, 18])
        contact = seated[np.all(seated[..., 2] == 0, axis=1)]
        self.assertEqual(len(contact), 2)
        self.assertGreater(np.linalg.norm(np.cross(contact[0, 1] - contact[0, 0],
                                                   contact[0, 2] - contact[0, 0])), 0)
        self.assertIs(clearance.call_args.args[0], self.state()["toolpath"])
        self.assertIs(clearance.call_args.args[2], self.state()["emit"]["scene"])
        first = np.array([m.target for m in self.state()["toolpath"].layers()[0]])
        np.testing.assert_array_equal(np.ptp(first, axis=0), [30, 6, 0])
        self.assertTrue(np.all(first[:, 2] == 3))
        self.assertEqual(result["layers"], 6)

        self.assertAlmostEqual(result["placement"]["origin"][2] + 3, cell_lc.PLATTER_FACE_Z)
        self.assertEqual(result["state"]["meta"]["params"]["oz"], "0.0")
        self.assertEqual(result["stl"]["size_mm"], [30, 6, 18])

    def test_pre_feature_cache_cannot_claim_a_new_seated_orientation(self):
        self.slice()
        self.state()["prepared"].pop("orientation_key")
        with patch.object(server, "slice_mesh", wraps=slice_mesh) as slicer:
            result = server.run_placement({"rotX": "70"}, self.ws)
        self.assertEqual(slicer.call_count, 1)
        self.assertEqual(result["stl"]["size_mm"], [30, 6, 18])

    def test_planar_moves_reuse_seated_source_and_saved_request_replays_exactly(self):
        self.slice(rotY="80")
        source = self.state()["prepared"]["toolpath"]
        initial = np.array(self.state()["emit"]["origin"])
        with patch.object(server, "slice_mesh", side_effect=AssertionError("Planar move resliced")):
            result = server.run_placement(dict(ox="10", oy="-12", yaw="37"), self.ws)
        self.assertIs(source, self.state()["prepared"]["toolpath"])
        np.testing.assert_allclose(np.array(result["placement"]["origin"]) - initial, [10, -12, 0])
        expected, _ = rotate_part(source, self.state()["prepared"]["mesh"], 37)
        self.assertEqual(to_gcode(expected), to_gcode(self.state()["toolpath"]))
        request = self.state()["request"]
        before = to_gcode(self.state()["toolpath"]).encode()
        replay = server.run_slice(request["params"], request["stl"], self.ws)
        self.assertEqual(before, to_gcode(self.state()["toolpath"]).encode())
        self.assertEqual(result["placement"], replay["placement"])
        self.assertEqual(result["stl"]["orientation"], replay["stl"]["orientation"])
        server.run_placement(dict(rotX="0", rotY="0", lay_flat="0", tilt="0"), self.ws)
        self.assertEqual(self.state()["prepared"]["mesh"].tobytes(), self.tri.tobytes())

    def test_legacy_tilt_also_snaps_and_ornl_receives_seated_geometry(self):
        result = self.slice(tilt="80")
        self.assertEqual(result["stl"]["size_mm"], [6, 18, 30])
        with patch.object(ornl, "slice_mesh", side_effect=lambda tri, options:
                          (slice_mesh(tri, 3), {"engine": "ornl"})) as engine:
            self.slice(slicer="ornl", rotX="70")
            np.testing.assert_array_equal(np.ptp(engine.call_args.args[0], axis=(0, 1)), [30, 6, 18])
            server.run_placement(dict(rotX="0", rotY="80"), self.ws)
            self.assertEqual(engine.call_count, 2)
            np.testing.assert_array_equal(np.ptp(engine.call_args.args[0], axis=(0, 1)), [6, 18, 30])

    def test_lay_flat_and_invalid_requests_preserve_the_accepted_job(self):
        self.raw = ornl.stl_bytes(transform(self.tri, 23, "x", drop_to_z0=False))
        self.slice()
        result = server.run_placement({"lay_flat": "1"}, self.ws)
        np.testing.assert_allclose(result["stl"]["size_mm"], [30, 18, 6], atol=0.001)
        for params in ({"rotX": "nan"}, {"rotY": "361"}, {"lay_flat": "yes"}):
            before = dict(self.state())
            with self.subTest(params=params), self.assertRaises(ValueError):
                server.run_placement(params, self.ws)
            for key in before:
                self.assertIs(self.state()[key], before[key])
        with self.assertRaisesRegex(ValueError, "requires an STL"):
            server.run_slice(dict(kind="wall", rotX="70"), None, self.ws)


if __name__ == "__main__":
    unittest.main()
