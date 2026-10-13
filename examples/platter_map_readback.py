'Fit a platter map from plan.json and VAR.DAT exports in job-number order.'


from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import platter_map, var_dat


def _plot(plan, summary: dict, path: Path) -> None:
    points = summary["points"]
    xyz = np.array([[row["x"], row["y"], row["z"]] for row in points])

    normal = plan.basis[:, 2]
    xyz[:, 2] = (plan.origin[2]
                 - (xyz[:, :2] - plan.origin[:2]) @ normal[:2] / normal[2])
    uv = (xyz - plan.origin) @ plan.basis
    residuals = np.array([row["residual"] for row in points])
    limit = max(float(np.max(np.abs(residuals))), 0.001)
    fig, ax = plt.subplots(figsize=(9, 7), constrained_layout=True)
    scatter = ax.scatter(uv[:, 0], uv[:, 1], c=residuals, cmap="coolwarm",
                         vmin=-limit, vmax=limit, s=35)
    for hole in plan.holes:
        center = (np.asarray(hole["center"]) - plan.origin) @ plan.basis
        ax.add_patch(Circle(center[:2], hole["radius"], fill=False, color="black"))
    ties = (plan.tie_points - plan.origin) @ plan.basis
    ax.scatter(ties[:, 0], ties[:, 1], marker="*", s=180, facecolors="none",
               edgecolors="black", linewidths=1.4, label="Tie stations")
    quad = (plan.corners - plan.origin) @ plan.basis
    boundary = np.vstack((quad, quad[0]))
    ax.plot(boundary[:, 0], boundary[:, 1], color="0.5", linewidth=0.8)
    ax.set(xlabel="Corner-plane u (mm)", ylabel="Corner-plane v (mm)",
           title=f"Platter-map residuals | RMS {summary['rms_mm']:.3f} mm")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="best")
    fig.colorbar(scatter, ax=ax, label="Fit residual (mm)")
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path, help="plan.json written by gen_platter_map.py")
    ap.add_argument("exports", nargs="+", type=Path, help="VAR_PLATMAP<j>.DAT in job-number order")
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    ap.add_argument("--degree", type=int, default=2, help="surface polynomial degree (default 2)")
    args = ap.parse_args()
    try:
        plan = platter_map.load_plan(json.loads(args.plan.read_text(encoding="utf-8")))
        if len(args.exports) != len(plan.jobs):
            raise ValueError(f"expected {len(plan.jobs)} exports in job order; got {len(args.exports)}")
        records = []
        for j, (path, probes) in enumerate(zip(args.exports, plan.jobs), 1):
            try:
                records.append(var_dat.probe_records(path, 44, len(probes)))
            except ValueError as exc:
                raise ValueError(f"job {j} ({path}): {exc}") from exc
        summary = platter_map.fit_map(plan, records, degree=args.degree)
        args.out.mkdir(parents=True, exist_ok=True)
        with (args.out / "map.csv").open("w", newline="", encoding="utf-8") as stream:
            fields = ("job", "pvar", "x", "y", "z", "residual")
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in summary["points"]:
                writer.writerow({**row, "pvar": f"P{row['pvar']:03d}"})
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        _plot(plan, summary, args.out / "residuals.png")
    except (ValueError, OSError) as exc:
        print(f"REFUSED: {exc}")
        return 1

    print(f"Plane tilt: {summary['plane_tilt_deg']:.4f} deg "
          f"at {summary['tilt_direction_deg']:.2f} deg")
    print(f"Flatness peak-to-valley: {summary['flatness_pv_mm']:.4f} mm")
    print(f"Fit residual: RMS {summary['rms_mm']:.4f} mm, "
          f"max |residual| {summary['max_abs_mm']:.4f} mm")
    print(f"Drift: {summary['drift_mm_per_touch']:.5f} mm/touch")
    print(f"Tie consistency: {summary['tie_consistency_mm']:.4f} mm")
    print(f"Drift-corrected tie B-A by job (mm): {summary['tie_differences_mm']}")
    print(f"Job offsets (mm): {summary['job_offsets_mm']}")
    print(f"wrote map.csv, summary.json and residuals.png to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
