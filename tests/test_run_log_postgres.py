from __future__ import annotations

import concurrent.futures
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import run_log_postgres as postgres, run_log_routes, run_log_store as store, runs
from tests._postgres import postgres_dsn
from tests import test_run_log_routes as http_routes

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
except ImportError:
    psycopg = None

OWNER = "owner@example.invalid"
TEAMMATE = "teammate@example.invalid"
STRANGER = "stranger@example.invalid"
TABLES = ("runs", "run_access", "setup", "layers", "layer_readings", "temperatures", "audit")


class _Catalog:


    def use_postgres(self):
        self.dsn = postgres_dsn()
        self.schema = "t_runlog_" + uuid.uuid4().hex[:12]
        for change in (patch.object(postgres, "SCHEMA", self.schema),
                       patch.dict(os.environ, {postgres.DSN_ENV: self.dsn})):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(self.drop_schema, self.schema)

    def drop_schema(self, schema):
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def query(self, statement, params=(), schema=None):

        names = {table: sql.Identifier(schema or self.schema, table) for table in (*TABLES, "schema_migrations")}
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cursor = conn.execute(sql.SQL(statement).format(**names), params)
            return cursor.fetchall() if cursor.description else None

    def relations(self, schema=None):
        return self.query("SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                          "WHERE n.nspname = %s ORDER BY 1", (schema or self.schema,))

    def elsewhere(self):

        return self.query("SELECT n.nspname, c.relname, c.relacl::text FROM pg_class c "
                          "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname <> %s "
                          "AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%%' "
                          "ORDER BY 1, 2", (self.schema,))


class TestConfiguration(unittest.TestCase):


    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "saves"

    def assert_refused_without_sqlite(self, environment, pattern=""):
        with patch.dict(os.environ, environment), patch.object(store.sqlite3, "connect") as sqlite:
            with self.assertRaisesRegex(store.CatalogError, pattern) as error:
                store.refresh_catalog(self.root)
        sqlite.assert_not_called()
        self.assertFalse((self.root / store.CATALOG_NAME).exists())
        return error.exception

    def test_configured_failure_never_opens_sqlite(self):
        with patch.object(postgres, "connect", side_effect=postgres.RunLogDatabaseError("Unavailable")):
            self.assert_refused_without_sqlite({postgres.DSN_ENV: "dbname=waam_runs"}, "Unavailable")

    def test_empty_dsn_is_failure_not_sqlite(self):
        for value in ("", "  "):
            self.assert_refused_without_sqlite({postgres.DSN_ENV: value}, "empty")

    def test_missing_psycopg_is_failure_not_sqlite(self):
        with patch.dict(sys.modules, {"psycopg": None}):
            self.assert_refused_without_sqlite({postgres.DSN_ENV: "dbname=waam_runs"}, "psycopg")

    def test_workbench_imports_and_uses_sqlite_without_psycopg(self):
        script = ("import sys; sys.modules['psycopg'] = None; sys.path.insert(0, sys.argv[1]);"
                  "from pathlib import Path; from waam_pipeline import run_log_postgres, run_log_store, server;"
                  "print(run_log_store.refresh_catalog(Path(sys.argv[2]))['runs']);"
                  "print(run_log_postgres.main(['--dsn', 'dbname=waam_runs', 'migrate']))")
        environment = {key: value for key, value in os.environ.items() if key != postgres.DSN_ENV}
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "src"), str(self.root)],
                                capture_output=True, text=True, timeout=300, env=environment, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[-2:], ["0", "1"])
        self.assertIn("psycopg", result.stderr)

    def test_connection_failures_are_sanitized(self):
        if psycopg is None:
            self.fail("psycopg 3 is not installed (pip install -r requirements-dev.txt)")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        for dsn in (f"host=127.0.0.1 port={port} dbname=waam_runs user=nobody password=private-value",
                    "dbname=waam_runs password=private-value bogus"):
            with self.subTest(dsn=dsn):
                error = self.assert_refused_without_sqlite({postgres.DSN_ENV: dsn}, "PostgreSQL")
                self.assertNotIn("private-value", str(error))
                self.assertIsNone(error.__cause__)
                self.assertTrue(error.__suppress_context__)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(postgres.main(["--dsn", dsn, "migrate"]), 1)
                self.assertIn("ERROR PostgreSQL", stderr.getvalue())
                self.assertNotIn("private-value", stderr.getvalue())

    def test_failure_text_keeps_the_cause_and_never_a_password(self):
        if psycopg is None:
            self.fail("psycopg 3 is not installed (pip install -r requirements-dev.txt)")
        nul = "PostgreSQL text fields cannot contain NUL (0x00) bytes"
        self.assertIn(f"DataError: {nul}", postgres._failure(psycopg.DataError(nul)))
        for text in ("connection failed: password=private-value refused",
                     "bad conninfo: sslpassword = 'private value' at line\n2",
                     "could not reach postgresql://waam:private-value@db.invalid/waam_runs"):
            with self.subTest(text=text):
                message = postgres._failure(psycopg.OperationalError(text))
                self.assertIn("OperationalError: ", message)
                self.assertNotIn("private", message)
                self.assertNotIn("\n", message)
        echoed = postgres._failure(psycopg.OperationalError("server said private-value"),
                                   "host=db.invalid password=private-value")
        self.assertEqual(echoed, "PostgreSQL run-log operation failed (OperationalError: server said ***).")

    def test_statements_bind_values_and_never_format_them(self):
        raw = Mock()
        connection = postgres.Connection(raw, "rows")
        hostile = "Robert'; DROP TABLE runs;-- %s ?"
        connection.execute("SELECT run_id FROM runs WHERE name LIKE '%' || ? AND run_id = ?", [hostile, "r"])
        raw.cursor.assert_called_once_with(row_factory="rows")
        raw.cursor.return_value.execute.assert_called_once_with(
            "SELECT run_id FROM runs WHERE name LIKE '%%' || %s AND run_id = %s", (hostile, "r"))
        connection.executemany("DELETE FROM runs WHERE run_id = ?", [])
        raw.cursor.return_value.executemany.assert_not_called()

    def test_with_block_is_one_transaction(self):
        raw = Mock()
        connection = postgres.Connection(raw, None)
        with connection:
            pass
        raw.commit.assert_called_once()
        with self.assertRaises(ValueError), connection:
            raise ValueError("bad source")
        raw.rollback.assert_called_once()
        raw.commit.assert_called_once()


