'HC10DTP touch-sense PROBE job -- the cobot analogue of gen_touch_job.py (GATED).'


from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_hc
from waam_pipeline.backends.jbi import _render_hc_search_jbi
from waam_pipeline.clearance import ClearanceError
from waam_pipeline.emit import write_job


EXPERIMENTAL_SUFFIX = "-EXPERIMENTAL"

EXPERIMENTAL_WARNING = """
!!! EXPERIMENTAL -- NOTHING BELOW WAS CHECKED !!!
    NOT checked: torch/arm clearance on every probe approach and retract
                 (rendered with scene=None, torch=None -- the guard did not run)
    NOT checked: the table height the probes search toward (nominal TABLE_Z,
                 no cell survey)
    NOT confirmed: WHICH RIN the 48V wire-touch latches on, so the search may
                 not stop on contact
    NOT calibrated: hc_scene is nominal vendor-furniture AABBs, not a verdict
    The job is stamped "clearance NO - UNCHECKED" and named with
    "%s". SINGLE-STEP it, hand on the e-stop.
""".rstrip() % EXPERIMENTAL_SUFFIX


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="HC-CALIB-TOUCH", help="controller job name")
    ap.add_argument("--out", default="hc_touch_face.JBI", help="output .JBI path")
    ap.add_argument("--rin", type=int, default=3, choices=(3, 4),
                    help="search input: 3=48V wire-touch (OT#1891) -- the HC's installed "
                         "sense; 4=AccuFast (OT#1892). WHICH RIN the 48V-TS actually latches "
                         "on is UNCONFIRMED on this cobot -- verify on the pendant "
                         "{IN/OUT}->{RIN} before running")
    ap.add_argument("--clean-job", default="",
                    help="controller job to ream+cut the wire before probing (CALL JOB:<name>); "
                         "empty string (default) = none. The HC cleaner job is CLEAN, but its "
                         "placement in this job is unconfirmed.")
    ap.add_argument("--experimental", action="store_true",
                    help="emit even when the nominal hc_scene refuses the approach, "
                         "with NO clearance check at all. The job and the file both "
                         f"get a {EXPERIMENTAL_SUFFIX} marker. Without this flag a "
                         "refused approach is a refusal.")


    ap.add_argument("--date", default=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"))
    args = ap.parse_args()


    origin = np.zeros(3)
    plan = cell_hc.hc_probe_plan(cell_hc.TABLE_Z, solve=cell_hc.hc_solver(origin))
    if not plan:
        print("REFUSED: no reachable probe on the nominal HC table")
        return 1
    scene = cell_hc.hc_scene(origin)
    torch = cell_hc.hc_torch_controller_tool0()

    def render(sc, tr, job_name):
        return _render_hc_search_jbi(plan, cell_hc.hc_solver(origin), name=job_name,
                                     date=args.date, scene=sc, torch=tr, rin=args.rin,
                                     clean_job=(args.clean_job or None))

    out = Path(args.out)
    try:
        job = render(scene, torch, args.name)
    except ClearanceError as exc:


        if not args.experimental:
            print(f"REFUSED: the nominal hc_scene refused the approach ({exc}).")
            print(EXPERIMENTAL_WARNING)
            print("Pass --experimental to emit it anyway.")
            return 1
        job_name = f"{args.name}{EXPERIMENTAL_SUFFIX}"
        out = out.with_name(f"{out.stem}{EXPERIMENTAL_SUFFIX}{out.suffix}")
        try:
            job = render(None, None, job_name)
        except ValueError as exc2:
            print(f"REFUSED: {exc2}")
            return 1
        print(f"NOTE: nominal hc_scene refused the approach ({exc});")
        print(EXPERIMENTAL_WARNING)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1


    write_job(job, out)
    print(f"wrote {out}  ({len(plan)} probes, RIN#{args.rin})")
    print("read these back (pendant VARIABLE, coordinate PULSE) in this order:")
    for i, pr in enumerate(plan):
        print(f"  P{40 + i:03d}  {pr.name}")
    print("\nREVIEW REQUIRED before running on the cobot (see the job's comment "
          "block). Export stays DISABLED (emit_hc_search_jbi refuses): the search "
          "RIN is UNCONFIRMED on this cell -- confirm on the pendant "
          "{IN/OUT}->{RIN}, then SINGLE-STEP the search.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
