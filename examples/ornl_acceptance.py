'Run the real pinned ORNL engine on analytical solids (no robot emission).'


from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline import ornl
from waam_pipeline.studio import build_state, render_studio


def block(width=60, depth=40, height=6, x=0):
    vertices = np.array([[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],
                         [-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]], float)
    vertices *= [width / 2, depth / 2, height]
    vertices += [x, 0, 0]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    return vertices[faces, :]


def ring():
    outer = np.array([[-40,-30,0],[40,-30,0],[40,30,0],[-40,30,0]], float)
    inner = np.array([[-12,-8,0],[12,-8,0],[12,8,0],[-12,8,0]], float)
    top = np.array([0,0,6])
    tris = []
    def quad(a,b,c,d):
        tris.extend([[a,b,c],[a,c,d]])
    for k in range(4):
        j = (k + 1) % 4
        quad(outer[k] + top, outer[j] + top, inner[j] + top, inner[k] + top)
        quad(outer[j], outer[k], inner[k], inner[j])
        quad(outer[k], outer[j], outer[j] + top, outer[k] + top)
        quad(inner[j], inner[k], inner[k] + top, inner[j] + top)
    return np.array(tris)


def crosses_hole(a, b):

    low, high = 0.0, 1.0
    for origin, end, extent in zip(a[:2], b[:2], (12 - 1e-5, 8 - 1e-5)):
        delta = end - origin
        if abs(delta) < 1e-12:
            if abs(origin) > extent:
                return False
        else:
            t1, t2 = sorted(((-extent - origin) / delta, (extent - origin) / delta))
            low, high = max(low, t1), min(high, t2)
    return low <= high


def block_coverage(tp, width):

    points = np.array([(x,y) for x in np.arange(-29.5,30,1) for y in np.arange(-19.5,20,1)])
    distance = np.full(len(points), np.inf)
    for a,b in zip(tp.moves,tp.moves[1:]):
        if not b.deposit or b.layer != 0:
            continue
        start,end = np.array(a.target[:2]),np.array(b.target[:2])
        delta=end-start
        ratio=np.clip(((points-start) @ delta) / max(float(delta @ delta),1e-20),0,1)
        distance=np.minimum(distance,np.linalg.norm(points-(start+ratio[:,None]*delta),axis=1))
    return float(np.mean(distance <= width/2 + 1e-5))


def run(output):
    options=ornl.Options(2,6,180)
    cases={"block-lines":(block(),options), "block-concentric":(block(),replace(options,pattern="concentric")),
           "block-contours":(block(),replace(options,pattern="none")),
           "through-hole":(ring(),options),
           "separate-islands":(np.concatenate([block(30,30,x=-35),block(30,30,x=35)]),options)}
    report={}
    for name,(tri,settings) in cases.items():
        directory=output/name
        tp,meta=ornl.slice_mesh(tri,settings,artifacts=directory)
        assert len(tp.layers())==3 and not tp.moves[0].deposit
        assert {round(m.z,4) for m in tp.moves if m.deposit}=={2,4,6}
        if name=="through-hole":
            assert not any(crosses_hole(a.target,b.target) for a,b in zip(tp.moves,tp.moves[1:]) if b.deposit)
        if name=="separate-islands":
            assert all(a.x*b.x>0 for a,b in zip(tp.moves,tp.moves[1:]) if b.deposit)
        if name=="block-contours":
            assert meta["region_length_mm"]["infill"]==0
        else:
            assert meta["region_length_mm"]["infill"]>0
        if name=="block-lines":
            directions=[]
            for layer in range(3):
                segments=[(a,b) for a,b in zip(tp.moves,tp.moves[1:]) if b.deposit and b.region=="infill" and b.layer==layer]
                a,b=max(segments,key=lambda ab:math.dist(ab[0].target,ab[1].target))
                directions.append("x" if abs(b.x-a.x)>abs(b.y-a.y) else "y")
            assert directions==["y","x","y"]
            meta["raster_directions"]=directions
            meta["nominal_area_coverage_fraction"]=block_coverage(tp,settings.bead_width_mm)
            assert meta["nominal_area_coverage_fraction"]>.98
        meta.update(moves=len(tp.moves),layers=len(tp.layers()),arc_starts=sum(e=="ARCON" for e,_ in tp.arc_events()))
        (directory/"metadata.json").write_text(json.dumps(meta,indent=2)+"\n",encoding="utf-8")
        state=build_state(tp,title="ORNL · "+name,part_mesh=tri,slicing=meta)
        (directory/"preview.html").write_text(render_studio(state),encoding="utf-8")
        report[name]=meta
        print(name,tp.summary(),flush=True)
    (output/"acceptance.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    run(parser.parse_args().output)
