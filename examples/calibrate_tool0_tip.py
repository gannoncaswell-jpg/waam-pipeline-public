'Fit the TOOL 0 contact-tip offset from recorded fixed-endpoint poses.'


from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.tool0_calibration import fit_tip_offset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path, help="JSON file of measured fixed-endpoint captures")
    parser.add_argument("--out", type=Path, help="new output JSON path; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.captures.stat().st_size > 65536:
            raise ValueError("Calibration input must be at most 64 KB")
        result = fit_tip_offset(json.loads(args.captures.read_text(encoding="utf-8-sig")))
        rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as destination:
                destination.write(rendered)
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Calibration refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
