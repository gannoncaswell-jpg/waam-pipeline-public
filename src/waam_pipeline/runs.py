'Named, bounded run records and immutable manifests for the host copy.'
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import arcmon
from . import passes as pass_tables
from .job_archive import safe_read

WORKSPACE_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
RUN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_[A-Z0-9](?:[A-Z0-9-]{0,22}[A-Z0-9])?_r(?:0[1-9]|[1-9][0-9]+)")
ATTACHMENT_EXTENSIONS = frozenset("jpg jpeg png heic webp gif pdf txt csv xlsx xls docx md json mp4 mov dat jbi zip".split())
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RUN_BYTES = 250 * 1024 * 1024
MAX_FILES = 300
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_LOG_INDEX = 2 ** 63 - 1
NO_RUN = "No run is open. A run starts with the plate's first VAR.DAT or the first robot job you export."
FINISHED = "this run is finished; start a new one"
_EXTENSIONS = {
    "WELD-JOB": "JBI", "PLATE-SENSE-JOB": "JBI", "LAYER-TOUCH-JOB": "JBI", "CYCLE-JOB": "JBI",
    "ROS2-JOB": "json", "GCODE": "gcode", "PLATE-CORNERS-VAR": "DAT",
    "PLATE-SENSE-VAR": "DAT", "LAYER-TOUCH-VAR": "DAT", "CYCLE-VAR": "DAT", "PART": "stl",
}
_JBI_KINDS = frozenset(("WELD-JOB", "PLATE-SENSE-JOB", "LAYER-TOUCH-JOB", "CYCLE-JOB"))

_PASS_KINDS = {"WELD-JOB": "JBI", "CYCLE-JOB": "JBI", "ROS2-JOB": "json"}

_MAX_SUMMARY_PASSES = 200
_FIXED_FILES = ("run.json", "files.csv", "temperatures.csv", "README.txt", "SEALED.json")

_SEAL_FILES = (*_FIXED_FILES, "cycles.csv", "layer_log.csv")
_CYCLE_FIELDS = (
    "cycle", "first", "last", "n", "step_mm", "growth_raw_mm", "growth_mm", "clamped",
    "mean_error_mm", "min_error_mm", "max_error_mm", "touches",
    "t_before_value", "t_before_unit", "t_before_at", "t_after_value", "t_after_unit", "t_after_at",
    "exported_at", "imported_at", "job", "interpass_s",
    "bead_height_mm", "bead_width_mm", "travel_mm_min", "wire_feed_m_min", "weld_ctwd_mm",
)
_STAGES = frozenset(("before-interpass", "after-interpass", "before-weld", "after-weld", "other"))
_PRIVATE_KEYS = frozenset(("k", "token", "session", "password", "sessionid", "accountpassword",
                           "accesstoken", "refreshtoken", "idtoken", "authorization", "cookie", "passwordhash"))
_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_ID_LOCK = threading.Lock()


def _workspace(ws: str) -> None:
    if not isinstance(ws, str) or not WORKSPACE_RE.fullmatch(ws):
        raise ValueError("invalid workspace")


def _run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    try:
        dt.date.fromisoformat(run_id[:10])
    except ValueError as exc:
        raise ValueError("invalid run id") from exc


def _lock(ws: str) -> threading.RLock:
    _workspace(ws)
    with _LOCK_GUARD:
        return _LOCKS.setdefault(ws, threading.RLock())


def _no_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("run path contains a link")


def _base(root: Path, ws: str) -> Path:
    _workspace(ws)
    root = Path(root).absolute()
    path = root / ws / "_runs"
    _no_links(path)
    resolved = path.resolve()
    if resolved.parent.parent != root.resolve() or resolved.parent.name != ws:
        raise ValueError("invalid run path")
    return resolved


def _folder(root: Path, ws: str, run_id: str) -> Path:
    _workspace(ws)
    _run_id(run_id)
    base = _base(root, ws)
    path = base / run_id
    _no_links(path)
    if path.resolve().parent != base:
        raise ValueError("invalid run path")
    return path


def _now(now=None) -> dt.datetime:
    value = now() if callable(now) else now
    if value is None:
        try:
            return dt.datetime.now(ZoneInfo(os.environ.get("WAAM_RUN_TZ", "America/Chicago")))
        except ZoneInfoNotFoundError:
            return dt.datetime.now().astimezone()
    if not isinstance(value, dt.datetime):
        raise ValueError("now must be a datetime or a clock")
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _json(value) -> bytes:
    try:
        return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except TypeError as exc:
        raise ValueError("run metadata must be JSON serializable") from exc


def _read(path: Path) -> dict:
    value = json.loads(safe_read(path, MAX_FILE_BYTES))
    if not isinstance(value, dict):
        raise ValueError("invalid run metadata")
    return value


def _atomic(path: Path, body: bytes) -> None:
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.chmod(temporary, 0o600)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _usage(path: Path) -> tuple[int, int]:
    size = count = 0
    _no_links(path)
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            _no_links(Path(parent) / name)
        for name in files:
            info = (Path(parent) / name).stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("runs must contain regular, unlinked files")
            size += info.st_size
            count += 1
    return size, count


