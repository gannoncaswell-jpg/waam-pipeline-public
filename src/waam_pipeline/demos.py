from __future__ import annotations

import math
from dataclasses import dataclass, replace

from . import arcparams
from .slicer import wall
from .toolpath import Move, Toolpath

FEED = 200.0


def _loop(
    points_for_layer,
    layers: int,
    layer_height_mm: float,
    *,
    feed_mm_min: float = FEED,
) -> Toolpath:

    tp = Toolpath()
    line = 1
    for k in range(layers):
        z = (k + 1) * layer_height_mm
        pts = list(points_for_layer(k))
        if len(pts) < 3:
            raise ValueError("a layer needs at least 3 points")
        x0, y0 = pts[0]
        tp.moves.append(Move(x0, y0, z, None, False, k, line))
        line += 1
        for x, y in pts[1:] + [pts[0]]:
            tp.moves.append(Move(x, y, z, feed_mm_min, True, k, line))
            line += 1
    return tp


BEAD_LIFT_MM = 10.0


def demo_wall(length_mm: float = 150.0, layers: int = 6,
              layer_height_mm: float = 2.0, feed_mm_min: float = FEED,
              thickness_mm: float = 0.0, bead_width_mm: float | None = None) -> Toolpath:


    if not thickness_mm:
        return wall(length_mm, layers, layer_height_mm, feed_mm_min)
    if bead_width_mm is None:
        raise ValueError("a wall thicker than one bead needs the bead width")
    beads = arcparams.wall_beads(thickness_mm, bead_width_mm, layer_height_mm)
    if beads.count == 1:
        return wall(length_mm, layers, layer_height_mm, feed_mm_min)
    half = (beads.count - 1) * beads.spacing_mm / 2.0
    across = [i * beads.spacing_mm - half for i in range(beads.count)]
    tp, line, x, y_last = Toolpath(), 1, 0.0, across[0]
    for k in range(layers):
        z = (k + 1) * layer_height_mm
        for j, y in enumerate(across if k % 2 == 0 else across[::-1]):
            if j:
                tp.moves += [Move(x, y_last, z + BEAD_LIFT_MM, None, False, k, line),
                             Move(x, y, z + BEAD_LIFT_MM, None, False, k, line + 1)]
                line += 2
            end = length_mm if x == 0.0 else 0.0
            tp.moves += [Move(x, y, z, None, False, k, line),
                         Move(end, y, z, feed_mm_min, True, k, line + 1)]
            line += 2
            x, y_last = end, y
    return tp


def demo_bead(length_mm: float = 101.6, layer_height_mm: float = 2.0,
              feed_mm_min: float = 200.0, count: int = 1,
              pitch_mm: float = 25.0) -> Toolpath:


    if type(count) is not int or count < 1:
        raise ValueError(f"bead count must be a whole number >= 1, got {count!r}")
    if count == 1:
        return wall(length_mm, 1, layer_height_mm, feed_mm_min)
    if not math.isfinite(pitch_mm) or pitch_mm <= 0:
        raise ValueError(f"bead spacing must be > 0 mm, got {pitch_mm!r}")
    tp = Toolpath()
    z = layer_height_mm
    for k in range(count):
        y, line = k * pitch_mm, 3 * k + 1
        tp.moves += [Move(0.0, y, z, None, False, k, line),
                     Move(length_mm, y, z, feed_mm_min, True, k, line + 1),
                     Move(length_mm, y, z + BEAD_LIFT_MM, None, False, k, line + 2)]
    return tp


def with_feed(toolpath: Toolpath, feed_mm_min: float) -> Toolpath:

    feed = float(feed_mm_min)
    if all(m.feed == feed for m in toolpath.moves if m.deposit):
        return toolpath
    return replace(toolpath, moves=[replace(m, feed=feed) if m.deposit else m
                                    for m in toolpath.moves])


