from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import arcmon, datastore, passes as pass_tables, runs
from tests._postgres import postgres_dsn

try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
except ImportError:
    psycopg = None

NOW = dt.datetime(2026, 10, 7, 17, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
WS = "Test_Operator"
ARC_CSV = ROOT / "docs/cell-data/arcmon/1-20251103-181340.CSV"
VAR_DAT = ROOT / "docs/cell-data/var-exports/VAR_AR1440_rin3.DAT"
PROTECTED = (".waam-accounts.json", ".waam-sessions.sqlite3", ".waam-sessions.sqlite3-wal",
             ".waam-tokens.json", ".waam-token", ".waam-auth.json")


class PostgresCase(unittest.TestCase):


    @classmethod
    def setUpClass(cls):
        cls.dsn = postgres_dsn()

    def setUp(self):
        self.schema = "t_" + uuid.uuid4().hex[:16]
        patcher = patch.object(datastore, "SCHEMA", self.schema)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._drop_schema)
        self.schema_dsn = self.dsn
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        datastore.pin(self.conn)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.saves = self.tmp / "saves"

    def _drop_schema(self):
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def one(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def rows(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def count(self, table):
        return self.one(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))[0]

    def counts(self):
        return {table: self.count(table) for table in datastore.TABLES if table != "ingest_runs"}

    def ingest(self, **kwargs):
        return datastore.ingest(self.conn, self.saves, **kwargs)


JBI = b"/JOB\r\n//NAME WALLTEST\r\n//POS\r\n///NPOS 1,0,0,0,0,0\r\nNOP\r\nEND\r\n"
REGISTRATION = {"version": 2, "frame": "lc50s_tool0_pulse", "weld_ctwd_mm": 9.525, "gap_direction": "torch",
                "tool0_tip_offset_mm": 8.92, "corners": [{"pulses": [1, 2, 3, 4, 5, 6], "tip_distance_mm": 8.92}] * 4}
PASSES = [dict(zip(pass_tables.FIELDS, row)) for row in (
    (1, 0, 1, "", 81, 81, 3.3, 3.3, 198.0, 177.8, 1, 22, 22, 2.0, 0, 53.88, 3.636),
    (2, 1, 1, "wall", 81, 81, 3.3, 3.3, 198.0, 177.8, 1, 30, 31, 30.0, 1, 53.88, None))]
SIDECAR = {"job": "WALLTEST", "commit": "abc1234",
           "params": {"lh": "1.2", "bead": "6.7", "travel": "457.2", "sub": "12.7", "thermal": "off",
                      "cooling": "adaptive", "substrate_registration": json.dumps(REGISTRATION),
                      "token": "SECRET-SIDECAR"},
           "query": {"arc": "81", "clean_first": "1", "interpass": "30", "interpass_every": "1",
                     "measure_every": "2", "measure_times": "20, 40", "ctwd": "19.05",
                     "platter": {"clear": True, "training_block_on": False, "fixture": None,
                                 "attested_by": "operator@example.invalid"}},
           "summary": {"weld_ctwd_mm": 19.05, "asf": [81], "layers": [0, 1], "measure_stops": [{"layer": 1}]}}


def touch_var() -> bytes:

    lines = VAR_DAT.read_text(encoding="latin-1").splitlines()
    p = lines.index("///P") + 1
    for i, pulses in enumerate(([-1, 2, -3, 4, -5, 6], [0, 0, 0, 0, 0, 0], [7, 8, 9, 10, 11, 12])):
        lines[p + 120 + i] = '"PULSE"0,0,0,000000000000000000000000,0,0,' + ",".join(map(str, pulses)) + ",0,0"
    b = lines.index("///B") + 1
    lines[b + 8] = "1,0,1,0,0,0,0,0,0,0"
    return ("\r\n".join(lines) + "\r\n").encode("latin-1")


def multi_segment_csv() -> bytes:


    head = ARC_CSV.read_text().split("//DATA")[0] + "//DATA\nCH1,CH2,CH3,CH4,CH5,CH6,CH7,CH8,CH9,CH10,CH11\n"
    rows, length = [], 10.0
    gap = max(1, round(arcmon.MERGE_GAP_S / 0.1))

    def row(volts, amps, step, spd, cond):
        rows.append(f"{volts},{amps},23.6,14.0,90.0,0.9,-0.9,{step},{spd},{cond},{length:.2f}")

    for _ in range(3):
        row(0.0, 0.0, 0, 0.0, 1083)
    for _ in range(2):
        row(12.0, 150.0, 0, 0.0, 83)
    for i in range(20):
        length += 0.76
        row(15.0 + (i % 2) * 0.2, 5.0 if 8 <= i < 8 + gap else 200.0 + i % 3, 4, 45.5, 83)
    for cond in (1084, 1084, 1083, 1083):
        row(13.0, 120.0, 0, 0.0, cond)
    for _ in range(gap + 1):
        row(0.0, 0.0, 0, 0.0, 1083)
    length = 5.0
    for i in range(15):
        length += 0.76
        row(16.0, 210.0 + i % 4, 7, 45.5, 86 if i in (3, 4) else 85)
    for _ in range(3):
        row(13.5, 110.0, 0, 0.0, 1085)
    for _ in range(4):
        row(0.0, 0.0, 0, 0.0, 1085)
    return (head + "\n".join(rows) + "\n0.0,0.0\n\n//ARC_MONDATAEND\n").encode()


def arc_csv(rows, *, sampling="0.1", len_unit="mm") -> bytes:

    head = ARC_CSV.read_text().split("//DATA")[0]
    head = head.replace("//SAMPLING,0.1sec", f"//SAMPLING,{sampling}sec")
    head = head.replace("//CH11,LEN,WELD LENGTH,0,0,1,mm", f"//CH11,LEN,WELD LENGTH,0,0,1,{len_unit}")
    body = "\n".join(",".join(str(v) for v in row) for row in rows)
    return (head + "//DATA\nCH1,CH2,CH3,CH4,CH5,CH6,CH7,CH8,CH9,CH10,CH11\n" + body + "\n//ARC_MONDATAEND\n").encode()


def welding(amps=200.0, step=4, length=1.0, spd=45.5, cond=83, volts=15.0):
    return [volts, amps, 23.6, 14.0, 90.0, 0.9, -0.9, step, spd, cond, length]


class SyntheticRun:
    def __init__(self, saves: Path, ws: str = WS, name: str = "WALLTEST"):
        self.saves, self.ws = saves, ws
        self.record = runs.start(saves, ws, operator="operator@example.invalid", name=name, now=NOW,
                                 commit="abc1234")
        self.run = self.record["run"]
        self.folder = saves / ws / "_runs" / self.run
        self.var = self.add(kind="PLATE-CORNERS-VAR", body=VAR_DAT.read_bytes(), ext="DAT", detail="VAR",
                            source="robot")
        self.weld = self.add(kind="WELD-JOB", body=JBI, ext="JBI", detail="WALLTEST", source="workbench",
                             sidecar=SIDECAR, passes=PASSES)
        self.touch = self.add(kind="LAYER-TOUCH-VAR", body=touch_var(), ext="DAT", detail="VAR", source="robot")
        body = ARC_CSV.read_bytes()
        self.arc = self.add(kind="ARCMON", body=body, ext="CSV", detail=ARC_CSV.stem, source="robot",
                            arcmon_summary=arcmon.parse(ARC_CSV.name, body))
        self.trace = self.add(kind="ATTACH", body=multi_segment_csv(), ext="CSV", detail="trace", source="operator")
        dat = b"//ARCMON 1\r\n1,2,3\r\n4,5,6,7,8,9\r\n"
        self.dat = self.add(kind="ARCMON", body=dat, ext="DAT", detail="ARCMON", source="robot",
                            arcmon_summary=arcmon.parse("ARCMON.DAT", dat))
        self.photo = self.add(kind="ATTACH", body=b"\xff\xd8 not really a jpeg", ext="jpg", detail="photo",
                              source="operator")
        runs.add_temperature(saves, ws, value=480.0, unit="F", stage="after-interpass", cycle=1, now=NOW)
        runs.add_temperature(saves, ws, value=120.0, unit="C", stage="before-weld", note="pyrometer", now=NOW)
        runs.set_layer_row(saves, ws, {"layer": 0, "pass": 1, "pass_base": 1, "unit": "F", "pre_weld": 300,
                                       "readings": {"20": 600, "40.5": 500}, "height_mm": 3.5, "width_mm": 7.0,
                                       "height_source": "manual", "note": "first"}, now=NOW)
        runs.set_cycle(saves, ws, {"cycle": 1, "layers": [0, 0], "status": "applied", "job": "WALLTEST",
                                   "file": self.weld, "touches": 20, "interpass_s": 30.0,
                                   "summary": {"growth_mm": 3.4, "error_range_mm": [-1.0, 1.25],
                                               "mean_error_mm": 0.1},
                                   "build_settings": {"wire_feed_m_min": 10.6}})
        runs.add_note(saves, ws, text="first wall", now=NOW)

        path = self.folder / f"{self.run}_run.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["session"] = "SECRET-RUNJSON"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def add(self, **kwargs):
        return runs.add_file(self.saves, self.ws, now=NOW, operator="operator@example.invalid", **kwargs)

    def finish(self):
        return runs.finish(self.saves, self.ws, outcome="completed", measurements={"height_mm": 3.5, "width_mm": 7},
                           by="operator@example.invalid", now=NOW)


class TestWithoutPsycopg(unittest.TestCase):
    def test_workbench_and_datastore_import_without_psycopg(self):
        script = ("import sys; sys.modules['psycopg'] = None; sys.path.insert(0, sys.argv[1]);"
                  "from waam_pipeline import datastore, server; print(datastore.main(['status']))")
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "src")], capture_output=True, text=True,
                                timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "1")
        self.assertIn("psycopg", result.stderr)


