from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from . import arcmon, runs
from .job_archive import safe_read

SCHEMA_VERSION = 1
CATALOG_NAME = ".run-logs.sqlite3"
_CATALOG_LOCK = threading.RLock()
_TEXT_FIELDS = ("specimen_id", "substrate_id", "material", "wire_material", "shielding_gas", "arc_file")
_NUMBER_LIMITS = {"wire_diameter_mm": 20, "gas_flow_l_min": 200,
                  "travel_mm_min": 100000, "wire_feed_m_min": 100,
                  "voltage_v": 200, "ctwd_mm": 200}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, operator TEXT NOT NULL,
    owner_key TEXT NOT NULL, name TEXT, part TEXT NOT NULL, status TEXT NOT NULL,
    outcome TEXT, data_class TEXT, started_at TEXT NOT NULL, finished_at TEXT,
    revision TEXT NOT NULL, source_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_access (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    email TEXT NOT NULL, PRIMARY KEY (run_id, email)
);
CREATE INDEX IF NOT EXISTS run_access_email ON run_access(email);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE TABLE IF NOT EXISTS setup (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    specimen_id TEXT, substrate_id TEXT, material TEXT, wire_material TEXT,
    shielding_gas TEXT, arc_file TEXT, wire_diameter_mm REAL, gas_flow_l_min REAL,
    travel_mm_min REAL, wire_feed_m_min REAL, voltage_v REAL, ctwd_mm REAL
);
CREATE TABLE IF NOT EXISTS layers (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL, layer INTEGER NOT NULL, pass INTEGER, pass_base INTEGER,
    unit TEXT NOT NULL, pre_weld REAL, pre_weld_value_c REAL, park_travel_s_est REAL,
    height_mm REAL, height_source TEXT, width_mm REAL, note TEXT, at TEXT,
    current_a REAL, voltage_v REAL,
    source_json TEXT NOT NULL, PRIMARY KEY (run_id, row_number)
);
CREATE TABLE IF NOT EXISTS layer_readings (
    run_id TEXT NOT NULL, row_number INTEGER NOT NULL, seconds REAL NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, value_c REAL NOT NULL,
    PRIMARY KEY (run_id, row_number, seconds),
    FOREIGN KEY (run_id, row_number) REFERENCES layers(run_id, row_number) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS temperatures (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    observation INTEGER NOT NULL, at TEXT, value REAL NOT NULL, unit TEXT NOT NULL,
    value_c REAL NOT NULL, stage TEXT, note TEXT, cycle INTEGER,
    PRIMARY KEY (run_id, observation)
);
CREATE TABLE IF NOT EXISTS audit (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (run_id, event)
);
"""

_REPORT_RUN_FIELDS = (
    "run", "name", "part", "operator", "status", "outcome", "data_class", "started_at", "finished_at",
    "setup_specimen_id", "setup_substrate_id", "setup_material", "setup_wire_material",
    "setup_shielding_gas", "setup_arc_file", "setup_wire_diameter_mm", "setup_gas_flow_l_min",
    "setup_travel_mm_min", "setup_wire_feed_m_min", "setup_voltage_v", "setup_ctwd_mm",
)
_REPORT_FIELDS = {
    "runs": (),
    "layers": ("layer", "pass", "pass_base", "unit", "pre_weld", "pre_weld_value_c", "park_travel_s_est",
               "height_mm", "height_source", "width_mm", "current_a", "voltage_v", "note", "at"),
    "cooling": ("layer", "pass", "pass_base", "seconds", "value", "unit", "value_c", "at"),
    "temperatures": ("observation", "at", "value", "unit", "value_c", "stage", "note", "cycle"),
}
_REPORT_SELECT = """
SELECT r.run_id AS run, r.name, r.part, r.operator, r.status, r.outcome, r.data_class,
       r.started_at, r.finished_at,
       s.specimen_id AS setup_specimen_id, s.substrate_id AS setup_substrate_id,
       s.material AS setup_material, s.wire_material AS setup_wire_material,
       s.shielding_gas AS setup_shielding_gas, s.arc_file AS setup_arc_file,
       s.wire_diameter_mm AS setup_wire_diameter_mm, s.gas_flow_l_min AS setup_gas_flow_l_min,
       s.travel_mm_min AS setup_travel_mm_min, s.wire_feed_m_min AS setup_wire_feed_m_min,
       s.voltage_v AS setup_voltage_v, s.ctwd_mm AS setup_ctwd_mm
"""
_REPORT_QUERIES = {
    "runs": "FROM runs AS r LEFT JOIN setup AS s ON s.run_id = r.run_id",
    "layers": """, l.layer, l.pass, l.pass_base, l.unit, l.pre_weld, l.pre_weld_value_c,
        l.park_travel_s_est, l.height_mm, l.height_source, l.width_mm, l.current_a, l.voltage_v, l.note, l.at
        FROM runs AS r LEFT JOIN setup AS s ON s.run_id = r.run_id
        JOIN layers AS l ON l.run_id = r.run_id""",
    "cooling": """, l.layer, l.pass, l.pass_base, c.seconds, c.value, c.unit, c.value_c, l.at
        FROM runs AS r LEFT JOIN setup AS s ON s.run_id = r.run_id
        JOIN layers AS l ON l.run_id = r.run_id
        JOIN layer_readings AS c ON c.run_id = l.run_id AND c.row_number = l.row_number""",
    "temperatures": """, t.observation, t.at, t.value, t.unit, t.value_c, t.stage, t.note, t.cycle
        FROM runs AS r LEFT JOIN setup AS s ON s.run_id = r.run_id
        JOIN temperatures AS t ON t.run_id = r.run_id""",
}
_REPORT_SCOPE = """
WHERE (r.owner_key = ? OR EXISTS (
    SELECT 1 FROM run_access AS access
    WHERE access.run_id = r.run_id AND access.email = ?))
"""


_REPORT_ORDER = {
    "runs": "ORDER BY r.started_at DESC, r.run_id",
    "layers": "ORDER BY r.started_at DESC, r.run_id, l.layer, l.pass NULLS FIRST, l.row_number",
    "cooling": "ORDER BY r.started_at DESC, r.run_id, l.layer, l.pass NULLS FIRST, l.row_number, c.seconds",
    "temperatures": "ORDER BY r.started_at DESC, r.run_id, t.observation",
}


class ConflictError(ValueError):
    pass


class CatalogError(OSError):
    pass


def _actor(value: str) -> str:
    value = runs._text(value, "signed-in identity", 320).strip().casefold()
    if not value:
        raise PermissionError("A signed-in identity is required for run logs.")
    if len(value.encode("utf-16-le")) > 640:
        raise ValueError("signed-in identity exceeds the SQL email length limit")
    return value


def _collaborators(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("collaborators must be a list of at most 50 email addresses")
    result = set()
    for address in value:
        address = _actor(address)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise ValueError("collaborators must be email addresses")
        result.add(address)
    return sorted(result)


def validate_setup(value) -> dict:

    if not isinstance(value, dict) or set(value) - {*_TEXT_FIELDS, *_NUMBER_LIMITS}:
        raise ValueError("invalid run setup fields")
    result = {}
    for key, item in value.items():
        if item is None:
            result[key] = None
        elif key in _TEXT_FIELDS:
            item = runs._text(item, key, 120).strip()
            if any(ord(character) < 32 for character in item):
                raise ValueError(f"{key} must be a single line")
            result[key] = item or None
        else:
            maximum = _NUMBER_LIMITS[key]
            if not runs._finite(item) or not 0 <= item <= maximum:
                raise ValueError(f"{key} must be finite and in 0..{maximum}")
            result[key] = float(item)
    return result


def catalog_path(root: Path) -> Path:
    root = Path(root).absolute()
    runs._no_links(root)
    path = root / CATALOG_NAME
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        runs._no_links(candidate)
        if candidate.exists():
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("SQL catalog must contain regular, unlinked files")
    return path


@contextmanager
def _database(root: Path):
    from . import run_log_postgres

    if run_log_postgres.DSN_ENV in os.environ:
        dsn = os.environ[run_log_postgres.DSN_ENV]
        if not dsn.strip():
            raise CatalogError(f"{run_log_postgres.DSN_ENV} is set but empty.")
        try:
            with run_log_postgres.connect(dsn) as connection:
                yield connection
        except run_log_postgres.RunLogDatabaseError as exc:
            raise CatalogError(str(exc)) from None
        return
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise CatalogError("Unsupported run-log SQL schema; rebuild the catalog from the run archives.")
        connection.executescript(_SCHEMA)
        connection.execute("PRAGMA user_version = 1")
        yield connection
    except sqlite3.Error as exc:
        raise CatalogError("Run-log SQL catalog is unavailable; the canonical run archives are retained.") from exc
    finally:
        if connection is not None:
            connection.close()


def _read(root: Path, workspace: str, run_id: str) -> tuple[dict, str]:
    folder = runs._folder(root, workspace, run_id)
    body = safe_read(folder / f"{run_id}_run.json", runs.MAX_FILE_BYTES)
    record = json.loads(body)
    if (not isinstance(record, dict) or record.get("run") != run_id
            or record.get("workspace") != workspace or record.get("status") not in ("open", "finished")
            or not isinstance(record.get("operator"), str)):
        raise ValueError("invalid run metadata")
    runs.layer_log(record)

    if "collaborators" in record:
        _collaborators(record["collaborators"])
    return record, hashlib.sha256(body).hexdigest()


def _access(record: dict, actor: str, admin: bool) -> tuple[bool, bool]:
    manage = admin or record["operator"].strip().casefold() == actor
    shared = actor in _collaborators(record.get("collaborators", []))
    return manage or shared, manage


def _detail(record: dict, revision: str, actor: str, admin: bool) -> dict:
    allowed, manage = _access(record, actor, admin)
    if not allowed:
        raise PermissionError("This run has not been shared with your account.")
    return {"record": record, "revision": revision, "can_manage": manage}


def _celsius(value, unit):
    if unit not in ("F", "C"):
        raise ValueError("invalid archived temperature unit")
    if value is None:
        return None
    if not runs._finite(value):
        raise ValueError("invalid archived temperature value")
    return (value - 32) * 5 / 9 if unit == "F" else value


def _integer(value, label: str, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not 0 <= value <= runs.MAX_LOG_INDEX:
        raise ValueError(f"archived {label} is outside the supported whole-number range")
    return value


def _catalog_copy(value):


    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, list):
        return [_catalog_copy(item) for item in value]
    if isinstance(value, dict):
        return {_catalog_copy(key): _catalog_copy(item) for key, item in value.items()}
    return value


def _index(connection, record: dict, revision: str) -> None:
    record = _catalog_copy(record)
    run_id = record["run"]
    connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    connection.execute("""INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (run_id, record["workspace"], record["operator"], record["operator"].strip().casefold(),
                        record.get("name"), record["part"], record["status"], record.get("outcome"),
                        record.get("data_class"), record["started_at"], record.get("finished_at"), revision,
                        runs._json(record).decode("utf-8")))
    connection.executemany("INSERT INTO run_access VALUES (?, ?)",
                           [(run_id, email) for email in _collaborators(record.get("collaborators", []))])
    setup = record.get("setup") or {}
    if not isinstance(setup, dict):
        raise ValueError("invalid archived setup")
    connection.execute("INSERT INTO setup VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (run_id, *(setup.get(key) for key in (*_TEXT_FIELDS, *_NUMBER_LIMITS))))
    for number, row in enumerate(runs.layer_log(record)):
        unit = row["unit"]
        connection.execute("INSERT INTO layers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (run_id, number, _integer(row["layer"], "layer"),
                            _integer(row.get("pass"), "pass", optional=True), row.get("pass_base"), unit,
                            row.get("pre_weld"), _celsius(row.get("pre_weld"), unit),
                            row.get("park_travel_s_est"), row.get("height_mm"), row.get("height_source"),
                            row.get("width_mm"), row.get("note"), row.get("at"), row.get("current_a"),
                            row.get("voltage_v"), runs._json(row).decode("utf-8")))
        for seconds, value in row.get("readings", {}).items():
            elapsed = float(seconds)
            if not runs._finite(elapsed) or not 0 <= elapsed <= 7200:
                raise ValueError("invalid archived reading time")
            connection.execute("INSERT INTO layer_readings VALUES (?, ?, ?, ?, ?, ?)",
                               (run_id, number, elapsed, value, unit, _celsius(value, unit)))
    for number, row in enumerate(record.get("temperatures", [])):
        connection.execute("INSERT INTO temperatures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (run_id, number, row.get("at"), row["value"], row["unit"],
                            _celsius(row["value"], row["unit"]), row.get("stage"), row.get("note"),
                            _integer(row.get("cycle"), "cycle", optional=True)))
    for number, row in enumerate(record.get("audit", [])):
        connection.execute("INSERT INTO audit VALUES (?, ?, ?, ?, ?)",
                           (run_id, number, row["actor"], row["action"], row["at"]))


