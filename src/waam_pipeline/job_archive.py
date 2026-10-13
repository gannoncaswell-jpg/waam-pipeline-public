from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_WORKSPACE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_BUNDLES = 5000
_LOCK = threading.Lock()
_FILENAMES = {"stl": "part.stl", "gcode": "part.gcode", "jbi": "job.JBI", "ros2": "job.ros2.json",
              "vardat": "VAR.DAT"}


def safe_read(path: Path, limit: int) -> bytes:

    return safe_read_stat(path, limit)[0]


def safe_read_stat(path: Path, limit: int) -> tuple[bytes, os.stat_result]:

    path = Path(path).absolute()
    directory_fd = None
    file_fd = None
    try:
        if os.name == "posix":
            directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for component in path.parts[1:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=directory_fd)
        else:
            for component in (path, *path.parents):
                if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                    raise ValueError("archive path contains a link")
            file_fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("archive must contain regular, unlinked files")
        if not 0 <= info.st_size <= limit:
            raise ValueError("archive file size limit")
        with os.fdopen(file_fd, "rb") as source:
            file_fd = None
            body = source.read(limit + 1)
        if len(body) != info.st_size or len(body) > limit:
            raise ValueError("archive changed while reading")
        return body, info
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _directory(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("archive path contains a link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _usage(root: Path) -> tuple[int, int]:
    size = count = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
                raise ValueError("archive contains a link")
        for name in files:
            p = Path(parent) / name
            info = p.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("archive must contain regular, unlinked files")
            size += info.st_size
            count += name == "metadata.json" and p.parent.parent.parent.name == ".artifacts"
    return size, count


def preserve(root: Path, workspace: str, kind: str, body: bytes, *, name: str = "",
             attributes: dict | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", workspace) or kind not in _FILENAMES:
        raise ValueError("invalid archive destination")
    if not body or len(body) > MAX_FILE_BYTES:
        raise ValueError("archive file size limit")
    digest = hashlib.sha256(body).hexdigest()
    with _LOCK:
        root = Path(root).absolute()
        _directory(root)
        personal = root / workspace
        base = personal / ".artifacts" / kind
        _directory(base)
        target = base / digest
        _directory_checks = (target, target / _FILENAMES[kind], target / "metadata.json")
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in _directory_checks):
            raise ValueError("archive contains a link")
        if target.exists():
            if safe_read(target / _FILENAMES[kind], MAX_FILE_BYTES) != body:
                raise ValueError("existing archive failed integrity check")
            return target / _FILENAMES[kind]
        record = {"kind": kind, "name": str(name)[:160], "sha256": digest,
                  "bytes": len(body), "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "qualification": "Original upload or requested output; archival is not approval to run."}
        if attributes is not None:
            record["attributes"] = attributes
        metadata = json.dumps(record, indent=2, allow_nan=False).encode("utf-8")
        if len(metadata) > 16384:
            raise ValueError("archive metadata size limit")
        total, count = _usage(root)
        own, _ = _usage(personal)
        added = len(body) + len(metadata)
        if total + added > MAX_TOTAL_BYTES or own + added > MAX_WORKSPACE_BYTES or count >= MAX_BUNDLES:
            raise ValueError("archive storage limit reached")
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=base))
        try:
            for filename, data in ((_FILENAMES[kind], body), ("metadata.json", metadata)):
                with (pending / filename).open("xb") as out:
                    os.chmod(out.name, 0o600)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
            pending.rename(target)
        finally:
            if pending.exists() and pending.parent == base and pending.name.startswith(".pending-"):
                shutil.rmtree(pending)
        return target / _FILENAMES[kind]
