from __future__ import annotations

from pathlib import Path
import re

_TYPED = re.compile(r'^"(?P<type>[^"]*)"(?P<rest>.*)$')
_PULSE_FIELDS = 14
_AXES = slice(6, 12)


SENSE_BASE = 120
FLAG_BASE = 80
MAX_POINTS = 80


def _read_text(source) -> str:

    if isinstance(source, Path):
        return source.read_text(encoding="latin-1")
    if isinstance(source, str) and "\n" not in source and "\r" not in source:
        p = Path(source)
        if p.exists():
            return p.read_text(encoding="latin-1")
    if not isinstance(source, str):
        raise TypeError("var_dat source must be a path or the file text")
    return source


def _position_lines(text: str) -> list[str]:

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "///P":
            start = i + 1
            break
    if start is None:
        raise ValueError("VAR.DAT: no ///P position-variable section found")
    out: list[str] = []
    for ln in lines[start:]:
        if ln.startswith("///"):
            break
        out.append(ln)
    return out


def parse_position_pulses(source) -> dict[int, list[int]]:


    pulses: dict[int, list[int]] = {}
    for idx, raw in enumerate(_position_lines(_read_text(source))):
        ln = raw.strip()
        if not ln:
            continue
        m = _TYPED.match(ln)
        if not m:
            raise ValueError(f"VAR.DAT: P{idx:03d}: unrecognised record {raw!r}")
        kind = m.group("type")
        if kind == "UNUSED":
            continue
        if kind != "PULSE":
            continue
        fields = m.group("rest").split(",")
        if len(fields) != _PULSE_FIELDS:
            raise ValueError(
                f"VAR.DAT: P{idx:03d}: expected {_PULSE_FIELDS} PULSE fields, got "
                f"{len(fields)} -- the parser is pinned to the AR1440 R1 export "
                "(see docs/cell-data/var-exports); re-verify before trusting it")
        try:
            pulses[idx] = [int(v) for v in fields[_AXES]]
        except ValueError:
            raise ValueError(f"VAR.DAT: P{idx:03d}: non-integer PULSE axis in {raw!r}") from None
    return pulses


def probe_records(source, base: int, count: int) -> list[list[int]]:


    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise ValueError("base must be a non-negative P-variable index")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive number of probes")
    pulses = parse_position_pulses(source)
    out: list[list[int]] = []
    for i in range(base, base + count):
        if i not in pulses:
            raise ValueError(
                f"VAR.DAT: P{i:03d} is not a usable PULSE position variable "
                "(unused, a Cartesian frame, or beyond the exported range)")
        out.append(pulses[i])
    return out


def _byte_values(text: str) -> list[int]:

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "///B") + 1
    except StopIteration:
        raise ValueError("VAR.DAT: no ///B byte-variable section found") from None
    values: list[int] = []
    for ln in lines[start:]:
        if ln.startswith("///"):
            break
        if not ln.strip():
            continue
        try:
            values += [int(v) for v in ln.split(",")]
        except ValueError:
            raise ValueError(f"VAR.DAT: B{len(values):03d}: non-integer byte value in {ln!r}") from None
    return values


def probe_hits(source, base: int, count: int, *, flag_base: int) -> tuple[list[list[int] | None], list[int]]:


    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise ValueError("base must be a non-negative variable index")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive number of probes")
    if not isinstance(flag_base, int) or isinstance(flag_base, bool) or flag_base < 0:
        raise ValueError("flag_base must be a non-negative B-variable index")
    text = _read_text(source)
    values = _byte_values(text)
    if flag_base + count > len(values):
        raise ValueError(f"VAR.DAT: B{flag_base + count - 1:03d} is beyond the exported ///B section")
    flags = values[flag_base:flag_base + count]
    for i, flag in enumerate(flags):
        if flag not in (0, 1):
            raise ValueError(f"VAR.DAT: B{flag_base + i:03d} = {flag} is not a touch flag (0 or 1)")
    pulses = parse_position_pulses(text)
    records: list[list[int] | None] = []
    for i, flag in enumerate(flags):
        if not flag:
            records.append(None)
        elif base + i not in pulses:
            raise ValueError(f"VAR.DAT: P{base + i:03d} is flagged touched but is not a usable PULSE position variable")
        else:
            records.append(pulses[base + i])
    return records, flags
