from __future__ import annotations

from array import array
import csv
import datetime as dt
import io
import math
from pathlib import Path
import re
import statistics

MAX_ROWS = 100_000


MAX_HEADER_ROWS = 256
MAX_FIELDS = 64
MAX_LINE_CHARS = 4096
SAMPLING_RANGE_S = (1e-4, 10.0)


_LIMITS = {"current": (-1e4, 1e4), "voltage": (-1e3, 1e3), "length": (0.0, 1e7),
           "step": (0.0, 1e6), "cond": (0.0, 1e6), "speed": (-1e5, 1e5)}
_ANY_LIMIT = 1e12
_NOT = "not an arc monitor export"
_EXPECTED = f"{_NOT}: expected a real-time arc monitor .CSV (//ARC_MONDATA) or ARCMON.DAT"
ARC_ON_A = 10.0


MERGE_GAP_S = 0.3
LOW_CURRENT_FRACTION = 0.7
SHORT_ARC_FRACTION = 0.5
SPEED_TOLERANCE = 0.10
PASS_FIELDS = ("pass", "layer", "asf", "first_step", "last_step", "matched_by", "arc_on_s",
               "weld_s", "current_mean_a", "current_min_a", "current_max_a", "voltage_mean_v",
               "voltage_min_v", "voltage_max_v", "cond_no", "aef_no", "weld_speed_mm_min", "weld_length_mm",
               "travel_mm_min", "energy_kj_mm", "arc_current_mean_a", "arc_current_min_a",
               "arc_current_max_a", "arc_voltage_mean_v", "arc_voltage_min_v", "arc_voltage_max_v",
               "dropouts", "flags")
_SPEED_UNITS = {"cm/min": 10.0, "mm/min": 1.0, "mm/s": 60.0, "in/min": 25.4}
_LENGTH_UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4}

MIN_HEAT_SPEED_MM_MIN = 1.0


