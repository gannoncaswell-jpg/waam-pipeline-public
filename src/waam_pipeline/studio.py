from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from .studio_layout import (
    STUDIO_SHELL, controls_script, placement_script, help_script, window_script, scale_script,
    settings_script, settings_styles,
)
from .studio_renderer import renderer_source

from .clearance import Scene, TorchModel, Violation, blocking
from .schedule import Schedule
from .toolpath import Toolpath


NUMERIC_LIMITS = {

    "lh": ("wbLh", 0.1, 100, "Bead height: enter 0.1–100 mm, the height each layer adds"),
    "tilt": ("wbTilt", -360, 360, "Tilt: enter -360–360 degrees"),
    "seam": ("wbSeam", -360, 360, "Seam rotation: enter -360–360 degrees per layer"),
    "bead": ("wbBead", 0.1, 100, "Bead width: enter 0.1–100 mm"),
    "margin": ("wbMargin", 0, 100, "Margin: enter 0–100 mm (cell minimum 5 mm)"),
    "wire": ("wbWire", 0.1, 10, "Wire diameter: enter 0.1–10 mm"),
    "travel": ("wbTravel", 1, 10000, "Travel speed: enter 1–10000 mm/min"),

    "wfs": ("wbWfs", 10, 2000, "Wire feed: enter 10–2000 in/min, or leave it blank"),

    "ctwd": ("wbCtwd", 5, 40, "Weld CTWD: enter the calibrated CTWD up to 40 mm, or leave blank for the default"),
    "ox": ("wbOx", -10000, 10000, "X offset: enter -10000 to 10000 mm"),
    "oy": ("wbOy", -10000, 10000, "Y offset: enter -10000 to 10000 mm"),
    "sub": ("wbSub", 0, 100, "Substrate thickness: enter 0–100 mm"),
    "bead_length": ("wbBeadLength", 1, 1000, "Bead length: enter 1 to 1000 mm"),
    "bead_count": ("wbBeadCount", 1, 12, "Bead count: enter a whole number from 1 to 12"),
    "bead_pitch": ("wbBeadPitch", 1, 300, "Bead spacing: enter 1 to 300 mm"),
    "wall_length": ("wbWallLength", 10, 1000, "Wall length: enter 10 to 1000 mm"),
    "wall_layers": ("wbWallLayers", 1, 200, "Wall layers: enter a whole number from 1 to 200"),
    "wall_thickness": ("wbWallThickness", 0, 300, "Wall thickness: enter 0 for one bead, or up to 300 mm"),
    "copies": ("wbCopies", 1, 4, "Copies: enter a whole number from 1 to 4"),
    "copy_spacing": ("wbCopySpacing", 0.1, 10000, "Copy spacing: enter 0.1 to 10000 mm"),


    "interpass": ("wbInterpass", 1, 3600, "Interpass dwell: enter 1 to 3600 s"),
    "interpass_every": ("wbInterpassEvery", 1, 200, "Layers per interpass: enter a whole number from 1 to 200"),
    "temp_check_every": ("wbTempCheckEvery", 0, 200, "Temperature check: enter 0 (off) or a whole number of layers up to 200"),
    "bead_dwell": ("wbBeadDwell", 0, 3600, "Between-bead dwell: enter 0 (off) or 0.01 to 3600 s"),
    "measure_every": ("wbMeasureEvery", 0, 1000, "Measure stop: enter 0 or leave blank (off), or a whole number of layers up to 1000"),
    "substrate_margin": ("wbSubstrateMargin", 0, 100, "Substrate edge margin: enter 0 to 100 mm"),
    "oz": ("wbOz", 0, 100, "Lift-off gap: enter 0–100 mm"),
    "yaw": ("wbYaw", -360, 360, "Z rotation: enter -360 to 360 degrees"),
    "rotX": ("wbRotX", -360, 360, "X tilt: enter -360 to 360 degrees"),
    "rotY": ("wbRotY", -360, 360, "Y tilt: enter -360 to 360 degrees"),
    "arc": ("wbArc", 1, 1000, "ASF#: enter the pendant value as a whole number from 1 to 1000"),
    "contours": ("wbContours", 1, 20, "ORNL contours: enter a whole number from 1 to 20"),
    "stepover": ("wbStepover", 10, 100, "ORNL bead spacing: enter 10–100% of bead width"),
    "infill_angle": ("wbInfillAngle", -360, 360, "Infill angle: enter -360–360 degrees"),
    "infill_rotation": ("wbInfillRotation", -360, 360, "Infill rotation: enter -360–360 degrees per layer"),
    "travel_lift": ("wbTravelLift", .1, 100, "Travel lift: enter 0.1–100 mm"),

    "sense_spacing": ("wbSenseSpacing", 10, 200, "Touch spacing: enter 10 to 200 mm"),
    "sense_inset": ("wbSenseInset", 5, 50, "Inset from edges: enter 5 to 50 mm"),
    "sense_points": ("wbSensePoints", 2, 21, "Maximum touches: enter a whole number from 2 to 21"),

    "sense_layer": ("wbSenseLayer", 0, 9999, "Layer to touch: enter a whole number from 0"),
    "layer_spacing": ("wbLayerSpacing", 5, 100, "Layer touch spacing: enter 5 to 100 mm"),
    "run_temperature": ("wbRunTemperature", -40, 2500, "Temperature: enter -40–2500 °F or -40–1400 °C"),
    "auto_spacing": ("wbAutoSpacing", 5, 100, "Layer touch spacing: enter 5 to 100 mm"),
    "auto_max_interpass": ("wbAutoMax", -40, 2500, "Maximum interpass: enter -40–2500 °F or -40–1400 °C, or leave blank"),
    "auto_ambient": ("wbAutoAmbient", -40, 140, "Ambient: enter -40–140 °F or -40–60 °C"),
    "auto_before": ("wbAutoBefore", -40, 2500, "T-before: enter -40–2500 °F or -40–1400 °C"),
    "auto_after": ("wbAutoAfter", -40, 2500, "T-after: enter -40–2500 °F or -40–1400 °C"),
    "auto_reading": ("wbAutoReading", -40, 2500, "Temperature: enter -40–2500 °F or -40–1400 °C"),

    "reindex_clear": ("wbReindexClear", 20, 200, "Clear height: enter 20 to 200 mm above the block top"),
}


def _tooling_state(torch, station_transform=None) -> dict:


    import numpy as np

    from .clearance import YASKAWA_45_CAPSULES, YASKAWA_45_TCP

    axis = np.array([0.0, 0.0, 1.0])
    if station_transform is not None:
        axis = np.asarray(station_transform, dtype=float)[:3, :3].T @ axis
    return {
        "envelope": torch.envelope().tolist(),
        "spheres": torch.spheres().tolist(),
        "axis": axis.tolist(),
        "tcp_flange": YASKAWA_45_TCP.tolist(),
        "capsules": [
            {"p0": list(a), "p1": list(b), "r": r}
            for a, b, r in YASKAWA_45_CAPSULES
        ],
    }


def build_state(
    toolpath: Toolpath,
    *,
    title: str = "WAAM build",
    schedule: Schedule | None = None,
    violations: list[Violation] | None = None,
    scene: Scene | None = None,
    torch: TorchModel | None = None,
    cell_name: str | None = None,
    robot: dict | None = None,
    live: dict | None = None,
    workbench: bool = False,
    scene_meshes: dict | None = None,
    scene_reference_boxes: list[dict] | None = None,
    part_mesh=None,
    arc: dict | None = None,
    arc_warnings: list[str] | None = None,
    thermal: dict | None = None,
    shape: str | None = None,
    workspace: str | None = None,
    email: str | None = None,
    stl_info: dict | None = None,
    placement: dict | None = None,
    correction: dict | None = None,
    request_params: dict | None = None,
    slicing: dict | None = None,
    ornl_status: dict | None = None,
    station_transform=None,
    cell_profile: dict | None = None,
    tooling_available: bool = True,
) -> dict:
    torch = torch or TorchModel.yaskawa_accufast_45()
    n_layers = len(toolpath.layers())
    per_layer = []
    if schedule is not None:
        for k in range(n_layers):
            p = schedule.for_layer(k)
            per_layer.append(
                None
                if p is None
                else {"wfs": p.wfs, "volts": p.volts, "dwell_s": p.dwell_s, "asf": p.asf}
            )
    scene = scene or Scene()
    part = None
    if part_mesh is not None:


        import numpy as np

        tri = np.asarray(part_mesh, dtype=float).reshape(-1, 3, 3)
        total = len(tri)
        display_limit = 16000
        if total > display_limit:
            keep = np.linspace(0, total - 1, display_limit, dtype=int)
            shown = tri[keep]
        else:
            shown = tri
        part = {
            "tris": [round(float(v), 2) for v in shown.reshape(-1)],
            "triangles": total,
            "displayed": len(shown),
        }
    return {


        "meta": {"title": title, "cell": cell_name, "workbench": workbench,
                  "shape": shape, "workspace": workspace, "email": email,
                  "stl": stl_info, "placement": placement,
                  "correction": correction, "params": request_params,
                  "slicing": slicing, "ornl": ornl_status,
                  "cell_profile": cell_profile,
                  "clearance_checked": violations is not None},
        "toolpath": {
            "moves": [
                {
                    "x": round(float(m.x), 2),
                    "y": round(float(m.y), 2),
                    "z": round(float(m.z), 2),
                    "d": 1 if m.deposit else 0,
                    "l": m.layer,
                    **({"region": m.region} if m.region is not None else {}),
                }
                for m in toolpath.moves
            ],

            "move_lines": [m.line_no for m in toolpath.moves],
            "layers": n_layers,
            "summary": toolpath.summary(),
        },
        "schedule": {"per_layer": per_layer} if per_layer else None,
        "violations": [
            {
                "move": v.move_index,
                "x": v.point[0],
                "y": v.point[1],
                "z": v.point[2],
                "clearance": v.clearance_mm,
                "obstacle": v.obstacle,
                "blocking": bool(blocking([v])),

                **{key: getattr(v, key) for key in ("tier", "geometry", "hull_derived")
                   if getattr(v, key, None) is not None},
            }
            for v in (violations or [])
        ],
        "scene": {
            "meshes": scene_meshes,
            "reference_boxes": scene_reference_boxes or [],
            "boxes": [
                {"name": n, "min": list(map(float, a)), "max": list(map(float, b))}
                for n, a, b in scene.boxes
            ],
            "capsules": [
                {"name": n, "p0": list(map(float, p0)), "p1": list(map(float, p1)), "r": float(r)}
                for n, p0, p1, r in scene.capsules
            ],
        },
        "part": part,
        "torch": {
            "standoff": torch.standoff_mm,
            "nozzle_d": torch.nozzle_diameter_mm,
            "nozzle_len": torch.nozzle_length_mm,
            "body_d": torch.body_diameter_mm,
            "body_len": torch.body_length_mm,
            **_tooling_state(torch, station_transform),
        } if tooling_available else {"available": False},
        "robot": robot,
        "live": live or {"linked": False, "channels": []},
        "arc": arc,
        "arc_warnings": list(arc_warnings or []),
        "thermal": thermal,
    }


def _filler_options(selected: str | None = None) -> str:
    from .arcparams import DEFAULT_FILLER, FILLERS

    sel = selected or DEFAULT_FILLER
    return "".join(
        f'<option value="{k}"{" selected" if k == sel else ""}'
        + (f' data-interpass-c="{f.interpass_max_c:g}"' if f.interpass_max_c is not None else "")
        + f'>{f.label}</option>'
        for k, f in FILLERS.items()
    )


def _demo_options(selected: str | None = None) -> str:
    from .demos import DEFAULT_DEMO, DEMOS

    sel = selected or DEFAULT_DEMO
    return "".join(
        f'<option value="{k}"{" selected" if k == sel else ""}>{d.label}</option>'
        for k, d in DEMOS.items()
    )


_PLACEHOLDER = re.compile(r"%%(\w+)%%")


_WRAP_START = "\n(async function(){"


_WRAP_END = ("\n})().catch(error=>{"
             "document.getElementById('wbMsg').textContent="
             "'Preview failed: '+error.message;"
             "console.error(error);throw error;});")


def render_studio(state: dict, *, hosted: bool = False) -> str:
    parts: dict[str, str] = {}
    for key, (control, low, high, message) in NUMERIC_LIMITS.items():

        step = "1" if key in ("arc", "contours", "sense_points", "sense_layer", "bead_count", "copies",
                              "wall_layers", "interpass_every", "temp_check_every", "measure_every") else "any"
        required = "" if key in ("auto_max_interpass", "auto_ambient", "wfs", "ctwd", "measure_every") else "required "
        parts[control] = (f'min="{low:g}" max="{high:g}" step="{step}" {required}'
                          f'data-error="{escape(message)}"')
    renderer = renderer_source() if not hosted else Path(__file__).with_name("studio_assets.js").read_text(encoding="utf-8")
    bootstrap = "const rendererAsset=null;"
    detail = ""
    if hosted:
        from . import web_assets
        renderer_key = json.dumps(web_assets.assets()["renderer"]["key"])
        bootstrap = f"""
const rendererAsset = {renderer_key};
document.getElementById("busy").hidden=false;
document.getElementById("busyText").textContent="Loading preview…";
try {{ await window.WaamAssets.prepare(S,rendererAsset); }}
catch(e) {{ window.WaamAssets.setMode("light",false); await window.WaamAssets.hydrate(S);
  document.getElementById("wbMsg").textContent="Using light preview. Select Full detail in Layers to retry."; }}
finally {{ document.getElementById("busy").hidden=true; }}
"""
        detail = '<label>Preview detail <select id="visualDetail"><option value="auto">Automatic</option><option value="full">Full detail</option><option value="light">Light preview</option></select></label>'
    parts.update({
        "WRAPSTART": _WRAP_START if hosted else "",
        "WRAPEND": _WRAP_END if hosted else "",
        "HELP": help_script(),
        "SETTINGS": settings_script(),
        "WINDOW": window_script(),
        "SCALE": scale_script(),
        "SETTINGS_CSS": settings_styles(),
        "TITLE": escape(str(state["meta"]["title"])),
        "RENDERER": renderer,
        "BOOTSTRAP": bootstrap,
        "DETAIL": detail,
        "CONTROLS": controls_script(),
        "PLACEMENT": placement_script(),
        "SUBSTRATE": Path(__file__).with_name("studio_substrate.js").read_text(encoding="utf-8"),
        "LAYERSENSE": Path(__file__).with_name("studio_layer_sense.js").read_text(encoding="utf-8"),
        "RUNS": Path(__file__).with_name("studio_runs.js").read_text(encoding="utf-8"),
        "AUTO": Path(__file__).with_name("studio_auto.js").read_text(encoding="utf-8"),
        "REINDEX": Path(__file__).with_name("studio_reindex.js").read_text(encoding="utf-8"),
        "LIBRARY": Path(__file__).with_name("studio_library.js").read_text(encoding="utf-8"),
        "FILLERS": _filler_options((state.get("arc") or {}).get("key")),
        "DEMOS": _demo_options(state.get("meta", {}).get("shape")),
        "DATA": json.dumps(state, separators=(",", ":")).replace("<", "\\u003c"),
    })

    def fill(match: re.Match) -> str:
        try:
            return parts[match.group(1)]
        except KeyError:
            raise KeyError(f"studio template: no value for {match.group(0)}") from None


    return _PLACEHOLDER.sub(fill, _TEMPLATE)


def write_studio(state: dict, path: str | Path) -> Path:
    out = Path(path)
    out.write_text(render_studio(state), encoding="utf-8")
    return out


