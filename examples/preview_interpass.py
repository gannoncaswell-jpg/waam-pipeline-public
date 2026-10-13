'Make a self-contained, explicitly illustrative interpass planning preview.'


import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline.interpass import illustrative_profile, illustrative_goldak_profile, simulate
from waam_pipeline.slicer import wall
from waam_pipeline.studio import build_state, write_studio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--heat-model", choices=("gaussian", "goldak"), default="gaussian")
    parser.add_argument("--length", type=float)
    parser.add_argument("--layers", type=int)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    goldak = args.heat_model == "goldak"
    p = illustrative_goldak_profile() if goldak else illustrative_profile()
    length = args.length if args.length is not None else (12 if goldak else 60)
    layers = args.layers if args.layers is not None else (3 if goldak else 6)
    tp = wall(length, layers, p.layer_height_mm, p.deposition_mm_min)
    reports = {}
    for policy in ("adaptive", "continuous"):
        print(f"Calculating {args.heat_model} / {policy} ...", flush=True)
        reports[policy] = simulate(tp, p, policy=policy)
        state = build_state(tp, thermal=reports[policy],
                            title=f"Illustrative {args.heat_model} interpass model · {policy}")
        write_studio(state, args.output / f"{policy}.html")
    (args.output / "illustrative-profile.json").write_text(json.dumps(asdict(p), indent=2), encoding="utf-8")
    (args.output / "comparison.json").write_text(json.dumps(reports, indent=2, allow_nan=False), encoding="utf-8")
    for policy, result in reports.items():
        print(f"{policy}: {result['completed_passes']}/{result['total_passes']} passes; "
              f"{result['cooling_s']:.1f} s cooling; {result['status']}")


if __name__ == "__main__":
    main()
