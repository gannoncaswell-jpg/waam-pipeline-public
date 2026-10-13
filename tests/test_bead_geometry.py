import ast
import inspect
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcparams, bead_geometry as bg
from waam_pipeline.bead_geometry import (BeadCrossSection, BeadModelRegistry,
                                         PathDatum, PARABOLA, CIRCULAR_ARC, COSINE,
                                         ELLIPSE, PROFILES, Provenance, bead_span,
                                         nominal_wall_bead, sweep_bead, wet_out)
from waam_pipeline.clearance import bead_slab
from waam_pipeline.toolpath import Move, Toolpath


class TestProfileArea(unittest.TestCase):
    def test_parabola_area_is_two_thirds_wh(self):
        self.assertAlmostEqual(PARABOLA.area(6.0, 3.0), (2.0 / 3.0) * 6.0 * 3.0)

    def test_parabola_matches_arcparams_free_bead(self):

        self.assertAlmostEqual(PARABOLA.area(7.0, 2.5),
                               arcparams.free_bead_area_mm2(7.0, 2.5))

    def test_cosine_area_is_two_over_pi_wh(self):
        self.assertAlmostEqual(COSINE.area(6.0, 3.0), (2.0 / math.pi) * 6.0 * 3.0)

    def test_ellipse_area_is_pi_over_four_wh(self):
        self.assertAlmostEqual(ELLIPSE.area(6.0, 3.0), (math.pi / 4.0) * 6.0 * 3.0)

    def test_area_matches_declared_coefficient(self):

        for prof in (PARABOLA, COSINE, ELLIPSE):
            self.assertAlmostEqual(prof.area(7.0, 2.5), prof.area_coefficient * 7.0 * 2.5,
                                   msg=prof.name)
        self.assertIsNone(CIRCULAR_ARC.area_coefficient)

    def test_profiles_registry(self):
        self.assertEqual(set(PROFILES), {"parabola", "circular_arc", "cosine", "ellipse"})
        for name, prof in PROFILES.items():
            self.assertEqual(prof.name, name)

    def test_semicircle_area(self):

        self.assertAlmostEqual(CIRCULAR_ARC.area(8.0, 4.0), math.pi * 8.0 ** 2 / 8.0)

    def test_arc_refuses_taller_than_semicircle_in_every_query(self):

        with self.assertRaises(ValueError):
            CIRCULAR_ARC.area(6.0, 4.0)
        with self.assertRaises(ValueError):
            CIRCULAR_ARC.crown(0.0, 6.0, 4.0)
        with self.assertRaises(ValueError):
            CIRCULAR_ARC.half_width_at(1.0, 6.0, 4.0)

    def test_ellipse_is_the_semicircle_at_half_width(self):

        w, h = 8.0, 4.0
        self.assertAlmostEqual(ELLIPSE.area(w, h), CIRCULAR_ARC.area(w, h))
        for x in (0.0, 1.0, 2.5, 3.9):
            self.assertAlmostEqual(ELLIPSE.crown(x, w, h), CIRCULAR_ARC.crown(x, w, h),
                                   places=9, msg=f"s={x}")
            self.assertAlmostEqual(ELLIPSE.half_width_at(x, w, h),
                                   CIRCULAR_ARC.half_width_at(x, w, h), places=9,
                                   msg=f"t={x}")

    def test_ellipse_allows_taller_than_semicircle(self):
        self.assertAlmostEqual(ELLIPSE.area(6.0, 4.0), (math.pi / 4.0) * 24.0)
        self.assertAlmostEqual(ELLIPSE.crown(3.0, 6.0, 4.0), 0.0)

    def test_area_integrates_crown(self):


        w, h, n = 6.0, 2.0, 20000
        for prof in PROFILES.values():
            ds = w / n
            total = sum(prof.crown(-w / 2.0 + (i + 0.5) * ds, w, h) for i in range(n)) * ds
            self.assertAlmostEqual(total, prof.area(w, h), places=4, msg=prof.name)

    def test_half_width_inverts_crown(self):
        for prof in PROFILES.values():
            for s in (0.5, 1.5, 2.9):
                t = prof.crown(s, 6.0, 2.0)
                self.assertAlmostEqual(prof.half_width_at(t, 6.0, 2.0), s, places=6,
                                       msg=f"{prof.name} s={s}")

    def test_height_for_area_round_trips_all_profiles(self):
        for prof in PROFILES.values():
            area = prof.area(6.0, 2.0)
            h = prof.height_for_area(area, 6.0)
            self.assertAlmostEqual(h, 2.0, places=5, msg=prof.name)

    def test_half_width_endpoints(self):
        for prof in PROFILES.values():
            self.assertAlmostEqual(prof.half_width_at(0.0, 6.0, 2.0), 3.0, places=6,
                                   msg=f"{prof.name} bottom")
            self.assertAlmostEqual(prof.half_width_at(2.0, 6.0, 2.0), 0.0, places=6,
                                   msg=f"{prof.name} crown")

    def test_crown_endpoints(self):
        for prof in PROFILES.values():
            self.assertAlmostEqual(prof.crown(0.0, 6.0, 2.0), 2.0, places=6,
                                   msg=f"{prof.name} apex")
            self.assertAlmostEqual(prof.crown(3.0, 6.0, 2.0), 0.0, places=6,
                                   msg=f"{prof.name} toe")


