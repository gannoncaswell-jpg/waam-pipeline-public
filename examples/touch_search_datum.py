"Recover the platter-face datum from a touch-sense probe job's results (T17)."


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, touch, var_dat

CALIBRATION_PATH = ROOT / "docs" / "cell-calibration.json"


def read_records(argv_records: list[str]) -> list[list[int]]:

    raw = list(argv_records)
    if not raw:
        if sys.stdin.isatty():
            print("Paste one PULSE record per probe (S L U R B T), P040 first, "
                  "blank line to finish:")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                break
            if not line.startswith("#"):
                raw.append(line)
    out: list[list[int]] = []
    for i, line in enumerate(raw, 1):
        parts = [p for p in line.replace(",", " ").split() if p]
        if len(parts) != 6:
            raise SystemExit(f"record {i}: expected 6 pulse values, got "
                             f"{len(parts)} in {line!r}")
        try:
            out.append([int(round(float(p))) for p in parts])
        except ValueError as exc:
            raise SystemExit(f"record {i}: {exc} in {line!r}") from exc
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="fixture name, e.g. charles_platter")
    ap.add_argument("--p", action="append", default=[], metavar="'S L U R B T'",
                    help="one probe result P-var; repeatable, in probe order")
    ap.add_argument("--substrate", type=float, default=0.0, metavar="MM",
                    help="thickness between the work surface and what was touched")
    ap.add_argument("--stickout-correction", type=float, default=0.0, metavar="MM",
                    help="physical stickout minus the stickout TOOL 0 was taught at")
    ap.add_argument("--tolerance", type=float, default=15.0, metavar="MM",
                    help="max distance a hit may fall from where the probe aimed")
    ap.add_argument("--save", action="store_true",
                    help="write the fixture into docs/cell-calibration.json")
    ap.add_argument("--force", action="store_true",
                    help="fit even if a hit failed the plan check")
    ap.add_argument("--var-dat", metavar="PATH",
                    help="read probe results from an exported VAR.DAT instead of "
                         "pasting them (P[--pvar-base].. in plan order)")
    ap.add_argument("--pvar-base", type=int, default=40, metavar="N",
                    help="first P-variable the probe job stored into (default 40)")
    ap.add_argument("--grid", type=int, metavar="N",
                    help="read back an NxN grid job (default: the 4-corner plan)")
    ap.add_argument("--centre", type=float, nargs=2, metavar=("X", "Y"),
                    help="probe centre in robot mm (must match the emitted job)")
    ap.add_argument("--half-span", type=float, metavar="MM",
                    help="probe half-span in mm (must match the emitted job)")
    args = ap.parse_args()


    plan_kw = {}
    if args.grid is not None:
        plan_kw["grid"] = args.grid
    if args.centre is not None:
        plan_kw["centre_xy"] = tuple(args.centre)
    if args.half_span is not None:
        plan_kw["half_span_mm"] = args.half_span
    plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z, **plan_kw)
    if args.var_dat:
        records = var_dat.probe_records(args.var_dat, args.pvar_base, len(plan))
    else:
        records = read_records(args.p)
    if len(records) != len(plan):
        raise SystemExit(f"got {len(records)} records for {len(plan)} probes -- "
                         "read back one P-var per probe, in order (P040 first)")

    points = [cell_lc.tcp_from_pulses(r) for r in records]
    hits = touch.hits_from_points(plan, points)
    print(f"\n{len(hits)} probe result(s) for {args.name!r}\n")
    print("  probe             wire tip x, y, z (robot mm)")
    for pr, pt in zip(plan, points):
        print(f"  {pr.name:<16}  {pt[0]:9.2f} {pt[1]:9.2f} {pt[2]:9.2f}")

    problems = touch.check_hits(plan, hits, tolerance_mm=args.tolerance)
    if problems:
        print("\n  PLAN CHECK FAILED:")
        for p in problems:
            print(f"    - {p}")
        if not args.force:
            print("\n  Refusing to fit. A hit that landed far from where the probe "
                  "aimed is usually a missed search reporting its overtravel "
                  "endpoint, or the wrong feature. Re-run the probe, or --force if "
                  "you have confirmed the hits.")
            return 1

    try:
        surface = cell_lc.fit_work_surface(
            args.name, records,
            substrate_mm=args.substrate,
            stickout_correction_mm=args.stickout_correction,
        )
    except ValueError as exc:
        print(f"\nFIT REFUSED: {exc}\n")
        return 1

    print(f"\n  {surface.summary()}")
    face_at_centroid = surface.z_at(float(surface.centroid[0]), float(surface.centroid[1]))
    print(f"  equivalent flat offset at the centroid: "
          f"{face_at_centroid - cell_lc.PLATTER_FACE_Z_MODEL:+.3f} mm "
          f"(model face {cell_lc.PLATTER_FACE_Z_MODEL:.1f})")
    if surface.residual_mm > 0.5:
        print(f"\n  WARNING: worst point is {surface.residual_mm:.2f} mm off the "
              "fitted plane -- the surface may not be flat, or a search was light.")

    block = {
        "touch_pulses": records,
        "substrate_mm": args.substrate,
        "stickout_correction_mm": args.stickout_correction,
        "source": "touch-sense search (gen_touch_job.py)",
        "_fitted": {
            "tilt_deg": round(surface.tilt_deg, 4),
            "residual_mm": round(surface.residual_mm, 4),
            "centroid_mm": [round(float(v), 3) for v in surface.centroid],
            "_note": "derived, for reading only - the plane is refitted from "
                     "touch_pulses at import so this can never drift",
        },
    }
    print("\n  fixture block for docs/cell-calibration.json under \"fixtures\":\n")
    print("    " + json.dumps({args.name: block}, indent=2).replace("\n", "\n    "))

    if args.save:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        data.setdefault("fixtures", {})[args.name] = block
        if data.get("active_fixture") is None:
            data["active_fixture"] = args.name
        CALIBRATION_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\n  saved to {CALIBRATION_PATH}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
