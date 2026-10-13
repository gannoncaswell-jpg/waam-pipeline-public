from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import cell_lc
from waam_pipeline.backends.jbi_read import read_jbi
from waam_pipeline.clearance import (
    YASKAWA_45_CAPSULES,
    YASKAWA_45_TCP,
    TorchModel,
)
from waam_pipeline.robots import ar1440
from waam_pipeline.robots.ar1440 import PULSES_PER_DEG
from waam_pipeline.slicer import wall


WALL_LEN_MM = 150.0
LAYERS = 6
LAYER_H_MM = 2.0
SUBSTRATE_MM = 6.35
ORIGIN_X, ORIGIN_Y = 1014.0, -180.0

FAIL = 0


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAIL
    if not ok:
        FAIL += 1
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


rule("1. SLICER -> TOOLPATH IR   (declared units: mm, mm/min)")

tp = wall(WALL_LEN_MM, LAYERS, LAYER_H_MM)
substrate_top = cell_lc.platter_work_surface_z(SUBSTRATE_MM)
origin = cell_lc.snap_toolpath_to_platter(
    tp, ORIGIN_X, ORIGIN_Y, substrate_mm=SUBSTRATE_MM
)

print(f"  {tp.summary()}")
print(f"  part origin in robot base frame: {np.round(origin, 2)} mm")
print(f"  work surface z {substrate_top:.2f} = face {cell_lc.PLATTER_FACE_Z_MODEL:.1f}"
      f" {cell_lc.PLATTER_FACE_OFFSET_MM:+.1f} (calibration) + substrate {SUBSTRATE_MM}")

feeds = {m.feed for m in tp.moves if m.feed is not None}
print(f"  distinct feedrates in IR: {sorted(feeds)} mm/min")

(x0, x1), (y0, y1), (z0, z1) = tp.bbox()
check(
    "wall length matches request",
    abs((x1 - x0) - WALL_LEN_MM) < 1e-6,
    f"IR spans {x1 - x0:.3f} mm, requested {WALL_LEN_MM}",
)
top = z1
check(
    "top layer sits at LAYERS * layer_height",
    abs(top - LAYERS * LAYER_H_MM) < 1e-6,
    f"top z {top:.3f} mm, expected {LAYERS * LAYER_H_MM}",
)
check(
    "first weld TCP lands on the substrate top (base-layer CTWD from the plate)",
    abs((origin[2] + min(p[2] for p in tp.deposit_points())) - substrate_top) < 1e-9,
    f"first weld TCP z (robot) {origin[2] + z0:.3f}, "
    f"substrate top {substrate_top:.3f}",
)


_plate = next(b for b in cell_lc.lc_scene(part_origin_robot=origin).boxes
              if b[0] == "charles_platter")
check(
    "collision scene and toolpath share one work-face height",
    abs((float(_plate[2][2]) + origin[2]) - cell_lc.PLATTER_FACE_Z) < 1e-6,
    f"scene platter top {float(_plate[2][2]) + origin[2]:.3f} vs "
    f"face {cell_lc.PLATTER_FACE_Z:.3f}",
)


rule("2. PART FRAME -> ROBOT BASE -> FLANGE POSE")

