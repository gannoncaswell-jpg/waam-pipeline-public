from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from waam_pipeline import cell_hc
from waam_pipeline.slicer import wall

from sim import pybullet_driver as pbd

WALL_LEN_MM = 150.0
WALL_LAYERS = 3
WALL_LAYER_H = 2.0

RULE = "=" * 78
THIN = "-" * 78


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool | None, str]] = []

    def add(self, name: str, passed: bool | None, detail: str) -> None:
        self.results.append((name, passed, detail))
        mark = {True: "PASS", False: "FAIL", None: "PENDING"}[passed]
        print(f"  [{mark:7s}] {name}")
        for line in detail.splitlines():
            print(f"            {line}")

    def verdict(self) -> int:
        print(RULE)
        marks = [p for _n, p, _d in self.results]
        if any(p is False for p in marks):
            print("VERDICT: HC10 whole-arm check NOT MET - a check failed above.")
            return 1
        if any(p is None for p in marks):
            print("VERDICT: HC10 whole-arm check UNPROVEN - the engine never ran.")
            return 2
        print(f"VERDICT: HC10 whole-arm check DONE-WHEN MET "
              f"({len(marks)}/{len(marks)} checks passed) -- UNCALIBRATED, "
              f"see the module header before treating this as a safety result.")
        return 0


