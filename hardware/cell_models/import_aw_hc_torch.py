'Build the pinned AW-HC cobot torch display asset from AW-HC.zip.'


from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DECODER_PATH = ROOT / "hardware" / "cell_models" / "import_motosim_hsf.py"


ARCHIVE_SHA256 = "74a5128f86eea7071ce5e4e8447d2f83faeb43c495ad3fa945cec8e6d0abac81"
MEMBER = "AW-HC/models/Yaskawa_Torch.hsf"
VCL_MEMBER = "AW-HC/AW-HC.vcl"
ARRAY_KEY = "Yaskawa_Torch"
EXPECTED_BANNER = ";; HSF V20.95"
EXPECTED_TRIANGLES = 13953


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vcl_model(vcl_text: str, name: str) -> dict:

    for block in re.findall(r"\bMODEL\s*\{([^{}]*)\}", vcl_text):
        fields = dict(re.findall(r"^\s*([A-Z0-9_]+)=([^;]*);", block, re.M))
        if fields.get("NAME") == name:
            return fields
    raise ValueError(f"AW-HC.vcl: MODEL {name} not found")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    archive_bytes = args.archive.read_bytes()
    archive_hash = sha256(archive_bytes)
    if archive_hash != ARCHIVE_SHA256:
        raise ValueError("Unreviewed AW-HC.zip hash; inspect units/transforms before importing the torch")

    spec = importlib.util.spec_from_file_location("audited_motosim_hsf_for_aw_hc_torch", DECODER_PATH)
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)

    with ZipFile(args.archive) as archive:
        names = set(archive.namelist())
        if MEMBER not in names or VCL_MEMBER not in names:
            raise ValueError("AW-HC.zip is missing the torch model or its VCL scene")
        hsf_bytes = archive.read(MEMBER)
        vcl_text = archive.read(VCL_MEMBER).decode("utf-8", "replace")

    torch_model = _vcl_model(vcl_text, "Yaskawa_Torch")
    if torch_model.get("PARENT") != "YRC1000-R01_flange":
        raise ValueError("Yaskawa_Torch is no longer parented to the flange in AW-HC.vcl")
    axis6 = [float(x) for x in torch_model["AXIS6"].split(",")]
    if not np.allclose(axis6, [0, 0, 0, 0, 0, 0]):
        raise ValueError(f"Yaskawa_Torch AXIS6 is no longer identity: {axis6}")
    scale = float(torch_model["SCALE"])
    if scale != 0.001:
        raise ValueError(f"Yaskawa_Torch SCALE is no longer the mm->m boundary: {scale}")

    tcp_model = _vcl_model(vcl_text, "Yaskawa_TCP")
    if tcp_model.get("PARENT") != "Yaskawa_Torch":
        raise ValueError("Yaskawa_TCP is no longer parented to Yaskawa_Torch in AW-HC.vcl")
    tcp_axis6 = [float(x) for x in tcp_model["AXIS6"].split(",")]

    with tempfile.TemporaryDirectory(prefix="aw-hc-torch-") as scratch:
        temp_path = Path(scratch) / "Yaskawa_Torch.hsf"
        temp_path.write_bytes(hsf_bytes)
        decoded = decoder.extract_hsf(temp_path)
        if decoded["banner"] != EXPECTED_BANNER:
            raise ValueError(f"Unreviewed HSF format version for the torch: {decoded['banner']}")
        triangles = decoder.triangulate_shells(decoded["shells"])

    if len(triangles) != EXPECTED_TRIANGLES:
        raise ValueError(f"Torch triangle count changed: {len(triangles)} != {EXPECTED_TRIANGLES}")
    if triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or not np.isfinite(triangles).all():
        raise ValueError("Decoded torch geometry is not a finite (N,3,3) triangle soup")

    flat = triangles.reshape(-1, 3)
    bounds = [flat.min(0).tolist(), flat.max(0).tolist()]

    args.out.mkdir(parents=True, exist_ok=True)
    npz_path = args.out / "aw_hc_torch.npz"
    np.savez_compressed(npz_path, **{ARRAY_KEY: triangles})

    manifest = {
        "source_archive": "AW-HC.zip",
        "source_archive_sha256": archive_hash,
        "source_member": MEMBER,
        "source_sha256": sha256(hsf_bytes),
        "format": decoded["banner"],
        "units": "mm",
        "array_frame": "native HSF mm, unrotated",
        "array_key": ARRAY_KEY,
        "triangles": len(triangles),
        "bounds_native_mm": bounds,
        "mounting": {
            "vcl_model": "Yaskawa_Torch",
            "vcl_parent": torch_model["PARENT"],
            "vcl_axis6": axis6,
            "vcl_scale": scale,
            "note": (
                "Identity AXIS6: native Z=0 is the flange mating face, mounted directly on "
                "YRC1000-R01_flange with no extra VCL rotation. VCL SCALE=0.001 is the "
                "mm-to-metre boundary for MotoSim and must not be reapplied to these "
                "native-millimetre triangles."
            ),
        },
        "tool_data_vendor_nominal": {
            "vcl_model": "Yaskawa_TCP",
            "vcl_parent": tcp_model["PARENT"],
            "local_xyz_mm": tcp_axis6[:3],
            "local_rxryrz_deg": tcp_axis6[3:],
            "status": (
                "Vendor-nominal MotoSim dummy marker from AW-HC.vcl, not a calibrated or "
                "controller-taught TCP. Distance from the torch's own native origin "
                f"(the flange face) is {float(np.linalg.norm(tcp_axis6[:3])):.3f} mm."
            ),
        },
        "mesh_sha256": sha256(npz_path.read_bytes()),
        "decoder_sha256": sha256(DECODER_PATH.read_bytes()),
        "notes": [
            "Display-only vendor MotoSim HSF tessellation, not a BRep or a collision/calibration source.",
            "Decoded with the project's general import_motosim_hsf.py reader; the torch's largest "
            "shell is far under its 500,000-point cap, so no VTK reduction is used or needed.",
            "Triangles are native HSF millimetres, unrotated. cell_hc.py applies the native-to-"
            "project-flange-frame rotation at mount time, the same rotation cell_models.py's "
            "yaskawa_torch_parts() applies at read time for the LC torch.",
        ],
    }
    manifest_path = args.out / "aw_hc_torch.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"{MEMBER}: {decoded['banner']}; {len(triangles)} triangles; bounds {bounds}; "
        f"wrote {npz_path} and {manifest_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
