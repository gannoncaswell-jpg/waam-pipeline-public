import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from waam_pipeline.clearance import _point_box_dist


def audit_primitives(samples=200, seed=20260908):
    import pybullet as p
    client = p.connect(p.DIRECT)
    reports = []
    try:
        sphere = p.createCollisionShape(p.GEOM_SPHERE, radius=.002, physicsClientId=client)
        probe = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=sphere, physicsClientId=client)
        for kind in ("box", "capsule"):
            for margin in (None, 0.):
                if kind == "box":
                    shape = p.createCollisionShape(p.GEOM_BOX, halfExtents=[.01,.02,.03], physicsClientId=client)
                else:
                    shape = p.createCollisionShape(p.GEOM_CAPSULE, radius=.003, height=.020, physicsClientId=client)
                body = p.createMultiBody(baseMass=0, baseCollisionShapeIndex=shape, physicsClientId=client)
                if margin is not None:
                    p.changeDynamics(body, -1, collisionMargin=margin, physicsClientId=client)
                errors = []
                worst = None
                for xyz in np.random.default_rng(seed).uniform(-40, 40, (samples, 3)):
                    p.resetBasePositionAndOrientation(probe, xyz/1000, [0,0,0,1], physicsClientId=client)
                    rows = p.getClosestPoints(probe, body, 1., physicsClientId=client)
                    if not rows:
                        raise RuntimeError("missing closest-point query result")
                    if kind == "box":
                        expected = _point_box_dist(xyz[None], np.array([-10,-20,-30]), np.array([10,20,30]))[0] - 2
                    else:
                        expected = np.linalg.norm(xyz - [0,0,np.clip(xyz[2], -10, 10)]) - 5
                    actual = rows[0][8]*1000
                    error = float(actual-expected)
                    if worst is None or abs(error) > abs(worst["error_mm"]):
                        worst = dict(xyz_mm=xyz.tolist(), expected_mm=float(expected), actual_mm=actual, error_mm=error)
                    errors.append(error)
                reports.append(dict(shape=kind, requested_margin_m=margin,
                                    engine_margin_m=p.getDynamicsInfo(body, -1, physicsClientId=client)[-1],
                                    samples=samples, seed=seed, min_error_mm=min(errors),
                                    max_error_mm=max(errors), worst=worst))
        return reports
    finally:
        p.disconnect(physicsClientId=client)


if __name__ == "__main__":
    print(json.dumps(audit_primitives(), indent=2, allow_nan=False))
