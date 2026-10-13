from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.backends.ros2 import ArcIo
from waam_pipeline.clearance import TorchModel
from waam_pipeline.emit import EmitRefused, emit_ros2
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
ARC_IO_PATH = ROOT / "examples" / "arc_io_lc50s.json"


HOLD_AXES: tuple[str, ...] = ()

_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--out", default=None,
                 help="output directory (default docs/jobs-out)")
_args, _ = _ap.parse_known_args()

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
from waam_pipeline.emit import platter_from_cli
try:
    PLATTER = platter_from_cli()
except EmitRefused as exc:
    sys.exit(f"refusing to emit: {exc}")
scene = cell_lc.lc_scene(part_origin_robot=origin, training_block=PLATTER.training_block_on)

arc_io = ArcIo.load(ARC_IO_PATH) if ARC_IO_PATH.exists() else None
weld = bool(arc_io and arc_io.verified and not arc_io.validation_errors())
if not weld:
    print(f"arc I/O: not armed ({ARC_IO_PATH.name} is a template) - air moves only")

try:
    job, summary = emit_ros2(
        tp, cell_lc.pulse_solver(origin),
        name=JOB_NAME, platter=PLATTER, scene=scene, torch=torch,
        arc_io=arc_io, weld=weld, bead_height_mm=LAYER_H_MM,
        air_v=AIR_V,
        hover_mm=HOVER_MM, interpass_timer_s=INTERPASS_S,
        hold_axes=HOLD_AXES,
    )
except EmitRefused as exc:
    sys.exit(f"refusing to emit: {exc}")

out_dir = Path(_args.out) if _args.out else ROOT / "docs" / "jobs-out"
out_dir.mkdir(parents=True, exist_ok=True)
out_json = out_dir / f"{JOB_NAME}.ros2.json"
out_json.write_text(job.to_json(), encoding="utf-8")
out_yaml = out_dir / f"{JOB_NAME}.seg01.yaml"
out_yaml.write_text(job.goal_yaml(0), encoding="utf-8")

print(f"wrote {out_json}")
print(f"      {out_yaml}   (segment 1 of {len(job.segments)}, for "
      "`ros2 action send_goal`)")
print("  " + ", ".join(f"{k}={v}" for k, v in summary.items()))
for n in job.notes:
    print(f"  note: {n}")
print(f"\n  python3 ros2/waam_ros2_runner.py {out_json.name} --dry-run")


import math

from waam_pipeline.backends.jbi import PULSES_PER_DEG
from waam_pipeline.emit import emit_job


COMPARE_ONLY_ASF = 1
jbi_text, _ = emit_job(tp, cell_lc.pulse_solver(origin), name=JOB_NAME,
                       arc_file=COMPARE_ONLY_ASF,
                       platter=PLATTER, scene=scene, torch=torch,
                       date="2026/01/01 00:00", air_v=AIR_V,
                       bead_height_mm=LAYER_H_MM,
                       hover_mm=HOVER_MM, interpass_timer_s=INTERPASS_S)
jbi_deg = [
    [int(v) / r for v, r in zip(line.split("=", 1)[1].split(","), PULSES_PER_DEG)]
    for line in jbi_text.splitlines() if line.startswith("C000")
]
ros_deg, seen = [], set()
for seg in job.segments:
    for p in seg.points:
        q = tuple(round(math.degrees(a), 6) for a in p.positions_rad)
        if q not in seen:
            seen.add(q)
            ros_deg.append(q)
worst = max(
    abs(a - b)
    for qa, qb in zip(jbi_deg, ros_deg) for a, b in zip(qa, qb)
)
print(f"\nback-end consistency AT ONE ORIGIN: {len(jbi_deg)} .JBI pulse records "
      f"vs {len(ros_deg)} ROS trajectory poses, worst axis difference "
      f"{worst:.4f} deg (pulse quantisation only)")
print("  NOTE: both sides came from this run's own origin, so this says "
      "nothing about whether the two SCRIPTS seat the job at the same "
      "height. That is what cell_lc.seat_job is for; the work-surface line "
      "printed above is the one to compare with gen_wall_jbi.py.")
