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

from waam_pipeline import cell_lc, clearance
from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking
from waam_pipeline.slicer import wall

from sim import pybullet_driver as pbd

WALL_LEN_MM = 150.0
WALL_LAYERS = 6
WALL_LAYER_H = 2.0
SUBSTRATE_MM = 6.35
ORIGIN_X = 1014.0

ORIGIN_Y_SCAN = np.arange(-100.0, -620.0, -20.0)

RULE = "=" * 78
THIN = "-" * 78


FOUL_WITH_BLOCK = True
CLEAN_WITH_BLOCK = False


def _wall_at(origin_y: float, *, training_block: bool):


    tp = wall(WALL_LEN_MM, WALL_LAYERS, WALL_LAYER_H)
    origin = cell_lc.snap_toolpath_to_platter(tp, ORIGIN_X, float(origin_y),
                                              substrate_mm=SUBSTRATE_MM)
    return tp, origin, cell_lc.lc_scene(part_origin_robot=origin,
                                        training_block=training_block)


def _tier1(tp, scene, margin_mm):
    return clearance.check(tp, TorchModel.yaskawa_accufast_45(), scene,
                           bead_height_mm=WALL_LAYER_H,
                           margin_mm=margin_mm, contact_ok=CONTACT_OK)


def select_scenarios(margin_mm: float):


    clean = None
    foul = None
    scanned = []
    for y in ORIGIN_Y_SCAN:


        for with_block, want_clean in ((CLEAN_WITH_BLOCK, True),
                                       (FOUL_WITH_BLOCK, False)):
            try:
                tp, origin, scene = _wall_at(y, training_block=with_block)
            except ValueError:
                continue
            b = blocking(_tier1(tp, scene, margin_mm))
            worst = min((x.clearance_mm for x in b), default=None)
            if want_clean:
                scanned.append((float(y), len(b), worst))
                if not b and clean is None:
                    clean = float(y)
            elif b and (foul is None or worst < foul[1]):
                foul = (float(y), worst)
    return clean, (foul[0] if foul else None), scanned


def probe_grid(quick: bool, wide: bool):


    if wide:
        return (np.arange(-200.0, 1400.0, 100.0),
                np.arange(-1000.0, 300.0, 100.0),
                np.arange(-100.0, 1200.0, 150.0))
    step = 100.0 if quick else 50.0
    return (np.arange(300.0, 750.0, step),
            np.arange(-1000.0, -650.0, step),
            np.arange(-100.0, 250.0, step))


def tier1_clean_poses(scene, origin, margin_mm, grid):


    torch = TorchModel.yaskawa_accufast_45()
    out = []
    xs, ys, zs = grid
    for rx in xs:
        for ry in ys:
            for rz in zs:
                tcp = np.array([float(rx), float(ry), float(rz)])
                part = tcp - origin
                try:
                    q = cell_lc.pulse_solver(origin)(tuple(part))
                except ValueError:
                    continue
                if blocking(clearance.check_point(part, torch, scene,
                                                  margin_mm=margin_mm,
                                                  contact_ok=CONTACT_OK)):
                    continue
                out.append((tcp, part, q))
    return out


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
            print("VERDICT: T13 NOT MET - a check failed above.")
            return 1
        if any(p is None for p in marks):
            print("VERDICT: T13 UNPROVEN - the engine never ran. "
                  "Nothing here has been executed against PyBullet.")
            return 2
        print(f"VERDICT: T13 DONE-WHEN MET ({len(marks)}/{len(marks)} checks passed).")
        return 0


