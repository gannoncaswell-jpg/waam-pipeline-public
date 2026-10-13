from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

STEEL_DENSITY = 7850.0


@dataclass(frozen=True)
class PlatterDesign:

    diameter_mm: float = 400.0
    thickness_mm: float = 19.05
    material: str = "ASME SA-36 plate (A36 equivalent acceptable)"

    mount_positions: tuple[tuple[float, float], ...] = ((100, 0), (-100, 0), (0, 100), (0, -100))
    mount_clearance_dia: float = 13.5
    mount_cbore_dia: float = 20.0
    mount_cbore_depth: float = 12.5
    dowel_positions: tuple[tuple[float, float], ...] = ((80, 0), (-80, 0))
    dowel_dia: float = 12.0
    center_access_dia: float = 20.0

    tap_thread: str = "1/2-13 UNC"
    tap_drill_dia: float = 10.8
    tap_grid_pitch: float = 50.0
    tap_r_min: float = 55.0
    tap_r_max: float = 175.0
    tap_keepout_mm: float = 18.0
    ground_lug_pos: tuple[float, float] = (0.0, -185.0)
    ground_lug_thread: str = "M10x1.5"
    ground_lug_drill_dia: float = 8.5


    probe_pin_positions: tuple[tuple[float, float], ...] = ((150.0, 0.0), (0.0, 150.0))
    probe_pin_dia: float = 8.0

    def tap_positions(self) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        n = int(self.tap_r_max // self.tap_grid_pitch) + 1
        interface = (
            list(self.mount_positions)
            + list(self.dowel_positions)
            + list(self.probe_pin_positions)
            + [(0.0, 0.0)]
        )
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                x, y = i * self.tap_grid_pitch, j * self.tap_grid_pitch
                r = float(np.hypot(x, y))
                if not (self.tap_r_min <= r <= self.tap_r_max):
                    continue
                if any(np.hypot(x - px, y - py) < self.tap_keepout_mm + self.mount_cbore_dia / 2
                       for px, py in interface):
                    continue
                pts.append((x, y))
        return pts

    def mass_kg(self) -> float:
        r = self.diameter_mm / 2000.0
        return float(np.pi * r * r * (self.thickness_mm / 1000.0) * STEEL_DENSITY)

    def inertia_kgm2(self) -> float:
        return 0.5 * self.mass_kg() * (self.diameter_mm / 2000.0) ** 2


def _dxf_circle(layer: str, x: float, y: float, r: float) -> str:
    return f"0\nCIRCLE\n8\n{layer}\n10\n{x:.3f}\n20\n{y:.3f}\n40\n{r:.3f}\n"


def write_dxf(design: PlatterDesign, path: str | Path) -> Path:
    e = [_dxf_circle("CUT", 0, 0, design.diameter_mm / 2)]
    for x, y in design.mount_positions:
        e.append(_dxf_circle("MOUNT_THRU", x, y, design.mount_clearance_dia / 2))
        e.append(_dxf_circle("MOUNT_CBORE", x, y, design.mount_cbore_dia / 2))
    for x, y in design.dowel_positions:
        e.append(_dxf_circle("DOWEL", x, y, design.dowel_dia / 2))
    e.append(_dxf_circle("ACCESS", 0, 0, design.center_access_dia / 2))
    for x, y in design.tap_positions():
        e.append(_dxf_circle("TAP_DRILL", x, y, design.tap_drill_dia / 2))
    gx, gy = design.ground_lug_pos
    e.append(_dxf_circle("GROUND_LUG", gx, gy, design.ground_lug_drill_dia / 2))
    for x, y in design.probe_pin_positions:
        e.append(_dxf_circle("PROBE_PIN", x, y, design.probe_pin_dia / 2))
    text = "0\nSECTION\n2\nENTITIES\n" + "".join(e) + "0\nENDSEC\n0\nEOF\n"
    out = Path(path)
    out.write_text(text, encoding="ascii")
    return out


def write_drawing(design: PlatterDesign, path: str | Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 10))
    R = design.diameter_mm / 2
    ax.add_patch(plt.Circle((0, 0), R, fill=False, lw=2, color="k"))
    ax.add_patch(plt.Circle((0, 0), 160, fill=False, lw=0.8, ls="--", color="0.6"))
    ax.annotate("faceplate Ø320 (ref)", (115, -122), color="0.45", fontsize=8)
    for x, y in design.mount_positions:
        ax.add_patch(plt.Circle((x, y), design.mount_cbore_dia / 2, fill=False, color="tab:blue"))
        ax.add_patch(plt.Circle((x, y), design.mount_clearance_dia / 2, fill=False, color="tab:blue", lw=0.8))
    for x, y in design.dowel_positions:
        ax.add_patch(plt.Circle((x, y), design.dowel_dia / 2, fill=False, color="tab:green", lw=1.5))
    ax.add_patch(plt.Circle((0, 0), design.center_access_dia / 2, fill=False, color="tab:red"))
    for x, y in design.tap_positions():
        ax.add_patch(plt.Circle((x, y), design.tap_drill_dia / 2, fill=False, color="0.35", lw=0.7))
        ax.plot([x - 4, x + 4], [y, y], color="0.35", lw=0.5)
        ax.plot([x, x], [y - 4, y + 4], color="0.35", lw=0.5)
    for x, y in design.probe_pin_positions:
        ax.add_patch(plt.Circle((x, y), design.probe_pin_dia / 2, fill=True, color="tab:purple", alpha=0.6))
    gx, gy = design.ground_lug_pos
    ax.add_patch(plt.Circle((gx, gy), design.ground_lug_drill_dia / 2, fill=False, color="tab:orange", lw=1.4))

    taps = design.tap_positions()
    schedule = [
        ("A", f"4x Ø{design.mount_clearance_dia} thru, cbore Ø{design.mount_cbore_dia} x "
              f"{design.mount_cbore_depth} — M12 SHCS to faceplate (±100,0)(0,±100)", "tab:blue"),
        ("B", f"2x Ø{design.dowel_dia} press fit — dowels to faceplate Ø12.02 reamed (±80,0)", "tab:green"),
        ("C", f"1x Ø{design.center_access_dia} thru — center access (faceplate maintenance plug)", "tab:red"),
        ("D", f"{len(taps)}x drill Ø{design.tap_drill_dia}, tap {design.tap_thread} thru — "
              f"work grid {design.tap_grid_pitch} mm pitch", "0.25"),
        ("E", f"1x drill Ø{design.ground_lug_drill_dia}, tap {design.ground_lug_thread} — aux ground lug", "tab:orange"),
        ("F", f"{len(design.probe_pin_positions)}x Ø{design.probe_pin_dia} press fit — touch-sense probe pins "
              f"(Ø8 dowels, ~15 proud) at (150,0)(0,150)", "tab:purple"),
    ]
    for i, (tag, txt, color) in enumerate(schedule):
        ax.text(-R, -R - 34 - 16 * i, f"[{tag}] {txt}", fontsize=8.5, color=color, family="monospace")
    ax.text(
        -R, R + 42,
        f"WAAM PLATTER — MH180 HEADSTOCK   Ø{design.diameter_mm:.0f} x {design.thickness_mm:.2f} mm   "
        f"{design.material}",
        fontsize=11, weight="bold",
    )
    ax.text(
        -R, R + 24,
        f"mass ≈ {design.mass_kg():.1f} kg   Izz ≈ {design.inertia_kgm2():.2f} kg·m² "
        f"(MH185 budget 47 kg·m²)   interface per 193602-1CD Fig. 3-4 — VERIFY ON UNIT BEFORE CUTTING",
        fontsize=8.5, color="0.35",
    )
    ax.set_xlim(-R - 20, R + 20)
    ax.set_ylim(-R - 120, R + 60)
    ax.set_aspect("equal")
    ax.axis("off")
    out = Path(path)
    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def scene_capsule(design: PlatterDesign, top_z_mm: float = 0.0):


    return (
        "platter",
        np.array([0.0, 0.0, top_z_mm - design.thickness_mm]),
        np.array([0.0, 0.0, top_z_mm]),
        design.diameter_mm / 2,
    )


if __name__ == "__main__":
    d = PlatterDesign()
    here = Path(__file__).parent
    print(f"platter Ø{d.diameter_mm:.0f} x {d.thickness_mm:.2f} mm {d.material}")
    print(f"mass {d.mass_kg():.1f} kg, Izz {d.inertia_kgm2():.3f} kg·m² (budget 47)")
    print(f"work grid: {len(d.tap_positions())}x {d.tap_thread}")
    print("dxf:", write_dxf(d, here / "platter.dxf"))
    print("drawing:", write_drawing(d, here / "platter_drawing.png"))
