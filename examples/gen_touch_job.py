'Emit a platter-face touch-search probe job (re-enabled 2026-09-19).'


from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, touch
from waam_pipeline.backends.jbi import emit_search_jbi
from waam_pipeline.clearance import ClearanceError, TorchModel
from waam_pipeline.emit import write_job


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="R1_TOUCH_FACE", help="controller job name")
    ap.add_argument("--out", default="touch_face.JBI", help="output .JBI path")
    ap.add_argument("--rin", type=int, default=3, choices=(3, 4),
                    help="search input: 3=WIRE TOUCH (the Lincoln R450 wire-contact sense, "
                         "SRCH RIN#3 / enable OT#1891 -- this is what the LC cell HAS); "
                         "4=AccuFast LASER (non-contact triangulation, SRCH RIN#4 / OT#1892+OT#4037 "
                         "-- a SEPARATE product NOT installed on this cell). AccuFast != R450.")
    ap.add_argument("--clean-job", default="CLEAN_R1",
                    help="controller job to ream + CUT the wire before probing, for a clean "
                         "repeatable stick-out the wire-touch sense needs (CALL JOB:<name>). "
                         "Empty string to skip. Must exist on the controller.")


    ap.add_argument("--grid", type=int, default=None,
                    help="N for an N x N probe lattice (denser touch-off, N>=2); "
                         "omit for the default 4-corner pattern")
    ap.add_argument("--centre-x", type=float, default=1014.0,
                    help="probe pattern centre X in robot mm (default model platter centre)")
    ap.add_argument("--centre-y", type=float, default=-300.0,
                    help="probe pattern centre Y in robot mm")
    ap.add_argument("--half-span", type=float, default=120.0,
                    help="half-width of the probe pattern in mm (keep it on the reachable face)")
    ap.add_argument("--date", default=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"))
    args = ap.parse_args()


    if Path(args.out).exists():
        print(f"REFUSED: {args.out} already exists; choose a new --out path")
        return 1


    origin = np.zeros(3)
    plan = touch.platter_probe_plan(cell_lc.PLATTER_FACE_Z, grid=args.grid,
                                    centre_xy=(args.centre_x, args.centre_y),
                                    half_span_mm=args.half_span)
    solve = cell_lc.pulse_solver(origin)
    scene = cell_lc.lc_scene(origin)
    torch = TorchModel.yaskawa_accufast_45()

    try:
        job = emit_search_jbi(plan, solve, name=args.name, date=args.date,
                              scene=scene, torch=torch, rin=args.rin,
                              clean_job=(args.clean_job or None))
    except ClearanceError as exc:
        print(f"REFUSED: an approach point fouls the cell -- {exc}")
        return 1
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1


    write_job(job, Path(args.out))
    print(f"wrote {args.out}  ({len(plan)} probes)")
    print("read these back (pendant VARIABLE, coordinate PULSE) in this order:")
    for i, pr in enumerate(plan):
        print(f"  P{40 + i:03d}  {pr.name}")
    print("\nREVIEW REQUIRED before running on the cell (see the job's comment "
          "block). Then: python examples/touch_search_datum.py <name> < pulses.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
