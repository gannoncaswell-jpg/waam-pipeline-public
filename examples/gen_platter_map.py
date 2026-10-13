'Generate reviewed platter-map search jobs and their replayable touch plan.'


from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import platter_map, var_dat
from waam_pipeline.clearance import ClearanceError
from waam_pipeline.emit import write_job


def _readme(plan, jobs: dict[str, str]) -> str:
    rows = platter_map.plan_json(plan)["probes"]
    inventory = "\n".join(
        f"  {name}.JBI: {sum(row['job'] == j for row in rows)} touches; "
        f"export VAR_PLATMAP{j}.DAT"
        for j, name in enumerate(jobs, 1)
    )
    return (
        "PLATTER MAP - LC CELL / AR1440\n\n"
        "1. Set S1 to 0.0 deg and remove the ManPro training block (block off).\n"
        "   Keep the same platter setup throughout all jobs and exports.\n"
        "2. Confirm the taught P040-P043 corners and hole rim touches describe\n"
        "   this setup. Review expected.csv and every job before loading.\n"
        "3. Confirm CLEAN_R1 is present; every job calls it first to cut the wire.\n"
        "   Confirm RIN#3 wire contact and the work area are clear.\n"
        f"   Searches use V=10 (10 mm/s), DIS=5 mm and {plan.hop_mm:g} mm hops.\n"
        f"   Maximum surface departure from the corner plane: {plan.max_bow_mm:g} mm.\n"
        "   Raise --max-bow and --hop if the platter may bow more, or if anything protrudes.\n"
        "   Check hop clearance over every protrusion and between stations.\n"
        "4. Perform a two-person supervised dry run of PLATMAP1 before mapping.\n"
        "   Verify approach, wire contact, retraction and the search-miss alarm.\n"
        "5. Run each job. Do not accept a run that raises a search-miss alarm.\n"
        "   Every job is A,B / up to 16 new stations / A,B: the same two tie\n"
        "   stations are touched at both ends. Keep all four tie results.\n"
        "6. Export VAR.DAT after EACH job, before running the next job:\n"
        f"{inventory}\n"
        "   P044-P063 are reused; the next job overwrites the previous results.\n"
        "7. Keep plan.json with all exports. Read them back in job-number order:\n"
        "   python examples/platter_map_readback.py plan.json "
        + " ".join(f"VAR_PLATMAP{j}.DAT" for j in range(1, len(jobs) + 1))
        + " --out readback\n"
        "   Review tie consistency, fit residuals and flatness before using the map.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corners", required=True, type=Path,
                    help="VAR.DAT containing corner touches P040-P043")
    ap.add_argument("--holes", action="append", default=[], type=Path,
                    help="hole rim export; repeat with --hole-points and --hole-count")
    ap.add_argument("--hole-points", action="append", default=[], type=int, choices=(3, 4),
                    help="rim touches per hole for the corresponding --holes file")
    ap.add_argument("--hole-count", action="append", default=[], type=int,
                    help="number of holes in the corresponding --holes file")
    ap.add_argument("--target", type=int, default=100, help="target number of grid stations")
    ap.add_argument("--hop", type=float, default=5.0,
                    help="hop height in mm (2..20; at least --max-bow + 3)")
    ap.add_argument("--max-bow", type=float, default=2.0,
                    help="maximum surface departure from the corner plane in mm (0.5..10)")
    ap.add_argument("--margin", type=float, default=8.0, help="clearance outside each hole in mm")
    ap.add_argument("--out", required=True, type=Path, help="new output directory")
    ap.add_argument("--folder", default="WAAM", help="controller job folder")
    ap.add_argument("--date", default=_dt.datetime.now().strftime("%Y/%m/%d %H:%M"))
    args = ap.parse_args()

    if args.out.exists():
        print(f"REFUSED: {args.out} already exists; choose a new --out directory")
        return 1
    if not len(args.holes) == len(args.hole_points) == len(args.hole_count):
        ap.error("each --holes file needs one --hole-points and one --hole-count")
    try:
        corners = platter_map.corners_from_records(var_dat.probe_records(args.corners, 40, 4))
        holes = []
        for path, points, count in zip(args.holes, args.hole_points, args.hole_count):
            if count < 1 or points * count > 20:
                raise ValueError(f"{path}: require 1..20 consecutive rim touches in P044-P063")
            records = var_dat.probe_records(path, 44, points * count)
            holes.extend(platter_map.holes_from_records(records, points, corners=corners))
        plan = platter_map.map_plan(corners, holes, target=args.target,
                                     hop_mm=args.hop, max_bow_mm=args.max_bow,
                                     hole_margin_mm=args.margin)
        jobs = platter_map.render_jobs(plan, folder=args.folder, date=args.date)
        record = platter_map.plan_json(plan)
        args.out.mkdir(parents=True)
        for name, job in jobs.items():
            write_job(job, args.out / f"{name}.JBI")
        (args.out / "plan.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        with (args.out / "expected.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("job", "pvar", "role", "x", "y", "z_expected"))
            for row in record["probes"]:
                writer.writerow((row["job"], f"P{row['pvar']:03d}", row["role"], *row["expected"]))
        (args.out / "README.txt").write_text(_readme(plan, jobs), encoding="utf-8")
    except (ValueError, OSError, ClearanceError) as exc:
        print(f"REFUSED: {exc}")
        return 1

    print(f"wrote {len(jobs)} jobs and {len(plan.points)} grid stations to {args.out}")
    print("Two-person review and dry run required; follow README.txt and export after EACH job.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
