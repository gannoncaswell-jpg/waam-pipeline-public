from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Mapping

from .arcparams import deposited_area_mm2
from .toolpath import Toolpath


FIELDS = ("pass", "layer", "bead", "region", "asf", "aef", "v_min_mm_s", "v_max_mm_s", "travel_mm_min",
          "length_mm", "weld_moves", "first_step", "last_step", "dwell_s", "pauses_before",
          "expected_s", "model_area_mm2")
_ASF_RANGE = "ASF#: enter the pendant value as a whole number from 1 to 1000"
_MOVES = frozenset(("MOVJ", "MOVL", "MOVC", "MOVS", "SMOVL", "SMOVC", "IMOV"))
_ENTRY = re.compile(r"(\d{1,4})(?:\s*(-)\s*(\d{1,4})?)?\s*:\s*(\d{1,4})")
MAX_SCHEDULE_CHARS = 400


def parse_arc_schedule(text) -> tuple[tuple[int, int | None, int], ...]:

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Arc schedule: write LAYERS:ASF entries, e.g. 0:81;1-:83")
    if len(text) > MAX_SCHEDULE_CHARS:
        raise ValueError(f"Arc schedule: at most {MAX_SCHEDULE_CHARS} characters")
    entries = []
    for part in text.strip().strip(";").split(";"):
        m = _ENTRY.fullmatch(part.strip())
        if not m:
            raise ValueError(f"Arc schedule entry {part.strip()!r}: write LAYERS:ASF, "
                             "e.g. 0:81, 1-4:83 or 5-:85 (layers count from 0)")
        first = int(m[1])
        last = first if not m[2] else (int(m[3]) if m[3] else None)
        asf = int(m[4])
        if last is not None and last < first:
            raise ValueError(f"Arc schedule entry {part.strip()!r}: the first layer is above the last")
        if not 1 <= asf <= 1000:
            raise ValueError(_ASF_RANGE)
        entries.append((first, last, asf))
    entries.sort(key=lambda e: e[0])
    for (_, l0, _), (f1, _, _) in zip(entries, entries[1:]):
        if l0 is None or f1 <= l0:
            raise ValueError(f"Arc schedule: layer {f1} is covered twice; "
                             "every layer needs exactly one ASF#")
    return tuple(entries)


def resolve_arc_schedule(entries, toolpath: Toolpath) -> dict[int, int]:

    layers = sorted({m.layer for m in toolpath.moves})
    if not layers:
        raise ValueError("Arc schedule: the part has no layers")
    top = layers[-1]
    out: dict[int, int] = {}
    for first, last, asf in entries:
        end = top if last is None else last
        if first > top:
            raise ValueError(f"Arc schedule entry {first}{'-' if last is None else ''}:{asf} starts above "
                             f"the part's top layer {top}; the part has layers {layers[0]}-{top}")
        if end > top:
            raise ValueError(f"Arc schedule names layer {end}, but the part has layers {layers[0]}-{top}. "
                             f"Write {first}- to run to the last layer")
        out.update({n: asf for n in range(first, end + 1)})
    missing = [n for n in layers if n not in out]
    if missing:
        raise ValueError(f"Arc schedule leaves layer {missing[0]} without an ASF#; "
                         f"cover every layer {layers[0]}-{top} exactly once")
    return {n: out[n] for n in layers}


def narrow_arc(arc_file, toolpath: Toolpath):

    if not isinstance(arc_file, Mapping):
        return arc_file
    emitted = {m.layer for m in toolpath.moves}
    return {n: asf for n, asf in arc_file.items() if n in emitted}


def arc_numbers(arc_file) -> list[int]:

    return sorted(set(arc_file.values())) if isinstance(arc_file, Mapping) else [arc_file]


def normalize_arc(arc_file):

    if isinstance(arc_file, Mapping):
        numbers = arc_numbers(arc_file)
        if len(numbers) == 1:
            return numbers[0]
        return dict(arc_file)
    return arc_file


def asf_label(arc_file) -> str:
    return ",".join(str(n) for n in arc_numbers(arc_file))


def job_passes(text: str) -> list[dict]:

    step, dwell, pauses, v, cur, out = 0, 0.0, 0, None, None, []
    lines = iter(text.splitlines())
    for raw in lines:
        if raw.strip() == "NOP":
            break
    for raw in lines:
        line = raw.strip()
        word = line.split(" ", 1)[0]
        if word in _MOVES:
            step += 1
            m = re.search(r"\bV=([0-9.]+)", line)
            v = m[1] if m else v
            if cur is not None:
                cur["steps"].append(step)
                cur["v"].append(v)
        elif word == "TIMER" and cur is None:
            m = re.search(r"\bT=([0-9.]+)", line)
            dwell += float(m[1]) if m else 0.0
        elif word == "PAUSE" and cur is None:
            pauses += 1
        elif word == "ARCON":
            m = re.search(r"ASF#\((\d+)\)", line)
            cur = {"asf": int(m[1]) if m else None, "dwell_s": round(dwell, 2),
                   "pauses_before": pauses, "steps": [], "v": []}
            dwell, pauses = 0.0, 0
        elif word == "ARCOF" and cur is not None:
            m = re.search(r"AEF#\((\d+)\)", line)
            cur["aef"] = int(m[1]) if m else None
            out.append(cur)
            cur = None
        elif word == "END":
            break
    return out


