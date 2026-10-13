from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import math

import numpy as np


@dataclass(frozen=True)
class Property:
    temperature_c: float
    conductivity_w_m_k: float
    specific_heat_j_kg_k: float


@dataclass(frozen=True)
class GoldakSettings:
    lateral_mm: float
    depth_mm: float
    front_mm: float
    rear_mm: float

    front_fraction: float
    density_kg_m3: float
    solidus_c: float
    liquidus_c: float
    latent_heat_j_kg: float
    properties: tuple[Property, ...]
    plate_length_mm: float
    plate_width_mm: float
    cell_mm: float
    max_step_s: float
    convection_w_m2_k: float
    emissivity: float
    bottom_contact_w_m2_k: float

    @classmethod
    def from_dict(cls, data):
        try:
            return cls(**{**data, "properties": tuple(Property(**r) for r in data["properties"])})
        except (KeyError, TypeError) as exc:
            raise ValueError("Goldak settings have missing or invalid fields") from exc

    def validate(self, ambient_c, width_mm, height_mm, substrate_mm):
        for key, value in asdict(self).items():
            if key == "properties":
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > 1e6):
                raise ValueError(f"Goldak {key} must be a finite number within 1e6")
            if key not in ("solidus_c", "liquidus_c", "convection_w_m2_k", "emissivity",
                           "bottom_contact_w_m2_k", "latent_heat_j_kg") and value <= 0:
                raise ValueError(f"Goldak {key} must be positive")
        if not 0 < self.front_fraction < 1 or not 0 <= self.emissivity <= 1:
            raise ValueError("Goldak front fraction must be in (0, 1), emissivity in [0, 1]")
        if min(self.convection_w_m2_k, self.bottom_contact_w_m2_k, self.latent_heat_j_kg) < 0:
            raise ValueError("Goldak losses and latent heat cannot be negative")
        if not ambient_c < self.solidus_c < self.liquidus_c <= 5000:
            raise ValueError("Goldak needs ambient < solidus < liquidus <= 5000 C")
        if not 0.25 <= self.cell_mm <= min(width_mm / 2, height_mm,
                                         self.lateral_mm / 2, self.depth_mm / 2,
                                         self.front_mm / 2, self.rear_mm / 2):
            raise ValueError("Goldak cell_mm must resolve the bead and each source semi-axis (at least 0.25 mm)")
        if not 0.0001 <= self.max_step_s <= 1 or substrate_mm <= 0:
            raise ValueError("Goldak needs a finite substrate and max_step_s in [0.0001, 1]")
        if not isinstance(self.properties, tuple) or not 2 <= len(self.properties) <= 64:
            raise ValueError("Goldak properties need 2-64 temperature rows")
        last = -math.inf
        for row in self.properties:
            if not isinstance(row, Property):
                raise ValueError("Goldak properties must be Property rows")
            if any(isinstance(v, bool) or not isinstance(v, (float, int))
                   or not math.isfinite(v) for v in asdict(row).values()):
                raise ValueError("Goldak properties must be finite numbers")
            if not (-273.15 < row.temperature_c <= 5000 and row.temperature_c > last
                    and 0.01 <= row.conductivity_w_m_k <= 1000
                    and 1 <= row.specific_heat_j_kg_k <= 10000):
                raise ValueError("Goldak property rows must have increasing temperatures and positive bounded k/cp")
            last = row.temperature_c
        if not self.properties[0].temperature_c <= ambient_c < self.liquidus_c < last:
            raise ValueError("Goldak property table must cover ambient through above liquidus")