class TestMigrate(_Catalog, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        postgres_dsn()

    def setUp(self):
        self.use_postgres()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "saves"

    def test_migrate_is_versioned_idempotent_and_stays_in_its_schema(self):
        others = self.elsewhere()
        self.assertEqual(postgres.migrate(self.dsn), [1])
        self.assertEqual(postgres.migrate(self.dsn), [])
        digest = hashlib.sha256((postgres.SQL_DIR / "0001_initial.sql").read_bytes()
                                .replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(self.query("SELECT version, name, sha256 FROM {schema_migrations}"),
                         [(1, "0001_initial", digest)])
        self.assertEqual({name for name, kind in self.relations() if kind == "r"}, {*TABLES, "schema_migrations"})
        for table in TABLES:
            self.assertEqual(self.query(f"SELECT count(*) FROM {{{table}}}"), [(0,)])
        detail = store.create_run(self.root, "owner", OWNER, name="Self contained")
        store.update_run(self.root, OWNER, detail["record"]["run"], revision=detail["revision"],
                         action="temperature", data={"value": 20, "unit": "C", "stage": "other"})
        self.assertEqual(self.elsewhere(), others)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(postgres.main(["--dsn", self.dsn, "migrate"]), 0)
        self.assertEqual(stdout.getvalue().strip(), f"{self.schema}: nothing pending")

    def test_columns_match_the_sqlite_catalog_and_sort_bytewise(self):
        postgres.migrate(self.dsn)
        lite = sqlite3.connect(":memory:")
        self.addCleanup(lite.close)
        lite.executescript(store._SCHEMA)
        for table in TABLES:
            with self.subTest(table=table):
                expected = [row[1] for row in lite.execute(f"PRAGMA table_info({table})")]
                actual = [name for (name,) in self.query(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                    "AND table_name = %s ORDER BY ordinal_position", (self.schema, table))]
                self.assertEqual(actual, expected)
        collated = set(self.query("SELECT table_name, column_name FROM information_schema.columns "
                                  "WHERE table_schema = %s AND collation_name = 'C'", (self.schema,)))
        self.assertEqual(collated, {(table, "run_id") for table in TABLES} | {("runs", "started_at")})

    def test_runtime_never_creates_or_alters_the_schema(self):
        self.assertRaisesRegex(store.CatalogError, "migrate", store.refresh_catalog, self.root)
        self.assertEqual(self.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,)), [])
        postgres.migrate(self.dsn)

        def layout():
            return (self.relations(), self.query(
                "SELECT table_name, column_name, data_type, ordinal_position FROM information_schema.columns "
                "WHERE table_schema = %s ORDER BY 1, 4", (self.schema,)),
                self.query("SELECT version, sha256 FROM {schema_migrations}"))

        before = layout()
        detail = store.create_run(self.root, "owner", OWNER, name="Layout", collaborators=[TEAMMATE])
        store.update_run(self.root, OWNER, detail["record"]["run"], revision=detail["revision"],
                         action="layer-row", data={"layer": 0, "unit": "C", "readings": {"10": 50}})
        store.reporting_rows(self.root, TEAMMATE, "cooling")
        self.assertEqual(layout(), before)
        self.assertEqual(self.query("SELECT count(*) FROM {runs}"), [(1,)])

    def test_runtime_refuses_a_pending_or_changed_migration(self):
        postgres.migrate(self.dsn)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        (folder / "0001_initial.sql").write_bytes((postgres.SQL_DIR / "0001_initial.sql").read_bytes())
        (folder / "0002_extra.sql").write_text("CREATE TABLE extra (x integer);\n")
        with patch.object(postgres, "SQL_DIR", folder):
            self.assertRaisesRegex(store.CatalogError, "migrate", store.refresh_catalog, self.root)
            self.assertNotIn(("extra", "r"), self.relations())
            self.assertEqual(postgres.migrate(self.dsn), [2])
            self.assertEqual(store.refresh_catalog(self.root)["runs"], 0)
            self.query("UPDATE {schema_migrations} SET sha256 = repeat('0', 64) WHERE version = 2")
            self.assertRaisesRegex(store.CatalogError, "migrate", store.refresh_catalog, self.root)
            self.assertRaisesRegex(RuntimeError, "changed after it was applied", postgres.migrate, self.dsn)

    def test_migration_names_and_numbering_are_checked(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        (folder / "0001_a.sql").write_text("SELECT 1;")
        (folder / "0003_c.sql").write_text("SELECT 1;")
        self.assertRaisesRegex(RuntimeError, "no gaps", postgres.migrations, folder)
        (folder / "0003_c.sql").unlink()
        (folder / "2-bad.sql").write_text("SELECT 1;")
        self.assertRaisesRegex(RuntimeError, "NNNN", postgres.migrations, folder)
        with patch.object(postgres, "SQL_DIR", folder):
            self.assertRaisesRegex(RuntimeError, "NNNN", postgres.migrate, self.dsn)
        self.assertEqual(self.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,)), [])

    def test_a_failed_migration_rolls_back_whole(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        (folder / "0001_initial.sql").write_bytes((postgres.SQL_DIR / "0001_initial.sql").read_bytes())
        (folder / "0002_broken.sql").write_text("CREATE TABLE half_done (x integer);\nSELECT 1 / 0;\n")
        with patch.object(postgres, "SQL_DIR", folder):
            with self.assertRaisesRegex(postgres.RunLogDatabaseError, "22012"):
                postgres.migrate(self.dsn)
        self.assertEqual(self.query("SELECT version FROM {schema_migrations}"), [(1,)])
        self.assertNotIn(("half_done", "r"), self.relations())

    def test_migrate_refuses_a_schema_owned_by_another_role(self):

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION pg_database_owner")
                         .format(sql.Identifier(self.schema)))
        with self.assertRaisesRegex(postgres.RunLogDatabaseError, "another role"):
            postgres.migrate(self.dsn)
        self.assertEqual(self.relations(), [])
        self.assertRaisesRegex(store.CatalogError, "migrate", store.refresh_catalog, self.root)

    def test_migrate_refuses_a_database_newer_than_the_checkout(self):
        postgres.migrate(self.dsn)
        self.query("INSERT INTO {schema_migrations} (version, name, sha256) VALUES (2, '0002_future', repeat('0', 64))")
        with self.assertRaisesRegex(postgres.RunLogDatabaseError, "migration 0002 applied.*newer than this code"):
            postgres.migrate(self.dsn)
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(postgres.main(["--dsn", self.dsn, "migrate"]), 1)
        self.assertIn("ERROR PostgreSQL schema", stderr.getvalue())
        self.assertNotIn("nothing pending", stdout.getvalue())

        self.assertRaisesRegex(store.CatalogError, "newer than this code", store.refresh_catalog, self.root)
        self.assertEqual(self.query("SELECT version FROM {schema_migrations} ORDER BY 1"), [(1,), (2,)])

    def late_role(self):


        role = "waam_late_" + uuid.uuid4().hex[:8]
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            try:
                conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            except psycopg.errors.InsufficientPrivilege:
                return "waam_read"
        self.addCleanup(self.drop_role, role)
        return role

    def drop_role(self, role):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {0}; DROP ROLE {0}").format(sql.Identifier(role)))

    def assert_reads_only(self, role):
        tables = [name for (name,) in self.query(
            "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r'", (self.schema,))]
        self.assertEqual(len(tables), len(TABLES) + 1)
        for name in tables:
            self.assertEqual(self.query("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, name)),
                             [(True,)], name)
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                self.assertEqual(self.query("SELECT has_table_privilege(%s, %s, %s)", (role, name, privilege)),
                                 [(False,)], f"{name} {privilege}")
        self.assertEqual(self.query("SELECT has_schema_privilege(%s, %s, 'USAGE'), "
                                    "has_schema_privilege(%s, %s, 'CREATE')",
                                    (role, self.schema, role, self.schema)), [(True, False)])

    def test_a_missing_read_role_refuses_and_changes_nothing(self):
        absent = "waam_absent_" + uuid.uuid4().hex[:8]
        self.assertEqual(self.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (absent,)), [])
        with self.assertRaisesRegex(postgres.RunLogDatabaseError, f"{absent}.*does not exist"):
            postgres.migrate(self.dsn, read_role=absent)
        self.assertEqual(self.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,)), [])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(postgres.main(["--dsn", self.dsn, "migrate", "--read-role", absent]), 1)
        self.assertIn("does not exist", stderr.getvalue())
        self.assertEqual(self.query("SELECT 1 FROM pg_namespace WHERE nspname = %s", (self.schema,)), [])

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(postgres.main(["--dsn", self.dsn, "migrate", "--read-role", ""]), 0)
        self.assertEqual(stdout.getvalue().strip(), f"{self.schema}: applied 0001")
        self.assertIn("WARNING --read-role ''", stderr.getvalue())
        self.assertEqual(self.query("SELECT has_schema_privilege('waam_read', %s, 'USAGE')", (self.schema,)),
                         [(False,)])

    def test_a_read_role_created_after_the_first_migrate_is_granted_on_the_next(self):
        self.assertEqual(postgres.migrate(self.dsn, read_role=""), [1])
        role = self.late_role()
        self.assertEqual(self.query("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, self.schema)),
                         [(False,)])
        self.assertEqual(postgres.migrate(self.dsn, read_role=role), [])
        self.assert_reads_only(role)

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE TABLE {} (x integer)").format(sql.Identifier(self.schema, "later")))
        self.assertEqual(self.query("SELECT has_table_privilege(%s, %s, 'SELECT')",
                                    (role, f"{self.schema}.later")), [(True,)])

    def test_waam_read_gets_select_only(self):
        if not self.query("SELECT 1 FROM pg_roles WHERE rolname = 'waam_read'"):
            try:
                self.query("CREATE ROLE waam_read NOLOGIN")
            except psycopg.Error as exc:
                self.fail(f"waam_read is missing and cannot be created here ({exc}); create it as on the VM")
        postgres.migrate(self.dsn)
        self.assert_reads_only("waam_read")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            if conn.execute("SELECT pg_has_role(current_user, 'waam_read', 'MEMBER')").fetchone()[0]:
                conn.execute("SET ROLE waam_read")
                runs_table = sql.Identifier(self.schema, "runs")
                self.assertEqual(conn.execute(sql.SQL("SELECT count(*) FROM {}").format(runs_table)).fetchone(), (0,))
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql.SQL("DELETE FROM {}").format(runs_table))

    def test_a_dsn_search_path_cannot_redirect_the_catalog(self):
        decoy = self.schema + "_decoy"
        self.addCleanup(self.drop_schema, decoy)
        with patch.object(postgres, "SCHEMA", decoy):
            postgres.migrate(self.dsn)
        postgres.migrate(self.dsn)
        tricky = make_conninfo(self.dsn, options=f"-c search_path={decoy},public")
        with patch.dict(os.environ, {postgres.DSN_ENV: tricky}):
            store.create_run(self.root, "owner", OWNER, name="Pinned")
        self.assertEqual(self.query("SELECT count(*) FROM {runs}"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM {runs}", schema=decoy), [(0,)])

    def test_a_temporary_table_cannot_shadow_the_catalog(self):
        postgres.migrate(self.dsn)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("CREATE TEMP TABLE runs (run_id text)")
            conn.execute("CREATE TEMP TABLE schema_migrations (version integer, sha256 text)")
            postgres._pin_search_path(conn)
            found = "SELECT relnamespace::regnamespace::text FROM pg_class WHERE oid = to_regclass(%s)"
            for name in ("runs", "schema_migrations"):
                self.assertEqual(conn.execute(found, (name,)).fetchone(), (self.schema,), name)
            self.assertEqual(conn.execute("SELECT current_schema()").fetchone(), (self.schema,))
            schemas = conn.execute("SELECT current_schemas(true)").fetchone()[0]
        self.assertEqual(schemas[:2], ["pg_catalog", self.schema])
        self.assertTrue(schemas[-1].startswith("pg_temp"))
        self.assertNotIn("public", schemas)


class TestStoreOnPostgres(_Catalog, unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        postgres_dsn()

    def setUp(self):
        self.use_postgres()
        postgres.migrate(self.dsn)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "saves"

    def create(self, workspace="owner", actor=OWNER, **kwargs):
        options = {"name": "Test plate", "setup": {"specimen_id": "A-12", "travel_mm_min": 300}}
        options.update(kwargs)
        return store.create_run(self.root, workspace, actor, **options)

    def update(self, detail, action, data, actor=OWNER, **kwargs):
        return store.update_run(self.root, actor, detail["record"]["run"], revision=detail["revision"],
                                action=action, data=data, **kwargs)

    def metadata(self, detail):
        record = detail["record"]
        return self.root / record["workspace"] / "_runs" / record["run"] / f"{record['run']}_run.json"

    def flaky_connect(self, failing_call):
        real, calls = postgres.connect, []

        def connect(dsn):
            calls.append(dsn)
            if len(calls) == failing_call:
                raise postgres.RunLogDatabaseError("PostgreSQL run-log operation failed (OperationalError).")
            return real(dsn)
        return patch.object(postgres, "connect", connect)

    def test_crud_units_sharing_and_revocation(self):
        detail = self.create(collaborators=[TEAMMATE])
        run_id = detail["record"]["run"]
        detail = self.update(detail, "layer-row", {"layer": 0, "pass": 1, "unit": "F", "pre_weld": 212,
                                                   "readings": {"20": 392}, "current_a": 130, "voltage_v": 21},
                             actor=TEAMMATE)
        detail = self.update(detail, "temperature", {"value": 392, "unit": "F", "stage": "other"}, actor=TEAMMATE)
        self.assertEqual(len(store.list_runs(self.root, TEAMMATE)), 1)
        for dataset in ("runs", "layers", "cooling", "temperatures"):
            self.assertEqual(len(store.reporting_rows(self.root, TEAMMATE, dataset)), 1)
            self.assertEqual(store.reporting_rows(self.root, STRANGER, dataset), [])
        self.assertEqual(self.query("SELECT pre_weld_value_c, current_a, voltage_v FROM {layers}"), [(100, 130, 21)])
        self.assertEqual(self.query("SELECT value_c FROM {layer_readings}"), [(200,)])
        self.assertEqual(self.query("SELECT value_c FROM {temperatures}"), [(200,)])
        self.assertEqual(self.query("SELECT actor, action FROM {audit} ORDER BY event"),
                         [(OWNER, "create"), (TEAMMATE, "layer-row"), (TEAMMATE, "temperature")])
        detail = self.update(detail, "setup", {"collaborators": []})
        self.assertEqual(store.reporting_rows(self.root, TEAMMATE, "runs"), [])
        with self.assertRaises(PermissionError):
            store.get_run(self.root, TEAMMATE, run_id)
        detail = self.update(detail, "finish", {"outcome": "not-run"})
        self.assertEqual(store.get_run(self.root, OWNER, run_id), detail)
        self.assertEqual(self.query("SELECT status, revision FROM {runs}"), [("finished", detail["revision"])])
        self.assertFalse((self.root / store.CATALOG_NAME).exists())

    def test_failed_refresh_never_serves_a_revoked_collaborator(self):
        detail = self.create(collaborators=[TEAMMATE])
        with patch.object(store, "_refresh_after_write", side_effect=store.CatalogError("test outage")):
            with self.assertRaises(store.CatalogError):
                self.update(detail, "setup", {"collaborators": []})

        self.assertEqual(self.query("SELECT email FROM {run_access}"), [(TEAMMATE,)])
        for dataset in ("runs", "layers", "cooling", "temperatures"):
            self.assertEqual(store.reporting_rows(self.root, TEAMMATE, dataset), [])
        self.assertEqual(self.query("SELECT email FROM {run_access}"), [])

    def test_outage_after_the_archive_write_is_save_not_confirmed(self):
        detail = self.create()
        with self.flaky_connect(2), self.assertRaisesRegex(store.CatalogError, "Run saved.*Reload"):
            self.update(detail, "note", {"text": "Safely in archive"})
        current = store.get_run(self.root, OWNER, detail["record"]["run"])
        self.assertEqual(current["record"]["notes"][-1]["text"], "Safely in archive")
        with self.assertRaises(store.ConflictError):
            self.update(detail, "note", {"text": "Duplicate retry"})
        before = self.metadata(detail).read_bytes()
        with self.flaky_connect(1), self.assertRaises(store.CatalogError):
            self.update(current, "note", {"text": "Database down before the write"})
        self.assertEqual(self.metadata(detail).read_bytes(), before)

    def test_stale_revision_and_simultaneous_requests_never_overwrite(self):
        original = self.create(collaborators=[TEAMMATE])

        def write(actor):
            try:
                return self.update(original, "note", {"text": actor}, actor=actor)
            except store.ConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [OWNER, TEAMMATE]))
        self.assertEqual(results.count("conflict"), 1)
        current = store.get_run(self.root, OWNER, original["record"]["run"])
        self.assertEqual(len(current["record"]["notes"]), 1)
        with self.assertRaises(store.ConflictError):
            self.update(original, "setup", {"setup": {"material": "stale"}})
        self.assertEqual(self.query("SELECT revision FROM {runs}"), [(current["revision"],)])

    def test_workbench_changes_are_discovered(self):
        detail = self.create()
        runs.add_note(self.root, "owner", text="Existing workbench record")
        with self.assertRaises(store.ConflictError):
            self.update(detail, "note", {"text": "Stale browser"})
        latest = store.get_run(self.root, OWNER, detail["record"]["run"])
        self.assertEqual(latest["record"]["notes"][0]["text"], "Existing workbench record")
        self.assertEqual(self.query("SELECT revision FROM {runs}"), [(latest["revision"],)])

    def test_sealed_archive_stays_byte_identical_and_the_catalog_rebuilds(self):
        detail = self.create()
        detail = self.update(detail, "layer-row", {"layer": 0, "unit": "C", "current_a": 123, "readings": {"20": 100}})
        detail = self.update(detail, "finish", {"outcome": "completed", "arcmon_waiver": True,
                                                "waiver_reason": "Logger not available", "height_mm": 2})
        folder = self.metadata(detail).parent
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        self.query("TRUNCATE {runs} CASCADE")
        restored = store.get_run(self.root, OWNER, detail["record"]["run"])
        self.assertEqual(restored, detail)
        self.assertEqual(self.query("SELECT count(*) FROM {layer_readings}"), [(1,)])
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})
        with self.assertRaisesRegex(ValueError, "finished"):
            self.update(restored, "note", {"text": "Too late"})
        self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})

    def test_an_invalid_source_rolls_back_the_whole_refresh(self):
        detail = self.create()
        second = self.create(workspace="z-other", actor=STRANGER, name="Other")
        before = self.query("SELECT run_id, revision FROM {runs} ORDER BY run_id")
        runs.add_note(self.root, "owner", text="First source changed")
        self.metadata(second).write_bytes(b"broken JSON")
        with self.assertRaises(ValueError):
            store.refresh_catalog(self.root)
        self.assertEqual(self.query("SELECT run_id, revision FROM {runs} ORDER BY run_id"), before)
        self.assertIn("First source changed", self.metadata(detail).read_text())

    def test_values_are_bound_never_spliced(self):
        detail = self.create()
        text = "O'Brien 100% ? %s $1 -- ;"
        self.update(detail, "layer-row", {"layer": 0, "unit": "C", "note": text})
        self.assertEqual(self.query("SELECT note FROM {layers}"), [(text,)])
        self.assertEqual(store.list_runs(self.root, OWNER, search="' OR 1=1 --"), [])
        for actor in ("'; DROP TABLE runs;--", "%s@example.invalid", "?@example.invalid"):
            self.assertEqual(store.reporting_rows(self.root, actor, "layers"), [])
        self.assertEqual(self.query("SELECT count(*) FROM {runs}"), [(1,)])

    def test_nul_text_is_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(ValueError, "NUL"):
            self.create(name="bad\x00name")
        self.assertIsNone(runs.active(self.root, "owner"))
        detail = self.create()
        before = self.metadata(detail).read_bytes()
        for action, data in (("note", {"text": "a\x00b"}),
                             ("layer-row", {"layer": 0, "unit": "C", "note": "a\x00b"}),
                             ("temperature", {"value": 20, "unit": "C", "stage": "other", "note": "\x00"})):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "NUL"):
                self.update(detail, action, data)
            self.assertEqual(self.metadata(detail).read_bytes(), before)
        self.assertEqual(len(store.list_runs(self.root, OWNER)), 1)

    def test_an_archived_nul_reaches_the_catalog_as_u_fffd_and_the_archive_is_kept(self):
        detail = self.create(collaborators=[TEAMMATE])
        detail = self.update(detail, "layer-row", {"layer": 0, "unit": "C", "note": "ok", "readings": {"5": 90}})
        detail = self.update(detail, "temperature", {"value": 20, "unit": "C", "stage": "other", "note": "ok"})
        other = self.create(workspace="z-other", actor=STRANGER, name="Other")
        run_id, path = detail["record"]["run"], self.metadata(detail)

        record = json.loads(path.read_text(encoding="utf-8"))
        record["name"] = "bad\x00name"
        record["layer_log"][0]["note"] = record["temperatures"][0]["note"] = "bad\x00note"
        path.write_bytes(runs._json(record))
        archived = path.read_bytes()
        self.assertEqual([run["run"] for run in store.list_runs(self.root, STRANGER)], [other["record"]["run"]])
        for dataset in ("runs", "layers", "cooling", "temperatures"):
            self.assertEqual(len(store.reporting_rows(self.root, STRANGER, dataset)), 1 if dataset == "runs" else 0)
        self.assertEqual(self.query("SELECT name FROM {runs} WHERE run_id = %s", (run_id,)), [("bad�name",)])
        self.assertEqual(self.query("SELECT note FROM {layers}"), [("bad�note",)])
        self.assertEqual(self.query("SELECT note FROM {temperatures}"), [("bad�note",)])

        self.assertEqual(self.query("SELECT source_json::jsonb ->> 'name' FROM {runs} WHERE run_id = %s",
                                    (run_id,)), [("bad�name",)])
        self.assertEqual(self.query("SELECT source_json::jsonb ->> 'note' FROM {layers}"), [("bad�note",)])
        self.assertEqual(path.read_bytes(), archived)
        self.assertEqual(store.get_run(self.root, OWNER, run_id)["record"]["name"], "bad\x00name")

        def feeds():
            return {dataset: run_log_routes.reporting_csv(self.root, TEAMMATE, dataset)
                    for dataset in ("runs", "layers", "cooling", "temperatures")}

        on_postgres = feeds()
        with patch.dict(os.environ):
            del os.environ[postgres.DSN_ENV]
            on_sqlite = feeds()
        self.assertEqual(on_postgres, on_sqlite)
        self.assertIn("bad�note", on_postgres["temperatures"].decode("utf-8-sig"))
        self.assertEqual(path.read_bytes(), archived)

    def test_listings_details_and_feeds_match_the_sqlite_catalog(self):
        first = self.create(collaborators=[TEAMMATE])
        first = self.update(first, "layer-row", {"layer": 0, "pass": 1, "unit": "F", "pre_weld": 212,
                                                 "readings": {"20": 392, "0": 400}, "current_a": 130,
                                                 "voltage_v": 21})

        first = self.update(first, "layer-row", {"layer": 0, "unit": "C", "height_mm": 2.5, "note": "=whole"})
        first = self.update(first, "layer-row", {"layer": 1, "pass": 2, "unit": "C", "readings": {"30.5": 150.25}})
        first = self.update(first, "temperature", {"value": 300, "unit": "F", "stage": "other", "cycle": 2})
        self.create(workspace="z-other", actor=STRANGER, name="Second")
        self.create(workspace="a-other", actor=OWNER, name="Third", setup={})

        def snapshot():
            seen = {"detail": store.get_run(self.root, OWNER, first["record"]["run"])}
            for actor in (OWNER, TEAMMATE, STRANGER):
                for admin in (False, True):
                    seen[actor, admin] = store.list_runs(self.root, actor, admin=admin)
                for dataset in ("runs", "layers", "cooling", "temperatures"):
                    seen[actor, dataset] = run_log_routes.reporting_csv(self.root, actor, dataset)
            return seen

        on_postgres = snapshot()
        with patch.dict(os.environ):
            del os.environ[postgres.DSN_ENV]
            on_sqlite = snapshot()
        self.assertTrue((self.root / store.CATALOG_NAME).exists())
        self.assertEqual(on_postgres, on_sqlite)
        layers = csv.DictReader(io.StringIO(on_postgres[OWNER, "layers"].decode("utf-8-sig")))
        self.assertEqual([(row["layer"], row["pass"]) for row in layers if row["run"] == first["record"]["run"]],
                         [("0", ""), ("0", "1"), ("1", "2")])
        self.assertEqual(len(on_postgres[STRANGER, True]), 3)

    def test_new_process_reads_persisted_records(self):
        detail = self.create()
        script = ("import json, sys; from pathlib import Path; "
                  "from waam_pipeline import run_log_postgres as p, run_log_store as s; p.SCHEMA = sys.argv[4]; "
                  "print(json.dumps(s.get_run(Path(sys.argv[1]), sys.argv[2], sys.argv[3])))")
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run([sys.executable, "-c", script, str(self.root), OWNER, detail["record"]["run"],
                                 self.schema], capture_output=True, text=True, timeout=60, env=environment,
                                check=True)
        self.assertEqual(json.loads(result.stdout), detail)
        self.assertFalse((self.root / store.CATALOG_NAME).exists())


