from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile

import numpy as np

from . import demos, job_archive
from .slicer import parse_stl, scale_stl_to_mm

MAX_STL_BYTES = 64 * 1024 * 1024
UNITS = frozenset(("auto", "mm", "in", "m"))
_DIGEST = re.compile(r"[0-9a-f]{64}")
_WORKSPACE = re.compile(r"[A-Za-z0-9_-]{1,80}")


def owner_id(email: str) -> str:
    value = str(email).strip().lower()
    if not value or len(value) > 254 or "@" not in value or any(ord(c) < 32 for c in value):
        raise ValueError("A signed-in account is required for the STL library")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def workspace_for(email: str) -> str:
    return "library-" + owner_id(email)


def inspect(body: bytes, units: str = "auto") -> dict:

    if units not in UNITS:
        raise ValueError("STL units must be auto, mm, in, or m")
    if not body or len(body) > MAX_STL_BYTES:
        raise ValueError("Choose a nonempty STL of at most 64 MB")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            tri = parse_stl(body)
            scaled, resolved, factor = scale_stl_to_mm(tri, units)
            raw_size = np.ptp(tri, axis=(0, 1))
            size = np.ptp(scaled, axis=(0, 1))
            if not np.isfinite(size).all():
                raise ValueError("non-finite dimensions")
    except (ValueError, TypeError, ArithmeticError, MemoryError, struct.error) as exc:

        raise ValueError("Could not read this STL. Export a binary or ASCII STL from your CAD.") from exc
    return {"requested_units": units, "resolved_units": resolved, "scale_to_mm": factor,
            "size_mm": [float(v) for v in size], "raw_size": [float(v) for v in raw_size],
            "triangles": len(tri), "estimated": units == "auto"}


def _bundle(root: Path, workspace: str, entry_id: str) -> Path:
    if not _WORKSPACE.fullmatch(workspace) or not _DIGEST.fullmatch(entry_id):
        raise ValueError("Unknown STL library item")
    return Path(root).absolute() / workspace / ".artifacts" / "stl" / entry_id


def _record(root: Path, workspace: str, entry_id: str, email: str | None = None) -> tuple[dict, dict]:
    folder = _bundle(root, workspace, entry_id)
    record = json.loads(job_archive.safe_read(folder / "metadata.json", 16384))
    if not isinstance(record, dict) or record.get("kind") != "stl" or record.get("sha256") != entry_id:
        raise ValueError("STL library metadata failed verification")
    count = record.get("bytes")
    if type(count) is not int or not 0 < count <= MAX_STL_BYTES:
        raise ValueError("STL library file size is invalid")
    info = (folder / "part.stl").lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != count:
        raise ValueError("STL library file failed verification")
    attributes = record.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise ValueError("STL library metadata failed verification")
    owner = None
    units = "auto"
    if workspace.startswith("library-"):
        owner = attributes.get("owner")
        if not isinstance(owner, str) or workspace_for(owner) != workspace or attributes.get("schema") != 1:
            raise ValueError("STL library owner failed verification")
        units = attributes.get("units", "auto")
        if units not in UNITS:
            raise ValueError("STL library units are invalid")
    if email is not None and (workspace != workspace_for(email) or owner != email.strip().lower()):
        raise ValueError("This STL is not in your library")
    entry = {"id": entry_id, "kind": "stl", "name": str(record.get("name") or "part.stl")[:160],
             "bytes": count, "created_at": str(record.get("created_at") or "")[:64],
             "units": units, "owner": owner, "source_owner": workspace,
             "legacy": owner is None}
    return record, entry


def read(root: Path, workspace: str, entry_id: str, *, email: str | None = None) -> tuple[bytes, dict]:
    _, entry = _record(root, workspace, entry_id, email)
    body = job_archive.safe_read(_bundle(root, workspace, entry_id) / "part.stl", MAX_STL_BYTES)
    if len(body) != entry["bytes"] or hashlib.sha256(body).hexdigest() != entry_id:
        raise ValueError("STL library file failed its integrity check. Re-upload the original.")
    return body, entry


