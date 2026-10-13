from __future__ import annotations

import json
from pathlib import Path

from .clearance import Violation
from .toolpath import Toolpath

_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>%%TITLE%%</title>
<style>
  html, body { margin: 0; height: 100%; background: #14161a; color: #d7dae0;
               font: 13px/1.4 system-ui, sans-serif; overflow: hidden; }
  #c { display: block; cursor: grab; }
  #hud { position: fixed; top: 10px; left: 10px; background: #1e2128cc;
         border: 1px solid #333a45; border-radius: 8px; padding: 10px 14px;
         max-width: 340px; }
  #hud h1 { font-size: 14px; margin: 0 0 6px; color: #fff; }
  #hud .sum { color: #9aa3b0; font-size: 12px; }
  #controls { position: fixed; bottom: 10px; left: 10px; right: 10px;
              background: #1e2128cc; border: 1px solid #333a45; border-radius: 8px;
              padding: 8px 14px; display: flex; gap: 14px; align-items: center; }
  #layer { flex: 1; }
  #viol { position: fixed; top: 10px; right: 10px; background: #2a1c1ccc;
          border: 1px solid #5a3030; border-radius: 8px; padding: 10px 14px;
          max-width: 300px; max-height: 40vh; overflow-y: auto; font-size: 12px; }
  #viol h2 { font-size: 13px; margin: 0 0 6px; color: #ff7b72; }
  #viol div { color: #d7a9a4; margin-bottom: 3px; }
  .ok { color: #7ee787 !important; }
  label { user-select: none; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="hud"><h1>%%TITLE%%</h1><div class="sum">%%SUMMARY%%<br>
drag: orbit &nbsp; wheel: zoom &nbsp; shift-drag: pan</div></div>
<div id="viol"></div>
<div id="controls">
  <label><input type="checkbox" id="travels" checked> travels</label>
  <input type="range" id="layer" min="0" value="0">
  <span id="layerLabel"></span>
</div>
<script>
const DATA = %%DATA%%;
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
let yaw = -0.9, pitch = 0.42, zoom = 1, panX = 0, panY = 0;
const moves = DATA.moves, viols = DATA.violations;
const maxLayer = moves.reduce((m, p) => Math.max(m, p.l), 0);
const slider = document.getElementById("layer");
slider.max = maxLayer; slider.value = maxLayer;

const xs = moves.map(p => p.x), ys = moves.map(p => p.y), zs = moves.map(p => p.z);
const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
const cz = (Math.min(...zs) + Math.max(...zs)) / 2;
const span = Math.max(Math.max(...xs) - Math.min(...xs),
                      Math.max(...ys) - Math.min(...ys),
                      Math.max(...zs) - Math.min(...zs), 1);

function project(p) {
  const x = p.x - cx, y = p.y - cy, z = p.z - cz;
  const x1 = x * Math.cos(yaw) - y * Math.sin(yaw);
  const y1 = x * Math.sin(yaw) + y * Math.cos(yaw);
  const y2 = y1 * Math.cos(pitch) - z * Math.sin(pitch);
  const z2 = y1 * Math.sin(pitch) + z * Math.cos(pitch);
  const s = zoom * Math.min(canvas.width, canvas.height) * 0.72 / span;
  return [canvas.width / 2 + panX + x1 * s, canvas.height / 2 + panY - z2 * s, y2];
}

function layerColor(l) { return `hsl(${210 - 170 * (maxLayer ? l / maxLayer : 0)} 75% 58%)`; }

function draw() {
  canvas.width = innerWidth; canvas.height = innerHeight;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const cut = +slider.value, showTravel = document.getElementById("travels").checked;
  document.getElementById("layerLabel").textContent = `layer ${cut}/${maxLayer}`;
  for (let i = 1; i < moves.length; i++) {
    const a = moves[i - 1], b = moves[i];
    if (b.l > cut || a.l > cut) continue;
    if (!b.d && !showTravel) continue;
    const [ax, ay] = project(a), [bx, by] = project(b);
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    if (b.d) { ctx.strokeStyle = layerColor(b.l); ctx.lineWidth = 2.4; ctx.setLineDash([]); }
    else { ctx.strokeStyle = "#555e6b"; ctx.lineWidth = 1; ctx.setLineDash([5, 4]); }
    ctx.stroke();
  }
  ctx.setLineDash([]);
  for (const v of viols) {
    const [x, y] = project(v);
    ctx.strokeStyle = "#ff5147"; ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(x - 7, y - 7); ctx.lineTo(x + 7, y + 7);
    ctx.moveTo(x - 7, y + 7); ctx.lineTo(x + 7, y - 7);
    ctx.stroke();
  }
}

const vbox = document.getElementById("viol");
if (viols.length) {
  vbox.innerHTML = `<h2>${viols.length} clearance violation(s)</h2>` + viols.map(v =>
    `<div>${v.clearance} mm to ${v.obstacle} @ (${v.x}, ${v.y}, ${v.z})</div>`).join("");
} else {
  vbox.innerHTML = `<h2 class="ok">clearance: clean</h2>`;
  vbox.style.background = "#1c2a1ccc"; vbox.style.borderColor = "#305a30";
}

let drag = null;
canvas.onmousedown = e => { drag = { x: e.clientX, y: e.clientY, pan: e.shiftKey }; };
onmousemove = e => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (drag.pan) { panX += dx; panY += dy; }
  else { yaw += dx * 0.008; pitch = Math.max(-1.55, Math.min(1.55, pitch + dy * 0.008)); }
  drag = { x: e.clientX, y: e.clientY, pan: drag.pan };
  draw();
};
onmouseup = () => drag = null;
canvas.onwheel = e => { e.preventDefault(); zoom *= e.deltaY < 0 ? 1.12 : 0.89; draw(); };
slider.oninput = draw;
document.getElementById("travels").onchange = draw;
onresize = draw;
draw();
</script>
</body>
</html>
"""


def write_html(
    toolpath: Toolpath,
    violations: list[Violation],
    path: str | Path,
    title: str = "WAAM toolpath",
) -> Path:
    data = {
        "moves": [
            {
                "x": round(m.x, 2),
                "y": round(m.y, 2),
                "z": round(m.z, 2),
                "d": 1 if m.deposit else 0,
                "l": m.layer,
            }
            for m in toolpath.moves
        ],
        "violations": [
            {
                "x": v.point[0],
                "y": v.point[1],
                "z": v.point[2],
                "clearance": v.clearance_mm,
                "obstacle": v.obstacle,
            }
            for v in violations
        ],
    }
    html = (
        _TEMPLATE.replace("%%TITLE%%", title)
        .replace("%%SUMMARY%%", toolpath.summary())
        .replace("%%DATA%%", json.dumps(data, separators=(",", ":")))
    )
    out = Path(path)
    out.write_text(html, encoding="utf-8")
    return out
