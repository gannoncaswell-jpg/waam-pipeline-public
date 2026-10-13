from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class JbiJob:
    name: str = ""
    npos: tuple[int, ...] = ()
    positions: dict[str, str] = field(default_factory=dict)
    date: str = ""
    attr: str = ""
    groups: list[str] = field(default_factory=list)
    pos_type: str = ""
    folder: str = ""
    header_lines: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)

    @property
    def uses_station_axis(self) -> bool:
        return any(g.startswith("ST") for g in self.groups)


def read_jbi(source: str | Path) -> JbiJob:

    text = source
    if isinstance(source, Path) or (
        isinstance(source, str) and "\n" not in source and source.upper().endswith(".JBI")
    ):
        text = Path(source).read_text(encoding="utf-8", errors="replace")

    job = JbiJob()
    in_instructions = False
    for raw in str(text).splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            continue
        if not in_instructions and stripped == "NOP":
            in_instructions = True
        if in_instructions:
            job.instructions.append(stripped)
            if stripped == "END":
                break
            continue

        job.header_lines.append(line)
        if stripped.startswith("//NAME"):
            job.name = stripped.split(None, 1)[1] if " " in stripped else ""
        elif stripped.startswith("///NPOS"):
            job.npos = tuple(int(v) for v in stripped.split(None, 1)[1].split(","))
        elif stripped.startswith("///DATE"):
            job.date = stripped.split(None, 1)[1] if " " in stripped else ""
        elif stripped.startswith("///ATTR"):
            job.attr = stripped.split(None, 1)[1] if " " in stripped else ""
        elif stripped.startswith("///POSTYPE"):
            job.pos_type = stripped.split(None, 1)[1] if " " in stripped else ""
        elif stripped.startswith("///FOLDERNAME"):
            job.folder = stripped.split(None, 1)[1] if " " in stripped else ""
        elif re.match(r"///GROUP\d", stripped):
            job.groups.extend(g.strip() for g in stripped.split(None, 1)[1].split(","))
        elif (m := re.match(r"([A-Z]{1,3}\d{4,6})=(.*)", stripped)) is not None:
            job.positions[m.group(1)] = m.group(2)
    if not job.instructions or job.instructions[-1] != "END":
        raise ValueError(f"JBI job {job.name!r}: no NOP..END instruction block found")
    return job
