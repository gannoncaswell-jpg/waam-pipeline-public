from __future__ import annotations

import csv
import io
import json
import sys

from . import run_log_postgres, run_log_store

_READS = ("list", "detail", "export")


def log_failure(action: str, exc: BaseException) -> None:


    causes = []
    while exc is not None and len(causes) < 5:
        causes.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    print(f"[waam] run-log {action} failed: {run_log_postgres.redact(' <- '.join(causes))}",
          file=sys.stderr, flush=True)


def _spreadsheet_value(value):

    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(record: dict) -> bytes:

    setup = record.get("setup") or {}
    identity = {key: record.get(key) for key in
                ("run", "name", "operator", "started_at", "finished_at", "status", "outcome", "data_class")}
    setup_keys = ("specimen_id", "substrate_id", "material", "wire_material", "wire_diameter_mm",
                  "shielding_gas", "gas_flow_l_min", "travel_mm_min", "wire_feed_m_min", "voltage_v",
                  "ctwd_mm", "arc_file")
    setup_fields = {"setup_" + key: setup.get(key) for key in setup_keys}
    layer_keys = ("layer", "pass", "unit", "pre_weld", "readings", "park_travel_s_est",
                  "height_mm", "height_source", "width_mm", "current_a", "voltage_v", "note", "at")
    fields = [*identity, *setup_fields, *layer_keys]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for layer in record.get("layer_log") or [{}]:
        row = {**identity, **setup_fields, **{key: layer.get(key) for key in layer_keys}}
        row["readings"] = json.dumps(layer.get("readings") or {}, sort_keys=True)
        writer.writerow({key: _spreadsheet_value(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def reporting_csv(root, actor: str, dataset: str) -> bytes:

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=run_log_store.reporting_fields(dataset))
    writer.writeheader()
    try:
        rows = run_log_store.reporting_rows(root, actor, dataset)
    except run_log_store.CatalogError as exc:

        log_failure(f"excel {dataset}", exc)
        raise
    for row in rows:
        writer.writerow({key: _spreadsheet_value(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def dispatch(handler, root, workspace: str, action: str, query: dict, *, admin=False, commit=None):

    who = getattr(handler, "waam_who", None) or {}
    actor = who.get("email")
    if not actor or who.get("guest") or workspace.startswith("guest-"):
        handler._json(403, {"ok": False, "error": "Sign in to use run logs."})
        return
    try:
        run_id = query.get("run", [""])[0]
        if action == "list":
            result = {"runs": run_log_store.list_runs(
                root, actor, admin=admin, search=query.get("search", [""])[0],
                status=query.get("status", [""])[0])}
        elif action in ("detail", "export"):
            result = run_log_store.get_run(root, actor, run_id, admin=admin)
            if action == "export":
                handler._send(200, export_csv(result["record"]), "text/csv; charset=utf-8",
                              extra={"Content-Disposition": f'attachment; filename="{run_id}_measurements.csv"'})
                return
        elif action == "add":

            run_log_store.get_run(root, actor, run_id, admin=admin)
            kind = query.get("kind", [""])[0]
            if kind not in ("attachment", "arcmon"):
                raise ValueError("Choose attachment or arcmon for the upload.")
            result = run_log_store.update_run(
                root, actor, run_id, admin=admin, revision=query.get("revision", [""])[0],
                action="add", data={"body": handler._read_run_body(25 * 1024 * 1024),
                                    "name": query.get("name", [""])[0],
                                    "kind": "ARCMON" if kind == "arcmon" else "ATTACH"})
        elif action in ("start", "update"):
            obj = json.loads(handler._read_run_body(65536))
            if not isinstance(obj, dict):
                raise ValueError("Send a JSON object.")
            if action == "start":
                result = run_log_store.create_run(
                    root, workspace, actor, name=obj.get("name"), setup=obj.get("setup"),
                    collaborators=obj.get("collaborators"), commit=commit)
            else:
                result = run_log_store.update_run(
                    root, actor, run_id, admin=admin, revision=obj.get("revision"),
                    action=obj.get("action"), data=obj.get("data"))
        else:
            handler._json(404, {"ok": False, "error": "Unknown run-log action."})
            return
        handler._json(200, {"ok": True, **result})
    except run_log_store.ConflictError as exc:
        handler._json(409, {"ok": False, "error": str(exc)})
    except (PermissionError, FileNotFoundError):
        handler._json(404, {"ok": False, "error": "Run not found or not shared with your account."})
    except (ValueError, TypeError) as exc:
        handler._json(400, {"ok": False, "error": str(exc)})
    except run_log_store.CatalogError as exc:
        log_failure(action, exc)
        if action in _READS:
            handler._json(503, {"ok": False, "error": "Run logs are unavailable: the SQL catalog could not be "
                                "read. Nothing was changed. Try again in a moment."})
        else:
            handler._json(500, {"ok": False, "save_uncertain": True, "error":
                               "The SQL catalog could not be refreshed. A save may already be recorded "
                               "in the archive. Reload and check the run before retrying."})
    except OSError as exc:
        log_failure(action, exc)
        handler._json(500, {"ok": False, "error": "Run logs could not be saved or read. Reload the run before retrying."})
