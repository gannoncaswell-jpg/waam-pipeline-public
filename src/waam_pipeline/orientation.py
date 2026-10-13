from dataclasses import dataclass
from itertools import product

import numpy as np


@dataclass(frozen=True)
class RestFace:
    normal: np.ndarray
    area: float
    triangles: np.ndarray


def _plane_tolerance(triangles):


    return max(1e-7, float(np.linalg.norm(np.ptp(triangles, axis=(0, 1)))) * 2e-7)


def rest_faces(triangles, *, angle_tolerance_deg=1.5, min_area_fraction=0.001):


    tri = np.asarray(triangles, dtype=float)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3) or not len(tri) or not np.isfinite(tri).all():
        raise ValueError("Place on face requires a finite triangle mesh")
    if not 0 < angle_tolerance_deg <= 2 or not 0 < min_area_fraction <= 1:
        raise ValueError("Invalid rest-face tolerances")

    tri = tri - (tri.min(axis=(0, 1)) + tri.max(axis=(0, 1))) / 2
    tolerance = _plane_tolerance(tri)
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    twice_area = np.linalg.norm(cross, axis=1)
    valid = np.flatnonzero(twice_area > tolerance**2)
    if not len(valid):
        return []
    normals = cross[valid] / twice_area[valid, None]

    dominant = np.argmax(np.abs(normals), axis=1)
    normals *= np.where(normals[np.arange(len(normals)), dominant] < 0, -1., 1.)[:, None]
    normals[np.abs(normals) < 1e-14] = 0
    centers = tri[valid].mean(axis=1)

    order = np.lexsort((centers[:, 2], centers[:, 1], centers[:, 0],
                        normals[:, 2], normals[:, 1], normals[:, 0], -twice_area[valid]))
    width = 2 * np.sin(np.radians(angle_tolerance_deg) / 2)
    cosine = np.cos(np.radians(angle_tolerance_deg))
    neighbors = list(product((-1, 0, 1), repeat=3))
    buckets, groups = {}, []
    for local in order:
        index, normal = valid[local], normals[local]
        key = tuple(np.floor(normal / width).astype(int))
        match = None


        for search_key in (key, tuple(np.floor(-normal / width).astype(int))):
            for delta in neighbors:
                for g in buckets.get(tuple(k + d for k, d in zip(search_key, delta)), ()):
                    n, point, indices = groups[g]
                    if (abs(normal @ n) >= cosine and
                            np.max(np.abs((tri[index] - point) @ n)) <= tolerance):
                        match = g
                        break
                if match is not None:
                    break
            if match is not None:
                break
        if match is None:
            buckets.setdefault(key, []).append(len(groups))
            groups.append((normal, tri[index, 0], [index]))
        else:
            groups[match][2].append(index)

    minimum_area = twice_area[valid].sum() * min_area_fraction / 2
    vertices = tri.reshape(-1, 3)
    candidates = []
    for normal, point, indices in groups:
        area = float(twice_area[indices].sum() / 2)
        if area < minimum_area:
            continue
        distances = vertices @ normal - point @ normal
        if distances.max() <= tolerance:
            outward = normal
        elif distances.min() >= -tolerance:
            outward = -normal
        else:
            continue
        candidates.append((tuple(outward), float(point @ outward),
                           RestFace(outward.copy(), area, np.array(indices, dtype=int))))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in candidates]


def _axis_rotation(degrees, axis):
    angle = np.radians(degrees)
    c, s = np.cos(angle), np.sin(angle)
    if axis == "x":
        return np.array([[1., 0, 0], [0, c, -s], [0, s, c]])
    return np.array([[c, 0, s], [0, 1., 0], [-s, 0, c]])


def _align_down(normal):
    down = np.array([0., 0., -1.])
    cross = np.cross(normal, down)
    sine, cosine = np.linalg.norm(cross), float(normal @ down)
    if sine < 1e-14:
        if cosine > 0:
            return np.eye(3)
        basis = np.eye(3)[np.argmin(np.abs(normal))]
        axis = np.cross(normal, basis)
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    axis = cross / sine
    x, y, z = axis
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + sine * skew + (1 - cosine) * (skew @ skew)


def place_on_face(triangles, rot_x=0., rot_y=0., *, legacy_tilt=0.):


    if not np.isfinite([rot_x, rot_y, legacy_tilt]).all():
        raise ValueError("Part orientation must use finite angles")
    tri = np.asarray(triangles, dtype=float)
    faces = rest_faces(tri)
    if not faces:
        raise ValueError("No supporting flat face found. Export a part with a planar base or add one in CAD.")
    requested = (_axis_rotation(rot_y, "y") @ _axis_rotation(rot_x, "x") @
                 _axis_rotation(legacy_tilt, "y"))
    scores = [float(-(requested @ face.normal)[2]) for face in faces]
    index = min(range(len(faces)), key=lambda i: (-round(scores[i], 12), -faces[i].area, i))
    face = faces[index]
    rotation = _align_down(requested @ face.normal) @ requested
    rotation[np.abs(rotation) < 1e-14] = 0.
    rotation[np.abs(rotation - 1.) < 1e-14] = 1.
    rotation[np.abs(rotation + 1.) < 1e-14] = -1.
    seated = tri @ rotation.T
    seated[..., 2] -= seated[..., 2].min()

    tolerance = _plane_tolerance(tri)
    if seated[face.triangles, :, 2].max() > 2 * tolerance:
        raise ValueError("The selected face could not be seated; check STL planarity")
    seated[..., 2][seated[..., 2] <= 2 * tolerance] = 0.
    return seated, {
        "face": index, "normal": face.normal.tolist(), "area_mm2": face.area,
        "candidate_count": len(faces), "rotation": rotation.tolist(),
        "snap_degrees": float(np.degrees(np.arccos(np.clip(scores[index], -1, 1)))),
        "contact_tolerance_mm": 2 * tolerance,
    }
