'Generate the platter-mapping job and the sheet to record it on.'


from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, station
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import (
    EmitRefused, PlatterState, emit_job, write_job,
)
from waam_pipeline.toolpath import Move, Toolpath


PLATE_INSET_MM = 60.0


STANDOFF_MM = 25.0

HOVER_MM = 80.0


def grid_points(nx: int, ny: int) -> list[tuple[float, float]]:


    length, width, _ = cell_lc.CHARLES_PLATE
    xs = np.linspace(-length / 2 + PLATE_INSET_MM, length / 2 - PLATE_INSET_MM, nx)
    ys = np.linspace(-width / 2 + PLATE_INSET_MM, width / 2 - PLATE_INSET_MM, ny)


    out: list[tuple[float, float]] = []
    for j, y in enumerate(ys):
        row = xs if j % 2 == 0 else xs[::-1]
        out.extend((float(x), float(y)) for x in row)
    return out


def rotated_points(points, theta_deg: float, origin) -> list[tuple[float, float, float]]:


    T = station.part_frame_transform(theta_deg, origin)
    out = []
    for px, py in points:
        p = np.array([px, py, 0.0, 1.0])
        moved = (T @ p)[:3] if theta_deg else p[:3]
        out.append((float(moved[0]), float(moved[1]), float(moved[2])))
    return out


def probe_toolpath(points, standoff_mm: float) -> Toolpath:


    tp = Toolpath()
    line = 1
    for x, y, z in points:
        tp.moves.append(Move(x, y, z + standoff_mm + HOVER_MM, 3000.0, False, 0, line))
        line += 1
        tp.moves.append(Move(x, y, z + standoff_mm, 600.0, False, 0, line))
        line += 1
        tp.moves.append(Move(x, y, z + standoff_mm + HOVER_MM, 3000.0, False, 0, line))
        line += 1
    return tp