def _pending_note() -> str:
    return (
        "waiting on the engine. When PyBullet is importable this check will:\n"
        "  - load robots/ar1440.urdf with useFixedBase and map every joint by name\n"
        "  - place the scene, the torch (TOOL INTERFERE 0) and the growing beads\n"
        "  - step the path through cell_lc.pulse_solver and query getClosestPoints\n"
        "  - report in clearance.Violation and compare verdicts with tier 1")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="T13 acceptance: tier-2 whole-arm interference, "
                    "checked against tier-1 on three scenarios.")
    ap.add_argument("--gui", action="store_true",
                    help="open the PyBullet window and play the clean path back")
    ap.add_argument("--sample-mm", type=float, default=4.0)
    ap.add_argument("--margin-mm", type=float, default=5.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--wide-probe", action="store_true")
    args = ap.parse_args(argv)
    sample_mm = 12.0 if args.quick else args.sample_mm

    print(RULE)
    print("T13 ACCEPTANCE - tier-2 whole-arm interference (sim/pybullet_driver.py)")
    print(RULE)


    if pbd.HAVE_PYBULLET:
        import pybullet

        print(f"engine     : pybullet {getattr(pybullet, '__version__', 'unknown')} "
              f"({'GUI' if args.gui else 'DIRECT'})")
    else:
        print(f"engine     : NOT AVAILABLE - {pbd.PYBULLET_IMPORT_ERROR}")
        print(f"             {pbd.WSL2_HINT}")

    report = pbd.inspect_urdf()
    problems = report.problems()
    print(f"urdf       : {report.path.relative_to(ROOT)}")
    print(f"             {len(report.links)} links, {len(report.joints)} joints, "
          f"{len(report.mesh_files)} mesh refs, {len(report.missing_meshes)} unresolved, "
          f"{report.inertials} inertials")
    if problems:
        print("             UNFIT: " + "; ".join(problems))
        print(RULE)
        print("VERDICT: T13 NOT MET - the URDF cannot be loaded as it stands.")
        return 1
    print(f"             fit to load; station joint is "
          f"{report.joint_types[pbd.station.STATION_JOINT_NAME]!r}")
    print(f"sampling   : {sample_mm} mm between waypoints, {args.margin_mm} mm margin")
    print(THIN)


    t0 = time.time()
    clean_y, foul_y, scanned = select_scenarios(args.margin_mm)
    print(f"scenario scan: {len(scanned)} platter seats in "
          f"{ORIGIN_Y_SCAN[0]:.0f}..{ORIGIN_Y_SCAN[-1]:.0f} mm "
          f"({time.time() - t0:.1f}s)")
    print(f"  known-clean   : origin_y = "
          f"{'none found' if clean_y is None else f'{clean_y:.0f} mm'}")
    if foul_y is None:
        print("  known-fouling : none found")
    else:
        _tp, _o, _sc = _wall_at(foul_y, training_block=FOUL_WITH_BLOCK)
        worst = min(blocking(_tier1(_tp, _sc, args.margin_mm)),
                    key=lambda v: v.clearance_mm)
        print(f"  known-fouling : origin_y = {foul_y:.0f} mm  "
              f"(tier 1: {worst.clearance_mm:+.2f} mm to {worst.obstacle})")
    print(THIN)

    out = Report()
    drv_kwargs = dict(gui=args.gui)


    if pbd.HAVE_PYBULLET:
        with pbd.PyBulletDriver(**drv_kwargs) as drv:
            print("collision shapes as the engine built them:")
            for rep in drv.inspect_collision_shapes():
                print(f"  {rep.link:12s} {rep.verdict}")
            print("  NOTE vertex counts do not measure hull inflation or prove containment.")
            print("       An exact hull is conservative relative to its input mesh, not")
            print("       necessarily the real robot. Sampling, meshes, omitted geometry")
            print("       and unsurveyed placement remain separate sources of error.")
            print()
            print("structural contacts at the home pose (all-zero arm):")
            home = drv.contacts_at([0.0] * 6, margin_mm=args.margin_mm,
                                   include_beads=False)
            if not home:
                print("  none")
            for link, obstacle, mm in sorted(home, key=lambda r: r[2]):
                print(f"  {link:12s} {mm:+8.2f} mm  {obstacle}")
            print(f"  ({len(pbd.DEFAULT_IGNORE_PAIRS)} mounting pairs are already "
                  f"suppressed: {list(pbd.DEFAULT_IGNORE_PAIRS)})")
        print(THIN)

    print("CHECKS")


    if clean_y is None:
        out.add("known-clean path reproduces tier-1's verdict", False,
                "no tier-1-clean seat found on the platter band; the scene may "
                "have changed. Widen ORIGIN_Y_SCAN or look at the fixture block.")
    else:
        tp, origin, scene = _wall_at(clean_y, training_block=CLEAN_WITH_BLOCK)
        t1 = _tier1(tp, scene, args.margin_mm)
        detail = [f"wall {WALL_LEN_MM:.0f} x {WALL_LAYERS} at origin_y {clean_y:.0f}, "
                  f"{len(tp.moves)} moves",
                  f"tier 1: CLEAN ({len(t1)} sub-margin note(s), 0 blocking)"]
        if not pbd.HAVE_PYBULLET:
            out.add("known-clean path reproduces tier-1's verdict", None,
                    "\n".join(detail) + "\ntier 2: " + _pending_note())
        else:
            with pbd.PyBulletDriver(scene=scene, part_origin_robot=origin,
                                    **drv_kwargs) as drv:
                t2 = drv.check(tp, bead_height_mm=WALL_LAYER_H, margin_mm=args.margin_mm,
                               sample_mm=sample_mm)
            cmp = pbd.compare(t1, t2.violations)
            detail.append(f"tier 2: {len(t2.blocking())} blocking over "
                          f"{t2.poses} poses, {t2.beads} beads, "
                          f"{len(t2.unreachable)} unreachable")
            detail.append(cmp.summary())
            detail += cmp.notes()
            out.add("known-clean path reproduces tier-1's verdict",
                    cmp.verdict_agrees and not t2.blocking() and not t2.unreachable,
                    "\n".join(detail))


    if foul_y is None:
        out.add("known-fouling path reproduces tier-1's verdict", False,
                "no tier-1-fouling seat found on the platter band; if the "
                "fixture block came off, put it back or foul something else.")
    else:
        tp, origin, scene = _wall_at(foul_y, training_block=FOUL_WITH_BLOCK)
        t1 = _tier1(tp, scene, args.margin_mm)
        b1 = blocking(t1)
        w1 = min(b1, key=lambda v: v.clearance_mm)
        detail = [f"wall {WALL_LEN_MM:.0f} x {WALL_LAYERS} at origin_y {foul_y:.0f}",
                  f"tier 1: {len(b1)} blocking, worst {w1.clearance_mm:+.2f} mm "
                  f"to {w1.obstacle} at move {w1.move_index}"]
        if not pbd.HAVE_PYBULLET:
            out.add("known-fouling path reproduces tier-1's verdict", None,
                    "\n".join(detail) + "\ntier 2: " + _pending_note())
        else:
            with pbd.PyBulletDriver(scene=scene, part_origin_robot=origin,
                                    **drv_kwargs) as drv:
                t2 = drv.check(tp, bead_height_mm=WALL_LAYER_H, margin_mm=args.margin_mm,
                               sample_mm=sample_mm)
            b2 = t2.blocking()
            cmp = pbd.compare(t1, t2.violations)
            if b2:
                w2 = min(b2, key=lambda v: v.clearance_mm)
                detail.append(f"tier 2: {len(b2)} blocking, worst "
                              f"{w2.clearance_mm:+.2f} mm to {w2.obstacle}")
            else:
                detail.append("tier 2: CLEAN - it did NOT see what tier 1 saw")
            detail.append(cmp.summary())
            detail += cmp.notes()
            out.add("known-fouling path reproduces tier-1's verdict",
                    cmp.verdict_agrees and bool(b2), "\n".join(detail))


    origin = cell_lc.DEFAULT_PART_ORIGIN
    scene = cell_lc.lc_scene(part_origin_robot=origin)
    grid = probe_grid(args.quick, args.wide_probe)
    t0 = time.time()
    candidates = tier1_clean_poses(scene, origin, args.margin_mm, grid)
    detail = [f"probed {len(grid[0]) * len(grid[1]) * len(grid[2])} TCP positions; "
              f"{len(candidates)} are reachable AND tier-1 clean "
              f"({time.time() - t0:.1f}s)",
              "tier 1 has no arm model at all, so every finding below is one it "
              "structurally cannot produce"]
    if not pbd.HAVE_PYBULLET:
        out.add("finds an arm-body case tier 1 cannot see", None,
                "\n".join(detail) + "\ntier 2: " + _pending_note())
    elif not candidates:
        out.add("finds an arm-body case tier 1 cannot see", False,
                "\n".join(detail) + "\nthe probe grid found no tier-1-clean pose "
                "to test; try --wide-probe")
    else:
        found: list[tuple[float, np.ndarray, str, str]] = []
        with pbd.PyBulletDriver(scene=scene, part_origin_robot=origin,
                                **drv_kwargs) as drv:
            for tcp, part, q in candidates:
                for link, obstacle, mm in drv.contacts_at(
                        q, margin_mm=args.margin_mm, include_beads=False):
                    if link in pbd.TORCH_SHAPE_NAMES:
                        continue
                    found.append((mm, tcp, link, obstacle))
        found.sort(key=lambda r: r[0])
        if found:
            worst = found[0]
            detail.append(f"tier 2: {len(found)} arm-link finding(s) across "
                          f"{len({tuple(f[1]) for f in found})} pose(s)")
            for mm, tcp, link, obstacle in found[:5]:
                detail.append(f"  TCP (robot) {np.round(tcp, 0).tolist()}: "
                              f"{link} {mm:+.2f} mm to {obstacle}")
            detail.append(f"worst: {worst[2]} {worst[0]:+.2f} mm into {worst[3]} "
                          f"while the torch is clear")
        else:
            detail.append("tier 2: no arm-link finding in this grid")
        out.add("finds an arm-body case tier 1 cannot see", bool(found),
                "\n".join(detail))


    if args.gui and pbd.HAVE_PYBULLET and clean_y is not None:
        print(THIN)
        print("GUI playback of the clean path (close the window to finish)...")
        tp, origin, scene = _wall_at(clean_y, training_block=CLEAN_WITH_BLOCK)
        with pbd.PyBulletDriver(scene=scene, part_origin_robot=origin, gui=True) as drv:
            solve = cell_lc.pulse_solver(origin)
            for move in tp.moves:
                try:
                    drv.pose(q_deg=solve(move.target))
                except ValueError:
                    continue
                time.sleep(0.25)

    return out.verdict()


if __name__ == "__main__":
    raise SystemExit(main())
