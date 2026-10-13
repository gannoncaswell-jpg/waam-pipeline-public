# WAAM Pipeline

STL and G-code processing, toolpath preview, clearance checks, and Yaskawa job export.

## Setup

Requires Python 3.10 or newer. Run from the repository root.

```sh
python -m venv .venv
```

Activate with `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on Linux/macOS, then:

```sh
python -m pip install -e .
python -m waam_pipeline.server --host 127.0.0.1 --port 8017
```

Open http://127.0.0.1:8017.

## Tests

Requires Node.js 24 or a compatible version listed in `package.json`.

```sh
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
python -m unittest discover -s tests
```

The full suite requires PostgreSQL: set `WAAM_TEST_PG_DSN` to a dedicated test database, or set `WAAM_PG_BIN` to a folder containing `initdb` and `pg_ctl` so the tests create a temporary server. See [database setup](docs/run-database.md). Optional PyBullet tests skip when the engine is unavailable.

## CLI

```sh
python -m waam_pipeline examples/square.gcode --check --html preview.html
```

Cell profiles and calibration live in `examples/` and `docs/*.json`. Verify them against your equipment before hardware use. Export review and clearance gates remain enabled.

Optional setup: [simulation](docs/wsl2-setup.md), [database](docs/run-database.md), [desktop releases](docs/desktop-download.md).