def build_workbook(rows: list[dict], path: Path, meta: dict) -> Path:


    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "points"

    head = ["point", "job file", "station deg", "part X mm", "part Y mm",
            "Job TCP X mm", "Job TCP Y mm", "Job TCP Z mm",
            "Model TCP contact Z mm", "Commanded TCP lift mm",
            "Measured contact-tip gap mm", "Surface Z mm (offset required)", "notes",
            "Verified tip minus TCP Z mm", "Measurement C-record"]
    ws.append(head)
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF2CC")
    for c, _ in enumerate(head, start=1):
        ws.cell(row=1, column=c).font = bold
        ws.cell(row=1, column=c).alignment = Alignment(wrap_text=True, vertical="top")

    for i, r in enumerate(rows, start=2):
        ws.append([r["point"], r["job"], r["theta"], r["px"], r["py"],
                   r["rx"], r["ry"], r["rz"], r["face"], r["standoff"],
                   None, None, None, None, r.get("record")])


        ws.cell(row=i, column=12).value = (
            f'=IF(OR(COUNT(K{i},N{i})<>2,K{i}<0),"",H{i}+N{i}-K{i})')
        ws.cell(row=i, column=11).fill = fill
        ws.cell(row=i, column=13).fill = fill
        ws.cell(row=i, column=14).fill = fill

    widths = [14, 22, 12, 12, 12, 15, 15, 15, 18, 18, 21, 21, 35, 23, 19]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"

    how = wb.create_sheet("how to run this")
    for line in [
        ["Platter map - contact tip to platter"],
        [],
        ["Generated", meta["when"]],
        ["Model reference", meta["commit"]],
        ["Model TCP contact Z (not surveyed physical face)", meta["face_z"]],
        ["Platter face offset now in cell-calibration.json", meta["offset"]],
        ["Standoff commanded above the predicted face", meta["standoff"]],
        [],
        ["RUN IT IN TEACH MODE, SLOW, ONE STEP PER POINT."],
        ["There are no per-point PAUSE instructions; stop by TEACH single-step at the listed C-records."],
        [],
        ["1.", "Jog the STATION by hand to the angle named in the job file."],
        ["", "The job does NOT move the station. Scale and sense are confirmed,"],
        ["", "but emitted station motion and its collision scene are unsupported."],
        ["", "Set it yourself; check the pendant angle matches the file name."],
        ["2.", "Load the matching .JBI and single-step it."],
        ["3.", "At each stop measure from the CONTACT TIP straight down to the"],
        ["", "platter, and put that number in MEASURED gap mm."],
        ["4.", "Enter the independently verified contact-tip Z minus calibrated TCP Z in column N; positive is above the TCP."],
        [],
        ["CONTACT TIP AND TCP ARE DIFFERENT DATUMS"],
        ["Surface Z = commanded TCP Z + verified tip-minus-TCP Z - measured gap."],
        ["Leave the offset blank if unknown; the derived height then stays blank."],
        ["The 3.175 mm touch and 9.525 mm weld extensions are not automatically this offset."],
        ["Raw gaps and notes can still be collected while the reference is unresolved."],
        [],
        ["WHAT COMES BACK OUT OF IT"],
        ["Station 0 points give the surface: tilt, dish and any cantilever sag"],
        ["at the unsupported end. If the numbers agree within a millimetre or"],
        ["two, say so and the single scalar stands. IF THEY DO NOT, SAY THAT"],
        ["TOO rather than averaging it away; retain all individual measurements."],
        ["Only independently referenced heights establish an absolute surface."],
        [],
        ["This mapping job is limited to station zero."],
        ["Read the existing C1-C3 robot/station calibration separately to locate"],
        ["the axis and determine its rotation sense."],
    ]:
        how.append(line)
    how.column_dimensions["A"].width = 55
    how.column_dimensions["B"].width = 78
    how["A1"].font = Font(bold=True, size=13)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--by", required=True,
                    help="who attested the platter (emit refuses without it)")
    ap.add_argument("--nx", type=int, default=5, help="grid columns at station 0")
    ap.add_argument("--ny", type=int, default=3, help="grid rows at station 0")
    ap.add_argument("--angles", default="0",
                    help="station angles, degrees. Only 0 can be emitted "
                         "today - see the refusal message for why")
    ap.add_argument("--standoff", type=float, default=STANDOFF_MM)
    ap.add_argument("--training-block", action="store_true",
                    help="the ManPro training block is on the platter")
    ap.add_argument("--fixture", default=None)
    args = ap.parse_args(argv)

    try:
        angles = [float(a) for a in args.angles.split(",") if a.strip()]
    except ValueError:
        return int(bool(print("--angles must be numbers, e.g. 0,15,-15")))
    if not angles:
        return int(bool(print("give at least one station angle")))

    torch = TorchModel.yaskawa_accufast_45()
    out_dir = ROOT / "docs" / "jobs-out"
    when = datetime.now().strftime("%Y/%m/%d %H:%M")
    rows: list[dict] = []
    written: list[Path] = []

    full = grid_points(args.nx, args.ny)


    corners = [full[0], full[len(full) // 2], full[-1]]

    for theta in angles:
        if theta != 0.0:


            print(f"  station {theta:+.0f} deg: REFUSED - the clearance gate "
                  "cannot see a turned platter yet")
            print("      (clearance.check takes station_transform; emit_job "
                  "and lc_scene do not)")
            continue
        pts = full if theta == angles[0] else corners


        name = f"CALIB-S{'M' if theta < 0 else 'P'}{abs(int(round(theta))):03d}"
        placed = rotated_points(pts, theta, np.array([
            cell_lc.DEFAULT_PART_ORIGIN[0], cell_lc.DEFAULT_PART_ORIGIN[1],
            cell_lc.platter_work_surface_z(0.0)]))
        tp = probe_toolpath(placed, args.standoff)


        origin = np.array([cell_lc.DEFAULT_PART_ORIGIN[0],
                           cell_lc.DEFAULT_PART_ORIGIN[1],
                           cell_lc.platter_work_surface_z(0.0)])
        scene = cell_lc.lc_scene(part_origin_robot=origin,
                                 training_block=args.training_block)
        platter = PlatterState(clear=True, training_block_on=args.training_block,
                               fixture=args.fixture, attested_by=args.by)
        try:
            text, summary = emit_job(
                tp, cell_lc.pulse_solver(origin),
                name=name, arc_file=1, scene=scene, torch=torch, date=when,

                bead_height_mm=1.0,
                margin_mm=5.0, hover_mm=HOVER_MM, allow_no_interpass=True,
                platter=platter,
                comments=(
                    "PLATTER MAP - NO ARC",
                    f"station {theta:+.0f} deg BY HAND",
                    "TEACH mode, slow, step",
                ),


                provenance=cell_lc.calibration_summary() + (
                    "PROBE ONLY - NO ARC IN THIS JOB",
                    "the ASF# above is INERT here",
                    f"station {theta:+.0f}deg SET BY HAND",
                    f"standoff {args.standoff:.0f}mm over pred face",
                ),
            )
        except EmitRefused as exc:
            print(f"  {name}: REFUSED - {exc}")
            continue


        if "ARCON" in text or "ARCOF" in text:
            print(f"  {name}: REFUSING TO WRITE - an arc instruction appeared "
                  "in a probe job")
            continue

        path = write_job(text, out_dir / f"{name}.JBI")
        written.append(path)
        print(f"  wrote {path.name}  ({summary['positions']} positions, "
              f"{len(pts)} measurement points, station {theta:+.0f} deg)")

        for k, ((px, py), (mx, my, mz)) in enumerate(zip(pts, placed), start=1):
            robot = np.array([mx, my, mz + args.standoff]) + origin
            rows.append({
                "point": f"{name[6:]}-{k:02d}", "job": path.name,
                "record": f"C{3 * k:05d}",
                "theta": round(theta, 1),
                "px": round(px, 1), "py": round(py, 1),
                "rx": round(float(robot[0]), 2), "ry": round(float(robot[1]), 2),
                "rz": round(float(robot[2]), 2),
                "face": round(float(cell_lc.platter_work_surface_z(0.0)), 2),
                "standoff": round(args.standoff, 1),
            })

    if not written:
        print("nothing was written - every job was refused above")
        return 1

    book = build_workbook(rows, out_dir / "platter-map.xlsx", {
        "when": when,
        "commit": cell_lc.calibration_summary()[0] if cell_lc.calibration_summary() else "",
        "face_z": round(float(cell_lc.platter_work_surface_z(0.0)), 2),
        "offset": cell_lc.PLATTER_FACE_OFFSET_MM,
        "standoff": args.standoff,
    })
    print(f"  wrote {book}  ({len(rows)} points to measure)")
    print("\n  Run the station-zero map in TEACH mode, slow, one step per point.")
    print("  Keep the station at zero; this pipeline cannot emit or validate")
    print("  station motion (use the existing C1/C2/C3 evidence for axis/sense).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
