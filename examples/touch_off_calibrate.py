'Calibrate a work-holding fixture from touch-off points. No controller setup.'


from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc

CALIBRATION_PATH = ROOT / "docs" / "cell-calibration.json"


def read_touches(argv_touches: list[str]) -> list[list[int]]:

    raw = list(argv_touches)
    if not raw:
        if sys.stdin.isatty():
            print("Paste one PULSE record per line (S L U R B T), blank line to finish:")
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
            raise SystemExit(
                f"touch {i}: expected 6 pulse values (S L U R B T), got "
                f"{len(parts)} in {line!r}"
            )
        try:
            out.append([int(round(float(p))) for p in parts])
        except ValueError as exc:
            raise SystemExit(f"touch {i}: {exc} in {line!r}") from exc
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="fixture name, e.g. charles_platter")
    ap.add_argument("--touch", action="append", default=[],
                    metavar="'S L U R B T'", help="one pulse record; repeatable")
    ap.add_argument("--substrate", type=float, default=0.0, metavar="MM",
                    help="thickness between the work surface and what you touched")
    ap.add_argument("--stickout-correction", type=float, default=0.0, metavar="MM",
                    help="physical stickout minus the stickout TOOL 0 was taught at")
    ap.add_argument("--save", action="store_true",
                    help="write the fixture into docs/cell-calibration.json")
    ap.add_argument("--activate", action="store_true",
                    help="with --save, also make it the active_fixture")
    args = ap.parse_args()

    touches = read_touches(args.touch)
    print(f"\n{len(touches)} touch point(s) for {args.name!r}\n")

    pts = np.array([cell_lc.tcp_from_pulses(p) for p in touches], float)
    print("  #   pulses (S L U R B T)                          wire tip x, y, z (robot mm)")
    for i, (p, t) in enumerate(zip(touches, pts), 1):
        print(f"  {i}   {str(p):<45} {t[0]:9.2f} {t[1]:9.2f} {t[2]:9.2f}")

    try:
        surface = cell_lc.fit_work_surface(
            args.name, touches,
            substrate_mm=args.substrate,
            stickout_correction_mm=args.stickout_correction,
        )
    except ValueError as exc:
        print(f"\nFIT REFUSED: {exc}\n")
        return 1

    print(f"\n  {surface.summary()}")
    print(f"  plane normal      {np.round(surface.normal, 5)}")
    print(f"  centroid          {np.round(surface.centroid, 2)}")
    print("\n  per-point deviation from the fitted plane:")
    for i, p in enumerate(surface.points, 1):
        d = float((p - surface.centroid) @ surface.normal)
        print(f"    {i}  {d:+7.3f} mm")

    if surface.residual_mm > 0.5:
        print(f"\n  WARNING: worst point is {surface.residual_mm:.2f} mm off the "
              "fitted plane. Either the surface is not flat, a touch was light "
              "or heavy, or a record was mistyped. Re-take before trusting it.")
    if args.stickout_correction == 0.0:
        print("\n  NOTE: stickout correction is 0. That is only right if the "
              "physical stickout matched the stickout TOOL 0 was taught at. "
              "If the wire was longer, the real surface is LOWER than this and "
              "jobs will run into it.")


    face_at_centroid = surface.z_at(float(surface.centroid[0]), float(surface.centroid[1]))
    print(f"\n  equivalent flat offset at the centroid: "
          f"{face_at_centroid - cell_lc.PLATTER_FACE_Z_MODEL:+.3f} mm "
          f"(model face {cell_lc.PLATTER_FACE_Z_MODEL:.1f})")

    block = {
        "touch_pulses": touches,
        "substrate_mm": args.substrate,
        "stickout_correction_mm": args.stickout_correction,
        "_fitted": {
            "tilt_deg": round(surface.tilt_deg, 4),
            "residual_mm": round(surface.residual_mm, 4),
            "centroid_mm": [round(float(v), 3) for v in surface.centroid],
            "_note": "derived, for reading only - the plane is refitted from "
                     "touch_pulses at import so this can never drift",
        },
    }
    print(f"\n  fixture block for docs/cell-calibration.json under \"fixtures\":\n")
    print("    " + json.dumps({args.name: block}, indent=2).replace("\n", "\n    "))

    if args.save:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        data.setdefault("fixtures", {})[args.name] = block
        if args.activate or data.get("active_fixture") is None:
            data["active_fixture"] = args.name
        CALIBRATION_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\n  saved to {CALIBRATION_PATH}"
              f"{' and made active' if data['active_fixture'] == args.name else ''}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
