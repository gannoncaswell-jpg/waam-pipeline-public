'Build an LC bead-width worksheet, analyse measurements, or export ONE bead.'


from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.bead_test import (TestSpec, analyse_measurements, emit_trial,
                                     measurement_csv, render_report, trials)
from waam_pipeline.emit import EmitRefused, PlatterState, write_job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="bead-test JSON plan")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--measurements", type=Path, help="completed or partial measurement CSV")
    parser.add_argument("--trial", help="export one trial, such as B01")
    parser.add_argument("--cell", type=Path, help="verified LC cell profile, required for export")
    parser.add_argument("--platter-clear", action="store_true", help="attest the platter was just checked")
    parser.add_argument("--block", choices=("on", "off"), help="current training-block state")
    parser.add_argument("--fixture", help="registered fixture name, if present")
    parser.add_argument("--by", help="person who checked the platter")
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise ValueError("Choose a new output directory; existing test records are preserved")
        spec = TestSpec.load(args.plan)
        csv_text = args.measurements.read_text(encoding="utf-8-sig") if args.measurements else measurement_csv(spec)
        measured = analyse_measurements(spec, csv_text) if args.measurements else None
        job, summary = None, None
        if args.trial:
            if args.measurements:
                raise ValueError("Analyse measurements and export jobs in separate commands")
            if not args.cell or not args.platter_clear or args.block is None or not (args.by or "").strip():
                raise EmitRefused("attestation", "Export requires --cell, --platter-clear, --block on|off and --by NAME")
            job, summary = emit_trial(spec, args.trial, cell_profile=args.cell,
                                      platter=PlatterState(True, args.block == "on", args.fixture, args.by))
        elif any((args.cell, args.platter_clear, args.block, args.fixture, args.by)):
            raise ValueError("Cell and platter arguments apply only to --trial export")


        manifest = dict(schema=2, plan=spec.fingerprint, cell="LC50S / AR1440",
                        generated_utc=datetime.now(timezone.utc).isoformat(),
                        planning_only=spec.planning_only,
                        nominal_bead_height=spec.nominal_bead_height_label,
                        trials=[{**asdict(t), "nominal_bead_height": spec.nominal_bead_height_label}
                                for t in trials(spec)],
                        export=summary)
        payloads = {"plan.json": json.dumps(asdict(spec), indent=2, allow_nan=False) + "\n",
                    "manifest.json": json.dumps(manifest, indent=2, allow_nan=False) + "\n",
                    "measurements.csv": csv_text, "worksheet.html": render_report(spec, measured)}
        if measured is not None:
            payloads["results.json"] = json.dumps(measured, indent=2, allow_nan=False) + "\n"
        args.out.mkdir(parents=True, exist_ok=False)
        for name, text in payloads.items():
            (args.out / name).write_text(text, encoding="utf-8", newline="")
        if job is not None:
            write_job(job, args.out / f"{spec.name}-{args.trial}.JBI")
        print(f"Test pack: {args.out.resolve()}")
        print("One checked bead job exported." if job else "Worksheet saved. No robot job exported.")
        return 0
    except (OSError, ValueError, TypeError, EmitRefused) as exc:
        print(f"Test pack refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