def _capacity(base: Path, folder: Path, changes: dict[str, bytes], extra_bytes: int = 0) -> None:
    run_bytes, count = _usage(folder)
    workspace_bytes, _ = _usage(base)
    delta = 0
    names = {p.name for p in folder.iterdir()} if folder.exists() else set()
    for name, body in changes.items():
        if len(body) > MAX_FILE_BYTES:
            raise ValueError("single file limit: 25 MB")
        path = folder / name
        delta += len(body) - (path.stat().st_size if path.exists() else 0)
        if name not in names:
            count += 1
        names.add(name)
    reserved = sum(f"{folder.name}_{suffix}" not in names for suffix in _SEAL_FILES)
    if count + reserved > MAX_FILES:
        raise ValueError("run file count limit: 300 files")
    seal_bytes = _seal_reserve(folder, changes, names)
    if run_bytes + delta + seal_bytes > MAX_RUN_BYTES:
        raise ValueError("run storage limit: 250 MB")
    if workspace_bytes + delta + extra_bytes + seal_bytes > MAX_WORKSPACE_BYTES:
        raise ValueError("workspace runs storage limit: 2 GB")


def _seal_reserve(folder: Path, changes: dict[str, bytes], names: set[str]) -> int:
    run_id = folder.name
    metadata = changes[f"{run_id}_run.json"]
    record = json.loads(metadata)
    if record["status"] == "finished":
        return 0
    derived = {
        f"{run_id}_files.csv": _csv(("name", "kind", "sha256", "bytes", "added_at", "source"), record["files"]),
        f"{run_id}_temperatures.csv": _csv(("at", "value", "unit", "value_c", "stage", "note", "cycle"), record["temperatures"]),
        f"{run_id}_layer_log.csv": _layer_log_csv(record),
        f"{run_id}_cycles.csv": _cycles_csv(record, folder, changes),
        f"{run_id}_README.txt": _readme(record, folder, changes),
    }
    if any(len(body) > MAX_FILE_BYTES for body in derived.values()):
        raise ValueError("single file limit: 25 MB including seal summaries")
    if len(metadata) + 32768 > MAX_FILE_BYTES or len(derived[f"{run_id}_README.txt"]) + 32768 > MAX_FILE_BYTES:
        raise ValueError("single file limit: 25 MB including Finish run notes")
    inventory = []
    for name in sorted(names | set(derived)):
        size = (len(derived[name]) if name in derived else len(changes[name]) if name in changes
                else (folder / name).stat().st_size)
        inventory.append({"name": name, "sha256": "0" * 64, "bytes": size})
    derived[f"{run_id}_SEALED.json"] = _json({"run": run_id, "sealed_at": record["started_at"], "files": inventory})

    return 65536 + sum(len(body) - ((folder / name).stat().st_size if (folder / name).exists() else 0)
                       for name, body in derived.items())


def _text(value, label: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label} limit: {maximum} characters")

    if "\x00" in value:
        raise ValueError(f"{label} cannot contain a NUL character")
    return value


def _finite(value) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def _basename(value: str) -> str:
    if not isinstance(value, str) or value in (".", "..") or any(c in value for c in "/\\:\x00"):
        raise ValueError("invalid file name")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid file name")
    return value


