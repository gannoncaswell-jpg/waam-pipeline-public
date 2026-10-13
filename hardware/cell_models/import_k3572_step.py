'Build the lightweight K3572-1 feeder-bracket mesh from its vendor STEP.'


from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import numpy as np

from decimate_ar1440 import bbox_error_mm


REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "hardware" / "cell_models" / "meshes" / "k3572_1_step.npy"
EXPECTED_SHA256 = "33E0DA1E95825324F587D5984AACFB9CEA9C699C3AB315C4376FA88D3B7416BD"
EXPECTED_MIN = np.array([-119.90578, -38.1, -130.31216])
EXPECTED_MAX = np.array([117.24894, 263.525, 2.65684])
TARGET_FACES = 3_000
MAX_FACES = 4_000
BBOX_TOL_MM = 1.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    digest = hashlib.sha256(args.step.read_bytes()).hexdigest().upper()
    if digest != EXPECTED_SHA256:
        raise SystemExit(
            f"unexpected K3572-1 STEP hash {digest}; expected {EXPECTED_SHA256}"
        )

    try:
        import cadquery as cq
    except ImportError as exc:
        raise SystemExit("CadQuery is required to rebuild the bracket mesh") from exc

    model = cq.importers.importStep(str(args.step))
    if model.solids().size() != 1:
        raise SystemExit(f"expected one solid, found {model.solids().size()}")
    shape = model.val()
    bbox = shape.BoundingBox()
    actual_min = np.array([bbox.xmin, bbox.ymin, bbox.zmin])
    actual_max = np.array([bbox.xmax, bbox.ymax, bbox.zmax])
    if not (
        np.allclose(actual_min, EXPECTED_MIN, atol=0.001)
        and np.allclose(actual_max, EXPECTED_MAX, atol=0.001)
    ):
        raise SystemExit(
            f"unexpected CAD bbox {actual_min.tolist()}..{actual_max.tolist()}"
        )

    try:
        import trimesh
        import vtk
        from vtk.util.numpy_support import (
            numpy_to_vtk,
            numpy_to_vtkIdTypeArray,
            vtk_to_numpy,
        )
    except ImportError as exc:
        raise SystemExit("VTK and trimesh are required to rebuild the bracket mesh") from exc

    vertices, facets = shape.tessellate(0.35, 0.1)
    points = np.asarray([[v.x, v.y, v.z] for v in vertices], dtype=float)
    source = points[np.asarray(facets, dtype=np.int64)]
    source_mesh = trimesh.Trimesh(
        vertices=points, faces=np.asarray(facets, dtype=np.int64), process=True
    )
    if not source_mesh.is_watertight:
        raise SystemExit("CAD tessellation unexpectedly is not watertight")
    poly = vtk.vtkPolyData()
    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(numpy_to_vtk(np.asarray(source_mesh.vertices)))
    poly.SetPoints(vtk_points)
    cells = np.column_stack(
        (
            np.full(len(source_mesh.faces), 3, dtype=np.int64),
            np.asarray(source_mesh.faces, dtype=np.int64),
        )
    ).ravel()
    vtk_cells = vtk.vtkCellArray()
    vtk_cells.SetCells(len(source_mesh.faces), numpy_to_vtkIdTypeArray(cells))
    poly.SetPolys(vtk_cells)

    decimator = vtk.vtkQuadricDecimation()
    decimator.SetInputData(poly)
    decimator.SetTargetReduction(1.0 - TARGET_FACES / len(source_mesh.faces))
    decimator.VolumePreservationOn()
    decimator.Update()
    simplified = decimator.GetOutput()
    simple_points = vtk_to_numpy(simplified.GetPoints().GetData())
    simple_cells = vtk_to_numpy(simplified.GetPolys().GetData()).reshape(-1, 4)
    if not np.all(simple_cells[:, 0] == 3):
        raise SystemExit("VTK simplifier unexpectedly produced non-triangle faces")
    simple_mesh = trimesh.Trimesh(
        vertices=simple_points, faces=simple_cells[:, 1:], process=True
    )
    if not simple_mesh.is_watertight or simple_mesh.volume <= 0:
        raise SystemExit("simplified bracket mesh is not a closed outward solid")
    decimated = np.asarray(simple_mesh.triangles, dtype=float)
    if len(decimated) > MAX_FACES:
        raise SystemExit(
            f"simplifier retained {len(decimated)} faces; maximum is {MAX_FACES}"
        )
    error = bbox_error_mm(source, decimated, unit_mm=1.0)
    if error > BBOX_TOL_MM:
        raise SystemExit(f"decimated bbox error {error:.3f} exceeds {BBOX_TOL_MM} mm")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, decimated.astype(np.float32))
    print(
        f"K3572-1: {len(source)} -> {len(decimated)} triangles; "
        f"bbox error {error:.3f} mm; SHA256 {digest}; wrote {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