class TestCrossSectionConstructors(unittest.TestCase):
    def test_from_area_and_width_preserves_area(self):
        xs = BeadCrossSection.from_area_and_width(18.0, 6.0)
        self.assertAlmostEqual(xs.area_mm2, 18.0)
        self.assertAlmostEqual(xs.height_mm, PARABOLA.height_for_area(18.0, 6.0))

    def test_from_width_and_height_computes_area(self):
        xs = BeadCrossSection.from_width_and_height(6.0, 3.0)
        self.assertAlmostEqual(xs.area_mm2, (2.0 / 3.0) * 6.0 * 3.0)

    def test_from_area_and_aspect(self):
        for prof in (PARABOLA, COSINE, ELLIPSE):
            xs = BeadCrossSection.from_area_and_aspect(18.0, 3.0, profile=prof)
            self.assertAlmostEqual(xs.width_mm / xs.height_mm, 3.0, places=6, msg=prof.name)
            self.assertAlmostEqual(xs.area_mm2, 18.0, places=6, msg=prof.name)

    def test_aspect_refused_for_arc(self):
        with self.assertRaises(ValueError):
            BeadCrossSection.from_area_and_aspect(18.0, 3.0, profile=CIRCULAR_ARC)

    def test_layer_pitch_and_radius(self):
        xs = BeadCrossSection.from_area_and_width(18.0, 6.0)
        self.assertAlmostEqual(xs.layer_pitch_mm, 3.0)
        self.assertAlmostEqual(xs.layer_pitch_mm, (2.0 / 3.0) * xs.height_mm)
        self.assertAlmostEqual(xs.outer_radius_mm, 3.0)

        self.assertAlmostEqual(xs.layer_pitch_mm,
                               arcparams.layer_height_for_area(18.0, 6.0))

    def test_measured_requires_source_and_labels(self):
        with self.assertRaises(ValueError):
            BeadCrossSection.measured(6.0, 3.0, source="")
        xs = BeadCrossSection.measured(6.0, 3.0, source="DOE row 3")
        self.assertIs(xs.provenance, Provenance.MEASURED)
        self.assertIn("measured", xs.label())
        self.assertIn("DOE row 3", xs.label())
        self.assertIn("parabola", xs.label())

    def test_measured_provenance_requires_source(self):

        with self.assertRaises(ValueError):
            BeadCrossSection.from_area_and_width(18.0, 6.0, provenance=Provenance.MEASURED)
        ok = BeadCrossSection.from_area_and_width(
            18.0, 6.0, provenance=Provenance.MEASURED, source="DOE row 9")
        self.assertIs(ok.provenance, Provenance.MEASURED)

    def test_direct_construction_validates(self):
        with self.assertRaises(ValueError):
            BeadCrossSection(6.0, 3.0, 999.0, PARABOLA, Provenance.NOMINAL_ANALYTIC, "x")
        with self.assertRaises(ValueError):
            BeadCrossSection(0.0, 3.0, 1.0, PARABOLA, Provenance.NOMINAL_ANALYTIC, "x")

    def test_nominal_labels_say_nominal(self):
        xs = BeadCrossSection.from_area_and_width(18.0, 6.0)
        self.assertIs(xs.provenance, Provenance.NOMINAL_ANALYTIC)
        self.assertIn("NOMINAL", xs.label())

    def test_nonpositive_inputs_raise(self):
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                BeadCrossSection.from_area_and_width(bad, 6.0)
            with self.assertRaises(ValueError):
                BeadCrossSection.from_area_and_width(18.0, bad)


