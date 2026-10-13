"Import the hash-pinned CAD_FIXTURE_001.STL into the cell catalog."
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.slicer import load_stl
from cell_models import WAAM_FIXTURE_SIZE, WAAM_FIXTURE_SOURCE_SHA256


def import_fixture(source: Path, destination: Path) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != WAAM_FIXTURE_SOURCE_SHA256:
        raise ValueError("Fixture source differs from the reviewed CAD_FIXTURE_001.STL")
    native = load_stl(source)


    mesh = native[:, :, [0, 2, 1]] * np.array([1.0, 1.0, -1.0])
    pts = mesh.reshape(-1, 3)
    lo, hi = pts.min(0), pts.max(0)
    np.testing.assert_allclose(hi - lo, WAAM_FIXTURE_SIZE, atol=0.001)
    mesh -= np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    destination.parent.mkdir(parents=True, exist_ok=True)
    np.save(destination, mesh.astype(np.float32), allow_pickle=False)
    destination.with_suffix(".json").write_text(json.dumps({
        "source": "CAD_FIXTURE_001.STL",
        "source_sha256": digest,
        "mesh_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "triangles": len(mesh),
        "units": "mm; verified against the 20 x 10 inch drawing, STL is unitless",
        "frame": "X along 508 mm length, Y across 254 mm width, Z up; underside Z=0; XY centered",
        "native_to_local_axes": "[X, Z, -Y], then center XY and set underside Z=0; raised bars above plate per drawing rev 1",
        "size_mm": list(WAAM_FIXTURE_SIZE),
        "registration": "unverified; reference asset only, absent from the default live scene",
    }, indent=2) + "\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).parent / "meshes" / "waam_fixture.npy")
    args = parser.parse_args()
    print(import_fixture(args.source, args.out))
