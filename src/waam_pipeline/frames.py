from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_REQUIRED = (
    "name",
    "robot",
    "tool_number",
    "user_frame",
    "arc_file",
    "between_layer_job",
    "ctwd_mm",
    "safe_z_mm",
)


_EMPTY_PROC_SLOTS = frozenset(range(7, 14))


def _populated_procs() -> str:
    return "1-6, 14-16"


@dataclass(frozen=True)
class CellConfig:
    name: str
    robot: str
    tool_number: int
    user_frame: int
    arc_file: int
    between_layer_job: str
    ctwd_mm: float
    safe_z_mm: float
    weave_file: int | None = None


    proc: int = 4

    def validation_errors(self) -> list[str]:


        e: list[str] = []
        if not 0 <= self.tool_number <= 63:
            e.append(f"tool_number must be 0..63 (TOOL.CND), got {self.tool_number}")
        if not 0 <= self.user_frame <= 63:
            e.append(f"user_frame must be 0..63 (UFRAME.CND), got {self.user_frame}")
        if not 1 <= self.arc_file <= 1000:
            e.append(f"arc_file must be 1..1000 (ASF#/AEF#), got {self.arc_file}")
        if not 1 <= self.proc <= 16:
            e.append(f"proc must be 1..16 (UWI PROC#), got {self.proc}")
        elif self.proc in _EMPTY_PROC_SLOTS:
            e.append(
                f"proc {self.proc} is an EMPTY slot on this cell's process "
                f"table (populated: {_populated_procs()}). An empty slot is "
                "in range and selects no process - photographed 2026-09-03, "
                "docs/pendant-capture-2026-09-03.md"
            )
        if not 5.0 <= self.ctwd_mm <= 40.0:
            e.append(f"ctwd_mm must be 5..40, got {self.ctwd_mm}")
        if self.safe_z_mm <= 0.0:
            e.append(f"safe_z_mm must be > 0 (it is a retreat height), got {self.safe_z_mm}")
        if not self.between_layer_job.strip():
            e.append("between_layer_job must name a pendant job")
        if self.weave_file is not None and not 1 <= self.weave_file <= 1000:
            e.append(f"weave_file must be 1..1000 or null, got {self.weave_file}")
        return e

    @classmethod
    def load(cls, path: str | Path) -> "CellConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        data.pop("vendor", None)
        missing = [k for k in _REQUIRED if k not in data]
        if missing:
            raise ValueError(f"{path}: missing required keys: {', '.join(missing)}")
        cfg = cls(**data)
        bad = cfg.validation_errors()
        if bad:
            raise ValueError(f"{path}: " + "; ".join(bad))
        return cfg


def load_cell_config(path: str | Path) -> CellConfig:

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if str(data.get("vendor", "yaskawa")).lower() == "fanuc":
        raise ValueError(
            "FANUC support was retired; this cell profile is no longer supported."
        )
    return CellConfig.load(path)
