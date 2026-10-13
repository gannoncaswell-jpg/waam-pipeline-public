from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time

import numpy as np

from .toolpath import Move, Toolpath

VERSION = "2.1.0"
PROFILE_VERSION = 1
CONFIG = Path(__file__).resolve().parents[2] / ".waam-ornl.json"
MAX_TRIANGLES = 200_000
MAX_LAYERS = 500
MAX_MOVES = 30_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 120
PATTERNS = {"lines": 0, "concentric": 2, "none": 0}


@dataclass(frozen=True)
class Options:
    layer_height_mm: float
    bead_width_mm: float
    feed_mm_min: float
    contours: int = 2
    stepover_percent: float = 75.0
    pattern: str = "lines"
    angle_deg: float = 0.0
    angle_rotation_deg: float = 90.0
    travel_lift_mm: float = 5.0

    def __post_init__(self):
        for key, low, high in (
            ("layer_height_mm", .1, 100), ("bead_width_mm", .1, 100),
            ("feed_mm_min", 1, 10000), ("stepover_percent", 10, 100),
            ("angle_deg", -360, 360), ("angle_rotation_deg", -360, 360),
            ("travel_lift_mm", .1, 100),
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"ORNL {key}: enter {low}–{high}")
        if isinstance(self.contours, bool) or not isinstance(self.contours, int) or not 1 <= self.contours <= 20:
            raise ValueError("ORNL contours: enter a whole number from 1 to 20")
        if self.pattern not in PATTERNS:
            raise ValueError("ORNL infill: choose lines, concentric or none")

    @property
    def spacing_mm(self):
        return self.bead_width_mm * self.stepover_percent / 100


def executable() -> Path | None:

    value = os.environ.get("WAAM_ORNL_EXECUTABLE")
    if not value and CONFIG.is_file():
        try:
            value = json.loads(CONFIG.read_text(encoding="utf-8"))["executable"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    if not value:
        value = shutil.which("ornlslicer_cli") or shutil.which("ornlslicer")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_file() else None


def availability() -> dict:
    configured = executable() is not None
    return {"configured": configured, "required_version": VERSION,
            "message": ("ORNL engine configured" if configured else
                        "ORNL engine is not configured on this server. Ask the server owner to configure ORNLSlicer 2.1.0.")}


def settings(options: Options) -> dict:

    width = options.bead_width_mm * 1000
    spacing = options.spacing_mm * 1000
    values = {
        "syntax": 30, "machine_type": 0, "slicing_mode": 0,
        "layer_height": options.layer_height_mm * 1000,
        "perimeter": True, "perimeter_count": 1, "perimeter_width": width,
        "inset": options.contours > 1, "inset_count": options.contours - 1,
        "inset_width": width, "inset_overlap_distance": width - spacing,
        "infill": options.pattern != "none", "infill_pattern": PATTERNS[options.pattern],
        "infill_manual_spacing": True, "infill_line_spacing": spacing,
        "infill_width": width, "infill_overlap_distance": width - spacing,
        "infill_angle": math.radians(options.angle_deg),
        "infill_angle_rotation": math.radians(options.angle_rotation_deg),
        "infill_combine_every_x_layers": 1,
        "skin": False, "support": False, "skeleton": False,
        "enable_spiralize_mode": False, "supports_G2_3": False,
        "perimeter_adapt": False, "inset_adapt": False,
        "force_minimum_layer_time": False,
        "travel_speed": 20000, "z_speed": 20000,
        "travel_lift_height": options.travel_lift_mm * 1000,
        "minimum_travel_for_lift": 0,
        "final_lift_distance": options.travel_lift_mm * 1000,
        "enable_default_startup_code": False, "enable_bounding_box": False,
        "enable_material_load": False, "start_code": "G21\nG90\n", "end_code": "",
        "gear_ratio": 1, "x_offset": 0, "y_offset": 0, "z_offset": 0,
        "minimum_x": -10000000, "maximum_x": 10000000,
        "minimum_y": -10000000, "maximum_y": 10000000,
        "minimum_z": 0, "maximum_z": 10000000,
        "path_order_optimization": 0, "point_order_optimization": 0,
    }
    for region in ("perimeter", "inset", "infill"):
        values[region + "_speed"] = options.feed_mm_min * 1000 / 60
        values[region + "_extruder_speed"] = 1
    return {"header": {"created_by": "WAAM pipeline", "version": 10.0}, "settings": [values]}


def validate_mesh(triangles, options: Options) -> np.ndarray:
    tri = np.asarray(triangles, dtype=float)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3) or not 1 <= len(tri) <= MAX_TRIANGLES:
        raise ValueError(f"ORNL needs an STL with 1–{MAX_TRIANGLES:,} triangles")
    if not np.isfinite(tri).all() or np.abs(tri).max() > 10000:
        raise ValueError("ORNL STL coordinates must be finite and within 10,000 mm")
    if abs(float(tri[:, :, 2].min())) > .001:
        raise ValueError("ORNL input must be transformed to millimetres with its bottom at Z=0")
    if float(tri[:, :, 2].max()) / options.layer_height_mm > MAX_LAYERS:
        raise ValueError(f"ORNL slice exceeds {MAX_LAYERS} layers; increase layer height or reduce the part")
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    if np.any(np.linalg.norm(cross, axis=1) < 1e-10):
        raise ValueError("ORNL bulk slicing needs a closed solid STL without degenerate triangles")

    _, ids = np.unique(tri.reshape(-1, 3), axis=0, return_inverse=True)
    ids = ids.reshape(-1, 3)
    edges = np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]])
    _, counts = np.unique(np.sort(edges, axis=1), axis=0, return_counts=True)
    if np.any(counts != 2):
        raise ValueError("ORNL bulk slicing needs a watertight solid STL; repair open or non-manifold edges in CAD")
    _, directed_counts = np.unique(edges, axis=0, return_counts=True)
    if np.any(directed_counts != 1):
        raise ValueError("ORNL STL face orientations are inconsistent; repair the solid in CAD")
    return tri


