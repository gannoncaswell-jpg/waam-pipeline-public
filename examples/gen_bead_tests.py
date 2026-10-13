from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.clearance import CONTACT_OK, TorchModel, blocking, check
from waam_pipeline.demos import demo_bead
from waam_pipeline.emit import EmitRefused, emit_job, platter_from_cli, write_job


BEAD_LEN_MM = 100.0
LAYER_H_MM = 2.0
SUBSTRATE_MM = 12.7
ORIGIN_X = 1014.0
ORIGIN_Y_FIRST = -140.0
BEAD_PITCH_MM = 20.0
WELD_MM_MIN = 833.0


AIR_V = "333.3"
HOVER_MM = 50.0


ASF_FILES = [
    (2,  "WFS125/Trim100 ManPro"),
    (13, "WFS150/Trim100"),
    (66, "WFS180/Trim150"),
    (12, "WFS200/Trim90-100"),
    (65, "WFS260/Trim102"),
]


_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--out", default=None, help="output directory (default docs/jobs-out)")
_args, _ = _ap.parse_known_args()


try:
    PLATTER = platter_from_cli()
except EmitRefused as exc:
    sys.exit(f"refusing to emit: {exc}")

torch = TorchModel.yaskawa_accufast_45()
out_dir = Path(_args.out) if _args.out else ROOT / "docs" / "jobs-out"
out_dir.mkdir(parents=True, exist_ok=True)

wrote = []
for i, (asf, note) in enumerate(ASF_FILES):
    y = ORIGIN_Y_FIRST + i * BEAD_PITCH_MM
    name = f"BEAD-ASF{asf:02d}"
    tp = demo_bead(BEAD_LEN_MM, LAYER_H_MM, feed_mm_min=WELD_MM_MIN)
    try:
        origin, surface = cell_lc.seat_job(tp, ORIGIN_X, y, substrate_mm=SUBSTRATE_MM)
    except ValueError as exc:
        sys.exit(f"{name}: refusing to emit: {exc}")
    scene = cell_lc.lc_scene(part_origin_robot=origin,
                             training_block=PLATTER.training_block_on)
    try:
        jbi, summary = emit_job(
            tp, cell_lc.pulse_solver(origin),
            name=name, arc_file=asf, platter=PLATTER, scene=scene, torch=torch,
            date=datetime.now().strftime("%Y/%m/%d %H:%M"),
            bead_height_mm=LAYER_H_MM,
            margin_mm=5.0, air_v=AIR_V, hover_mm=HOVER_MM,
            comments=(
                "WAAM bead-param test",
                f"bead {i + 1}/{len(ASF_FILES)} y={y:.0f} same V",
                f"ASF#{asf} {note}",
            ),
            provenance=cell_lc.calibration_summary() + (
                f"bead test ASF#{asf} {note}",
                f"substrate {SUBSTRATE_MM:g}mm 1/2in CTWD 3/8in",
            ),
        )
    except EmitRefused as exc:

        for v in sorted(blocking(check(tp, torch, scene, bead_height_mm=LAYER_H_MM,
                                       margin_mm=5.0, contact_ok=CONTACT_OK)),
                        key=lambda v: v.clearance_mm)[:10]:
            print(f"  CLEARANCE: move {v.move_index} at {v.point}: "
                  f"{v.clearance_mm} mm to {v.obstacle}")
        sys.exit(f"{name}: refusing to emit: {exc}")
    path = write_job(jbi, out_dir / f"{name}.JBI")
    wrote.append(name)
    print(f"wrote {path.name}  y={y:+.0f}  ASF#{asf}  ({note})  "
          f"{summary['positions']} pos, {summary['deposit_mm']:.0f} mm bead, "
          f"{summary['violations']} proximity note(s)")

print(f"\n{len(wrote)} bead jobs in {out_dir.relative_to(ROOT)}: {', '.join(wrote)}")
print(f"Constant travel speed {WELD_MM_MIN:g} mm/min, substrate {SUBSTRATE_MM:g} mm, "
      f"beads {BEAD_PITCH_MM:g} mm apart in Y. Run in order; pull arcmon after each.")