def _pending_note() -> str:
    return (
        "waiting on the engine. When PyBullet is importable this check will:\n"
        "  - load robots/hc10dtp.urdf with useFixedBase and map its 6 arm "
        "joints by name (no station joint to find -- requires_station=False)\n"
        "  - place the nominal AW-HC table/furniture scene and the four "
        "controller-derived torch capsules\n"
        "  - step the path through cell_hc.hc_solver (hc10.ik, never "
        "ar1440's analytic IK or a pulse scale -- this arm has none) and "
        "query getClosestPoints")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="HC10DTP whole-arm acceptance: does the station-less "
                    "CellBinding load, pose and query the arm end to end.")
    ap.add_argument("--gui", action="store_true",
                    help="open the PyBullet window and play the demo wall back")
    ap.add_argument("--sample-mm", type=float, default=4.0)
    ap.add_argument("--margin-mm", type=float, default=5.0)
    args = ap.parse_args(argv)

    print(RULE)
    print("HC10DTP ACCEPTANCE - whole-arm interference, station-less binding "
          "(sim/pybullet_driver.HC10_BINDING)")
    print(RULE)

    if pbd.HAVE_PYBULLET:
        import pybullet

        print(f"engine     : pybullet {getattr(pybullet, '__version__', 'unknown')} "
              f"({'GUI' if args.gui else 'DIRECT'})")
    else:
        print(f"engine     : NOT AVAILABLE - {pbd.PYBULLET_IMPORT_ERROR}")
        print(f"             {pbd.WSL2_HINT}")

    binding = pbd.HC10_BINDING
    report = pbd.inspect_urdf(binding.urdf_path)
    problems = report.problems(arm_link_names=binding.arm_link_names,
                               arm_joint_names=binding.arm_joint_names,
                               requires_station=binding.requires_station)
    print(f"urdf       : {report.path.relative_to(ROOT)}")
    print(f"             {len(report.links)} links, {len(report.joints)} joints, "
          f"{len(report.mesh_files)} mesh refs, {len(report.missing_meshes)} unresolved, "
          f"station required: {binding.requires_station}")
    print(THIN)

    out = Report()


    out.add("HC10DTP URDF is fit to load with no station branch required",
            not problems,
            "clean" if not problems else "UNFIT: " + "; ".join(problems))
    if problems:
        return out.verdict()
    print("CHECKS")
    print(THIN)

    drv_kwargs = dict(gui=args.gui, binding=binding)


    if not pbd.HAVE_PYBULLET:
        out.add("the HC10 binding constructs, maps joints/links by name, "
                "and poses the arm", None, _pending_note())
    else:
        with pbd.PyBulletDriver(**drv_kwargs) as drv:
            detail = [f"joints mapped by name: {list(drv.joint_index)}",
                      f"queried arm links: {list(drv.queried_links)}",
                      f"torch shapes (UNCALIBRATED): {list(drv._torch_bodies)}",
                      f"ignore-pairs ({len(binding.ignore_pairs)}): "
                      f"{list(binding.ignore_pairs)}"]
            out.add("the HC10 binding constructs, maps joints/links by "
                    "name, and poses the arm", True, "\n".join(detail))

            print("collision shapes as the engine built them:")
            for rep in drv.inspect_collision_shapes():
                print(f"  {rep.link:12s} {rep.verdict}")
            print("  NOTE vertex counts do not measure hull inflation or prove containment;")
            print("       see sim/pybullet_driver.py's module docstring.")
            print()
            print(f"structural contacts at the home pose (all-zero arm), "
                  f"{len(binding.ignore_pairs)} mounting pair(s) suppressed:")
            home = drv.contacts_at([0.0] * 6, margin_mm=args.margin_mm,
                                   include_beads=False)
            if not home:
                print("  none")
            for link, obstacle, mm in sorted(home, key=lambda r: r[2]):
                print(f"  {link:24s} {mm:+8.2f} mm  {obstacle}")
        print(THIN)


    tp = wall(WALL_LEN_MM, WALL_LAYERS, WALL_LAYER_H)
    detail = [f"wall {WALL_LEN_MM:.0f} mm x {WALL_LAYERS} layers, "
              f"{len(tp.moves)} moves, at binding.default_origin "
              f"(robot/URDF-base frame) {np.round(binding.default_origin, 1).tolist()}",
              "UNCALIBRATED CELL: hc_torch_controller_tool0() is the controller's "
              "own tool now, but hc_scene() is still a nominal, unsurveyed cell "
              "-- this proves the check RAN, not that the HC10 cell "
              "is collision-checked. Any 'blocking' count below is "
              "informational, not the pass/fail bar."]
    if not pbd.HAVE_PYBULLET:
        out.add("a whole-arm check runs an HC10 toolpath end to end",
                None, "\n".join(detail) + "\n" + _pending_note())
    else:
        with pbd.PyBulletDriver(**drv_kwargs) as drv:
            t0 = time.time()
            res = drv.check(tp, margin_mm=args.margin_mm, sample_mm=args.sample_mm)
            elapsed = time.time() - t0
        blocking = res.blocking()
        detail.append(f"{res.poses}/{res.waypoints} waypoints solved "
                      f"({len(res.unreachable)} unreachable), {res.beads} bead(s) "
                      f"laid, {len(blocking)} blocking finding(s) against the "
                      f"UNCALIBRATED torch/scene ({elapsed:.1f}s)")
        for v in sorted(blocking, key=lambda v: v.clearance_mm)[:5]:
            detail.append(f"  move {v.move_index} (L{v.gcode_line}): "
                          f"{v.clearance_mm:+.2f} mm to {v.obstacle}")
        for u in res.unreachable[:5]:
            detail.append(f"  UNREACHABLE move {u[0]}: {u[1]}")
        out.add("a whole-arm check runs an HC10 toolpath end to end",
                len(res.unreachable) == 0, "\n".join(detail))


    if args.gui and pbd.HAVE_PYBULLET:
        print(THIN)
        print("GUI playback of the demo wall (close the window to finish)...")
        with pbd.PyBulletDriver(**{**drv_kwargs, "gui": True}) as drv:
            solve = cell_hc.hc_solver(binding.native_origin(drv.origin))
            for move in tp.moves:
                try:
                    drv.pose(q_deg=solve(move.target))
                except ValueError:
                    continue
                time.sleep(0.25)

    return out.verdict()


if __name__ == "__main__":
    raise SystemExit(main())
