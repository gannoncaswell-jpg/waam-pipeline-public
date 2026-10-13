'Build the source-resolution MotoSim torch display library without changing TCP.'


from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from zipfile import ZipFile

import numpy as np


ARCHIVE_SHA256 = "a2082235c850d21e9cde8ce9edb11c3b3df7b6eb305623e4ca7ae257d9bd0f11"
DEFAULT_VARIANT = "45d_shock_mnt"
NATIVE_TO_FLANGE = np.array([[0., 0., 1.], [1., 0., 0.], [0., 1., 0.]])
MIN_AREA_MM2 = 1e-6
EXPECTED = {
    "22d_hard_mnt_brake": (9, 27337, 0),
    "22d_hard_mnt": (12, 67698, 0),
    "22d_shock_mnt_brake": (15, 147856, 1),
    "22d_shock_mnt": (17, 124314, 0),
    "45d_hard_mnt_brake": (9, 27076, 0),
    "45d_hard_mnt": (12, 67441, 0),
    "45d_shock_mnt_brake": (15, 147600, 2),
    "45d_shock_mnt": (17, 124057, 0),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_members(archive: ZipFile) -> dict[str, object]:
    entries = archive.infolist()
    if len(entries) > 2000 or sum(e.file_size for e in entries) > 512 * 1024**2:
        raise ValueError("Unexpected archive size")
    members = {}
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or ":" in name:
            raise ValueError(f"Unsafe archive member: {name}")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise ValueError(f"Archive symlink: {name}")
        key = name.casefold()
        if key in members:
            raise ValueError(f"Duplicate archive member: {name}")
        members[key] = entry
    return members


def parse_item(text: str, hsf_name: str) -> dict:
    if "ATTRIBUTE=TOOL_FLANGE" not in text:
        raise ValueError("Expected explicit flange-mounted ITEM")
    models = re.findall(r"\bMODEL\s*\{([^{}]*)\}", text, re.S)
    matches = [m for m in models if re.search(
        r"FILENAME\s*=\s*" + re.escape(hsf_name) + r"\s*;", m, re.I)]
    if len(matches) != 1:
        raise ValueError("Expected exactly one model for the source HSF")
    block = matches[0]
    axis = [float(x) for x in re.search(r"AXIS6\s*=\s*([^;]+);", block)[1].split(",")]
    scale = float(re.search(r"SCALE\s*=\s*([^;]+);", block)[1])
    parent = re.search(r"PARENT\s*=\s*([^;]+);", block)[1].strip()
    if not np.allclose(axis, [0, 0, 0, 0, 0, -90], atol=1e-12) or scale != .001 or parent != "world":
        raise ValueError(f"Unexpected source model transform: {axis}, {scale}, {parent}")
    tool = re.search(r"TOOL_DATA\s*\{.*?NAME=[^\r\n]+\s*([^\r\n]+)", text, re.S)
    tool_data = [float(x) for x in tool[1].strip().split(",")]
    if len(tool_data) != 6:
        raise ValueError("Expected six TOOL_DATA values")
    return {"axis6": axis, "scale": scale, "parent": parent,
            "tool_data_yaskawa": tool_data}


def area_vectors(triangles: np.ndarray) -> np.ndarray:
    return .5 * np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])


def presentation_finish(variant: str, shell: int) -> dict:

    finish = {"color": "#8c949c", "metalness": .72, "roughness": .3,
              "source": "Neutral presentation finish; HSF source materials are not decoded"}
    finish_shells = {
        "45d_shock_mnt": {"housing": 11, "nozzle": 9, "inner_tube": 5},
        "45d_shock_mnt_brake": {"housing": 9, "nozzle": 8, "inner_tube": 3},
    }.get(variant)
    if finish_shells is None:
        return finish
    if shell == finish_shells["housing"]:
        finish.update(color="#252a30", metalness=.12, roughness=.4,
                      source="Charcoal shock housing presentation, visually matched to supplied item.bmp; not decoded HSF material")
    elif shell == finish_shells["nozzle"]:
        finish.update(color="#b58a43", metalness=.78, roughness=.28,
                      source="Brass nozzle/coupling presentation, visually matched to supplied item.bmp; not decoded HSF material")
    elif shell == finish_shells["inner_tube"]:
        finish.update(color="#b58059", metalness=.75, roughness=.3,
                      source="Copper inner tube/contact-tip presentation assumption; not decoded HSF material or an installed-consumable identification")
    return finish


