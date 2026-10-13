"Copy everything the VM workbench keeps to the host's synced OneDrive folder."


from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import shutil
import stat

import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline import runs

COPY_LABEL = "LETU OneDrive RAW SLICER FILES"
REMOTE_PATH_RE = re.compile(r"[A-Za-z0-9/._-]+")
SRC_RE = re.compile(r"[A-Za-z0-9_-]{1,80}(?:/[A-Za-z0-9._-]{1,120}){1,6}")
DEST_PART_RE = re.compile(r"[A-Za-z0-9@+_-][A-Za-z0-9 ._@+-]{0,119}")
_RESERVED = frozenset(["con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))])
MAX_DEST = 240
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 50000
BATCH_BYTES = 256 * 1024 * 1024
BATCH_FILES = 400
LOCK_STALE_S = 2 * 3600
README = """RAW SLICER FILES -- copied from the WAAM workbench server

Copied here every 5 minutes by tools/mirror_runs.py on the lab PC. Every file
the workbench keeps for each signed-in person is copied:

  <person>/Runs/<run>/            one folder per part-on-plate run log
  <person>/Downloads/<date>/      every job and G-code downloaded
  <person>/Uploads/VAR.DAT/<date>/  every VAR.DAT uploaded
  <person>/Uploads/STL/<date>/    STL uploads
  <person>/Saved builds/<build>/  each saved build (request.json, part.gcode)
  Part library/<owner>/<date>/    STL part library uploads

Download names are <time>_<name>_<first 8 of sha256>.<ext>. Times use
WAAM_RUN_TZ when valid, otherwise the system's local time. To load a job on the pendant, download it from the workbench
(or rename a copy to its job name).

Never edit files here: the copy updates them. A file changed here is kept,
and the server's version is written beside it as <name>.conflict-1.
The copy never deletes anything. A file deleted here is copied back while
the server still has it.
"""


def command_runner(argv: list[str], *, timeout: int, stdout=None, stdin: bytes | None = None):
    return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE if stdout is None else stdout,
                          stderr=subprocess.PIPE, check=True, timeout=timeout)  # nosec B603


def _os(path: Path) -> Path:

    text = str(path)
    if os.name == "nt" and len(text) >= 240 and not text.startswith("\\\\?\\"):
        return Path("\\\\?\\" + text)
    return path


def _unlinked(path: Path) -> None:
    for item in (path, *path.parents):
        item = _os(item)
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("mirror path contains a link")


def _file_digest(path: Path) -> tuple[str, int]:
    _unlinked(path)
    path = _os(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("mirror files must be regular and unlinked")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), info.st_size


def validate_entry(entry) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("invalid manifest entry")
    src, dest, digest, size = (entry.get(key) for key in ("src", "dest", "sha256", "bytes"))
    if (not isinstance(src, str) or not SRC_RE.fullmatch(src)
            or any(part in (".", "..") for part in src.split("/"))):
        raise ValueError(f"unsafe source path {src!r}")
    parts = dest.split("/") if isinstance(dest, str) else []
    if not parts or len(dest) > MAX_DEST or len(parts) > 6 or any(
            not DEST_PART_RE.fullmatch(part) or part.endswith((" ", "."))
            or part.split(".")[0].strip().lower() in _RESERVED or ".conflict-" in part
            for part in parts):
        raise ValueError(f"unsafe destination {dest!r}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid manifest sha256")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError("invalid manifest file size")
    return {"src": src, "dest": dest, "sha256": digest, "bytes": size}


def validate_listing(listing) -> list[dict]:
    if not isinstance(listing, dict) or listing.get("version") != 1 or not isinstance(listing.get("files"), list):
        raise ValueError("invalid file manifest")
    if len(listing["files"]) > MAX_ENTRIES:
        raise ValueError("file manifest entry limit")
    entries = [validate_entry(entry) for entry in listing["files"]]
    for key in ("src", "dest"):
        values = [entry[key].lower() if key == "dest" else entry[key] for entry in entries]
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate {key} in file manifest")
    if "readme.txt" in {entry["dest"].lower() for entry in entries}:
        raise ValueError("README.txt is written by the host")
    return entries


def validate_manifest(record: dict) -> dict:

    if not isinstance(record, dict):
        raise ValueError("invalid run manifest")
    ws, run_id = record.get("workspace"), record.get("run")
    if (not isinstance(ws, str) or not runs.WORKSPACE_RE.fullmatch(ws)
            or not isinstance(run_id, str) or not runs.RUN_RE.fullmatch(run_id)):
        raise ValueError("invalid workspace or run id")
    files = record.get("files")
    if not isinstance(record.get("sealed_at"), str) or not isinstance(files, list):
        raise ValueError("invalid sealed manifest")
    if not 4 <= len(files) <= runs.MAX_FILES - 1:
        raise ValueError("manifest file count limit")
    names, total = set(), 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("invalid manifest file")
        name, digest, size = entry.get("name"), entry.get("sha256"), entry.get("bytes")
        if (not isinstance(name, str) or not runs.valid_file_name(run_id, name)
                or name == run_id + "_SEALED.json" or name in names):
            raise ValueError("invalid manifest file name or extension")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid manifest sha256")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= runs.MAX_FILE_BYTES:
            raise ValueError("invalid manifest file size")
        names.add(name)
        total += size
    if total > runs.MAX_RUN_BYTES:
        raise ValueError("run size limit: 250 MB")
    required = {run_id + suffix for suffix in
                ("_run.json", "_files.csv", "_temperatures.csv", "_README.txt")}
    if not required <= names:
        raise ValueError("manifest missing run records")
    return {"workspace": ws, "run": run_id, "sealed_at": record["sealed_at"], "files": files}


def verify_run(folder: Path, manifest: dict) -> None:
    manifest = validate_manifest(manifest)
    _unlinked(folder)
    if not _os(folder).is_dir():
        raise ValueError("run folder is missing")
    seal_name = manifest["run"] + "_SEALED.json"
    expected = {entry["name"]: entry for entry in manifest["files"]}
    if {item.name for item in _os(folder).iterdir()} != set(expected) | {seal_name}:
        raise ValueError("run contains missing or extra files")
    _, seal_size = _file_digest(folder / seal_name)
    if seal_size > 1024 * 1024:
        raise ValueError("sealed manifest size limit")
    seal = json.loads(_os(folder / seal_name).read_bytes())
    if seal != {key: manifest[key] for key in ("run", "sealed_at", "files")}:
        raise ValueError("SEALED manifest mismatch")
    for name, entry in expected.items():
        if _file_digest(folder / name) != (entry["sha256"], entry["bytes"]):
            raise ValueError("sha256 or size mismatch: " + name)


def _copy_exclusive(source: Path, target: Path) -> None:
    _unlinked(target)
    with _os(source).open("rb") as incoming, _os(target).open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _place(staged: Path, target: Path, *, replace: bool) -> None:

    try:
        if replace:
            os.replace(_os(staged), _os(target))
        else:
            if _os(target).exists():
                raise FileExistsError(str(target))
            os.rename(_os(staged), _os(target))
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    if not replace:
        _copy_exclusive(staged, target)
        return
    temporary = target.with_name(f".{target.name}.incoming")
    _os(temporary).unlink(missing_ok=True)
    _copy_exclusive(staged, temporary)
    os.replace(_os(temporary), _os(target))


class Mirror:
    def __init__(self, options, *, runner=command_runner, log=None):
        self.options = options
        self.runner = runner
        self.log = log or logging.getLogger("waam-mirror")
        for value in (options.remote_repo, options.remote_python):
            if not REMOTE_PATH_RE.fullmatch(value):
                raise ValueError("remote paths must match ^[A-Za-z0-9/._-]+$")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", options.ssh):
            raise ValueError("SSH target must be USER@HOST")
        if not 1 <= options.port <= 65535:
            raise ValueError("SSH port must be 1..65535")
        self.dest = Path(options.dest).absolute()
        self.state = Path(options.state).absolute()
        self.staging = Path(getattr(options, "staging", None) or self.state.parent / "staging").absolute()
        _unlinked(self.dest)
        _unlinked(self.state)
        _unlinked(self.staging)

        if self.state.resolve().is_relative_to(self.dest.resolve()):
            raise ValueError("state must be outside the destination folder")
        if self.staging.resolve().is_relative_to(self.dest.resolve()):
            raise ValueError("staging must be outside the destination folder")
        if self.state.name.lower() in {"mirror.log", "mirror.log.1", "mirror.log.2", "mirror.log.3",
                                       "mirror.lock"}:
            raise ValueError("state must not use a mirror log or lock filename")
        self.ssh = [shutil.which("ssh") or "ssh", "-i", str(options.key), "-p", str(options.port),
                    "-o", "BatchMode=yes", options.ssh]

    def _remote(self, command: str, *, timeout: int, stdout=None, stdin: bytes | None = None):
        if stdin is None:
            return self.runner([*self.ssh, command], timeout=timeout, stdout=stdout)
        return self.runner([*self.ssh, command], timeout=timeout, stdout=stdout, stdin=stdin)

    def _module_command(self, arguments: str, module: str = "waam_pipeline.runs") -> str:
        command = (f"cd {self.options.remote_repo} && PYTHONPATH=src exec "
                   f"{self.options.remote_python} -m {module} {arguments}")
        return "sudo -u waam sh -c " + shlex.quote(command)

    def _load_state(self) -> dict:
        state = {"version": 2, "files": {}, "runs": {}}
        if self.state.exists():
            loaded = json.loads(self.state.read_bytes())
            if not isinstance(loaded, dict) or not isinstance(loaded.get("runs"), dict):
                raise ValueError("invalid mirror state")
            files = loaded.get("files", {})
            if not isinstance(files, dict):
                raise ValueError("invalid mirror state")
            state.update(runs=loaded["runs"], files=files)
        return state

    def _save_state(self, state: dict) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        descriptor, pending = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.state.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(state, output, indent=2, allow_nan=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, self.state)
        finally:
            Path(pending).unlink(missing_ok=True)

    def _target(self, dest: str) -> Path:
        return self.dest.joinpath(*dest.split("/"))

    def _status(self, entry: dict, files: dict) -> str:

        target = self._target(entry["dest"])
        _unlinked(target)
        if not _os(target).exists():
            return "new"
        current, _ = _file_digest(target)
        if current == entry["sha256"]:
            return "present"
        return "update" if files.get(entry["dest"]) == current else "conflict"

    def _fetch(self, batch: list[dict], staging: Path) -> dict[str, Path]:

        archive = staging / "incoming.tar"
        command = (f"sudo -u waam tar -C {self.options.remote_repo}/saves "
                   "--null --verbatim-files-from -T - -cf -")
        names = b"".join(entry["src"].encode("ascii") + b"\0" for entry in batch)
        with archive.open("xb") as output:
            self._remote(command, timeout=900, stdout=output, stdin=names)
        expected = {entry["src"]: entry for entry in batch}
        staged: dict[str, Path] = {}
        with tarfile.open(archive, "r:") as incoming:
            for member in incoming:
                entry = expected.get(member.name)
                if (entry is None or member.name in staged or not member.isfile()
                        or member.sparse is not None or member.size != entry["bytes"]):
                    raise ValueError(f"unexpected tar member {member.name!r}")

                path = staging / f"{len(staged):05d}.bin"
                source = incoming.extractfile(member)
                with source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if _file_digest(path) != (entry["sha256"], entry["bytes"]):
                    raise ValueError("sha256 or size mismatch: " + member.name)
                staged[member.name] = path
        if set(staged) != set(expected):
            raise ValueError("the VM did not send every requested file")
        archive.unlink()
        return staged

    def _install(self, staged: Path, entry: dict, status: str, files: dict) -> str:
        target = self._target(entry["dest"])
        _unlinked(target)
        _os(target.parent).mkdir(parents=True, exist_ok=True)
        if status == "conflict":
            number = 1
            while True:
                conflict = target.with_name(f"{target.name}.conflict-{number}")
                try:
                    _place(staged, conflict, replace=False)
                    break
                except FileExistsError:
                    if _file_digest(conflict)[0] == entry["sha256"]:
                        break
                    number += 1
            self.log.error("Conflict: kept the changed %s; the server's version is %s", target, conflict.name)
            return "conflict"
        _place(staged, target, replace=status == "update")
        if _file_digest(target) != (entry["sha256"], entry["bytes"]):
            raise ValueError("installed copy does not verify: " + entry["dest"])
        files[entry["dest"]] = entry["sha256"]
        return "copied" if status == "new" else "updated"

    def _readme(self, files: dict) -> None:
        body = README.encode("utf-8")
        entry = {"dest": "README.txt", "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        status = self._status(entry, files)
        if status in ("present", "conflict"):
            if status == "present":
                files["README.txt"] = entry["sha256"]
            return
        self.staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="waam-mirror-", dir=self.staging) as temporary:
            staged = Path(temporary) / "README.txt"
            staged.write_bytes(body)
            self._install(staged, entry, status, files)

    def _copy_files(self, entries: list[dict], state: dict) -> tuple[dict, bool]:
        counts = {"present": 0, "copied": 0, "updated": 0, "conflict": 0}
        failed = False
        pending = []
        for entry in entries:
            try:
                status = self._status(entry, state["files"])
            except (OSError, ValueError) as exc:
                failed = True
                self.log.error("Cannot check %s: %s", entry["dest"], exc)
                continue
            if status == "present":
                counts["present"] += 1
                state["files"][entry["dest"]] = entry["sha256"]
            else:
                pending.append((entry, status))
        if self.options.dry_run:
            for entry, status in pending:
                self.log.info("would %s %s", {"new": "copy", "update": "update"}.get(status, "keep a conflict copy of"),
                              entry["dest"])
            return {**counts, "pending": len(pending)}, failed
        batches, batch, size = [], [], 0
        for item in pending:
            if batch and (len(batch) >= BATCH_FILES or size + item[0]["bytes"] > BATCH_BYTES):
                batches.append(batch)
                batch, size = [], 0
            batch.append(item)
            size += item[0]["bytes"]
        if batch:
            batches.append(batch)
        for batch in batches:
            try:
                self.staging.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="waam-mirror-", dir=self.staging) as temporary:
                    staged = self._fetch([entry for entry, _ in batch], Path(temporary))
                    for entry, status in batch:
                        try:
                            outcome = self._install(staged[entry["src"]], entry, status, state["files"])
                            counts[outcome] += 1
                            failed |= outcome == "conflict"
                        except (OSError, ValueError) as exc:
                            failed = True
                            self.log.error("Cannot place %s: %s", entry["dest"], exc)
            except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
                failed = True
                self.log.error("Batch of %d files failed: %s", len(batch), exc)
            self._save_state(state)
        return counts, failed

    def _ack_sealed(self, entries: list[dict], state: dict) -> bool:
        failed = False
        result = self._remote(self._module_command("manifest"), timeout=60)
        manifests = json.loads(result.stdout)
        if not isinstance(manifests, list):
            raise ValueError("run manifest response must be an array")
        folders = {}
        for entry in entries:
            parts = entry["src"].split("/")
            if len(parts) == 4 and parts[1] == "_runs":
                folders[(parts[0], parts[2])] = self._target(entry["dest"]).parent
        for record in manifests:
            try:
                manifest = validate_manifest(record)
                key = (manifest["workspace"], manifest["run"])
                done = state["runs"].get(manifest["run"])
                if done and done.get("sealed_at") == manifest["sealed_at"]:
                    continue
                if self.options.dry_run:
                    self.log.info("would acknowledge %s once its copy verifies", manifest["run"])
                    continue
                if key not in folders:
                    raise ValueError("sealed run is not in the file manifest")
                verify_run(folders[key], manifest)
                arguments = f"ack --to {shlex.quote(COPY_LABEL)} -- {manifest['workspace']} {manifest['run']}"
                self._remote(self._module_command(arguments), timeout=60)
                state["runs"][manifest["run"]] = {
                    "workspace": manifest["workspace"], "to": COPY_LABEL, "sealed_at": manifest["sealed_at"],
                    "mirrored_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                self._save_state(state)
                self.log.info("Acknowledged sealed run %s", manifest["run"])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failed = True
                self.log.error("Sealed run not acknowledged: %s", exc)
        return failed

    def run(self) -> int:
        try:
            state = self._load_state()
            result = self._remote(self._module_command("", "waam_pipeline.mirror_manifest"), timeout=300)
            listing = json.loads(result.stdout)
            entries = validate_listing(listing)
            if not self.options.dry_run:
                _os(self.dest).mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.log.error("Cannot list the server's files: %s", exc)
            return 1
        for item in listing.get("skipped") or []:
            self.log.warning("The server skipped %s", item)
        counts, failed = self._copy_files(entries, state)
        try:
            if not self.options.dry_run:
                self._readme(state["files"])
            failed |= self._ack_sealed(entries, state)
            if not self.options.dry_run:
                self._save_state(state)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            failed = True
            self.log.error("Finishing the copy failed: %s", exc)
        self.log.info("%s files listed: %s", len(entries),
                      ", ".join(f"{count} {name}" for name, count in counts.items()))
        return int(failed)


def _lock(path: Path) -> bool:

    try:
        if path.exists() and time.time() - path.stat().st_mtime > LOCK_STALE_S:
            path.unlink()
        with path.open("x", encoding="utf-8") as lock:
            lock.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False


def main(argv=None, *, runner=command_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", required=True)
    parser.add_argument("--ssh", default="waam@127.0.0.1")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "waam_ed25519"))
    parser.add_argument("--remote-python", default="/srv/waam/venv/bin/python")
    parser.add_argument("--remote-repo", default="/srv/waam/waam-pipeline")
    parser.add_argument("--state")
    parser.add_argument("--staging", help="work folder outside --dest; same drive lets files appear in one rename")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    if options.state is None:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            parser.error("LOCALAPPDATA is unavailable; provide --state")
        options.state = str(Path(local) / "waam-mirror" / "state.json")
    try:
        mirror = Mirror(options, runner=runner)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        mirror.state.parent.mkdir(parents=True, exist_ok=True)
        log_path = mirror.state.parent / "mirror.log"
        _unlinked(log_path)
        handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"Cannot open mirror log: {exc}", file=sys.stderr)
        return 1
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    mirror.log.setLevel(logging.INFO)
    mirror.log.addHandler(handler)
    lock = mirror.state.parent / "mirror.lock"
    try:
        if not _lock(lock):
            mirror.log.info("Another copy is running; skipped this one")
            return 0
        try:
            return mirror.run()
        finally:
            lock.unlink(missing_ok=True)
    finally:
        mirror.log.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