def demo_cylinder(radius_mm: float = 45.0, layers: int = 12,
                  layer_height_mm: float = 2.0, segments: int = 48,
                  seam_deg_per_layer: float = 25.0) -> Toolpath:

    def pts(k):
        s = math.radians(seam_deg_per_layer * k)
        return [
            (radius_mm * math.cos(s + 2 * math.pi * i / segments),
             radius_mm * math.sin(s + 2 * math.pi * i / segments))
            for i in range(segments)
        ]
    return _loop(pts, layers, layer_height_mm)


def demo_square_tube(side_mm: float = 80.0, layers: int = 12,
                     layer_height_mm: float = 2.0,
                     corner_r_mm: float = 10.0) -> Toolpath:


    h = side_mm / 2.0
    r = min(corner_r_mm, h * 0.9)

    def pts(_k):
        out = []
        for cx, cy, a0 in ((h - r, h - r, 0.0), (-(h - r), h - r, 90.0),
                           (-(h - r), -(h - r), 180.0), (h - r, -(h - r), 270.0)):
            for i in range(7):
                a = math.radians(a0 + 90.0 * i / 6.0)
                out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return out
    return _loop(pts, layers, layer_height_mm)


def demo_cone(base_radius_mm: float = 55.0, top_radius_mm: float = 20.0,
              layers: int = 16, layer_height_mm: float = 2.0,
              segments: int = 48, seam_deg_per_layer: float = 25.0) -> Toolpath:


    if layers < 2:
        raise ValueError("a cone needs at least 2 layers")

    def pts(k):
        f = k / (layers - 1)
        r = base_radius_mm + (top_radius_mm - base_radius_mm) * f
        s = math.radians(seam_deg_per_layer * k)
        return [
            (r * math.cos(s + 2 * math.pi * i / segments),
             r * math.sin(s + 2 * math.pi * i / segments))
            for i in range(segments)
        ]
    return _loop(pts, layers, layer_height_mm)


def demo_twisted_square(side_mm: float = 70.0, layers: int = 16,
                        layer_height_mm: float = 2.0,
                        twist_deg_total: float = 60.0) -> Toolpath:

    h = side_mm / 2.0
    base = [(h, h), (-h, h), (-h, -h), (h, -h)]

    def pts(k):
        a = math.radians(twist_deg_total * (k / max(1, layers - 1)))
        ca, sa = math.cos(a), math.sin(a)
        out = []
        for x, y in base:
            out.append((x * ca - y * sa, x * sa + y * ca))

        dense = []
        for i, p in enumerate(out):
            q = out[(i + 1) % len(out)]
            for t in range(4):
                f = t / 4.0
                dense.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f))
        return dense
    return _loop(pts, layers, layer_height_mm)


@dataclass(frozen=True)
class Demo:
    key: str
    label: str
    build: object
    blurb: str


DEMOS: dict[str, Demo] = {
    "wall": Demo("wall", "Wall", demo_wall,
                 "Straight wall: length, layers and thickness. Thickness 0 is one bead."),
    "bead": Demo("bead", "Bead-on-plate test (4 in)", demo_bead,
                 "4 in (101.6 mm) beads, one or a parallel set. Set substrate thickness and review the weld schedule."),
    "cylinder": Demo("cylinder", "Round tube", demo_cylinder,
                     "O90 x 12 layers, seam walked 25 deg/layer."),
    "square": Demo("square", "Square tube", demo_square_tube,
                   "80 mm section, radiused corners."),
    "cone": Demo("cone", "Tapered cone", demo_cone,
                 "O110 to O40 over 16 layers - overhang per layer."),
    "twist": Demo("twist", "Twisted square", demo_twisted_square,
                  "70 mm section twisting 60 deg over its height."),
}

DEFAULT_DEMO = "wall"


def build(key: str, *, layer_height_mm: float | None = None, **options) -> Toolpath:

    demo = DEMOS.get(key)
    if demo is None:
        raise ValueError(f"unknown demo {key!r}; have {sorted(DEMOS)}")
    if layer_height_mm is not None:
        options["layer_height_mm"] = float(layer_height_mm)
    return demo.build(**options)