def _inspection(body: bytes, units: str) -> dict:
    try:
        return {"inspection": inspect(body, units)}
    except ValueError as exc:
        return {"inspection_error": str(exc)}


def preserve(root: Path, email: str, body: bytes, *, name: str, units: str = "auto") -> dict:
    if units not in UNITS:
        raise ValueError("STL units must be auto, mm, in, or m")
    if not isinstance(name, str) or not name.lower().endswith(".stl"):
        raise ValueError("Choose a file ending in .stl")
    if not body or len(body) > MAX_STL_BYTES:
        raise ValueError("Choose a nonempty STL of at most 64 MB")
    email = email.strip().lower()
    workspace = workspace_for(email)
    path = job_archive.preserve(root, workspace, "stl", body, name=name,
                                attributes={"schema": 1, "owner": email, "units": units})


    return set_units(root, email, path.parent.name, units, body=body)


def set_units(root: Path, email: str, entry_id: str, units: str, *, body: bytes | None = None) -> dict:
    if units not in UNITS:
        raise ValueError("STL units must be auto, mm, in, or m")
    workspace = workspace_for(email)
    if body is None:
        body, _ = read(root, workspace, entry_id, email=email)
    elif hashlib.sha256(body).hexdigest() != entry_id:
        raise ValueError("STL library content failed verification")
    with job_archive._LOCK:
        record, entry = _record(root, workspace, entry_id, email)
        if entry["units"] != units:
            record["attributes"]["units"] = units
            data = json.dumps(record, indent=2, allow_nan=False).encode("utf-8")
            if len(data) > 16384:
                raise ValueError("STL library metadata size limit")
            folder = _bundle(root, workspace, entry_id)
            previous = job_archive.safe_read(folder / "metadata.json", 16384)
            delta = len(data) - len(previous)
            total, _ = job_archive._usage(Path(root))
            own, _ = job_archive._usage(Path(root) / workspace)
            if total + delta > job_archive.MAX_TOTAL_BYTES or own + delta > job_archive.MAX_WORKSPACE_BYTES:
                raise ValueError("STL library storage limit reached")
            job_archive._directory(folder)
            fd, pending = tempfile.mkstemp(prefix=".units-", dir=folder)
            try:
                with os.fdopen(fd, "wb") as out:
                    os.chmod(pending, 0o600)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(pending, folder / "metadata.json")
            finally:
                if os.path.exists(pending):
                    os.unlink(pending)
            entry["units"] = units
    return {**entry, **_inspection(body, units)}


def samples(email: str) -> list[dict]:
    return [{"id": "sample:" + item.key, "kind": "sample", "name": item.label,
             "description": item.blurb, "shape": item.key, "units": "mm",
             "owner": email.strip().lower(), "source_owner": workspace_for(email)}
            for item in demos.DEMOS.values()]


def listing(root: Path, email: str, *, all_users: bool = False) -> dict:

    root = Path(root).absolute()
    own = workspace_for(email)
    workspaces = [own]
    warnings = 0
    if all_users and root.exists():

        for parent in (root, *root.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("STL library path contains a link")
        workspaces = sorted(p.name for p in root.iterdir() if _WORKSPACE.fullmatch(p.name))
    rows = []
    for workspace in workspaces:
        base = root / workspace / ".artifacts" / "stl"

        if not base.exists():
            continue
        for parent in (base, *base.parents):
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("STL library path contains a link")
        for folder in base.iterdir():
            if not _DIGEST.fullmatch(folder.name):
                continue
            if len(rows) >= job_archive.MAX_BUNDLES:
                raise ValueError("STL library inventory limit reached")
            try:
                _, entry = _record(root, workspace, folder.name, None if all_users else email)
                rows.append(entry)
            except (OSError, ValueError, TypeError):
                warnings += 1
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    return {"entries": rows + samples(email), "owner_id": owner_id(email),
            "warnings": warnings, "all_users": all_users}


def copy_into(root: Path, email: str, source_owner: str, entry_id: str) -> dict:
    body, entry = read(root, source_owner, entry_id)
    name = entry["name"]
    if not name.lower().endswith(".stl"):
        name += ".stl"
    return preserve(root, email, body, name=name, units=entry["units"])
