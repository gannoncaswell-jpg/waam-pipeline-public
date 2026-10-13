from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.robots import ar1440
from waam_pipeline.robots.ar1440 import AR1440_JOINTS
cm = cell_lc._cell_models()

FLOOR = cell_lc.FLOOR_Z
POSE = [-25.0, 20.0, -45.0, 0.0, 35.0, 0.0]
FRAME_C = cell_lc.FRAME_CENTER


def world_triangles() -> np.ndarray:

    tris = []
    sm = cell_lc.lc_scene_meshes(part_origin_robot=np.zeros(3))
    for flat in sm.values():
        tris.append(np.asarray(flat, float).reshape(-1, 3, 3))
    frames = ar1440.fk(POSE)
    link_frames = {"base_link": np.eye(4)}
    for i, j in enumerate(AR1440_JOINTS):
        link_frames[f"link_{i + 1}_{j.name[-1]}"] = frames[j.name]
    rob = cell_lc._decimated_link_meshes()
    if rob:
        for link, entry in rob.items():
            t = np.asarray(entry["tris"], float).reshape(-1, 3, 3)
            t = t + np.asarray(entry["offset"], float)
            T = link_frames[link]
            pts = t.reshape(-1, 3) @ T[:3, :3].T + T[:3, 3]
            tris.append(pts.reshape(-1, 3, 3))
    tris.append(cell_lc.wire_conduit_mesh(POSE))
    return np.concatenate(tris)


def draw_edges(ax, tris2d):
    segs = np.concatenate(
        [tris2d[:, [0, 1]], tris2d[:, [1, 2]], tris2d[:, [2, 0]]]
    )

    ax.add_collection(LineCollection(segs, colors="0.62", linewidths=0.25, alpha=0.5, rasterized=True))


def dim(ax, p0, p1, off, label, perp=None, color="#0a58c4"):

    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    d = p1 - p0
    n = np.linalg.norm(d)
    if perp is None:
        perp = np.array([-d[1], d[0]]) / (n or 1.0)
    perp = np.asarray(perp, float)
    a, b = p0 + perp * off, p1 + perp * off
    for p, q in ((p0, a), (p1, b)):
        ax.plot([p[0], q[0]], [p[1], q[1]], color=color, lw=0.5, alpha=0.75)
    ax.annotate("", xy=b, xytext=a,
                arrowprops=dict(arrowstyle="<->", color=color, lw=1.0))
    mid = (a + b) / 2
    ax.annotate(label, mid, color=color, fontsize=9.0, ha="center", va="bottom",
                rotation=(90 if abs(d[0]) < abs(d[1]) else 0),
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#9bb8e8",
                          lw=0.5, alpha=1.0))


def _style(ax, title):
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_color("0.8")
    ax.set_title(title, fontsize=13, pad=12)


def page_top(ax, tris):
    fx, fy = cell_lc.FACEPLATE_X, cell_lc.FACEPLATE_Y
    t = tris[:, :, [1, 0]] * np.array([-1, 1])
    draw_edges(ax, t)
    _style(ax, "TOP VIEW (plan) — robot base = origin, forward = up")
    rail_l, rail_r = -FRAME_C[1] - cm.CELL_L / 2, -FRAME_C[1] + cm.CELL_L / 2
    dim(ax, (0, 0), (0, fx), 950, f"[station-axis-forward] {fx:.0f}")
    dim(ax, (0, 0), (-fy, 0), -1200, f"[faceplate-plane-lateral] {-fy:.0f}")
    dim(ax, (0, 0), (660, 140), -350, "[reamer-stand-position] 660 R / 140 fwd (LOG)")
    dim(ax, (rail_l, FRAME_C[0]), (rail_r, FRAME_C[0]), 900,
        f"[frame outside width] {cm.CELL_L:.0f} (reconstructed)?")
    dim(ax, (rail_l, FRAME_C[0] - cm.CELL_W / 2), (rail_l, FRAME_C[0] + cm.CELL_W / 2), 340,
        f"[frame outside depth] {cm.CELL_W:.0f} (print)")
    dim(ax, (0, 0), (0, FRAME_C[0] + 1070), -2100,
        f"[cell-frame-fore-aft] robot->front rail {FRAME_C[0] + 1070:.0f}?")
    dim(ax, (0, 0), (rail_r, 0), -1550,
        f"[cell-frame-lateral] robot->right rail {rail_r:.0f}?")
    dim(ax, (0, 700), (450, 700), 320, "[hood-plan-position] 700 fwd / 450 R?")
    ax.set_xlim(-2400, 2700)
    ax.set_ylim(-2100, 2500)


def page_front(ax, tris):
    fy = cell_lc.FACEPLATE_Y
    f = tris[:, :, [1, 2]] * np.array([-1, 1])
    draw_edges(ax, f)
    _style(ax, "FRONT VIEW — looking at the robot from the operator side")


    dim(ax, (-250, FLOOR), (-250, 0), 1300, "[robot base height] 860.4 (tape)")
    dim(ax, (300, FLOOR), (300, FLOOR + 2100), 900, "[hood-mouth-height] ~2100?")
    dim(ax, (300, FLOOR + 2100), (600, FLOOR + 2100), 380, "[hood-mouth-dia] 300?")
    dim(ax, (1050, cell_lc.AXIS_Z), (1050, cell_lc.PLATTER_FACE_Z), -250,
        f"[platter-face-row] {cell_lc.PLATTER_FACE_Z - cell_lc.AXIS_Z:.1f}?")
    dim(ax, (1450, FLOOR), (1450, cell_lc.AXIS_Z), -150, "[axis 923 above rail] (print)")
    frame_right = -FRAME_C[1] + cm.CELL_L / 2
    dimension_x = float(f[:, :, 0].max()) + 300
    dim(ax, (frame_right, FLOOR), (frame_right, FLOOR + cm.CELL_HEIGHT),
        frame_right - dimension_x,
        f"[frame-height] {cm.CELL_HEIGHT:.0f} (sheet 2)")
    ax.set_xlim(-2400, max(2700, dimension_x + 200))
    ax.set_ylim(FLOOR - 300, FLOOR + 2900)