def display_precision(triangles: np.ndarray) -> dict:
    flange = triangles @ NATIVE_TO_FLANGE.T
    original_vectors = area_vectors(flange)
    original_area = np.linalg.norm(original_vectors, axis=1)
    report = {}
    for places in (5, 6, None):
        rounded = flange if places is None else np.round(flange, places)
        displayed = rounded.astype(np.float32).astype(np.float64)
        vectors = area_vectors(displayed)
        areas = np.linalg.norm(vectors, axis=1)
        flipped = np.einsum("ij,ij->i", vectors, original_vectors) < 0
        report["source_float32" if places is None else str(places)] = {
            "collapsed_faces": int(np.count_nonzero(areas == 0)),
            "flipped_faces": int(np.count_nonzero(flipped)),
            "faces_below_display_area_threshold": int(np.count_nonzero(areas <= MIN_AREA_MM2)),
            "minimum_area_mm2": float(areas.min()),
            "collapsed_source_area_mm2": float(original_area[areas == 0].sum()),
            "flipped_source_area_mm2": float(original_area[flipped].sum()),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--decoder", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source_hash = sha256(args.archive.read_bytes())
    if source_hash != ARCHIVE_SHA256:
        raise ValueError("Unreviewed archive hash; inspect units, transforms and completeness first")
    spec = importlib.util.spec_from_file_location("audited_motosim_hsf", args.decoder)
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)
    args.out.mkdir(parents=True, exist_ok=True)
    arrays, variants, audit = {}, {}, {}
    with ZipFile(args.archive) as archive, tempfile.TemporaryDirectory(prefix="torch-import-", dir=args.out) as scratch:
        members = safe_members(archive)
        for key, entry in sorted(members.items()):
            if not key.endswith(".hsf"):
                continue
            member_name = entry.filename.replace("\\", "/")
            path = PurePosixPath(member_name)
            variant = path.parent.name.removesuffix("_Yaskawa_Torch").lower().replace("-", "_")
            if variant not in EXPECTED:
                raise ValueError(f"Unreviewed variant: {variant}")
            item_entry = members[str(path.parent / "ITEM.ini").casefold()]
            item_bytes = archive.read(item_entry)
            item = parse_item(item_bytes.decode("utf-8-sig"), path.name)
            source_bytes = archive.read(entry)
            temp_path = Path(scratch) / path.name
            temp_path.write_bytes(source_bytes)
            decoded = decoder.extract_hsf(temp_path)
            if decoded["banner"] != ";; HSF V20.95":
                raise ValueError("Unreviewed HSF format version")
            header = np.asarray(decoded["header_bbox_mm"], dtype=float).reshape(2, 3)
            if not 250 < header[1, 2] < 400:
                raise ValueError("Torch scale is not independently consistent with mm")
            groups, rejected = [], []
            source_triangles, removed_faces, removed_area = 0, 0, 0.
            for index, shell in enumerate(decoded["shells"]):
                vertices = np.asarray(shell["vertices"], dtype=np.float64)
                faces = shell["faces"]
                if faces is None:


                    if not np.all(np.abs(vertices) < np.finfo(np.float32).tiny):
                        raise ValueError(f"Nontrivial unsupported shell {variant}/{index}")
                    rejected.append({"shell": index, "vertex_count": len(vertices),
                                     "reason": "False S00 signature: only float32 denormals in integer payload, no face record",
                                     "maximum_absolute_coordinate": float(np.abs(vertices).max())})
                    continue
                if not faces or any(len(face) != 3 or len(set(map(int, face))) != 3 for face in faces):
                    raise ValueError(f"Non-triangular or repeated-index face: {variant}/{index}; no fan triangulation permitted")
                triangles = vertices[np.asarray(faces, dtype=np.int64)]
                areas = np.linalg.norm(area_vectors(triangles), axis=1)
                keep = areas > MIN_AREA_MM2
                source_triangles += len(triangles)
                removed_faces += int(np.count_nonzero(~keep))
                removed_area += float(areas[~keep].sum())
                retained = triangles[keep].astype(np.float32)
                array_key = f"{variant}_shell_{index:02d}"
                arrays[array_key] = retained
                groups.append({
                    "array_key": array_key, "shell": index,
                    "triangles": len(retained), "source_triangles": len(triangles),
                    "removed_subthreshold_triangles": int(np.count_nonzero(~keep)),
                    "removed_area_mm2": float(areas[~keep].sum()),
                    "bounds_native_mm": [retained.min((0, 1)).tolist(), retained.max((0, 1)).tolist()],
                    "appearance": presentation_finish(variant, index),
                })
            expected_shells, expected_faces, expected_rejected = EXPECTED[variant]
            if (len(groups), source_triangles, len(rejected)) != EXPECTED[variant]:
                raise ValueError(f"Completeness mismatch for {variant}: {len(groups)}, {source_triangles}, {len(rejected)}")
            combined = np.concatenate([arrays[g["array_key"]] for g in groups]).astype(np.float64)
            bounds = np.array([combined.min((0, 1)), combined.max((0, 1))])

            if not np.allclose(bounds, header, rtol=0, atol=5e-5):
                raise ValueError(f"Geometry/header bounds mismatch for {variant}: {bounds} vs {header}")
            precision = display_precision(combined)
            variants[variant] = {
                "name": path.parent.name,
                "source_member": member_name,
                "source_sha256": sha256(source_bytes),
                "source_item_member": item_entry.filename.replace("\\", "/"),
                "source_item_sha256": sha256(item_bytes),
                "tool_data_yaskawa": item["tool_data_yaskawa"],
                "source_model_axis6": item["axis6"],
                "source_model_scale": item["scale"],
                "registration": {
                    "rotation": NATIVE_TO_FLANGE.tolist(), "translation_mm": [0., 0., 0.],
                    "status": "Vendor nominal flange-mounted display; installed side orientation and consumable not independently verified",
                    "evidence": "ITEM AXIS6 Rz(-90 deg), followed by proper Yaskawa-to-project rotation (z,-y,x), gives native (z,x,y); native flange plane Z=0; no TCP fit or extra scale",
                },
                "groups": groups,
                "bounds_native_mm": bounds.tolist(),
                "triangles": len(combined), "source_triangles": source_triangles,
                "removed_subthreshold_triangles": removed_faces,
                "removed_area_mm2": removed_area,
                "notes": [
                    "Display-only vendor MotoSim HSF tessellation, not a BRep or a collision/calibration source.",
                    "Every supported face is already a source triangle; no concave polygons, polygon holes, decimation or fan triangulation used.",
                    "All geometric shells retained, including internal surfaces; empirically decoded HSF materials and full segment tree remain unsupported.",
                    "Header and decoded bounds match independently in mm. ITEM SCALE=0.001 converts these mm to MotoSim metres and must not be applied again.",
                    "Native mounting flange is Z=0. Non-brake locating pilots extend to Z=-4.5; bounding-box minimum is not a seating plane.",
                    "Vendor TOOL_DATA is nominal metadata only; installed controller TOOL0 remains authoritative.",
                ],
            }
            audit[variant] = {"rejected_signatures": rejected, "precision_native_to_flange_then_float32": precision,
                              "exact_header_bounds_match_mm": bool(np.array_equal(bounds, header))}
            print(f"{variant}: {source_triangles} source -> {len(combined)} retained triangles; {len(groups)} shells; removed area {removed_area:.9g} mm2", flush=True)
    if set(variants) != set(EXPECTED):
        raise ValueError("Incomplete eight-variant library")
    npz_path = args.out / "yaskawa_torches.npz"
    np.savez_compressed(npz_path, **arrays)
    metadata = {
        "source_archive": args.archive.name, "source_sha256": source_hash,
        "source_bytes": args.archive.stat().st_size,
        "format": "Yaskawa MotoSim HSF V20.95", "units": "mm", "array_frame": "native HSF mm",
        "default_variant": DEFAULT_VARIANT,
        "display_area_threshold_mm2": MIN_AREA_MM2,
        "mesh_sha256": sha256(npz_path.read_bytes()),
        "decoder_sha256": sha256(args.decoder.read_bytes()),
        "variants": variants,
    }
    (args.out / "yaskawa_torches.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (args.out / "yaskawa_torches-import-audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {npz_path} ({npz_path.stat().st_size:,} bytes)", flush=True)


if __name__ == "__main__":
    main()
