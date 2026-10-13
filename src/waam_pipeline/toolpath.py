from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import dist


class EmptyToolpath(ValueError):
    pass


@dataclass(frozen=True)
class Move:


    x: float
    y: float
    z: float
    feed: float | None
    deposit: bool
    layer: int
    line_no: int
    region: str | None = None


    seat: float = 0.0


    ctwd_lift: float = 0.0
    copy: int = 0

    @property
    def target(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass
class Toolpath:
    moves: list[Move] = field(default_factory=list)

    unhandled: list[tuple[int, str]] = field(default_factory=list)
    copies: int = 1

    def deposit_points(self) -> list[tuple[float, float, float]]:

        points = []
        for i, move in enumerate(self.moves):
            if move.deposit:
                if i:
                    points.append(self.moves[i - 1].target)
                points.append(move.target)
        return points

    def layers(self) -> list[list[Move]]:

        groups: list[list[Move]] = []
        for m in self.moves:
            if not groups or groups[-1][0].layer != m.layer:
                groups.append([])
            groups[-1].append(m)
        return groups

    def layer_range(self, first: int, last: int) -> "Toolpath":


        index = [i for i, m in enumerate(self.moves) if first <= m.layer <= last]
        if not index:
            raise ValueError(f"No moves in layers {first} to {last}")
        if index != list(range(index[0], index[-1] + 1)):
            raise ValueError(f"Layers {first} to {last} are not one contiguous run of moves")
        moves = self.moves[index[0]:index[-1] + 1]
        if moves[0].deposit:
            if index[0] == 0:
                raise ValueError("The toolpath's first move deposits, so it has no weld start")
            moves = [replace(self.moves[index[0] - 1], deposit=False, layer=first)] + moves
        return Toolpath(list(moves), list(self.unhandled), copies=self.copies)

    def arc_events(self) -> list[tuple[str, int]]:


        events: list[tuple[str, int]] = []
        for i, m in enumerate(self.moves):
            was_deposit = i > 0 and self.moves[i - 1].deposit
            if m.deposit and not was_deposit:
                events.append(("ARCON", i))
            elif not m.deposit and was_deposit:
                events.append(("ARCOFF", i))
        if self.moves and self.moves[-1].deposit:
            events.append(("ARCOFF", len(self.moves)))
        return events

    def bbox(self) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:

        if not self.moves:
            raise EmptyToolpath(
                "this toolpath has no moves, so it has no bounding box. "
                "Nothing sliced: check the mesh and the layer height"
            )
        xs = [m.x for m in self.moves]
        ys = [m.y for m in self.moves]
        zs = [m.z for m in self.moves]
        return ((min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs)))

    def _length(self, deposit: bool) -> float:


        return sum(
            dist(a.target, b.target)
            for a, b in zip(self.moves, self.moves[1:])
            if b.deposit == deposit
        )

    def deposit_length(self) -> float:
        return self._length(deposit=True)

    def travel_length(self) -> float:
        return self._length(deposit=False)

    def summary(self) -> str:
        (x0, x1), (y0, y1), (z0, z1) = self.bbox()
        return (
            f"{len(self.moves)} moves, {len(self.layers())} layers, "
            f"{len([e for e in self.arc_events() if e[0] == 'ARCON'])} arc starts; "
            f"deposit {self.deposit_length():.1f} mm, travel {self.travel_length():.1f} mm; "
            f"bbox x[{x0:.1f}, {x1:.1f}] y[{y0:.1f}, {y1:.1f}] z[{z0:.1f}, {z1:.1f}] mm"
        )


def single_part(toolpath: Toolpath, what: str) -> Toolpath:

    if not isinstance(toolpath, Toolpath):
        raise ValueError("toolpath must be a Toolpath")
    copy_ids = {move.copy for move in toolpath.moves}
    if (type(toolpath.copies) is not int or toolpath.copies != 1
            or any(type(copy) is not int or copy != 0 for copy in copy_ids)):
        raise ValueError(f"{what} does not support interleaved copies; slice one copy")
    return toolpath