class TestMigrations(PostgresCase):
    def test_apply_once_and_idempotent(self):
        self.assertEqual(datastore.migrate(self.conn), [1])
        self.assertEqual(datastore.migrate(self.conn), [])
        self.assertEqual(self.rows("SELECT version, name FROM schema_migrations"), [(1, "0001_initial")])
        for table in datastore.TABLES:
            self.assertEqual(self.count(table), 0)
        self.assertIsNotNone(self.one("SELECT to_regclass('arc_segments')")[0])

    def test_changed_applied_migration_refuses(self):
        datastore.migrate(self.conn)
        self.conn.execute("UPDATE schema_migrations SET sha256 = repeat('0', 64)")
        with self.assertRaisesRegex(RuntimeError, "changed after it was applied"):
            datastore.migrate(self.conn)

    def test_numbering_and_names_are_checked(self):
        folder = self.tmp / "sql"
        folder.mkdir()
        (folder / "0001_a.sql").write_text("SELECT 1;")
        (folder / "0003_c.sql").write_text("SELECT 1;")
        with self.assertRaisesRegex(RuntimeError, "no gaps"):
            datastore.migrations(folder)
        (folder / "0003_c.sql").unlink()
        (folder / "2-bad.sql").write_text("SELECT 1;")
        with self.assertRaisesRegex(RuntimeError, "NNNN"):
            datastore.migrations(folder)

    def test_failed_migration_rolls_back_whole(self):
        folder = self.tmp / "sql"
        folder.mkdir()
        (folder / "0001_initial.sql").write_bytes((datastore.SQL_DIR / "0001_initial.sql").read_bytes())
        (folder / "0002_broken.sql").write_text("CREATE TABLE half_done (x integer);\nSELECT 1 / 0;\n")
        with self.assertRaises(psycopg.errors.DivisionByZero):
            datastore.migrate(self.conn, folder)
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,)])
        self.assertIsNone(self.one("SELECT to_regclass('half_done')")[0])

    def test_a_missing_read_role_refuses_and_only_an_explicit_empty_one_skips(self):
        absent = "waam_absent_" + uuid.uuid4().hex[:8]
        self.assertIsNone(self.one("SELECT 1 FROM pg_roles WHERE rolname = %s", (absent,)))
        SyntheticRun(self.saves)
        with self.assertRaisesRegex(RuntimeError, f"read role {absent} does not exist"):
            datastore.migrate(self.conn, read_role=absent)
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            self.ingest(read_role=absent)
        self.assertIsNone(self.one("SELECT to_regclass('runs')")[0])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = datastore.main(["--dsn", self.schema_dsn, "--read-role", absent, "ingest", "--saves",
                                   str(self.saves)])
        self.assertEqual(code, 1)
        self.assertIn(f"ERROR read role {absent} does not exist", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = datastore.main(["--dsn", self.schema_dsn, "--read-role", "", "ingest", "--saves", str(self.saves)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("WARNING -: --read-role '': no role was granted the index", out.getvalue())
        self.assertFalse(self.one("SELECT has_schema_privilege('waam_read', %s, 'USAGE')", (self.schema,))[0])

    def test_read_grants_are_restored_on_every_ingest(self):
        SyntheticRun(self.saves)
        self.ingest()
        self.assertTrue(self.one("SELECT has_table_privilege('waam_read', 'runs', 'SELECT')")[0])
        names = {"schema": sql.Identifier(self.schema)}
        for statement in ("REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM waam_read",
                          "REVOKE USAGE ON SCHEMA {schema} FROM waam_read",
                          "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE SELECT ON TABLES FROM waam_read"):
            self.conn.execute(sql.SQL(statement).format(**names))
        self.assertFalse(self.one("SELECT has_table_privilege('waam_read', 'runs', 'SELECT')")[0])
        report = self.ingest()
        self.assertEqual((report.unchanged, report.problems), (1, []))
        self.assertTrue(self.one("SELECT has_table_privilege('waam_read', 'arc_segments', 'SELECT')")[0])
        self.assertTrue(self.one("SELECT has_schema_privilege('waam_read', %s, 'USAGE')", (self.schema,))[0])
        self.conn.execute("CREATE TABLE later (x integer)")
        self.assertTrue(self.one("SELECT has_table_privilege('waam_read', 'later', 'SELECT')")[0])

    def test_a_role_granted_after_the_index_exists_is_granted_on_the_next_migrate(self):
        SyntheticRun(self.saves)
        self.assertEqual(self.ingest(read_role="").new, 1)
        self.assertFalse(self.one("SELECT has_table_privilege('waam_read', 'runs', 'SELECT')")[0])
        self.assertEqual(datastore.migrate(self.conn, read_role="waam_read"), [])
        self.assertTrue(self.one("SELECT has_table_privilege('waam_read', 'runs', 'SELECT')")[0])

    def test_a_read_role_created_after_the_index_is_granted_on_the_next_migrate(self):
        if not self.one("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")[0]:
            self.skipTest("this role cannot create roles (the VM's waam); the test above covers the retry")
        role = "waam_late_" + uuid.uuid4().hex[:8]
        SyntheticRun(self.saves)
        self.ingest(read_role="")
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            datastore.migrate(self.conn, read_role=role)
        self.conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        self.addCleanup(self._drop_role, role)
        datastore.migrate(self.conn, read_role=role)
        self.assertTrue(self.one("SELECT has_table_privilege(%s, 'runs', 'SELECT')", (role,))[0])

    def _drop_role(self, role):
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))

    def test_the_index_stays_in_its_schema_whatever_the_search_path(self):
        other = "t_other_" + uuid.uuid4().hex[:12]
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(other)))
        self.addCleanup(self._drop, other)
        SyntheticRun(self.saves)

        self.conn.execute(sql.SQL("SET search_path = {}").format(sql.Identifier(other)))
        self.assertEqual(self.ingest().new, 1)
        self.conn.execute("SET search_path = public")
        self.assertEqual(self.ingest().unchanged, 1)
        homes = self.rows("SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                          "WHERE c.relname = 'runs' AND n.nspname IN (%s, %s, 'public')", (self.schema, other))
        self.assertEqual(homes, [(self.schema,)])

        self.assertEqual(self.one("SELECT count(*) FROM pg_default_acl d, aclexplode(d.defaclacl) a "
                                  "WHERE a.grantee = 'waam_read'::regrole AND (d.defaclnamespace = 0 "
                                  "OR d.defaclnamespace = 'public'::regnamespace)")[0], 0)
        with contextlib.closing(datastore.connect(self.dsn)) as conn:
            self.assertEqual(conn.execute("SHOW search_path").fetchone()[0], f"{self.schema}, pg_temp")

    def _drop(self, schema):
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def test_migrate_refuses_a_schema_it_does_not_own(self):
        owner = self.one("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'")[0]
        self.assertNotEqual(owner, self.one("SELECT current_user")[0])
        with patch.object(datastore, "SCHEMA", "public"):
            with self.assertRaisesRegex(RuntimeError, "owned by"):
                datastore.migrate(self.conn)
        self.assertIsNone(self.one("SELECT to_regclass('public.schema_migrations')")[0])

    def test_rebuild_is_drop_schema_then_ingest(self):
        SyntheticRun(self.saves)
        self.ingest()
        before = self.counts()
        self.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0), report.problems)
        self.assertEqual(self.counts(), before)
        self.assertTrue(self.one("SELECT has_table_privilege('waam_read', 'runs', 'SELECT')")[0])

    def test_waam_read_gets_select_only(self):
        if self.one("SELECT 1 FROM pg_roles WHERE rolname = 'waam_read'") is None:
            try:
                self.conn.execute("CREATE ROLE waam_read NOLOGIN")
            except psycopg.Error as exc:
                self.fail(f"waam_read is missing and cannot be created here ({exc}); create it as on the VM")
        datastore.migrate(self.conn)
        relations = self.rows("SELECT c.oid::regclass::text, c.relkind FROM pg_class c JOIN pg_namespace n "
                              "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'S')",
                              (self.schema,))
        self.assertLessEqual({("arc_segments", "v"), ("arcmon_segments", "r")},
                             {(name.split(".")[-1], kind) for name, kind in relations})
        for name, kind in relations:
            if kind == "S":
                self.assertFalse(self.one("SELECT has_sequence_privilege('waam_read', %s, 'USAGE')", (name,))[0])
                continue
            self.assertTrue(self.one("SELECT has_table_privilege('waam_read', %s, 'SELECT')", (name,))[0], name)
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                self.assertFalse(self.one("SELECT has_table_privilege('waam_read', %s, %s)",
                                          (name, privilege))[0], f"{name} {privilege}")
        self.assertTrue(self.one("SELECT has_schema_privilege('waam_read', %s, 'USAGE')", (self.schema,))[0])
        self.assertFalse(self.one("SELECT has_schema_privilege('waam_read', %s, 'CREATE')", (self.schema,))[0])
        if self.one("SELECT pg_has_role(current_user, 'waam_read', 'MEMBER')")[0]:
            self.conn.execute("SET ROLE waam_read")
            try:
                self.assertEqual(self.count("runs"), 0)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    self.conn.execute("INSERT INTO notes (run_id, seq) VALUES ('x', 1)")
            finally:
                self.conn.execute("RESET ROLE")


