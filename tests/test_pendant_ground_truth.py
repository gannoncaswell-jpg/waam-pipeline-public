import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.backends.jbi import PARK_PULSES
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import YASKAWA_45_CAPSULES
from waam_pipeline.robots import ar1440
from waam_pipeline.robots.ar1440 import PULSES_PER_DEG

JOBS = ROOT / "docs" / "cell-data" / "jobs"


def load(name: str):
    path = JOBS / name
    if not path.exists():
        raise unittest.SkipTest(f"missing pendant job {path}")
    return read_jbi(path)


def joints(record: str) -> list[float]:
    return [float(p) / r for p, r in zip(record.split(","), PULSES_PER_DEG)]


def robot_records(job) -> dict[str, str]:


    return {
        k: v for k, v in job.positions.items()
        if not k.startswith("E") and len(v.split(",")) == len(PULSES_PER_DEG)
    }


def tcp_of(q) -> np.ndarray:

    f = ar1440.fk(q)["flange"]
    return f[:3, 3] + f[:3, :3] @ cell_lc.TOOL_TCP


_YASK_TO_FLANGE = np.array([[0, 0, 1], [0, -1, 0], [1, 0, 0]], float)
_WIRE_AXIS = _YASK_TO_FLANGE @ np.array([-np.sqrt(0.5), 0.0, np.sqrt(0.5)])
_WIRE_AXIS = _WIRE_AXIS / np.linalg.norm(_WIRE_AXIS)


def wire_offset_deg(q_from, q_to) -> float:

    d = tcp_of(q_to) - tcp_of(q_from)
    n = float(np.linalg.norm(d))
    if n < 1e-6:
        return 0.0
    wire = ar1440.fk(q_from)["flange"][:3, :3] @ _WIRE_AXIS
    return float(np.degrees(np.arccos(abs(float(np.clip((d / n) @ wire, -1, 1))))))


class TestPendantJobsAreDecodable(unittest.TestCase):
    def test_every_saved_posture_is_inside_the_soft_limits(self):


        checked = 0
        for path in sorted(JOBS.glob("*.JBI")):
            job = read_jbi(path)
            for key, rec in robot_records(job).items():
                q = joints(rec)
                for k, j in enumerate(ar1440.AR1440_JOINTS):
                    self.assertTrue(
                        j.lower_deg <= q[k] <= j.upper_deg,
                        f"{path.name}:{key} axis {'SLURBT'[k]} = {q[k]:.2f} deg "
                        f"outside [{j.lower_deg}, {j.upper_deg}]",
                    )
                checked += 1
        self.assertGreater(checked, 40, "expected the full pendant dump to be present")

    def test_which_rates_the_soft_limits_actually_validate(self):


        records = [
            joints(rec)
            for path in sorted(JOBS.glob("*.JBI"))
            for rec in robot_records(read_jbi(path)).values()
        ]
        constrained = set()
        for k, name in enumerate("SLURBT"):
            j = ar1440.AR1440_JOINTS[k]
            for factor in (0.5, 2.0):
                if any(not (j.lower_deg <= q[k] * factor <= j.upper_deg) for q in records):
                    constrained.add(name)
                    break
        self.assertEqual(
            constrained, {"S", "L", "U", "B"},
            f"soft-limit falsifiability changed: now {sorted(constrained)}. If it "
            "grew, tighten this claim; if it shrank, a rate lost its only "
            "external validation",
        )


ROBOT_CALIBRATION_C1 = (-23605, 70044, -71193, 141046, -81183, -31192)


ROBOT_CALIBRATION_C2 = (-24221, 56606, -79133, 141023, -77396, -29917)
ROBOT_CALIBRATION_C3 = (-22386, 45256, -75784, 138030, -68565, -26862)
ROBOT_CALIBRATION_S1_PULSES = {"C1": -75380, "C2": -3158, "C3": 82510}


class TestRobotStationCalibration(unittest.TestCase):


    def test_c1_decodes_inside_the_soft_limits(self):
        q = joints(",".join(str(p) for p in ROBOT_CALIBRATION_C1))
        for k, j in enumerate(ar1440.AR1440_JOINTS):
            self.assertTrue(
                j.lower_deg <= q[k] <= j.upper_deg,
                f"calibration C1 axis {'SLURBT'[k]} = {q[k]:.2f} deg outside "
                f"[{j.lower_deg}, {j.upper_deg}]",
            )

    def test_c1_is_the_first_external_check_on_the_R_pulse_rate(self):


        q = joints(",".join(str(p) for p in ROBOT_CALIBRATION_C1))
        constrained = set()
        for k, name in enumerate("SLURBT"):
            j = ar1440.AR1440_JOINTS[k]
            if any(not (j.lower_deg <= q[k] * f <= j.upper_deg) for f in (0.5, 2.0)):
                constrained.add(name)
        self.assertIn(
            "R", constrained,
            "calibration C1 no longer falsifies a wrong R rate -- it was the "
            "only posture that did",
        )

    def test_c1_c2_c3_pin_the_station_axis_and_sense(self):


        from waam_pipeline import station
        pts = [cell_lc.tcp_from_pulses(c) for c in
               (ROBOT_CALIBRATION_C1, ROBOT_CALIBRATION_C2, ROBOT_CALIBRATION_C3)]
        scale = 4096.0 * station.GEAR_RATIO / 360.0
        angles = [ROBOT_CALIBRATION_S1_PULSES[k] / scale for k in ("C1", "C2", "C3")]
        fit = station.fit_station_axis(pts, angles_deg=angles)


        self.assertLess(fit.residual_mm, 1.0, f"circle residual {fit.residual_mm:.3f} mm")

        self.assertGreater(abs(float(fit.direction[1])), 0.999)

        computed = fit.sense if fit.direction[1] > 0 else -fit.sense
        self.assertEqual(computed, station.STATION_SENSE)
        self.assertEqual(computed, +1)
        self.assertLess(fit.sense_residual_deg, 2.0)

        self.assertAlmostEqual(float(fit.origin[0]), cell_lc.FACEPLATE_X, delta=1.0)
        self.assertAlmostEqual(float(fit.origin[2]), cell_lc.AXIS_Z, delta=1.0)


