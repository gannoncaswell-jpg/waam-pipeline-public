from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "measurement-sheet.csv"


COLUMNS = ["id", "component", "parameter", "current_value_mm", "basis",
           "priority", "how_to_measure",
           "MEASURED_VALUE_MM", "MEASURED_BY", "DATE", "NOTES"]
BLANKS = ["MEASURED_VALUE_MM", "MEASURED_BY", "DATE", "NOTES"]
PRIORITY_ORDER = {"H": 0, "M": 1, "L": 2}


def _cell_models():

    spec = importlib.util.spec_from_file_location(
        "cell_models", Path(__file__).with_name("cell_models.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows(measurements) -> list[dict[str, str]]:

    seen: set[str] = set()
    out = []
    for n, m in enumerate(measurements):
        missing = [c for c in COLUMNS if c not in BLANKS and c not in m]
        if missing:
            raise ValueError(f"MEASUREMENTS[{n}] is missing {missing}")
        rid = str(m["id"])
        if rid in seen:
            raise ValueError(f"duplicate measurement id {rid!r}")
        seen.add(rid)
        if m["priority"] not in PRIORITY_ORDER:
            raise ValueError(f"{rid}: priority must be H/M/L, got {m['priority']!r}")
        row = {c: "" for c in COLUMNS}
        for c in COLUMNS:
            if c in m:
                v = m[c]
                row[c] = f"{v:g}" if isinstance(v, float) else str(v)
        out.append(row)
    order = {r["id"]: i for i, r in enumerate(out)}
    return sorted(out, key=lambda r: (PRIORITY_ORDER[r["priority"]], order[r["id"]]))


def carried_entries(path: Path) -> dict[str, dict[str, str]]:


    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            existing = list(csv.DictReader(fh))
    except OSError as exc:
        raise SystemExit(f"cannot read existing {path.name} ({exc}); move it aside first")
    if existing and "id" not in existing[0]:
        raise SystemExit(
            f"{path.name} has no id column; refusing to overwrite it and lose data")
    out: dict[str, dict[str, str]] = {}
    for row in existing:
        rid = (row.get("id") or "").strip()
        if not rid:
            continue
        keep = {c: row[c] for c in BLANKS if (row.get(c) or "").strip()}
        if keep:
            out[rid] = keep
    return out


def write_sheet(path: Path | str = OUT, measurements=None) -> Path:


    if measurements is None:
        measurements = _cell_models().MEASUREMENTS
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    carried = carried_entries(out)
    new_rows = rows(measurements)
    for row in new_rows:
        row.update(carried.get(row["id"], {}))
    dropped = sorted(set(carried) - {r["id"] for r in new_rows})
    if dropped:

        raise SystemExit(
            f"{out.name} holds field data for row id(s) the model no longer "
            f"defines: {', '.join(dropped)}. Rename the id in the sheet or "
            "restore it in cell_models.MEASUREMENTS; refusing to drop a "
            "measurement on the floor.")
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(new_rows)
    return out


def main() -> int:
    cm = _cell_models()
    path = write_sheet(measurements=cm.MEASUREMENTS)
    counts = cm.measurements_by_priority()
    tally = "  ".join(f"{k}={counts.get(k, 0)}" for k in ("H", "M", "L"))
    components = sorted({str(m["component"]) for m in cm.MEASUREMENTS})
    print(f"{len(cm.MEASUREMENTS)} rows  ({tally})  over {len(components)} "
          f"components -> {path.relative_to(REPO)}")
    for c in components:
        n = sum(1 for m in cm.MEASUREMENTS if m["component"] == c)
        print(f"  {c:20s} {n:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
