from __future__ import annotations

import argparse
from pathlib import Path

from . import parse
from .backends.jbi import emit_inform_preview
from .clearance import CONTACT_OK, Scene, TorchModel, blocking, check, plot
from .frames import load_cell_config


def main() -> int:
    ap = argparse.ArgumentParser(prog="waam_pipeline")
    ap.add_argument("gcode", type=Path)
    ap.add_argument("--cell", type=Path, help="cell config JSON; enables the controller preview")
    ap.add_argument("--arc-on", help="explicit arc-on command (e.g. G82) for non-E dialects")
    ap.add_argument("--arc-off", help="explicit arc-off command (e.g. G83)")
    ap.add_argument("--check", action="store_true", help="run the torch-clearance check")
    ap.add_argument("--scene", type=Path, help="static obstacles JSON (fixture, clamps)")
    ap.add_argument("--bead-width", type=float, default=9.0, help="bead width mm (default 9)")
    ap.add_argument("--bead-height", type=float, default=3.0,
                    help="bead height mm = the layer height the G-code was sliced at (default 3)")
    ap.add_argument("--margin", type=float, default=3.0, help="required clearance mm (default 3)")
    ap.add_argument("--plot", type=Path, help="save a 3D toolpath PNG (implies --check)")
    ap.add_argument("--html", type=Path, help="save an interactive 3D viewer HTML (implies --check)")
    ap.add_argument("--studio", type=Path, help="save the WAAM Studio front end HTML (implies --check)")
    args = ap.parse_args()

    toolpath = parse(
        args.gcode.read_text(encoding="utf-8"), arc_on=args.arc_on, arc_off=args.arc_off
    )
    print(toolpath.summary())
    if toolpath.unhandled:
        shown = ", ".join(f"L{n}:{c}" for n, c in toolpath.unhandled[:8])
        more = "" if len(toolpath.unhandled) <= 8 else f" (+{len(toolpath.unhandled) - 8} more)"
        print(f"unhandled (review): {shown}{more}")

    violations = []
    blockers = []
    scene = Scene.load(args.scene) if args.scene else None
    if args.check or args.plot or args.html or args.studio:
        violations = check(
            toolpath,


            TorchModel.yaskawa_accufast_45(),
            scene,
            bead_width_mm=args.bead_width,
            bead_height_mm=args.bead_height,
            margin_mm=args.margin,


            contact_ok=CONTACT_OK,
        )
        blockers = blocking(violations)


        blocker_ids = {id(v) for v in blockers}
        notes = [v for v in violations if id(v) not in blocker_ids]
        if blockers:
            print(f"CLEARANCE: {len(blockers)} blocking violation(s), margin {args.margin} mm:")
            for v in blockers[:20]:
                print(
                    f"  move {v.move_index} (gcode L{v.gcode_line}) at {v.point}: "
                    f"{v.clearance_mm} mm to {v.obstacle}"
                )
            if len(blockers) > 20:
                print(f"  ... +{len(blockers) - 20} more")
        else:
            print(f"CLEARANCE: clean (margin {args.margin} mm)")
        if notes:
            print(
                f"  {len(notes)} proximity note(s) (forgiven: contact with the "
                f"work plate and the growing bead)"
            )
        if args.plot:
            print(f"plot saved: {plot(toolpath, violations, args.plot)}")
        if args.html:
            from .viewer import write_html

            print(f"viewer saved: {write_html(toolpath, violations, args.html, args.gcode.stem)}")
        if args.studio:
            from .studio import build_state, write_studio

            state = build_state(
                toolpath,
                title=args.gcode.stem,
                violations=violations,
                scene=scene,
                cell_name=args.cell.stem if args.cell else None,
            )
            print(f"studio saved: {write_studio(state, args.studio)}")

    if args.cell:
        print()
        cfg = load_cell_config(args.cell)
        print(emit_inform_preview(toolpath, cfg))


    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
