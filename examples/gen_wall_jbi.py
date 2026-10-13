from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc, weld_ctwd
from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking
from waam_pipeline.emit import EmitRefused, emit_job, platter_from_cli, write_job
from waam_pipeline.slicer import wall


JOB_NAME = "WAAM-WALL1"
WALL_LEN_MM = 150.0
LAYERS = 6
LAYER_H_MM = 2.0
SUBSTRATE_MM = 6.35
ORIGIN_X = 1014.0
ORIGIN_Y = -180.0


WELD_MM_MIN = 833.0


AIR_V = "333.3"
INTERPASS_S = 30.0


HOVER_MM = 50.0


FIRST_LAYER_GAP_MM = 0.0


_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--cell", default=None,
                 help="cell profile the ASF# comes from "
                      "(default examples/cell_lc50s.json)")
_ap.add_argument("--out", default=None,
                 help="output directory (default docs/jobs-out)")
_ap.add_argument("--weld-ctwd", type=float, default=None,
                 help="weld CTWD in mm (default: the calibrated weld stick-out; "
                      "see docs/weld-ctwd.md)")
_ap.add_argument("--clean-first", action="store_true",
                 help="call CLEAN_R1 before approaching the first weld")
_args, _ = _ap.parse_known_args()


try:
    ARC_FILE = cell_lc.arc_file(_args.cell)
except ValueError as exc:
    sys.exit(f"refusing to emit: {exc}")

tp = wall(WALL_LEN_MM, LAYERS, LAYER_H_MM, feed_mm_min=WELD_MM_MIN)


try:
    origin, surface = cell_lc.seat_job(
        tp, ORIGIN_X, ORIGIN_Y,
        substrate_mm=SUBSTRATE_MM, gap_mm=FIRST_LAYER_GAP_MM,
    )
except ValueError as exc:
    sys.exit(f"refusing to emit: {exc}")
if surface is not None:
    print(f"work surface: {surface.summary()}")
    print(f"  measured plane + substrate {SUBSTRATE_MM}"
          f"  ->  part origin z = {origin[2]:.2f}")
else:
    print(f"work surface z = {cell_lc.platter_work_surface_z(SUBSTRATE_MM):.2f} "
          f"(face {cell_lc.PLATTER_FACE_Z_MODEL:.1f}"
          f"{cell_lc.PLATTER_FACE_OFFSET_MM:+.1f} + substrate {SUBSTRATE_MM})"
          f"  ->  part origin z = {origin[2]:.2f}")

torch = TorchModel.yaskawa_accufast_45()


try:
    PLATTER = platter_from_cli()
except EmitRefused as exc:
    sys.exit(f"refusing to emit: {exc}")
scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=PLATTER.training_block_on)
try:
    preview_tp, _, _, violations, _, _ = weld_ctwd.checked(
        tp, _args.weld_ctwd, clean_first=_args.clean_first,
        torch=torch, scene=scene, bead_height_mm=LAYER_H_MM,
        margin_mm=5.0, contact_ok=CONTACT_OK,
    )
except ValueError as exc:
    sys.exit(f"refusing to emit: {exc}")


out_dir = Path(_args.out) if _args.out else ROOT / "docs" / "jobs-out"
out_dir.mkdir(parents=True, exist_ok=True)
out_jbi = out_dir / f"{JOB_NAME}.JBI"
try:
    jbi, summary = emit_job(
        tp,
        cell_lc.pulse_solver(origin),
        name=JOB_NAME,
        arc_file=ARC_FILE,
        platter=PLATTER,
        scene=scene,
        torch=torch,
        date=datetime.now().strftime("%Y/%m/%d %H:%M"),
        bead_height_mm=LAYER_H_MM,
        margin_mm=5.0,
        air_v=AIR_V,
        hover_mm=HOVER_MM,
        interpass_timer_s=INTERPASS_S,
        weld_ctwd_mm=_args.weld_ctwd,
        clean_first=_args.clean_first,
        comments=(
            "WAAM wall - waam-pipeline",
            f"origin {ORIGIN_X:.0f},{ORIGIN_Y:.0f}",
            f"check ASF#{ARC_FILE} + dry run",
        ),


        provenance=cell_lc.calibration_summary() + (
            f"fixture {surface.name} tilt {surface.tilt_deg:.2f}deg" if surface
            else "fixture NONE (none recorded)",
            f"layer h {LAYER_H_MM} mm (UNMEASURED)",
        f"interpass {INTERPASS_S:.0f}s OPEN-LOOP",
        ),
    )
except EmitRefused as exc:


    for v in sorted(blocking(violations),
                    key=lambda v: v.clearance_mm)[:10]:
        print(f"CLEARANCE VIOLATION: move {v.move_index} at {v.point}: "
              f"{v.clearance_mm} mm to {v.obstacle}")
    sys.exit(f"refusing to emit: {exc}")

print(f"clearance: clean ({summary['violations']} proximity note(s) within tolerance)")
write_job(jbi, out_jbi)
print(f"wrote {out_jbi}  ({summary['positions']} positions, "
      f"{summary['passes']} weld passes, {summary['deposit_mm']:.0f} mm of bead)")


from waam_pipeline.studio import build_state, write_studio


state = build_state(
    preview_tp,
    title=f"{JOB_NAME} - LC50S wall job",
    scene=scene,
    torch=torch,
    violations=violations,
    robot=cell_lc.robot_shadow(preview_tp, part_origin_robot=origin),
    scene_meshes=cell_lc.lc_scene_meshes(part_origin_robot=origin),
    cell_name="ArcWorld LC50S",
)
print(f"studio: {write_studio(state, out_dir / 'wall1-shadow.html')}")