class TestCleanR1(unittest.TestCase):


    def test_park_matches_the_hardcoded_constant(self):
        job = load("CLEAN_R1.JBI")
        park = [int(v) for v in job.positions["P00100"].split(",")]
        self.assertEqual(tuple(park), PARK_PULSES)

    def test_ream_plunge_runs_along_the_wire_axis(self):


        job = load("CLEAN_R1.JBI")
        keys = [k for k in job.positions if k.startswith("C")]
        angles = [
            wire_offset_deg(joints(job.positions[a]), joints(job.positions[b]))
            for a, b in zip(keys, keys[1:])
        ]
        coaxial = [a for a in angles if a < 10.0]
        lateral = [a for a in angles if a > 80.0]

        self.assertGreaterEqual(
            len(coaxial), 4,
            f"expected several coaxial plunge/retract moves, got angles {angles}",
        )
        self.assertGreaterEqual(
            len(lateral), 2,
            f"expected lateral repositioning moves to stay distinct, got {angles}",
        )
        self.assertLess(
            max(coaxial), 3.0,
            f"ream plunge is {max(coaxial):.2f} deg off the wire axis (docs record "
            "2.25); the pulse rates, TOOL_TCP, the joint convention or the FK no "
            "longer reproduce the real robot's own taught job",
        )

    def test_plunge_invariant_is_falsifiable(self):


        job = load("CLEAN_R1.JBI")
        keys = [k for k in job.positions if k.startswith("C")]

        def worst_coaxial(rates) -> float:
            def jj(rec):
                return [float(p) / r for p, r in zip(rec.split(","), rates)]
            angles = [
                wire_offset_deg(jj(job.positions[a]), jj(job.positions[b]))
                for a, b in zip(keys, keys[1:])
            ]
            coax = [a for a in angles if a < 10.0]
            return max(coax) if coax else 99.0

        constrained = []
        for k, name in enumerate("SLURBT"):
            bumped = list(PULSES_PER_DEG)
            bumped[k] *= 1.10
            if worst_coaxial(bumped) > 3.0:
                constrained.append(name)


        self.assertEqual(
            set(constrained), {"U", "B"},
            f"the plunge invariant constrains {sorted(constrained)}; it used to "
            "constrain exactly U and B. If this grew, tighten the claim above; "
            "if it shrank, the test has gone tautological",
        )


HOME_BASE_XYZ_MM = (243.659, -0.324, 213.312)
HOME_BASE_RXYZ_DEG = (180.0, 42.8592, 0.0)
BASE_ORIGIN_Z_MM = 450.0
_rot = ar1440._rot


class TestHomeBaseReading(unittest.TestCase):


    def test_home_tcp_matches_the_base_screen(self):
        tcp = cell_lc.tcp_from_pulses(PARK_PULSES) - np.array([0.0, 0.0, BASE_ORIGIN_Z_MM])
        np.testing.assert_allclose(tcp, HOME_BASE_XYZ_MM, atol=0.005, rtol=0)

    def test_home_tool_orientation_matches_the_base_screen(self):
        f = ar1440.fk(joints(",".join(str(p) for p in PARK_PULSES)))["flange"]

        model = f[:3, :3] @ _YASK_TO_FLANGE @ _rot((0, 1, 0), -45.0)
        rx, ry, rz = HOME_BASE_RXYZ_DEG
        pendant = _rot((0, 0, 1), rz) @ _rot((0, 1, 0), ry) @ _rot((1, 0, 0), rx)
        cos = float(np.clip((np.trace(model.T @ pendant) - 1) / 2, -1, 1))
        self.assertLess(float(np.degrees(np.arccos(cos))), 0.001)

    def test_the_package_uses_this_proper_map(self):
        self.assertAlmostEqual(float(np.linalg.det(_YASK_TO_FLANGE)), 1.0)
        np.testing.assert_array_equal(cell_lc.YASKAWA_FLANGE_TO_OURS, _YASK_TO_FLANGE)

    def test_the_manuals_flange_definition_gives_the_same_map(self):


        r = ar1440.fk([0.0] * 6)["flange"][:3, :3]
        zf, xf = r[:, 0], np.array([0.0, 0.0, 1.0])
        axes = r.T @ np.column_stack([xf, np.cross(zf, xf), zf])
        np.testing.assert_allclose(axes, cell_lc.YASKAWA_FLANGE_TO_OURS, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
