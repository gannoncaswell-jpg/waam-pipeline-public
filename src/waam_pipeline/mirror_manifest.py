'Every file the workbench keeps for an account, named for the OneDrive copy.'


from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import job_archive, runs

VERSION = 1
_KINDS = {"jbi": ("Downloads", ".JBI"), "gcode": ("Downloads", ".gcode"),
          "ros2": ("Downloads", ".ros2.json"), "stl": ("Uploads/STL", ".stl"),
          "vardat": ("Uploads/VAR.DAT", ".DAT")}
_BUILD_FILES = ("request.json", "part.gcode", "part.stl")
_BUILD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ACCOUNT_RE = re.compile(r"(.+?)_at_.+_[0-9a-f]{6}")
_UNSAFE = re.compile(r"[^A-Za-z0-9._@+-]+")


def _zone():

    try:
        return ZoneInfo(os.environ.get("WAAM_RUN_TZ", "America/Chicago"))
    except ZoneInfoNotFoundError:
        return dt.datetime.now().astimezone().tzinfo


def _label(text, fallback: str, limit: int = 48) -> str:
    label = _UNSAFE.sub("_", str(text or ""))[:limit].strip("._ -")
    return label or fallback


def _digest(path: Path) -> tuple[str, int]:

    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_size > job_archive.MAX_FILE_BYTES:
        raise ValueError("file size limit")
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), info.st_size


def _dirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())
    except FileNotFoundError:
        return []


def _people(workspaces: list[str]) -> dict[str, str]:


    short = {}
    for ws in workspaces:
        match = _ACCOUNT_RE.fullmatch(ws)
        short[ws] = match.group(1) if match else ws
    lowered = [name.lower() for name in short.values()]
    full = {ws.lower() for ws in workspaces}
    people = {ws: ws if lowered.count(name.lower()) > 1 or (name != ws and name.lower() in full) else name
              for ws, name in short.items()}
    final = [name.lower() for name in people.values()]
    return {ws: f"{name}_{hashlib.sha256(ws.encode()).hexdigest()[:6]}" if final.count(name.lower()) > 1
            else name for ws, name in people.items()}


def _stamp(meta: dict, path: Path) -> dt.datetime:
    try:
        when = dt.datetime.fromisoformat(meta["created_at"])
        if when.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        when = dt.datetime.fromtimestamp(os.lstat(path).st_mtime, dt.timezone.utc)
    return when.astimezone(_zone())


def _artifacts(ws_dir: Path, folder_for, out: list, skipped: list) -> None:
    for kind, (category, ext) in _KINDS.items():
        for bundle in _dirs(ws_dir / ".artifacts" / kind):
            if not _DIGEST_RE.fullmatch(bundle.name):
                continue
            body = bundle / job_archive._FILENAMES[kind]
            metadata = bundle / "metadata.json"
            try:
                meta = json.loads(job_archive.safe_read(metadata, 16384))
                if not isinstance(meta, dict):
                    raise ValueError("metadata is not an object")
                digest, size = _digest(body)
                if (meta.get("kind") != kind or meta.get("sha256") != digest
                        or digest != bundle.name or type(meta.get("bytes")) is not int
                        or meta["bytes"] != size):
                    raise ValueError("archive metadata does not match its payload")
                folder = folder_for(kind, category, meta)
                name = str(meta.get("name") or "")
                if name.lower().endswith(ext.lower()):
                    name = name[:-len(ext)]
                when = _stamp(meta, body)
                dest = (f"{folder}/{when:%Y-%m-%d}/{when:%H%M%S}_"
                        f"{_label(name, kind.upper())}_{bundle.name[:8]}{ext}")
                out.append((body, dest))
                out.append((metadata, dest + ".metadata.json"))
            except (OSError, ValueError) as exc:
                skipped.append({"src": str(bundle), "why": str(exc)})


def build(saves: Path) -> dict:

    saves = Path(saves).absolute()
    pairs: list[tuple[Path, str]] = []
    skipped: list[dict] = []
    workspaces = [d.name for d in _dirs(saves)
                  if runs.WORKSPACE_RE.fullmatch(d.name) and not d.name.startswith(("guest-", "library-"))]
    people = _people(workspaces)
    for ws in workspaces:
        ws_dir, person = saves / ws, people[ws]
        _artifacts(ws_dir, lambda kind, category, meta, person=person: f"{person}/{category}", pairs, skipped)
        for run_dir in _dirs(ws_dir / "_runs"):
            run = run_dir.name
            if not runs.RUN_RE.fullmatch(run):
                continue
            for path in sorted(run_dir.iterdir()):
                if path.name == f"{run}_SEALED.json" or runs.valid_file_name(run, path.name):
                    pairs.append((path, f"{person}/Runs/{run}/{path.name}"))
        for build_dir in _dirs(ws_dir):
            if _BUILD_RE.fullmatch(build_dir.name) and (build_dir / "request.json").is_file():
                for name in _BUILD_FILES:
                    if (build_dir / name).exists():
                        pairs.append((build_dir / name, f"{person}/Saved builds/{build_dir.name}/{name}"))
    for library in _dirs(saves):
        if library.name.startswith("library-") and runs.WORKSPACE_RE.fullmatch(library.name):
            def owner(kind, category, meta, fallback=library.name[8:14]):
                attributes = meta.get("attributes")
                email = attributes.get("owner") if isinstance(attributes, dict) else None
                return f"Part library/{_label(email, fallback)}"
            _artifacts(library, owner, pairs, skipped)
    files, seen = [], set()
    for path, dest in sorted(pairs, key=lambda pair: pair[1]):
        if dest in seen:
            skipped.append({"src": str(path), "why": "duplicate destination " + dest})
            continue
        try:
            digest, size = _digest(path)
        except (OSError, ValueError) as exc:
            skipped.append({"src": str(path), "why": str(exc)})
            continue
        seen.add(dest)
        files.append({"src": path.relative_to(saves).as_posix(), "dest": dest,
                      "sha256": digest, "bytes": size})
    for item in skipped:
        item["src"] = os.path.relpath(item["src"], saves).replace(os.sep, "/")
    return {"version": VERSION, "files": files, "skipped": skipped}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--saves", type=Path, default=Path(__file__).resolve().parents[2] / "saves")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.saves), allow_nan=False))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