def _part(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")[:24].rstrip("-") or "PART"


def _detail(value: str, *, cycle_job: bool = False) -> str:
    pattern = r"[^A-Za-z0-9._-]+" if cycle_job else r"[^A-Za-z0-9.-]+"
    return re.sub(pattern, "-", _basename(value))[:40]


def _extension(kind: str, ext: str) -> str:
    if not isinstance(ext, str) or not re.fullmatch(r"\.?[A-Za-z0-9]+", ext):
        raise ValueError("invalid file extension")
    ext = ext.lstrip(".")
    if kind in _EXTENSIONS:
        if ext.lower() != _EXTENSIONS[kind].lower():
            raise ValueError("invalid file extension for kind")
        return _EXTENSIONS[kind]
    if kind == "ATTACH" and ext.lower() in ATTACHMENT_EXTENSIONS:
        return ext
    if kind == "ARCMON" and ext.lower() in ("csv", "dat"):
        return ext
    raise ValueError("attachment extension limit: extension is not allowed")


def valid_file_name(run_id: str, name: str) -> bool:
    try:
        _run_id(run_id)
        _basename(name)
    except ValueError:
        return False
    if name in {f"{run_id}_{suffix}" for suffix in _SEAL_FILES}:
        return True

    for suffix, parents in ((".passes.csv", _PASS_KINDS), (".passes.json", _PASS_KINDS),
                            (".arcmon-passes.csv", {"ARCMON": "csv"})):
        if name.endswith(suffix):
            stem = name[:-len(suffix)]
            return any(re.fullmatch(re.escape(run_id) + rf"_\d{{2,3}}_{re.escape(kind)}(?:_.+)?", stem)
                       and valid_file_name(run_id, f"{stem}.{ext}") for kind, ext in parents.items())
    kinds = "|".join((*_EXTENSIONS, "ARCMON", "ATTACH"))
    match = re.fullmatch(re.escape(run_id) + rf"_(\d{{2,3}})_({kinds})(?:_([A-Za-z0-9._-]{{1,40}}))?\.([A-Za-z0-9]+)", name)
    if not match or int(match[1]) == 0 or match[1] != f"{int(match[1]):02d}":
        return False
    if match[2] != "CYCLE-JOB" and "_" in (match[3] or ""):
        return False
    if match[2] in _JBI_KINDS and match[4] == "json":
        return True
    try:
        return _extension(match[2], match[4]) == match[4]
    except ValueError:
        return False


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()
                if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def layer_log(record: dict) -> list[dict]:

    log = record.get("layer_log", []) if isinstance(record, dict) else []
    if not isinstance(log, list):
        raise ValueError("invalid run layer_log")
    for index, row in enumerate(log, 1):
        if not isinstance(row, dict):
            raise ValueError(f"invalid run layer_log row {index}")
        pass_number = row.get("pass")
        if pass_number is None:
            continue
        if row.get("pass_base") != 1:
            raise ValueError(
                f"Run layer_log row {index} has pass {pass_number!r} but no pass_base: 1 "
                "marker; refusing to guess pass numbering")
        if type(pass_number) is not int or pass_number < 1:
            raise ValueError(
                f"Run layer_log row {index} has invalid 1-based pass {pass_number!r}")
    return log


def _active(root: Path, ws: str) -> dict | None:
    path = _base(root, ws) / "active.json"
    if not path.exists():
        return None
    run_id = _read(path).get("run")
    folder = _folder(root, ws, run_id)
    record = _read(folder / f"{run_id}_run.json")
    if record.get("run") != run_id or record.get("workspace") != ws:
        raise ValueError("invalid run metadata")
    record.setdefault("layer_log", [])
    layer_log(record)
    return record


def active(root: Path, ws: str) -> dict | None:
    with _lock(ws):
        record = _active(root, ws)
        return None if record is not None and _sealed(root, ws, record) else record


def _sealed(root: Path, ws: str, record: dict) -> bool:
    return (_folder(root, ws, record["run"]) / f"{record['run']}_SEALED.json").exists()


def _open(root: Path, ws: str) -> tuple[dict, Path]:
    record = _active(root, ws)
    if record is None:
        raise ValueError(NO_RUN)
    folder = _folder(root, ws, record["run"])
    if (folder / f"{record['run']}_SEALED.json").exists() or record.get("status") != "open":
        raise ValueError(FINISHED)
    return record, folder


def _audit(record: dict, actor, action: str, now=None) -> None:

    if actor is not None:
        actor = _text(actor, "audit actor", 320)
        record.setdefault("audit", []).append({"actor": actor, "action": action, "at": _now(now).isoformat()})


def _start(root: Path, ws: str, *, operator, name, part_hint, now, commit,
           setup=None, collaborators=None, audit_actor=None) -> dict:
    name = _text("" if name is None else name, "run name", 40)
    _basename(name)
    operator = _text(operator, "operator", 320)
    if not isinstance(part_hint, str):
        raise ValueError("invalid part hint")
    previous = _active(root, ws)
    if previous is not None:
        if not _sealed(root, ws, previous):
            raise ValueError("A run is already open; finish it before starting another.")
        (_base(root, ws) / "active.json").unlink()
    part = _part(name or part_hint)
    moment = _now(now)
    prefix = f"{moment.date().isoformat()}_{part}_r"
    base = _base(root, ws)
    with _ID_LOCK:
        used = set()
        root_path = Path(root).absolute()
        _no_links(root_path)
        if root_path.exists():
            for workspace in root_path.iterdir():
                if WORKSPACE_RE.fullmatch(workspace.name):
                    other = _base(root, workspace.name)
                    if other.exists():
                        used.update(p.name for p in other.iterdir() if RUN_RE.fullmatch(p.name))
        number = 1
        while f"{prefix}{number:02d}" in used:
            number += 1
        run_id = f"{prefix}{number:02d}"
        folder = _folder(root, ws, run_id)
        record = {"schema": "waam-run/1", "run": run_id, "status": "open", "outcome": None,
                  "workspace": ws, "operator": operator, "part": part, "name": name or None,
                  "started_at": moment.isoformat(), "finished_at": None,
                  "commit": commit or os.environ.get("WAAM_COMMIT") or None,
                  "cell": "lc50s", "data_class": "preliminary", "files": [],
                  "temperatures": [], "layer_log": [], "notes": [], "measurements": None,
                  "arc_monitor": {"files": [], "summaries": [], "waiver": None}, "cycles": []}
        if setup is not None:
            record["setup"] = setup
        if collaborators is not None:
            record["collaborators"] = collaborators
        _audit(record, audit_actor, "create", moment)
        metadata_name = f"{run_id}_run.json"
        pointer = _json({"run": run_id})
        body = _json(record)
        _capacity(base, folder, {metadata_name: body}, len(pointer))
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            _atomic(folder / metadata_name, body)
            _atomic(base / "active.json", pointer)
        except (OSError, ValueError):
            (folder / metadata_name).unlink(missing_ok=True)
            folder.rmdir()
            raise
        return record


def start(root: Path, ws: str, *, operator, name=None, part_hint="", now=None, commit=None,
          setup=None, collaborators=None, audit_actor=None) -> dict:
    with _lock(ws):
        return _start(root, ws, operator=operator, name=name, part_hint=part_hint, now=now, commit=commit,
                      setup=setup, collaborators=collaborators, audit_actor=audit_actor)


def _save(root: Path, ws: str, record: dict, folder: Path) -> None:
    filename = f"{record['run']}_run.json"
    body = _json(record)
    _capacity(_base(root, ws), folder, {filename: body})
    _atomic(folder / filename, body)


def _latest_passes(record: dict, folder: Path, job: str | None) -> tuple[str, list] | None:


    tables = [item["name"] for item in record["files"] if item["name"].endswith(".passes.json")
              and re.search(r"_\d{2,3}_(WELD|CYCLE)-JOB", item["name"])]
    for name in reversed(tables):
        data = json.loads(safe_read(folder / name, MAX_FILE_BYTES))
        if not job or data.get("job") == job:
            return name, data.get("passes") or []
    if tables:
        data = json.loads(safe_read(folder / tables[-1], MAX_FILE_BYTES))
        return tables[-1], data.get("passes") or []
    return None


def add_file(root: Path, ws: str, *, kind, body, detail="", ext, source, sidecar=None,
             operator="", part_hint="", start_if_none=False, now=None, name=None,
             commit=None, arcmon_summary=None, passes=None, audit_actor=None) -> str | None:


    _workspace(ws)
    if not isinstance(kind, str) or kind not in {*_EXTENSIONS, "ARCMON", "ATTACH"}:
        raise ValueError("invalid run file kind")
    if not isinstance(body, bytes) or len(body) > MAX_FILE_BYTES:
        raise ValueError("single file limit: 25 MB")
    if source not in ("workbench", "robot", "operator"):
        raise ValueError("invalid run file source")
    if sidecar is not None and not isinstance(sidecar, dict):
        raise ValueError("invalid job sidecar")
    detail = _detail(detail, cycle_job=kind == "CYCLE-JOB")
    extension = _extension(kind, ext)
    moment = _now(now)
    with _lock(ws):
        record = _active(root, ws)
        if record is None or (start_if_none and _sealed(root, ws, record)):
            if not start_if_none:
                return None
            record = _start(root, ws, operator=operator, name=name, part_hint=part_hint,
                            now=moment, commit=commit or (sidecar or {}).get("commit"))
        record, folder = _open(root, ws)
        digest = hashlib.sha256(body).hexdigest()
        if kind == "PART":
            for item in record["files"]:
                if item["kind"] == "PART" and item["sha256"] == digest:
                    return item["name"]
        sequence = 1 + max((int(item["name"][len(record["run"]) + 1:].split("_", 1)[0])
                            for item in record["files"]), default=0)
        stem = f"{record['run']}_{sequence:02d}_{kind}" + (f"_{detail}" if detail else "")
        filename = f"{stem}.{extension}"
        changes = {filename: body}
        if kind in _JBI_KINDS:
            sidecar = sidecar or {}
            metadata = {"kind": kind, "job": sidecar.get("job", detail),
                        "exported_at": moment.isoformat(), "commit": sidecar.get("commit", record["commit"]),
                        "params": _clean(sidecar.get("params", {})), "query": _clean(sidecar.get("query", {})),
                        "summary": _clean(sidecar.get("summary")), "sha256": digest}
            measure_stops = sidecar.get("measure_stops")
            if measure_stops is None and isinstance(metadata["summary"], dict):
                measure_stops = metadata["summary"].get("measure_stops")
            if measure_stops is not None:
                metadata["measure_stops"] = _clean(measure_stops)
            if kind == "CYCLE-JOB":
                metadata.update(cycle=_clean(sidecar.get("cycle")), stations=_clean(sidecar.get("stations", [])))
            if passes is not None and kind in _PASS_KINDS:
                metadata.update(passes={**_clean(sidecar.get("passes") or {}),
                                        "csv": f"{stem}.passes.csv", "json": f"{stem}.passes.json"})
            changes[f"{stem}.json"] = _json(metadata)
        derived = {}
        if passes is not None and kind in _PASS_KINDS:
            rows = _clean(list(passes))
            job = (sidecar or {}).get("job", detail)
            derived[f"{stem}.passes.csv"] = pass_tables.to_csv(rows)
            derived[f"{stem}.passes.json"] = pass_tables.to_json(rows, run=record["run"], job=job,
                                                                 job_file=filename)
        if kind == "ARCMON" and (arcmon_summary or {}).get("kind") == "realtime-csv":
            try:
                found = _latest_passes(record, folder, arcmon_summary.get("job"))
                joined = None if found is None else arcmon.per_pass(body, found[1])
            except (ValueError, OSError) as exc:

                found, joined = None, None
                arcmon_summary = {**arcmon_summary, "passes": {"error": str(exc)[:300]}}
            if joined is not None:
                derived[f"{stem}.arcmon-passes.csv"] = arcmon.passes_csv(joined["rows"])
                shown = joined["rows"][:_MAX_SUMMARY_PASSES]
                arcmon_summary = {**arcmon_summary, "passes": {
                    **joined, "rows": shown, "rows_total": len(joined["rows"]),
                    "table": found[0], "file": f"{stem}.arcmon-passes.csv"}}
        changes.update(derived)
        for stored, data in changes.items():
            if not valid_file_name(record["run"], stored) or (folder / stored).exists():
                raise ValueError("invalid or existing run file name")
            stored_kind = ("ARCMON-PASSES" if stored.endswith(".arcmon-passes.csv")
                           else "PASSES" if stored in derived else kind)
            record["files"].append({"name": stored, "kind": stored_kind, "sha256": hashlib.sha256(data).hexdigest(),
                                    "bytes": len(data), "added_at": moment.isoformat(), "source": source})
        if kind == "ARCMON":
            record["arc_monitor"]["files"].append(filename)
            if arcmon_summary is not None:
                record["arc_monitor"]["summaries"].append(_clean(arcmon_summary))
        _audit(record, audit_actor, "add", moment)
        metadata_name = f"{record['run']}_run.json"
        changes[metadata_name] = _json(record)
        _capacity(_base(root, ws), folder, changes)
        written = []
        try:
            for stored, data in changes.items():
                _atomic(folder / stored, data)
                if stored != metadata_name:
                    written.append(folder / stored)
        except (OSError, ValueError):
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return filename


def add_temperature(root: Path, ws: str, *, value, unit, stage, note="", cycle=None, now=None,
                    audit_actor=None) -> dict:
    if not isinstance(unit, str) or unit not in ("F", "C") or not isinstance(stage, str) or stage not in _STAGES:
        raise ValueError("invalid temperature unit or stage")
    if not _finite(value):
        raise ValueError("temperature must be finite")
    if not -40 <= value <= (2500 if unit == "F" else 1400):
        raise ValueError("temperature limit: -40..2500 F or -40..1400 C")
    if cycle is not None and (type(cycle) is not int or not 1 <= cycle <= MAX_LOG_INDEX):
        raise ValueError("cycle must be a positive whole number within the supported range")
    note = _text(note, "temperature note", 200)
    with _lock(ws):
        record, folder = _open(root, ws)
        record["temperatures"].append({"at": _now(now).isoformat(), "value": value, "unit": unit,
                                       "value_c": (value - 32) * 5 / 9 if unit == "F" else value,
                                       "stage": stage, "note": note, "cycle": cycle})
        _audit(record, audit_actor, "temperature", now)
        _save(root, ws, record, folder)
        return record


def set_cycle(root: Path, ws: str, record: dict) -> dict:
    if not isinstance(record, dict) or type(record.get("cycle")) is not int or record["cycle"] < 1:
        raise ValueError("cycle must be a positive whole number")
    item = json.loads(_json(_clean(record)))
    with _lock(ws):
        run, folder = _open(root, ws)
        cycles = run.setdefault("cycles", [])
        index = next((i for i, old in enumerate(cycles) if old["cycle"] == item["cycle"]), len(cycles))
        if index == len(cycles):
            cycles.append(item)
        else:
            cycles[index] = item
        _save(root, ws, run, folder)
        return run


def set_layer_row(root: Path, ws: str, row: dict, *, now=None, audit_actor=None) -> dict:

    fields = {"layer", "pass", "pass_base", "unit", "pre_weld", "readings", "park_travel_s_est",
              "height_mm", "height_source", "width_mm", "current_a", "voltage_v", "note", "at"}
    if not isinstance(row, dict) or set(row) - fields:
        raise ValueError("invalid layer row")
    layer, pass_number = row.get("layer"), row.get("pass")
    if type(layer) is not int or not 0 <= layer <= MAX_LOG_INDEX:
        raise ValueError("layer must be a nonnegative whole number within the supported range")
    if pass_number is not None and (type(pass_number) is not int or not 1 <= pass_number <= MAX_LOG_INDEX):
        raise ValueError("pass must be a whole number >= 1 within the supported range")
    if row.get("pass_base", 1) != 1:
        raise ValueError("pass_base must be 1")
    unit = row.get("unit")
    if not isinstance(unit, str) or unit not in ("F", "C"):
        raise ValueError("invalid temperature unit")
    item = {"layer": layer, "pass": pass_number, "unit": unit}
    if pass_number is not None:
        item["pass_base"] = 1
    for key, low, high in (("pre_weld", -50, 2000), ("park_travel_s_est", 0, 7200),
                           ("height_mm", 0, 200), ("width_mm", 0, 200),
                           ("current_a", 0, 2000), ("voltage_v", 0, 200)):
        if key in ("current_a", "voltage_v") and key not in row:
            continue
        value = row.get(key)
        if value is not None and (not _finite(value) or not low <= value <= high):
            raise ValueError(f"{key} must be finite and in {low}..{high}")
        item[key] = float(value) if value is not None else None
    readings = row.get("readings", {})
    if not isinstance(readings, dict):
        raise ValueError("readings must be a seconds-to-temperature object")
    item["readings"] = {}
    for key, value in readings.items():
        if not isinstance(key, str) or not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", key):
            raise ValueError("reading seconds must be numeric keys in 0..7200")
        seconds = float(key)
        if not math.isfinite(seconds) or not 0 <= seconds <= 7200:
            raise ValueError("reading seconds must be numeric keys in 0..7200")
        canonical = str(int(seconds)) if seconds.is_integer() else str(seconds)
        if canonical in item["readings"]:
            raise ValueError("reading seconds must be distinct")
        if not _finite(value) or not -50 <= value <= 2000:
            raise ValueError("reading temperature must be finite and in -50..2000")
        item["readings"][canonical] = float(value)
    source = row.get("height_source")
    if source is not None and (not isinstance(source, str) or source not in ("sense", "manual")):
        raise ValueError("invalid height source")
    item.update(height_source=source, note=_text(row.get("note", ""), "layer note", 200), at=_now(now).isoformat())
    with _lock(ws):
        record, folder = _open(root, ws)
        rows = record["layer_log"]
        index = next((i for i, old in enumerate(rows)
                      if (old["layer"], old.get("pass")) == (layer, pass_number)), len(rows))
        if index == len(rows):
            rows.append(item)
        else:
            rows[index] = {**rows[index], **item}
        _audit(record, audit_actor, "layer-row", now)
        _save(root, ws, record, folder)
        return record


def add_note(root: Path, ws: str, *, text, now=None, audit_actor=None) -> dict:
    text = _text(text, "note", 2000)
    with _lock(ws):
        record, folder = _open(root, ws)
        record["notes"].append({"at": _now(now).isoformat(), "text": text})
        _audit(record, audit_actor, "note", now)
        _save(root, ws, record, folder)
        return record


def _csv(fields, rows) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _cycles_csv(record: dict, folder: Path, changes: dict[str, bytes] | None = None) -> bytes:
    rows = []
    files = {item["name"] for item in record["files"]}
    for cycle in record.get("cycles", []):
        info = {**(cycle.get("summary") or {}), **cycle}
        row = {key: info.get(key) for key in _CYCLE_FIELDS}
        first, last = cycle.get("layers", (None, None))
        row.update(first=first, last=last, n=last - first + 1 if first is not None and last is not None else None)
        error_range = info.get("error_range_mm") or (None, None)
        row.update(min_error_mm=error_range[0], max_error_mm=error_range[1])
        for label, stage in (("t_before", "before-interpass"), ("t_after", "after-interpass")):
            temperature = cycle.get(label) or next((reading for reading in reversed(record["temperatures"])
                                                   if reading.get("cycle") == cycle["cycle"] and reading["stage"] == stage), {})
            for key in ("value", "unit", "at"):
                row[f"{label}_{key}"] = temperature.get(key)
        sidecar_name = str(Path(cycle.get("file", "")).with_suffix(".json")) if cycle.get("file") else ""
        sidecar = {}
        if sidecar_name in files:
            sidecar = (json.loads(changes[sidecar_name]) if changes and sidecar_name in changes
                       else _read(folder / sidecar_name))
        params = sidecar.get("params") or {}
        settings = {"bead_height_mm": params.get("lh"), "bead_width_mm": params.get("bead"),
                    "travel_mm_min": params.get("travel"),
                    "wire_feed_m_min": (sidecar.get("summary") or {}).get("wfs_m_min"),
                    "weld_ctwd_mm": (sidecar.get("summary") or {}).get("weld_ctwd_mm")}
        settings.update(cycle.get("build_settings") or {})
        row.update({key: settings.get(key) for key in ("bead_height_mm", "bead_width_mm", "travel_mm_min",
                                                        "wire_feed_m_min", "weld_ctwd_mm")})
        rows.append(row)
    return _csv(_CYCLE_FIELDS, rows)


def _layer_log_csv(record: dict) -> bytes:
    log = layer_log(record)
    seconds = sorted({key for row in log for key in row["readings"]}, key=float)
    fields = ["layer", "pass", "pass_base", "unit", "pre_weld", "pre_weld_value_c", "park_travel_s_est",
              "height_mm", "height_source", "width_mm", "note", "at"]
    fields.extend(("current_a", "voltage_v"))
    fields.extend(field for key in seconds for field in (f"reading_{key}_s", f"reading_{key}_s_value_c"))
    rows = []
    for item in log:
        row = {key: item.get(key) for key in fields[:14]}
        value = item["pre_weld"]
        row["pre_weld_value_c"] = ((value - 32) * 5 / 9 if item["unit"] == "F" else value) if value is not None else None
        for key, value in item["readings"].items():
            row[f"reading_{key}_s"] = value
            row[f"reading_{key}_s_value_c"] = (value - 32) * 5 / 9 if item["unit"] == "F" else value
        rows.append(row)
    return _csv(fields, rows)


def _readme(record: dict, folder: Path, changes: dict[str, bytes] | None = None) -> bytes:
    lines = [f"Run {record['run']}", f"Operator: {record['operator']}",
             f"Started: {record['started_at']}", f"Finished: {record['finished_at']}",
             f"Part: {record['part']}", f"Outcome: {record['outcome']}",
             f"Data class: {record['data_class']}", "", "Settings of the last weld export:"]
    weld = next((item for item in reversed(record["files"])
                 if item["kind"] in ("WELD-JOB", "CYCLE-JOB") and item["name"].endswith(".json")), None)
    if weld:
        settings = (json.loads(changes[weld["name"]]) if changes and weld["name"] in changes
                    else _read(folder / weld["name"]))
        ctwd = (settings.get("summary") or {}).get("weld_ctwd_mm")
        if isinstance(ctwd, (int, float)):
            lines.append(f"Weld CTWD: {ctwd:g} mm ({ctwd / 25.4:.3f} in)")
        lines.extend((json.dumps(settings["params"], indent=2, ensure_ascii=False),
                      json.dumps(settings["query"], indent=2, ensure_ascii=False)))
    else:
        lines.append("No weld job was exported.")
    lines.extend(("", "Files in arrival order:"))
    lines.extend(f"{item['name']} ({item['kind']}, {item['bytes']} bytes)" for item in record["files"])
    lines.extend(("", "Temperatures:"))
    lines.extend(f"{item['at']}: {item['value']} {item['unit']} ({item['value_c']:.2f} C), "
                 f"{item['stage']}; {item['note']}" for item in record["temperatures"])
    lines.extend(("", "Arc monitor:"))
    lines.extend(record["arc_monitor"]["files"])
    waiver = record["arc_monitor"]["waiver"]
    if waiver:
        lines.append(f"Waiver by {waiver['by']} at {waiver['at']}: {waiver['reason']}")
    elif not record["arc_monitor"]["files"]:
        lines.append("Not run; no arc was struck.")
    if record["measurements"]:
        lines.extend(("", "Measurements (mm): " + json.dumps(record["measurements"])))
    if record["notes"]:
        lines.extend(("", "Notes:"))
        lines.extend(f"{item['at']}: {item['text']}" for item in record["notes"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def finish(root: Path, ws: str, *, outcome, data_class="preliminary", measurements=None,
           notes="", waiver=None, by="", now=None, audit_actor=None) -> dict:
    if outcome not in ("completed", "stopped", "not-run"):
        raise ValueError("invalid run outcome")
    if data_class not in ("preliminary", "controlled"):
        raise ValueError("invalid data class")
    by = _text(by, "operator", 320)
    notes = _text(notes, "finish notes", 2000)
    if measurements is not None:
        if not isinstance(measurements, dict) or set(measurements) - {"height_mm", "width_mm"}:
            raise ValueError("invalid measurements")
        measurements = {key: measurements.get(key) for key in ("height_mm", "width_mm")}
        for value in measurements.values():
            if value is not None and (not _finite(value) or value < 0):
                raise ValueError("measurements must be finite nonnegative millimetres")
    if waiver is not None and waiver is not False and waiver is not True and not isinstance(waiver, dict):
        raise ValueError("invalid arc monitor waiver")
    moment = _now(now).isoformat()
    waiver_record = None
    if waiver is True or isinstance(waiver, dict):
        reason = waiver.get("reason") if isinstance(waiver, dict) else ""
        waiver_record = {"by": by, "at": moment,
                         "reason": _text(reason or "Arc monitor export not set up yet", "waiver reason", 2000)}
    with _lock(ws):
        record, folder = _open(root, ws)
        if outcome != "not-run" and not record["arc_monitor"]["files"] and waiver_record is None:
            raise ValueError("Finish run requires an arc monitor export or a recorded waiver.")
        record.update(status="finished", outcome=outcome, data_class=data_class,
                      measurements=measurements, finished_at=moment)
        record["arc_monitor"]["waiver"] = waiver_record
        if notes:
            record["notes"].append({"at": moment, "text": notes})
        _audit(record, audit_actor, "finish", now)
        run_id = record["run"]


        for stale in folder.glob(".pending-*"):
            if stale.is_file() and not stale.is_symlink():
                stale.unlink()
        original_metadata = safe_read(folder / f"{run_id}_run.json", MAX_FILE_BYTES)
        changes = {f"{run_id}_run.json": _json(record),
                   f"{run_id}_files.csv": _csv(("name", "kind", "sha256", "bytes", "added_at", "source"), record["files"]),
                   f"{run_id}_temperatures.csv": _csv(("at", "value", "unit", "value_c", "stage", "note", "cycle"), record["temperatures"]),
                   f"{run_id}_layer_log.csv": _layer_log_csv(record),
                   f"{run_id}_cycles.csv": _cycles_csv(record, folder),
                   f"{run_id}_README.txt": _readme(record, folder)}
        names = {path.name for path in folder.iterdir()} | set(changes)
        manifest_files = []
        for filename in sorted(names):
            if not valid_file_name(run_id, filename):
                raise ValueError("invalid file in run")
            body = changes[filename] if filename in changes else safe_read(folder / filename, MAX_FILE_BYTES)
            manifest_files.append({"name": filename, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
        changes[f"{run_id}_SEALED.json"] = _json({"run": run_id, "sealed_at": moment, "files": manifest_files})
        _capacity(_base(root, ws), folder, changes)
        written = []
        try:
            for filename, body in changes.items():
                _atomic(folder / filename, body)
                written.append(filename)
        except (OSError, ValueError):
            if not (folder / f"{run_id}_SEALED.json").exists():
                for filename in written:
                    if filename != f"{run_id}_run.json":
                        (folder / filename).unlink(missing_ok=True)
                if f"{run_id}_run.json" in written:
                    _atomic(folder / f"{run_id}_run.json", original_metadata)
            raise
        (_base(root, ws) / "active.json").unlink()
        return record


def summary(root: Path, ws: str) -> dict:
    with _lock(ws):
        base = _base(root, ws)
        recent = []
        if base.exists():
            for candidate in base.iterdir():
                if not RUN_RE.fullmatch(candidate.name):
                    continue
                folder = _folder(root, ws, candidate.name)
                if not (folder / f"{candidate.name}_SEALED.json").exists():
                    continue
                record = _read(folder / f"{candidate.name}_run.json")
                record.setdefault("layer_log", [])
                layer_log(record)
                ack_file = base / ".acks" / f"{candidate.name}.json"
                record["ack"] = _read(ack_file) if ack_file.exists() else None
                recent.append(record)
        recent.sort(key=lambda record: dt.datetime.fromisoformat(record["finished_at"]), reverse=True)
        current = _active(root, ws)
        if current is not None and _sealed(root, ws, current):
            current = None
        return {"active": current, "recent": recent[:10]}


def sealed_runs(root: Path) -> list[dict]:
    root = Path(root).absolute()
    _no_links(root)
    result = []
    if not root.exists():
        return result
    for workspace in sorted(root.iterdir()):
        if not WORKSPACE_RE.fullmatch(workspace.name):
            continue
        with _lock(workspace.name):
            base = _base(root, workspace.name)
            if not base.exists():
                continue
            for candidate in sorted(base.iterdir()):
                if not RUN_RE.fullmatch(candidate.name):
                    continue
                folder = _folder(root, workspace.name, candidate.name)
                seal = folder / f"{candidate.name}_SEALED.json"
                if seal.exists():
                    manifest = _read(seal)
                    if manifest.get("run") != candidate.name:
                        raise ValueError("invalid sealed run manifest")
                    result.append({"workspace": workspace.name, "run": candidate.name,
                                   "sealed_at": manifest["sealed_at"], "files": manifest["files"]})
    return result


def ack(root: Path, ws: str, run_id: str, *, to, now=None) -> dict:
    _workspace(ws)
    _run_id(run_id)
    to = _text(to, "copy destination label", 200)
    with _lock(ws):
        folder = _folder(root, ws, run_id)
        if not (folder / f"{run_id}_SEALED.json").exists():
            raise ValueError("only sealed runs can be acknowledged")
        record = {"run": run_id, "to": to, "at": _now(now).isoformat()}
        base = _base(root, ws)
        target = base / ".acks" / f"{run_id}.json"
        _no_links(target)
        if target.exists():
            existing = _read(target)
            if existing.get("run") == run_id and existing.get("to") == to:
                return existing
        body = _json(record)
        size, _ = _usage(base)
        if size + len(body) - (target.stat().st_size if target.exists() else 0) > MAX_WORKSPACE_BYTES:
            raise ValueError("workspace runs storage limit: 2 GB")
        _atomic(target, body)
        return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    default = Path(__file__).resolve().parents[2] / "saves"
    manifest_parser = commands.add_parser("manifest")
    manifest_parser.add_argument("--saves", type=Path, default=default)
    ack_parser = commands.add_parser("ack")
    ack_parser.add_argument("workspace")
    ack_parser.add_argument("run")
    ack_parser.add_argument("--to", required=True)
    ack_parser.add_argument("--saves", type=Path, default=default)
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            records = [record for record in sealed_runs(args.saves)
                       if _read(_folder(args.saves, record["workspace"], record["run"]) /
                                f"{record['run']}_run.json")["outcome"] != "not-run"]
            print(json.dumps(records, allow_nan=False))
        else:
            ack(args.saves, args.workspace, args.run, to=args.to)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
