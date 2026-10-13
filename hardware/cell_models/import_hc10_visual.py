from pathlib import Path
import sys

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.robots.hc10 import _MESH_LINKS
from waam_pipeline.slicer import load_stl

MESHES = ROOT / "robots" / "meshes"


def main() -> None:
    for link, (coll_rel, _offset) in _MESH_LINKS.items():
        dae = MESHES / coll_rel.replace("/collision/", "/visual/").replace(".stl", ".dae")
        out = dae.with_suffix(".stl")
        mesh = trimesh.load(str(dae), force="mesh")
        col = load_stl(str(MESHES / coll_rel)).reshape(-1, 3)
        drift = max(float(np.max(np.abs(mesh.bounds[0] - col.min(0)))),
                    float(np.max(np.abs(mesh.bounds[1] - col.max(0)))))
        if drift > 0.03:
            raise SystemExit(f"{link}: visual/collision bounds differ by {drift * 1000:.0f} mm "
                             "-- check the COLLADA unit/axis before trusting it")
        mesh.export(str(out))
        print(f"{link}: {len(mesh.faces)} faces -> {out.relative_to(ROOT)} (drift {drift * 1000:.1f} mm)")


if __name__ == "__main__":
    main()