def _number(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(_EXPECTED) from exc
    if not math.isfinite(result):
        raise ValueError(_EXPECTED)
    return result


def _stats(values: list[float]) -> dict:
    return {"mean": statistics.fmean(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None}


def _summary(meta: dict, rows: list) -> dict:
    current_index, voltage_index = meta["current_index"], meta["voltage_index"]
    currents, voltages = [], []
    for values in rows:
        if current_index < len(values) and values[current_index] > ARC_ON_A:
            currents.append(values[current_index])
            if voltage_index < len(values):
                voltages.append(values[voltage_index])
    shown = {key: value for key, value in meta.items() if key not in ("current_index", "voltage_index", "header")}
    return {"kind": "realtime-csv", **shown, "samples": len(rows),
            "arc_on_s": len(currents) * meta["sampling_s"],
            "current": _stats(currents), "voltage": _stats(voltages)}


def _is_realtime(text: str) -> bool:

    return text[:64].splitlines()[:1] == ["//ARC_MONDATA"]


def _lines(text: str):

    start, size = 0, len(text)
    while start < size:
        end = text.find("\n", start, start + MAX_LINE_CHARS + 1)
        if end < 0:
            if size - start > MAX_LINE_CHARS:
                raise ValueError(f"{_NOT}: a line is longer than {MAX_LINE_CHARS} characters")
            end = size - 1
        yield text[start:end + 1]
        start = end + 1


def _values(row: list[str], header: list[str], limits: list, last: bool, number: int) -> array:
    if last:
        while row and not row[-1].strip():
            row.pop()
    if len(row) > len(header) or (len(row) < len(header) and not last):
        raise ValueError(_EXPECTED)
    values = array("d", [_number(value.strip()) for value in row])
    for index, low, high, label in limits:
        if index < len(values) and not low <= values[index] <= high:
            raise ValueError(f"{_NOT}: {label} reading {values[index]:g} in data row {number} "
                             f"is outside {low:g}..{high:g}")
    return values


def _realtime_rows(text: str) -> tuple[dict, list[array]]:
    rows = (row for row in csv.reader(_lines(text)) if any(x.strip() for x in row))
    channels = []
    date = None
    job = None
    sampling = None
    for count, row in enumerate(rows, 1):
        if count > MAX_HEADER_ROWS or len(row) > MAX_FIELDS:
            raise ValueError(f"{_NOT}: more than {MAX_HEADER_ROWS} rows before //DATA "
                             f"or {MAX_FIELDS} fields in a row")
        tag = row[0].strip()
        if tag == "//DATE" and len(row) > 1:
            date = row[1].strip().lstrip(":")
        elif tag == "//JOB" and len(row) > 1:
            job = row[1].strip().lstrip(":").strip("*") or None
        elif tag == "//SAMPLING" and len(row) > 1:
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)sec", row[1].strip())
            if not match:
                raise ValueError(_EXPECTED)
            sampling = _number(match[1])
        elif re.fullmatch(r"//CH[0-9]+", tag):
            if len(row) < 7:
                raise ValueError(_EXPECTED)
            channels.append({"channel": tag[2:], "attribute": row[1].strip(),
                             "name": row[2].strip(), "unit": row[6].strip()})
        elif tag == "//DATA":
            break
    else:
        raise ValueError(_EXPECTED)
    if not date or sampling is None:
        raise ValueError(_EXPECTED)
    low, high = SAMPLING_RANGE_S
    if not low <= sampling <= high:
        raise ValueError(f"{_NOT}: sampling {sampling:g} s is outside {low:g}..{high:g} s")
    try:
        dt.datetime.strptime(date, "%Y/%m/%d %H:%M:%S")
        header = [item.strip() for item in next(rows)]
        current_ch = next(c["channel"] for c in channels
                          if c["name"] == "CURRENT" and re.fullmatch(r"AIN[0-9]+", c["attribute"]))
        voltage_ch = next(c["channel"] for c in channels
                          if c["name"] == "VOLTAGE" and re.fullmatch(r"AIN[0-9]+", c["attribute"]))
        current_index, voltage_index = header.index(current_ch), header.index(voltage_ch)
    except (ValueError, IndexError, StopIteration) as exc:
        raise ValueError(_EXPECTED) from exc
    if len(header) > MAX_FIELDS or len(set(header)) != len(header) \
            or set(header) != {c["channel"] for c in channels}:
        raise ValueError(_EXPECTED)
    meta = {"date": date, "sampling_s": sampling, "channels": channels, "header": header,
            "current_index": current_index, "voltage_index": voltage_index}
    if job:
        meta["job"] = job
    limited = {"current": current_index, "voltage": voltage_index, "length": _channel(meta, "LEN", "WELD LENGTH"),
               "step": _channel(meta, "STEP", "STEP NO."), "cond": _channel(meta, "FILE", "WELD COND.NO"),
               "speed": _channel(meta, "SPD", "WELD SPEED")}
    names = {c["channel"]: c["name"] for c in channels}
    labels = [f"{names[channel]} ({channel})" for channel in header]
    limits = [(index, *_LIMITS[key], labels[index]) for key, index in limited.items() if index is not None]
    limits += [(index, -_ANY_LIMIT, _ANY_LIMIT, labels[index]) for index in range(len(header))]
    values_rows: list[array] = []
    pending, ended = None, False
    for row in rows:
        if ended or len(row) > len(header):
            raise ValueError(_EXPECTED)
        if row == ["//ARC_MONDATAEND"]:
            ended = True
            continue
        if pending is not None:
            values_rows.append(_values(pending, header, limits, False, len(values_rows) + 1))
        if len(values_rows) >= MAX_ROWS:
            raise ValueError("arc monitor row limit: 100000 rows")
        pending = row
    if pending is not None:
        values_rows.append(_values(pending, header, limits, True, len(values_rows) + 1))
    return meta, values_rows


def _dat(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) % 3:
        raise ValueError(_EXPECTED)
    blocks = []
    seen = set()
    for index in range(0, len(lines), 3):
        match = re.fullmatch(r"//ARCMON ([0-9]+)", lines[index])
        if not match or int(match[1]) in seen:
            raise ValueError(_EXPECTED)
        number = int(match[1])
        seen.add(number)
        values = [[_number(value.strip()) for value in line.split(",")]
                  for line in lines[index + 1:index + 3]]
        if len(values[0]) != 3 or len(values[1]) != 6:
            raise ValueError(_EXPECTED)
        if any(value != 0 for row in values for value in row):
            blocks.append({"file": number, "values": values})
    return {"kind": "arcmon-dat", "blocks": blocks}


def parse(name: str, body: bytes) -> dict:

    try:
        text = body.decode("utf-8-sig").strip()
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ValueError(_EXPECTED) from exc
    if Path(name).suffix.lower() == ".csv" and _is_realtime(text):
        try:
            return _summary(*_realtime_rows(text))
        except (csv.Error, OverflowError) as exc:
            raise ValueError(_EXPECTED) from exc
    if name.upper() == "ARCMON.DAT":
        return _dat(text)
    raise ValueError(_EXPECTED)


def _rows_of(body: bytes) -> tuple[dict, list[array]]:
    try:
        text = body.decode("utf-8-sig").strip()
        if not _is_realtime(text):
            raise ValueError(_EXPECTED)
        return _realtime_rows(text)
    except (AttributeError, UnicodeDecodeError, csv.Error, OverflowError) as exc:
        raise ValueError(_EXPECTED) from exc


def read_realtime(body: bytes) -> tuple[dict, dict, list[array]]:

    meta, rows = _rows_of(body)
    return _summary(meta, rows), meta, rows


def unit_scale(meta: dict, index: int | None, units: dict) -> float | None:

    if index is None:
        return None
    channel = meta["header"][index]
    return next((units.get(c["unit"]) for c in meta["channels"] if c["channel"] == channel), None)


def _channel(meta: dict, attribute: str, name: str) -> int | None:
    for c in meta["channels"]:
        if (c["attribute"] == attribute or c["name"] == name) and c["channel"] in meta["header"]:
            return meta["header"].index(c["channel"])
    return None


def _segments(on: list[bool], gap: int) -> list[dict]:

    out: list[dict] = []
    for i, flag in enumerate(on):
        if not flag:
            continue
        if out and i == out[-1]["end"]:
            out[-1]["end"] = i + 1
        elif out and i - out[-1]["end"] <= gap:
            out[-1]["end"] = i + 1
            out[-1]["dropouts"] += 1
        else:
            out.append({"start": i, "end": i + 1, "dropouts": 0})
    return out


def _cond_numbers(values) -> tuple[int | None, int | None]:


    asf = [int(v) for v in values if 0 < v < 1000]
    aef = [int(v) - 1000 for v in values if v > 1000]
    return (statistics.mode(asf) if asf else None, statistics.mode(aef) if aef else None)


class _Trace:


    def __init__(self, meta: dict, rows: list):
        self.dt = meta["sampling_s"]
        index = {"current": meta["current_index"], "voltage": meta["voltage_index"],
                 "step": _channel(meta, "STEP", "STEP NO."), "speed": _channel(meta, "SPD", "WELD SPEED"),
                 "cond": _channel(meta, "FILE", "WELD COND.NO"), "length": _channel(meta, "LEN", "WELD LENGTH")}
        self.has_step = index["step"] is not None
        self.speed_unit = unit_scale(meta, index["speed"], _SPEED_UNITS)
        self.length_unit = unit_scale(meta, index["length"], _LENGTH_UNITS)

        def column(key):
            i = index[key]
            return [row[i] if i is not None and i < len(row) else None for row in rows]

        self.current, self.voltage, self.steps, self.speeds, self.conds, self.lengths = (
            column(k) for k in ("current", "voltage", "step", "speed", "cond", "length"))
        self.on = [(c or 0.0) > ARC_ON_A for c in self.current]
        self.segments = _segments(self.on, max(1, round(MERGE_GAP_S / self.dt)))

    def welding(self, i: int) -> bool:

        if self.has_step:
            return bool(self.steps[i])
        return self.conds[i] is None or self.conds[i] < 1000

    def figures(self, arc: list[int], weld: list[int]) -> dict:

        def values(series, samples):
            return [v for v in (series[i] for i in samples) if v is not None]

        amps, volts = _stats(values(self.current, weld)), _stats(values(self.voltage, weld))
        arc_amps, arc_volts = _stats(values(self.current, arc)), _stats(values(self.voltage, arc))
        cond, _ = _cond_numbers(values(self.conds, weld))
        _, aef = _cond_numbers(values(self.conds, arc))
        speeds, lengths = values(self.speeds, weld), values(self.lengths, arc)
        return {"arc_on_s": round(len(arc) * self.dt, 3), "weld_s": round(len(weld) * self.dt, 3),
                "current_mean_a": amps["mean"], "current_min_a": amps["min"],
                "current_max_a": amps["max"], "voltage_mean_v": volts["mean"],
                "voltage_min_v": volts["min"], "voltage_max_v": volts["max"],
                "cond_no": cond, "aef_no": aef,
                "weld_speed_mm_min": statistics.median(speeds) * self.speed_unit
                if speeds and self.speed_unit else None,
                "weld_length_mm": max(lengths) * self.length_unit if lengths and self.length_unit else None,
                "arc_current_mean_a": arc_amps["mean"], "arc_current_min_a": arc_amps["min"],
                "arc_current_max_a": arc_amps["max"], "arc_voltage_mean_v": arc_volts["mean"],
                "arc_voltage_min_v": arc_volts["min"], "arc_voltage_max_v": arc_volts["max"]}


def per_segment(meta: dict, rows: list) -> list[dict]:


    trace = _Trace(meta, rows)
    out = []
    for number, seg in enumerate(trace.segments, 1):
        arc = [i for i in range(seg["start"], seg["end"]) if trace.on[i]]
        weld = [i for i in arc if trace.welding(i)]
        figures = trace.figures(arc, weld)
        steps = [int(trace.steps[i]) for i in weld if trace.steps[i]]
        crater = [trace.current[i] for i in arc if (trace.conds[i] or 0) > 1000]
        speed, amps, volts = figures["weld_speed_mm_min"], figures["current_mean_a"], figures["voltage_mean_v"]
        heat = None
        if speed is not None and speed >= MIN_HEAT_SPEED_MM_MIN and amps is not None and volts is not None:
            heat = volts * amps / (speed / 60.0) / 1000.0
        out.append({"segment": number, "start_s": seg["start"] * trace.dt, "end_s": seg["end"] * trace.dt,
                    "first_step": min(steps, default=None), "last_step": max(steps, default=None),
                    "dropouts": seg["dropouts"], **figures,
                    "crater_current_mean_a": statistics.fmean(crater) if crater else None,
                    "heat_input_kj_mm": heat})
    return out


def per_pass(body: bytes, passes: list[dict]) -> dict:


    meta, rows = _rows_of(body)
    trace = _Trace(meta, rows)
    dt_s, on, steps, segments = trace.dt, trace.on, trace.steps, trace.segments
    ranges = [(p.get("first_step"), p.get("last_step")) for p in passes]
    step_pass: dict[int, int] = {}
    for k, (a, b) in enumerate(ranges):
        if a is not None and b is not None and 0 <= int(b) - int(a) <= MAX_ROWS:
            for st in range(int(a), int(b) + 1):
                step_pass.setdefault(st, k)

    def pass_of(i):
        st = steps[i]
        return step_pass.get(int(st)) if st else None

    warnings: list[str] = []
    owner: dict[int, int] = {}
    mode = "order"
    if trace.has_step and step_pass:
        for s, seg in enumerate(segments):
            votes: dict[int, int] = {}
            for i in range(seg["start"], seg["end"]):
                k = pass_of(i) if on[i] else None
                if k is not None:
                    votes[k] = votes.get(k, 0) + 1
            if votes:
                owner[s] = max(votes, key=lambda k: (votes[k], -k))
        if owner:
            mode = "step"
            stray = len(segments) - len(owner)
            if stray:
                warnings.append(f"{stray} arc-on stretch(es) carry no step of this job's passes")
        elif segments:
            warnings.append("the monitor's STEP NO. readings match none of this job's passes "
                            "(is this the right job?); matched arc-on stretches to passes in order")
    if mode == "order":
        owner = {s: s for s in range(min(len(segments), len(passes)))}
        if len(segments) != len(passes):
            warnings.append(f"{len(segments)} arc-on stretch(es) for {len(passes)} passes: "
                            "matched in order, check the job and the monitor window")
    seen = {step_pass[int(st)] for st in steps if st and int(st) in step_pass} if mode == "step" else set()
    by_pass: dict[int, list[int]] = {}
    for s, k in owner.items():
        by_pass.setdefault(k, []).append(s)

    out = []
    has_weld = False
    for k, p in enumerate(passes):
        mine = by_pass.get(k, [])
        arc = [i for s in mine for i in range(segments[s]["start"], segments[s]["end"]) if on[i]]
        if mode == "step":
            weld = [i for i in arc if pass_of(i) == k]
        else:
            weld = [i for i in arc if trace.welding(i)]
        has_weld = has_weld or bool(weld)
        flags = []
        if not arc:
            flags.append("no arc" if k in seen else "not recorded")
        dropouts = sum(segments[s]["dropouts"] for s in mine) + max(0, len(mine) - 1)
        if dropouts:
            flags.append("dropout")
        figures = trace.figures(arc, weld)
        amps, volts = figures["current_mean_a"], figures["voltage_mean_v"]
        cond, speed = figures["cond_no"], figures["weld_speed_mm_min"]
        travel = p.get("travel_mm_min")
        energy = None
        if amps and volts and travel:
            energy = volts * amps / (travel / 60.0) / 1000.0
        if p.get("asf") is not None and cond is not None and cond != p["asf"]:
            flags.append("ASF mismatch")
        if p.get("expected_s") and arc and len(weld) * dt_s < SHORT_ARC_FRACTION * p["expected_s"]:
            flags.append("short arc")
        if speed and travel and abs(speed - travel) > SPEED_TOLERANCE * travel:
            flags.append("speed off")
        out.append({"pass": p.get("pass", k + 1), "layer": p.get("layer"), "asf": p.get("asf"),
                    "first_step": p.get("first_step"), "last_step": p.get("last_step"),
                    "matched_by": mode if arc else "", **figures, "travel_mm_min": travel,
                    "energy_kj_mm": None if energy is None else round(energy, 4),
                    "dropouts": dropouts, "flags": flags})
    if any(row["arc_on_s"] for row in out) and not has_weld:
        warnings.append("no welding samples in this file (strike/crater only)")
    means = [r["current_mean_a"] for r in out if r["current_mean_a"]]
    if len(means) >= 2:
        median = statistics.median(means)
        for r in out:
            if r["current_mean_a"] and r["current_mean_a"] < LOW_CURRENT_FRACTION * median:
                r["flags"].append("low current")
    for r in out:
        r["flags"] = ";".join(r["flags"])
    return {"mode": mode, "job": meta.get("job"), "segments": len(segments),
            "rows": out, "warnings": warnings}


def passes_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=PASS_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
