'Small numerical sensitivity study, not material or process validation.'


import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline.interpass import _passes, illustrative_profile
from waam_pipeline.slicer import wall
from waam_pipeline.thermal import GoldakHistory, GoldakSettings, Property


def run():
    p = illustrative_profile(width_mm=4, height_mm=2, travel_mm_min=600, substrate_mm=2)
    g = GoldakSettings(2, 2, 2, 4, 1/3, 8000, 1400, 1500, 200000,
                       (Property(20, 40, 500), Property(4000, 40, 500)),
                       10, 8, 1, .002, 0, 0, 0)
    results = []
    for cell in (1., .5, .25):
        case = replace(p, heat_model="goldak", goldak=replace(g, cell_mm=cell),
                       sample_mm=cell, net_power_w=200)
        case.validate()
        h = GoldakHistory(case, _passes(wall(4, 1, 2), case), 0)
        for i in range(round(4/cell)):
            h.deposit(np.array([i*cell, 0., 2.]), np.array([(i+1)*cell, 0., 2.]),
                      i*cell*.1, cell*.1, 2)
        h.advance(.8)
        results.append(dict(cell_mm=cell, sample_mm=cell, cells=h.size,
                            probe_c=h.temperature((2, 0, 0), .8), peak_c=h.peak_c,
                            balance_error_j=h.diagnostics()["balance_error_j"]))
    return {"purpose": "Uncalibrated numerical sensitivity study", "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(run(), indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