_TEMPLATE = STUDIO_SHELL.replace(
    '      <p class="help" id="ctwdHelp">',
    '      <div class="row"><label for="wbCleanFirst">Clean wire (CLEAN_R1) before the first weld</label>'
    '<input id="wbCleanFirst" type="checkbox" aria-describedby="ctwdHelp"></div>\n'
    '      <p class="help" id="ctwdHelp">',
) + """<script data-app>%%WRAPSTART%%
%%SUBSTRATE%%
%%LAYERSENSE%%
%%RUNS%%
%%AUTO%%
%%REINDEX%%
const S = %%DATA%%;
let gpu=null;


const WB_K=new URLSearchParams(location.search).get("k");


let IS_GUEST=false;
const UI={step:"prepare",view:"cell",busy:false,invalidField:null,
  storage:"waamStudio:"+(S.meta.workspace||"offline"),
  fields:"waamWb:"+(S.meta.workspace||"offline")};
function api(path, extra){
  const u=new URLSearchParams(extra||{});
  if(WB_K) u.set("k", WB_K);
  if(S.meta.workspace) u.set("ws", S.meta.workspace);


  if(S.meta.gen!=null) u.set("gen", S.meta.gen);
  const q=u.toString();
  return q ? path+(path.includes("?")?"&":"?")+q : path;
}
%%BOOTSTRAP%%
async function hydrateState(state){
  if(!window.WaamAssets) return "";
  try{await window.WaamAssets.hydrate(state);return "";}
  catch(e){


    window.WaamAssets.setMode("light",false);await window.WaamAssets.hydrate(state);
    if(gpu){gpu.dispose();gpu=null;}
    const detail=document.getElementById("visualDetail");if(detail) detail.value="light";
    document.getElementById("showBoxes").checked=true;
    return "Using light preview for the updated build. Select Full detail in Layers to retry.";
  }
}
function lightPreview(){return !!(window.WaamAssets && window.WaamAssets.light);}
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");

const showWalls=document.getElementById("showWalls");
const showFrame=document.getElementById("showFrame");
const showBoxes=document.getElementById("showBoxes");
const showTorch=document.getElementById("showTorch");
const showRobot=document.getElementById("showRobot");
const showScene=document.getElementById("showScene");
const showScale=document.getElementById("showScale");
const showTravel=document.getElementById("showTravel");
const showGhost=document.getElementById("showGhost");
const layerNow=document.getElementById("layerNow");
const depNow=document.getElementById("depNow");
const moveNow=document.getElementById("moveNow");
const readout=document.getElementById("readout");
const paramRows=document.getElementById("paramRows");
const liveRows=document.getElementById("liveRows");

const collisionCache = new WeakMap();
const collisionColors = { contact: ["#f0a629", "#f03435"], artifact: ["#54a9ff", "#be58ed"] };
function collisionIndex(state) {
  const cached = collisionCache.get(state);
  if (cached && cached.path === state.toolpath && cached.source === state.violations) return cached;
  const moves = state.toolpath.moves, lines = new Map(), byMove = new Map(), segments = new Map();
  const sourceLines = state.toolpath.move_lines || [];
  moves.forEach((m, i) => {
    const line = sourceLines[i] ?? m.line;
    if (i && m.d && Number.isInteger(line)) {

      lines.set(line, lines.has(line) ? null : i);
    }
  });
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
  const events = (state.violations || []).map(v => {

    const match = /^bead\\(line (-?\\d+)\\)$/.exec(v.obstacle || "");
    const owner = match ? lines.get(Number(match[1])) : null, i = v.move;
    const a = moves[i - 1], b = moves[i], prev = moves[i - 2];
    const valid = Number.isInteger(i) && i >= 0 && i < moves.length;
    const point = [v.x, v.y, v.z].every(Number.isFinite);
    let corner = false, adjacent = false;
    if (valid && owner === i - 1 && a && b && prev && a.d && b.d && a.l === b.l) {
      adjacent = true;
      const u = [a.x - prev.x, a.y - prev.y, a.z - prev.z], w = [b.x - a.x, b.y - a.y, b.z - a.z];
      const norm = Math.hypot(...u) * Math.hypot(...w);
      const angle = norm ? Math.acos(Math.max(-1, Math.min(1, u.reduce((s, x, k) => s + x * w[k], 0) / norm))) : 0;


      const near = Math.max(0.05, (Number((state.meta.params || {}).bead) || 8.6) / 2);
      corner = point && angle >= Math.PI / 6 && distance(v, a) <= near &&
        b.z >= a.z - 1e-6 && v.z >= Math.max(prev.z, a.z) - 0.02;
    }
    const hull = (v.tier === 2 || v.tier === "tier2") &&
      (v.hull_derived === true || v.geometry === "convex_hull");
    const artifact = corner || hull, penetration = Number.isFinite(v.clearance) && v.clearance < 0;
    const reason = hull ? "likely hull artifact (tier 2)" : corner ? "likely continuous-corner artifact" :
      "tooling contact; no artifact evidence";
    let seek = valid ? i : null;
    if (valid && i > 0 && point) {
      const d = [b.x - a.x, b.y - a.y, b.z - a.z], length2 = d.reduce((s, x) => s + x * x, 0);
      const t = length2 ? ((v.x-a.x)*d[0]+(v.y-a.y)*d[1]+(v.z-a.z)*d[2])/length2 : 1;
      seek = i - 1 + Math.max(0, Math.min(1, t));
    }
    const e = { v, owner, adjacent, artifact, reason, penetration, seek, point,
      color: collisionColors[artifact ? "artifact" : "contact"][penetration ? 1 : 0],
      rank: (penetration ? 2 : 0) + (artifact ? 1 : 2) };
    if (valid) {
      let row = byMove.get(i);
      if (!row) { row = { events: [], worst: e }; byMove.set(i, row); }
      row.events.push(e); if (e.rank > row.worst.rank) row.worst = e;
      for (const end of [i, owner]) {
        if (!Number.isInteger(end) || end < 1) continue;
        if (!segments.has(end) || segments.get(end).rank < e.rank) segments.set(end, e);
      }
    }
    return e;
  }).sort((a, b) => a.v.move - b.v.move);
  const result = { path: state.toolpath, source: state.violations, events, byMove, segments };
  collisionCache.set(state, result); return result;
}

window.waamCollisionIndex=collisionIndex;
let M = S.toolpath.moves, N = M.length;
let collisionData = collisionIndex(S), selectedCollision = null, collisionPage = 0;
function selectedEvent(){return selectedCollision && selectedCollision.seek===cursor ? selectedCollision : null;}
document.getElementById("cellChip").textContent = S.meta.cell || "No cell selected";


function showIdentity(email, sso){
  if(!S.meta.workspace) return;
  const chip=document.getElementById("wsChip");
  chip.textContent=email||S.meta.workspace;
  chip.style.display="";
  chip.style.color="var(--accent)";
  chip.title=email
    ? "Signed in as "+email
    : "Workspace: "+S.meta.workspace;


  document.getElementById("signOut").style.display = sso ? "" : "none";
}


function showSignIn(url, label){
  const btn=document.getElementById("signIn");
  if(!url){btn.style.display="none";return;}
  btn.href=url;
  btn.textContent = label ? ("Sign in with "+label) : "Sign in";
  btn.title="Sign in to access your saved builds and part library.";
  btn.style.display="";
}


function showAdminLink(isAdmin){
  document.getElementById("adminLink").style.display = isAdmin ? "" : "none";
}
showIdentity(S.meta.email, !!S.meta.email);


function showOffer(j){
  const bar=document.getElementById("offer");
  if(!bar || !j.sign_in_url || j.sso) return;
  let why=null;
  if(j.guest){
    why='<b>Guest preview.</b> Sign in to save builds and export files.';
  }else if(j.workspace && !j.email){
    why='<b>Shared workspace.</b> Anyone with this link can access this build. Sign in to use your account workspace.';
  }
  if(!why) return;
  let hidden=false;
  try{ hidden = localStorage.getItem("waamOffer")==="off"; }catch(e){}
  if(hidden) return;
  document.getElementById("offerText").innerHTML=why;
  document.getElementById("offerBtn").href=j.sign_in_url;
  bar.style.display="flex";
  document.getElementById("offerX").onclick=()=>{
    bar.style.display="none";


    try{ localStorage.setItem("waamOffer","off"); }catch(e){}
  };
}


const cum = [0]; let totalDep = 0;
const slider = document.getElementById("cursor");
function updateToolpath(){
  M=S.toolpath.moves; N=M.length;
  cum.length=1; totalDep=0;
  for(let i=1;i<N;i++){
    const a=M[i-1], b=M[i];
    if(b.d) totalDep+=Math.hypot(b.x-a.x,b.y-a.y,b.z-a.z);
    cum.push(totalDep);
  }
  slider.max=N-1;
}
updateToolpath();

let yaw=-0.85, pitch=0.32, zoom=1.45, panX=0, panY=0, cursor=0, playing=false;
let perspective=false, dolly=0;


let pcx=0,pcy=0,pcz=0,cx=0,cy=0,cz=0,span=1,partZoom=1;
let cellCenter=[0,0,0];
function updateBounds(){
  let bx0=Infinity,by0=Infinity,bz0=Infinity,bx1=-Infinity,by1=-Infinity,bz1=-Infinity;
  function grow(x,y,z){
    if(x<bx0)bx0=x; if(x>bx1)bx1=x;
    if(y<by0)by0=y; if(y>by1)by1=y;
    if(z<bz0)bz0=z; if(z>bz1)bz1=z;
  }
  for(const p of M) grow(p.x,p.y,p.z);
  const substrateMesh=S.scene?.meshes?.bead_test_coupon;
  if(substrateMesh) for(let i=0;i+2<substrateMesh.length;i+=3) grow(substrateMesh[i],substrateMesh[i+1],substrateMesh[i+2]);
  const partSpan=Math.max(bx1-bx0, by1-by0, bz1-bz0, 1);
  pcx=(bx0+bx1)/2; pcy=(by0+by1)/2; pcz=(bz0+bz1)/2;
  if(S.scene && S.scene.meshes){
    for(const k in S.scene.meshes){
      const a=S.scene.meshes[k];
      for(let i=0;i+2<a.length;i+=3) grow(a[i],a[i+1],a[i+2]);
    }
  }
  if(S.part && S.part.tris){
    const a=S.part.tris;
    for(let i=0;i+2<a.length;i+=3) grow(a[i],a[i+1],a[i+2]);
  }
  if(S.robot && S.robot.skeleton){
    for(const sk of S.robot.skeleton){
      if(!sk) continue;
      for(const p of sk) grow(p[0],p[1],p[2]);
    }
  }
  cx=(bx0+bx1)/2; cy=(by0+by1)/2; cz=(bz0+bz1)/2;
  cellCenter=[cx,cy,cz];
  span=Math.max(bx1-bx0, by1-by0, bz1-bz0, 1);

  partZoom=Math.max(1, span/partSpan);
}
updateBounds();

function cameraScale(){
  return zoom*Math.min(canvas.width,canvas.height)*0.62/span;
}
function cameraEyeDistance(){

  return span/(2*Math.tan(25*Math.PI/180)*zoom*.62);
}
function proj(p){
  const x=p.x-cx, y=p.y-cy, z=p.z-cz;
  const x1=x*Math.cos(yaw)-y*Math.sin(yaw), y1=x*Math.sin(yaw)+y*Math.cos(yaw);
  const z2=y1*Math.sin(pitch)+z*Math.cos(pitch);
  const depth=y1*Math.cos(pitch)-z*Math.sin(pitch);
  let s=cameraScale();
  if(perspective){
    const eyeDist=cameraEyeDistance(), distance=eyeDist+depth-dolly;
    const near=Math.max(span*1e-6,1e-5,Math.min(span*.001,Math.abs(eyeDist-dolly)*.001));

    if(distance<near) return [NaN,NaN,0,depth];
    s*=eyeDist/distance;
  }
  return [canvas.width/2+panX+x1*s, canvas.height/2+panY-z2*s, s, depth];
}
const lcol = l => `hsl(${208-166*(S.toolpath.layers>1?l/(S.toolpath.layers-1):0)} 78% 60%)`;

function drawSeg(a,b,style,w,dash){
  const [ax,ay,as]=proj(a),[bx,by,bs]=proj(b);
  if(!as || !bs) return;
  ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(bx,by);
  ctx.strokeStyle=style; ctx.lineWidth=w; ctx.setLineDash(dash||[]); ctx.stroke();
  ctx.setLineDash([]);
}
function drawTorch(p){

  const axis=S.torch.axis||[0,0,1], event=selectedEvent();
  const commandZ=p.z+(S.torch.command_from_physical_z_mm||0);
  const row=collisionData.byMove.get(event?event.v.move:Math.ceil(cursor));
  ctx.save(); ctx.fillStyle=row?row.worst.color:"#248fbb"; ctx.globalAlpha=.08;
  for(const [z,r] of S.torch.spheres||[]){
    const [x,y,scale]=proj({x:p.x+z*axis[0],y:p.y+z*axis[1],z:commandZ+z*axis[2]});
    if(!scale) continue;
    ctx.beginPath();ctx.arc(x,y,r*scale,0,2*Math.PI);ctx.fill();
  }
  ctx.restore();
}
const renderStyle=document.getElementById("renderStyle");
const partToggleRow=document.getElementById("partToggleRow");
const showPart=document.getElementById("showPart");
const showFumeHood=document.getElementById("showFumeHood");
try{showFumeHood.checked=localStorage.getItem("waamFumeHood")!=="off";}catch(e){}


function addFace(faces,p0,p1,p2,pal,edge,twoSided){
  const s0=proj(p0),s1=proj(p1),s2=proj(p2);
  if(!s0[2] || !s1[2] || !s2[2]) return;
  const ax=s1[0]-s0[0],ay=s1[1]-s0[1],bx=s2[0]-s0[0],by=s2[1]-s0[1];


  if(ax*by-ay*bx>=0 && !twoSided && !perspective) return;
  const ux=p1.x-p0.x,uy=p1.y-p0.y,uz=p1.z-p0.z;
  const vx=p2.x-p0.x,vy=p2.y-p0.y,vz=p2.z-p0.z;
  let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
  const nn=Math.hypot(nx,ny,nz)||1; nx/=nn;ny/=nn;nz/=nn;
  const lit=.28+.72*Math.abs(nx*-.25+ny*-.42+nz*.87);
  const light=Math.round(pal[2]+pal[3]*lit);
  faces.push({p:[s0,s1,s2],z:(s0[3]+s1[3]+s2[3])/3,edge:edge,
              fill:`hsla(${pal[0]} ${pal[1]}% ${light}% / ${pal[4]})`});
}
function paintFaces(faces){


  faces.sort((a,b)=>b.z-a.z);
  for(const f of faces){
    ctx.beginPath();ctx.moveTo(f.p[0][0],f.p[0][1]);
    ctx.lineTo(f.p[1][0],f.p[1][1]);ctx.lineTo(f.p[2][0],f.p[2][1]);ctx.closePath();
    ctx.fillStyle=f.fill;ctx.fill();
    if(f.edge){ctx.strokeStyle=f.edge;ctx.lineWidth=.35;ctx.stroke();}
  }
}
function wireFace(p0,p1,p2,twoSided){
  const s0=proj(p0),s1=proj(p1),s2=proj(p2);
  if(!s0[2] || !s1[2] || !s2[2]) return;
  const ax=s1[0]-s0[0],ay=s1[1]-s0[1],bx=s2[0]-s0[0],by=s2[1]-s0[1];
  if(ax*by-ay*bx>=0 && !twoSided && !perspective) return;
  ctx.moveTo(s0[0],s0[1]);ctx.lineTo(s1[0],s1[1]);ctx.lineTo(s2[0],s2[1]);ctx.closePath();
}
const SCENE_PAL = {
  mh185_headstock: [206, 14, 40, 30, 1.0],
  charles_platter: [ 32, 16, 42, 28, 1.0],
  bead_test_coupon: [205, 25, 52, 24, 1.0],
  robot_pedestal:  [210,  8, 30, 22, 1.0],
  cell_base:       [210,  8, 20, 22, 1.0],
  reamer_station:  [210,  8, 30, 22, 1.0],
  yrc1000_cabinet: [212, 10, 27, 20, 1.0],
  lincoln_r450:    [355, 55, 30, 26, 1.0],
  equipment_rack:  [210, 12, 35, 25, 1.0],
  power_disconnect: [210, 10, 38, 25, 1.0],
  waam_fixture:    [32, 25, 44, 28, 1.0],
  fume_hood:       [205, 10, 24, 18, 0.85],
  cell_frame_v2:   [212, 12, 22, 16, 0.75],
  cell_walls:      [210,  8, 18, 12, 0.35],
  hc_table:        [210, 10, 38, 26, 1.0],
  cell_panels:     [210,  5, 10, 10, 1.0],
  hc_back_panel:   [210,  5, 10, 10, 1.0],
  hc_cleaner:      [210,  8, 46, 24, 1.0],
  lc_cleaner:      [210,  8, 46, 24, 1.0],
  lc_cleaner_post: [204,  8, 23, 18, 1.0],
  lc_cleaner_mount: [206, 9, 29, 20, 1.0],
  lc_cleaner_foot: [206, 9, 29, 20, 1.0],
  lc_cleaner_base: [208, 10, 18, 16, 1.0],
  _default:        [216, 14, 30, 24, 0.95],
};
function drawSceneMeshes(faces){
  if(!S.scene.meshes) return;
  const solid=!!faces;
  if(!solid) ctx.beginPath();
  for(const name in S.scene.meshes){
    if(lightPreview() && name!=="bead_test_coupon") continue;
    if((name==="cell_walls" || name==="cell_panels") && !showWalls.checked) continue;
    if((name==="fume_hood" || name==="hc_fume_extraction") && !showFumeHood.checked) continue;
    if(name==="cell_frame_v2" && !showFrame.checked) continue;


    const twoSided = name!=="cell_walls" && name!=="cell_panels" && name!=="cell_frame_v2" && name!=="waam_fixture";
    const T=S.scene.meshes[name];


    const pal = SCENE_PAL[name] || SCENE_PAL._default;
    const edge = name==="waam_fixture" ? null : "rgba(150,168,190,.16)";
    for(let t=0;t<T.length;t+=9){
      const p0={x:T[t],y:T[t+1],z:T[t+2]},p1={x:T[t+3],y:T[t+4],z:T[t+5]},p2={x:T[t+6],y:T[t+7],z:T[t+8]};
      solid?addFace(faces,p0,p1,p2,pal,edge,twoSided)
           :wireFace(p0,p1,p2,twoSided);
    }
  }
  if(!solid){ctx.strokeStyle="rgba(118,132,158,0.30)";ctx.lineWidth=.7;ctx.stroke();}
}
function drawPartMesh(faces){
  if(lightPreview()) return;
  if(!S.part||!S.part.tris||!showPart.checked) return;
  const T=S.part.tris,solid=!!faces,draft=window.waamPlacement?.preview();
  const point=i=>{const p={x:T[i],y:T[i+1],z:T[i+2]};return draft?window.WaamPlacement.transform(p,draft):p;};
  if(!solid) ctx.beginPath();
  for(let t=0;t<T.length;t+=9){
    const p0=point(t),p1=point(t+3),p2=point(t+6);
    solid?addFace(faces,p0,p1,p2,[34,66,24,34,.68],"rgba(255,190,75,.13)"):wireFace(p0,p1,p2);
  }
  if(!solid){ctx.strokeStyle="rgba(255,184,78,.45)";ctx.lineWidth=.65;ctx.stroke();}
}

function drawSceneReferences(){

  if(!lightPreview()) return;
  ctx.strokeStyle="rgba(58,70,88,.55)";ctx.lineWidth=1;
  for(const b of S.scene.reference_boxes||[]){
    if(b.name.startsWith("hc_curtain_")&&!showWalls.checked) continue;
    if(b.name==="hc_fume_extraction"&&!showFumeHood.checked) continue;
    const corners=[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]].map(k=>
      proj({x:k[0]?b.max[0]:b.min[0],y:k[1]?b.max[1]:b.min[1],z:k[2]?b.max[2]:b.min[2]}));
    ctx.beginPath();
    for(const [i,j] of [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]]){
      if(!corners[i][2] || !corners[j][2]) continue;
      ctx.moveTo(corners[i][0],corners[i][1]);ctx.lineTo(corners[j][0],corners[j][1]);
    }
    ctx.stroke();
  }
}
function drawSceneBoxes(){


  if(!showBoxes.checked) return;
  ctx.strokeStyle="#3a4658"; ctx.lineWidth=1;
  for(const b of S.scene.boxes){
    const c=[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]].map(k=>
      proj({x:k[0]?b.max[0]:b.min[0], y:k[1]?b.max[1]:b.min[1], z:k[2]?b.max[2]:b.min[2]}));
    for(const [i,j] of [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]]){
      if(!c[i][2] || !c[j][2]) continue;
      ctx.beginPath(); ctx.moveTo(c[i][0],c[i][1]); ctx.lineTo(c[j][0],c[j][1]); ctx.stroke();
    }
  }
  for(const cp of S.scene.capsules){
    const ring=[];
    for(let a=0;a<=36;a++){
      const th=a/36*2*Math.PI;
      ring.push(proj({x:cp.p1[0]+cp.r*Math.cos(th), y:cp.p1[1]+cp.r*Math.sin(th), z:cp.p1[2]}));
    }
    if(ring.some(q=>!q[2])) continue;
    ctx.beginPath(); ctx.moveTo(ring[0][0],ring[0][1]);
    for(const q of ring) ctx.lineTo(q[0],q[1]);
    ctx.fillStyle="rgba(70,84,104,0.25)"; ctx.fill(); ctx.stroke();
  }
}
let ROB=null;
gpu=!lightPreview() && window.WaamRenderer ? window.WaamRenderer.create({canvas:document.getElementById("gl"),state:S}) : null;
const showConduit=document.getElementById("showConduit");
function updateDisplay(){
  ROB=S.robot && S.robot.skeleton ? S.robot : null;
  partToggleRow.style.display=S.part && S.part.tris && S.part.tris.length ? "" : "none";
  document.getElementById("robotToggleRow").style.display=ROB ? "" : "none";
  document.getElementById("conduitToggleRow").style.display=
    ROB && ROB.conduits && ROB.conduits.length ? "" : "none";
  const note=document.getElementById("toolDisplayNote");
  note.textContent=((ROB && ROB.tool_display)||"")+
    (lightPreview() && S.scene.reference_boxes?.length?" Light mode shows component extents only.":"");
  note.style.display=note.textContent ? "" : "none";
  showTorch.disabled=S.torch.available===false;
  showTorch.title=showTorch.disabled?"Cobot tool registration is pending":"";
}
updateDisplay();

function rotAxis(a, deg){
  const t=deg*Math.PI/180, c=Math.cos(t), s=Math.sin(t);
  const n=Math.hypot(a[0],a[1],a[2]), x=a[0]/n, y=a[1]/n, z=a[2]/n, C=1-c;
  return [c+x*x*C, x*y*C-z*s, x*z*C+y*s,
          y*x*C+z*s, c+y*y*C, y*z*C-x*s,
          z*x*C-y*s, z*y*C+x*s, c+z*z*C];
}
function matMul(A,B){
  const R=new Array(9);
  for(let r=0;r<3;r++)for(let c=0;c<3;c++)
    R[r*3+c]=A[r*3]*B[c]+A[r*3+1]*B[3+c]+A[r*3+2]*B[6+c];
  return R;
}
function fkFrames(q){
  let R=[1,0,0,0,1,0,0,0,1], p=[0,0,0];
  const frames=[{R:R,p:p}];
  for(let i=0;i<ROB.chain.length;i++){
    const j=ROB.chain[i], o=j.origin;
    p=[p[0]+R[0]*o[0]+R[1]*o[1]+R[2]*o[2],
       p[1]+R[3]*o[0]+R[4]*o[1]+R[5]*o[2],
       p[2]+R[6]*o[0]+R[7]*o[1]+R[8]*o[2]];
    R=matMul(R, rotAxis(j.axis, q[i]));
    frames.push({R:R,p:p});
  }
  return frames;
}
const LINK_ORDER=["base_link","link_1_s","link_2_l","link_3_u","link_4_r","link_5_b","link_6_t"];
function conduitCurve(spec,frames){
  const p=spec.controls.map(c=>{
    const f=frames[c.link],R=f.R,v=c.point;
    return [f.p[0]+R[0]*v[0]+R[1]*v[1]+R[2]*v[2],
            f.p[1]+R[3]*v[0]+R[4]*v[1]+R[5]*v[2],
            f.p[2]+R[6]*v[0]+R[7]*v[1]+R[8]*v[2]];
  });
  const points=[];
  for(let i=0;i<=spec.samples;i++){
    const t=i/spec.samples,s=1-t;
    points.push([0,1,2].map(k=>s*s*s*p[0][k]+3*s*s*t*p[1][k]+3*s*t*t*p[2][k]+t*t*t*p[3][k]));
  }
  return points;
}
function drawWireConduits(frames,faces){
  if(!showConduit.checked || !ROB.conduits) return;
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  for(const spec of ROB.conduits){
    const points=conduitCurve(spec,frames),bo=ROB.base_offset;
    for(let i=0;i<points.length-1;i++){
      const a=points[i],b=points[i+1],delta=b.map((v,k)=>v-a[k]),len=Math.hypot(...delta);
      if(len<1e-6) continue;
      const d=delta.map(v=>v/len),ref=Math.abs(d[2])<.9?[0,0,1]:[1,0,0];
      let u=cross(d,ref); const un=Math.hypot(...u); u=u.map(v=>v/un);
      const v=cross(d,u),rings=[a,b].map(p=>Array.from({length:8},(_,j)=>{
        const th=j*Math.PI/4,pt=p.map((x,k)=>x+bo[k]+spec.radius*(Math.cos(th)*u[k]+Math.sin(th)*v[k]));
        return {x:pt[0],y:pt[1],z:pt[2]};
      }));
      for(let j=0;j<8;j++){
        const n=(j+1)%8;
        for(const tri of [[rings[0][j],rings[0][n],rings[1][n]],
                          [rings[0][j],rings[1][n],rings[1][j]]]){
          faces?addFace(faces,...tri,[210,8,16,20,1],null):wireFace(...tri);
        }
      }
    }
  }
}
let meshCache={q:null, paths:null};
const cadPalettes=new Map();
function cadPalette(color){
  if(cadPalettes.has(color)) return cadPalettes.get(color);
  const n=parseInt(color.slice(1),16),r=(n>>16&255)/255,g=(n>>8&255)/255,b=(n&255)/255;
  const hi=Math.max(r,g,b),lo=Math.min(r,g,b),d=hi-lo,l=(hi+lo)/2;
  let h=0,s=0;
  if(d){
    s=d/(1-Math.abs(2*l-1));
    h=hi===r?((g-b)/d+6)%6:hi===g?(b-r)/d+2:(r-g)/d+4;
  }
  const pal=[h*60,s*100,l*60,l*40,1];
  cadPalettes.set(color,pal); return pal;
}
function drawRobotMesh(q,faces){
  if(lightPreview()) return false;
  if(!ROB.meshes) return false;
  const bo=ROB.base_offset, frames=fkFrames(q);
  const solid=!!faces;
  if(!solid) ctx.beginPath();
  for(let li=0;li<LINK_ORDER.length;li++){
    const m=ROB.meshes[LINK_ORDER[li]]; if(!m) continue;
    const f=frames[li], R=f.R, fp=f.p, off=m.offset, T=m.tris;
    for(let t=0;t<T.length;t+=9){
      const world=[];
      for(let v=0;v<3;v++){
        const lx=T[t+v*3]+off[0], ly=T[t+v*3+1]+off[1], lz=T[t+v*3+2]+off[2];
        const wx=fp[0]+R[0]*lx+R[1]*ly+R[2]*lz+bo[0],
              wy=fp[1]+R[3]*lx+R[4]*ly+R[5]*lz+bo[1],
              wz=fp[2]+R[6]*lx+R[7]*ly+R[8]*lz+bo[2];
        world.push({x:wx,y:wy,z:wz});
      }
      let pal=S.meta.cell_profile?.id==="hc10"?[45,12,55,35,1]:[214,45,22,33,.96];
      if(m.groups){
        const ti=t/9,g=m.groups.find(x=>ti>=x.start&&ti<x.end);
        if(g&&g.kind==="feeder") pal=[2,66,22,32,.98];
        else if(g&&g.kind==="bracket") pal=[220,9,15,24,.98];
        else if(g&&g.kind==="spool") pal=[38,44,18,26,.98];
        else if(g&&g.kind==="spool_housing") pal=[215,8,12,20,1];
        else if(g&&g.kind==="spool_support") pal=[215,10,18,24,1];
        else if(g&&g.kind==="spool_hub") pal=[212,9,40,23,1];
        else if(g&&g.kind==="torch") pal=[213,12,18,29,.98];
        else if(g&&g.kind==="tcp") pal=[42,90,55,25,1.0];
        if(g&&g.appearance&&g.appearance.color) pal=cadPalette(g.appearance.color);
      }
      solid?addFace(faces,world[0],world[1],world[2],pal,"rgba(135,175,225,.10)")
           :wireFace(world[0],world[1],world[2]);
    }
  }
  drawWireConduits(frames,faces);
  if(!solid){ctx.strokeStyle="rgba(96,140,205,0.30)";ctx.lineWidth=.7;ctx.stroke();}
  return true;
}
function robotFlangePoint(point,frame,base){
  const R=frame.R;
  return [0,1,2].map(k=>frame.p[k]+base[k]+R[k*3]*point[0]+R[k*3+1]*point[1]+R[k*3+2]*point[2]);
}
function drawTorchMesh(faces,pose){
  if(lightPreview() || !ROB) return;
  const local=ROB.torch_mesh_flange,mesh=local||ROB.torch_mesh;
  if(!mesh) return;
  const frame=local && pose && pose.q && ROB.chain ? fkFrames(pose.q)[6] : null;
  if(local && !frame) return;
  const solid=!!faces, pal=[209,10,34,32,.98];
  if(!solid) ctx.beginPath();
  for(const tri of mesh){
    const world=frame ? tri.map(p=>robotFlangePoint(p,frame,ROB.base_offset)) : tri;
    const w0={x:world[0][0],y:world[0][1],z:world[0][2]},
          w1={x:world[1][0],y:world[1][1],z:world[1][2]},
          w2={x:world[2][0],y:world[2][1],z:world[2][2]};
    solid?addFace(faces,w0,w1,w2,pal,"rgba(135,175,225,.10)",true)
         :wireFace(w0,w1,w2,true);
  }
  if(!solid){ctx.strokeStyle="rgba(96,140,205,0.30)";ctx.lineWidth=.7;ctx.stroke();}
}
function drawTcpMarker(pose){
  if(!ROB || !showRobot.checked) return;
  let point=ROB.tcp_marker;
  if(ROB.tcp_flange){
    if(!pose || !pose.q || !ROB.chain) return;
    point=robotFlangePoint(ROB.tcp_flange,fkFrames(pose.q)[6],ROB.base_offset);
  }
  if(!point) return;
  const [x,y,scale]=proj({x:point[0],y:point[1],z:point[2]});
  if(!scale) return;
  ctx.save();ctx.fillStyle="#d39c47";ctx.beginPath();ctx.arc(x,y,3,0,2*Math.PI);ctx.fill();ctx.restore();
}


function lerpPose(A, B, f){
  if(!A) return B; if(!B) return A;
  const out=[];
  for(let i=0;i<A.length;i++){
    const a=A[i], b=B[i];
    if(typeof a==="number" && typeof b==="number"){ out.push(a+(b-a)*f); }
    else if(Array.isArray(a) && Array.isArray(b)){
      out.push(a.map((v,j)=>v+(b[j]-v)*f));
    } else { out.push(b); }
  }
  return out;
}
function robotPose(cur){
  const n = ROB.skeleton.length;
  const i0 = Math.max(0, Math.min(n-1, Math.floor(cur)));
  const i1 = Math.min(n-1, i0+1);
  const f  = Math.max(0, Math.min(1, cur - i0));
  const skA = ROB.skeleton[i0], skB = ROB.skeleton[i1];
  const sk = (skA && skB) ? lerpPose(skA, skB, f) : (skA || skB);
  if(!sk) return null;
  let q = null;
  if(ROB.joints){
    const qA = ROB.joints[Math.min(i0, ROB.joints.length-1)];
    const qB = ROB.joints[Math.min(i1, ROB.joints.length-1)];
    q = (qA && qB) ? lerpPose(qA, qB, f) : (qA || qB);
  }
  return {sk:sk, q:q};
}


function drawRobotSpine(sk, meshed){
  const widths=meshed ? [2.5,3,2.8,2.5,2.2,1.8,1.4] : [5,7,6.5,5.5,4.5,3.5,2.5];
  const col = meshed ? "rgba(96,140,205,0.35)" : "rgba(96,140,205,0.55)";
  for(let i=1;i<sk.length;i++){
    const a={x:sk[i-1][0],y:sk[i-1][1],z:sk[i-1][2]}, b={x:sk[i][0],y:sk[i][1],z:sk[i][2]};
    drawSeg(a,b,col,widths[i-1]||3);
  }
  if(!meshed) for(let i=1;i<sk.length-1;i++){
    const [x,y,scale]=proj({x:sk[i][0],y:sk[i][1],z:sk[i][2]});
    if(!scale) continue;
    ctx.beginPath(); ctx.arc(x,y,3.2,0,2*Math.PI);
    ctx.fillStyle="rgba(140,175,225,0.8)"; ctx.fill();
  }
}
function drawSubstrateCorners(){
  if(!showScene.checked || !S.meta.substrate || UI.step!=="place") return;
  const origin=S.meta.substrate.origin_robot_mm,dpr=window.devicePixelRatio||1;
  ctx.save();ctx.font=`${12*dpr}px Segoe UI`;ctx.textAlign="center";ctx.textBaseline="middle";
  S.meta.substrate.points_robot_mm.forEach((p,i)=>{
    const [x,y,scale]=proj({x:p[0]-origin[0],y:p[1]-origin[1],z:p[2]-origin[2]});
    if(!scale) return;
    ctx.beginPath();ctx.arc(x,y,10*dpr,0,Math.PI*2);ctx.fillStyle="#173f57";ctx.fill();
    ctx.strokeStyle="#8ed9ff";ctx.stroke();ctx.fillStyle="#fff";ctx.fillText(String(i+1),x,y);
  });
  ctx.restore();
}


function drawScaleBars(placed){
  const out=document.getElementById("placeTape");
  const f=UI.step==="place" && window.WaamScale ? window.WaamScale.frame(S) : null;
  let e=null;
  if(f){
    const pts=[];
    for(let i=1;i<N;i++) if(M[i].d){const a=placed(M[i-1]),b=placed(M[i]);pts.push([a.x,a.y,a.z],[b.x,b.y,b.z]);}
    e=window.WaamScale.extents(pts,f);
    if(showScene.checked && showScale.checked) window.WaamScale.draw(ctx,proj,f,e,{dpr:window.devicePixelRatio||1});
  }
  const text=f?window.WaamScale.summary(f,e):"";
  if(out.textContent!==text) out.textContent=text;
  out.hidden=!text;
}
function draw(){
  updateProjectionControl();
  document.getElementById("gl").style.visibility=gpu && gpu.active ? "visible" : "hidden";
  const dpr=window.devicePixelRatio||1, view=document.getElementById("view");
  canvas.width=view.clientWidth*dpr; canvas.height=view.clientHeight*dpr;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const showT=showTravel.checked, i0=Math.floor(cursor);
  const draft=window.waamPlacement?.preview();
  const placementFocus=UI.step==="place" && UI.view==="part";
  const placed=p=>draft?window.WaamPlacement.transform(p,draft):p;
  if(!N) return;
  if(gpu){
    const pose=ROB ? robotPose(cursor) : null;
    const a=M[i0], b=M[Math.min(i0+1,N-1)], f=cursor-i0;
    const event=selectedEvent();
    const tip={x:a.x+(b.x-a.x)*f,y:a.y+(b.y-a.y)*f,z:a.z+(b.z-a.z)*f,d:b.d,l:b.l};
    if(event && event.point) Object.assign(tip,{x:event.v.x,y:event.v.y,z:event.v.z});
    const rendered=gpu.render({width:canvas.width,height:canvas.height,dpr,
      center:[cx,cy,cz],cellCenter,span,yaw,pitch,zoom,panX,panY,perspective,dolly,cursor,pose,
      placementPreview:draft,
      checkedPoint:event && event.point ? tip : null,
      violationMove:event ? event.v.move : Math.ceil(cursor),
      frames:pose&&pose.q&&ROB.chain?fkFrames(pose.q):null,
      flags:{scene:showScene.checked,robot:showRobot.checked && !placementFocus,part:showPart.checked,
        walls:showWalls.checked,frame:showFrame.checked,fumeHood:showFumeHood.checked,conduits:showConduit.checked,
        boxes:showBoxes.checked,travel:showTravel.checked,ghost:showGhost.checked,
        torch:showTorch.checked && !placementFocus && S.torch.available!==false},
      style:renderStyle.value});
    if(rendered){updatePanels(tip,i0);drawScaleBars(placed);drawSubstrateCorners();window.waamPlacement?.draw(ctx); return;}
  }


  const faces = renderStyle.value==="solid" ? [] : null;
  let pose=null, meshed=false;
  if(showScene.checked) drawSceneMeshes(faces);
  if(ROB && showRobot.checked && !placementFocus){
    pose = robotPose(cursor);
    if(pose && pose.q && ROB.chain) meshed = drawRobotMesh(pose.q, faces);
    drawTorchMesh(faces,pose);
  }
  drawPartMesh(faces);
  if(faces) paintFaces(faces);
  if(ROB && showRobot.checked && !placementFocus) drawTcpMarker(pose);
  if(showScene.checked){drawSceneReferences();drawSceneBoxes();}
  drawScaleBars(placed);
  drawSubstrateCorners();
  if(pose) drawRobotSpine(pose.sk, meshed);
  if(showGhost.checked) for(let i=Math.max(i0+1,1);i<N;i++){
    if(M[i].d) drawSeg(placed(M[i-1]),placed(M[i]),draft?"#226fac":"rgba(110,125,150,0.18)",1.2);
  }
  for(let i=1;i<=i0;i++){
    const b=M[i];
    if(b.d) drawSeg(placed(M[i-1]),placed(b),draft?"#226fac":collisionData.segments.has(i)?collisionData.segments.get(i).color:lcol(b.l),2.6);
    else if(showT) drawSeg(placed(M[i-1]),placed(b),"#49556a",1,[5,4]);
  }
  let tip=M[i0];
  if(i0<N-1){
    const f=cursor-i0, a=M[i0], b=M[i0+1];
    tip={x:a.x+(b.x-a.x)*f, y:a.y+(b.y-a.y)*f, z:a.z+(b.z-a.z)*f, d:b.d, l:b.l};
    if(b.d) drawSeg(placed(a),placed(tip),draft?"#226fac":collisionData.segments.has(i0+1)?collisionData.segments.get(i0+1).color:lcol(b.l),2.6); else if(showT) drawSeg(placed(a),placed(tip),"#49556a",1,[5,4]);
  }
  for(const event of draft?[]:collisionData.events){
    const v=event.v;
    if(event.point){
      const [x,y,scale]=proj(v);
      if(!scale) continue;
      ctx.strokeStyle=event.color; ctx.lineWidth=2.2;
      ctx.beginPath(); ctx.moveTo(x-6,y-6); ctx.lineTo(x+6,y+6);
      ctx.moveTo(x-6,y+6); ctx.lineTo(x+6,y-6); ctx.stroke();
    }
  }
  const event=selectedEvent();
  if(event && event.point) tip={...tip,x:event.v.x,y:event.v.y,z:event.v.z};
  if(showTorch.checked && !draft && !placementFocus && S.torch.available!==false) drawTorch(tip);
  updatePanels(tip, i0);
  window.waamPlacement?.draw(ctx);
}
function fmt(v,unit,dec){ return v==null ? "Not available" : v.toFixed(dec===undefined?0:dec)+" "+unit; }
function updatePanels(tip,i0){
  updateCollisionStatus();
  layerNow.textContent = tip.l;
  document.getElementById("layerReadout").textContent="Layer "+(tip.l+1)+" / "+S.toolpath.layers;
  slider.setAttribute("aria-valuetext","Move "+i0+", layer "+tip.l);
  depNow.textContent = cum[i0].toFixed(0)+" / "+totalDep.toFixed(0)+" mm";
  moveNow.textContent = i0+" / "+(N-1);
  let ro = "X "+tip.x.toFixed(1)+"  Y "+tip.y.toFixed(1)+"  Z "+tip.z.toFixed(1);
  if(ROB && ROB.joints){
    const q = ROB.joints[Math.min(i0, ROB.joints.length-1)];
    ro += q ? "   |   S "+q[0].toFixed(0)+" L "+q[1].toFixed(0)+" U "+q[2].toFixed(0)+
              " R "+q[3].toFixed(0)+" B "+q[4].toFixed(0)+" T "+q[5].toFixed(0)
            : "   |   UNREACHABLE";
  }
  readout.textContent = ro;
  if(S.schedule){
    const p=S.schedule.per_layer[tip.l];
    paramRows.innerHTML = p ?
      '<div class="row"><span title="Controller wire-feed units">Wire feed</span><span class="v big">'+fmt(p.wfs,"")+'</span></div>'+
      '<div class="row"><span>Voltage</span><span class="v">'+fmt(p.volts,"V",1)+'</span></div>'+
      '<div class="row"><span>Dwell</span><span class="v">'+fmt(p.dwell_s,"s",1)+'</span></div>'+
      (p.asf!=null?'<div class="row"><span>ASF#</span><span class="v">'+p.asf+'</span></div>':"")
      : '<div class="empty">No schedule for layer '+tip.l+'</div>';
    drawChart(tip.l);
  }else{
    paramRows.innerHTML='<div class="empty">No schedule attached</div>';
    const chart=document.getElementById("paramChart");
    chart.getContext("2d").clearRect(0,0,chart.width,chart.height);
  }
}
function drawChart(layerNowIdx){
  const el=document.getElementById("paramChart"); if(!S.schedule) return;
  const dpr=window.devicePixelRatio||1;
  el.width=el.clientWidth*dpr; el.height=74*dpr;
  const g=el.getContext("2d"), P=S.schedule.per_layer, n=P.length;
  const series=[["wfs","#ffb84e"],["volts","#4ea1ff"]];
  for(const [key,color] of series){
    const vals=P.map(p=>p&&p[key]!=null?p[key]:null).filter(v=>v!=null);
    if(!vals.length) continue;
    const lo=Math.min(...vals), hi=Math.max(...vals), rng=(hi-lo)||1;
    g.beginPath(); let started=false;
    P.forEach((p,k)=>{
      if(!p||p[key]==null) return;
      const x=k/(n-1||1)*el.width, y=el.height-8*dpr-(p[key]-lo)/rng*(el.height-16*dpr);
      started?g.lineTo(x,y):g.moveTo(x,y); started=true;
    });
    g.strokeStyle=color; g.lineWidth=1.6*dpr; g.stroke();
  }
  const x=layerNowIdx/(n-1||1)*el.width;
  g.strokeStyle="#dde3ec55"; g.lineWidth=1*dpr;
  g.beginPath(); g.moveTo(x,0); g.lineTo(x,el.height); g.stroke();
}
window.waamStudio={
  state:S,
  setLive(channels){
    S.live={linked:true,channels:channels||[]};
    const i0=Math.floor(cursor);
    liveRows.innerHTML=(S.live.channels.length?S.live.channels.map(ch=>{
      const val=ch.per_move?ch.per_move[Math.min(i0,ch.per_move.length-1)]:ch.value;
      return '<div class="row"><span>'+ch.name+'</span><span class="v">'+
        (val==null?"Not available":val.toFixed(1))+' '+(ch.unit||"")+'</span></div>';
    }).join(""):'<div class="empty">Connected. No live measurements available.</div>');
  }
};
if(S.live && S.live.channels.length) window.waamStudio.setLive(S.live.channels);

const playBtn=document.getElementById("play");
function setPlaying(p){
  if(p && (UI.busy || window.waamPlacement?.pending() || window.waamSettings?.reducedMotion())) return;
  if(p) selectedCollision=null;
  if(p && cursor>=N-1){cursor=0;slider.value=0;}
  const reduced=window.waamSettings?.reducedMotion();
  playing=p; playBtn.innerHTML=reduced?"&#9197;":p?"&#10074;&#10074;":"&#9654;";
  playBtn.setAttribute("aria-label",reduced?"Next layer":p?"Pause toolpath":"Play toolpath");
  playBtn.title=reduced?"Next layer (Space)":"Play / pause (Space)";
}
function togglePlayback(){
  if(!window.waamSettings?.reducedMotion()){setPlaying(!playing);return;}
  if(UI.busy || window.waamPlacement?.pending() || !N) return;
  setPlaying(false);selectedCollision=null;
  const layer=M[Math.floor(cursor)]?.l;
  const next=M.findIndex((move,i)=>i>cursor && move.l!==layer);
  cursor=next<0?0:next;slider.value=cursor;draw();rememberView();
}
playBtn.onclick=togglePlayback;
slider.oninput=()=>{selectedCollision=null;cursor=+slider.value; setPlaying(false); draw();};
document.getElementById("speed").onchange=draw;
renderStyle.onchange=draw;


for(const cb of document.querySelectorAll('label.t input[type="checkbox"]')){
  cb.onchange=()=>{
    if(cb===showFumeHood){
      try{localStorage.setItem("waamFumeHood",cb.checked?"on":"off");}catch(e){}
    }
    draw();
  };
}
let last=0;
function tick(t){
  if(playing){
    const dt=(t-last)/1000, movesPerSec=+document.getElementById("speed").value/60;
    cursor=Math.min(N-1, cursor+dt*movesPerSec);
    if(cursor>=N-1) setPlaying(false);
    slider.value=Math.floor(cursor); draw();
  }
  last=t; requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function seekCollision(event){
  if(event.seek===null) return;
  setPlaying(false); cursor=event.seek; selectedCollision=event; slider.value=cursor;
  showTorch.checked=true; draw();
}
function updateCollisionStatus(){
  const el=document.getElementById("collisionNow"); if(!el) return;
  const event=selectedEvent(), move=event?event.v.move:Math.ceil(cursor), row=collisionData.byMove.get(move);
  el.style.color=row?row.worst.color:"";
  el.textContent=row ? "Move "+move+": "+row.events.length+" findings. "+
    row.worst.reason+"." : "Move "+move+": no clearance findings.";
}
function initializeCollisionUI(){
  const style=document.createElement("style");
  style.textContent="#collisionTriage{padding:12px;border:1px solid #637185;margin:8px 0;font-size:12px}"+
    "#collisionTriage p{margin:7px 0}#collisionEvents{display:grid;gap:5px}"+
    "#collisionEvents button{text-align:left;white-space:normal;padding:7px;background:#172130}"+
    "#collisionTrack{display:block;width:calc(100% - 16px);height:24px;margin:0 8px;cursor:pointer}"+
    "#collisionNav{display:flex;gap:8px;align-items:center;margin-top:8px}";
  document.head.appendChild(style);
  const wrap=document.createElement("div");wrap.style.cssText="flex:1;min-width:0;width:100%";
  slider.before(wrap);wrap.appendChild(slider);slider.style.width="100%";
  const track=document.createElement("canvas");track.id="collisionTrack";track.width=2048;track.height=24;
  track.setAttribute("role","img");track.setAttribute("aria-label","Clearance timeline. Select a finding in Review to inspect it.");
  wrap.appendChild(track);
  const panel=document.createElement("section");panel.id="collisionTriage";
  panel.setAttribute("aria-label","Collision details");
  panel.innerHTML='<strong>Collision details</strong><p id="collisionSummary"></p>'+
    '<details><summary>Color key</summary>'+
    '<p><span style="color:#f03435">Red: penetration</span><br><span style="color:#f0a629">Amber: proximity</span><br>'+
    '<span style="color:#be58ed">Purple: possible artifact, penetration</span><br>'+
    '<span style="color:#54a9ff">Blue: possible artifact, proximity</span></p>'+
    '<p>Artifact labels do not remove export blocks. Inspect each finding and verify the model.</p></details>'+
    '<details><summary>Model details</summary><p id="toolingGeometryNote"></p></details><p id="collisionNow" role="status"></p>'+
    '<div id="collisionEvents"></div><div id="collisionNav"></div>';
  document.getElementById("step-review").appendChild(panel);
  track.onclick=ev=>{
    const events=collisionData.events.filter(e=>e.seek!==null); if(!events.length) return;
    const rect=track.getBoundingClientRect(), move=Math.max(0,Math.min(1,(ev.clientX-rect.left)/rect.width))*Math.max(1,N-1);
    let lo=0,hi=events.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(events[mid].v.move<move)lo=mid+1;else hi=mid;}
    const right=events[Math.min(lo,events.length-1)],left=events[Math.max(0,lo-1)];
    seekCollision(Math.abs(left.v.move-move)<=Math.abs(right.v.move-move)?left:right);
  };
  renderCollisionUI();
}
function renderCollisionUI(){
  const list=document.getElementById("collisionEvents");if(!list) return;
  const preview=!!(S.meta.cell_profile && S.meta.cell_profile.planning_only);
  document.getElementById("collisionTriage").hidden=preview;
  document.getElementById("collisionTrack").style.display=preview?"none":"";
  if(preview) return;
  const events=collisionData.events, size=8, pages=Math.max(1,Math.ceil(events.length/size));
  collisionPage=Math.max(0,Math.min(collisionPage,pages-1));
  const artifacts=events.filter(e=>e.artifact).length;
  document.getElementById("collisionSummary").textContent=events.length ?
    events.length+" findings; "+artifacts+" possible model artifacts. Negative clearance indicates penetration." :
    (S.meta.clearance_checked===false?"Clearance has not been checked.":"No clearance findings.");
  document.getElementById("toolingGeometryNote").textContent=
    (window.WaamRenderer && window.WaamRenderer.collisionDisplay ? "Volumes show the bead-check envelope, scene-check spheres, and controller tool capsules. " :
      "Detailed collision volumes are unavailable. Reload the preview to retry. ")+
    (ROB && ROB.joints && ROB.chain ?
      "Tool orientation is interpolated between solved poses." :
      "Tool orientation is a reference; the clearance envelope includes wrist rotation.")+
    (gpu && gpu.active?"":" Basic preview shows scene-check spheres only.");
  list.replaceChildren();
  for(const event of events.slice(collisionPage*size,(collisionPage+1)*size)){
    const v=event.v, button=document.createElement("button");button.type="button";
    button.style.color=event.color;button.style.borderLeft="4px solid "+event.color;
    const depth=Number.isFinite(v.clearance)?v.clearance.toFixed(2):"unknown";
    button.textContent="Move "+v.move+" · "+v.obstacle+" · "+depth+" mm · "+
      (event.penetration?"penetration":"proximity")+" · "+event.reason+
      " · "+(v.blocking?"blocks":"nonblocking")+(event.seek===null?" · no path position":"");
    button.disabled=event.seek===null;button.onclick=()=>seekCollision(event);list.appendChild(button);
  }
  const nav=document.getElementById("collisionNav");nav.replaceChildren();
  for(const [label,delta] of [["Previous",-1],["Next",1]]){
    const button=document.createElement("button");button.type="button";button.textContent=label;
    button.disabled=delta<0?collisionPage===0:collisionPage===pages-1;
    button.onclick=()=>{collisionPage+=delta;renderCollisionUI();};nav.appendChild(button);
  }
  const page=document.createElement("span");page.textContent="Page "+(collisionPage+1)+" / "+pages;nav.appendChild(page);
  const track=document.getElementById("collisionTrack"), g=track.getContext("2d");g.clearRect(0,0,track.width,24);
  for(const event of events){
    if(event.seek===null) continue;
    const lane=(event.artifact?2:0)+(event.penetration?0:1);
    g.fillStyle=event.color;g.fillRect(event.v.move/Math.max(1,N-1)*(track.width-3),lane*6,3,5);
  }
  updateCollisionStatus();
}

let drag=null;
canvas.onpointerdown=e=>{
  if(drag) return;
  if(UI.busy) return;
  canvas.focus({preventScroll:true});
  if(window.waamPlacement?.down(e)) return;
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  drag={x:e.clientX,y:e.clientY,pan:e.shiftKey||e.button===1,id:e.pointerId};
};
canvas.onpointermove=e=>{
  if(window.waamPlacement?.move(e)) return;
  if(!drag || drag.id!==e.pointerId || UI.busy) return;
  const dx=e.clientX-drag.x, dy=e.clientY-drag.y;
  if(drag.pan) panCamera(dx*(window.devicePixelRatio||1),dy*(window.devicePixelRatio||1));
  else{yaw+=dx*0.008; pitch=Math.max(-1.55,Math.min(1.55,pitch+dy*0.008));}
  drag={x:e.clientX,y:e.clientY,pan:drag.pan,id:drag.id}; draw();
};
canvas.onpointerup=e=>{
  if(window.waamPlacement?.up(e)) return;
  if(drag && drag.id===e.pointerId){drag=null;rememberView();}
};
canvas.onpointercancel=e=>{if(drag?.id===e.pointerId) drag=null;window.waamPlacement?.cancel(true,e);};
canvas.onlostpointercapture=canvas.onpointercancel;
canvas.oncontextmenu=e=>e.preventDefault();
canvas.onwheel=e=>{
  e.preventDefault();if(UI.busy || !e.deltaY)return;
  window.waamPlacement?.cancel();drag=null;
  if(perspective){
    const unit=e.deltaMode===1?16:e.deltaMode===2?canvas.height/(window.devicePixelRatio||1):1;
    const delta=Math.max(-240,Math.min(240,e.deltaY*unit));
    dolly-=delta*cameraEyeDistance()*.001*(e.shiftKey ? .2 : 1);
  }else zoom=Math.max(.15,Math.min(200,zoom*(e.deltaY<0?1.12:0.89)));
  draw();rememberView();
};
window.onkeydown=e=>{
  if(document.getElementById("helpDlg").open) return;
  if(window.waamSettings?.isOpen()) return;
  if(document.getElementById("platterDlg").open) return;
  if(e.code==="Escape" && window.waamPlacement){
    if(window.waamPlacement.hasGesture())window.waamPlacement.cancel();else window.waamPlacement.revert();
    drag=null;return;
  }


  const sliceKey=e.key==="Enter" && !e.altKey && !e.shiftKey && !e.repeat && UI.step==="prepare" &&
    ((e.ctrlKey||e.metaKey) || (e.target.tagName==="INPUT" && e.target.type==="number" && !!e.target.closest("#step-prepare")));
  const slice=document.getElementById("sliceBtn");
  if(sliceKey && slice && !slice.disabled && S.meta.workbench){e.preventDefault();slice.onclick?.();return;}
  if(/^(INPUT|SELECT|TEXTAREA|BUTTON|SUMMARY|A)$/.test(e.target.tagName)) return;
  if(e.code==="Space"){e.preventDefault(); togglePlayback();}
  if(e.code==="ArrowRight"){selectedCollision=null;cursor=Math.min(N-1,cursor+1); slider.value=cursor; draw();}
  if(e.code==="ArrowLeft"){selectedCollision=null;cursor=Math.max(0,cursor-1); slider.value=cursor; draw();}
  if(e.code==="KeyF") frameView(UI.view==="cell"?"part":"cell");
  if(e.code==="Backslash"){e.preventDefault();window.waamWindow?.toggleInspector();}
};

function renderThermal(){
  const body=document.getElementById("thermalBody"), t=S.thermal;
  body.textContent="";
  const add=(tag,text,parent=body)=>{const el=document.createElement(tag);el.textContent=text;parent.appendChild(el);return el;};
  if(!t || t.status==="not_configured"){
    add("p","Simulation is off. Select a profile in Prepare to estimate cooling between passes.");
    return;
  }
  if(t.status==="unavailable"){
    add("strong","Thermal prediction unavailable");add("p",t.error);return;
  }
  const p=t.profile;
  add("strong",p.evidence==="illustrative"?"Example profile. Not calibrated to this cell.":"Uploaded profile. Cell validation required.");
  const info=add("details","");
  add("summary","Simulation details",info);
  add("p",p.name+" · "+p.provenance,info);
  add("p",p.heat_model==="goldak"?"Temperature model: Goldak heat source, conduction, latent heat and surface cooling.":"Temperature model: local Gaussian heat estimate.",info);
  add("p",`${t.completed_passes} / ${t.total_passes} passes accepted in simulation · ${t.cooling_s.toFixed(1)} s cooling · ${t.policy} policy.`);
  add("p",`Limits: width error ${p.max_width_error_mm.toFixed(2)} mm; accumulated height error ${p.max_height_error_mm.toFixed(2)} mm; support ${(100*p.min_support_fraction).toFixed(0)}%; reserve ${p.min_support_reserve.toFixed(2)}.`);
  if(t.heat && t.heat.model==="goldak_finite_volume"){
    const h=t.heat, audit=add("details","",info);
    add("summary","Heat calculation for accepted passes",audit);
    add("p",`${h.cells} cells · peak material temperature ${h.peak_material_c.toFixed(0)} °C. This peak differs from the temperature before the next pass.`,audit);
    add("p",`Absorbed ${(h.input_j/1000).toFixed(3)} kJ; retained ${(h.stored_j/1000).toFixed(3)} kJ; cooling losses ${(h.loss_j/1000).toFixed(3)} kJ; energy balance error ${h.balance_error_j.toExponential(1)} J.`,audit);
    if(h.source_capture_min!=null) add("p",`Source power sampled in material: ${(100*h.source_capture_min).toFixed(0)}–${(100*h.source_capture_max).toFixed(0)}% before normalization to absorbed power. This correction needs a grid and source-shape check.`,audit);
    add("p","The heat calculation uses nominal deposited geometry. Grid, deposition sampling, material properties and boundary conditions still require validation.",audit);
  }
  const labels={response_range:"temperature outside response data",support_coverage:"insufficient bead support",hot_support:"insufficient hot support reserve",width_error:"width error",height_error:"accumulated height error"};
  for(const row of t.passes){
    const detail=add("details",""), action=row.status==="cool"?`cool ${row.pause_s.toFixed(1)} s`:row.status==="stop"?"stop":"continue without cooling";
    add("summary",`Pass ${row.pass_index+1}: ${action} · ${row.peak_interpass_c.toFixed(0)} °C`,detail);
    const metric=(v,unit)=>v==null?"unknown":v.toFixed(2)+unit;
    add("p",`Width error ${metric(row.width_error_mm," mm")}; height error ${metric(row.height_error_mm," mm")}; support ${metric(row.support_fraction==null?null:row.support_fraction*100,"%")}; reserve ${metric(row.support_reserve,"")}.`,detail);
    if(row.trigger.reasons.length) add("p","Without cooling: "+row.trigger.reasons.map(k=>labels[k]||k).join(", ")+` at ${row.trigger.peak_interpass_c.toFixed(0)} °C.`,detail);
    if(row.status==="stop") add("p",t.policy==="continuous"?"Stopped at the first predicted failure; subsequent bead shape is unknown.":"No acceptable complete pass found within the cooling search. Existing deformation and unsupported geometry cannot be reset by a pause.",detail);
    const button=add("button","Show pass",detail);
    button.onclick=()=>{cursor=Math.min(N-1,row.start_move);slider.value=cursor;setPlaying(false);draw();};
  }
  add("p",t.limitations,info);
}
function renderArcParams(){
  renderThermal();
  const body=document.getElementById("arcBody");
  body.className=S.arc ? "" : "empty";
  body.textContent="Arc estimate unavailable. Check the material and slice settings, then slice again.";
  document.getElementById("arcWarn").textContent=(S.arc_warnings||[]).join("\\n");
  if(!S.arc) return;
  const a=S.arc;
  const row=(k,v)=>`<div class="row"><span>${k}</span><span class="v">${v}</span></div>`;
  body.innerHTML=
    row("Filler", a.filler)+
    row(a.beads_across>1?"Wire feed per bead":"Wire feed", a.wfs_m_min.toFixed(2)+" m/min")+
    row("", `<span class="mono" style="color:var(--dim)">${a.wfs_ipm.toFixed(0)} ipm</span>`)+
    (a.bead_area_mm2?row("Area per bead", a.bead_area_mm2.toFixed(2)+" mm²"):"")+
    (a.entered_wfs_ipm?row("Entered wire feed", a.entered_wfs_ipm.toFixed(0)+" ipm"):"")+
    (a.predicted_pitch_mm?row("Predicted layer pitch", a.predicted_pitch_mm.toFixed(2)+" mm"):"")+
    (a.free_crown_mm?row("Free bead crown (NOMINAL)", a.free_crown_mm.toFixed(2)+" mm")+
      row("Free bead pitch (NOMINAL)", a.free_pitch_mm.toFixed(2)+" mm"):"")+
    row("Deposition", a.deposition_kg_h.toFixed(2)+" kg/h")+
    row("Estimated current", a.amps_est.toFixed(0)+" A")+
    row("Estimated voltage", a.volts_est.toFixed(1)+" V")+
    row("Heat input", a.heat_kj_mm.toFixed(2)+" kJ/mm");
}
renderArcParams();

function renderState(next){
  window.waamPlacement?.reset();
  Object.assign(S,next);

  if(typeof collisionIndex === "function"){
    collisionData=collisionIndex(S); selectedCollision=null; collisionPage=0;
  }
  updateToolpath(); updateBounds(); updateDisplay();
  if(gpu) gpu.setState(S);
  cursor=0; slider.value=0; setPlaying(false); drag=null;
  document.title=S.meta.title+" | WAAM Studio";
  document.querySelector("header .title").textContent=S.meta.title;
  document.getElementById("cellChip").textContent=S.meta.cell||"No cell selected";
  renderClearance();
  if(typeof renderCollisionUI === "function") renderCollisionUI();
  renderArcParams(); frameView(UI.view);
  window.waamHelp?.refresh();
}

if(S.meta.workbench){
  document.getElementById("wb").style.display="block";
  for(const link of document.querySelectorAll("a[data-run-sheet]")) link.href=api("/run-sheet");
  const wbCellProfile=document.getElementById("wbCellProfile");
  const wbShape=document.getElementById("wbShape");
  const wbOx=document.getElementById("wbOx");
  const wbOy=document.getElementById("wbOy");
  const wbOz=document.getElementById("wbOz");
  const wbSubstrateRegistration=document.getElementById("wbSubstrateRegistration");
  const wbSub=document.getElementById("wbSub");
  const wbYaw=document.getElementById("wbYaw");
  const wbRotX=document.getElementById("wbRotX");
  const wbRotY=document.getElementById("wbRotY");
  const wbLayFlat=document.getElementById("wbLayFlat");
  const wbTilt=document.getElementById("wbTilt");
  const wbFixture=document.getElementById("wbFixture");
  const wbRepair=document.getElementById("wbRepair");
  const wbLayerSense=document.getElementById("wbLayerSense");
  const wbSubstrateMargin=document.getElementById("wbSubstrateMargin");
  const wbBeadLength=document.getElementById("wbBeadLength");
  const wbBeadCount=document.getElementById("wbBeadCount");
  const wbBeadPitch=document.getElementById("wbBeadPitch");
  const wbWallLength=document.getElementById("wbWallLength");
  const wbWallLayers=document.getElementById("wbWallLayers");
  const wbWallThickness=document.getElementById("wbWallThickness");
  const wbCopies=document.getElementById("wbCopies");
  const wbCopySpacing=document.getElementById("wbCopySpacing");
  const wbInterpass=document.getElementById("wbInterpass");
  const wbInterpassEvery=document.getElementById("wbInterpassEvery");
  const wbTempCheckEvery=document.getElementById("wbTempCheckEvery");
  const wbBeadDwell=document.getElementById("wbBeadDwell");
  const wbThermalTarget=document.getElementById("wbThermalTarget");
  const wbThermalUnit=document.getElementById("wbThermalUnit");
  const wbThermalPeak=document.getElementById("wbThermalPeak");
  const wbThermalAmbient=document.getElementById("wbThermalAmbient");
  const wbThermalModel=document.getElementById("wbThermalModel");
  const wbThermalCompute=document.getElementById("wbThermalCompute");
  const wbThermalApply=document.getElementById("wbThermalApply");
  let thermalPlan=null,thermalPlanInputs=null,thermalApplied=null,thermalRequestRevision=0;
  const wbMeasureEvery=document.getElementById("wbMeasureEvery");
  const wbMeasureTimes=document.getElementById("wbMeasureTimes");
  const wbMeasurePause=document.getElementById("wbMeasurePause");
  const wbMeasureClean=document.getElementById("wbMeasureClean");
  const wbMeasurePreWeld=document.getElementById("wbMeasurePreWeld");
  const wbSlicer=document.getElementById("wbSlicer");
  const wbHcCleaner=document.getElementById("wbHcCleaner");
  const wbLh=document.getElementById("wbLh");
  const wbSeam=document.getElementById("wbSeam");
  const wbBead=document.getElementById("wbBead");
  const wbMargin=document.getElementById("wbMargin");
  const wbUnits=document.getElementById("wbUnits");
  const wbContours=document.getElementById("wbContours");
  const wbStepover=document.getElementById("wbStepover");
  const wbInfill=document.getElementById("wbInfill");
  const wbInfillAngle=document.getElementById("wbInfillAngle");
  const wbInfillRotation=document.getElementById("wbInfillRotation");
  const wbTravelLift=document.getElementById("wbTravelLift");
  const wbFiller=document.getElementById("wbFiller");
  const wbWire=document.getElementById("wbWire");
  const wbTravel=document.getElementById("wbTravel");
  const wbWfs=document.getElementById("wbWfs");
  const wbCtwd=document.getElementById("wbCtwd");
  const wbCleanFirst=document.getElementById("wbCleanFirst");
  const wbThermal=document.getElementById("wbThermal");
  const wbCooling=document.getElementById("wbCooling");
  const wbThermalProfile=document.getElementById("wbThermalProfile");
  function syncSliceFeedback(error=false){
    const note=document.getElementById("sliceStatus");
    note.textContent=document.getElementById("wbMsg").textContent;
    note.dataset.state=error?"error":"info";
    note.setAttribute("role",error?"alert":"status");
  }
  function fieldFeedback(el,error){
    let note=document.getElementById(el.id+"Error");
    if(!note){
      note=document.createElement("div"); note.id=el.id+"Error";
      note.className="field-error"; note.setAttribute("aria-live","polite");
      (el.closest(".row")||el.parentElement).after(note);
      el.setAttribute("aria-describedby", ((el.getAttribute("aria-describedby")||"")+" "+note.id).trim());
    }
    el.setCustomValidity(error);
    el.setAttribute("aria-invalid",error?"true":"false");
    note.textContent=error;
    return error;
  }
  function numberError(el){
    if(["wbWfs","wbCtwd","wbMeasureEvery","wbThermalPeak"].includes(el.id)&&el.value.trim()===""&&!el.validity?.badInput) return fieldFeedback(el,"");
    const n=el.valueAsNumber;
    const bad=el.value.trim()==="" || !Number.isFinite(n) || n<Number(el.min) || n>Number(el.max)
      || (["wbArc","wbContours","wbBeadCount","wbCopies","wbWallLayers","wbInterpassEvery","wbTempCheckEvery","wbMeasureEvery"].includes(el.id) && !Number.isInteger(n))
      || (el.id==="wbBeadDwell" && n>0 && n<0.01);
    return fieldFeedback(el,bad?el.dataset.error:"");
  }
  UI.validateNumber=numberError;
  function measureTimesError(){
    const value=wbMeasureTimes.value.trim();
    if(!value)return fieldFeedback(wbMeasureTimes,"");
    const tokens=value.split(/[,\\s]+/);
    if(tokens.length>10)return fieldFeedback(wbMeasureTimes,"Reading times after weld: enter at most 10 values");
    const times=tokens.map(token=>/^[+-]?(?:\\d+\\.?\\d*|\\.\\d+)(?:e[+-]?\\d+)?$/i.test(token)?Number(token):NaN);
    if(times.some(n=>!Number.isFinite(n)||n<1||n>3600))
      return fieldFeedback(wbMeasureTimes,"Reading times after weld: enter seconds from 1 to 3600, separated by commas or spaces");
    return fieldFeedback(wbMeasureTimes,times.some((n,index)=>index>0&&n<=times[index-1])?
      "Reading times after weld: values must be strictly increasing":"");
  }
  function jobNameError(el){
    el.value=el.value.toUpperCase();
    const valid=new RegExp("^(?:"+el.pattern+")$").test(el.value);
    return fieldFeedback(el,valid?"":document.getElementById("wbBuildMode").value==="auto"?
      "Auto cycle base name: use 1–28 characters, start with A–Z or 0–9, then A–Z, 0–9, - or _":
      "Controller job name: use 1–32 characters, start with A–Z or 0–9, then A–Z, 0–9, - or _");
  }

  function jobFolder(){return document.getElementById("wbJobFolder").value.trim();}

  function arcSchedule(){return document.getElementById("wbArcSchedule").value.trim();}
  function scheduleAsfs(text){
    return [...new Set(text.split(";").map(e=>e.split(":")[1]).filter(v=>v&&v.trim()).map(v=>String(Number(v.trim()))))];
  }
  function arcQuery(){
    const schedule=arcSchedule();
    return schedule?"arc_schedule="+encodeURIComponent(schedule)
      :"arc="+encodeURIComponent(String(document.getElementById("wbArc").valueAsNumber));
  }
  function passSummary(r){
    const n=r.headers.get("X-Waam-Pass-Count"),asf=r.headers.get("X-Waam-Asf"),travel=r.headers.get("X-Waam-Travel");
    if(!n)return "";
    return " "+n+" pass"+(n==="1"?"":"es")+(asf?" · ASF#"+asf:"")+(travel?" · travel "+travel.replace("-","–")+" mm/min":"")+".";
  }
  function jobFolderError(el){
    const value=el.value.trim();
    const valid=value===""||new RegExp("^(?:"+el.pattern+")$").test(value);
    return fieldFeedback(el,valid?"":"Controller job folder: up to 32 letters, digits, - or _, as the pendant's JOB FOLDER LIST shows it");
  }
  function validateFields(fields,msg){
    const errors=[];
    UI.invalidField=null;
    for(const el of fields){
      const error=numberError(el);
      if(error){errors.push(error);if(!UI.invalidField) UI.invalidField=el;}
    }
    if(errors.length) msg.textContent=errors.join("\\n");
    return errors.length===0;
  }
  function validateEmit(needsArc){
    const errors=[];
    if(buildDraftError()) errors.push(buildDraftError());
    const registrationError=substrateRegistrationError();
    if(registrationError) errors.push(registrationError);
    if(S.meta.cell_profile && S.meta.cell_profile.planning_only){
      errors.push(S.meta.cell_profile.export_reason);
    }
    if(wbCellProfile.value!==((S.meta.params||{}).cell_profile||"lc50s")){
      errors.push("Slice the selected cell profile before exporting");
    }
    const nameError=jobNameError(document.getElementById("wbJbiName"));
    if(nameError) errors.push(nameError);
    for(const el of [wbInterpass,wbInterpassEvery,wbCtwd,wbTempCheckEvery,wbBeadDwell,wbMeasureEvery]){
      const error=numberError(el);
      if(error) errors.push(error);
    }
    const timesError=measureTimesError();
    if(timesError) errors.push(timesError);
    if(needsArc){
      const wbArc=document.getElementById("wbArc");
      const arcError=!arcSchedule()?numberError(wbArc)
        :fieldFeedback(wbArc,wbArc.value.trim()?"Give either one ASF# or an ASF# by layer schedule, not both":"");
      if(arcError) errors.push(arcError);
      const folderError=jobFolderError(document.getElementById("wbJobFolder"));
      if(folderError) errors.push(folderError);
    }
    if(!M.length) errors.push("Slice & check a part first");
    if(!S.meta.placement){
      errors.push("Enable the cell view, then slice and check before exporting.");
    }
    if(S.meta.clearance_checked===false){
      errors.push("Clearance has not been checked. Slice and check before exporting.");
    }
    const stop=S.violations.filter(v=>v.blocking);
    if(stop.length){
      const worst=stop.reduce((a,b)=>a.clearance<b.clearance?a:b);
      errors.push(`Clearance: ${stop.length} blocking collisions; ${worst.obstacle} at move ${worst.move} (${worst.clearance.toFixed(1)} mm). Adjust the part or placement, then Slice & check.`);
    }
    return errors;
  }
  for(const el of document.querySelectorAll('#wb input[type="number"]')){
    if(el.getAttribute("data-auto-input")!==null) continue;
    el.addEventListener("input",()=>numberError(el));
  }
  document.getElementById("wbJbiName").addEventListener("input",ev=>jobNameError(ev.target));
  document.getElementById("wbJobFolder").addEventListener("input",ev=>jobFolderError(ev.target));
  wbMeasureTimes.addEventListener("input",measureTimesError);

  const WB_FIELDS=["wbCellProfile","wbHcCleaner","wbShape","wbUnits","wbLh","wbTilt","wbSeam","wbBead","wbMargin",
                   "wbFiller","wbWire","wbTravel","wbWfs","wbCtwd","wbCleanFirst","wbOx","wbOy","wbOz","wbSub",
                   "wbYaw","wbRotX","wbRotY","wbLayFlat","wbFixture","wbRepair","wbArc","wbArcSchedule","wbJbiName","wbJobName",
                   "wbThermal","wbCooling","wbThermalProfile",
                   "wbSlicer","wbContours","wbStepover","wbInfill","wbInfillAngle","wbInfillRotation","wbTravelLift",
                   "wbSubstrateRegistration","wbSubstrateMargin","wbBeadLength","wbJobFolder",
                   "wbBeadCount","wbBeadPitch","wbWallLength","wbWallLayers","wbWallThickness","wbCopies","wbCopySpacing","wbInterpass","wbInterpassEvery","wbTempCheckEvery","wbBeadDwell","wbSmooth",
                   "wbMeasureEvery","wbMeasureTimes","wbMeasurePause","wbMeasureClean","wbMeasurePreWeld",
                   "wbLayerSense","wbPrintLayers","wbSenseLayer","wbLayerSpacing","wbLayerSenseName",
                   "wbBuildMode","wbAutoSpacing","wbAutoMax","wbAutoMaxUnit","wbAutoAmbient",
                   "wbThermalTarget","wbThermalUnit","wbThermalPeak","wbThermalAmbient","wbThermalModel"];
  const WB_PARAM_FIELD={cell_profile:"wbCellProfile", hc_cleaner:"wbHcCleaner",
                        shape:"wbShape", units:"wbUnits", lh:"wbLh", tilt:"wbTilt", seam:"wbSeam",
                        bead:"wbBead", margin:"wbMargin", filler:"wbFiller",
                        wire:"wbWire", travel:"wbTravel", wfs:"wbWfs", ctwd:"wbCtwd", ox:"wbOx", oy:"wbOy",
                        oz:"wbOz", sub:"wbSub", yaw:"wbYaw", rotX:"wbRotX", rotY:"wbRotY", lay_flat:"wbLayFlat", fixture:"wbFixture", repair:"wbRepair",
                        thermal:"wbThermal", cooling:"wbCooling", thermal_profile:"wbThermalProfile",
                        slicer:"wbSlicer",contours:"wbContours",stepover:"wbStepover",infill:"wbInfill",
                        infill_angle:"wbInfillAngle",infill_rotation:"wbInfillRotation",travel_lift:"wbTravelLift",
                        substrate_registration:"wbSubstrateRegistration",substrate_margin:"wbSubstrateMargin",bead_length:"wbBeadLength",
                        bead_count:"wbBeadCount",bead_pitch:"wbBeadPitch",wall_length:"wbWallLength",wall_layers:"wbWallLayers",
                        wall_thickness:"wbWallThickness",copies:"wbCopies",copy_spacing:"wbCopySpacing",
                        layer_sense:"wbLayerSense"};
  function sourceIdentity(value){
    if(!value||typeof value!=="object")return null;
    if(value.kind==="none")return {kind:"none"};
    if(value.kind==="upload"&&typeof value.name==="string")return {
      kind:"upload",name:value.name,pending:value.pending===true};
    if(value.kind==="library"&&value.id!=null)return {
      kind:"library",id:String(value.id),
      units:["auto","mm","in","m"].includes(value.units)?value.units:"auto"};
    return null;
  }
  const checkedSourceParams=S.meta.params||{};
  let selectedSource=sourceIdentity(checkedSourceParams.library?{
    kind:"library",id:checkedSourceParams.library,units:checkedSourceParams.units
  }:S.meta.stl?{kind:"upload",name:String(S.meta.title||"")}:{kind:"none"});
  let restoredDraftSource=null;
  let loadedStl=S.meta.stl ? S.meta.title : null;
  let wbBusy=false, wbDisabled=[];


  let applyAccountControls=()=>{};
  let storedDraft=null;
  try{
    storedDraft=JSON.parse(sessionStorage.getItem(UI.fields)||"null");
  }catch(e){                                                        }

  for(const [key,id] of Object.entries(WB_PARAM_FIELD)){
    if(S.meta.params && S.meta.params[key]!==undefined) document.getElementById(id).value=S.meta.params[key];
    else if(["rotX","rotY","lay_flat"].includes(key)) document.getElementById(id).value="0";
    else if(key==="copies") document.getElementById(id).value="1";
    else if(key==="substrate_registration"||key==="layer_sense"||key==="ctwd") document.getElementById(id).value="";
  }
  let checkedFields=null,draftStale=false,missingDraftFile=false,draftBlocked=null;
  const placementFields=new Set(["ox","oy","oz","sub","yaw","rotX","rotY","lay_flat","tilt","fixture","repair",
    "substrate_registration","substrate_margin","layer_sense","bead_length","bead_count","bead_pitch","copies","copy_spacing"]);
  function buildIdentity(){return JSON.stringify([S.meta.gen,S.meta.title,S.meta.params||{}]);}
  function fieldValue(id){
    const el=document.getElementById(id);
    return el.type==="number" && el.value.trim() && Number.isFinite(Number(el.value))?Number(el.value):el.value;
  }
  function draftValues(){
    const fields=Object.fromEntries(Object.entries(WB_PARAM_FIELD).map(([key,id])=>[key,fieldValue(id)]));
    const file=document.getElementById("stlFile").files[0];
    const source=file?{kind:"upload",name:file.name,pending:true}:selectedSource.kind==="library"?
      {...selectedSource,units:wbUnits.value}:selectedSource;
    fields.source=JSON.stringify(sourceIdentity(source));
    return fields;
  }
  function draftChanges(){
    if(!checkedFields) return [];
    const current=draftValues();
    return Object.keys(current).filter(key=>current[key]!==checkedFields[key]);
  }
  function preparationDirty(){return missingDraftFile||draftChanges().some(key=>!placementFields.has(key));}
  function applyDraftControlLocks(){
    for(const id of ["stlFile","thermalFile"]){
      const upload=document.getElementById(id);
      if(upload&&!wbBusy)upload.disabled=draftBlocked!==null;
    }
  }
  function refreshDraft(){
    rememberWb();renderClearance();applyAccountControls();applyDraftControlLocks();
    window.waamPlacement?.update();renderReadiness();
    window.waamRuns?.paint();window.waamAuto?.paint();window.waamLayerSense?.paint();
    window.waamSubstrate?.paint();window.waamReindex?.paint();window.waamLibrary?.update();
    window.waamSettings?.paint?.();
  }
  function currentDraftSource(){
    const file=document.getElementById("stlFile").files[0];
    return sourceIdentity(file?{kind:"upload",name:file.name,pending:true}:selectedSource.kind==="library"?
      {...selectedSource,units:wbUnits.value}:selectedSource);
  }
  function exportDraft(){
    const file=document.getElementById("stlFile").files[0];
    return {version:1,base:buildIdentity(),source:currentDraftSource(),
      pendingFile:missingDraftFile||!!file,
      fields:Object.fromEntries(WB_FIELDS.map(id=>{
        const el=document.getElementById(id);
        return [id,el.type==="checkbox"?el.checked:el.value];
      }))};
  }
  function normalizeStoredDraft(value){
    if(!value||typeof value!=="object"||Array.isArray(value))return null;
    if(value.version===1)return value;
    if(typeof value._draftBase!=="string")return null;
    const fields=Object.fromEntries(WB_FIELDS.map(id=>{
      const el=document.getElementById(id),saved=value[id];
      return [id,el.type==="checkbox"?saved===true:
        saved===undefined?el.value:String(saved)];
    }));
    const pending=value._pendingFile===true;
    let source=sourceIdentity(value._draftSource)||selectedSource||{kind:"none"};
    if(source.kind==="library")source={...source,units:fields.wbUnits};
    if(pending)source=source.kind==="upload"?{...source,pending:true}:
      {kind:"upload",name:"pending STL",pending:true};
    return {version:1,base:value._draftBase,source,pendingFile:pending,fields};
  }
  function draftImportReason(draft){
    try{
      if(!draft||typeof draft!=="object"||Array.isArray(draft)||draft.version!==1||
          typeof draft.base!=="string")return "invalid";
      if(draft.base!==buildIdentity())return "base";
      if(typeof draft.pendingFile!=="boolean"||!draft.fields||typeof draft.fields!=="object"||
          Array.isArray(draft.fields))return "invalid";
      const keys=Object.keys(draft.fields);
      if(keys.length!==WB_FIELDS.length||keys.some(id=>!WB_FIELDS.includes(id)))return "invalid";
      for(const id of WB_FIELDS){
        if(!Object.hasOwn(draft.fields,id))return "invalid";
        const checkbox=document.getElementById(id).type==="checkbox";
        if(checkbox?typeof draft.fields[id]!=="boolean":typeof draft.fields[id]!=="string")return "invalid";
      }
      const source=draft.source;
      const sourceOk=source&&typeof source==="object"&&!Array.isArray(source)&&(
        source.kind==="none" ||
        (source.kind==="upload"&&typeof source.name==="string"&&source.name&&typeof source.pending==="boolean") ||
        (source.kind==="library"&&typeof source.id==="string"&&source.id&&
          ["auto","mm","in","m"].includes(source.units)&&draft.fields.wbUnits===source.units));
      if(!sourceOk||draft.pendingFile!==(source.kind==="upload"&&source.pending===true))return "invalid";
      return "";
    }catch(e){return "invalid";}
  }
  function renderStlChip(){
    const f=document.getElementById("stlFile").files[0];
    const selectedName=selectedSource.kind==="upload"?selectedSource.name||loadedStl:
      selectedSource.kind==="library"?loadedStl:null;
    const name=f ? f.name : selectedName;
    document.getElementById("stlName").textContent=name||"";
    document.getElementById("stlChip").style.display=name!==null ? "" : "none";
    sourceSelection(name,!!f);
    for(const id of ["beadLengthRow","beadCountRow","beadPitchRow"])
      document.getElementById(id).hidden=!!name||wbShape.value!=="bead";
    for(const id of ["wallLengthRow","wallLayersRow","wallThicknessRow"])
      document.getElementById(id).hidden=!!name||wbShape.value!=="wall";
    for(const id of ["copiesRow","copySpacingRow"])
      document.getElementById(id).hidden=!!name||!["wall","bead"].includes(wbShape.value);
    window.waamLibrary?.paint();
  }
  function restoreDraft(draft,notifyLibrary=true){
    for(const id of WB_FIELDS){
      const el=document.getElementById(id);
      if(el.type==="checkbox")el.checked=draft.fields[id];else el.value=draft.fields[id];
    }
    document.getElementById("stlFile").value="";
    selectedSource=sourceIdentity(draft.source);restoredDraftSource=selectedSource;
    loadedStl=selectedSource.kind==="upload"&&!selectedSource.pending?selectedSource.name:null;
    missingDraftFile=draft.pendingFile;
    renderSlicerControls();renderStlChip();renderInterpass();
    if(notifyLibrary){
      const restored=window.waamLibrary?.restoreSource?.(selectedSource);
      if(selectedSource.kind==="library"&&restored?.name){loadedStl=restored.name;renderStlChip();}
    }
    refreshDraft();
  }
  function importDraft(draft){
    const reason=draftImportReason(draft);
    if(reason)return {ok:false,reason};
    restoreDraft(draft);
    return {ok:true,missingFile:missingDraftFile};
  }
  function setDraftBlocked(reason){
    if(reason!==null&&(typeof reason!=="string"||reason.length===0))return false;
    const prior=draftBlocked;draftBlocked=reason;
    refreshDraft();
    const msg=document.getElementById("wbMsg");
    if(reason!==null)msg.textContent=reason;
    else if(msg.textContent===prior)msg.textContent="";
    return true;
  }
  window.waamDraft={
    version:1,fields:Object.freeze(WB_FIELDS.slice()),ready:false,
    export:exportDraft,import:importDraft,setBlocked:setDraftBlocked,
    blocked:()=>draftBlocked,
    error:()=>draftBlocked!==null?draftBlocked:draftStale?"This workspace changed in another page. Reload to review the current build. Your draft is retained for this page.":
      missingDraftFile?"Reselect the pending STL file, then Slice & check. Browsers cannot restore a file selection after reload.":
      draftChanges().length?(preparationDirty()?"Settings changed. Slice & check to update the build before saving or exporting.":
        "Placement changed. Apply placement & check before saving or exporting."):"",
    stale:()=>{draftStale=true;refreshDraft();},
    checked:()=>{checkedFields=draftValues();draftStale=false;missingDraftFile=false;},
    preparationDirty,
    refresh:refreshDraft,
  };
  function guardWbWrite(statusId="wbMsg"){
    const reason=window.waamDraft?.blocked();
    if(reason===null||reason===undefined)return true;
    const status=document.getElementById(statusId)||document.getElementById("wbMsg");
    if(status)status.textContent=reason;
    return false;
  }
  function rememberWb(){
    try{
      sessionStorage.setItem(UI.fields, JSON.stringify(window.waamDraft.export()));
    }catch(e){}
  }
  for(const id of WB_FIELDS){
    document.getElementById(id).addEventListener("input",refreshDraft);
    document.getElementById(id).addEventListener("change",refreshDraft);
  }
  let persistWbOnPagehide=true;
  window.addEventListener("pagehide",()=>{if(persistWbOnPagehide)rememberWb();});
  function renderSlicerControls(){
    const chosen=document.getElementById("wbSlicer").value==="ornl";
    document.getElementById("ornlOptions").hidden=!chosen;
    document.getElementById("seamRow").hidden=chosen;
    const status=S.meta.ornl;
    const option=[...document.getElementById("wbSlicer").options].find(option=>option.value==="ornl");
    option.disabled=!(status&&status.configured);
    option.textContent=option.disabled?"ORNL · unavailable":"ORNL · solid parts";
    document.getElementById("ornlEngineStatus").textContent=chosen ?
      ((status&&status.configured) ? "Use ORNL for contours and infill in a closed solid STL." :
       "ORNL is unavailable. Contact the administrator.") : "";
  }
  document.getElementById("wbSlicer").addEventListener("change",renderSlicerControls);
  renderSlicerControls();
  document.getElementById("thermalFile").onchange=async ev=>{
    if(!guardWbWrite()){
      ev.target.value="";
      return false;
    }
    const file=ev.target.files[0];if(!file)return;
    try{
      if(file.size>12000)throw new Error("Simulation profiles must be 12 KB or smaller.");
      const profile=JSON.parse(await file.text());
      document.getElementById("wbThermalProfile").value=JSON.stringify(profile);
      document.getElementById("wbThermal").value="profile";
      refreshDraft();
      document.getElementById("wbMsg").textContent="Simulation profile loaded. Slice to update the cooling estimate.";
    }catch(e){document.getElementById("wbMsg").textContent="Thermal profile: "+e.message;}
  };

  function demoSizes(demo){
    const layout={copies:wbCopies.value,copy_spacing:wbCopySpacing.value};
    if(demo && wbShape.value==="bead") return {bead_length:wbBeadLength.value,bead_count:wbBeadCount.value,bead_pitch:wbBeadPitch.value,...layout};
    if(demo && wbShape.value==="wall") return {wall_length:wbWallLength.value,wall_layers:wbWallLayers.value,
                                                 wall_thickness:wbWallThickness.value,...layout};
    return {};
  }
  let copySpacingTouched=false;
  function syncCopySpacing(){
    if(copySpacingTouched)return;
    const width=wbShape.value==="bead" ? wbBead.valueAsNumber+(wbBeadCount.valueAsNumber-1)*wbBeadPitch.valueAsNumber
      : Math.max(wbBead.valueAsNumber,wbWallThickness.valueAsNumber);
    if(Number.isFinite(width)){wbCopySpacing.value=String(Math.round((width+30)*1000)/1000);numberError(wbCopySpacing);}
  }
  wbCopySpacing.addEventListener("input",()=>{copySpacingTouched=true;});
  for(const el of [wbBead,wbBeadCount,wbBeadPitch,wbWallThickness]) el.addEventListener("input",syncCopySpacing);
  if(!(S.meta.params&&S.meta.params.copy_spacing!==undefined))syncCopySpacing();
  window.waamDraft.checked();
  const ownDraft=normalizeStoredDraft(storedDraft);
  if(!draftImportReason(ownDraft))restoreDraft(ownDraft,false);

  function clearArcSchedule(){document.getElementById("wbArcSchedule").value="";rememberWb();}
  function clearStl(){
    loadedStl=null;selectedSource=sourceIdentity({kind:"none"});restoredDraftSource=selectedSource;
    missingDraftFile=false;clearArcSchedule();
    copySpacingTouched=false;syncCopySpacing();
    document.getElementById("stlFile").value="";
    window.waamLibrary?.clear();
    renderStlChip();
    document.getElementById("wbMsg").textContent="Demo selected. Slice to update the preview.";
    refreshDraft();
  }
  document.getElementById("clearStl").onclick=clearStl;
  document.getElementById("wbShape").onchange=clearStl;
  document.getElementById("stlFile").onchange=()=>{
    if(!guardWbWrite()){
      document.getElementById("stlFile").value="";
      return false;
    }
    const file=document.getElementById("stlFile").files[0];
    loadedStl=null;selectedSource=sourceIdentity(file?
      {kind:"upload",name:file.name,pending:true}:{kind:"none"});restoredDraftSource=selectedSource;
    missingDraftFile=false;
    clearArcSchedule();renderStlChip();
    refreshDraft();
    return window.waamLibrary?.fileChanged();
  };
  renderStlChip();
  function applyCellControls(){
    const preview=!!(S.meta.cell_profile && S.meta.cell_profile.planning_only);
    document.getElementById("wbFixture").disabled=wbBusy || preview;
    document.getElementById("wbRepair").disabled=wbBusy || preview;
  }
  function renderWbStatus(){
    renderSlicerControls();
    const hc=(S.meta.params||{}).cell_profile==="hc10";
    const profile=S.meta.cell_profile;
    document.getElementById("hcCleanerRow").hidden=!hc;
    document.getElementById("cellProfileNote").textContent=profile?profile.note:"";
    document.getElementById("placeTitle").textContent=hc?"Place on table":"Place on platter";
    document.getElementById("surfaceTitle").textContent=hc?"Table setup":"Platter setup";
    document.getElementById("placeCenter").textContent=hc?"Center on table":"Center on platter";
    document.getElementById("sliceBtn").textContent=hc?"Slice & preview ›":"Slice & check ›";
    document.getElementById("sliceHelp").textContent=hc?"Preview only. Clearance is not checked.":"Next: confirm placement and platter setup.";
    document.getElementById("placeBtn").textContent=hc?"Apply placement":"Apply placement & check";
    document.getElementById("marginHelp").textContent=hc?"Clearance requires calibrated cell and tool geometry.":"Minimum clearance: 5 mm.";
    document.getElementById("trainingBlockRow").hidden=hc;
    document.getElementById("surfaceHelp").textContent=hc?
      "Placement uses the nominal table dimensions. Verify the installed setup.":
      "Set substrate thickness and match the training block to the physical setup.";
    const measured=!!S.meta.substrate;
    document.getElementById("subLabel").textContent=measured?"Plate thickness (model only)":"Substrate thickness";
    if(measured){
      document.getElementById("placeTitle").textContent="Place on substrate";
      document.getElementById("surfaceTitle").textContent="Measured substrate";
      document.getElementById("placeCenter").textContent="Center on substrate";
      document.getElementById("surfaceHelp").textContent="The measured corners set the height. Thickness only draws the plate and its collision solid below the top; it does not move the torch.";
    }
    document.getElementById("offsetXLabel").textContent=measured?"X · along corners 1–2":"X · forward";
    document.getElementById("offsetYLabel").textContent=measured?"Y · across substrate":"Y · left";
    document.getElementById("yawLabel").textContent=measured?"Rotation on plane":"Z rotation";
    document.getElementById("positionHelp").textContent=measured?"Offsets start at the substrate center. The bead width and edge margin must stay inside its corners.":"Drag the handles or enter offsets from the default position.";
    window.waamSubstrate?.paint();
    window.waamLayerSense?.paint();
    window.waamAuto?.paint();
    window.waamReindex?.paint();
    applyCellControls();
    const sliced=S.meta.slicing;
    const review=document.getElementById("slicingSummary");
    if(sliced && sliced.engine==="ornl"){
      review.textContent=`ORNL ${sliced.version} · ${sliced.options.contours} contour passes · ${sliced.options.pattern} infill · `+
        `${Number(sliced.spacing_mm).toFixed(2)} mm bead spacing. `+
        `Planned height ${sliced.build_height_mm.toFixed(2)} mm (STL ${sliced.input_height_mm.toFixed(2)} mm).`;
    }else if(sliced && sliced.wall){
      const w=sliced.wall;
      review.textContent=(w.beads>1?`Built-in wall · ${w.beads} beads across, ${Number(w.spacing_mm).toFixed(2)} mm apart `+
        `(${Math.round(w.fraction*100)}% of the bead width; model optimum ${Math.round(w.optimum*100)}%) · `+
        `outer edges ${Number(w.thickness_mm).toFixed(2)} mm apart.`:"Built-in wall · one bead.")+(w.note?" "+w.note:"");
    }else{review.textContent="Built-in slicing. One bead per contour; no infill.";}
    const ctwd=S.meta.weld_ctwd;
    if(ctwd){
      const inch=mm=>(mm/25.4).toFixed(3);
      wbCtwd.min=String(ctwd.default_mm);
      wbCtwd.placeholder=String(ctwd.default_mm);
      document.getElementById("ctwdStatus").textContent=ctwd.delta_mm?
        `Checked at ${ctwd.mm} mm (${inch(ctwd.mm)} in): welds and later moves raised ${ctwd.delta_mm.toFixed(2)} mm. `+ctwd.notes.join(" "):
        `Checked at the calibrated ${ctwd.default_mm} mm (${inch(ctwd.default_mm)} in).`;
    }
    const p=S.meta.placement, c=S.meta.correction;
    document.getElementById("placementStatus").textContent=p ?
      `Part origin X ${p.origin[0].toFixed(1)}, Y ${p.origin[1].toFixed(1)} mm; bottom Z ${(p.origin[2]+p.bottom_z).toFixed(2)} mm.`+
      (p.adjustment?" "+p.adjustment:"") : "";
    document.getElementById("correctionStatus").textContent=c ?
      `${c.repaired_travels} travel paths corrected. ${c.remaining} blocking collisions remain.`+
      (c.relocated?" Part moved to a clear placement.":"")+
      (c.remaining?" Unresolved collisions block robot export. Review the findings above.":"") : "";
    const msg=document.getElementById("wbMsg");
    msg.textContent=S.toolpath.moves.length+" moves · "+S.toolpath.layers+" layers";
    if(S.meta.stl){
      const u={mm:"millimetres",in:"inches",m:"metres"}[S.meta.stl.resolved_units]||S.meta.stl.resolved_units;
      const dims=S.meta.stl.size_mm.map(v=>Number(v).toFixed(1)).join(" x ");
      msg.textContent=`STL interpreted as ${u}; ${dims} mm`;
    }
    renderInterpass();
  }
  function savedBuildNameError(){
    const el=document.getElementById("wbJobName"),value=el.value.trim();
    return fieldFeedback(el,value && !/^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$/.test(value)?
      "Build name: start with a letter or digit; use up to 40 letters, digits, spaces, _ or -.":"");
  }
  function renderReadiness(){
    const box=document.getElementById("buildReadiness");if(!box)return;
    const params=S.meta.params||{},draft=buildDraftError();
    const robot=S.robot;
    const rows=[draft||"Settings match the checked build.",
      S.meta.clearance_checked===false?"Torch clearance: not checked.":
        S.violations.some(v=>v.blocking)?"Torch clearance: blocking findings.":"Torch clearance: passed for the modeled setup.",
      robot?(robot.unreachable?`Robot reach: ${robot.unreachable} unreachable poses.`:"Robot reach: all preview poses solved."):
        "Robot reach: preview unavailable; export performs its own checks.",
      "Whole-arm interference is not established by the torch clearance result.",
      `Checked bead ${params.bead||wbBead.value} mm wide × ${params.lh||wbLh.value} mm high; travel ${params.travel||wbTravel.value} mm/min.`,
      arcSchedule()?"Controller schedule: ASF# by layer selected; confirm the listed schedules on the pendant at export.":
        document.getElementById("wbArc").value.trim()?"Controller schedule: ASF# "+document.getElementById("wbArc").value.trim()+" selected; confirm its weld settings on the pendant.":
          "Controller schedule: choose an ASF# in Export. Arc estimates do not set the controller schedule.",
      `Export cooling: ${wbInterpass.value} s after every ${wbInterpassEvery.value==="1"?"layer":wbInterpassEvery.value+" layers"}; ${wbBeadDwell.value} s between beads. `+
        (thermalApplied?"Applied thermal plan included.":"Review simulation estimates are illustrative. Use Compute and Apply in Export to include a thermal plan."),
      S.meta.cell_profile?.planning_only?S.meta.cell_profile.export_reason:
        "Calibration: uses the recorded cell and tool geometry. Inspect the physical platter and confirm block/fixture state at export."];
    box.replaceChildren();
    for(const text of rows){const item=document.createElement("li");item.textContent=text;box.appendChild(item);}
  }
  renderWbStatus();
  renderReadiness();


  function lockGuestControls(){
    const firstLock=applyAccountControls!==lockGuestControls;
    applyAccountControls=lockGuestControls;
    window.waamRuns?.paint();
    window.waamAuto?.paint();
    const hint="Sign in to save builds and export files.";
    for(const id of ["saveBtn","loadBtn","emitBtn","emitRosBtn","wbThermalCompute","wbThermalApply"]){
      const b=document.getElementById(id);
      if(b){ b.disabled=true; b.title=hint; }
    }
    for(const a of document.querySelectorAll('#wb a[download="part.gcode"]')){
      a.removeAttribute("href"); a.title=hint;
      a.style.opacity=".5"; a.style.pointerEvents="none";
      a.setAttribute("aria-disabled","true");
    }
    const jobs=document.getElementById("wbJobs");
    if(jobs){
      jobs.innerHTML="";
      const o=document.createElement("option");
      o.value=""; o.textContent="Sign in to save and load builds";
      jobs.appendChild(o);
    }

    if(firstLock && !wbBusy){
      document.getElementById("wbMsg").textContent=
        "Guest preview. Sign in to save builds and export files.";
    }
  }
  function unlockAccountControls(){
    applyAccountControls=unlockAccountControls;
    window.waamRuns?.paint();
    window.waamAuto?.paint();
    if(wbBusy) return;
    const substrateError=substrateRegistrationError(),draftError=buildDraftError();
    const desktopBlock=window.waamDraft?.blocked();
    const registrationError=substrateError||draftError;
    for(const id of ["saveBtn","loadBtn","emitBtn","emitRosBtn","wbThermalCompute"]){
      const b=document.getElementById(id);
      if(b){
        const unavailable=(id==="emitBtn"||id==="emitRosBtn") && S.meta.cell_profile && S.meta.cell_profile.planning_only;
        const blockedError=id==="wbThermalCompute"?(substrateError||(desktopBlock===null?draftError:"")):
          id==="loadBtn"?desktopBlock:registrationError;
        if(blockedError){b.disabled=true;b.title=blockedError;}
        else if(id==="saveBtn" && savedBuildNameError()){b.disabled=true;b.title=savedBuildNameError();}
        else if(unavailable){b.disabled=true;b.title=S.meta.cell_profile.export_reason;}
        else{b.disabled=false;b.title="";}
      }
    }
    wbThermalApply.disabled=!thermalPlan||!!registrationError;
    for(const a of document.querySelectorAll('#wb a[download="part.gcode"]')){
      if(registrationError){
        a.removeAttribute("href");a.title=registrationError;a.style.pointerEvents="none";
        a.setAttribute("aria-disabled","true");continue;
      }
      a.href=api("/api/gcode");
      a.href+=(a.href.includes("?")?"&":"?")+"run_name="+encodeURIComponent(document.getElementById("wbRunName").value.trim());
      a.title="";
      a.style.opacity=""; a.style.pointerEvents="";
      a.removeAttribute("aria-disabled");
    }
    window.waamAuto?.paint();
  }


  function beginWb(){
    if(!guardWbWrite())return false;
    if(wbBusy) return false;
    if(UI.busy) return false;
    wbBusy=true;
    window.waamRuns?.paint();
    window.waamAuto?.paint();
    wbDisabled=[...document.querySelectorAll('#wb button:not(:disabled), #wb input:not(:disabled), #wb select:not(:disabled)')];
    for(const el of wbDisabled) el.disabled=true;
    setBusy("Updating build…");
    for(const a of document.querySelectorAll('#wb a[download="part.gcode"]')){
      a.removeAttribute("href"); a.style.pointerEvents="none";
      a.setAttribute("aria-disabled","true");
    }
    return true;
  }
  function endWb(){
    wbBusy=false;
    for(const el of wbDisabled) el.disabled=false;
    wbDisabled=[];
    setBusy(null);
    applyCellControls();
    updateCorrectionControls();
    window.waamSubstrate?.paint();
    window.waamLayerSense?.paint();
    window.waamAuto?.paint();
    window.waamReindex?.paint();

    applyAccountControls();
    applyDraftControlLocks();
    if(UI.invalidField){revealField(UI.invalidField);UI.invalidField=null;}
  }
  function renderWorkbench(next){
    const panel=document.querySelector("aside"), scrollTop=panel.scrollTop;
    const scrollX=window.scrollX, scrollY=window.scrollY;
    clearThermalResult();thermalApplied=null;
    renderState(next);

    for(const [key,id] of Object.entries(WB_PARAM_FIELD)){
      if(key==="shape" || key==="units") continue;
      if(S.meta.params && S.meta.params[key]!==undefined){
        const el=document.getElementById(id);
        el.value=S.meta.params[key];
        if(el.type==="number") numberError(el);
      }
      else if(["rotX","rotY","lay_flat"].includes(key)){
        const el=document.getElementById(id);el.value="0";
        if(el.type==="number") numberError(el);
      }
      else if(key==="copies") document.getElementById(id).value="1";
      else if(key==="substrate_registration"||key==="layer_sense"||key==="ctwd") document.getElementById(id).value="";
    }
    copySpacingTouched=!!(S.meta.params&&S.meta.params.copy_spacing!==undefined);
    if(!copySpacingTouched)syncCopySpacing();
    window.waamDraft.checked();
    renderStlChip(); renderWbStatus(); rememberWb();
    switchStep(window.waamPlacement?.isApplying() && UI.step==="place" ? "place" : "review");
    applyAccountControls();
    renderClearance();renderReadiness();
    panel.scrollTop=scrollTop;
    window.scrollTo(scrollX,scrollY);
  }
  async function applyPlacement(correct=false,registration=false){
    if(!guardWbWrite("placementStatus"))return false;
    if(draftStale){document.getElementById("placementStatus").textContent=buildDraftError();return false;}
    if(preparationDirty()){
      if(!correct && !registration){const slice=document.getElementById("sliceBtn");return await slice.onclick();}
      document.getElementById("placementStatus").textContent="Prepare settings changed. Slice & check before this operation. Your edits are kept.";
      return false;
    }
    if(!registration && substrateRegistrationError()){
      document.getElementById("placementStatus").textContent=substrateRegistrationError();return false;
    }
    if(!beginWb()) return false;
    const msg=document.getElementById("placementStatus");
    let placementSnapshot=null;
    try{
      const beadSizes=!loadedStl&&wbShape.value==="bead"?["wbBeadLength","wbBeadCount","wbBeadPitch"]:[];
      const copySizes=!loadedStl&&["wall","bead"].includes(wbShape.value)?["wbCopies","wbCopySpacing"]:[];
      if(!validateFields(["wbOx","wbOy","wbOz","wbSub","wbYaw","wbRotX","wbRotY","wbTilt","wbSubstrateMargin",...beadSizes,...copySizes].map(id=>document.getElementById(id)),msg)) return false;
      if(S.meta.placement) placementSnapshot=window.waamPlacement?.begin();
      const preview=S.meta.cell_profile && S.meta.cell_profile.planning_only;
      setBusy(preview?"Updating placement…":correct?"Finding a clear placement…":"Checking placement…");
      msg.textContent=preview?"Updating the part on the nominal table...":correct?"Checking corrected paths and clear placements...":"Updating orientation, placement and clearance...";
      const qs=new URLSearchParams({ox:wbOx.value,oy:wbOy.value,oz:wbOz.value,
        ...(registration?{registration_action:wbSubstrateRegistration.value?"apply":"clear"}:{}),
        sub:wbSub.value,yaw:wbYaw.value,rotX:wbRotX.value,rotY:wbRotY.value,lay_flat:wbLayFlat.value,
        tilt:wbTilt.value,fixture:wbFixture.value,repair:wbRepair.value,
        correct:correct?"1":"0",substrate_registration:wbSubstrateRegistration.value,layer_sense:wbLayerSense.value,
        substrate_margin:wbSubstrateMargin.value,
        ...(copySizes.length?{copies:wbCopies.value,copy_spacing:wbCopySpacing.value}:{}),
        ...(beadSizes.length?{bead_length:wbBeadLength.value,bead_count:wbBeadCount.value,bead_pitch:wbBeadPitch.value}:{})});
      const r=await fetch(api("/api/place?"+qs),{method:"POST"});
      const j=await r.json();
      if(j.reason==="stale") window.waamDraft.stale();
      if(!r.ok || !j.ok) throw new Error(j.error||"Placement failed");
      const previewWarning=await hydrateState(j.state); renderWorkbench(j.state);
      if(placementSnapshot) window.waamPlacement.accepted(placementSnapshot);
      if(previewWarning) msg.textContent=previewWarning;
      return true;
    }catch(e){
      if(placementSnapshot) window.waamPlacement?.rejected();
      msg.textContent="Placement failed: "+e.message.replace(/[.\\s]+$/g,"")+". The displayed job is unchanged.";
      return false;
    }finally{
      endWb(); draw();
    }
  }
  document.getElementById("placeBtn").onclick=()=>applyPlacement();
  if(window.WaamRuns){
    window.waamRuns=window.WaamRuns.create({document,busy:()=>wbBusy||UI.busy,guest:()=>IS_GUEST,
      blocked:()=>window.waamDraft?.blocked(),
      request:async(path,options)=>{
        const response=await fetch(api(path),options);
        if(response.ok&&path==="/api/run/finish")await window.waamAuto?.refresh();
        return response;
      },confirm:message=>window.confirm(message),
      query:fields=>new URLSearchParams(fields).toString(),
      begin:message=>{const started=beginWb();if(started)setBusy(message);return started;},end:endWb,
      changed:()=>applyAccountControls()});
  }
  function runName(){return window.waamRuns?.name()||"";}
  async function filedInRun(response){return await window.waamRuns?.response(response);}
  if(window.WaamSubstrate){
    window.waamSubstrate=window.WaamSubstrate.create({document,state:()=>S,busy:()=>wbBusy||UI.busy,
      storage:sessionStorage,storageKey:UI.fields+":substrate",
      changed:()=>{applyAccountControls();renderClearance();},
      pause:()=>{playing=false;},
      guest:()=>IS_GUEST,blocked:()=>window.waamDraft?.blocked(),
      senseParams:()=>({sub:wbSub.value,fixture:wbFixture.value}),
      senseJob:async(raw,options)=>{
        if(IS_GUEST)throw new Error("Sign in to export robot programs.");
        if(!beginWb())throw new Error("Wait for the current workbench request to finish.");
        try{
          const qs=new URLSearchParams({...options,substrate_registration:raw,folder:jobFolder(),run_name:runName()});
          if(options.layout==="path")qs.set("gen",String(S.meta.gen));
          const r=await fetch(api("/api/sense-job?"+qs));
          const run=await filedInRun(r);
          if(!r.ok){
            const j=await r.json().catch(()=>null);
            throw new Error(j?.error||("Sense job refused (HTTP "+r.status+")."));
          }
          const blob=await r.blob();
          const url=URL.createObjectURL(blob);
          const a=document.createElement("a");
          a.href=url;a.download=String(options.name||"SENSE_PLATE").toUpperCase()+".JBI";
          document.body.appendChild(a);a.click();a.remove();
          URL.revokeObjectURL(url);
          return {count:Number(r.headers.get("X-Waam-Probes")),base:Number(r.headers.get("X-Waam-Base")),archive:r.headers.get("X-Waam-Archive"),run};
        }finally{endWb();}
      },
      importVarDat:async(body,base,count,purpose)=>{
        if(!beginWb())throw new Error(buildDraftError()||"Wait for the current workbench request to finish.");
        try{
          const r=await fetch(api("/api/var-dat?"+new URLSearchParams({base,count,purpose,run_name:runName()})),{method:"POST",body});
          await filedInRun(r);
          const j=await r.json().catch(()=>({ok:false,error:"The workbench did not respond."}));
          if(!r.ok||!j.ok) throw new Error(j.error||"VAR.DAT import failed.");
          return j.records;
        }finally{endWb();}
      },
      apply:async(raw,options)=>{
        const preservePlacement=!!options?.preservePlacement;
        const prior={raw:wbSubstrateRegistration.value,x:wbOx.value,y:wbOy.value,yaw:wbYaw.value,z:wbOz.value};
        wbSubstrateRegistration.value=raw;
        if(!preservePlacement){wbOx.value="0";wbOy.value="0";wbYaw.value="0";wbOz.value="0";}
        const ok=await applyPlacement(false,true);
        if(!ok){wbSubstrateRegistration.value=prior.raw;wbOx.value=prior.x;wbOy.value=prior.y;wbYaw.value=prior.yaw;wbOz.value=prior.z;}
        else if(!preservePlacement)window.waamPlacement?.reset();
        rememberWb();return ok;
      }});
    renderClearance();
  }

  function printLayers(){
    const value=document.getElementById("wbPrintLayers").value.trim();
    return value?"&layers="+encodeURIComponent(value):"";
  }

  function ctwdQuery(){return "&ctwd="+encodeURIComponent(wbCtwd.value.trim())+
    "&clean_first="+(wbCleanFirst.checked?"1":"0");}
  function thermalTraceQuery(){
    if(!thermalApplied)return "";
    if(wbInterpass.value!==thermalApplied.interpass||wbInterpassEvery.value!==thermalApplied.every||wbBeadDwell.value!==thermalApplied.bead){
      thermalApplied=null;return "";
    }
    return "&"+new URLSearchParams(thermalApplied.query).toString();
  }
  function interpassQuery(){
    return "&interpass="+encodeURIComponent(wbInterpass.value)+"&interpass_every="+encodeURIComponent(wbInterpassEvery.value)+
      "&temp_check_every="+encodeURIComponent(wbTempCheckEvery.value)+"&bead_dwell="+encodeURIComponent(wbBeadDwell.value)+
      "&measure_every="+encodeURIComponent(wbMeasureEvery.value)+"&measure_times="+encodeURIComponent(wbMeasureTimes.value)+
      "&measure_pause="+(wbMeasurePause.checked?"1":"0")+"&measure_clean="+(wbMeasureClean.checked?"1":"0")+
      "&measure_pre_weld="+(wbMeasurePreWeld.checked?"1":"0");
  }

  function renderInterpass(){
    const note=document.getElementById("interpassSummary");
    const seconds=wbInterpass.valueAsNumber, every=wbInterpassEvery.valueAsNumber;
    if(!Number.isFinite(seconds)||!Number.isInteger(every)||every<1){note.textContent="Enter the dwell and how many layers run between dwells.";return;}
    const layers=Number(S.toolpath&&S.toolpath.layers)||0;
    if(document.getElementById("wbBuildMode").value==="auto"){
      note.textContent=`Each cycle prints up to ${every} layers. With the arc off at park, it pauses for a temperature reading, waits ${seconds} s, pauses for another reading, then touches the top. The last cycle ends after welding.`;
      return;
    }
    const dwells=layers>1?Math.floor((layers-1)/every):0;
    const check=wbTempCheckEvery.valueAsNumber,bead=wbBeadDwell.valueAsNumber;
    const extra=(Number.isInteger(check)&&check>0?" It then pauses for a temperature reading after every "+(check===1?"layer":check+" layers")+"; press START to go on.":"")+
      (Number.isFinite(bead)&&bead>0?" Between beads of one layer it waits "+bead+" s at the lift.":"");

    const unit=S.meta.shape==="bead"&&!S.meta.stl?"bead":"layer";
    const cadence=every===1?"after every "+unit:"after every "+every+" "+unit+"s";
    note.textContent="With the arc off, the torch waits "+seconds+" s 50 mm above the next "+unit+"'s start "+cadence+". "+
      (layers>1?(dwells?dwells+" dwell"+(dwells===1?"":"s")+" in this "+layers+"-"+unit+" part.":
        "This "+layers+"-"+unit+" part would never pause, so export is refused. Lower layers per interpass."):"A single-"+unit+" part has no interpass.")+extra;
  }
  for(const el of [wbInterpass,wbInterpassEvery,wbTempCheckEvery,wbBeadDwell]) el.addEventListener("input",renderInterpass);
  for(const el of [wbInterpass,wbInterpassEvery,wbBeadDwell]) el.addEventListener("input",()=>{thermalApplied=null;renderReadiness();});
  renderInterpass();
  function thermalTemperatureError(el,optional=false,ambient=false){
    const raw=el.value.trim();
    if(optional&&raw==="")return fieldFeedback(el,"");
    const value=Number(raw),unit=wbThermalUnit.value;
    const high=ambient?(unit==="F"?140:60):(unit==="F"?2500:1400);
    return fieldFeedback(el,raw===""||!Number.isFinite(value)||value<-40||value>high?el.dataset.error:"");
  }
  function clearThermalResult(){
    thermalRequestRevision++;
    thermalPlan=null;thermalPlanInputs=null;
    wbThermalApply.hidden=true;wbThermalApply.disabled=true;
    wbThermalApply.textContent="Apply";
    document.getElementById("thermalPlanTable").hidden=true;
    document.getElementById("thermalPlanRows").textContent="";
  }
  function toC(value,unit){return unit==="F"?(value-32)*5/9:value;}
  function thermalSeconds(value){return Number(value).toLocaleString(undefined,{maximumFractionDigits:2})+" s";}
  function thermalRequestState(){return {gen:S.meta.gen,fields:{
    target_preweld:wbThermalTarget.value,unit:wbThermalUnit.value,max_peak:wbThermalPeak.value,
    ambient:wbThermalAmbient.value,model:wbThermalModel.value}};}
  wbThermalCompute.onclick=async()=>{
    if(IS_GUEST){document.getElementById("thermalPlanSummary").textContent="Sign in to compute a thermal plan.";return;}
    const errors=[thermalTemperatureError(wbThermalTarget),thermalTemperatureError(wbThermalPeak,true),
                  thermalTemperatureError(wbThermalAmbient,false,true)].filter(Boolean);
    if(errors.length){document.getElementById("thermalPlanSummary").textContent=errors.join(" ");return;}
    clearThermalResult();
    const requestRevision=thermalRequestRevision,request=thermalRequestState();
    const summary=document.getElementById("thermalPlanSummary");
    const notes=document.getElementById("thermalPlanNotes");
    wbThermalCompute.disabled=true;summary.textContent="Computing dwellsâ€¦";notes.textContent="";
    try{
      const response=await fetch(api("/api/thermal-plan?"+new URLSearchParams({...request.fields,gen:String(request.gen)})));
      const result=await response.json().catch(()=>null);
      if(requestRevision!==thermalRequestRevision)return;
      const current=thermalRequestState();
      if(current.gen!==request.gen||JSON.stringify(current.fields)!==JSON.stringify(request.fields)){
        summary.textContent="Thermal inputs or the checked build changed. Compute again.";return;
      }
      if(!response.ok||!result?.ok)throw new Error(result?.error||("Thermal plan refused (HTTP "+response.status+")."));
      if(result.gen!==request.gen){summary.textContent="The checked build changed. Compute again.";return;}
      thermalPlan=result;
      thermalPlanInputs={target:toC(Number(request.fields.target_preweld),request.fields.unit),
                         peak:request.fields.max_peak.trim()===""?Number(result.max_peak_c):toC(Number(request.fields.max_peak),request.fields.unit),
                         model:result.model.key};
      const body=document.getElementById("thermalPlanRows");
      for(const row of result.rows){
        const tr=document.createElement("tr");
        const layer=row.copy===undefined?row.layer:String(row.layer)+" / "+String(row.copy+1);
        for(const [index,value] of [layer,row.start_c,row.peak_c,row.next_preweld_c].entries()){
          const td=document.createElement("td");
          td.textContent=index===0?String(value):Number(value).toFixed(1)+" Â°C";tr.appendChild(td);
        }
        body.appendChild(tr);
      }
      document.getElementById("thermalPlanTable").hidden=false;
      summary.textContent="Layer dwell "+thermalSeconds(result.layer_dwell_s)+" Â· bead dwell "+thermalSeconds(result.bead_dwell_s)+
        " Â· added time "+thermalSeconds(result.added_time_s)+" Â· model C "+Number(result.model.C).toFixed(1)+
        " J/K, tau "+thermalSeconds(result.model.tau)+" Â· "+result.model.source;
      const invalid=result.valid===false?"Fit not valid: "+(result.reasons||[]).join(" "):"";
      notes.textContent=[invalid,...(result.model.notes||[]),...(result.notes||[])].filter(Boolean).join(" ");
      wbThermalApply.textContent=result.valid===false?"Apply anyway":"Apply";
      wbThermalApply.hidden=false;wbThermalApply.disabled=false;
    }catch(error){summary.textContent=error.message;}
    finally{applyAccountControls();}
  };
  wbThermalApply.onclick=()=>{
    if(!guardWbWrite("thermalPlanNotes"))return;
    if(!thermalPlan||!thermalPlanInputs)return;
    wbInterpass.value=String(thermalPlan.layer_dwell_s);
    wbInterpassEvery.value="1";
    wbBeadDwell.value=String(thermalPlan.bead_dwell_s);
    for(const el of [wbInterpass,wbInterpassEvery,wbBeadDwell]){
      el.dispatchEvent(new Event("input",{bubbles:true}));
      el.dispatchEvent(new Event("change",{bubbles:true}));
    }
    thermalApplied={interpass:wbInterpass.value,every:wbInterpassEvery.value,bead:wbBeadDwell.value,query:{
      thermal_target:String(thermalPlanInputs.target),thermal_peak:String(thermalPlanInputs.peak),
      thermal_model:thermalPlanInputs.model,thermal_c:String(thermalPlan.model.C),
      thermal_tau:String(thermalPlan.model.tau)}};
    document.getElementById("thermalPlanNotes").textContent=
      "Applied to the interpass and between-bead dwell fields, with interpass set to every layer. "+document.getElementById("thermalPlanNotes").textContent;
    renderReadiness();
  };
  for(const el of [wbThermalTarget,wbThermalPeak,wbThermalAmbient]) el.addEventListener("input",()=>{
    if(thermalPlan)clearThermalResult();
  });
  for(const el of [wbThermalUnit,wbThermalModel]) el.addEventListener("change",()=>{
    if(thermalPlan)clearThermalResult();
  });
  if(window.WaamLayerSense){
    window.waamLayerSense=window.WaamLayerSense.create({document,state:()=>S,busy:()=>wbBusy||UI.busy||document.getElementById("wbBuildMode").value==="auto",
      guest:()=>IS_GUEST,draftError:buildDraftError,changed:()=>applyAccountControls(),pause:()=>{playing=false;},
      layerSensePlan:async o=>{
        const r=await fetch(api("/api/layer-sense-job?"+new URLSearchParams({...o,plan:"1",gen:String(S.meta.gen)})));
        const j=await r.json().catch(()=>null);
        if(!r.ok||!j?.ok)throw new Error(j?.error||("Layer touch plan refused (HTTP "+r.status+")."));
        return j;
      },
      layerSenseJob:async o=>{
        if(IS_GUEST)throw new Error("Sign in to export robot programs.");
        const draftError=buildDraftError();
        if(draftError)throw new Error(draftError);
        if(!beginWb())throw new Error("Wait for the current workbench request to finish.");
        try{
          const r=await fetch(api("/api/layer-sense-job?"+new URLSearchParams({...o,gen:String(S.meta.gen),folder:jobFolder(),run_name:runName()})));
          const run=await filedInRun(r);
          if(!r.ok){
            const j=await r.json().catch(()=>null);
            throw new Error(j?.error||("Layer touch job refused (HTTP "+r.status+")."));
          }
          const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
          a.href=url;a.download=String(o.name||"LAYER_TOUCH").toUpperCase()+".JBI";
          document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
          return {count:Number(r.headers.get("X-Waam-Probes")),base:Number(r.headers.get("X-Waam-Base")),
                  flag_base:Number(r.headers.get("X-Waam-Flag-Base")),layer:Number(r.headers.get("X-Waam-Layer")),
                  archive:r.headers.get("X-Waam-Archive"),run};
        }finally{endWb();}
      },
      importVarDat:async(body,base,count,purpose,flagBase)=>{
        if(!beginWb())throw new Error(buildDraftError()||"Wait for the current workbench request to finish.");
        try{
          const r=await fetch(api("/api/var-dat?"+new URLSearchParams({base,count,purpose,flags:"1",flag_base:flagBase,run_name:runName()})),{method:"POST",body});
          await filedInRun(r);
          const j=await r.json().catch(()=>({ok:false,error:"The workbench did not respond."}));
          if(!r.ok||!j.ok) throw new Error(j.error||"VAR.DAT import failed.");
          if(!Array.isArray(j.hits))throw new Error("The VAR.DAT import returned no hit flags.");
          return {records:j.records,hits:j.hits};
        }finally{endWb();}
      },
      apply:async raw=>{
        const prior=wbLayerSense.value;
        wbLayerSense.value=raw;
        const ok=await applyPlacement();
        if(!ok)wbLayerSense.value=prior;
        rememberWb();return ok;
      }});
  }
  if(window.WaamAuto){
    window.waamAuto=window.WaamAuto.create({document,state:()=>S,busy:()=>wbBusy||UI.busy,guest:()=>IS_GUEST,
      blocked:()=>window.waamDraft?.blocked(),
      query:fields=>new URLSearchParams(fields).toString(),pause:()=>{playing=false;},
      changed:()=>{rememberWb();window.waamLayerSense?.paint();applyAccountControls();renderInterpass();},
      refreshRun:()=>{void window.waamRuns?.refresh();},
      request:async(path,options)=>{
        if(!options?.method)return fetch(api(path),options);
        if(IS_GUEST)throw new Error("Sign in to use Auto cycles.");
        if(!beginWb())throw new Error("Wait for the current workbench request to finish.");
        try{return await fetch(api(path),options);}finally{endWb();}
      },
      apply:async raw=>{
        const prior=wbLayerSense.value;wbLayerSense.value=raw;
        const ok=await applyPlacement();
        if(!ok)wbLayerSense.value=prior;
        rememberWb();return ok;
      },
      exportCycle:async options=>{
        const errors=validateEmit(true),ready=readyForRobotExport();
        if(errors.length)throw new Error(errors.join("\\n"));
        if(!ready)throw new Error("Slice and check the part before exporting a cycle.");
        const name=document.getElementById("wbJbiName").value,schedule=arcSchedule();
        const arc=schedule?{arc_schedule:schedule}:{arc:document.getElementById("wbArc").value};
        if(name.length>28)throw new Error("Auto cycle base name: use 1–28 characters so the cycle suffix fits.");
        const att=await attestPlatter(schedule?scheduleAsfs(schedule):[],schedule);
        if(!att)throw new Error("Export cancelled. Platter check not confirmed.");
        if(!beginWb())throw new Error("Wait for the current workbench request to finish.");
        try{
          setBusy("Checking clearance and emitting the cycle job…");
          const query=new URLSearchParams({...options,name,...arc,folder:jobFolder(),
            smooth:document.getElementById("wbSmooth").checked?"1":"0",run_name:runName()});
          const r=await fetch(api("/api/cycle-job?"+query+att+interpassQuery()+ctwdQuery())),run=await filedInRun(r);
          if(!r.ok){const data=await r.json().catch(()=>null);throw new Error(data?.error||`Cycle export refused (HTTP ${r.status}).`);}
          const cycle=Number(r.headers.get("X-Waam-Cycle")),layers=(r.headers.get("X-Waam-Layers")||"").split("-").map(Number);
          if(!Number.isInteger(cycle)||cycle<1||layers.length!==2||layers.some(layer=>!Number.isInteger(layer)))
            throw new Error("The cycle job response did not identify its cycle and layers.");
          const job=name+"_C"+String(cycle).padStart(2,"0"),blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
          a.href=url;a.download=job+".JBI";document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
          return {cycle,firstLayer:layers[0],lastLayer:layers[1],last_cycle:r.headers.get("X-Waam-Last")==="1",job,
            archive:r.headers.get("X-Waam-Archive"),run,passes:passSummary(r),notes:r.headers.get("X-Waam-Notes")||""};
        }finally{endWb();}
      }});
  }
  if(window.WaamReindex){
    const reindexJson=async(path,options)=>{
      if(IS_GUEST)throw new Error("Sign in to export robot programs.");
      const writing=!!options?.method&&String(options.method).toUpperCase()!=="GET";
      if(writing&&!beginWb())throw new Error(buildDraftError()||"Wait for the current workbench request to finish.");
      try{
        const r=await fetch(api(path),options),j=await r.json().catch(()=>null);
        if(!r.ok||!j?.ok)throw new Error(j?.error||("Re-index request refused (HTTP "+r.status+")."));
        return j;
      }finally{if(writing)endWb();}
    };
    window.waamReindex=window.WaamReindex.create({document,busy:()=>wbBusy||UI.busy,
      guest:()=>IS_GUEST,blocked:()=>window.waamDraft?.blocked(),
      changed:()=>{applyAccountControls();window.waamReindex?.paint();},jobFolder,
      post:(path,obj)=>reindexJson(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(obj)}),
      get:path=>reindexJson(path),
      download:async(path,obj,fallbackName)=>{
        if(IS_GUEST)throw new Error("Sign in to export robot programs.");
        if(!beginWb())throw new Error("Wait for the current workbench request to finish.");
        try{
          const r=await fetch(api(path),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(obj)});
          if(!r.ok){
            const j=await r.json().catch(()=>null);
            throw new Error(j?.error||("Re-index job refused (HTTP "+r.status+")."));
          }
          const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
          a.href=url;a.download=fallbackName;
          document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
          return {count:Number(r.headers.get("X-Waam-Probes")),base:Number(r.headers.get("X-Waam-Base")),
                  shift:Number(r.headers.get("X-Waam-Reindex-Shift-Mm")),rotation:Number(r.headers.get("X-Waam-Reindex-Rotation-Deg")),
                  archive:r.headers.get("X-Waam-Archive")};
        }finally{endWb();}
      },
      importVarDat:async(text,base,count)=>{
        if(!beginWb())throw new Error(buildDraftError()||"Wait for the current workbench request to finish.");
        try{
          const r=await fetch(api("/api/var-dat?base="+base+"&count="+count),{method:"POST",body:text});
          const j=await r.json().catch(()=>({ok:false,error:"The workbench did not respond."}));
          if(!r.ok||!j.ok) throw new Error(j.error||"VAR.DAT import failed.");
          return j.records;
        }finally{endWb();}
      }});
    document.getElementById("reindexPanel").addEventListener("toggle",()=>{
      if(document.getElementById("reindexPanel").open)window.waamReindex.refresh();
    });
  }
  document.getElementById("correctBtn").onclick=()=>{if(!guardWbWrite("placementStatus"))return;wbRepair.value="1";applyPlacement(true);};
  document.getElementById("wbFixture").onchange=()=>applyPlacement();
  for(const b of document.querySelectorAll('[data-nudge]')) b.onclick=()=>{
    if(!guardWbWrite("placementStatus")||wbBusy || UI.busy) return;
    const [axis,step]=b.dataset.nudge.split(",");
    const input=document.getElementById(axis==="x"?"wbOx":"wbOy");
    input.value=Number(input.value)+Number(step); applyPlacement();
  };
  document.getElementById("snapBtn").onclick=()=>{
    if(!guardWbWrite("placementStatus")||wbBusy || UI.busy) return;
    wbOz.value="0";
    applyPlacement();
  };
  document.getElementById("sliceBtn").onclick=async()=>{
    if(!guardWbWrite())return false;
    if(draftStale){document.getElementById("wbMsg").textContent=buildDraftError();return false;}
    if(missingDraftFile){document.getElementById("wbMsg").textContent=buildDraftError();return false;}
    if(wbSlicer.value==="ornl" && !S.meta.ornl?.configured){
      document.getElementById("wbMsg").textContent="ORNL is unavailable. Select Built-in to slice thin walls, or ask the administrator to enable ORNL for a closed solid STL.";
      return false;
    }
    if(wbBusy || UI.busy) return;
    if(substrateRegistrationError()){
      document.getElementById("wbMsg").textContent=substrateRegistrationError();return;
    }
    if(window.waamLibrary && !await window.waamLibrary.prepareSlice()) return;
    if(!beginWb()) return;
    const msg=document.getElementById("wbMsg");
    try{
      const ornlFields=new Set(["wbContours","wbStepover","wbInfillAngle","wbInfillRotation","wbTravelLift"]);


      const activeFields=[...document.querySelectorAll('#wb input[type="number"]:not(#wbArc):not(#wbInterpass):not(#wbInterpassEvery):not(#wbTempCheckEvery):not(#wbBeadDwell):not(#wbMeasureEvery):not(#wbRunTemperature)')]
        .filter(el=>el.getAttribute("data-auto-input")===null)
        .filter(el=>wbSlicer.value==="ornl" ? el.id!=="wbSeam" : !ornlFields.has(el.id))
        .filter(el=>!el.closest(".row")?.hidden);
      if(!validateFields(activeFields,msg)){syncSliceFeedback(true);return;}
      const f=document.getElementById("stlFile").files[0];
      if(f && !/\\.stl$/i.test(f.name)){
        msg.textContent="Choose a binary or ASCII STL file.";
        syncSliceFeedback(true);
        return;
      }
      const preview=wbCellProfile.value==="hc10";
      msg.textContent=preview?"Slicing the geometry preview…":"Slicing and checking clearance…";
      syncSliceFeedback();
      setBusy(preview?"Slicing the geometry preview…":"Slicing & checking clearance…");
      rememberWb();
      const priorStl=!f && loadedStl!==null;
      const qs=new URLSearchParams({
        cell_profile:wbCellProfile.value,hc_cleaner:wbHcCleaner.value,
        lh:wbLh.value, tilt:wbTilt.value, seam:wbSeam.value,
        bead:wbBead.value, margin:wbMargin.value, kind:(f||priorStl)?"stl":"wall",
        units:wbUnits.value, reuse:priorStl?"1":"0",
        shape:wbShape.value,
        substrate_margin:wbSubstrateMargin.value,
        ...demoSizes(!(f||priorStl)),
        substrate_registration:wbSubstrateRegistration.value,layer_sense:wbLayerSense.value,
        slicer:wbSlicer.value,contours:wbContours.value,stepover:wbStepover.value,infill:wbInfill.value,
        infill_angle:wbInfillAngle.value,infill_rotation:wbInfillRotation.value,travel_lift:wbTravelLift.value,
        filler:wbFiller.value, wire:wbWire.value, travel:wbTravel.value, wfs:wbWfs.value.trim(), ctwd:wbCtwd.value.trim(),
        ox:wbOx.value, oy:wbOy.value, oz:wbOz.value, sub:wbSub.value,
        yaw:wbYaw.value,rotX:(f||priorStl)?wbRotX.value:"0",rotY:(f||priorStl)?wbRotY.value:"0",
        lay_flat:(f||priorStl)?wbLayFlat.value:"0",fixture:wbFixture.value,repair:wbRepair.value,
        thermal:wbThermal.value,cooling:wbCooling.value,
        thermal_profile:wbThermal.value==="profile"?wbThermalProfile.value:"",
        name:f?f.name:(priorStl?loadedStl:wbShape.options[wbShape.selectedIndex].text),
        ...(window.waamLibrary?.params()||{}),
      });
      const body=f?await f.arrayBuffer():null;
      const r=await fetch(api("/api/slice?"+qs),{method:"POST",body});
      const j=await r.json();
      if(j.reason==="stale") window.waamDraft.stale();
      window.waamLibrary?.sliced(j);
      if(!r.ok || !j.ok) throw new Error((j.error||"Slice failed")+(j.archive_warning?" "+j.archive_warning:""));
      loadedStl=j.state.meta.stl ? j.state.meta.title : null;
      if(f)selectedSource=sourceIdentity({kind:"upload",name:f.name});
      else if(!window.waamLibrary?.params().library)selectedSource=loadedStl?
        sourceIdentity({kind:"upload",name:loadedStl}):sourceIdentity({kind:"none"});
      restoredDraftSource=null;
      document.getElementById("stlFile").value="";
      const previewWarning=await hydrateState(j.state); renderWorkbench(j.state);
      switchStep("place");frameView("part");
      const warnings=[j.archive_warning,previewWarning].filter(Boolean);
      if(warnings.length) msg.textContent=warnings.join(" ");
      syncSliceFeedback();
      return true;
    }catch(e){msg.textContent="Slice failed: "+e.message;syncSliceFeedback(true);return false;}finally{endWb();}
  };
  document.getElementById("wbCellProfile").onchange=async()=>{
    wbOx.value="0";wbOy.value="0";wbOz.value="0";
    await document.getElementById("sliceBtn").onclick();
    frameView("cell");
  };
  document.getElementById("wbHcCleaner").onchange=()=>document.getElementById("sliceBtn").onclick();


  if(S.meta.workspace){
    document.getElementById("wbWho").textContent=" - "+S.meta.workspace;
  }
  if(window.WaamLibrary){
    window.waamLibrary=window.WaamLibrary.create({document,api,
      fetch:(...args)=>fetch(...args),state:()=>S,file:()=>document.getElementById("stlFile").files[0],
      loadedName:()=>loadedStl,draftSource:()=>restoredDraftSource,busy:()=>wbBusy||UI.busy,
      blocked:()=>window.waamDraft?.blocked(),
      begin:message=>{const started=beginWb();if(started)setBusy(message);return started;},end:endWb,
      sourceChanged:()=>{const file=document.getElementById("stlFile").files[0];loadedStl=null;
        selectedSource=sourceIdentity(file?
          {kind:"upload",name:file.name,pending:true}:{kind:"none"});restoredDraftSource=selectedSource;
        clearArcSchedule();renderStlChip();refreshDraft();},
      adopt:(entry,source)=>{selectedSource=sourceIdentity(source)||sourceIdentity({kind:"none"});restoredDraftSource=null;
        loadedStl=entry?.kind==="stl"?entry.name:null;document.getElementById("stlFile").value="";
        clearArcSchedule();renderStlChip();refreshDraft();},
      feedback:(text,error)=>{document.getElementById("wbMsg").textContent=text;syncSliceFeedback(error);},
    });
  }


  (async()=>{
    let guest=false, identityKnown=false;
    try{
      const r=await fetch(api("/api/whoami"));
      const j=await r.json();
      if(j.ok){
        if(j.gen!=null&&S.meta.gen!=null&&j.gen!==S.meta.gen)window.waamDraft.stale();
        identityKnown=true;
        guest=!!j.guest;


        S.meta.email=j.email||null;
        showIdentity(j.email, !!j.sso);
        showSignIn(j.sso ? null : j.sign_in_url, j.sign_in_label);
        showAdminLink(!!j.admin);
        showOffer(j);
        setStudioIdentity(j);


        if(j.guest){
          IS_GUEST=true;
          const chip=document.getElementById("wsChip");
          chip.textContent="Guest";
          chip.style.color="var(--weld)";
          chip.style.display="";
          chip.title="Sign in to save builds and export files.";


          lockGuestControls();
        }
        document.getElementById("wbWho").textContent=
          " - "+(j.workspace||S.meta.workspace)+(j.email?" ("+j.email+")":"");
        await window.waamLibrary?.setIdentity(j);
      }
    }catch(e){                                                          }finally{
      if(!identityKnown) await window.waamLibrary?.setIdentity({unavailable:true});
      if(!identityKnown) setStudioIdentity({unavailable:true});
      if(!guest) unlockAccountControls();
      await window.waamRuns?.refresh();
      await window.waamAuto?.refresh();
    }
  })();
  let generationChecking=false;
  async function verifyBuildGeneration(){
    if(generationChecking||wbBusy)return;
    generationChecking=true;
    try{
      const r=await fetch(api("/api/whoami")),j=await r.json();
      if(r.ok&&j.ok&&j.gen!=null&&S.meta.gen!=null&&j.gen!==S.meta.gen)window.waamDraft.stale();
    }catch(e){                                                              }
    finally{generationChecking=false;}
  }
  window.addEventListener("focus",verifyBuildGeneration);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)void verifyBuildGeneration();});


  function adoptWb(){
    document.getElementById("wbArcSchedule").value="";
    try{
      const out=JSON.parse(sessionStorage.getItem(UI.fields)||"{}");
      if(out.version===1&&out.fields)out.fields.wbArcSchedule="";else out.wbArcSchedule="";
      sessionStorage.setItem(UI.fields, JSON.stringify(out));
    }catch(e){                                                }
  }
  async function wbListJobs(){
    const sel=document.getElementById("wbJobs");
    try{
      const r=await fetch(api("/api/jobs"));
      if(!r.ok){


        sel.innerHTML="";
        const o=document.createElement("option");
        o.value="";
        o.textContent = r.status===403 ? "Sign in to save and load builds"
                                       : "jobs unavailable (HTTP "+r.status+")";
        sel.appendChild(o);
        return;
      }
      const j=await r.json();
      sel.innerHTML="";
      for(const job of (j.jobs||[])){
        const o=document.createElement("option");
        o.value=job.name;
        const date=job.saved_at?new Date(job.saved_at):null;
        o.textContent=job.name+(date?"  ("+(Number.isNaN(date.getTime())?job.saved_at:
          date.toLocaleString(undefined,{dateStyle:"short",timeStyle:"short"})+" "+
          Intl.DateTimeFormat(undefined,{timeZoneName:"short"}).formatToParts(date).find(part=>part.type==="timeZoneName").value)+")":"");
        sel.appendChild(o);
      }
      if(!sel.options.length){
        const o=document.createElement("option");
        o.value=""; o.textContent="No saved builds";
        sel.appendChild(o);
      }
    }catch(e){                                                         }
  }
  wbListJobs();
  document.getElementById("saveBtn").onclick=async()=>{
    if(wbBusy) return;
    const msg=document.getElementById("wbMsg");
    if(IS_GUEST){msg.textContent="Sign in to save builds.";return;}
    if(!guardWbWrite())return;
    if(buildDraftError()){msg.textContent=buildDraftError();return;}
    if(savedBuildNameError()){msg.textContent=savedBuildNameError();return;}
    if(substrateRegistrationError()){msg.textContent=substrateRegistrationError();return;}
    const name=document.getElementById("wbJobName").value.trim();
    if(!name){msg.textContent="Enter a name for this build.";return;}
    msg.textContent="Saving build…";
    try{
      const r=await fetch(api("/api/save?name="+encodeURIComponent(name)),{method:"POST"});
      if(!r.ok){
        let j=null; try{j=await r.json();}catch(e){}
        msg.textContent = j&&j.error ? j.error : ("Save failed (HTTP "+r.status+").");
        return;
      }
      const j=await r.json();
      if(j.ok){msg.textContent="Saved as "+j.job+"."; wbListJobs();}
      else{msg.textContent="Request failed: "+j.error;}
    }catch(e){msg.textContent="Request failed: "+e.message;}
  };


  async function attestPlatter(asfs=[],schedule=""){
    const dlg=document.getElementById("platterDlg");
    document.getElementById("pdErrors").textContent="";
    document.getElementById("pdClear").checked=false;

    const asfRow=document.getElementById("pdAsfRow"),asfCheck=document.getElementById("pdAsf");
    asfRow.hidden=asfs.length<2;asfCheck.checked=false;
    document.getElementById("pdAsfList").textContent=asfs.join(", ")+(schedule?" (by layer: "+schedule+")":"");
    document.getElementById("pdBlock").value=wbFixture.value==="1"?"1":"0";
    const who=document.getElementById("pdBy");
    if(!who.value) who.value=(S.meta.email||S.meta.workspace||"");
    const fx=document.getElementById("pdFixture");
    fx.innerHTML='<option value="">None (bare platter)</option>';
    try{
      const r=await fetch(api("/api/fixtures"));
      if(r.ok){
        const j=await r.json();
        for(const n of (j.fixtures||[])){const o=document.createElement("option");o.value=n;o.textContent=n;fx.appendChild(o);}
        if(j.active) fx.value=j.active;
      }


    }catch(e){}
    return new Promise(res=>{
      const form=document.getElementById("platterForm");
      const done=v=>{form.onsubmit=null;dlg.oncancel=null;document.getElementById("pdCancel").onclick=null;dlg.close();res(v);};
      document.getElementById("pdCancel").onclick=()=>done(null);
      dlg.oncancel=ev=>{ev.preventDefault();done(null);};
      form.onsubmit=ev=>{
        ev.preventDefault();
        const errors=[];
        if(!document.getElementById("pdClear").checked) errors.push("Inspect the physical platter before confirming it is clear.");
        if(!who.value.trim()) errors.push("Enter the name of the person who checked the platter.");
        if(asfs.length>1&&!asfCheck.checked) errors.push("Confirm each listed ASF/AEF exists on the pendant.");
        if(S.meta.placement && (document.getElementById("pdBlock").value==="1")!==S.meta.placement.fixture)
          errors.push("Match the training block setting in Place to the physical setup, then check again.");
        document.getElementById("pdErrors").textContent=errors.join("\\n");
        if(errors.length) return;
        done("&clear=1&block="+document.getElementById("pdBlock").value
             +"&fixture="+encodeURIComponent(fx.value)
             +"&by="+encodeURIComponent(who.value.trim())+(asfs.length>1?"&asf_attest=1":""));
      };
      dlg.showModal();
    });
  }
  for(const link of document.querySelectorAll('#wb a[download="part.gcode"]'))link.onclick=async event=>{
    event.preventDefault();
    const msg=document.getElementById("wbMsg");
    if(IS_GUEST){msg.textContent="Sign in to export files.";return;}
    const error=substrateRegistrationError();
    if(error){msg.textContent=error;return;}
    if(!beginWb())return;
    try{
      const r=await fetch(api("/api/gcode",{run_name:runName()})),run=await filedInRun(r);
      if(!r.ok){
        const j=await r.json().catch(()=>null);
        throw new Error(j?.error||("G-code export refused (HTTP "+r.status+")."));
      }
      const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");
      a.href=url;a.download="part.gcode";document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
      msg.textContent="Downloaded part.gcode.";
      if(r.headers.get("X-Waam-Archive")==="failed")msg.textContent+=" Server copy failed. Keep the downloaded file.";
      if(run==="failed")msg.textContent+=" Run filing failed. Keep the downloaded file.";
    }catch(e){msg.textContent="Request failed: "+e.message;}finally{endWb();}
  };


  document.getElementById("emitBtn").onclick=async()=>{
    if(wbBusy) return;
    const msg=document.getElementById("wbMsg");
    if(IS_GUEST){msg.textContent="Sign in to export robot programs.";return;}
    if(window.waamAuto?.enabled()){await window.waamAuto.exportCycle();return;}
    const errors=validateEmit(true);
    const ready=readyForRobotExport();
    if(errors.length){msg.textContent=errors.join("\\n");return;}
    if(!ready) return;
    const name=document.getElementById("wbJbiName").value;
    const arc=String(document.getElementById("wbArc").valueAsNumber);
    const schedule=arcSchedule();
    const folder=jobFolder();
    const att=await attestPlatter(schedule?scheduleAsfs(schedule):[],schedule);
    if(!att){msg.textContent="Export cancelled. Platter check not confirmed.";return;}
    if(!beginWb()) return;
    setBusy("Checking clearance and emitting the robot job…");
    msg.textContent="Checking clearance and preparing the robot job…";
    try{
      const r=await fetch(api("/api/jbi?"+arcQuery()+"&name="+encodeURIComponent(name)+"&folder="+encodeURIComponent(folder)+"&smooth="+(document.getElementById("wbSmooth").checked?"1":"0")+att+printLayers()+interpassQuery()+thermalTraceQuery()+ctwdQuery(),{run_name:runName()}));
      const run=await filedInRun(r);
      if(!r.ok){
        let j=null; try{j=await r.json();}catch(e){}
        msg.textContent=j&&j.error?j.error:("Export refused (HTTP "+r.status+").");
        return;
      }
      const blob=await r.blob();
      const url=URL.createObjectURL(blob);
      const a=document.createElement("a");
      a.href=url; a.download=name+".JBI";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      msg.textContent="Exported "+name+".JBI"+(folder&&folder!=="NONE"?" for job folder "+folder:"")+". Verify "+(schedule?"each ASF# in the layer schedule "+schedule:"ASF#"+arc)+" and complete an arc-off trial before welding."+passSummary(r);
      const notes=r.headers.get("X-Waam-Notes");
      if(notes)msg.textContent+=" Note: "+notes+".";
      if(r.headers.get("X-Waam-Archive")==="failed") msg.textContent+=" Server copy failed. Keep the downloaded file.";
      if(run==="failed")msg.textContent+=" Run filing failed. Keep the downloaded file.";
    }catch(e){msg.textContent="Request failed: "+e.message;}finally{endWb();}
  };


  document.getElementById("emitRosBtn").onclick=async()=>{
    if(wbBusy) return;
    const msg=document.getElementById("wbMsg");
    if(IS_GUEST){msg.textContent="Sign in to export robot programs.";return;}
    const errors=validateEmit(false);
    const ready=readyForRobotExport();
    if(errors.length){msg.textContent=errors.join("\\n");return;}
    if(!ready) return;
    const name=document.getElementById("wbJbiName").value;
    const att=await attestPlatter();
    if(!att){msg.textContent="Export cancelled. Platter check not confirmed.";return;}
    if(!beginWb()) return;
    setBusy("Checking clearance and emitting ROS 2 motion…");
    msg.textContent="Checking clearance and preparing ROS 2 motion…";
    try{
      const r=await fetch(api("/api/ros2?name="+encodeURIComponent(name)+att+printLayers()+interpassQuery()+ctwdQuery(),{run_name:runName()}));
      const run=await filedInRun(r);
      if(!r.ok){
        let j=null; try{j=await r.json();}catch(e){}
        msg.textContent=j&&j.error?j.error:("Export refused (HTTP "+r.status+").");
        return;
      }
      const segs=r.headers.get("X-Waam-Segments")||"?";
      const blob=await r.blob();
      const url=URL.createObjectURL(blob);
      const a=document.createElement("a");
      a.href=url; a.download=name+".ros2.json";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      msg.textContent="Exported "+name+".ros2.json ("+segs+" motion segments). Run an arc-off trial before use."+passSummary(r);
      if(r.headers.get("X-Waam-Archive")==="failed") msg.textContent+=" Server copy failed. Keep the downloaded file.";
      if(run==="failed")msg.textContent+=" Run filing failed. Keep the downloaded file.";
    }catch(e){msg.textContent="Request failed: "+e.message;}finally{endWb();}
  };
  document.getElementById("loadBtn").onclick=async()=>{
    const msg=document.getElementById("wbMsg");
    if(IS_GUEST){msg.textContent="Sign in to load saved builds.";return;}
    const name=document.getElementById("wbJobs").value;
    if(!name){msg.textContent="Choose a saved build.";return;}
    if(!beginWb()) return;
    msg.textContent="Loading "+name+"…";
    setBusy("Loading saved build…");
    rememberWb();
    try{
      const r=await fetch(api("/api/load?name="+encodeURIComponent(name)),{method:"POST"});
      if(!r.ok){
        let j=null; try{j=await r.json();}catch(e){}
        msg.textContent = j&&j.error ? j.error : ("Load failed (HTTP "+r.status+").");
        return;
      }
      const j=await r.json();
      if(j.ok){if(j.note) window.alert(j.note); adoptWb();persistWbOnPagehide=false;
        rememberView("review");location.reload();}
      else{msg.textContent="Request failed: "+j.error;}
    }catch(e){msg.textContent="Request failed: "+e.message;}finally{endWb();}
  };
}
%%CONTROLS%%
%%PLACEMENT%%

let studioResumesView=!!location.hash;
let studioStoredViewOwner=null;
try{
  const saved=sessionStorage.getItem(UI.storage);
  const savedView=JSON.parse(saved||"{}");
  UI.preferencesPending=!!savedView.preferencesPending;
  studioResumesView=studioResumesView || (!!saved && !UI.preferencesPending);
  studioStoredViewOwner=savedView.owner||null;
}catch(e){}
let studioInitialView=null,studioPathLayers=null;
function studioViewSignature(){
  return JSON.stringify({step:UI.step,view:UI.view,cursor,camera:camera(),
    flags:[...document.querySelectorAll(".layers-body input")].map(el=>[el.id,el.checked])});
}
async function setStudioIdentity(identity){
  let owner=null;
  if(identity.ok || identity.offline){
    owner=identity.sso?identity.library_owner:identity.offline?"offline":identity.guest?"guest":"shared";


    if(studioStoredViewOwner!==owner && !location.hash && studioInitialView===studioViewSignature()){
      studioResumesView=false;
      UI.preferencesPending=true;
      for(const el of document.querySelectorAll(".layers-body input")) el.checked=el.defaultChecked;
      studioPathLayers=null;
      studioInitialView=studioViewSignature();
    }
    UI.viewOwner=owner;rememberView();
  }
  const ready=await window.waamSettings?.setIdentity(identity);
  if(ready && owner && UI.viewOwner===owner){
    UI.preferencesPending=false;rememberView();
  }
  return ready;
}
function applyStudioPreferences(next,previous,{initial=false}={}){

  if(initial) UI.preferencesPending=false;


  const canSetView=!initial || (!studioResumesView && studioInitialView===studioViewSignature());
  if(canSetView){
    const frameChanged=initial || next.default_frame!==previous.default_frame;
    const cameraChanged=initial || next.default_camera!==previous.default_camera;
    if(cameraChanged){
      yaw=next.default_camera==="top"?0:-.85;
      pitch=next.default_camera==="top"?Math.PI/2:.60;
    }
    if(frameChanged) frameView(next.default_frame);
    if(initial?next.path_only:next.path_only!==previous.path_only){
      const ids=["showPart","showTorch","showScene","showScale","showFrame","showWalls","showFumeHood","showBoxes","showRobot","showConduit"];
      if(next.path_only){
        studioPathLayers=Object.fromEntries(ids.map(id=>[id,document.getElementById(id).checked]));
        for(const id of ids) document.getElementById(id).checked=false;
      }else{
        for(const id of ids){
          const el=document.getElementById(id);
          el.checked=studioPathLayers?studioPathLayers[id]:el.defaultChecked;
        }
        studioPathLayers=null;
      }
    }
    rememberView();draw();
  }
  if(next.reduce_motion!==previous.reduce_motion || next.reduce_motion) setPlaying(false);
  if(initial) rememberView();
}
initializeStudioUI();
initializeCollisionUI();
window.waamPlacement=window.WaamPlacement.create({
  validate:el=>UI.validateNumber?.(el),
  changed:()=>window.waamDraft?.refresh(),
  document,canvas,state:()=>S,step:()=>UI.step,busy:()=>UI.busy,
  blocked:()=>window.waamDraft?.blocked(),
  project:proj,dpr:()=>window.devicePixelRatio||1,draw,
  pause:()=>setPlaying(false),clearance:renderClearance,
  camera,restoreCamera,
  apply:()=>document.getElementById("placeBtn").onclick()
});
const visualDetail=document.getElementById("visualDetail");
if(visualDetail){
  visualDetail.value=window.WaamAssets.mode;
  if(lightPreview()) document.getElementById("showBoxes").checked=true;
  visualDetail.onchange=async()=>{
    if(UI.busy){visualDetail.value=window.WaamAssets.mode;return;}
    window.waamPlacement?.cancel();
    UI.busy=true; visualDetail.disabled=true;
    setPlaying(false);
    const previousMode=window.WaamAssets.mode;
    const busy=document.getElementById("busy");busy.hidden=false;
    document.getElementById("busyText").textContent="Loading preview…";
    try{
      window.WaamAssets.setMode(visualDetail.value);
      await window.WaamAssets.prepare(S,rendererAsset);
      if(gpu){gpu.dispose();gpu=null;}
      if(!lightPreview()) gpu=window.WaamRenderer.create({canvas:document.getElementById("gl"),state:S});
      else document.getElementById("showBoxes").checked=true;
      updateDisplay();renderCollisionUI();draw();
    }catch(e){
      window.WaamAssets.setMode(previousMode);visualDetail.value=previousMode;
      await window.WaamAssets.hydrate(S);
      document.getElementById("wbMsg").textContent="Preview failed: "+e.message;
    }
    finally{UI.busy=false;visualDetail.disabled=false;busy.hidden=true;}
  };
}
window.waamHelp=window.WaamHelp.create({
  document,state:()=>S,guest:()=>IS_GUEST,busy:()=>UI.busy,
  showStep:step=>openStep(step,true),pause:()=>setPlaying(false)
});
window.onresize=resizeStudioView;


if(window.ResizeObserver){
  const view=document.getElementById("view");let seen=view.clientWidth+"x"+view.clientHeight;
  new ResizeObserver(()=>{const size=view.clientWidth+"x"+view.clientHeight;if(size!==seen){seen=size;resizeStudioView();}}).observe(view);
}
window.waamWindow=window.WaamWindow.create({document,resize:window.ResizeObserver?null:resizeStudioView});

const hm=location.hash.match(/m=(\\d+)/);
if(hm){cursor=Math.min(N-1,+hm[1]); slider.value=Math.floor(cursor);}
const hz=location.hash.match(/z=([\\d.]+)/);   if(hz) zoom=+hz[1];
const hp=location.hash.match(/p=(-?[\\d.]+)/); if(hp) pitch=+hp[1];
const hy=location.hash.match(/y=(-?[\\d.]+)/); if(hy) yaw=+hy[1];
studioInitialView=studioViewSignature();
window.waamSettings=window.WaamSettings.create({
  document,api,fetch:(...args)=>fetch(...args),pause:()=>setPlaying(false),
  busy:()=>UI.busy,blocked:()=>window.waamDraft?.blocked(),
  applyPreview:applyStudioPreferences,resize:resizeStudioView
});
if(!S.meta.workbench) setStudioIdentity({offline:true});
function markDraftReady(){
  if(!window.waamDraft||window.waamDraft.ready)return;
  window.waamDraft.ready=true;
  document.dispatchEvent(new CustomEvent("waam:draft-ready",{detail:{version:1}}));
}
markDraftReady();
draw();%%WRAPEND%%
</script>
</body>
</html>
"""