probe = tp.moves[len(tp.moves) // 2]
p_part = np.array(probe.target, float)
p_robot = p_part + origin
print(f"  probe move #{len(tp.moves) // 2}  part {np.round(p_part, 3)}"
      f"  ->  robot {np.round(p_robot, 3)} mm")

pose = cell_lc._flange_pose(p_robot)
tcp_back = pose[:3, 3] + pose[:3, :3] @ cell_lc.TOOL_TCP
check(
    "flange pose inverts the tool transform exactly",
    np.allclose(tcp_back, p_robot, atol=1e-9),
    f"max residual {np.max(np.abs(tcp_back - p_robot)):.2e} mm",
)


YASK_TO_FLANGE = np.array([[0, 0, 1], [0, -1, 0], [1, 0, 0]], float)
torch = TorchModel.yaskawa_accufast_45()
sph = torch.spheres()
R = pose[:3, :3]

rels = [
    R @ (YASK_TO_FLANGE @ (np.asarray(p, float) - YASKAWA_45_TCP))
    for p0, p1, _ in YASKAWA_45_CAPSULES for p in (p0, p1)
]
lowest = min(rels, key=lambda v: v[2])
print(f"  nearest torch solid to the wire tip: {lowest[2]:.2f} mm above,"
      f" {np.hypot(lowest[0], lowest[1]):.2f} mm lateral")


wire_flange = YASK_TO_FLANGE @ np.array([-np.sqrt(0.5), 0.0, np.sqrt(0.5)])
wire_robot = R @ (wire_flange / np.linalg.norm(wire_flange))
print(f"  wire axis in the robot frame: {np.round(wire_robot, 4)}")
check(
    "wire axis vertical (clearance stacks the torch straight up from the TCP)",
    abs(abs(wire_robot[2]) - 1.0) < 1e-6,
    f"Z component {wire_robot[2]:.6f}",
)
uncovered = [
    (float(v[2]), float(np.hypot(v[0], v[1])))
    for v in rels
    if sph[int(np.argmin(np.abs(sph[:, 0] - v[2]))), 1] < np.hypot(v[0], v[1])
]
check(
    "yaw-swept envelope covers every real capsule endpoint",
    not uncovered,
    f"{len(uncovered)} uncovered: {uncovered[:2]}",
)
print(f"  envelope: {len(sph)} spheres, axial {sph[:, 0].min():.1f}"
      f"..{sph[:, 0].max():.1f} mm, max radius {sph[:, 1].max():.1f} mm")


rule("3. IK -> DEGREES -> PULSES -> DEGREES -> FK   (the full round trip)")

solve = cell_lc.pulse_solver(origin)
rows = []
for i, m in enumerate(tp.moves):
    q = solve(m.target)
    pulses = [int(round(a * r)) for a, r in zip(q, PULSES_PER_DEG)]
    q_back = [p / r for p, r in zip(pulses, PULSES_PER_DEG)]
    tcp = ar1440.fk(q_back)["flange"]
    tcp = tcp[:3, 3] + tcp[:3, :3] @ cell_lc.TOOL_TCP
    part_back = tcp - origin
    rows.append((i, np.array(m.target, float), np.array(part_back), q, pulses))

err = np.array([np.linalg.norm(t - b) for _, t, b, _, _ in rows])
print(f"  {len(rows)} points solved")
print(f"  round-trip TCP error:  mean {err.mean():.4f} mm   max {err.max():.4f} mm")
worst = int(np.argmax(err))
print(f"  worst at move {worst}: target {np.round(rows[worst][1], 3)}"
      f"  actual {np.round(rows[worst][2], 3)}")

check("round-trip error under 0.1 mm", err.max() < 0.1, f"max {err.max():.4f} mm")


rule("4. ENCODER QUANTISATION BUDGET   (what one pulse costs at the TCP)")

q_mid = rows[len(rows) // 2][3]
base = ar1440.fk(q_mid)["flange"]
base_tcp = base[:3, 3] + base[:3, :3] @ cell_lc.TOOL_TCP
print("  axis   p/deg      1 pulse =        TCP shift")
total = 0.0
for k, name in enumerate("SLURBT"):
    dq = list(q_mid)
    dq[k] += 1.0 / PULSES_PER_DEG[k]
    t = ar1440.fk(dq)["flange"]
    t = t[:3, 3] + t[:3, :3] @ cell_lc.TOOL_TCP
    shift = float(np.linalg.norm(t - base_tcp))
    total += shift
    print(f"   {name}   {PULSES_PER_DEG[k]:9.3f}  {1.0 / PULSES_PER_DEG[k]:.6f} deg"
          f"     {shift * 1000:7.2f} um")
print(f"  worst-case sum of half-pulse errors: {total / 2 * 1000:.1f} um")
check("quantisation budget under 50 um", total / 2 < 0.050, f"{total / 2 * 1000:.1f} um")


rule("5. JOINT LIMITS   (every emitted position, against the factory soft limits)")

print("  axis    min       max     limit low  limit high   margin")
for k, name in enumerate("SLURBT"):
    vals = [r[3][k] for r in rows]
    j = ar1440.AR1440_JOINTS[k]
    margin = min(min(vals) - j.lower_deg, j.upper_deg - max(vals))
    print(f"   {name}  {min(vals):8.2f}  {max(vals):8.2f}   {j.lower_deg:8.1f}"
          f"   {j.upper_deg:8.1f}  {margin:8.2f}")
    check(f"{name} inside soft limits", margin > 0, f"margin {margin:.2f} deg")


rule("6. STEP CONTINUITY   (adjacent-point joint jumps)")

weld_jumps, travel_jumps = [], []
for i in range(1, len(rows)):
    j = max(abs(a - b) for a, b in zip(rows[i - 1][3], rows[i][3]))
    (weld_jumps if tp.moves[i].deposit and tp.moves[i - 1].deposit
     else travel_jumps).append(j)
def rpt(label, xs, limit):
    if not xs:
        print(f"  {label:18s} (none in this toolpath)")
        return
    print(f"  {label:18s} max {max(xs):7.3f} deg over {len(xs)} step(s)")
    check(f"{label} under {limit} deg", max(xs) < limit, f"max {max(xs):.3f} deg")

rpt("deposit->deposit", weld_jumps, 5.0)
rpt("across travel", travel_jumps, 45.0)
print("  emitter guard (check_step) fires above 45.0 deg")


rule("7. EMITTED .JBI ON DISK   (does the artefact match this trace?)")

jbi_path = ROOT / "docs" / "jobs-out" / "WAAM-WALL1.JBI"
if not jbi_path.exists():
    print(f"  (no file at {jbi_path} — skipping)")
else:
    raw = jbi_path.read_bytes()
    lines = raw.split(b"\r\n")
    bare_lf = sum(1 for ln in lines if b"\n" in ln)
    check("every line terminated CRLF", bare_lf == 0,
          f"{bare_lf} line(s) with a bare LF")

    parsed = read_jbi(jbi_path)
    n_c = sum(1 for k in parsed.positions if k.startswith("C"))
    check("///NPOS matches the C-record count", parsed.npos[0] == n_c,
          f"NPOS {parsed.npos[0]} vs {n_c} records")
    check("RB1-only (no station records)", parsed.groups == ["RB1"],
          f"groups {parsed.groups}")

    arcon = [ln for ln in parsed.instructions if ln.startswith("ARCON")]
    arcof = [ln for ln in parsed.instructions if ln.startswith("ARCOF")]
    check("every ARCON has a matching ARCOF", len(arcon) == len(arcof),
          f"{len(arcon)} on / {len(arcof)} off")


    depth = 0
    arc_at_park = False
    for ln in parsed.instructions:
        if ln.startswith("ARCON"):
            depth += 1
        elif ln.startswith("ARCOF"):
            depth -= 1
        elif ln.startswith("MOVJ") and depth > 0:
            arc_at_park = True
    check("no MOVJ while the arc is on", not arc_at_park)


    bad = []
    for key, vals in parsed.positions.items():
        if not key.startswith("C"):
            continue
        q = [float(p) / r for p, r in zip(vals.split(','), PULSES_PER_DEG)]
        for k, j in enumerate(ar1440.AR1440_JOINTS):
            if not (j.lower_deg <= q[k] <= j.upper_deg):
                bad.append((key, "SLURBT"[k], round(q[k], 2)))
    check("every position record is inside the soft limits", not bad, str(bad[:4]))

rule("RESULT")
print(f"  {'ALL CHECKS PASSED' if FAIL == 0 else str(FAIL) + ' CHECK(S) FAILED'}")
sys.exit(1 if FAIL else 0)
