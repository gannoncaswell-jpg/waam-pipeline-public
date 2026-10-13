from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

try:
    import psycopg
except ImportError:
    psycopg = None

ROOT = Path(__file__).resolve().parents[1]


class _Postgres:
    dsn: str | None = None
    error: str | None = None


def _exe(directory: Path, name: str) -> Path:
    return directory / (name + ".exe" if os.name == "nt" else name)


def _reachable(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT 1")
    return dsn


def _stop_cluster(pg_ctl: Path, data: Path, temp: Path) -> None:
    subprocess.run([str(pg_ctl), "-D", str(data), "-m", "immediate", "-w", "stop"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
    shutil.rmtree(temp, ignore_errors=True)


def _temporary_cluster(bin_dir: Path) -> str:
    temp = Path(tempfile.mkdtemp(prefix="waam-pg-"))
    data, log = temp / "data", temp / "server.log"
    initdb = subprocess.run([str(_exe(bin_dir, "initdb")), "-D", str(data), "-A", "trust", "-U", "postgres",
                             "-E", "UTF8", "--no-locale"], capture_output=True, text=True, timeout=300, check=False)
    if initdb.returncode:
        shutil.rmtree(temp, ignore_errors=True)
        raise RuntimeError(f"initdb failed: {initdb.stderr.strip()[-400:]}")
    pg_ctl = _exe(bin_dir, "pg_ctl")
    for _ in range(3):

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        options = f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off -c full_page_writes=off"
        if os.name != "nt":
            options += f" -c unix_socket_directories={temp}"

        started = subprocess.run([str(pg_ctl), "-D", str(data), "-o", options, "-l", str(log), "-w", "-t", "60",
                                  "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
                                 check=False)
        if started.returncode == 0:
            break
    else:
        tail = log.read_text(errors="replace")[-400:] if log.exists() else ""
        shutil.rmtree(temp, ignore_errors=True)
        raise RuntimeError(f"pg_ctl start failed: {tail}")
    atexit.register(_stop_cluster, pg_ctl, data, temp)
    dsn = f"host=127.0.0.1 port={port} user=postgres dbname=postgres"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE ROLE waam_read NOLOGIN")
    return dsn


def postgres_dsn() -> str:
    if _Postgres.dsn:
        return _Postgres.dsn
    if _Postgres.error:
        raise AssertionError(_Postgres.error)
    bin_dir = Path(os.environ.get("WAAM_PG_BIN") or ROOT.parent / "tools" / "postgres" / "pgsql" / "bin")
    try:
        if psycopg is None:
            raise RuntimeError("psycopg 3 is not installed (pip install -r requirements-dev.txt)")
        if os.environ.get("WAAM_TEST_PG_DSN"):
            dsn = _reachable(os.environ["WAAM_TEST_PG_DSN"])
        elif _exe(bin_dir, "initdb").exists() and _exe(bin_dir, "pg_ctl").exists():
            dsn = _temporary_cluster(bin_dir)
        elif os.name == "posix":
            dsn = _reachable("dbname=waam_test")
        else:
            raise RuntimeError(f"no initdb/pg_ctl in {bin_dir}")
    except Exception as exc:
        _Postgres.error = (f"No PostgreSQL for the database tests ({exc}). Set WAAM_TEST_PG_DSN, point "
                           "WAAM_PG_BIN at a folder with initdb and pg_ctl, or create database waam_test "
                           "(docs/run-database.md). Missing tools fail, never skip.")
        raise AssertionError(_Postgres.error) from exc
    _Postgres.dsn = dsn
    return dsn