class Enthalpy:


    def __init__(self, settings, ambient_c):
        self.settings = settings
        rows = settings.properties
        self.temperatures = np.array([r.temperature_c for r in rows])
        self.conductivities = np.array([r.conductivity_w_m_k for r in rows]) / 1000
        cp = np.array([r.specific_heat_j_kg_k for r in rows])
        self.knots = np.unique(np.r_[self.temperatures, settings.solidus_c, settings.liquidus_c])
        cv = np.interp(self.knots, self.temperatures, cp) * settings.density_kg_m3 / 1e9
        delta = np.diff(self.knots)
        self.slopes = np.diff(cv) / delta
        self.capacities = cv[:-1].copy()
        mushy = (self.knots[:-1] >= settings.solidus_c) & (self.knots[1:] <= settings.liquidus_c)
        self.capacities[mushy] += (settings.latent_heat_j_kg * settings.density_kg_m3 / 1e9
                                  / (settings.liquidus_c - settings.solidus_c))
        self.integrals = np.r_[0, np.cumsum(self.capacities * delta + self.slopes * delta**2 / 2)]
        self.minimum_capacity = float(cv.min())
        self.offset = 0.0
        self.offset = float(self.energy(ambient_c))

    def energy(self, temperature):
        t = np.asarray(temperature, dtype=float)
        if not np.isfinite(t).all() or np.any(t < self.knots[0]) or np.any(t > self.knots[-1]):
            raise ValueError("Goldak temperature outside supplied material properties; no extrapolation")
        i = np.clip(np.searchsorted(self.knots, t, side="right") - 1, 0, len(self.slopes) - 1)
        dt = t - self.knots[i]
        return self.integrals[i] + self.capacities[i] * dt + self.slopes[i] * dt**2 / 2 - self.offset

    def temperature(self, energy):
        u = np.asarray(energy, dtype=float) + self.offset
        if (not np.isfinite(u).all() or np.any(u < self.integrals[0] - 1e-10)
                or np.any(u > self.integrals[-1] + 1e-10)):
            raise ValueError("Goldak energy outside supplied material properties; refine inputs/model")

        u = np.clip(u, self.integrals[0], self.integrals[-1])
        i = np.clip(np.searchsorted(self.integrals, u, side="right") - 1, 0, len(self.slopes) - 1)
        du = u - self.integrals[i]
        root = np.sqrt(np.maximum(0, self.capacities[i]**2 + 2 * self.slopes[i] * du))
        return self.knots[i] + 2 * du / (self.capacities[i] + root)

    def conductivity(self, temperature):
        return np.interp(temperature, self.temperatures, self.conductivities)


def goldak_density(points, centre, direction, settings, power_w):


    d = np.asarray(direction, dtype=float)[:2]
    if not np.isfinite(d).all() or np.linalg.norm(d) < 1e-12:
        raise ValueError("Goldak source needs a nonzero planar travel direction")
    d = d / np.linalg.norm(d)
    offset = np.asarray(points) - centre
    along = offset[..., 0] * d[0] + offset[..., 1] * d[1]
    lateral = -offset[..., 0] * d[1] + offset[..., 1] * d[0]
    depth = -offset[..., 2]
    c = np.where(along >= 0, settings.front_mm, settings.rear_mm)
    f = 2 * np.where(along >= 0, settings.front_fraction, 1 - settings.front_fraction)
    norm = 6 * math.sqrt(3) * f * power_w / (math.pi**1.5 * settings.lateral_mm * settings.depth_mm * c)
    return np.where(depth >= 0, norm * np.exp(-3 * ((along / c)**2
                    + (lateral / settings.lateral_mm)**2 + (depth / settings.depth_mm)**2)), 0)


@dataclass
class Work:
    cell_steps: int = 0
    steps: int = 0

    def charge(self, cells):
        self.cell_steps += cells
        self.steps += 1
        if self.cell_steps > 150_000_000 or self.steps > 100_000:
            raise ValueError("Goldak preview exceeded its trial budget (150 million cell steps / 100000 steps); use a smaller study")


