from __future__ import annotations

import gzip
import hashlib
import json
import re
from functools import lru_cache
from threading import Lock

_BUILD_LOCK = Lock()


def _asset(kind: str, body: bytes, ctype: str) -> dict:
    key = kind + "-" + hashlib.sha256(body).hexdigest()
    return {"key": key, "body": body, "gzip": gzip.compress(body, compresslevel=6, mtime=0),
            "type": ctype}


@lru_cache(maxsize=1)
def _assets() -> dict:
    from .cell_lc import _decimated_link_meshes
    from .studio_renderer import renderer_source

    assets = {}
    for kind, body, ctype in (
        ("renderer", renderer_source().encode("utf-8"), "text/javascript; charset=utf-8"),
        ("robot", json.dumps(_decimated_link_meshes(), separators=(",", ":")).encode("utf-8"),
         "application/json"),
    ):
        assets[kind] = _asset(kind, body, ctype)
    return assets


@lru_cache(maxsize=1)
def _hc_robot_asset() -> dict:

    from .cell_hc import _link_meshes

    body = json.dumps(_link_meshes(), separators=(",", ":")).encode("utf-8")
    return _asset("robot", body, "application/json")


def assets() -> dict:

    with _BUILD_LOCK:
        return _assets()


def find(key: str) -> dict | None:
    found = next((a for a in assets().values() if a["key"] == key), None)
    if found is not None or not isinstance(key, str) or not re.fullmatch(r"robot-[a-f0-9]{64}", key):
        return found


    with _BUILD_LOCK:
        hc = _hc_robot_asset()
    return hc if hc["key"] == key else None


def _state_robot_asset(state: dict) -> dict:
    meta = state.get("meta") or {}
    profile = meta.get("cell_profile")
    selected = (meta.get("params") or {}).get("cell_profile")
    if profile is not None:
        if not isinstance(profile, dict) or "id" not in profile:
            raise ValueError("Invalid cell identity for robot display")
        model = profile["id"]
        if selected is not None and selected != model:
            raise ValueError("Conflicting cell identity for robot display")
    else:


        model = "lc50s" if selected is None else selected
    if model == "lc50s":
        return assets()["robot"]
    if model == "hc10":
        with _BUILD_LOCK:
            return _hc_robot_asset()
    raise ValueError("Unsupported cell identity for robot display")


def transport_state(state: dict) -> dict:
    if not state.get("robot"):
        return state
    return {**state, "robot": {**state["robot"], "meshes": None,
                              "mesh_asset": _state_robot_asset(state)["key"]}}


def accepts_gzip(value: str) -> bool:

    qualities = {}
    for entry in value.lower().split(","):
        name, *params = entry.strip().split(";")
        q = 1.0
        for param in params:
            if param.strip().startswith("q="):
                try:
                    q = float(param.strip()[2:])
                except ValueError:
                    q = 0.0
        qualities[name.strip()] = q if 0 <= q <= 1 else 0.0
    return qualities.get("gzip", qualities.get("*", 0)) > 0
