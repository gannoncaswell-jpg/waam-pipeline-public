'PostgreSQL index of the run archive (docs/run-database.md).'


from __future__ import annotations

import argparse
import contextlib
import csv
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

from . import arcmon, runs, var_dat
from . import passes as pass_tables
from .job_archive import safe_read, safe_read_stat

DSN_ENV = "WAAM_PG_DSN"
DEFAULT_DSN = "dbname=waam_runs"
SCHEMA = "runindex"
READ_ROLE = "waam_read"
INGEST_VERSION = 1
SQL_DIR = Path(__file__).resolve().parent / "sql"
TABLES = ("runs", "run_files", "exports", "passes", "arcmon_files", "arcmon_samples", "arcmon_segments",
          "temperatures", "notes", "layer_log", "layer_readings", "cycles", "var_files", "var_positions",
          "thermocouple_samples", "ingest_runs", "ingest_attempts")
_LOCK_CLASS = 0x5741414D


MAX_ATTEMPTS = 3
_MAX_JOB_CHARS = 200
_MAX_NUMBER = 1e15
_KEPT_BODIES = ("run.json", "SEALED.json")
_NO_READER = "--read-role '': no role was granted the index, so no dashboard can read it"
_MIGRATION_RE = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
_KIND_RE = "|".join(re.escape(kind) for kind in (*runs._EXTENSIONS, "ARCMON", "ATTACH"))
_RECORD_KINDS = {"run.json": "RUN-JSON", "files.csv": "FILES-CSV", "temperatures.csv": "TEMPERATURES-CSV",
                 "cycles.csv": "CYCLES-CSV", "layer_log.csv": "LAYER-LOG-CSV", "README.txt": "README",
                 "SEALED.json": "SEALED"}
_TABULATED = ("files", "temperatures", "notes", "layer_log", "cycles")
_THERMAL_QUERY = ("interpass", "interpass_every", "max_interpass", "max_unit", "temp_check_every", "bead_dwell",
                  "thermal_target", "thermal_peak", "thermal_model", "thermal_c", "thermal_tau")
