'Rebuild the K3560-1 display asset from the hash-pinned, supplied STEP.'


from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys

import numpy as np

SOURCE_SHA256 = "7a37c771341028ed812f9d1247203e8a6e17bc86b117dec15fbb987f11d93f99"
MESH_DIR = Path(__file__).resolve().parent / "meshes"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cad-runtime", type=Path)
    parser.add_argument("--out-dir", type=Path, default=MESH_DIR)
    args = parser.parse_args()
    if hashlib.sha256(args.source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Different STEP: review its identity, units and mounting fit first")
    if args.cad_runtime:
        sys.path.insert(0, str(args.cad_runtime))

    from OCP.BRep import BRep_Tool
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf, XCAFDoc_ColorGen
    from OCP.collections import Sequence_TDF_Label

    metadata = json.loads((MESH_DIR / "k3560_1_step.json").read_text(encoding="utf-8"))
    document = TDocStd_Document(TCollection_ExtendedString("K3560-1"))
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    if reader.ReadFile(str(args.source)) != IFSelect_RetDone or not reader.Transfer(document):
        raise ValueError("STEP import failed")
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    colors = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    labels = Sequence_TDF_Label()
    shapes.GetFreeShapes(labels)

    def explore(shape, kind):
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            yield explorer.Current()
            explorer.Next()

    def rgb(shape, fallback=(.735357, .735357, .735357)):
        color = Quantity_Color()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if colors.GetColor(shape, kind, color):
                return tuple(round(c, 6) for c in color.Values(Quantity_TOC_sRGB))
        return fallback

    solids = [TopoDS.Solid(s) for i in range(1, labels.Length() + 1)
              for s in explore(shapes.GetShape_s(labels.Value(i)), TopAbs_SOLID)]
    if labels.Length() != 1 or len(solids) != 21:
        raise ValueError("Unexpected source assembly structure")
    settings = metadata["tessellation"]
    chunks = {}
    removed_count = 0
    zero_count = 0
    removed_area = 0.0
    for solid_id, solid in enumerate(solids):
        if not BRepCheck_Analyzer(solid).IsValid():
            raise ValueError(f"Invalid B-rep solid {solid_id}")
        mesher = BRepMesh_IncrementalMesh(solid, settings["linear_deflection_mm"],
                                        False, settings["angular_deflection_rad"], True)
        if not mesher.IsDone():
            raise ValueError(f"Tessellation failed for solid {solid_id}")
        base_color = rgb(solid)
        for shape in explore(solid, TopAbs_FACE):
            face = TopoDS.Face(shape)
            location = TopLoc_Location()
            mesh = BRep_Tool.Triangulation_s(face, location)
            if mesh is None:
                raise ValueError(f"Missing tessellation in solid {solid_id}")
            transform = location.Transformation()
            nodes = [mesh.Node(i).Transformed(transform) for i in range(1, mesh.NbNodes() + 1)]
            vertices = np.array([[p.X(), p.Y(), p.Z()] for p in nodes], dtype=np.float64)
            indices = np.array([mesh.Triangle(i).Get() for i in range(1, mesh.NbTriangles() + 1)]) - 1
            if face.Orientation() == TopAbs_REVERSED:
                indices = indices[:, [0, 2, 1]]
            triangles = vertices[indices]
            area = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                           triangles[:, 2] - triangles[:, 0]), axis=1) / 2
            keep = area > 1e-6
            removed_count += int((~keep).sum())
            zero_count += int((area <= 1e-10).sum())
            removed_area += float(area[~keep].sum())
            chunks.setdefault((solid_id, rgb(face, base_color)), []).append(triangles[keep])

    known = {(g["solid"], tuple(g["rgb_srgb"])): g for g in metadata["groups"]}
    if set(chunks) != set(known):
        raise ValueError("CAD color/solid partition changed; review before replacing the asset")
    arrays = {}
    cursor = 0
    for group in metadata["groups"]:
        key = (group["solid"], tuple(group["rgb_srgb"]))
        pieces = chunks[key]
        triangles = np.concatenate(pieces)
        arrays[group["array_key"]] = triangles
        group["start"], group["end"] = cursor, cursor + len(triangles)
        cursor += len(triangles)
        points = triangles.reshape(-1, 3)
        group["bounds_mesh"] = [points.min(0).tolist(), points.max(0).tolist()]
    combined = np.concatenate([arrays[g["array_key"]] for g in metadata["groups"]])
    points = combined.reshape(-1, 3)
    bounds = np.array([points.min(0), points.max(0)])
    if not np.isfinite(combined).all() or not np.allclose(bounds, metadata["root_bounds"][0], atol=.05):
        raise ValueError("Invalid coordinates or changed mm envelope")
    if len(combined) > 197000:
        raise ValueError("Feeder exceeds the documented 200,000-face assembly budget")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / "k3560_1_step.npz"
    np.savez_compressed(target, **arrays)
    raw = io.BytesIO()
    np.save(raw, combined)
    metadata.update(triangles=len(combined), mesh_bounds=bounds.tolist(),
                    native_triangle_sha256=hashlib.sha256(raw.getvalue()).hexdigest(),
                    mesh_asset_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    removed_zero_area_triangles=zero_count,
                    removed_sliver_triangles=removed_count, removed_area_mm2=removed_area)
    metadata["triangle_filter"]["removed_area_mm2"] = removed_area
    (args.out_dir / "k3560_1_step.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(combined):,} triangles / {len(arrays)} color groups to {target}")


if __name__ == "__main__":
    main()