def toolpath_passes(toolpath: Toolpath) -> list[dict]:

    moves, out, per_layer = toolpath.moves, [], {}
    ons = [i for kind, i in toolpath.arc_events() if kind == "ARCON"]
    for on in ons:
        off = next((j for j in range(on, len(moves)) if not moves[j].deposit), len(moves))
        start = moves[on - 1].target if on else moves[on].target
        segs = []
        for m in moves[on:off]:
            segs.append((math.dist(start, m.target), m.feed))
            start = m.target
        layer = moves[on].layer
        per_layer[layer] = per_layer.get(layer, 0) + 1
        out.append({"layer": layer, "bead": per_layer[layer], "region": moves[on].region or "",
                    "segments": segs})
    return out


def _token_bounds(tokens) -> tuple[float | None, float | None]:
    values = [float(t) for t in tokens if t is not None]
    return (min(values), max(values)) if values else (None, None)


def pass_table(toolpath: Toolpath | None, *, text: str | None = None,
               v_unit_mm_per_min: float, weld_v: str | None = None,
               arc_file=None, model: tuple[float, float] | None = None) -> list[dict]:


    from .backends.jbi import v_speed_mm_min, v_token

    unit = v_unit_mm_per_min
    jps = job_passes(text) if text is not None else None
    tps = toolpath_passes(toolpath) if toolpath is not None else None
    if jps is not None and tps is not None and len(jps) != len(tps):
        raise ValueError(f"pass table: the job has {len(jps)} ARCON blocks but the "
                         f"toolpath has {len(tps)} passes")
    count = len(jps) if jps is not None else len(tps or [])
    single = arc_file is None or not isinstance(arc_file, Mapping) or len(arc_numbers(arc_file)) == 1
    rows = []
    for k in range(count):
        jp = jps[k] if jps is not None else None
        tp = tps[k] if tps is not None else None
        tokens = jp["v"] if jp else [v_token(f, unit) if f is not None else weld_v
                                      for _, f in tp["segments"]]
        fallback = next((t for t in tokens if t is not None), weld_v)
        length = time = None
        if tp is not None:
            length = time = 0.0
            for seg, feed in tp["segments"]:
                tok = v_token(feed, unit) if feed is not None else fallback
                speed = v_speed_mm_min(tok, unit) if tok is not None else None
                length += seg
                time = None if (time is None or not speed) else time + seg / speed
        if length and time:
            travel = length / time
        else:
            speeds = [v_speed_mm_min(t, unit) for t in tokens if t is not None]
            travel = sum(speeds) / len(speeds) if speeds else None
        if tp is not None:
            layer, bead, region = tp["layer"], tp["bead"], tp["region"]
        else:
            layer = bead = None
            region = ""
        asf = jp["asf"] if jp else None
        area = None
        if model is not None and single and travel:
            try:
                area = round(deposited_area_mm2(model[0], model[1], travel), 3)
            except ValueError:
                area = None
        rows.append({
            "pass": k + 1, "layer": layer, "bead": bead, "region": region,
            "asf": asf, "aef": jp.get("aef") if jp else None,
            "v_min_mm_s": _token_bounds(tokens)[0], "v_max_mm_s": _token_bounds(tokens)[1],
            "travel_mm_min": None if travel is None else round(travel, 1),
            "length_mm": None if length is None else round(length, 2),
            "weld_moves": len(jp["steps"]) if jp else len(tp["segments"]),
            "first_step": jp["steps"][0] if jp and jp["steps"] else None,
            "last_step": jp["steps"][-1] if jp and jp["steps"] else None,
            "dwell_s": jp["dwell_s"] if jp else None,
            "pauses_before": jp["pauses_before"] if jp else None,
            "expected_s": round(time * 60.0, 2) if time else None,
            "model_area_mm2": area,
        })
    return rows


def digest(rows: list[dict]) -> dict:

    travel = [r["travel_mm_min"] for r in rows if r["travel_mm_min"] is not None]
    return {"count": len(rows),
            "asf": sorted({r["asf"] for r in rows if r["asf"] is not None}),
            "travel_mm_min": [min(travel), max(travel)] if travel else None}


def to_csv(rows: list[dict], fields=FIELDS) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def to_json(rows: list[dict], **meta) -> bytes:
    return (json.dumps({"schema": "waam-passes/1", **meta, "fields": list(FIELDS), "passes": rows},
                       indent=2, allow_nan=False) + "\n").encode("utf-8")