class TestIngest(PostgresCase):
    def test_synthetic_run_fills_every_table(self):
        run = SyntheticRun(self.saves)
        report = self.ingest()
        self.assertEqual((report.seen, report.new, report.errors), (1, 1, 0), report.problems)
        self.assertEqual([p for p in report.problems], [])

        status, sealed, workspace, part, operator, commit, plate, path = self.one(
            "SELECT status, sealed, workspace, part, operator, git_commit, plate, archive_path FROM runs")
        self.assertEqual((status, sealed, workspace, part, operator, commit),
                         ("open", False, WS, "WALLTEST", "operator@example.invalid", "abc1234"))
        self.assertEqual(path, f"{WS}/_runs/{run.run}")
        self.assertEqual(plate["registration"]["corners"], 4)
        self.assertEqual(plate["registration"]["export"], run.weld[:-4] + ".json")
        self.assertEqual((plate["corner_vars"], plate["sense_vars"], plate["substrate_mm"]), ([run.var], [], 12.7))

        on_disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.folder.iterdir()}
        self.assertEqual(dict(self.rows("SELECT name, sha256 FROM run_files")), on_disk)
        kinds = dict(self.rows("SELECT name, kind FROM run_files"))
        self.assertEqual(kinds[f"{run.run}_run.json"], "RUN-JSON")
        self.assertEqual(kinds[run.weld[:-4] + ".passes.json"], "PASSES")
        self.assertEqual(kinds[run.arc[:-4] + ".arcmon-passes.csv"], "ARCMON-PASSES")
        self.assertEqual(self.one("SELECT seq, size_bytes FROM run_files WHERE name = %s", (run.weld,)),
                         (2, len(JBI)))

        export = self.one("SELECT kind, job, job_file, asf, weld_ctwd_mm, clean_first, layers, thermal, measure, "
                          "platter, params, jbi_sha256, jbi_matches, arc_schedule, git_commit FROM exports")
        self.assertEqual(export[:7], ("WELD-JOB", "WALLTEST", run.weld, [81], 19.05, True, [0, 1]))
        self.assertEqual(export[7], {"interpass": "30", "interpass_every": "1", "thermal": "off",
                                     "cooling": "adaptive"})
        self.assertEqual(export[8], {"measure_every": "2", "measure_times": "20, 40",
                                     "measure_stops": [{"layer": 1}]})
        self.assertTrue(export[9]["clear"])
        self.assertNotIn("token", export[10])
        self.assertEqual(export[11:], (hashlib.sha256(JBI).hexdigest(), True, "81", "abc1234"))
        stored = self.rows("SELECT pass, layer, bead, region, asf, aef, v_min_mm_s, v_max_mm_s, travel_mm_min, "
                           "length_mm, weld_moves, first_step, last_step, dwell_s, pauses_before, expected_s, "
                           "model_area_mm2 FROM passes ORDER BY pass")
        self.assertEqual(stored, [tuple(row[key] for key in pass_tables.FIELDS) for row in PASSES])

        files = {name: rest for name, *rest in self.rows(
            "SELECT file_name, format, samples, export_match, sample_period_s, has_step, job FROM arcmon_files")}
        self.assertEqual(set(files), {run.arc, run.trace, run.dat})
        self.assertEqual(files[run.arc], ["realtime-csv", 300, "latest-before", 0.1, True, None])
        self.assertEqual(files[run.dat][:2], ["arcmon-dat", 0])
        blocks = self.one("SELECT summary FROM arcmon_files WHERE file_name = %s", (run.dat,))[0]
        self.assertEqual(blocks["blocks"], [{"file": 1, "values": [[1, 2, 3], [4, 5, 6, 7, 8, 9]]}])
        _, trace_rows = arcmon._realtime_rows(multi_segment_csv().decode().strip())
        self.assertEqual(self.count("arcmon_samples"), 300 + len(trace_rows))
        _, arc_rows = arcmon._realtime_rows(ARC_CSV.read_text().strip())
        index = next(i for i, row in enumerate(arc_rows) if row[1] > arcmon.ARC_ON_A)
        sample = self.one("SELECT volts, amps, step, cond_no, weld_length_mm, other, t_s FROM arcmon_samples s "
                          "JOIN arcmon_files f ON f.id = s.file_id WHERE f.file_name = %s AND s.idx = %s",
                          (run.arc, index))
        expected = arc_rows[index]
        self.assertEqual(sample[:5], (expected[0], expected[1], int(expected[7]), int(expected[9]), expected[10]))
        self.assertEqual(sample[5], {f"CH{i + 1}": expected[i] for i in (2, 3, 4, 5, 6, 8)})
        self.assertAlmostEqual(sample[6], index * 0.1)

        self.assertEqual(self.rows("SELECT seq, value, unit, value_c, stage, note, cycle FROM temperatures "
                                   "ORDER BY seq"),
                         [(1, 480.0, "F", (480 - 32) * 5 / 9, "after-interpass", "", 1),
                          (2, 120.0, "C", 120.0, "before-weld", "pyrometer", None)])
        self.assertEqual(self.rows("SELECT note FROM notes"), [("first wall",)])
        self.assertEqual(self.one("SELECT layer, pass, pre_weld_c, height_mm, width_mm, height_source FROM layer_log"),
                         (0, 1, (300 - 32) * 5 / 9, 3.5, 7.0, "manual"))
        self.assertEqual(self.rows("SELECT seconds, value, value_c FROM layer_readings ORDER BY seconds"),
                         [(20.0, 600.0, (600 - 32) * 5 / 9), (40.5, 500.0, (500 - 32) * 5 / 9)])
        cycle = self.one("SELECT cycle, first_layer, last_layer, n, growth_mm, min_error_mm, max_error_mm, "
                         "t_after_c, t_before_c, bead_height_mm, travel_mm_min, wire_feed_m_min, weld_ctwd_mm "
                         "FROM cycles")
        self.assertEqual(cycle, (1, 0, 0, 1, 3.4, -1.0, 1.25, (480 - 32) * 5 / 9, None, 1.2, 457.2, 10.6, 19.05))

        kinds = dict(self.rows("SELECT file_name, kind FROM var_files"))
        self.assertEqual(kinds, {run.var: "PLATE-CORNERS-VAR", run.touch: "LAYER-TOUCH-VAR"})
        touches = self.rows("SELECT p_index, pulse_s, pulse_l, pulse_u, pulse_r, pulse_b, pulse_t, hit_flag "
                            "FROM var_positions p JOIN var_files f ON f.id = p.var_file_id "
                            "WHERE f.file_name = %s AND p_index BETWEEN 120 AND 122 ORDER BY p_index", (run.touch,))
        self.assertEqual(touches, [(120, -1, 2, -3, 4, -5, 6, 1), (121, 0, 0, 0, 0, 0, 0, 0),
                                   (122, 7, 8, 9, 10, 11, 12, 1)])
        self.assertEqual(self.one("SELECT hit_flag FROM var_positions p JOIN var_files f ON f.id = p.var_file_id "
                                  "WHERE f.file_name = %s AND p_index = 1", (run.var,)), (None,))
        self.assertEqual(self.count("thermocouple_samples"), 0)
        for table in datastore.TABLES:
            text = self.one(sql.SQL("SELECT coalesce(string_agg(t::text, ''), '') FROM {} t")
                            .format(sql.Identifier(table)))[0]
            self.assertNotIn("SECRET", text, table)

    def test_reingest_unchanged_is_a_no_op(self):
        SyntheticRun(self.saves)
        self.ingest()
        before = (self.counts(), self.one("SELECT updated_at, ingested_at FROM runs"),
                  self.rows("SELECT id, file_name FROM exports"))
        report = self.ingest()
        self.assertEqual((report.seen, report.new, report.updated, report.unchanged), (1, 0, 0, 1))
        self.assertEqual(report.problems, [])
        self.assertEqual(before, (self.counts(), self.one("SELECT updated_at, ingested_at FROM runs"),
                                  self.rows("SELECT id, file_name FROM exports")))
        self.assertEqual(self.count("ingest_runs"), 2)

    def test_changed_open_run_updates_in_place(self):
        run = SyntheticRun(self.saves)
        self.ingest()
        export_id = self.one("SELECT id FROM exports")[0]
        runs.add_temperature(self.saves, WS, value=300.0, unit="F", stage="before-weld", now=NOW)
        runs.add_note(self.saves, WS, text="second", now=NOW)
        body = multi_segment_csv().replace(b"200.0", b"201.0")
        second = run.add(kind="ARCMON", body=body, ext="CSV", detail="second", source="robot",
                         arcmon_summary=arcmon.parse("x.csv", body))
        report = self.ingest()
        self.assertEqual((report.updated, report.new, report.errors), (1, 0, 0), report.problems)
        self.assertEqual((self.count("temperatures"), self.count("notes"), self.count("arcmon_files")), (3, 2, 4))
        self.assertEqual(self.one("SELECT id FROM exports")[0], export_id)
        self.assertIn(second, [name for (name,) in self.rows("SELECT name FROM run_files")])
        self.assertEqual(self.one("SELECT export_id, export_match FROM arcmon_files WHERE file_name = %s",
                                  (second,)), (export_id, "latest-before"))

    def test_sealing_updates_then_a_changed_sealed_file_is_refused(self):
        run = SyntheticRun(self.saves)
        self.ingest()
        run.finish()
        report = self.ingest()
        self.assertEqual((report.updated, report.errors), (1, 0), report.problems)
        seal = run.folder / f"{run.run}_SEALED.json"
        self.assertEqual(self.one("SELECT sealed, status, outcome, seal_sha256 FROM runs"),
                         (True, "finished", "completed", hashlib.sha256(seal.read_bytes()).hexdigest()))
        self.assertIn(f"{run.run}_layer_log.csv", [name for (name,) in self.rows("SELECT name FROM run_files")])
        before = (self.counts(), self.rows("SELECT name, sha256 FROM run_files ORDER BY name"),
                  self.one("SELECT updated_at FROM runs"))
        with (run.folder / run.weld).open("ab") as jbi:
            jbi.write(b"'edited after the seal\r\n")
        report = self.ingest()
        self.assertEqual((report.refused, report.errors), (1, 1))
        self.assertIn("SEALED RUN CHANGED ON DISK", report.problems[-1]["message"])
        self.assertIn(run.weld, report.problems[-1]["message"])
        self.assertEqual(before, (self.counts(), self.rows("SELECT name, sha256 FROM run_files ORDER BY name"),
                                  self.one("SELECT updated_at FROM runs")))
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(io.StringIO()):
            code = datastore.main(["--dsn", self.schema_dsn, "ingest", "--saves", str(self.saves)])
        self.assertEqual(code, 1)
        self.assertIn("SEALED RUN CHANGED ON DISK", output.getvalue())

        manifest = json.loads(seal.read_text(encoding="utf-8"))
        for item in manifest["files"]:
            if item["name"] == run.weld:
                body = (run.folder / run.weld).read_bytes()
                item.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        seal.write_text(json.dumps(manifest), encoding="utf-8")
        report = self.ingest()
        self.assertEqual(report.refused, 1)
        self.assertIn("its rows were not touched", report.problems[-1]["message"])
        self.assertIn(seal.name, report.problems[-1]["message"])
        self.assertEqual(before[1], self.rows("SELECT name, sha256 FROM run_files ORDER BY name"))

    def test_sealed_run_that_disagrees_with_its_manifest_is_never_ingested(self):
        run = SyntheticRun(self.saves)
        run.finish()
        (run.folder / run.photo).write_bytes(b"replaced")
        report = self.ingest()
        self.assertEqual((report.refused, report.new), (1, 0))
        self.assertIn("SEALED manifest", report.problems[-1]["message"])
        self.assertEqual(self.count("runs"), 0)

    def test_bad_files_are_reported_not_fatal(self):
        run = SyntheticRun(self.saves)
        garbage = run.add(kind="ARCMON", body=b"//ARC_MONDATA\r\nnot,a,monitor\r\n", ext="CSV", detail="bad",
                          source="robot")
        broken_var = run.add(kind="PLATE-SENSE-VAR", body=b'///P\r\n"PULSE"1,2\r\n', ext="DAT", detail="VAR",
                             source="robot")
        pass_table = run.folder / (run.weld[:-4] + ".passes.json")
        pass_table.write_text("{not json")
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0))
        flagged = {item["file"] for item in report.problems if item["level"] == "warning"}
        self.assertEqual(flagged, {garbage, broken_var, pass_table.name})
        errors = dict(self.rows("SELECT name, parse_error FROM run_files WHERE parse_error IS NOT NULL"))
        self.assertEqual(set(errors), flagged)
        self.assertIn("arc monitor", errors[garbage])
        self.assertEqual(self.count("passes"), 2)
        self.assertEqual((self.count("exports"), self.count("arcmon_files"), self.count("var_files")), (1, 3, 2))
        self.assertEqual(self.count("temperatures"), 2)

    def test_an_unreadable_run_refuses_only_itself(self):
        SyntheticRun(self.saves)
        other = SyntheticRun(self.saves, ws="account-b", name="OTHER")
        (other.folder / f"{other.run}_run.json").write_text("{torn")
        third = SyntheticRun(self.saves, ws="account-c", name="THIRD")
        path = third.folder / f"{third.run}_run.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["measurements"] = {"height_mm": float("nan")}
        path.write_text(json.dumps(record), encoding="utf-8")
        report = self.ingest()
        self.assertEqual((report.seen, report.new, report.refused), (3, 1, 2))
        self.assertEqual({item["run"] for item in report.problems if item["level"] == "error"},
                         {other.run, third.run})
        self.assertEqual(self.rows("SELECT workspace FROM runs"), [(WS,)])
        self.assertEqual(self.one("SELECT count(*) FROM run_files WHERE run_id = %s", (third.run,))[0], 0)

    def test_arc_segments_match_arcmon_per_pass(self):
        run = SyntheticRun(self.saves)
        self.ingest()
        checked = 0
        for name in (run.arc, run.trace):
            body = (run.folder / name).read_bytes()
            segments = arcmon.per_pass(body, [])["segments"]
            expected = arcmon.per_pass(body, [{} for _ in range(segments)])["rows"]
            got = self.rows("SELECT segment, arc_on_s, weld_s, current_mean_a, current_min_a, current_max_a, "
                            "voltage_mean_v, arc_current_mean_a, arc_voltage_mean_v, asf, aef, dropouts, "
                            "weld_length_mm, speed_mm_min, heat_input_kj_mm FROM arc_segments s "
                            "JOIN arcmon_files f ON f.id = s.file_id WHERE f.file_name = %s ORDER BY segment",
                            (name,))
            self.assertEqual(len(got), segments)
            for want, have in zip(expected, got):
                with self.subTest(file=name, segment=have[0]):
                    self.assertAlmostEqual(round(have[1], 3), want["arc_on_s"])
                    self.assertAlmostEqual(round(have[2], 3), want["weld_s"])
                    for index, key in ((3, "current_mean_a"), (4, "current_min_a"), (5, "current_max_a"),
                                       (6, "voltage_mean_v"), (7, "arc_current_mean_a"), (8, "arc_voltage_mean_v")):
                        self.assertAlmostEqual(have[index], want[key], places=9)
                    self.assertEqual((have[9], have[10], have[11], have[12], have[13]),
                                     (want["cond_no"], want["aef_no"], want["dropouts"], want["weld_length_mm"],
                                      want["weld_speed_mm_min"]))
                    self.assertAlmostEqual(have[14], have[6] * have[3] / (have[13] / 60) / 1000)
                    checked += 1
        self.assertEqual(checked, 3)
        self.assertEqual(self.rows("SELECT asf, aef, dropouts, first_step, last_step, round(speed_mm_min::numeric, 6) "
                                   "FROM arc_segments s JOIN arcmon_files f ON f.id = s.file_id "
                                   "WHERE f.file_name = %s ORDER BY segment", (run.trace,)),
                         [(83, 84, 1, 4, 4, 455), (85, 85, 0, 7, 7, 455)])

    def test_accounts_sessions_and_tokens_are_never_read(self):
        run = SyntheticRun(self.saves)
        for folder in (self.tmp, self.saves, self.saves / WS, self.saves / WS / "_runs", run.folder):
            for name in PROTECTED:
                (folder / name).write_text("SECRET-PROTECTED")
        opened = []
        real_os_open, real_open = os.open, builtins.open

        def os_open(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_os_open(path, *args, **kwargs)

        def open_(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        with patch("os.open", os_open), patch("builtins.open", open_), patch("io.open", open_):
            report = self.ingest()
        names = {Path(os.fsdecode(path)).name for path in opened}
        self.assertIn(f"{run.run}_run.json", names)
        self.assertFalse(names & set(PROTECTED), names & set(PROTECTED))
        skipped = {item["file"] for item in report.problems if "not read" in item["message"]}
        self.assertEqual(skipped, set(PROTECTED))
        for table in datastore.TABLES:
            text = self.one(sql.SQL("SELECT coalesce(string_agg(t::text, ''), '') FROM {} t")
                            .format(sql.Identifier(table)))[0]
            self.assertNotIn("SECRET", text, table)

    def test_links_are_never_followed(self):
        run = SyntheticRun(self.saves)
        outside = self.tmp / "outside.txt"
        outside.write_text("outside the archive")
        linked = run.folder / f"{run.run}_90_ATTACH_linked.txt"
        os.link(outside, linked)
        elsewhere = self.tmp / "elsewhere"
        shutil.copytree(run.folder, elsewhere / "2026-10-07_LINKED_r01")
        target = self.saves / WS / "_runs" / "2026-10-07_LINKED_r01"
        try:
            os.symlink(elsewhere / "2026-10-07_LINKED_r01", target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            import _winapi
            _winapi.CreateJunction(str(elsewhere / "2026-10-07_LINKED_r01"), str(target))
        report = self.ingest()
        self.assertEqual(self.rows("SELECT run_id FROM runs"), [(run.run,)])
        messages = {(item["run"], item["file"]): item["message"] for item in report.problems}
        self.assertIn("link", messages[("2026-10-07_LINKED_r01", None)])
        self.assertIn("not read", messages[(run.run, linked.name)])
        self.assertNotIn(linked.name, [name for (name,) in self.rows("SELECT name FROM run_files")])

    def test_mirror_layout_and_cli(self):
        run = SyntheticRun(self.saves)
        run.finish()
        mirror = self.tmp / "RAW SLICER FILES"
        shutil.copytree(run.folder, mirror / "operator" / "Runs" / run.run)
        (mirror / "Part library").mkdir()
        (mirror / "README.txt").write_text("written by the copy")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = datastore.main(["--dsn", self.schema_dsn, "ingest", "--mirror", "--saves", str(mirror),
                                   "--workspace", WS])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("1 runs seen, 1 new", out.getvalue())
        self.assertEqual(self.one("SELECT sealed, archive_path FROM runs"), (True, f"operator/Runs/{run.run}"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(datastore.main(["--dsn", self.schema_dsn, "ingest", "--mirror", "--saves",
                                             str(mirror), "--workspace", "someone-else"]), 0)
        self.assertIn("0 runs seen", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(datastore.main(["--dsn", self.schema_dsn, "status"]), 0)
        text = out.getvalue()
        self.assertIn("schema: 0001_initial", text)
        self.assertIn("last ingest: ", text)
        self.assertIn("0 seen", text)
        self.assertRegex(text, r"arcmon_samples\s+\d+")

    def test_documented_queries_run(self):
        run = SyntheticRun(self.saves)
        self.ingest()
        text = (ROOT / "docs/run-database.md").read_text(encoding="utf-8")
        blocks = [block.split("```", 1)[0] for block in text.split("```sql\n")[1:]]
        statements = [s.strip() for block in blocks for s in block.split(";") if s.strip()]
        self.assertGreaterEqual(len(statements), 8)
        self.assertTrue(all("runindex." in statement for statement in statements))
        for statement in statements:
            with self.subTest(statement=statement[:60]):
                self.conn.execute(statement.replace("2026-10-06_LAYER-TOUCHV7_r01", run.run)
                                  .replace("runindex.", f"{self.schema}.")).fetchall()
        per_asf = self.rows("SELECT asf, count(*) FROM arc_segments GROUP BY asf ORDER BY asf")
        self.assertEqual(per_asf, [(1, 1), (83, 1), (85, 1)])

    def test_dry_run_writes_nothing(self):
        SyntheticRun(self.saves)
        report = self.ingest(dry_run=True)
        self.assertEqual((report.seen, report.new), (1, 1))
        self.assertIsNone(self.one("SELECT to_regclass('runs')")[0])

    def test_a_second_ingest_waits_for_none(self):
        SyntheticRun(self.saves)
        with psycopg.connect(self.schema_dsn, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", (datastore._LOCK_CLASS + 1, self.schema))
            self.assertIsNone(self.ingest())
        self.assertEqual(self.ingest().new, 1)


class TestIngestHardening(PostgresCase):


    def test_an_implausible_arc_csv_is_refused_and_every_query_still_runs(self):
        run = SyntheticRun(self.saves)
        huge = [welding(amps=(i + 1) * 1e200, length=1.0 + i) for i in range(3)]
        poison = run.add(kind="ATTACH", body=arc_csv(huge), ext="csv", detail="poison", source="operator")
        slow = run.add(kind="ATTACH", body=arc_csv([welding()] * 3, sampling="1" + "0" * 306), ext="csv",
                       detail="slow", source="operator")
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0), report.problems)
        errors = dict(self.rows("SELECT name, parse_error FROM run_files WHERE parse_error IS NOT NULL"))
        self.assertEqual(set(errors), {poison, slow})
        self.assertIn("CURRENT (CH2) reading 1e+200 in data row 1 is outside -10000..10000", errors[poison])
        self.assertIn("sampling 1e+306 s is outside", errors[slow])
        self.assertEqual(self.count("arcmon_files"), 3)
        for query in ("SELECT * FROM arc_segments",
                      "SELECT asf, avg(current_mean_a), avg(heat_input_kj_mm), stddev(voltage_mean_v) "
                      "FROM arc_segments GROUP BY asf"):
            self.assertTrue(self.rows(query))

    def test_arc_segments_are_stored_rows_not_a_scan_of_every_sample(self):
        SyntheticRun(self.saves)
        self.ingest()
        file_id = self.one("SELECT min(file_id) FROM arcmon_segments")[0]
        plan = json.dumps(self.one("EXPLAIN (FORMAT JSON) SELECT * FROM arc_segments WHERE file_id = %s",
                                   (file_id,))[0])
        for node in ("arcmon_samples", "WindowAgg", "Aggregate", "CTE Scan"):
            self.assertNotIn(node, plan)

    def test_segment_speed_is_weld_speed_and_length_follows_its_unit(self):
        run = SyntheticRun(self.saves)
        off = [welding(amps=0.0, step=0, length=0.0, spd=0.0, cond=1083)] * 5
        single = [welding(length=4.0)]
        reset = [welding(length=v) for v in (10.0, 11.0, 0.5, 1.5)]
        body = arc_csv(off + single + off + reset + off, len_unit="cm")
        name = run.add(kind="ATTACH", body=body, ext="csv", detail="cm", source="operator")
        odd = run.add(kind="ATTACH", body=arc_csv(off + reset, len_unit="furlong"), ext="csv", detail="odd",
                      source="operator")
        report = self.ingest()
        self.assertEqual(report.problems, [])
        got = self.rows("SELECT segment, speed_mm_min, weld_length_mm, first_step, arc_on_s FROM arc_segments "
                        "WHERE file_name = %s ORDER BY segment", (name,))
        self.assertEqual(got, [(1, 455.0, 40.0, 4, 0.1), (2, 455.0, 110.0, 4, 0.4)])
        want = arcmon.per_pass(body, [{}, {}])["rows"]
        self.assertEqual([(row["weld_speed_mm_min"], row["weld_length_mm"]) for row in want],
                         [(speed, length) for _, speed, length, _, _ in got])
        self.assertEqual(self.rows("SELECT DISTINCT s.weld_length_mm FROM arcmon_samples s JOIN arcmon_files f "
                                   "ON f.id = s.file_id WHERE f.file_name = %s AND s.amps > 10 ORDER BY 1",
                                   (name,)), [(5.0,), (15.0,), (40.0,), (100.0,), (110.0,)])
        self.assertEqual(self.one("SELECT weld_length_mm FROM arc_segments WHERE file_name = %s", (odd,)), (None,))
        self.assertEqual(self.one("SELECT other->>'CH11' FROM arcmon_samples s JOIN arcmon_files f ON "
                                  "f.id = s.file_id WHERE f.file_name = %s AND idx = 6", (odd,)), ("11.0",))

    def test_inventory_keeps_only_the_record_bodies(self):
        run = SyntheticRun(self.saves)
        run.finish()
        worker = datastore._Ingest(self.conn, self.saves, mirror=False, workspace=None, report=datastore.Report())
        files, skipped = worker.inventory(run.run, run.folder, [])
        self.assertEqual(skipped, {})
        self.assertEqual({name for name, f in files.items() if f.body is not None},
                         {f"{run.run}_run.json", f"{run.run}_SEALED.json"})
        self.assertEqual(worker.read(files[run.trace]), (run.folder / run.trace).read_bytes())
        (run.folder / run.trace).write_bytes(b"swapped")
        with self.assertRaisesRegex(ValueError, "changed while it was being indexed"):
            worker.read(files[run.trace])

    def test_a_run_the_ingest_died_in_goes_last_then_is_skipped(self):
        first = SyntheticRun(self.saves, ws="account-a", name="FIRST")
        second = SyntheticRun(self.saves, ws="account-b", name="SECOND")
        real_write, order = datastore._Ingest.write, []

        def dies_in_first(worker, run_id, *args, **kwargs):
            order.append(run_id)
            if run_id == first.run:
                raise KeyboardInterrupt
            return real_write(worker, run_id, *args, **kwargs)

        with patch.object(datastore._Ingest, "write", dies_in_first):
            for tick in range(datastore.MAX_ATTEMPTS):
                with self.assertRaises(KeyboardInterrupt):
                    self.ingest()
                self.assertEqual(self.one("SELECT attempts FROM ingest_attempts WHERE run_id = %s",
                                          (first.run,)), (tick + 1,))
        self.assertEqual(order, [first.run, second.run, first.run, first.run])
        report = self.ingest()
        self.assertEqual((report.refused, report.unchanged), (1, 1))
        self.assertIn("stopped inside it 3 times", report.problems[-1]["message"])
        self.assertEqual(self.count("runs"), 1)
        runs.add_note(self.saves, "account-a", text="changed", now=NOW)
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0), report.problems)
        self.assertEqual(self.count("ingest_attempts"), 0)

    def test_entries_added_to_a_sealed_run_are_a_change_even_unread(self):
        run = SyntheticRun(self.saves)
        run.finish()
        self.assertEqual(self.ingest().new, 1)
        outside = self.tmp / "outside.bin"
        outside.write_bytes(b"x")
        added = (f"{run.run}_90_ATTACH_hardlink.csv", f"{run.run}_91_ATTACH_big.csv", f"{run.run}_92_ATTACH_bad_name.csv")
        os.link(outside, run.folder / added[0])
        (run.folder / added[1]).write_bytes(b"0" * (runs.MAX_FILE_BYTES + 1))
        (run.folder / added[2]).write_bytes(b"1")
        report = self.ingest()
        self.assertEqual((report.refused, report.unchanged), (1, 0))
        message = report.problems[-1]["message"]
        self.assertIn("SEALED RUN CHANGED ON DISK", message)
        for name, why in zip(added, ("unreadable", "unreadable", "not an archive file")):
            self.assertIn(f"{name} (not read: {why})", message)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(datastore.main(["--dsn", self.schema_dsn, "ingest", "--saves", str(self.saves)]), 1)

    def test_an_ingest_version_bump_rederives_an_open_run(self):
        run = SyntheticRun(self.saves)
        self.ingest()
        before = self.counts()
        self.conn.execute("UPDATE exports SET job = 'STALE'")
        self.conn.execute("""UPDATE arcmon_files SET summary = '{"stale": true}'""")
        self.conn.execute("UPDATE var_files SET kind = 'STALE'")
        self.conn.execute("DELETE FROM arcmon_segments")
        with patch.object(datastore, "INGEST_VERSION", datastore.INGEST_VERSION + 1):
            report = self.ingest()
        self.assertEqual((report.updated, report.errors), (1, 0), report.problems)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows("SELECT job FROM exports"), [("WALLTEST",)])
        self.assertEqual(self.one("SELECT count(*) FROM arcmon_files WHERE summary ? 'stale'")[0], 0)
        self.assertEqual(self.one("SELECT kind FROM var_files WHERE file_name = %s", (run.var,)),
                         ("PLATE-CORNERS-VAR",))
        self.assertEqual(self.one("SELECT ingest_version FROM runs")[0], datastore.INGEST_VERSION + 1)

    def test_parse_errors_survive_an_unrelated_update_and_inputs_rederive_an_export(self):
        run = SyntheticRun(self.saves)
        table = run.folder / (run.weld[:-4] + ".passes.json")
        good = table.read_text(encoding="utf-8")
        table.write_text("{not json")
        self.ingest()
        broken = self.rows("SELECT name, parse_error FROM run_files WHERE parse_error IS NOT NULL")
        self.assertEqual([name for name, _ in broken], [table.name])
        runs.add_note(self.saves, WS, text="unrelated", now=NOW)
        self.assertEqual(self.ingest().updated, 1)
        self.assertEqual(self.rows("SELECT name, parse_error FROM run_files WHERE parse_error IS NOT NULL"), broken)

        data = json.loads(good)
        data["passes"][0]["travel_mm_min"] = 222.0
        table.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.ingest().updated, 1)
        self.assertEqual(self.rows("SELECT name FROM run_files WHERE parse_error IS NOT NULL"), [])
        self.assertEqual(self.one("SELECT travel_mm_min FROM passes WHERE pass = 1"), (222.0,))

    def test_nul_in_a_waiver_or_a_note_is_replaced_not_fatal(self):
        run = SyntheticRun(self.saves)

        def before_nul_refusal(value, label, maximum):

            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{label} limit: {maximum} characters")
            return value

        with patch.object(runs, "_text", before_nul_refusal):
            runs.add_note(self.saves, WS, text="a\x00b", now=NOW)
            runs.finish(self.saves, WS, outcome="completed", waiver={"reason": "no monitor\x00"},
                        by="op@example.invalid", now=NOW)
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0), report.problems)
        self.assertEqual(self.one("SELECT arc_waiver->>'reason' FROM runs WHERE run_id = %s", (run.run,)),
                         ("no monitor\ufffd",))
        self.assertIn(("a\ufffdb",), self.rows("SELECT note FROM notes"))

    def test_a_failure_in_one_run_or_file_does_not_stop_the_rest(self):
        run = SyntheticRun(self.saves)
        deep = SyntheticRun(self.saves, ws="account-b", name="DEEP")
        path = deep.folder / f"{deep.run}_run.json"
        path.write_text(path.read_text(encoding="utf-8")[:-2] + ', "deep": ' + "[" * 100_000 + "]" * 100_000 + "}")
        sidecar = run.folder / (run.weld[:-4] + ".json")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data["job"] = "J" * 3000
        sidecar.write_text(json.dumps(data), encoding="utf-8")
        report = self.ingest()
        self.assertEqual((report.new, report.refused), (1, 1), report.problems)
        self.assertIn("longer than 200 characters", dict(self.rows(
            "SELECT name, parse_error FROM run_files WHERE parse_error IS NOT NULL"))[sidecar.name])
        third = SyntheticRun(self.saves, ws="account-c", name="THIRD")
        limit = psycopg.errors.ProgramLimitExceeded

        def plate(worker, run_id, *args):
            raise limit("plate: over a limit")

        def write(worker, run_id, *args, **kwargs):
            raise limit("run: over a limit")

        with patch.object(datastore._Ingest, "plate", plate):
            report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 1))
        self.assertIn("plate: over a limit", " ".join(p["message"] for p in report.problems))
        runs.add_note(self.saves, "account-c", text="again", now=NOW)
        with patch.object(datastore._Ingest, "write", write):
            report = self.ingest()
        self.assertEqual(report.refused, 2)
        self.assertEqual(self.one("SELECT count(*) FROM notes WHERE run_id = %s", (third.run,))[0], 1)

    def test_a_torn_cycle_sidecar_keeps_the_cycle_row(self):
        run = SyntheticRun(self.saves)
        (run.folder / (run.weld[:-4] + ".json")).write_text("{torn")
        report = self.ingest()
        self.assertEqual((report.new, report.errors), (1, 0), report.problems)
        self.assertEqual(self.one("SELECT cycle, wire_feed_m_min, bead_height_mm FROM cycles"), (1, 10.6, None))

    def test_var_files_need_no_layer_sense(self):
        SyntheticRun(self.saves)
        with patch.dict(sys.modules, {"waam_pipeline.layer_sense": None}):
            report = self.ingest()
        self.assertEqual((report.new, report.problems), (1, []))
        self.assertGreater(self.count("var_positions"), 0)

    def test_mtime_comes_from_the_descriptor_read(self):
        run = SyntheticRun(self.saves)
        with patch.object(datastore.os, "lstat", side_effect=FileNotFoundError(errno.ENOENT, "not needed")):
            report = self.ingest()
        self.assertEqual((report.new, report.problems), (1, []))
        mtime = self.one("SELECT mtime FROM run_files WHERE name = %s", (run.weld,))[0]
        self.assertAlmostEqual(mtime.timestamp(), (run.folder / run.weld).stat().st_mtime, places=3)

    def test_out_of_range_numbers_are_null_not_an_overflow_later(self):
        self.assertIsNone(datastore._num(1e200))
        self.assertIsNone(datastore._num("-1e16"))
        self.assertIsNone(datastore._num(10 ** 400))
        self.assertEqual(datastore._num("12.5"), 12.5)


class TestIngestService(unittest.TestCase):
    def test_unit_hardening(self):
        unit = (ROOT / "deploy/waam-ingest.service").read_text(encoding="utf-8")
        for line in ("TemporaryFileSystem=/srv:ro", "InaccessiblePaths=-/etc/waam -/etc/metabase -/var/log",
                     "PrivateNetwork=yes", "SystemCallFilter=@system-service", "UMask=0077",
                     "Environment=OPENBLAS_NUM_THREADS=1", "RestrictAddressFamilies=AF_UNIX", "MemoryMax=1G"):
            self.assertIn(f"\n{line}\n", unit)
        binds = next(row for row in unit.splitlines() if row.startswith("BindReadOnlyPaths="))
        self.assertEqual(binds.split("=", 1)[1].split(), ["/srv/waam/waam-pipeline/src",
                                                          "/srv/waam/waam-pipeline/saves", "/srv/waam/venv"])


if __name__ == "__main__":
    unittest.main()
