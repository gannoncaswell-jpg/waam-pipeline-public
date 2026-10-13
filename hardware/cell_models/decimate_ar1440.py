from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "robots" / "meshes" / "ar1440" / "collision"
OUT = REPO / "robots" / "meshes" / "ar1440" / "decimated"

BUDGET = 1200
GRID_MM = 12.0
GRID_MAX = 60.0
BBOX_TOL_MM = 20.0
MESH_UNIT_MM = 1000.0


def _stl():
    try:
        from waam_pipeline.slicer import load_stl, write_stl
    except ImportError:
        sys.path.insert(0, str(REPO / "src"))
        from waam_pipeline.slicer import load_stl, write_stl
    return load_stl, write_stl


def cluster_decimate(triangles: np.ndarray, grid_mm: float,
                     unit_mm: float = MESH_UNIT_MM) -> np.ndarray:


    tris = np.asarray(triangles, float)
    grid = grid_mm / unit_mm
    verts = tris.reshape(-1, 3)
    origin = verts.min(axis=0)
    cells = np.floor((verts - origin) / grid).astype(np.int64)
    keys, inverse = np.unique(cells, axis=0, return_inverse=True)
    reps = np.zeros((len(keys), 3), dtype=float)
    counts = np.bincount(inverse, minlength=len(keys)).astype(float)
    for axis in range(3):
        reps[:, axis] = np.bincount(inverse, weights=verts[:, axis],
                                    minlength=len(keys)) / counts
    faces = inverse.reshape(-1, 3)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    faces = faces[keep]
    if len(faces) == 0:
        raise ValueError(f"grid {grid_mm} mm collapsed the whole mesh")
    _, first = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
    faces = faces[np.sort(first)]
    return reps[faces]


def decimate_to_budget(triangles: np.ndarray, budget: int = BUDGET,
                       grid_mm: float = GRID_MM, grid_max: float = GRID_MAX,
                       unit_mm: float = MESH_UNIT_MM) -> tuple[np.ndarray, float]:

    grid = grid_mm
    out = cluster_decimate(triangles, grid, unit_mm)
    while len(out) > budget and grid < grid_max:
        grid = min(grid * 1.25, grid_max)
        out = cluster_decimate(triangles, grid, unit_mm)
    if len(out) > budget:
        raise ValueError(f"cannot reach {budget} triangles by {grid_max} mm grid")
    return out, grid


def bbox_error_mm(source: np.ndarray, decimated: np.ndarray,
                  unit_mm: float = MESH_UNIT_MM) -> float:

    a, b = np.asarray(source, float).reshape(-1, 3), np.asarray(decimated, float).reshape(-1, 3)
    return unit_mm * float(max(np.abs(a.min(axis=0) - b.min(axis=0)).max(),
                               np.abs(a.max(axis=0) - b.max(axis=0)).max()))


def main() -> int:
    load_stl, write_stl = _stl()
    OUT.mkdir(parents=True, exist_ok=True)
    sources = sorted(SRC.glob("*.stl"))
    if not sources:
        raise SystemExit(f"no collision meshes in {SRC}")
    worst = 0.0
    for path in sources:
        src = load_stl(path)
        dec, grid = decimate_to_budget(src)
        err = bbox_error_mm(src, dec)
        worst = max(worst, err)
        out = OUT / f"{path.stem}_dec.stl"
        write_stl(dec, out, path.stem.encode()[:80])
        print(f"{path.name:24s} {len(src):5d} -> {len(dec):4d} tris  "
              f"grid {grid:4.1f} mm  bbox err {err:4.1f} mm  -> {out.name}")
        if err > BBOX_TOL_MM:
            raise SystemExit(f"{out.name}: bbox error {err:.1f} mm exceeds {BBOX_TOL_MM}")
    print(f"worst bbox error {worst:.1f} mm (tolerance {BBOX_TOL_MM:.0f} mm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
