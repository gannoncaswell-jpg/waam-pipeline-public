from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from statistics import fmean
from typing import NamedTuple

from . import arcparams

PRESETS_PATH = Path(__file__).resolve().parents[2] / "docs" / "bead-presets.json"
FORMAT_VERSION = 1
PROVENANCE = ("NOMINAL", "MEASURED")
FAMILIES = ("STT", "CV", "Pulse", "Rapid X")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

WIRE_RANGE_IN = (0.020, 0.0625)

TRAVEL_TOL_MM_MIN = 0.5
WFS_TOL_IPM = 0.5


def mass_balance_area_mm2(wire_dia_in: float, wfs_ipm: float, travel_mm_min: float) -> float:

    return arcparams.deposited_area_mm2(wire_dia_in * arcparams.INCH, wfs_ipm * arcparams.INCH / 1000.0,
                                        travel_mm_min)


@dataclass(frozen=True)
class BeadPreset:
    id: str
    label: str
    material: str
    wire_dia_in: float
    gas: str
    asf: int
    r450_mode: int
    process: str
    wfs_ipm: float
    travel_mm_min: float
    weld_ctwd_mm: float
    bead_width_mm: float
    bead_height_mm: float
    crown_height_mm: float
    provenance: str
    source: str

    def __post_init__(self):
        where = f"bead preset {self.id!r}"
        for name in ("id", "label", "material", "gas", "process", "provenance", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}: {name} must be a non-empty string")
        if not _ID_RE.fullmatch(self.id):
            raise ValueError(f"{where}: id must be lower-case letters, digits and -")
        for name in ("asf", "r450_mode"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{where}: {name} must be a whole number")
        if not 1 <= self.asf <= 1000:
            raise ValueError(f"{where}: asf must be 1 to 1000, the pendant's ASF# range")
        if self.r450_mode < 0:
            raise ValueError(f"{where}: r450_mode must not be negative")
        for name in ("wire_dia_in", "wfs_ipm", "travel_mm_min", "weld_ctwd_mm",
                     "bead_width_mm", "bead_height_mm", "crown_height_mm"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{where}: {name} must be a finite positive number")
        if not WIRE_RANGE_IN[0] <= self.wire_dia_in <= WIRE_RANGE_IN[1]:
            raise ValueError(f"{where}: wire_dia_in is inches, {WIRE_RANGE_IN[0]:g} to "
                             f"{WIRE_RANGE_IN[1]:g} (0.045, not 1.143 mm)")
        if self.process not in FAMILIES:
            raise ValueError(f"{where}: process must be one of {', '.join(FAMILIES)}")
        if self.provenance not in PROVENANCE:
            raise ValueError(f"{where}: provenance must be NOMINAL or MEASURED")
        if self.bead_height_mm > self.crown_height_mm * (1 + 1e-9):

            raise ValueError(f"{where}: bead_height_mm is the layer pitch and cannot exceed the crown")

    @property
    def wire_dia_mm(self) -> float:
        return self.wire_dia_in * arcparams.INCH

    @property
    def deposited_area_mm2(self) -> float:
        return mass_balance_area_mm2(self.wire_dia_in, self.wfs_ipm, self.travel_mm_min)

    @classmethod
    def from_dict(cls, raw: dict) -> "BeadPreset":
        if not isinstance(raw, dict):
            raise ValueError("each bead preset must be a JSON object")
        names = {f.name for f in fields(cls)}
        missing, extra = names - raw.keys(), raw.keys() - names
        if missing or extra:
            raise ValueError(f"bead preset {raw.get('id')!r}: missing {sorted(missing)}, "
                             f"unknown {sorted(extra)}")
        return cls(**raw)

    def to_dict(self) -> dict:
        return asdict(self)


_ASF_FIELDS = ("material", "process", "gas", "wire_dia_in", "r450_mode", "wfs_ipm")


def load_presets(path: Path | str = PRESETS_PATH) -> list[BeadPreset]:

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"bead presets {path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int \
            or data["version"] != FORMAT_VERSION or not isinstance(data.get("presets"), list):
        raise ValueError(f"bead presets {path}: expected version {FORMAT_VERSION} with a presets list")
    presets = [BeadPreset.from_dict(raw) for raw in data["presets"]]
    ids = [p.id for p in presets]
    if len(set(ids)) != len(ids):
        raise ValueError(f"bead presets {path}: duplicate id")
    keys = [(p.asf, round(p.travel_mm_min, 3)) for p in presets]
    if len(set(keys)) != len(keys):
        raise ValueError(f"bead presets {path}: two presets share an ASF# and travel")
    by_asf: dict[int, BeadPreset] = {}
    for p in presets:
        first = by_asf.setdefault(p.asf, p)
        clash = [f for f in _ASF_FIELDS if getattr(first, f) != getattr(p, f)]
        if clash:
            raise ValueError(f"bead presets {path}: {first.id!r} and {p.id!r} are both ASF #{p.asf} "
                             f"but differ in {', '.join(clash)}")
    return presets


def dump_presets(presets: list[BeadPreset], path: Path | str, *, comment: str = "") -> None:
    data = {"version": FORMAT_VERSION}
    if comment:
        data["_comment"] = comment
    data["presets"] = [p.to_dict() for p in presets]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


LOG_COLUMNS = ("Run ID", "Material", "Family", "ASF #", "Speed", "Travel mm/min", "WFS in/min",
               "CTWD mm", "Width mm", "Height mm", "Section area mm2", "Keep?")

WIRE_COLUMN = "Wire in"
KEEP_VALUES = ("yes", "y", "true", "x", "1")


@dataclass(frozen=True)
class MeasuredBead:


    asf: int
    travel_mm_min: float
    wfs_ipm: float
    ctwd_mm: float | None
    wire_dia_in: float
    material: str
    process: str
    runs: tuple[str, ...]
    bead_width_mm: float
    crown_height_mm: float
    area_mm2: float
    area_basis: str
    bead_height_mm: float
    transfer_efficiency: float
    source: str
    problem: str = ""


class BeadLogImport(NamedTuple):
    beads: list[MeasuredBead]
    problems: list[str]


class _RowProblem(ValueError):
    pass


def _number(row: dict, column: str, run: str) -> float | None:
    text = (row.get(column) or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise _RowProblem(f"run {run!r}: {column} {text!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise _RowProblem(f"run {run!r}: {column} {text!r} must be positive")
    return value


def _kept_row(row: dict, run: str, wire_by_asf: dict[int, float]) -> dict:
    width = _number(row, "Width mm", run)
    height = _number(row, "Height mm", run)
    if width is None or height is None:
        raise _RowProblem(f"run {run!r}: kept but its width or height is blank")
    asf_text = (row.get("ASF #") or "").strip()
    if not re.fullmatch(r"\d+", asf_text):
        raise _RowProblem(f"run {run!r}: ASF # {asf_text!r} is not a whole number")
    asf = int(asf_text)
    travel = _number(row, "Travel mm/min", run)
    wfs = _number(row, "WFS in/min", run)
    if travel is None or wfs is None:
        raise _RowProblem(f"run {run!r}: a kept bead needs its travel and WFS")
    wire = _number(row, WIRE_COLUMN, run) or wire_by_asf.get(asf)
    if wire is None:
        raise _RowProblem(f"run {run!r}: no wire diameter (add a {WIRE_COLUMN!r} column or a preset "
                          f"for ASF #{asf})")
    if not WIRE_RANGE_IN[0] <= wire <= WIRE_RANGE_IN[1]:
        raise _RowProblem(f"run {run!r}: wire {wire:g} in is outside {WIRE_RANGE_IN[0]:g}-"
                          f"{WIRE_RANGE_IN[1]:g} in")
    section = _number(row, "Section area mm2", run)
    area = section if section is not None else (2.0 / 3.0) * width * height
    return dict(run=run, asf=asf, travel=travel, width=width, height=height, area=area,
                section=section is not None, wfs=wfs, wire=wire, ctwd=_number(row, "CTWD mm", run),
                efficiency=area / mass_balance_area_mm2(wire, wfs, travel),
                material=(row.get("Material") or "").strip(), process=(row.get("Family") or "").strip())


def import_bead_log(csv_path: Path | str, *, presets: list[BeadPreset] | None = None) -> BeadLogImport:


    path = Path(csv_path)
    if presets is None:
        presets = load_presets()
    wire_by_asf = {p.asf: p.wire_dia_in for p in presets}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [(h or "").strip() for h in reader.fieldnames or []]
        missing = [c for c in LOG_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"bead log {path.name}: missing columns {missing}")
        rows = [{(k or "").strip(): v for k, v in row.items()} for row in reader]
    kept, problems = [], []
    for line, row in enumerate(rows, 2):
        if (row.get("Keep?") or "").strip().lower() not in KEEP_VALUES:
            continue
        run = (row.get("Run ID") or "").strip() or f"line {line}"
        try:
            kept.append(_kept_row(row, run, wire_by_asf))
        except _RowProblem as exc:
            problems.append(f"{path.name} {exc}")
    groups: list[list[dict]] = []
    for r in sorted(kept, key=lambda r: (r["asf"], r["travel"])):
        if groups and groups[-1][0]["asf"] == r["asf"] \
                and r["travel"] - groups[-1][0]["travel"] <= TRAVEL_TOL_MM_MIN:
            groups[-1].append(r)
        else:
            groups.append([r])
    beads = []
    for group in groups:
        asf, runs = group[0]["asf"], tuple(g["run"] for g in group)
        travel = fmean(g["travel"] for g in group)
        for key, label in (("wfs", "WFS"), ("wire", "wire diameter"), ("ctwd", "CTWD")):
            if len({g[key] for g in group}) > 1:
                raise ValueError(f"bead log ASF #{asf} at {travel:g} mm/min mixes {label} values "
                                 f"across runs {', '.join(runs)}; split them before importing")
        sections = sum(g["section"] for g in group)
        basis = "section" if sections == len(group) else "parabola" if not sections else "mixed"
        width, area = fmean(g["width"] for g in group), fmean(g["area"] for g in group)
        crown = fmean(g["height"] for g in group)
        problem = ""
        if area / width > crown:
            problem = (f"ASF #{asf} at {travel:g} mm/min: section area {area:.2f} mm2 is more than "
                       f"width x height ({width * crown:.2f} mm2), so its layer pitch would exceed "
                       "the crown. Check that Height is the crown above the plate and that the "
                       "section area excludes penetration below it.")
            problems.append(problem)
        beads.append(MeasuredBead(
            asf=asf, travel_mm_min=travel, wfs_ipm=group[0]["wfs"], ctwd_mm=group[0]["ctwd"],
            wire_dia_in=group[0]["wire"], material=group[0]["material"], process=group[0]["process"],
            runs=runs, bead_width_mm=width, crown_height_mm=crown, area_mm2=area, area_basis=basis,
            bead_height_mm=area / width, transfer_efficiency=fmean(g["efficiency"] for g in group),
            source=f"MEASURED: {path.name} runs {', '.join(runs)} (n={len(group)}, area from {basis})",
            problem=problem))
    return BeadLogImport(beads, problems)


def supersede(current: BeadPreset, new: BeadPreset) -> BeadPreset:

    if current.provenance == "MEASURED" and new.provenance != "MEASURED":
        raise ValueError(f"bead preset {current.id!r}: a NOMINAL bead never replaces a MEASURED one")
    return new


def _same_schedule(preset: BeadPreset, bead: MeasuredBead) -> bool:
    return (bead.asf == preset.asf
            and abs(bead.travel_mm_min - preset.travel_mm_min) <= TRAVEL_TOL_MM_MIN
            and abs(bead.wfs_ipm - preset.wfs_ipm) <= WFS_TOL_IPM
            and math.isclose(bead.wire_dia_in, preset.wire_dia_in))


def apply_measured(preset: BeadPreset, bead: MeasuredBead) -> BeadPreset:


    if not _same_schedule(preset, bead):
        raise ValueError(f"bead preset {preset.id!r}: the measured bead is ASF #{bead.asf} at "
                         f"{bead.travel_mm_min:g} mm/min, {bead.wfs_ipm:g} in/min, "
                         f"{bead.wire_dia_in:g} in wire, not this schedule")
    if bead.problem:
        raise ValueError(f"bead preset {preset.id!r}: {bead.problem}")
    return supersede(preset, replace(
        preset, bead_width_mm=bead.bead_width_mm, bead_height_mm=bead.bead_height_mm,
        crown_height_mm=bead.crown_height_mm, provenance="MEASURED", source=bead.source))


def merge_measured(presets: list[BeadPreset], beads: list[MeasuredBead]) -> list[BeadPreset]:


    out = []
    for preset in presets:
        for bead in beads:
            if _same_schedule(preset, bead) and not bead.problem:
                preset = apply_measured(preset, bead)
        out.append(preset)
    return out