def stl_bytes(tri: np.ndarray) -> bytes:
    records = np.zeros(len(tri), dtype=np.dtype([("normal", "<f4", (3,)),
                       ("vertices", "<f4", (3, 3)), ("attribute", "<u2")]))
    records["vertices"] = tri
    return b"WAAM ORNL millimetres".ljust(80, b"\0") + struct.pack("<I", len(tri)) + records.tobytes()


_COMMENT = re.compile(r"\([^()]*\)")
_WORD = re.compile(r"([XYZFS])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
_LAYER = re.compile(r"\(BEGINNING LAYER: (\d+)\)")
_COUNT = re.compile(r"\(LAYER COUNT: (\d+)\)")
_ANNOTATIONS = {
    "G0": {"TRAVEL", "TRAVEL LOWER Z", "TRAVEL LIFT Z", "TRAVEL FINAL LIFT Z"},
    "G1": {"PERIMETER", "INSET", "INFILL"},
    "M3": {"TURN EXTRUDER ON"},
    "M5": {"TURN EXTRUDER OFF", "TURN EXTRUDER OFF END OF PRINT"},
    "M01": {"OPTIONAL STOP - LAYER CHANGE"},
    "M1": {"OPTIONAL STOP - LAYER CHANGE"},
    "M02": {"END OF G-CODE"}, "M2": {"END OF G-CODE"},
}
_HEADER = re.compile(
    r"\((?:Sliced on|Expected Weight|Expected Build Time|Minimum Layer Time|"
    r"Maximum Layer Time|XYZ Translation Data|Part Translation|Travel Types|"
    r"Travel Colors|Filename): [^()]*\)|\(Copyright \d{4} ORNL\)")
_FOOTER = re.compile(r"\(([a-z][a-zA-Z0-9_-]*) (.+)\)")


def parse_output(text: str, options: Options, *, expected_job: str | None = None,
                 expected_height_mm: float | None = None) -> Toolpath:


    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("ORNL output exceeds the planning size limit")
    if f"(G-Code generated by ORNLSlicer {VERSION})" not in text or "(G-Code Syntax: ORNL-Metric)" not in text:
        raise ValueError("Expected ORNLSlicer 2.1.0 ORNL-Metric profile output")
    if expected_job is not None and re.findall(r"^\(WAAM_JOB ([0-9a-f]{64})\)$", text, re.M) != [expected_job]:
        raise ValueError("ORNL output does not belong to this mesh and profile; refusing stale or shared-cache output")
    tp = Toolpath()
    layer, count, arc, ended = -1, None, False, False
    metric = absolute = False
    xyz, feed = {}, None
    seen_layers = set()
    footer = False
    for line_no, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        def refuse(reason, source_line=line_no):
            raise ValueError(f"ORNL output line {source_line}: {reason}")


        if raw == "(Settings Footer)":
            if not ended or footer:
                refuse("misplaced settings footer")
            footer = True
            continue
        if footer and (record := _FOOTER.fullmatch(raw)):
            try:
                value = json.loads(record[2])
            except ValueError:
                refuse("malformed settings footer")
            if not isinstance(value, (str, int, float, bool, list)):
                refuse("malformed settings footer")
            continue
        if match := _COUNT.fullmatch(raw):
            if ended or count is not None or layer != -1:
                refuse("duplicate or misplaced layer count")
            count = int(match[1])
            if not 1 <= count <= MAX_LAYERS:
                refuse("unsupported layer count")
        if match := _LAYER.fullmatch(raw):
            incoming = int(match[1]) - 1
            if ended or arc or incoming != layer + 1 or count is None or incoming >= count:
                refuse("out-of-order layer or arc active during layer change")
            layer = incoming
        comments = _COMMENT.findall(raw)
        code = _COMMENT.sub("", raw).strip()
        command = code.split(maxsplit=1)[0] if code else ""
        if code:
            if len(comments) > 1 or any(c[1:-1] not in _ANNOTATIONS.get(command, set()) for c in comments):
                refuse("unknown, contradictory or misplaced region/command annotation")
        elif raw:
            metadata = _COUNT.fullmatch(raw) or _LAYER.fullmatch(raw)
            header = layer == -1 and not ended and (
                _HEADER.fullmatch(raw) or raw in (
                    f"(G-Code generated by ORNLSlicer {VERSION})",
                    "(G-Code Syntax: ORNL-Metric)", "(Slicing Parameters)")
                or re.fullmatch(r"\(WAAM_JOB [0-9a-f]{64}\)", raw))
            if not metadata and not header:
                refuse("unknown or malformed metadata/region annotation")
        if not code:
            continue
        if ended:
            refuse("machine command after end of toolpath")
        parts = code.split(maxsplit=1)
        command = parts[0]
        tail = parts[1] if len(parts) > 1 else ""
        values = {}
        for token in tail.split():
            word = _WORD.fullmatch(token)
            if not word or word[1] in values:
                refuse("unsupported or duplicate coordinate/parameter")
            value = float(word[2])
            if not math.isfinite(value) or abs(value) > 100000:
                refuse("non-finite or out-of-range value")
            values[word[1]] = value
        if command in ("G21", "G90") and not values and not tp.moves:
            metric |= command == "G21"
            absolute |= command == "G90"
        elif command in ("M1", "M01") and not values and not arc and layer >= 0:

            if "(OPTIONAL STOP - LAYER CHANGE)" not in comments:
                refuse("unexpected optional stop")
        elif command == "M3" and values == {"S": 1.0} and tp.moves and not tp.moves[-1].deposit and not arc:
            arc = True
        elif command == "M5" and not values:
            arc = False
        elif command in ("M2", "M02") and not values and not arc:
            ended = True
        elif command in ("G0", "G1"):
            if not metric or not absolute or layer < 0 or not {"X", "Y"} <= values.keys():
                refuse("motion lacks metric/absolute modes, layer, or XY coordinates")
            if "S" in values and values["S"] != 1:
                refuse("unexpected transport process marker")
            if command == "G0" and arc or command == "G1" and not arc:
                refuse("travel/deposition disagrees with M3/M5 state")
            xyz.update({k: values[k] for k in "XYZ" if k in values})
            if len(xyz) != 3:
                refuse("first motion has no complete XYZ target")
            if "F" in values:
                feed = values["F"]
            role = None
            if arc:
                roles = [c[1:-1].lower() for c in comments if c in ("(PERIMETER)", "(INSET)", "(INFILL)")]
                if len(roles) != 1 or feed is None or not math.isclose(feed, options.feed_mm_min, abs_tol=.001):
                    refuse("deposition needs a known region and the requested feed rate")
                role = roles[0]
                if role == "infill" and options.pattern == "none" or role == "inset" and options.contours == 1:
                    refuse("unexpected region for this profile")
                expected_z = (layer + 1) * options.layer_height_mm
                if not tp.moves or abs(xyz["Z"] - expected_z) > .002 or abs(tp.moves[-1].z - xyz["Z"]) > .002:
                    refuse("non-planar deposition or incorrect layer height")
                seen_layers.add(layer)
            tp.moves.append(Move(xyz["X"], xyz["Y"], xyz["Z"], feed if arc else None,
                                 arc, layer, line_no, region=role))
            if len(tp.moves) > MAX_MOVES:
                refuse(f"more than {MAX_MOVES:,} moves; simplify the mesh or increase bead spacing")
        else:
            refuse(f"unsupported command {command!r}")
    if not ended or arc or count is None or layer + 1 != count or seen_layers != set(range(count)):
        raise ValueError("ORNL output is incomplete or contains empty deposition layers")
    if expected_height_mm is not None:


        height = max(m.z for m in tp.moves if m.deposit)
        if abs(height - expected_height_mm) > options.layer_height_mm / 2 + .01:
            raise ValueError("ORNL output height does not match the input part; incomplete build extent")
    return tp


@contextmanager
def _engine_lock():


    root = _engine_data_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "waam-ornlslicer-2.1.lock"
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("ORNL is slicing another part; try again when that slice finishes") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("ORNL is slicing another part; try again when that slice finishes") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _engine_data_root() -> Path:
    if os.name == "nt":


        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 26, None, 0, buffer):
            raise ValueError("Could not resolve ORNL's shared application data directory")
        return Path(buffer.value).resolve()
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).resolve()


