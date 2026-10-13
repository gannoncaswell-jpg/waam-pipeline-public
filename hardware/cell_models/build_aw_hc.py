'Build the reviewed AW-HC display assets from an extracted vendor archive.'


import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import struct
import zlib
import numpy as np
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("hsf",ROOT/"hardware/cell_models/import_motosim_hsf.py")
hsf=importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsf)

def extract_triangle_records(path):


    raw=path.read_bytes()
    start=min(p for p in (raw.find(b"\x78\xda"),raw.find(b"\x78\x9c")) if p>=0)
    stream=zlib.decompressobj()
    buf=stream.decompress(raw[start:],128*1024*1024)
    if not stream.eof:
        raise ValueError("Incomplete or oversized AW-HC stream")
    cursor=0;shells=[]
    while True:
        pos=buf.find(b"\x53\x00",cursor)
        if pos<0 or pos+12>len(buf):break
        cursor=pos+1
        count=struct.unpack_from("<i",buf,pos+8)[0]
        end=pos+12+count*12
        if not 3<=count<=2_000_000 or end>len(buf):continue
        vertices=np.frombuffer(buf,"<f4",count*3,offset=pos+12).reshape(-1,3)
        if (not np.isfinite(vertices).all() or np.max(np.abs(vertices))>4000
                or np.ptp(vertices,axis=0).max()<1e-5):continue
        faces,nextpos=hsf._parse_faces(buf,end,count)
        if not faces:continue
        if any(len(face)!=3 for face in faces):
            raise ValueError("Unsupported polygon in pinned AW-HC furniture")
        cursor=nextpos
        shells.append({"vertices":vertices.copy(),"faces":faces})
    if not shells:raise ValueError("No triangle records recovered")
    return {"shells":shells}
def main():
    import argparse
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, required=True, help="extracted AW-HC directory")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "hardware/cell_models/meshes")
    args = ap.parse_args()
    SOURCE, OUT = args.source.resolve(), args.out_dir.resolve()
    approved = json.loads((ROOT / "hardware/cell_models/meshes/aw_hc_import.json").read_text())
    if hashlib.sha256((SOURCE / "AW-HC.vcl").read_bytes()).hexdigest() != approved["vcl_sha256"]:
        raise ValueError("AW-HC scene revision differs from the reviewed source")
    for asset in approved["assets"].values():
        path = (SOURCE / asset["source"]).resolve()
        if not path.is_relative_to(SOURCE) or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError("AW-HC component differs from the reviewed source: " + asset["source"])
    OUT.mkdir(parents=True, exist_ok=True)
    vcl=(SOURCE/"AW-HC.vcl").read_text()
    records={}
    for block in re.findall(r"\bMODEL\s*\{([^{}]*)\}",vcl):
        fields=dict(re.findall(r"^\s*([A-Z0-9_]+)=([^;]*);",block,re.M))
        records[fields["NAME"]]=fields

    def reduce_shell(vertices, faces, budget):
        ids=np.asarray(faces,dtype=np.int64)


        if len(ids)<=budget or np.min(np.ptp(vertices,axis=0)) < 1e-6:
            return vertices[ids].astype(np.float32)
        points=vtk.vtkPoints()
        points.SetData(numpy_to_vtk(vertices,deep=True))
        cells=vtk.vtkCellArray()
        cells.ImportLegacyFormat(numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(ids),3,dtype=np.int64),ids]).ravel(),deep=True))
        poly=vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)


        clean=vtk.vtkCleanPolyData()
        clean.SetInputData(poly)
        clean.SetTolerance(0.0)
        clean.Update()
        dec=vtk.vtkQuadricDecimation()
        dec.SetInputConnection(clean.GetOutputPort())
        dec.SetTargetReduction(1-budget/len(ids))
        dec.VolumePreservationOn()
        dec.Update()
        result=dec.GetOutput()
        pts=vtk_to_numpy(result.GetPoints().GetData())
        fs=vtk_to_numpy(result.GetPolys().GetConnectivityArray()).reshape(-1,3)
        return pts[fs].astype(np.float32)

    budgets={"Table":18000,"BackPanel":14000,"Curtian-Left":6000,"Curtian-Right":6000,
             "Balancer":3000,"Pendant_Bracket":2000,"Pendant":4000,"Fume__Extraction":10000,
             "Yaskawa_Reamer":10000,"Tregaskiss_Reamer":12000,"Binzel_Reamer":10000}
    arrays={}
    audit={"source_archive":"AW-HC.zip","source_archive_sha256":"74a5128f86eea7071ce5e4e8447d2f83faeb43c495ad3fa945cec8e6d0abac81",
           "vcl_sha256":hashlib.sha256((SOURCE/"AW-HC.vcl").read_bytes()).hexdigest(),
           "units":"mm", "source_frame":"AW-HC world, Z up",
           "reduction":"planar shells retained; other shells use exact duplicate vertex welding then quadric decimation", "assets":{}}


    for name,budget in budgets.items():
        field=records[name]
        filename=field["FILENAME"].replace("%CELPATH%\\","").replace("\\","/")
        source=SOURCE/filename
        result=extract_triangle_records(source)
        shells=result["shells"]
        total=sum(len(s["faces"] or []) for s in shells)
        out=[]
        shell_audit=[]
        for index,shell in enumerate(shells):
            faces=shell["faces"]
            vertices=shell["vertices"]
            entry={"shell":index,"vertices":len(vertices),"source_faces":len(faces or []),
                   "source_bounds_mm":[vertices.min(0).tolist(),vertices.max(0).tolist()]}
            if not faces:
                entry["omitted_reason"]="No supported face connectivity; not reconstructed"
            else:
                if any(len(f)!=3 for f in faces):
                    raise ValueError("Non-triangle source shell: "+name)
                tri=reduce_shell(vertices,faces,max(32,round(budget*len(faces)/total)))

                tri=tri[:,:,[0,2,1]].copy()
                tri[:,:,1]*=-1
                area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)
                tri=tri[area>1e-10]
                out.append(tri)
                entry["display_triangles"]=len(tri)
            shell_audit.append(entry)
        mesh=np.concatenate(out)
        arrays[name]=mesh
        audit["assets"][name]={"source":filename,"sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                               "source_faces":total,"display_triangles":len(mesh),"shells":shell_audit,
                               "bounds_mm":[mesh.min((0,1)).tolist(),mesh.max((0,1)).tolist()],
                               "parent":field["PARENT"],"axis6":field["AXIS6"],"vcl_scale":field["SCALE"]}
        print(name,total,"->",len(mesh),flush=True)
    np.savez_compressed(OUT/"aw_hc_display.npz",**arrays)
    audit["mesh_sha256"]=hashlib.sha256((OUT/"aw_hc_display.npz").read_bytes()).hexdigest()
    (OUT/"aw_hc_import.json").write_text(json.dumps(audit,indent=2)+"\n")


if __name__ == "__main__":
    main()
