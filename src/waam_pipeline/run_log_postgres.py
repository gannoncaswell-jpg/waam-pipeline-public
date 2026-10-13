from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sys

DSN_ENV = "WAAM_RUN_LOG_PG_DSN"
DEFAULT_DSN = "dbname=waam_runs"
SCHEMA = "runlog"
READ_ROLE = "waam_read"
SQL_DIR = Path(__file__).resolve().parent / "sql" / "runlog"
_LOCK_CLASS = 0x524C4F47
_MIGRATION_RE = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
_BOOTSTRAP = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    sha256      text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now())"""
_NOT_MIGRATED = ("PostgreSQL run-log schema is missing or out of date; run "
                 "`python -m waam_pipeline.run_log_postgres migrate` as the service user.")
_PASSWORD_RE = re.compile(r"(\w*password\s*=\s*)('(?:[^'\\]|\\.)*'?|\S+)", re.IGNORECASE)
_URI_PASSWORD_RE = re.compile(r"(://[^:/@\s]*:)[^@\s]*@")


class RunLogDatabaseError(OSError):
    pass


def _psycopg():
    try:
        import psycopg
    except ImportError:
        raise RunLogDatabaseError("PostgreSQL run logs are configured but psycopg 3 is not installed: "
                                  "pip install 'psycopg[binary]>=3.2,<4'") from None
    return psycopg


def redact(text: str, dsn: str | None = None) -> str:

    secret = None
    if dsn:
        try:
            from psycopg.conninfo import conninfo_to_dict
            secret = conninfo_to_dict(dsn).get("password")
        except Exception:
            secret = None
    if secret:
        text = text.replace(str(secret), "***")
    text = _URI_PASSWORD_RE.sub(r"\1***@", _PASSWORD_RE.sub(r"\1***", text))
    return " ".join(text.split())


def _failure(exc, dsn: str | None = None) -> str:


    state = getattr(exc, "sqlstate", None)
    message = exc.diag.message_primary if state else None
    if message:
        detail = f"SQLSTATE {state}: {redact(message, dsn)}"
    else:
        text = redact(str(exc), dsn)
        detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return f"PostgreSQL run-log operation failed ({detail})."


def _ahead(applied, local) -> str | None:

    extra = sorted(set(applied) - set(local))
    if not extra:
        return None
    return (f"PostgreSQL schema {SCHEMA} has migration {', '.join(f'{v:04d}' for v in extra)} applied, "
            "which this checkout does not have: the database is newer than this code. Deploy the "
            "checkout that added it; never migrate an older checkout over it.")


def _pin_search_path(conn) -> None:
    from psycopg import sql


    conn.execute(sql.SQL("SET search_path = {}, pg_temp").format(sql.Identifier(SCHEMA)))


def migrations(directory: Path | None = None) -> list[tuple[int, str, str]]:
    found = []
    for path in sorted(Path(directory or SQL_DIR).iterdir()):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match:
            found.append((int(match[1]), path.stem, path.read_text(encoding="utf-8").replace("\r\n", "\n")))
        elif path.suffix == ".sql":
            raise RuntimeError(f"migration {path.name}: name it NNNN_lowercase_words.sql")
    if [version for version, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migrations must be numbered 0001, 0002, ... with no gaps")
    return found


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Connection:


    def __init__(self, raw, row_factory):
        self._raw = raw
        self._row_factory = row_factory

    @staticmethod
    def _sql(statement: str) -> str:


        return statement.replace("%", "%%").replace("?", "%s")

    def execute(self, statement, parameters=()):

        return self._raw.cursor(row_factory=self._row_factory).execute(self._sql(statement), tuple(parameters))

    def executemany(self, statement, parameters):
        rows = [tuple(row) for row in parameters]
        if rows:
            self._raw.cursor().executemany(self._sql(statement), rows)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False


@contextmanager
def connect(dsn: str):

    psycopg = _psycopg()
    from psycopg.rows import dict_row
    raw = None
    try:
        raw = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        _pin_search_path(raw)
        raw.execute("SET statement_timeout = '30s'")
        ready = raw.execute("SELECT current_schema() = %s AND to_regclass('schema_migrations') IS NOT NULL",
                            (SCHEMA,)).fetchone()[0]
        expected = [(version, _digest(body)) for version, _, body in migrations()]
        applied = raw.execute("SELECT version, sha256 FROM schema_migrations "
                              "ORDER BY version").fetchall() if ready else []
        if not ready or applied != expected:
            raise RunLogDatabaseError(_ahead([v for v, _ in applied], [v for v, _ in expected]) or _NOT_MIGRATED)
        raw.autocommit = False
        yield Connection(raw, dict_row)
    except psycopg.Error as exc:
        raise RunLogDatabaseError(_failure(exc, dsn)) from None
    finally:

        if raw is not None:
            raw.close()


def _grant_reader(conn, role: str) -> None:


    from psycopg import sql
    names = {"schema": sql.Identifier(SCHEMA), "role": sql.Identifier(role)}
    for statement in ("GRANT USAGE ON SCHEMA {schema} TO {role}",
                      "GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}",
                      "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}"):
        conn.execute(sql.SQL(statement).format(**names))


def migrate(dsn: str, read_role: str | None = READ_ROLE) -> list[int]:


    psycopg = _psycopg()
    from psycopg import sql
    pending = migrations()
    applied = []
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:

            if read_role and conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                          (read_role,)).fetchone() is None:
                raise RunLogDatabaseError(
                    f"Read role {read_role!r} does not exist. Create it first (CREATE ROLE {read_role} NOLOGIN, "
                    "docs/run-logs.md), or pass --read-role '' to migrate without a reporting grant.")
            with conn.transaction():
                conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
                owned = conn.execute("SELECT pg_get_userbyid(nspowner) = current_user FROM pg_namespace "
                                     "WHERE nspname = %s", (SCHEMA,)).fetchone()[0]

            if not owned:
                raise RunLogDatabaseError(f"Schema {SCHEMA} belongs to another role; run migrate as its owner "
                                          "(the service user), or drop it first.")
            _pin_search_path(conn)
            lock = "SELECT pg_advisory_xact_lock(%s, hashtext(%s))"
            with conn.transaction():
                conn.execute(lock, (_LOCK_CLASS, SCHEMA))
                conn.execute(_BOOTSTRAP)
                done = [version for (version,) in conn.execute("SELECT version FROM schema_migrations")]
                ahead = _ahead(done, [version for version, _, _ in pending])
                if ahead:
                    raise RunLogDatabaseError(ahead)
            for version, name, body in pending:
                with conn.transaction():
                    conn.execute(lock, (_LOCK_CLASS, SCHEMA))
                    row = conn.execute("SELECT sha256 FROM schema_migrations WHERE version = %s",
                                       (version,)).fetchone()
                    if row is not None:
                        if row[0] != _digest(body):
                            raise RuntimeError(f"migration {name} changed after it was applied; "
                                               "add a new migration instead")
                        continue
                    conn.execute(body)
                    conn.execute("INSERT INTO schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
                                 (version, name, _digest(body)))
                    applied.append(version)
            if read_role:
                with conn.transaction():
                    conn.execute(lock, (_LOCK_CLASS, SCHEMA))
                    _grant_reader(conn, read_role)
    except psycopg.Error as exc:
        raise RunLogDatabaseError(_failure(exc, dsn)) from None
    return applied


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Administer the PostgreSQL run-log catalog (docs/run-logs.md).")
    parser.add_argument("--dsn", help=f"libpq connection string (default ${DSN_ENV}, else {DEFAULT_DSN!r})")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser(
        "migrate", help=f"create schema {SCHEMA} if missing, apply pending migrations, grant the read role")
    migrate_parser.add_argument("--read-role", default=READ_ROLE,
                                help=f"group granted SELECT on {SCHEMA} after every migrate (default {READ_ROLE}); "
                                     "it must exist. '' skips the grant")
    args = parser.parse_args(argv)
    if not args.read_role:
        print(f"WARNING --read-role '': schema {SCHEMA} is not granted to any reporting group", file=sys.stderr)
    try:
        applied = migrate(args.dsn or os.environ.get(DSN_ENV) or DEFAULT_DSN, args.read_role)
    except (RunLogDatabaseError, RuntimeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"{SCHEMA}: " + (", ".join(f"applied {v:04d}" for v in applied) if applied else "nothing pending"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
