'Reproducible engine/FK consistency audit; NOT physical robot calibration.'


from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from waam_pipeline.robots import ar1440, hc10

MODELS = {"ar1440": (ar1440, ar1440.AR1440_JOINTS, "ar1440.urdf"),
          "hc10dtp": (hc10, hc10.HC10DTP_JOINTS, "hc10dtp.urdf")}


def rotation_error_deg(a, b):
    r = np.asarray(a) @ np.asarray(b).T
    sine = np.linalg.norm([r[2, 1]-r[1, 2], r[0, 2]-r[2, 0],
                           r[1, 0]-r[0, 1]]) / 2
    return float(np.degrees(np.arctan2(sine, (np.trace(r)-1)/2)))


def audit_model(name, *, samples=500, seed=20260908, urdf_path=None,
                position_tol_mm=0.1, rotation_tol_deg=0.001):
    import pybullet as p
    if samples < 1:
        raise ValueError("samples must be positive")
    model, joints, filename = MODELS[name]
    path = Path(urdf_path) if urdf_path is not None else ROOT / "robots" / filename
    rng = np.random.default_rng(seed)
    lo = np.array([j.lower_deg for j in joints])
    hi = np.array([j.upper_deg for j in joints])


    boundaries = [np.zeros(6)]
    for axis in range(6):
        for value in (lo[axis], hi[axis]):
            q = np.zeros(6)
            q[axis] = value
            boundaries.append(q)
    boundaries.extend(np.where([(mask >> i) & 1 for i in range(6)], hi, lo)
                      for mask in range(64))
    postures = [*rng.uniform(lo, hi, (samples, 6)), *boundaries]
    client = p.connect(p.DIRECT)
    if client < 0:
        raise RuntimeError("PyBullet DIRECT connection failed")
    try:
        p.setPhysicsEngineParameter(enableFileCaching=0, physicsClientId=client)
        body = p.loadURDF(str(path), useFixedBase=True, physicsClientId=client)
        rows = [p.getJointInfo(body, i, physicsClientId=client)
                for i in range(p.getNumJoints(body, physicsClientId=client))]
        indices = {row[1].decode(): row[0] for row in rows}
        targets = [(j.name, indices[j.name]) for j in joints]
        targets.append(("flange", next(row[0] for row in rows
                                       if row[12].decode() == "flange")))
        worst = {n: {"position_mm": 0.0, "rotation_deg": 0.0,
                     "position_pose_deg": [], "rotation_pose_deg": []}
                 for n, _ in targets}
        for q in postures:
            for joint, radians in zip(joints, np.radians(q)):
                p.resetJointState(body, indices[joint.name], float(radians),
                                  physicsClientId=client)
            expected = model.fk(q)
            if not all(np.isfinite(frame).all() for frame in expected.values()):
                raise ValueError(f"{name}: non-finite FK state at {q.tolist()}")
            for joint, idx in targets:
                state = p.getLinkState(body, idx, computeForwardKinematics=True,
                                       physicsClientId=client)
                if not all(np.isfinite(value).all() for value in state):
                    raise ValueError(f"{name}/{joint}: non-finite engine state at {q.tolist()}")
                xyz = np.asarray(state[4]) * 1000
                rot = np.asarray(p.getMatrixFromQuaternion(state[5])).reshape(3, 3)
                if not (np.isfinite(xyz).all() and np.isfinite(rot).all()):
                    raise ValueError(f"{name}/{joint}: non-finite engine transform at {q.tolist()}")
                errors = {"position_mm": float(np.linalg.norm(xyz-expected[joint][:3, 3])),
                          "rotation_deg": rotation_error_deg(rot, expected[joint][:3, :3])}
                for metric, error in errors.items():


                    if not np.isfinite(error):
                        raise ValueError(f"{name}/{joint}: non-finite {metric} at {q.tolist()}")
                    if error > worst[joint][metric]:
                        worst[joint][metric] = error
                        worst[joint][metric.split('_')[0] + "_pose_deg"] = q.tolist()
        return {"model": name, "urdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "seed": seed, "random_postures": samples,
                "boundary_postures": len(boundaries), "links": worst,
                "position_tol_mm": position_tol_mm, "rotation_tol_deg": rotation_tol_deg,
                "passed": all(v["position_mm"] <= position_tol_mm and
                              v["rotation_deg"] <= rotation_tol_deg for v in worst.values())}
    finally:
        p.disconnect(physicsClientId=client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260908)
    args = parser.parse_args()
    try:
        engine = importlib.metadata.version("pybullet")
    except importlib.metadata.PackageNotFoundError:
        print("engine: NOT AVAILABLE — install PyBullet in an isolated test runtime")
        return 2
    reports = [audit_model(name, samples=args.samples, seed=args.seed) for name in MODELS]
    print(json.dumps({"python": platform.python_version(), "pybullet": engine,
                      "scope": "model consistency only; no physical calibration", "models": reports},
                     indent=2, allow_nan=False))
    return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