class TestRunLogRoutesOnPostgres(_Catalog, http_routes.TestRunLogRoutes):


    def setUp(self):
        super().setUp()
        self.use_postgres()
        postgres.migrate(self.dsn)

    def test_catalog_outage_after_an_archive_write_is_save_not_confirmed(self):
        detail = self.start()
        real, calls = postgres.connect, []

        def connect(dsn):
            calls.append(dsn)
            if len(calls) == 2:
                raise postgres.RunLogDatabaseError("PostgreSQL run-log operation failed (OperationalError).")
            return real(dsn)

        with patch.object(postgres, "connect", connect), contextlib.redirect_stderr(io.StringIO()):
            code, body, _ = self.update(detail, "note", {"text": "In the archive"})
        self.assertEqual(code, 500)
        self.assertTrue(body["save_uncertain"])
        self.assertEqual(self.update(detail, "note", {"text": "Retry"})[0], 409)
        self.assertEqual(self.query("SELECT count(*) FROM {runs}"), [(1,)])

    def test_an_outage_answers_reads_503_and_keeps_writes_save_uncertain(self):
        detail = self.start()
        run_id = detail["record"]["run"]
        down = postgres.RunLogDatabaseError("PostgreSQL run-log operation failed (ConnectionTimeout).")
        with patch.object(postgres, "connect", side_effect=down), contextlib.redirect_stderr(io.StringIO()):
            for route in ("/api/run-logs", "/api/run-logs/detail", "/api/run-logs/export"):
                with self.subTest(route=route):
                    code, body, _ = self.request(route, query={"run": run_id})
                    self.assertEqual(code, 503)
                    self.assertNotIn("save_uncertain", body)
                    self.assertIn("Nothing was changed", body["error"])

            query = {"run": run_id, "revision": detail["revision"], "kind": "attachment", "name": "a.txt"}
            for code, body, _ in (self.request("/api/run-logs/start", obj={"name": "DOWN"}),
                                  self.update(detail, "note", {"text": "down"}),
                                  self.request("/api/run-logs/add", query=query, raw=b"")):
                self.assertEqual(code, 500)
                self.assertTrue(body["save_uncertain"])

    def test_a_catalog_failure_is_logged_with_its_cause_and_never_a_password(self):
        self.start()
        missing = make_conninfo(self.dsn, dbname="waam_no_such_db_" + uuid.uuid4().hex[:6], password="private-value")
        echo = psycopg.OperationalError("refused postgresql://waam:private-value@db.invalid password=private-value")
        cases = ((missing, None, "does not exist"), (self.dsn, echo, "refused postgresql://waam:***@db.invalid"))
        for dsn, failure, cause in cases:
            with self.subTest(cause=cause), contextlib.ExitStack() as stack:
                stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                stack.enter_context(patch.dict(os.environ, {postgres.DSN_ENV: dsn}))
                if failure:
                    stack.enter_context(patch.object(psycopg, "connect", side_effect=failure))
                code, body, _ = self.request("/api/run-logs")
                with self.assertRaises(store.CatalogError):
                    run_log_routes.reporting_csv(http_routes.server.SAVES_DIR, OWNER, "runs")
                stack.close()
                self.assertEqual(code, 503)
                self.assertNotIn(cause, json.dumps(body))
                lines = stderr.getvalue().splitlines()
                self.assertTrue(any(line.startswith("[waam] run-log list failed: CatalogError: PostgreSQL")
                                    and cause in line for line in lines), lines)
                self.assertTrue(any(line.startswith("[waam] run-log excel runs failed: ") and cause in line
                                    for line in lines), lines)
                self.assertNotIn("private-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