def _run(command, directory, env, timeout):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, cwd=directory, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, creationflags=flags)
    except OSError:
        raise ValueError("Could not start ORNL; check the configured executable and its dependencies") from None
    log = bytearray()
    exceeded = threading.Event()
    done = threading.Event()

    def drain():
        try:
            while chunk := process.stdout.read1(65536):
                remaining = MAX_OUTPUT_BYTES - len(log)
                log.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()


                    break
        finally:
            done.set()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while True:


            size = sum(p.stat().st_size for p in Path(directory).rglob("*.gcode") if p.is_file())
            if exceeded.is_set() or size + len(log) > MAX_OUTPUT_BYTES:
                raise ValueError("ORNL output exceeds the planning size limit during generation")
            if process.poll() is not None and done.is_set():
                break
            if time.monotonic() >= deadline:
                raise ValueError(f"ORNL exceeded {timeout} seconds; simplify the part or increase layer height")
            time.sleep(.005)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=1)
        process.stdout.close()
    if process.returncode:
        raise ValueError(f"ORNL exited with code {process.returncode}; check the engine installation and STL")
    return log.decode("utf-8", errors="replace")


def slice_mesh(triangles, options: Options, *, artifacts: Path | None = None) -> tuple[Toolpath, dict]:
    tri = validate_mesh(triangles, options)
    binary = executable()
    if binary is None:
        raise ValueError(availability()["message"])
    mesh_data = stl_bytes(tri)
    config = settings(options)
    canonical = json.dumps(config, sort_keys=True)
    job_tag = hashlib.sha256(mesh_data + canonical.encode()).hexdigest()
    config["settings"][0]["start_code"] += f"(WAAM_JOB {job_tag})\n"
    profile = json.dumps(config, sort_keys=True)
    with _engine_lock(), tempfile.TemporaryDirectory(prefix="waam-ornl-") as temporary:
        directory = Path(temporary)
        env = dict(os.environ, OMP_NUM_THREADS="2", QT_QPA_PLATFORM="offscreen")
        if os.name != "nt":

            env.update(XDG_DATA_HOME=str(directory / "data"), XDG_CONFIG_HOME=str(directory / "config"))
        version = _run([str(binary), "--version"], directory, env, 10)
        if not re.search(r"\bversion=2\.1\.0(?:\s|$)", version):
            raise ValueError("This adapter requires ORNLSlicer 2.1.0; configure the pinned release")
        (directory / "part.stl").write_bytes(mesh_data)
        (directory / "profile.s2c").write_text(profile, encoding="utf-8")

        command = [str(binary), "--input_stl_files", str(directory / "part.stl"),
                   "--input_global_settings", str(directory / "profile.s2c"),
                   "--output_location", str(directory / "result"),
                   "--shift_parts_on_load", "false", "--align_parts", "false",
                   "--use_implicit_transforms", "true"]
        log = _run(command, directory, env, TIMEOUT_SECONDS)
        output = directory / "result.gcode"
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_OUTPUT_BYTES:
            raise ValueError("ORNL did not create a usable toolpath; check its writable application cache and STL")
        text = output.read_text(encoding="utf-8")
        tp = parse_output(text, options, expected_job=job_tag,
                          expected_height_mm=float(tri[:, :, 2].max()))
        if artifacts is not None:
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "part.stl").write_bytes(mesh_data)
            (artifacts / "profile.s2c").write_text(profile, encoding="utf-8")
            (artifacts / "ornl-output.gcode").write_text(text, encoding="utf-8")
            (artifacts / "engine.log").write_text(version + "\n" + log, encoding="utf-8")
    deposits = np.array(tp.deposit_points())
    low, high = tri.min(axis=(0, 1)), tri.max(axis=(0, 1))
    if np.any(deposits[:, :2] < low[:2] - .01) or np.any(deposits[:, :2] > high[:2] + .01):
        raise ValueError("ORNL moved the part outside its input frame; check import units and transforms")
    if deposits[:, 2].max() > high[2] + options.layer_height_mm / 2 + .01:
        raise ValueError("ORNL output height does not match the input part")
    lengths = {role: 0.0 for role in ("perimeter", "inset", "infill")}
    for a, b in zip(tp.moves, tp.moves[1:]):
        if b.deposit:
            lengths[b.region] += math.dist(a.target, b.target)
    metadata = {"engine": "ornl", "version": VERSION, "profile_version": PROFILE_VERSION,
                "options": asdict(options), "spacing_mm": options.spacing_mm,
                "job_sha256": job_tag,
                "profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
                "mesh_sha256": hashlib.sha256(mesh_data).hexdigest(),
                "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "region_length_mm": lengths, "input_height_mm": float(high[2]),
                "build_height_mm": float(deposits[:, 2].max()),
                "note": "Geometric bead spacing is a planning input; fusion and weld parameters require cell validation."}
    return tp, metadata


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Configure the ORNL planar slicing engine")
    parser.add_argument("--configure", type=Path, help="Path to ORNLSlicer 2.1.0 CLI executable")
    args = parser.parse_args()
    if args.configure:
        path = args.configure.expanduser().resolve()
        if not path.is_file():
            parser.error("Executable does not exist")
        output = _run([str(path), "--version"], path.parent, os.environ.copy(), 10)
        if not re.search(r"\bversion=2\.1\.0(?:\s|$)", output):
            parser.error("ORNLSlicer 2.1.0 is required")
        CONFIG.write_text(json.dumps({"executable": str(path)}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(availability(), indent=2))


if __name__ == "__main__":
    main()
