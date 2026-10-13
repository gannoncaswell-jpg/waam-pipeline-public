from __future__ import annotations

import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import cell_lc as lc
from waam_pipeline.studio import build_state, render_studio
from waam_pipeline.toolpath import Move, Toolpath


def inspection_page(state, path, note, sibling, sibling_label):
    html = render_studio(state)
    banner = ('<section style="border-color:#b78934"><b>Geometry inspection</b>'
              f'<p>{escape(note)}</p><a style="color:#79b8ff" href="{sibling}">'
              f'{escape(sibling_label)}</a></section>')
    html = html.replace('id="step-review" role="tabpanel" aria-labelledby="tab-review" hidden>', 'id="step-review" role="tabpanel" aria-labelledby="tab-review" hidden>' + banner, 1)

    html = html.replace("clean at required margin", "Geometry review - clearance not evaluated")
    if not state.get("robot"):
        html = html.replace('id="showTorch" checked', 'id="showTorch"')
    path.write_text(html, encoding="utf-8")
    return path


def main():
    cm = lc._cell_models()
    origin = lc.DEFAULT_PART_ORIGIN

    path = Toolpath([Move(0, 0, 150, 1000, False, 0, 0)])
    cell = build_state(path, title="LC50S - corrected model review", cell_name="LC50S",
                       scene=lc.lc_scene(origin, training_block=False),
                       scene_meshes=lc.lc_scene_meshes(origin, training_block=False),
                       robot=lc.robot_shadow(path, origin))
    out = ROOT / "docs"
    print(inspection_page(cell, out / "cell-model-review.html",
          "Base rails, anchor feet, pedestal connections and the rotating spool housing now follow the documented arrangement. Housing/support dimensions and conduit routing are provisional; the conduit follows the spool and feeder as the arm moves. The rack remains on the side you confirmed. Gray torch is the controller envelope; gold marks the TCP. Clearance is not evaluated here.",
          "waam-fixture-review.html", "Inspect the shared WAAM fixture"))
    reference = build_state(Toolpath([Move(0, 0, 0, None, False, 0, 0)]),
                            title="CAD_FIXTURE_001 - reference asset, not installed",
                            scene_meshes={"waam_fixture": cm.waam_fixture().reshape(-1).tolist()})
    print(inspection_page(reference, out / "waam-fixture-review.html",
          "Imported source STL: 508 x 254 mm footprint, 50.8 mm overall height, including raised bars. Underside is Z=0. Mounting position, clamps, and work-surface calibration are unverified. This asset is absent from the default live scene.",
          "cell-model-review.html", "Return to the cell model"))


if __name__ == "__main__":
    main()