_MEASURE_QUERY = ("measure_every", "measure_times", "measure_pause", "measure_clean", "measure_pre_weld")
_VAR_KINDS = frozenset(("PLATE-CORNERS-VAR", "PLATE-SENSE-VAR", "LAYER-TOUCH-VAR", "CYCLE-VAR"))
_WELD_KINDS = ("WELD-JOB", "CYCLE-JOB")
_PARSE_ERRORS = (ValueError, KeyError, TypeError, IndexError, csv.Error, OverflowError, RecursionError)
_BOOTSTRAP = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    sha256      text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now())"""


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("the run database needs psycopg 3: pip install 'psycopg[binary]>=3.2,<4'") from exc
    return psycopg


def pin(conn) -> str:


    from psycopg import sql
    conn.execute(sql.SQL("SET search_path = {}, pg_temp").format(sql.Identifier(SCHEMA)))
    return SCHEMA


def connect(dsn: str | None = None):

    conn = _psycopg().connect(dsn or os.environ.get(DSN_ENV) or DEFAULT_DSN, autocommit=True)
    try:
        pin(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrations(directory: Path = SQL_DIR) -> list[tuple[int, str, str]]:
    found = []
    for path in sorted(Path(directory).iterdir()):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match:
            found.append((int(match[1]), path.stem, path.read_text(encoding="utf-8").replace("\r\n", "\n")))
        elif path.suffix == ".sql":
            raise RuntimeError(f"migration {path.name}: name it NNNN_lowercase_words.sql")
    if [version for version, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migrations must be numbered 0001, 0002, ... with no gaps")
    return found


def migrate(conn, directory: Path = SQL_DIR, *, read_role: str | None = READ_ROLE) -> list[int]:


    from psycopg import sql
    schema = pin(conn)
    pending = migrations(directory)
    if read_role and conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (read_role,)).fetchone() is None:
        raise RuntimeError(f"read role {read_role} does not exist: create it (NOLOGIN), "
                           "or pass --read-role '' to index without granting one")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LOCK_CLASS, schema))
        owner = conn.execute("SELECT nspowner::regrole::text, nspowner = (SELECT oid FROM pg_roles "
                             "WHERE rolname = current_user) FROM pg_namespace WHERE nspname = %s",
                             (schema,)).fetchone()
        if owner is None:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        elif not owner[1]:
            raise RuntimeError(f"schema {schema} is owned by {owner[0]}, not by the connecting role; "
                               "migrate and ingest as its owner")
        conn.execute(_BOOTSTRAP)
    applied = []
    for version, name, body in pending:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LOCK_CLASS, schema))
            row = conn.execute("SELECT sha256 FROM schema_migrations WHERE version = %s", (version,)).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise RuntimeError(f"migration {name} changed after it was applied; add a new migration instead")
                continue
            conn.execute(body)
            conn.execute("INSERT INTO schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
                         (version, name, digest))
            applied.append(version)
    if read_role:
        _grant_reader(conn, read_role)
    return applied


def _grant_reader(conn, role: str) -> None:


    from psycopg import sql
    names = {"schema": sql.Identifier(SCHEMA), "role": sql.Identifier(role)}
    with conn.transaction():
        for statement in ("GRANT USAGE ON SCHEMA {schema} TO {role}",
                          "GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}",
                          "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}"):
            conn.execute(sql.SQL(statement).format(**names))


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and abs(value) <= _MAX_NUMBER:
        return float(value)
    return None


def _int(value) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number.is_integer() else None


def _whole(value, label: str) -> int | None:
    if value is None:
        return None
    if not float(value).is_integer():
        raise ValueError(f"non-integral {label} reading {value!r}")
    return int(value)


def _ints(value) -> list[int] | None:
    if isinstance(value, (list, tuple)) and all(_int(item) is not None for item in value):
        return [_int(item) for item in value]
    return None


def _scrub_text(value: str) -> str:


    value = value.replace("\x00", "\ufffd")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "replace").decode("utf-8")
    return value


def _scrub(value):
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {_scrub_text(key) if isinstance(key, str) else key: _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _text(value) -> str | None:
    return _scrub_text(value) if isinstance(value, str) else None


def _ts(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _to_c(value, unit) -> float | None:
    number = _num(value)
    if number is None:
        return None
    return (number - 32) * 5 / 9 if unit == "F" else number


def _dumps(value) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def _jsonb(value):

    if value is None:
        return None
    from psycopg.types.json import Jsonb
    return Jsonb(_scrub(runs._clean(value)), dumps=_dumps)


def _json_object(body: bytes) -> dict:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _name_parts(run_id: str, name: str):

    derived = None
    for suffix, label in ((".arcmon-passes.csv", "arcmon-passes"), (".passes.csv", "passes"),
                          (".passes.json", "passes")):
        if name.endswith(suffix):
            derived = label
    match = re.fullmatch(re.escape(run_id) + rf"_(\d{{2,3}})_({_KIND_RE})(?:_(.+?))?\.([A-Za-z0-9]+)", name)
    if not match:
        return None
    return int(match[1]), match[2], match[3] or "", match[4], derived


def _note(errors: dict, name: str, message: str | None = None) -> None:


    if message is None:
        errors.setdefault(name, None)
    else:
        errors[name] = f"{errors[name]}; {message}" if errors.get(name) else message


@dataclass
class _File:
    name: str
    path: Path
    sha256: str
    size: int
    mtime: dt.datetime
    head: bytes
    body: bytes | None = None


def _digest(files: dict[str, _File], skipped: dict[str, str]) -> str:

    entries = {**{name: f.sha256 for name, f in files.items()},
               **{name: f"not read: {why}" for name, why in skipped.items()}}
    text = "".join(f"{name}\t{value}\n" for name, value in sorted(entries.items()))
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def _names(changed: list[str], skipped: dict[str, str]) -> str:
    shown = [name + (f" (not read: {skipped[name]})" if name in skipped else "") for name in changed[:10]]
    return ", ".join(shown) + (f" and {len(changed) - 10} more" if len(changed) > 10 else "")


@dataclass
class Report:
    seen: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    refused: int = 0
    problems: list = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(item["level"] == "error" for item in self.problems)


class _Ingest:
    def __init__(self, conn, saves: Path, *, mirror: bool, workspace: str | None, report: Report,
                 dry_run: bool = False):
        self.conn = conn
        self.saves = Path(saves).absolute()
        self.mirror = mirror
        self.workspace = workspace if mirror else None
        self.report = report
        self.dry_run = dry_run

    def problem(self, level: str, run_id: str | None, name: str | None, message, into: list | None = None) -> None:
        item = {"level": level, "run": run_id, "file": None if name is None else _scrub_text(name),
                "message": _scrub_text(str(message)[:500])}
        (self.report.problems if into is None else into).append(item)

    def all_runs(self, workspace: str | None, run: str | None) -> None:
        folders = list(self.folders(workspace, run))
        attempts = {run_id: (digest, count) for run_id, digest, count in self.conn.execute(
            "SELECT run_id, content_sha256, attempts FROM ingest_attempts").fetchall()}
        folders.sort(key=lambda item: item[1].name in attempts)
        for ws_dir, folder in folders:
            self.run_folder(ws_dir, folder, attempts.get(folder.name))


    def folders(self, workspace: str | None, run: str | None):
        runs._no_links(self.saves)
        if not self.saves.is_dir():
            raise ValueError(f"saves folder not found: {self.saves}")
        subfolder = "Runs" if self.mirror else "_runs"
        for top in sorted(self.saves.iterdir()):
            if _is_link(top) or not top.is_dir():
                continue
            if not self.mirror:
                if not runs.WORKSPACE_RE.fullmatch(top.name) or top.name.startswith(("guest-", "library-")):
                    continue
                if workspace and top.name != workspace:
                    continue
            base = top / subfolder
            if _is_link(base):
                self.problem("error", None, base.relative_to(self.saves).as_posix(),
                             "run folder base is a link; not followed")
                continue
            if not base.is_dir():
                continue
            for folder in sorted(base.iterdir()):
                if not runs.RUN_RE.fullmatch(folder.name) or (run and folder.name != run):
                    continue
                if _is_link(folder):
                    self.problem("error", folder.name, None, "run folder is a link; not followed")
                    continue
                if folder.is_dir():
                    yield top.name, folder

    def inventory(self, run_id: str, folder: Path, found: list) -> tuple[dict[str, _File], dict[str, str]]:


        files, skipped = {}, {}
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if name.startswith(".pending-"):
                skipped[name] = "temporary file"
                continue
            if name != f"{run_id}_SEALED.json" and not runs.valid_file_name(run_id, name):
                self.problem("warning", run_id, name, "not a run archive file; not read", found)
                skipped[name] = "not an archive file"
                continue
            path = folder / name
            try:
                if _is_link(path):
                    raise ValueError("a link; not followed")
                body, info = safe_read_stat(path, runs.MAX_FILE_BYTES)
            except (OSError, ValueError) as exc:
                self.problem("error", run_id, name, f"not read: {exc}", found)
                skipped[name] = "unreadable"
                continue
            keep = name[len(run_id) + 1:] in _KEPT_BODIES
            files[name] = _File(name, path, hashlib.sha256(body).hexdigest(), len(body),
                                dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc), body[:64],
                                body if keep else None)
        return files, skipped

    def read(self, f: _File) -> bytes:

        if f.body is not None:
            return f.body
        try:
            body = safe_read(f.path, runs.MAX_FILE_BYTES)
        except OSError as exc:
            raise ValueError(f"not read: {exc}") from exc
        if hashlib.sha256(body).hexdigest() != f.sha256:
            raise ValueError("changed while it was being indexed")
        return body

    def verify_seal(self, run_id: str, files: dict[str, _File], skipped: dict[str, str]) -> dict:
        seal = _json_object(files[f"{run_id}_SEALED.json"].body)
        listed = seal.get("files")
        if seal.get("run") != run_id or not isinstance(listed, list):
            raise ValueError("invalid SEALED manifest")
        expected = {}
        for item in listed:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("invalid SEALED manifest entry")
            expected[item["name"]] = (item.get("sha256"), item.get("bytes"))
        actual = {name: (f.sha256, f.size) for name, f in files.items() if name != f"{run_id}_SEALED.json"}
        actual.update((name, ("not read", why)) for name, why in skipped.items())
        changed = sorted(name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name))
        if changed:
            raise ValueError("SEALED RUN CHANGED ON DISK: it no longer matches its SEALED manifest: "
                             + _names(changed, skipped))
        return seal

    def refuse(self, run_id: str, message: str, found: list) -> None:
        self.report.refused += 1
        self.report.problems.extend(found)
        self.problem("error", run_id, None, message)

    @staticmethod
    def _decision(row, digest: str, workspace: str) -> str:
        if row is None:
            return "write"
        if row[3] != workspace:
            return "workspace"
        if row[0] == digest and (row[1] or row[2] == INGEST_VERSION):
            return "unchanged"
        return "sealed" if row[1] else "write"

    def run_folder(self, ws_dir: str, folder: Path, attempt: tuple[str, int] | None = None) -> None:


        psycopg = _psycopg()
        run_id = folder.name
        found: list = []
        files, skipped = self.inventory(run_id, folder, found)
        try:
            source = files.get(f"{run_id}_run.json")
            if source is None:
                raise ValueError("no run.json")
            record = _json_object(source.body)
            workspace = record.get("workspace")
            if record.get("run") != run_id:
                raise ValueError("run.json names another run")
            if not isinstance(workspace, str) or not runs.WORKSPACE_RE.fullmatch(workspace):
                raise ValueError("run.json has an invalid workspace")
            if not self.mirror and workspace != ws_dir:
                raise ValueError("run.json names another workspace")
            if not isinstance(record.get("files", []), list):
                raise ValueError("run.json files is not a list")
            seal = self.verify_seal(run_id, files, skipped) if f"{run_id}_SEALED.json" in files else None
        except _PARSE_ERRORS as exc:
            self.report.seen += 1
            self.refuse(run_id, f"run refused: {exc}", found)
            return
        if self.workspace and workspace != self.workspace:
            return
        self.report.seen += 1
        digest = _digest(files, skipped)
        if attempt is not None and attempt[0] == digest and attempt[1] >= MAX_ATTEMPTS:
            self.refuse(run_id, f"run skipped: the ingest stopped inside it {attempt[1]} times (killed or out of "
                                "memory; see the journal); it is tried again when its files change", found)
            return
        query = "SELECT content_sha256, sealed, ingest_version, workspace FROM runs WHERE run_id = %s"


        marked = not self.dry_run and self._decision(
            self.conn.execute(query, (run_id,)).fetchone(), digest, workspace) == "write"
        if marked:
            self.conn.execute(
                "INSERT INTO ingest_attempts (run_id, content_sha256, attempts, started_at) VALUES (%s, %s, 1, now()) "
                "ON CONFLICT (run_id) DO UPDATE SET attempts = CASE WHEN ingest_attempts.content_sha256 = "
                "EXCLUDED.content_sha256 THEN ingest_attempts.attempts + 1 ELSE 1 END, "
                "content_sha256 = EXCLUDED.content_sha256, started_at = now()", (run_id, digest))
        try:
            with self.conn.transaction():
                row = self.conn.execute(query + " FOR UPDATE", (run_id,)).fetchone()
                decision = self._decision(row, digest, workspace)
                if decision == "workspace":
                    outcome = f"run refused: already indexed under workspace {row[3]}"
                elif decision == "unchanged":
                    outcome = "unchanged"
                elif decision == "sealed":
                    old = dict(self.conn.execute("SELECT name, sha256 FROM run_files WHERE run_id = %s",
                                                 (run_id,)).fetchall())
                    now = {name: f.sha256 for name, f in files.items()}
                    now.update((name, f"not read: {why}") for name, why in skipped.items())
                    changed = sorted(name for name in old.keys() | now.keys() if old.get(name) != now.get(name))
                    outcome = "SEALED RUN CHANGED ON DISK; its rows were not touched: " + _names(changed, skipped)
                else:
                    self.report.problems.extend(found)
                    self.write(run_id, folder, files, record, seal, digest,
                               rederive=row is not None and row[2] != INGEST_VERSION)
                    outcome = "new" if row is None else "updated"
        except (*_PARSE_ERRORS, psycopg.Error) as exc:
            if self.conn.broken:
                raise
            outcome = None
            self.refuse(run_id, f"run refused, nothing written: {exc}", [])
        if marked or (attempt is not None and not self.dry_run):
            self.conn.execute("DELETE FROM ingest_attempts WHERE run_id = %s", (run_id,))
        if outcome is None:
            return
        if outcome == "unchanged":
            self.report.unchanged += 1
        elif outcome == "new":
            self.report.new += 1
        elif outcome == "updated":
            self.report.updated += 1
        else:
            self.refuse(run_id, outcome, found)


    def write(self, run_id, folder, files, record, seal, digest, rederive=False) -> None:
        if rederive:


            self.conn.execute("DELETE FROM exports WHERE run_id = %s", (run_id,))
            self.conn.execute("DELETE FROM arcmon_files WHERE run_id = %s", (run_id,))
            self.conn.execute("DELETE FROM var_files WHERE run_id = %s", (run_id,))
        arc = record.get("arc_monitor") if isinstance(record.get("arc_monitor"), dict) else {}
        sealed = seal is not None
        values = (run_id, record["workspace"], _text(record.get("part")), _text(record.get("name")),
                  _text(record.get("operator")), _text(record.get("cell")), _text(record.get("commit")),
                  _text(record.get("schema")), _text(record.get("status")) or "unknown",
                  _text(record.get("outcome")), _text(record.get("data_class")),
                  _ts(record.get("started_at")), _ts(record.get("finished_at")), sealed,
                  _ts(seal.get("sealed_at")) if sealed else None,
                  files[f"{run_id}_SEALED.json"].sha256 if sealed else None,
                  _jsonb(record.get("measurements")), _jsonb(arc.get("waiver")),
                  _jsonb({key: value for key, value in record.items() if key not in _TABULATED}),
                  folder.relative_to(self.saves).as_posix(), digest, INGEST_VERSION)
        self.conn.execute(
            "INSERT INTO runs (run_id, workspace, part, name, operator, cell, git_commit, record_schema, status, "
            "outcome, data_class, started_at, finished_at, sealed, sealed_at, seal_sha256, measurements, "
            "arc_waiver, record, archive_path, content_sha256, ingest_version) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id) DO UPDATE SET workspace = EXCLUDED.workspace, part = EXCLUDED.part, "
            "name = EXCLUDED.name, operator = EXCLUDED.operator, cell = EXCLUDED.cell, "
            "git_commit = EXCLUDED.git_commit, record_schema = EXCLUDED.record_schema, status = EXCLUDED.status, "
            "outcome = EXCLUDED.outcome, data_class = EXCLUDED.data_class, started_at = EXCLUDED.started_at, "
            "finished_at = EXCLUDED.finished_at, sealed = EXCLUDED.sealed, sealed_at = EXCLUDED.sealed_at, "
            "seal_sha256 = EXCLUDED.seal_sha256, measurements = EXCLUDED.measurements, "
            "arc_waiver = EXCLUDED.arc_waiver, record = EXCLUDED.record, archive_path = EXCLUDED.archive_path, "
            "content_sha256 = EXCLUDED.content_sha256, ingest_version = EXCLUDED.ingest_version, "
            "updated_at = now()", values)
        listed = {item["name"]: item for item in record.get("files") or []
                  if isinstance(item, dict) and isinstance(item.get("name"), str)}
        errors: dict[str, str | None] = {}
        self.exports(run_id, files, listed, errors)
        self.arcmon(run_id, files, listed, errors)
        self.vars(run_id, files, listed, errors)
        self.link_arcmon(run_id)
        for label, action in (("temperatures", lambda: self.temperatures(run_id, record)),
                              ("notes", lambda: self.notes(run_id, record)),
                              ("layer_log", lambda: self.layer_log(run_id, record)),
                              ("cycles", lambda: self.cycles(run_id, record, files, errors))):
            self.section(run_id, f"{run_id}_run.json", label, action, errors)
        self.section(run_id, None, "plate", lambda: self.plate(run_id, files, listed), errors)
        self.run_files(run_id, files, listed, errors)

    def section(self, run_id, name, label, action, errors) -> None:

        psycopg = _psycopg()
        if name:
            _note(errors, name)
        try:
            with self.conn.transaction():
                action()
        except (*_PARSE_ERRORS, psycopg.Error) as exc:
            if self.conn.broken:
                raise
            message = f"{label}: {exc}"
            self.problem("warning", run_id, name, message)
            if name:
                _note(errors, name, _scrub_text(message))

    def run_files(self, run_id, files, listed, errors) -> None:

        carried = {name: error for name, sha, error in self.conn.execute(
            "SELECT name, sha256, parse_error FROM run_files WHERE run_id = %s", (run_id,)).fetchall()
            if name in files and files[name].sha256 == sha}
        self.conn.execute("DELETE FROM run_files WHERE run_id = %s", (run_id,))
        rows = []
        for name, f in files.items():
            parts = _name_parts(run_id, name)
            record_kind = _RECORD_KINDS.get(name[len(run_id) + 1:])
            kind = (listed.get(name, {}).get("kind") or record_kind
                    or {"passes": "PASSES", "arcmon-passes": "ARCMON-PASSES"}.get(parts[4] if parts else None)
                    or (parts[1] if parts else "UNKNOWN"))
            item = listed.get(name, {})
            rows.append((run_id, name, parts[0] if parts else None, _text(str(kind)), f.sha256, f.size, f.mtime,
                         _ts(item.get("added_at")), _text(item.get("source")),
                         errors[name] if name in errors else carried.get(name)))
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO run_files (run_id, name, seq, kind, sha256, size_bytes, mtime, added_at, "
                            "source, parse_error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)

    def _ordered(self, run_id, files):
        out = []
        for name, f in files.items():
            parts = _name_parts(run_id, name)
            if parts is not None:
                out.append((parts, name, f))
        return sorted(out, key=lambda item: (item[0][0], item[1]))

    @staticmethod
    def _export_inputs(name, kind, files) -> str:

        stem = name[:-len(".json")]
        inputs = (name, f"{stem}.passes.json", f"{stem}.passes.csv", name if kind == "ROS2-JOB" else f"{stem}.JBI")
        text = "".join(f"{item}\t{files[item].sha256 if item in files else '-'}\n" for item in inputs)
        return hashlib.sha256(text.encode()).hexdigest()

    def exports(self, run_id, files, listed, errors) -> None:
        existing = {name: (export_id, sha) for export_id, name, sha in self.conn.execute(
            "SELECT id, file_name, inputs_sha256 FROM exports WHERE run_id = %s", (run_id,)).fetchall()}
        wanted = set()
        for (seq, kind, detail, ext, derived), name, f in self._ordered(run_id, files):
            if derived or ext != "json" or (kind not in runs._JBI_KINDS and kind != "ROS2-JOB"):
                continue
            wanted.add(name)
            inputs = self._export_inputs(name, kind, files)
            old = existing.get(name)
            if old is not None and old[1] == inputs:
                continue
            if old is not None:
                self.conn.execute("DELETE FROM exports WHERE id = %s", (old[0],))
            self.section(run_id, name, "export",
                         lambda a=(seq, kind, detail, name, f, inputs): self.export(run_id, *a, files, listed, errors),
                         errors)
        for name in existing.keys() - wanted:
            self.conn.execute("DELETE FROM exports WHERE id = %s", (existing[name][0],))

    def export(self, run_id, seq, kind, detail, name, f, inputs, files, listed, errors) -> None:
        stem = name[:-len(".json")]
        sidecar = {} if kind == "ROS2-JOB" else _json_object(self.read(f))
        rows, table_job = self.pass_rows(run_id, stem, files, errors)
        job = _text(sidecar.get("job")) or table_job or detail or None
        if job is not None and len(job) > _MAX_JOB_CHARS:
            raise ValueError(f"job name longer than {_MAX_JOB_CHARS} characters")
        query = sidecar.get("query") if isinstance(sidecar.get("query"), dict) else {}
        params = sidecar.get("params") if isinstance(sidecar.get("params"), dict) else {}
        summary = sidecar.get("summary") if isinstance(sidecar.get("summary"), dict) else {}
        digest = sidecar.get("passes") if isinstance(sidecar.get("passes"), dict) else {}
        job_file = name if kind == "ROS2-JOB" else f"{stem}.JBI"
        asf = (_ints(summary.get("asf")) or _ints(digest.get("asf"))
               or sorted({row[4] for row in rows if row[4] is not None}) or None)
        if asf is None and _int(query.get("arc")) is not None:
            asf = [_int(query.get("arc"))]
        ctwd = _num(summary.get("weld_ctwd_mm"))
        ctwd = _num(query.get("ctwd")) if ctwd is None else ctwd
        clean = query.get("clean_first")
        clean_first = (clean == "1") if clean in ("0", "1") else (
            summary.get("clean_first") if isinstance(summary.get("clean_first"), bool) else None)
        thermal = {key: query[key] for key in _THERMAL_QUERY if key in query}
        thermal.update({key: params[key] for key in ("thermal", "cooling", "thermal_profile") if key in params})
        measure = {key: query[key] for key in _MEASURE_QUERY if key in query}
        stops = sidecar.get("measure_stops", summary.get("measure_stops"))
        if stops is not None:
            measure["measure_stops"] = stops
        jbi_sha = _text(sidecar.get("sha256"))
        jbi_matches = (jbi_sha == files[job_file].sha256) if jbi_sha and job_file in files else None
        cycle = {"cycle": sidecar.get("cycle"), "stations": sidecar.get("stations")} if "cycle" in sidecar else None
        exported = _ts(sidecar.get("exported_at")) or _ts(listed.get(name, {}).get("added_at"))
        export_id = self.conn.execute(
            "INSERT INTO exports (run_id, file_name, job_file, seq, kind, job, exported_at, git_commit, arc_schedule, "
            "asf, weld_ctwd_mm, clean_first, layers, thermal, measure, platter, params, query, summary, cycle, "
            "jbi_sha256, jbi_matches, sidecar_sha256, inputs_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (run_id, name, job_file if job_file in files else None, seq, kind, job, exported,
             _text(sidecar.get("commit")),
             _text(query.get("arc_schedule")) or _text(query.get("arc")), asf, ctwd, clean_first,
             _ints(summary.get("layers")),
             _jsonb(thermal or None), _jsonb(measure or None), _jsonb(query.get("platter")),
             _jsonb(params or None), _jsonb(query or None), _jsonb(summary or None), _jsonb(cycle),
             jbi_sha, jbi_matches, f.sha256, inputs)).fetchone()[0]
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO passes (export_id, pass, layer, bead, region, asf, aef, v_min_mm_s, v_max_mm_s, "
                    "travel_mm_min, length_mm, weld_moves, first_step, last_step, dwell_s, pauses_before, "
                    "expected_s, model_area_mm2) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s)", [(export_id, *row) for row in rows])

    def pass_rows(self, run_id, stem, files, errors) -> tuple[list[tuple], str | None]:

        candidates = [name for name in (f"{stem}.passes.json", f"{stem}.passes.csv") if name in files]
        for name in candidates:
            _note(errors, name)
        for name in candidates:
            f = files[name]
            try:
                if name.endswith(".json"):
                    data = _json_object(self.read(f))
                    table, job = data.get("passes"), _text(data.get("job"))
                else:
                    table, job = list(csv.DictReader(io.StringIO(self.read(f).decode("utf-8-sig")))), None
                if not isinstance(table, list) or not all(isinstance(row, dict) for row in table):
                    raise ValueError("expected a list of pass rows")
                rows, numbers = [], set()
                for index, row in enumerate(table, 1):
                    number = _int(row.get("pass")) or index
                    if number in numbers:
                        raise ValueError(f"pass {number} appears twice")
                    numbers.add(number)
                    rows.append((number, *(_text(row.get(key)) or "" if key == "region"
                                          else _int(row.get(key)) if key in ("layer", "bead", "asf", "aef",
                                                                              "weld_moves", "first_step",
                                                                              "last_step", "pauses_before")
                                          else _num(row.get(key)) for key in pass_tables.FIELDS[1:])))
                return rows, job
            except (*_PARSE_ERRORS, UnicodeDecodeError) as exc:
                self.problem("warning", run_id, name, f"pass table: {exc}")
                _note(errors, name, _scrub_text(f"pass table: {exc}"))
        return [], None

    def arcmon(self, run_id, files, listed, errors) -> None:
        for (seq, kind, detail, ext, derived), name, f in self._ordered(run_id, files):
            kind = listed.get(name, {}).get("kind", kind)
            realtime = ext.lower() == "csv" and f.head.removeprefix(b"\xef\xbb\xbf").startswith(b"//ARC_MONDATA")
            if derived or not (kind == "ARCMON" or (kind == "ATTACH" and realtime)):
                continue
            if self.conn.execute("SELECT 1 FROM arcmon_files WHERE sha256 = %s", (f.sha256,)).fetchone():
                continue
            self.section(run_id, name, "arc monitor", lambda a=(name, ext, f): self.arcmon_file(run_id, *a), errors)

    def arcmon_file(self, run_id, name, ext, f) -> None:
        if ext.lower() == "dat":
            summary = arcmon.parse("ARCMON.DAT", self.read(f))
            self.conn.execute("INSERT INTO arcmon_files (sha256, run_id, file_name, format, summary) "
                              "VALUES (%s, %s, %s, 'arcmon-dat', %s)", (f.sha256, run_id, name, _jsonb(summary)))
            return

        summary, meta, rows = arcmon.read_realtime(self.read(f))
        period, header = meta["sampling_s"], meta["header"]
        length = arcmon._channel(meta, "LEN", "WELD LENGTH")
        length_scale = arcmon.unit_scale(meta, length, arcmon._LENGTH_UNITS)
        columns = {"amps": meta["current_index"], "volts": meta["voltage_index"],
                   "step": arcmon._channel(meta, "STEP", "STEP NO."),
                   "cond_no": arcmon._channel(meta, "FILE", "WELD COND.NO"),
                   "weld_length_mm": length if length_scale else None}
        used = {index for index in columns.values() if index is not None}
        channel_map = {"channels": meta["channels"], "header": header,
                       "columns": {key: header[index] for key, index in columns.items() if index is not None}}
        file_id = self.conn.execute(
            "INSERT INTO arcmon_files (sha256, run_id, file_name, format, job, controller_date, sample_period_s, "
            "samples, arc_on_a, merge_gap_samples, has_step, channel_map, summary) VALUES "
            "(%s, %s, %s, 'realtime-csv', %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (f.sha256, run_id, name, _text(meta.get("job")),
             dt.datetime.strptime(meta["date"], "%Y/%m/%d %H:%M:%S"),
             period, len(rows), arcmon.ARC_ON_A, max(1, round(arcmon.MERGE_GAP_S / period)),
             columns["step"] is not None, _jsonb(channel_map), _jsonb(summary))).fetchone()[0]

        def value(row, key):
            index = columns[key]
            if index is None or index >= len(row):
                return None
            return row[index] * length_scale if key == "weld_length_mm" else row[index]

        from psycopg.types.json import Jsonb
        with self.conn.cursor() as cur, cur.copy(
                "COPY arcmon_samples (file_id, idx, t_s, volts, amps, step, cond_no, weld_length_mm, other) "
                "FROM STDIN") as copy:
            for index, row in enumerate(rows):
                other = {header[i]: v for i, v in enumerate(row) if i not in used}
                copy.write_row((file_id, index, round(index * period, 6), value(row, "volts"), value(row, "amps"),
                                _whole(value(row, "step"), "STEP NO."), _whole(value(row, "cond_no"), "COND.NO"),
                                value(row, "weld_length_mm"), Jsonb(other, dumps=_dumps)))
        segments = [(file_id, s["segment"], s["start_s"], s["end_s"], s["first_step"], s["last_step"], s["cond_no"],
                     s["aef_no"], s["arc_on_s"], s["weld_s"], s["dropouts"], s["current_mean_a"], s["current_min_a"],
                     s["current_max_a"], s["voltage_mean_v"], s["voltage_min_v"], s["voltage_max_v"],
                     s["arc_current_mean_a"], s["arc_voltage_mean_v"], s["crater_current_mean_a"],
                     s["weld_length_mm"], s["weld_speed_mm_min"], s["heat_input_kj_mm"])
                    for s in arcmon.per_segment(meta, rows)]
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO arcmon_segments (file_id, segment, start_s, end_s, first_step, last_step, asf, aef, "
                "arc_on_s, weld_s, dropouts, current_mean_a, current_min_a, current_max_a, voltage_mean_v, "
                "voltage_min_v, voltage_max_v, arc_current_mean_a, arc_voltage_mean_v, crater_current_mean_a, "
                "weld_length_mm, speed_mm_min, heat_input_kj_mm) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", segments)

    def link_arcmon(self, run_id) -> None:


        exports = self.conn.execute("SELECT id, seq, job FROM exports WHERE run_id = %s AND kind = ANY(%s) "
                                    "ORDER BY seq", (run_id, list(_WELD_KINDS))).fetchall()
        for file_id, name, job in self.conn.execute("SELECT id, file_name, job FROM arcmon_files WHERE run_id = %s",
                                                    (run_id,)).fetchall():
            parts = _name_parts(run_id, name)
            before = [row for row in exports if parts and row[1] < parts[0]]
            named = [row for row in before if job and row[2] == job]
            chosen, how = (named[-1], "job") if named else ((before[-1], "latest-before") if before else (None, None))
            self.conn.execute("UPDATE arcmon_files SET export_id = %s, export_match = %s WHERE id = %s",
                              (chosen[0] if chosen else None, how, file_id))

    def vars(self, run_id, files, listed, errors) -> None:
        for (seq, kind, detail, ext, derived), name, f in self._ordered(run_id, files):
            if derived or kind not in _VAR_KINDS:
                continue
            if self.conn.execute("SELECT 1 FROM var_files WHERE sha256 = %s", (f.sha256,)).fetchone():
                continue
            self.section(run_id, name, "VAR.DAT", lambda a=(name, kind, f): self.var_file(run_id, *a, errors), errors)

    def var_file(self, run_id, name, kind, f, errors) -> None:
        sense, flag, count = var_dat.SENSE_BASE, var_dat.FLAG_BASE, var_dat.MAX_POINTS
        text = self.read(f).decode("latin-1")
        if "\n" not in text and "\r" not in text:
            raise ValueError("not a VAR.DAT export")
        pulses = var_dat.parse_position_pulses(text)
        try:
            flags = var_dat._byte_values(text)
        except ValueError as exc:
            flags = None
            self.problem("warning", run_id, name, f"VAR.DAT: {exc}")
            _note(errors, name, _scrub_text(f"VAR.DAT: {exc}"))
        var_id = self.conn.execute("INSERT INTO var_files (sha256, run_id, file_name, kind, b_values) "
                                   "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                   (f.sha256, run_id, name, kind, flags)).fetchone()[0]
        rows = []
        for index, axes in sorted(pulses.items()):
            hit = None
            if flags is not None and sense <= index < sense + count and flag + index - sense < len(flags):
                hit = flags[flag + index - sense]
            rows.append((var_id, index, *axes, hit))
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO var_positions (var_file_id, p_index, pulse_s, pulse_l, pulse_u, pulse_r, "
                            "pulse_b, pulse_t, hit_flag) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)


    def temperatures(self, run_id, record) -> None:
        self.conn.execute("DELETE FROM temperatures WHERE run_id = %s", (run_id,))
        rows = []
        for seq, item in enumerate(record.get("temperatures") or [], 1):
            if not isinstance(item, dict):
                raise ValueError(f"temperature {seq} is not an object")
            rows.append((run_id, seq, _ts(item.get("at")), _num(item.get("value")), _text(item.get("unit")),
                         _num(item.get("value_c")), _text(item.get("stage")), _text(item.get("note")),
                         _int(item.get("cycle"))))
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO temperatures (run_id, seq, at, value, unit, value_c, stage, note, cycle) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)

    def notes(self, run_id, record) -> None:
        self.conn.execute("DELETE FROM notes WHERE run_id = %s", (run_id,))
        rows = [(run_id, seq, _ts(item.get("at")), _text(item.get("text")))
                for seq, item in enumerate(record.get("notes") or [], 1) if isinstance(item, dict)]
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO notes (run_id, seq, at, note) VALUES (%s, %s, %s, %s)", rows)

    def layer_log(self, run_id, record) -> None:
        self.conn.execute("DELETE FROM layer_log WHERE run_id = %s", (run_id,))
        log, readings = [], []
        for row_no, item in enumerate(runs.layer_log(record), 1):
            unit = _text(item.get("unit"))
            layer = _int(item.get("layer"))
            if layer is None:
                raise ValueError(f"layer_log row {row_no} has no layer")
            log.append((run_id, row_no, layer, _int(item.get("pass")), unit, _num(item.get("pre_weld")),
                        _to_c(item.get("pre_weld"), unit), _num(item.get("park_travel_s_est")),
                        _num(item.get("height_mm")), _text(item.get("height_source")), _num(item.get("width_mm")),
                        _text(item.get("note")), _ts(item.get("at"))))
            for seconds, value in (item.get("readings") or {}).items():
                if _num(seconds) is None:
                    raise ValueError(f"layer_log row {row_no}: reading time {seconds!r} is not a number")
                readings.append((run_id, row_no, _num(seconds), _num(value), _to_c(value, unit)))
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO layer_log (run_id, row_no, layer, pass, unit, pre_weld, pre_weld_c, "
                            "park_travel_s_est, height_mm, height_source, width_mm, note, at) VALUES "
                            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", log)
            cur.executemany("INSERT INTO layer_readings (run_id, row_no, seconds, value, value_c) "
                            "VALUES (%s, %s, %s, %s, %s)", readings)

    def cycles(self, run_id, record, files, errors) -> None:


        self.conn.execute("DELETE FROM cycles WHERE run_id = %s", (run_id,))
        temperatures = [item for item in record.get("temperatures") or [] if isinstance(item, dict)]
        rows = []
        for cycle in record.get("cycles") or []:
            if not isinstance(cycle, dict) or _int(cycle.get("cycle")) is None:
                raise ValueError("a cycle without a cycle number")
            number = _int(cycle["cycle"])
            info = {**(cycle.get("summary") if isinstance(cycle.get("summary"), dict) else {}), **cycle}
            layers = cycle.get("layers") if isinstance(cycle.get("layers"), list) else [None, None]
            first, last = (_int(layers[0]), _int(layers[1])) if len(layers) == 2 else (None, None)
            error_range = info.get("error_range_mm") if isinstance(info.get("error_range_mm"), list) else [None, None]
            readings = {}
            for label, stage in (("t_before", "before-interpass"), ("t_after", "after-interpass")):
                reading = cycle.get(label) if isinstance(cycle.get(label), dict) else next(
                    (item for item in reversed(temperatures)
                     if item.get("cycle") == number and item.get("stage") == stage), {})
                readings[label] = (_num(reading.get("value_c")) if _num(reading.get("value_c")) is not None
                                   else _to_c(reading.get("value"), reading.get("unit")))
            sidecar = {}
            file_name = _text(cycle.get("file"))
            if file_name and file_name.endswith(".JBI") and file_name[:-4] + ".json" in files:
                try:
                    sidecar = _json_object(self.read(files[file_name[:-4] + ".json"]))
                except _PARSE_ERRORS as exc:
                    message = _scrub_text(f"cycle {number}: export settings not read: {exc}")
                    self.problem("warning", run_id, file_name[:-4] + ".json", message)
                    _note(errors, file_name[:-4] + ".json", message)
            params = sidecar.get("params") if isinstance(sidecar.get("params"), dict) else {}
            summary = sidecar.get("summary") if isinstance(sidecar.get("summary"), dict) else {}
            settings = {"bead_height_mm": params.get("lh"), "bead_width_mm": params.get("bead"),
                        "travel_mm_min": params.get("travel"), "wire_feed_m_min": summary.get("wfs_m_min"),
                        "weld_ctwd_mm": summary.get("weld_ctwd_mm")}
            if isinstance(cycle.get("build_settings"), dict):
                settings.update(cycle["build_settings"])
            rows.append((run_id, number, first, last,
                         last - first + 1 if first is not None and last is not None else _int(cycle.get("n")),
                         _text(cycle.get("status")), _text(cycle.get("job")), file_name,
                         _text(cycle.get("var_file")), _num(info.get("step_mm")), _num(info.get("growth_raw_mm")),
                         _num(info.get("growth_mm")),
                         info.get("clamped") if isinstance(info.get("clamped"), bool) else None,
                         _num(info.get("mean_error_mm")), _num(error_range[0]) if len(error_range) == 2 else None,
                         _num(error_range[1]) if len(error_range) == 2 else None, _int(info.get("touches")),
                         _num(info.get("spacing_mm")), _num(info.get("interpass_s")),
                         _ts(cycle.get("exported_at")), _ts(cycle.get("imported_at")), _ts(cycle.get("applied_at")),
                         readings["t_before"], readings["t_after"],
                         *(_num(settings.get(key)) for key in ("bead_height_mm", "bead_width_mm", "travel_mm_min",
                                                               "wire_feed_m_min", "weld_ctwd_mm")),
                         _jsonb(cycle.get("summary")), _jsonb(cycle)))
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO cycles (run_id, cycle, first_layer, last_layer, n, status, job, file_name, var_file, "
                "step_mm, growth_raw_mm, growth_mm, clamped, mean_error_mm, min_error_mm, max_error_mm, touches, "
                "spacing_mm, interpass_s, exported_at, imported_at, applied_at, t_before_c, t_after_c, "
                "bead_height_mm, bead_width_mm, travel_mm_min, wire_feed_m_min, weld_ctwd_mm, summary, record) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)

    def plate(self, run_id, files, listed) -> None:

        plate = {"corner_vars": [], "sense_vars": []}
        for name in sorted(files):
            kind = listed.get(name, {}).get("kind")
            if kind in ("PLATE-CORNERS-VAR", "PLATE-SENSE-VAR"):
                plate["corner_vars" if kind == "PLATE-CORNERS-VAR" else "sense_vars"].append(name)
        row = self.conn.execute("SELECT file_name, params FROM exports WHERE run_id = %s "
                                "AND params ? 'substrate_registration' ORDER BY seq DESC LIMIT 1",
                                (run_id,)).fetchone()
        if row is not None:
            params = row[1]
            registration = params.get("substrate_registration")
            if isinstance(registration, str) and registration.strip():
                registration = json.loads(registration)
            if isinstance(registration, dict):
                sensed = registration.get("sensed") if isinstance(registration.get("sensed"), dict) else {}
                plate["registration"] = {
                    "export": row[0], "version": registration.get("version"), "frame": registration.get("frame"),
                    "corners": len(registration.get("corners") or []),
                    "sensed_points": len(sensed.get("points") or []),
                    "weld_ctwd_mm": registration.get("weld_ctwd_mm"),
                    "gap_direction": registration.get("gap_direction"),
                    "tool0_tip_offset_mm": registration.get("tool0_tip_offset_mm")}
            plate["substrate_mm"] = _num(params.get("sub"))
        self.conn.execute("UPDATE runs SET plate = %s WHERE run_id = %s", (_jsonb(plate), run_id))


def ingest(conn, saves: Path, *, mirror: bool = False, workspace: str | None = None, run: str | None = None,
           dry_run: bool = False, read_role: str | None = READ_ROLE) -> Report | None:

    report = Report()
    schema = pin(conn)
    worker = _Ingest(conn, saves, mirror=mirror, workspace=workspace, report=report, dry_run=dry_run)
    started = dt.datetime.now(dt.timezone.utc)
    if not conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", (_LOCK_CLASS + 1, schema)).fetchone()[0]:
        return None
    try:

        with conn.transaction(force_rollback=True) if dry_run else contextlib.nullcontext():
            migrate(conn, read_role=read_role)
            if not read_role:
                worker.problem("warning", None, None, _NO_READER)
            worker.all_runs(workspace, run)
            if not dry_run:
                conn.execute("INSERT INTO ingest_runs (started_at, finished_at, saves, runs_seen, runs_new, "
                             "runs_updated, runs_unchanged, runs_refused, problems) VALUES "
                             "(%s, now(), %s, %s, %s, %s, %s, %s, %s)",
                             (started, str(worker.saves), report.seen, report.new, report.updated,
                              report.unchanged, report.refused, _jsonb(report.problems[:500])))
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (_LOCK_CLASS + 1, schema))
    return report


def status(conn) -> dict:

    from psycopg import sql
    pin(conn)
    out = {"migrations": [], "tables": {}, "last_ingest": None}
    if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is not None:
        out["migrations"] = [name for (name,) in conn.execute(
            "SELECT name FROM schema_migrations ORDER BY version").fetchall()]
    for table in TABLES:
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None:
            out["tables"][table] = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
    if "ingest_runs" in out["tables"]:
        row = conn.execute("SELECT finished_at, saves, runs_seen, runs_new, runs_updated, runs_unchanged, "
                           "runs_refused, jsonb_array_length(problems) FROM ingest_runs "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            out["last_ingest"] = dict(zip(("finished_at", "saves", "seen", "new", "updated", "unchanged",
                                           "refused", "problems"), row))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", help=f"libpq connection string (default ${DSN_ENV}, else {DEFAULT_DSN!r})")
    parser.add_argument("--read-role", default=READ_ROLE,
                        help=f"role granted SELECT on the index (default {READ_ROLE}); '' skips the grant")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("ingest", help="index the run archive (read-only on the files)")
    run_parser.add_argument("--saves", type=Path, default=Path(__file__).resolve().parents[2] / "saves")
    run_parser.add_argument("--mirror", action="store_true",
                            help="--saves is a OneDrive copy (<person>/Runs/<run>/), not the VM's saves/")
    run_parser.add_argument("--workspace")
    run_parser.add_argument("--run")
    run_parser.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    commands.add_parser("migrate", help="apply pending schema migrations")
    commands.add_parser("status", help="row counts and the last ingest")
    args = parser.parse_args(argv)
    try:
        psycopg = _psycopg()
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    try:
        with connect(args.dsn) as conn:
            if args.command == "migrate":
                applied = migrate(conn, read_role=args.read_role)
                print("applied: " + (", ".join(f"{v:04d}" for v in applied) if applied else "nothing pending"))
                if not args.read_role:
                    print(f"WARNING {_NO_READER}")
                return 0
            if args.command == "status":
                info = status(conn)
                print("schema: " + (", ".join(info["migrations"]) or "none (run ingest)"))
                last = info["last_ingest"]
                print("last ingest: " + ("never" if last is None else
                                         f"{last['finished_at']:%Y-%m-%d %H:%M:%S %z} ({last['seen']} seen, "
                                         f"{last['new']} new, {last['updated']} updated, {last['unchanged']} "
                                         f"unchanged, {last['refused']} refused, {last['problems']} problems)"))
                for table, count in info["tables"].items():
                    print(f"  {table:<22}{count:>10}")
                return 0
            report = ingest(conn, args.saves, mirror=args.mirror, workspace=args.workspace, run=args.run,
                            dry_run=args.dry_run, read_role=args.read_role)
    except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    if report is None:
        print("another ingest is running; skipped")
        return 0
    for item in report.problems:
        where = "/".join(part for part in (item["run"], item["file"]) if part) or "-"
        print(f"{item['level'].upper()} {where}: {item['message']}", file=sys.stderr if item["level"] == "error"
              else sys.stdout)
    print(("dry run, rolled back: " if args.dry_run else "") +
          f"{report.seen} runs seen, {report.new} new, {report.updated} updated, {report.unchanged} unchanged, "
          f"{report.refused} refused; {len(report.problems) - report.errors} warnings, {report.errors} errors")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