def page_right(ax, tris):
    fx = cell_lc.FACEPLATE_X
    r = tris[:, :, [0, 2]]
    draw_edges(ax, r)
    _style(ax, "RIGHT VIEW — looking from the robot's right side")
    dim(ax, (0, FLOOR), (fx, FLOOR), -260, f"[station-axis-forward] {fx:.0f}")
    dim(ax, (-620, FLOOR), (1520, FLOOR), -520,
        "[frame depth] 2140 — robot 620? from back rail")
    dim(ax, (700, FLOOR + 2100), (700, FLOOR + 2350), -350, "[hood-cone-height] 250?")
    ax.annotate("[feeder-arm-station] / [spool-mount-offset]:\n"
                "K3572-1 lower pairs on rear U-arm pads;\n"
                "mesh-fit placement / spool support provisional",
                (-2000, FLOOR + 500), fontsize=9, color="#0a58c4",
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#9bb8e8", lw=0.5))
    ax.set_xlim(-2100, 2700)
    ax.set_ylim(FLOOR - 800, FLOOR + 2900)


def page_notes(ax):
    ax.axis("off")
    lines = [
        "LC50S CELL SHADOW — MARKUP DRAWING   (dwg ref: waam-pipeline docs/cell-drawing)",
        "",
        "All mm. Robot base plane = Z0; floor at -860.4.",
        "[ids] join to docs/measurement-sheet.csv.",
        "Values with '?' are INFERRED — cross out and write the measured value;",
        "initial + date.",
        "",
        "  id                          current          corrected",
        "  station-axis-forward        1114.4           ________________",
        "  faceplate-plane-lateral      641.4           ________________",
        "  axis-above-robot-base         62.6           ________________",
        f"  platter-face-row             {cell_lc.PLATTER_FACE_Z - cell_lc.AXIS_Z:.3f}          ________________",
        "  cell-frame-fore-aft        ~1520 F / 620 B   ________________",
        "  cell-frame-lateral         ~1370 R / 770 L   ________________",
        "  hood-mouth-height          ~2100             ________________",
        "  hood-plan-position         700 fwd / 450 R   ________________",
        "  reamer-stand-position      660 R / 140 fwd   ________________",
        "  feeder-arm-station         -75 rear pad X   ________________",
        "  spool-mount-offset         -330 / 0 / 480?   ________________",
        f"  frame-height (print)       {cm.CELL_HEIGHT:.0f}              ________________",
        "  mh185-axis-above-foot      166.5 (MotoSim)   ________________",
        "  mh185-stand-splice-z       ~823?             ________________",
        "  yrc1000-position             ?               ________________",
        "  r450-position                ?               ________________",
        "",
        "Notes:",
        "  - Reamer XY uses the historical CLEAN_R1 reconstruction; its",
        "    displayed head is 131 mm below the conservative collision box.",
        "    Re-register with active TOOL 0 and a stand measurement.",
        "  - mh185-axis-above-foot = 821.5 EX-POS Z - 655.0 column top",
        "    from the vendor MHT185 MotoSim item; confirm only if convenient.",
        "  - K3572-1 lower staggered pairs attach to the rear U-arm pads (owner).",
        "    Placement is fitted to mesh bores; feeder body slide/yaw remains open.",
        "  - 3123 is the system width including the external equipment rack.",
        "  - Rack moved to the opposite side per user; precise offsets remain provisional.",
        "  - Base rails, pedestal joints and rotating spool housing are reconstructed.",
        "    Wire conduit follows S/U endpoints; routing and support dimensions need measurement.",
        "    Fence floor-layout reference datums are 2000 x 2000 (sheet 5).",
        "    Rack structure/placement and fence registration are provisional.",
        "",
        f"Full {len(cm.MEASUREMENTS)}-row list: docs/measurement-sheet.csv (worst-first).",
    ]
    ax.text(0.04, 0.97, "\n".join(lines), va="top", family="monospace", fontsize=11)


def main():
    tris = world_triangles()
    out_pdf = ROOT / "docs" / "cell-drawing.pdf"
    pages = [
        ("top", lambda ax: page_top(ax, tris)),
        ("front", lambda ax: page_front(ax, tris)),
        ("right", lambda ax: page_right(ax, tris)),
        ("notes", page_notes),
    ]
    with PdfPages(out_pdf) as pdf:
        for name, builder in pages:
            fig, ax = plt.subplots(figsize=(16.5, 10.5))
            builder(ax)
            fig.suptitle(
                "ArcWorld LC50S — cell shadow markup sheet "
                f"({name.upper()}) — inferred values marked '?'",
                fontsize=14,
            )
            fig.tight_layout(rect=(0, 0, 1, 0.96))
            pdf.savefig(fig, dpi=110)
            fig.savefig(ROOT / "docs" / f"cell-drawing-{name}.png", dpi=130)
            plt.close(fig)
    print("wrote", out_pdf, "(4 pages) and docs/cell-drawing-<page>.png")


if __name__ == "__main__":
    main()