class GoldakHistory:


    VIZ_MAX_PAYLOAD_BYTES = 3_900_000

    def __init__(self, profile, passes, substrate_z, *, enable_viz: bool = False,
                 viz_max_payload_bytes: int | None = None):
        self.profile, self.settings = profile, profile.goldak
        g = self.settings
        self.material = Enthalpy(g, profile.ambient_c)
        points = np.array([point for samples, _ in passes for s in samples for point in s[:2]])
        centre = (points.min(axis=0) + points.max(axis=0)) / 2
        if (np.ptp(points[:, 0]) + profile.bead_width_mm > g.plate_length_mm
                or np.ptp(points[:, 1]) + profile.bead_width_mm > g.plate_width_mm):
            raise ValueError("Goldak wall footprint does not fit the supplied finite plate")


        nx = math.ceil(g.plate_length_mm / g.cell_mm)
        ny = math.ceil(g.plate_width_mm / g.cell_mm)
        nb = math.ceil(profile.substrate_mm / g.cell_mm)
        nl = max(1, math.ceil((points[:, 2].max() - substrate_z) / profile.layer_height_mm - 1e-7))
        per_layer = math.ceil(profile.layer_height_mm / g.cell_mm)
        nz = nb + nl * per_layer
        if nx * ny * nz > 25000:
            raise ValueError("Goldak preview is limited to 25000 cells; reduce the study domain")
        self.edges = (
            np.linspace(centre[0] - g.plate_length_mm / 2, centre[0] + g.plate_length_mm / 2, nx + 1),
            np.linspace(centre[1] - g.plate_width_mm / 2, centre[1] + g.plate_width_mm / 2, ny + 1),
            np.r_[np.linspace(substrate_z - profile.substrate_mm, substrate_z, nb + 1),
                  substrate_z + np.arange(1, nl * per_layer + 1) * profile.layer_height_mm / per_layer])
        self.shape, self.size = (nx, ny, nz), nx * ny * nz
        widths = [np.diff(e) for e in self.edges]
        centres = [(e[:-1] + e[1:]) / 2 for e in self.edges]
        self.points = np.stack(np.meshgrid(*centres, indexing="ij"), axis=-1).reshape(-1, 3)
        sizes = np.stack(np.meshgrid(*widths, indexing="ij"), axis=-1).reshape(-1, 3)
        self.volume = sizes.prod(axis=1)
        self.face_area = self.volume[:, None] / sizes
        ids = np.arange(self.size).reshape(self.shape)
        ei, ej, areas, distances = [], [], [], []
        thickness_i, thickness_j = [], []
        for axis in range(3):
            left, right = [slice(None)] * 3, [slice(None)] * 3
            left[axis], right[axis] = slice(None, -1), slice(1, None)
            a, b = ids[tuple(left)].ravel(), ids[tuple(right)].ravel()
            ei.extend(a); ej.extend(b)
            areas.extend(self.face_area[a, axis])
            distances.extend((sizes[a, axis] + sizes[b, axis]) / 2)
            thickness_i.extend(sizes[a, axis])
            thickness_j.extend(sizes[b, axis])
        self.ei, self.ej = np.array(ei), np.array(ej)
        self.areas, self.distances = np.array(areas), np.array(distances)
        self.thickness_i, self.thickness_j = np.array(thickness_i), np.array(thickness_j)
        self.contact_area = np.zeros(self.size)
        self.contact_area[ids[:, :, 0].ravel()] = self.face_area[ids[:, :, 0].ravel(), 2] / 1e6
        self.active = self.points[:, 2] < substrate_z
        self._viz_enabled = bool(enable_viz)
        self.u = np.zeros(self.size)
        self.time_s = self.input_j = self.loss_j = 0.0
        self.peak_c = profile.ambient_c
        self.capture_min, self.capture_max = math.inf, 0.0
        self.steps, self.work = 0, Work()
        if self._viz_enabled:
            self._init_viz(viz_max_payload_bytes, substrate_z)
        self._connect()

    def clone(self):
        obj = copy.copy(self)
        obj.active, obj.u = self.active.copy(), self.u.copy()
        if self._viz_enabled:

            if self._viz_birth_time is not None:
                obj._viz_birth_time = self._viz_birth_time.copy()
            obj._viz_snapshots = [(time, frame.copy()) for time, frame in self._viz_snapshots]

        return obj

    def _init_viz(self, budget, substrate_z):
        self._viz_max_payload_bytes = (self.VIZ_MAX_PAYLOAD_BYTES if budget is None
                                       else min(int(budget), self.VIZ_MAX_PAYLOAD_BYTES))
        self._viz_substrate_z = substrate_z
        self._viz_snapshots = []
        self._viz_birth_time = None
        self._viz_unavailable_reason = None


        metadata_bytes = 8 * self.size + sum(e.nbytes for e in self.edges) + 1024
        self._viz_max_frames = max(0, (self._viz_max_payload_bytes - metadata_bytes)
                                   // (4 * self.size + 8))
        if self._viz_max_frames == 0:
            self._viz_unavailable_reason = "viz payload budget is too small for grid metadata and one float32 frame"
            return
        self._viz_birth_time = np.full(self.size, np.nan, dtype=np.float32)
        self._viz_birth_time[self.active] = 0.0

    def _record_viz_snapshot(self, temperature):
        if self._viz_unavailable_reason is not None:
            return


        if len(self._viz_snapshots) == self._viz_max_frames:
            self._viz_snapshots = self._viz_snapshots[::2][:self._viz_max_frames - 1]
        self._viz_snapshots.append((float(self.time_s), temperature.astype(np.float32)))

    def viz_payload(self):


        if not self._viz_enabled:
            return None
        if self._viz_unavailable_reason is not None:
            return {"available": False, "reason": self._viz_unavailable_reason}
        return {"available": True, "reason": None,
                "shape": list(self.shape), "edges": [e.copy() for e in self.edges],
                "deposited_cell_ids": np.flatnonzero(self.active & (self.points[:, 2] >= self._viz_substrate_z)).astype(np.int32),
                "birth_time_s": self._viz_birth_time.copy(),
                "snapshots": [{"time_s": time, "temperature_c": frame.copy()} for time, frame in self._viz_snapshots]}

    def _connect(self):
        both = self.active[self.ei] & self.active[self.ej]
        self.i, self.j = self.ei[both], self.ej[both]
        self.interface_area = self.areas[both]
        self.interface_thickness_i = self.thickness_i[both]
        self.interface_thickness_j = self.thickness_j[both]
        self.geometry = self.areas[both] / self.distances[both]
        covered = (np.bincount(self.i, self.areas[both], minlength=self.size)
                   + np.bincount(self.j, self.areas[both], minlength=self.size))
        self.exposed = (2 * self.face_area.sum(axis=1) * self.active - covered) / 1e6
        if self.settings.bottom_contact_w_m2_k:
            self.exposed -= self.contact_area
        self.exposed = np.maximum(self.exposed, 0)

    def activate(self, a, b, time_s=None):


        d = b[:2] - a[:2]
        length = np.linalg.norm(d)
        direction = d / length
        offset = self.points[:, :2] - a[:2]
        along = offset @ direction
        lateral = offset @ np.array([-direction[1], direction[0]])
        mask = ((along >= -1e-7) & (along <= length + 1e-7)
                & (abs(lateral) <= self.profile.bead_width_mm / 2)
                & (self.points[:, 2] > a[2] - self.profile.layer_height_mm)
                & (self.points[:, 2] < a[2]))
        if self._viz_enabled and self._viz_birth_time is not None:
            self._viz_birth_time[mask & ~self.active] = self.time_s if time_s is None else float(time_s)
        self.active |= mask
        self._connect()

    def _rates(self):
        t = self.material.temperature(self.u)
        k = self.material.conductivity(t)
        conductance = self.interface_area / (self.interface_thickness_i / (2 * k[self.i])
                                            + self.interface_thickness_j / (2 * k[self.j]))
        flux = conductance * (t[self.j] - t[self.i])
        power = (np.bincount(self.i, flux, minlength=self.size)
                 - np.bincount(self.j, flux, minlength=self.size))
        outgoing = (np.bincount(self.i, conductance, minlength=self.size)
                    + np.bincount(self.j, conductance, minlength=self.size))
        g, ambient = self.settings, self.profile.ambient_c
        sigma = 5.670374419e-8
        losses = (self.exposed * (g.convection_w_m2_k * (t - ambient)
                   + g.emissivity * sigma * ((t + 273.15)**4 - (ambient + 273.15)**4))
                  + self.contact_area * g.bottom_contact_w_m2_k * (t - ambient))

        outgoing += (self.exposed * (g.convection_w_m2_k + 4 * g.emissivity * sigma * (t + 293.15)**3)
                     + self.contact_area * g.bottom_contact_w_m2_k)
        dt = 0.7 / max(float(np.max(outgoing / (self.volume * self.material.minimum_capacity))), 1e-12)
        return t, power, losses, min(dt, g.max_step_s)

    def _source(self, centre, direction):
        raw = goldak_density(self.points, centre, direction, self.settings, self.profile.net_power_w)
        powers = raw * self.volume * self.active
        captured = float(powers.sum()) / self.profile.net_power_w
        if not 0.05 <= captured <= 2:
            raise ValueError("Goldak source is poorly resolved or outside active material; refine geometry/source")
        return powers / captured, captured

    def advance(self, target_s, segment=None):
        if not math.isfinite(target_s) or not 0 <= target_s <= 86400:
            raise ValueError("Goldak target time must be within the 24-hour study domain")
        if target_s < self.time_s - 1e-7:
            raise ValueError("Goldak history cannot run backward")
        if segment is None and not self.has_heat():
            self.time_s = target_s
            return
        start = self.time_s
        while self.time_s < target_s - 1e-10:
            self.work.charge(self.size)
            _, conduction, losses, stable = self._rates()
            dt = min(stable, target_s - self.time_s)
            source, capture = np.zeros(self.size), None
            if segment is not None:
                a, b = segment

                displacement = b - a
                distance = np.linalg.norm(displacement)
                if distance == 0:
                    raise ValueError("Goldak source segment must have non-zero length")
                direction = displacement / distance
                axis_spacing = np.min([
                    np.diff(edge).min() / abs(d) for edge, d in zip(self.edges, abs(direction))
                    if d > 1e-12
                ])
                dt = min(dt, axis_spacing / 2 / distance * (target_s - start))
                for _ in range(20):
                    f = (self.time_s + dt / 2 - start) / (target_s - start)
                    source, capture = self._source(a + (b - a) * f, b - a)
                    limit = 20 * self.material.minimum_capacity / float(np.max(source / self.volume))
                    if dt <= limit:
                        break
                    dt = 0.9 * limit
                else:
                    raise ValueError("Goldak source timestep could not be resolved")
            if dt < 1e-8:
                raise ValueError("Goldak timestep is too small for the bounded preview")
            self.u += (conduction - losses + source) * dt / self.volume
            temperature = self.material.temperature(self.u)
            if temperature.min() < self.profile.ambient_c - 1e-6:
                raise ValueError("Goldak numerical undershoot; refine the time step")
            self.peak_c = max(self.peak_c, float(temperature.max()))
            self.input_j += float(source.sum()) * dt
            self.loss_j += float(losses.sum()) * dt
            if capture is not None:
                self.capture_min = min(self.capture_min, capture)
                self.capture_max = max(self.capture_max, capture)
            self.time_s += dt
            if self._viz_enabled:
                self._record_viz_snapshot(temperature)
            self.steps += 1
        self.time_s = target_s

    def temperature(self, point, time_s):
        self.advance(time_s)

        index = [np.searchsorted(e, x - (1e-8 if axis == 2 else 0), side="right") - 1
                 for axis, (e, x) in enumerate(zip(self.edges, point))]
        if any(i < 0 or i >= n for i, n in zip(index, self.shape)):
            return self.profile.ambient_c
        i = np.ravel_multi_index(tuple(index), self.shape)
        temperature = float(self.material.temperature(self.u[i])) if self.active[i] else self.profile.ambient_c

        return max(self.profile.ambient_c, temperature)

    def prepare(self, time_s):
        self.advance(time_s)

    def deposit(self, a, b, start_s, duration_s, height_mm):
        self.advance(start_s)
        self.activate(a, b, start_s)
        self.advance(start_s + duration_s, (a, b))

    def diagnostics(self):
        stored = float(np.sum(self.u * self.volume))
        return dict(model="goldak_finite_volume", cells=self.size, steps=self.steps,
                    trial_cell_steps=self.work.cell_steps, trial_steps=self.work.steps,
                    active_cells=int(self.active.sum()),
                    input_j=self.input_j, loss_j=self.loss_j, stored_j=stored,
                    balance_error_j=stored + self.loss_j - self.input_j,
                    peak_material_c=self.peak_c,
                    source_capture_min=None if math.isinf(self.capture_min) else self.capture_min,
                    source_capture_max=self.capture_max)

    def has_heat(self):
        return bool(np.any(self.u))