def _refresh(connection, root: Path) -> int:
    root = Path(root).absolute()
    runs._no_links(root)
    known = {row["run_id"]: row["revision"] for row in connection.execute("SELECT run_id, revision FROM runs")}
    found = set()

    with connection:
        for workspace in sorted(root.iterdir()) if root.exists() else []:
            if not runs.WORKSPACE_RE.fullmatch(workspace.name):
                continue
            with runs._lock(workspace.name):
                base = runs._base(root, workspace.name)
                if not base.exists():
                    continue
                for candidate in sorted(base.iterdir()):
                    if not runs.RUN_RE.fullmatch(candidate.name):
                        continue
                    record, revision = _read(root, workspace.name, candidate.name)
                    if record["run"] in found:
                        raise ValueError("duplicate run identity in archive")
                    found.add(record["run"])
                    if known.get(record["run"]) != revision:
                        _index(connection, record, revision)
        connection.executemany("DELETE FROM runs WHERE run_id = ?", [(key,) for key in known.keys() - found])
    return len(found)


def refresh_catalog(root: Path) -> dict:

    with _CATALOG_LOCK, _database(root) as connection:
        count = _refresh(connection, root)
    return {"schema_version": SCHEMA_VERSION, "runs": count}


def _locate(root: Path, run_id: str) -> str:
    runs._run_id(run_id)
    with _CATALOG_LOCK, _database(root) as connection:
        _refresh(connection, root)
        row = connection.execute("SELECT workspace FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise FileNotFoundError("Run not found.")
    return row["workspace"]


def list_runs(root: Path, actor: str, *, admin=False, search="", status="") -> list[dict]:
    actor = _actor(actor)
    search = runs._text(search, "search", 120).strip().casefold()
    if status not in ("", "open", "finished"):
        raise ValueError("invalid run status filter")
    with _CATALOG_LOCK, _database(root) as connection:
        _refresh(connection, root)
        candidates = connection.execute("""
            SELECT run_id, workspace FROM runs
            WHERE (? = 1 OR owner_key = ? OR EXISTS (
                SELECT 1 FROM run_access WHERE run_access.run_id = runs.run_id AND email = ?))
            AND (? = '' OR status = ?)
            ORDER BY started_at DESC, run_id DESC
            """, (1 if admin else 0, actor, actor, status, status)).fetchall()
    result = []
    for candidate in candidates:
        with runs._lock(candidate["workspace"]):
            record, _ = _read(root, candidate["workspace"], candidate["run_id"])
            allowed, manage = _access(record, actor, admin)
            if not allowed or (status and record["status"] != status):
                continue
            setup = record.get("setup") or {}
            searchable = " ".join(str(record.get(key) or "") for key in ("run", "name", "part", "operator"))
            searchable += " " + " ".join(str(value or "") for value in setup.values())
            if search and search not in searchable.casefold():
                continue
            result.append({key: record.get(key) for key in (
                "run", "name", "part", "operator", "status", "started_at", "finished_at", "outcome", "data_class")}
                | {"setup": setup, "can_manage": manage})
    return result


def get_run(root: Path, actor: str, run_id: str, *, admin=False) -> dict:
    actor = _actor(actor)
    workspace = _locate(root, run_id)
    with runs._lock(workspace):
        return _detail(*_read(root, workspace, run_id), actor, admin)


def reporting_fields(dataset: str) -> tuple[str, ...]:

    if not isinstance(dataset, str) or dataset not in _REPORT_FIELDS:
        raise ValueError("invalid reporting dataset")
    return _REPORT_RUN_FIELDS + _REPORT_FIELDS[dataset]


def reporting_rows(root: Path, actor: str, dataset: str) -> list[dict]:

    actor = _actor(actor)
    reporting_fields(dataset)


    query = _REPORT_SELECT + _REPORT_QUERIES[dataset] + _REPORT_SCOPE + _REPORT_ORDER[dataset]
    with _CATALOG_LOCK, _database(root) as connection:
        _refresh(connection, root)
        return [dict(row) for row in connection.execute(query, (actor, actor))]


def _refresh_after_write(root: Path) -> None:
    try:
        refresh_catalog(root)
    except (OSError, ValueError) as exc:
        raise CatalogError("Run saved in its canonical archive, but SQL refresh failed. Reload before retrying.") from exc


def create_run(root: Path, workspace: str, actor: str, *, name, setup=None,
               collaborators=None, commit=None) -> dict:

    with _CATALOG_LOCK:
        return _create_run(root, workspace, actor, name=name, setup=setup,
                           collaborators=collaborators, commit=commit)


def _create_run(root: Path, workspace: str, actor: str, *, name, setup=None,
                collaborators=None, commit=None) -> dict:
    actor = _actor(actor)
    name = runs._text(name, "run name", 40).strip()
    if not name:
        raise ValueError("A run name is required.")
    setup = validate_setup({} if setup is None else setup)
    collaborators = _collaborators([] if collaborators is None else collaborators)

    refresh_catalog(root)
    with runs._lock(workspace):
        record = runs.start(root, workspace, operator=actor, name=name, commit=commit,
                            setup=setup, collaborators=collaborators, audit_actor=actor)
        result = _detail(*_read(root, workspace, record["run"]), actor, False)
    _refresh_after_write(root)
    return result


def _fields(data: dict, allowed: set[str]) -> None:
    if set(data) - allowed:
        raise ValueError("invalid fields for run action")


def _finish(root, workspace, actor, data):
    _fields(data, {"outcome", "data_class", "height_mm", "width_mm", "measurements", "notes",
                   "arcmon_waiver", "waiver_reason", "waiver"})
    waiver = data.get("waiver")
    if "arcmon_waiver" in data:
        if type(data["arcmon_waiver"]) is not bool:
            raise ValueError("arcmon_waiver must be true or false")
        waiver = {"reason": data.get("waiver_reason", "")} if data["arcmon_waiver"] else None
    measurements = data.get("measurements")
    if "height_mm" in data or "width_mm" in data:
        measurements = {key: data.get(key) for key in ("height_mm", "width_mm")}
    return runs.finish(root, workspace, outcome=data.get("outcome"),
                       data_class=data.get("data_class", "preliminary"), measurements=measurements,
                       notes=data.get("notes", ""), waiver=waiver, by=actor, audit_actor=actor)


def update_run(root: Path, actor: str, run_id: str, *, revision, action, data, admin=False) -> dict:
    with _CATALOG_LOCK:
        return _update_run(root, actor, run_id, revision=revision, action=action, data=data, admin=admin)


def _update_run(root: Path, actor: str, run_id: str, *, revision, action, data, admin=False) -> dict:
    actor = _actor(actor)
    if not isinstance(data, dict):
        raise ValueError("run action data must be an object")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ConflictError("Reload the run to obtain its current revision before saving.")
    if action not in ("setup", "layer-row", "temperature", "note", "finish", "add"):
        raise ValueError("invalid run action")
    workspace = _locate(root, run_id)
    with runs._lock(workspace):
        record, current_revision = _read(root, workspace, run_id)
        detail = _detail(record, current_revision, actor, admin)
        if action in ("setup", "finish") and not detail["can_manage"]:
            raise PermissionError("Only the run owner or an administrator can manage setup, sharing or finish this run.")
        if revision != current_revision:
            raise ConflictError("This run changed since you opened it. Reload it before saving your entry.")
        folder = runs._folder(root, workspace, run_id)
        if record["status"] != "open" or runs._sealed(root, workspace, record):
            raise ValueError(runs.FINISHED)
        active = runs._active(root, workspace)
        if active is None or active["run"] != run_id:
            raise ConflictError("This is no longer the active run. Reload the run list.")
        if action == "setup":
            _fields(data, {"setup", "collaborators"})
            if "setup" in data:
                record["setup"] = {**(record.get("setup") or {}), **validate_setup(data["setup"])}
            if "collaborators" in data:
                record["collaborators"] = _collaborators(data["collaborators"])
            runs._audit(record, actor, action)
            runs._save(root, workspace, record, folder)
        elif action == "layer-row":
            runs.set_layer_row(root, workspace, data, audit_actor=actor)
        elif action == "temperature":
            _fields(data, {"value", "unit", "stage", "note", "cycle"})
            runs.add_temperature(root, workspace, value=data.get("value"), unit=data.get("unit"),
                                 stage=data.get("stage"), note=data.get("note", ""), cycle=data.get("cycle"),
                                 audit_actor=actor)
        elif action == "note":
            _fields(data, {"text"})
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("A note is required.")
            runs.add_note(root, workspace, text=text, audit_actor=actor)
        elif action == "finish":
            _finish(root, workspace, actor, data)
        else:
            _fields(data, {"body", "name", "kind"})
            kind = data.get("kind", "ATTACH")
            if kind not in ("ATTACH", "ARCMON"):
                raise ValueError("invalid attachment kind")
            name = runs._basename(data.get("name"))
            body = data.get("body")
            if not isinstance(body, bytes) or len(body) > runs.MAX_FILE_BYTES:
                raise ValueError("single file limit: 25 MB")
            parsed = arcmon.parse(name, body) if kind == "ARCMON" else None
            runs.add_file(root, workspace, kind=kind, body=body, detail=name,
                          ext=Path(name).suffix, source="robot" if kind == "ARCMON" else "operator",
                          arcmon_summary=parsed, audit_actor=actor)
        result = _detail(*_read(root, workspace, run_id), actor, admin)
    _refresh_after_write(root)
    return result