class TestNominalWallBead(unittest.TestCase):
    def test_area_pitch_and_crown(self):
        xs = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        self.assertAlmostEqual(xs.area_mm2, 18.0)
        self.assertAlmostEqual(xs.layer_pitch_mm, 3.0)
        self.assertAlmostEqual(xs.height_mm, 4.5)

    def test_refuses_035_wire(self):
        with self.assertRaises(ValueError) as cm:
            nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0,
                              wire_dia_mm=arcparams.WIRE_035_MM)
        self.assertIn("0.035", str(cm.exception))

    def test_accepts_default_045_wire(self):
        xs = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0,
                               wire_dia_mm=arcparams.WIRE_045_MM)
        self.assertIs(xs.provenance, Provenance.NOMINAL_ANALYTIC)


class TestBeadSpanAndDatum(unittest.TestCase):
    def test_default_datum_is_bottom_like_seating_and_clearance(self):

        self.assertEqual(bead_span(10.0, 4.5), (10.0, 14.5))
        self.assertIs(inspect.signature(sweep_bead).parameters["datum"].default, PathDatum.BOTTOM)

    def test_top_datum_reads_old_bead_bottom_paths(self):

        self.assertEqual(bead_span(10.0, 4.5, PathDatum.TOP), (5.5, 10.0))

    def test_bottom_datum(self):
        self.assertEqual(bead_span(10.0, 4.5, PathDatum.BOTTOM), (10.0, 14.5))

    def test_midheight_datum(self):
        self.assertEqual(bead_span(10.0, 4.0, PathDatum.MIDHEIGHT), (8.0, 12.0))

    def test_unknown_or_nonfinite_inputs_raise(self):
        for bad in ("top", None, 3):
            with self.assertRaises((ValueError, TypeError)):
                bead_span(10.0, 3.0, bad)
        with self.assertRaises(ValueError):
            bead_span(float("nan"), 3.0)


class TestSweep(unittest.TestCase):
    def _wall(self):

        return Toolpath([
            Move(0.0, 0.0, 3.0, None, False, 0, 1),
            Move(40.0, 0.0, 3.0, 100.0, True, 0, 2),
            Move(40.0, 0.0, 6.0, None, False, 1, 3),
            Move(0.0, 0.0, 6.0, 100.0, True, 1, 4),
        ])

    def test_sweep_segments_and_bounds(self):
        xs = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        swept = sweep_bead(self._wall(), xs, datum=PathDatum.TOP)
        self.assertEqual(len(swept.segments), 2)
        self.assertAlmostEqual(swept.segments[0].bottom_z, 3.0 - 4.5)
        self.assertAlmostEqual(swept.segments[0].top_z, 3.0)
        bottom, top = swept.bounds_z()
        self.assertAlmostEqual(bottom, 3.0 - 4.5)
        self.assertAlmostEqual(top, 6.0)

    def test_clearance_capsule_radius_and_axis(self):
        xs = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        seg = sweep_bead(self._wall(), xs).segments[0]
        cap = seg.clearance_capsule()
        self.assertAlmostEqual(cap["radius_mm"], 3.0)
        (p0, p1) = cap["axis"]
        self.assertAlmostEqual(p0[2], 0.5 * (seg.bottom_z + seg.top_z))
        self.assertAlmostEqual(p1[2], p0[2])

    def test_default_sweep_capsule_matches_clearance_bead_slab(self):

        xs = BeadCrossSection.from_width_and_height(6.0, 3.0)
        seg = sweep_bead(self._wall(), xs).segments[0]
        a, b, top, _ = bead_slab(seg.start, seg.end, 3.0)
        (p0, p1) = seg.clearance_capsule()["axis"]
        self.assertAlmostEqual(p0[2], a[2])
        self.assertAlmostEqual(p1[2], b[2])
        self.assertAlmostEqual(seg.top_z, top)

    def test_clearance_capsule_follows_slope(self):

        tp = Toolpath([
            Move(0.0, 0.0, 3.0, None, False, 0, 1),
            Move(100.0, 0.0, 7.0, 100.0, True, 0, 2),
        ])
        xs = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        (p0, p1) = sweep_bead(tp, xs, datum=PathDatum.TOP).segments[0].clearance_capsule()["axis"]
        self.assertAlmostEqual(p0[2], 0.5 * ((3.0 - 4.5) + 3.0))
        self.assertAlmostEqual(p1[2], 0.5 * ((7.0 - 4.5) + 7.0))
        self.assertNotAlmostEqual(p0[2], p1[2])

    def test_first_move_deposit_has_nothing_to_sweep(self):
        tp = Toolpath([Move(0.0, 0.0, 3.0, 100.0, True, 0, 1)])
        with self.assertRaises(ValueError):
            sweep_bead(tp, nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0))


