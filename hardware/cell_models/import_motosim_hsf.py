'Extract polygon meshes from Yaskawa MotoSim HSF V20.x files.'


from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

import numpy as np


def _parse_faces(buf: bytes, pos: int, point_count: int):

    if pos >= len(buf) or buf[pos] != 0x01:
        return None, pos
    length = struct.unpack_from("<I", buf, pos + 1)[0]
    if length < 2 or pos + 5 + length > len(buf):
        return None, pos
    bits = buf[pos + 5]
    if bits not in (8, 16, 32):
        return None, pos
    width = bits // 8
    payload = buf[pos + 6 : pos + 5 + length]
    if len(payload) % width:
        return None, pos
    dtype = {8: "<u1", 16: "<u2", 32: "<u4"}[bits]
    values = np.frombuffer(payload, dtype).astype(np.int64)
    faces: list[np.ndarray] = []
    index = 0
    while index < len(values):
        count = int(values[index])
        if count < 3 or index + 1 + count > len(values):
            return None, pos
        face = values[index + 1 : index + 1 + count]
        if int(face.max()) >= point_count:
            return None, pos
        faces.append(face)
        index += 1 + count
    return faces, pos + 5 + length


def extract_hsf(path: str | Path) -> dict:


    path = Path(path)
    data = path.read_bytes()
    newline = data.find(b"\n")
    banner = data[:newline].decode("ascii", "replace").strip()
    bbox_pos = data.find(b"B", newline)
    if bbox_pos < 0:
        raise ValueError(f"{path}: HSF bounding-box opcode not found")
    header = np.array(struct.unpack_from("<6f", data, bbox_pos + 3), dtype=float)

    zlib_pos = data.find(b"\x78\xda")
    if zlib_pos < 0:
        zlib_pos = data.find(b"\x78\x9c")
    if zlib_pos < 0:
        raise ValueError(f"{path}: compressed HSF opcode stream not found")
    buf = zlib.decompressobj().decompress(data[zlib_pos:])


    header_scale = 1000.0 if np.abs(header).max() < 50 else 1.0
    lo, hi = header[:3], header[3:]
    tolerance = 0.02 * max(hi - lo) + (0.01 if header_scale == 1000 else 10.0)
    alt_lo, alt_hi, alt_tolerance = lo * 1000, hi * 1000, tolerance * 1000

    shells = []
    cursor = 0
    while True:
        cursor = buf.find(b"\x53\x00", cursor)
        if cursor < 0 or cursor + 12 > len(buf):
            break
        count = struct.unpack_from("<i", buf, cursor + 8)[0]
        end = cursor + 12 + count * 12
        if not 3 <= count <= 500_000 or end > len(buf):
            cursor += 1
            continue
        values = np.frombuffer(buf, "<f4", count * 3, offset=cursor + 12)
        if not np.all(np.isfinite(values)):
            cursor += 1
            continue
        points = values.reshape(-1, 3)
        point_lo, point_hi = points.min(0), points.max(0)
        if np.all(point_lo >= lo - tolerance) and np.all(point_hi <= hi + tolerance):
            shell_scale = header_scale
        elif (
            header_scale == 1000
            and np.all(point_lo >= alt_lo - alt_tolerance)
            and np.all(point_hi <= alt_hi + alt_tolerance)
            and max(point_hi - point_lo) > 1.5 * max(hi - lo)
        ):
            shell_scale = 1.0
        else:
            cursor += 1
            continue
        faces, face_end = _parse_faces(buf, end, count)
        shells.append({"vertices": points.copy() * shell_scale, "faces": faces})
        cursor = face_end if faces else end

    if not shells:
        raise ValueError(f"{path}: no supported HSF shell records found")
    return {
        "path": path,
        "banner": banner,
        "header_bbox_mm": header * header_scale,
        "shells": shells,
    }


def triangulate_shells(shells: list[dict]) -> np.ndarray:

    triangles = []
    for shell in shells:
        vertices = shell["vertices"]
        for face in shell["faces"] or []:
            for index in range(1, len(face) - 1):
                triangles.append(vertices[[face[0], face[index], face[index + 1]]])
    if not triangles:
        return np.empty((0, 3, 3), dtype=np.float32)
    return np.asarray(triangles, dtype=np.float32)


def _write_outputs(result: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = result["path"].stem
    npz_path = output_dir / f"{stem}_shells.npz"
    obj_path = output_dir / f"{stem}.obj"
    arrays = {}
    with obj_path.open("w", encoding="ascii", newline="\n") as obj:
        obj.write(f"# {stem} extracted from HSF; units mm\n")
        vertex_offset = 1
        for shell_index, shell in enumerate(result["shells"]):
            vertices = np.asarray(shell["vertices"], dtype=np.float32)
            arrays[f"v{shell_index}"] = vertices
            faces = shell["faces"] or []
            if faces:
                arrays[f"f{shell_index}"] = np.concatenate(
                    [np.concatenate(([len(face)], face)) for face in faces]
                ).astype(np.int32)
            for vertex in vertices:
                obj.write(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}\n")
            for face in faces:
                obj.write("f " + " ".join(str(int(i) + vertex_offset) for i in face) + "\n")
            vertex_offset += len(vertices)
    np.savez_compressed(npz_path, **arrays)
    return npz_path, obj_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hsf", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    args = parser.parse_args()
    for path in args.hsf:
        result = extract_hsf(path)
        triangles = triangulate_shells(result["shells"])
        bbox = result["header_bbox_mm"]
        npz_path, obj_path = _write_outputs(result, args.out)
        print(
            f"{path.name}: {result['banner']}; {len(result['shells'])} shells; "
            f"{len(triangles)} triangles; bbox "
            f"{np.round(bbox[:3], 3).tolist()}..{np.round(bbox[3:], 3).tolist()}; "
            f"wrote {npz_path} and {obj_path}"
        )


if __name__ == "__main__":
    main()
