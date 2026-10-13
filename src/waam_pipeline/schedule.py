from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeldParams:
    wfs: float | None = None
    volts: float | None = None
    asf: int | None = None
    dwell_s: float | None = None

    def arcset_line(self, proc: int) -> str | None:
        if self.asf is not None:
            return f"ARCSET ASF#({self.asf})"
        tags = []
        if self.wfs is not None:
            tags.append(f"WFS={self.wfs:.0f}")
        if self.volts is not None:
            tags.append(f"V={self.volts:.1f}")
        if not tags:
            return None
        return f"ARCSET PROC#({proc}) " + " ".join(tags)


class Schedule:


    def __init__(self, entries: list[tuple[int, int, WeldParams]]):
        self.entries = list(entries)

    def for_layer(self, layer: int) -> WeldParams | None:
        found = None
        for lo, hi, params in self.entries:
            if lo <= layer <= hi:
                found = params
        return found

    @staticmethod
    def ramp(
        first_layer: int,
        last_layer: int,
        wfs: float | tuple[float, float] | None = None,
        volts: float | tuple[float, float] | None = None,
        dwell_s: float | tuple[float, float] | None = None,
    ) -> "Schedule":


        def value(spec, k):
            if spec is None:
                return None
            if isinstance(spec, tuple):
                a, b = spec
                span = max(1, last_layer - first_layer)
                return a + (b - a) * (k - first_layer) / span
            return float(spec)

        entries = [
            (k, k, WeldParams(wfs=value(wfs, k), volts=value(volts, k), dwell_s=value(dwell_s, k)))
            for k in range(first_layer, last_layer + 1)
        ]
        return Schedule(entries)