class TestOneWaySupersession(unittest.TestCase):
    def test_measured_supersedes_nominal(self):
        reg = BeadModelRegistry()
        nom = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        meas = BeadCrossSection.measured(6.4, 2.9, source="DOE row 1")
        reg.set("er70s6@lh3", nom)
        reg.set("er70s6@lh3", meas)
        self.assertIs(reg.get("er70s6@lh3"), meas)

    def test_nominal_cannot_replace_measured(self):
        reg = BeadModelRegistry()
        reg.set("k", BeadCrossSection.measured(6.4, 2.9, source="DOE row 1"))
        with self.assertRaises(ValueError):
            reg.set("k", nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0))

    def test_resolve_falls_back_to_nominal(self):
        reg = BeadModelRegistry()
        nom = nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)
        self.assertIs(reg.resolve("absent", nom), nom)


class TestQuarantine(unittest.TestCase):


    ALLOWED = {"src/waam_pipeline/server.py"}

    def test_only_the_arc_summary_reads_bead_geometry(self):
        offenders = []
        for top in ("src", "sim", "examples"):
            for p in (ROOT / top).rglob("*.py"):
                rel = p.relative_to(ROOT).as_posix()
                if p.name == "bead_geometry.py" or rel in self.ALLOWED:
                    continue
                if "bead_geometry" in p.read_text(encoding="utf-8", errors="replace"):
                    offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_server_imports_only_the_report_inside_the_arc_summary(self):
        source = (ROOT / "src/waam_pipeline/server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.ImportFrom) and node.module == "bead_geometry":
                        found.append((fn.name, [a.name for a in node.names]))
        self.assertEqual(found, [("_arc_bead_summary", ["free_bead_report"])])
        self.assertEqual(source.count("bead_geometry"), 1)

    def test_free_bead_report_is_nominal_and_refuses_the_035_wire(self):
        r = bg.free_bead_report(area_mm2=18.0, width_mm=6.0)
        self.assertEqual((r["provenance"], r["profile"]), ("NOMINAL", "parabola"))
        self.assertAlmostEqual(r["pitch_mm"], 3.0)
        self.assertAlmostEqual(r["crown_mm"], 4.5)
        self.assertIn("NOMINAL", r["label"])
        with self.assertRaises(ValueError):
            bg.free_bead_report(area_mm2=18.0, width_mm=6.0, wire_dia_mm=arcparams.WIRE_035_MM)


class TestThermalWetOut(unittest.TestCase):
    def _base(self):
        return nominal_wall_bead(layer_height_mm=3.0, bead_width_mm=6.0)

    def test_refuses_until_sensitivity_measured(self):

        self.assertIsNone(bg.WETOUT_WIDTH_SENSITIVITY_MM_PER_C)
        with self.assertRaises(ValueError):
            wet_out(self._base(), interpass_temp_c=300.0, reference_temp_c=100.0)

    def test_hotter_widens_at_constant_area(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            base = self._base()
            hot = wet_out(base, interpass_temp_c=300.0, reference_temp_c=100.0)
            self.assertAlmostEqual(hot.width_mm, 6.0 + 0.02 * 200.0)
            self.assertAlmostEqual(hot.area_mm2, base.area_mm2)
            self.assertLess(hot.height_mm, base.height_mm)

    def test_cooler_narrows_and_grows_taller(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            base = self._base()
            cold = wet_out(base, interpass_temp_c=50.0, reference_temp_c=100.0)
            self.assertLess(cold.width_mm, base.width_mm)
            self.assertGreater(cold.height_mm, base.height_mm)
            self.assertAlmostEqual(cold.area_mm2, base.area_mm2)

    def test_reference_temp_is_a_no_op(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            base = self._base()
            same = wet_out(base, interpass_temp_c=120.0, reference_temp_c=120.0)
            self.assertAlmostEqual(same.width_mm, base.width_mm)

    def test_refuses_nonpositive_width(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            with self.assertRaises(ValueError):
                wet_out(self._base(), interpass_temp_c=-500.0, reference_temp_c=100.0)

    def test_nonfinite_temps_raise(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            for bad in (float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    wet_out(self._base(), interpass_temp_c=bad, reference_temp_c=100.0)

    def test_wetout_is_a_nominal_prediction_even_from_measured(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            meas = BeadCrossSection.measured(6.0, 3.0, source="DOE row 3")
            hot = wet_out(meas, interpass_temp_c=250.0, reference_temp_c=100.0)
            self.assertIs(hot.provenance, Provenance.NOMINAL_ANALYTIC)
            self.assertIn("wet-out", hot.source)

            reg = BeadModelRegistry()
            reg.set("k", meas)
            with self.assertRaises(ValueError):
                reg.set("k", hot)

    def test_refuses_outside_measured_range(self):
        with mock.patch.object(bg, "WETOUT_WIDTH_SENSITIVITY_MM_PER_C", 0.02):
            with mock.patch.object(bg, "WETOUT_VALID_RANGE_C", (20.0, 300.0)):
                base = self._base()
                wet_out(base, interpass_temp_c=250.0, reference_temp_c=100.0)
                with self.assertRaises(ValueError):
                    wet_out(base, interpass_temp_c=500.0, reference_temp_c=100.0)


class TestSectionFit(unittest.TestCase):
    def _outline(self, prof, w, h, n=41):
        half = w / 2.0
        return [(-half + i * w / (n - 1), prof.crown(-half + i * w / (n - 1), w, h))
                for i in range(n)]

    def test_recovers_the_parabola(self):
        fit = bg.fit_measured_section(self._outline(PARABOLA, 6.0, 3.0), source="coupon A")
        self.assertIs(fit.best_profile, PARABOLA)
        self.assertLess(fit.residuals_mm["parabola"], 1e-6)
        self.assertAlmostEqual(fit.width_mm, 6.0, places=6)
        self.assertAlmostEqual(fit.height_mm, 3.0, places=6)
        self.assertAlmostEqual(fit.measured_area_mm2, (2.0 / 3.0) * 6.0 * 3.0, places=1)
        self.assertIs(fit.cross_section.provenance, Provenance.MEASURED)
        self.assertIs(fit.cross_section.profile, PARABOLA)

    def test_recovers_the_ellipse(self):
        fit = bg.fit_measured_section(self._outline(ELLIPSE, 6.0, 2.0), source="coupon B")
        self.assertIs(fit.best_profile, ELLIPSE)

    def test_arc_scored_inf_for_a_tall_section(self):
        fit = bg.fit_measured_section(self._outline(PARABOLA, 6.0, 4.0), source="tall")
        self.assertEqual(fit.residuals_mm["circular_arc"], math.inf)
        self.assertIsNot(fit.best_profile, CIRCULAR_ARC)

    def test_mass_balance_cross_check(self):
        fit = bg.fit_measured_section(
            self._outline(PARABOLA, 6.0, 3.0), source="coupon C",
            wire_dia_mm=arcparams.WIRE_045_MM, wfs_m_min=6.0, travel_mm_min=400.0)
        self.assertIsNotNone(fit.deposited_area_mm2)
        self.assertGreater(fit.crown_area_ratio, 0.0)
        self.assertIn("mass-balance", fit.report())

    def test_validation(self):
        with self.assertRaises(ValueError):
            bg.fit_measured_section([(0.0, 0.0), (1.0, 1.0)], source="x")
        with self.assertRaises(ValueError):
            bg.fit_measured_section(self._outline(PARABOLA, 6.0, 3.0), source="")


if __name__ == "__main__":
    unittest.main()
