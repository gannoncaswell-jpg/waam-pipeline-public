


(function (global) {
  "use strict";
  const LINK_NAMES = ["base_link", "link_1_s", "link_2_l", "link_3_u", "link_4_r", "link_5_b", "link_6_t"];

  function upperBound(values, value) {
    let lo = 0, hi = values.length;
    while (lo < hi) { const m = (lo + hi) >>> 1; if (values[m] <= value) lo = m + 1; else hi = m; }
    return lo;
  }
  function creaseNormals(positions, creaseDegrees, groups) {


    const count = Math.floor(positions.length / 9), face = new Float64Array(count * 3);
    const regions = new Int32Array(count), corners = new Array(count * 3), welded = new Map();
    (groups || []).forEach((g, i) => regions.fill(i + 1, Math.max(0, g.start), Math.min(count, g.end)));
    const out = new Float32Array(count * 9);
    const cutoff = Math.cos((creaseDegrees === undefined ? 48 : creaseDegrees) * Math.PI / 180);
    for (let f = 0; f < count; f++) {
      const o = f * 9, a = positions.slice(o, o + 3), b = positions.slice(o + 3, o + 6), c = positions.slice(o + 6, o + 9);
      const u = b.map((x, k) => x - a[k]), v = c.map((x, k) => x - a[k]);
      const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
      const length = Math.hypot(n[0], n[1], n[2]);
      if (!Number.isFinite(length) || length < 1e-12) continue;
      for (let k = 0; k < 3; k++) face[f * 3 + k] = n[k] / length;
      const points = [a, b, c];
      for (let j = 0; j < 3; j++) {
        const p = points[j], q = points[(j + 1) % 3], r = points[(j + 2) % 3];
        let dot = 0, q2 = 0, r2 = 0;
        for (let k = 0; k < 3; k++) { const x = q[k] - p[k], y = r[k] - p[k]; dot += x * y; q2 += x * x; r2 += y * y; }
        const weight = Math.acos(Math.max(-1, Math.min(1, dot / Math.sqrt(q2 * r2))));
        const key = regions[f] + ":" + p.map(x => Math.round(x * 10000)).join(",");
        let bucket = welded.get(key);
        if (!bucket) { bucket = []; welded.set(key, bucket); }
        bucket.push(f, weight); corners[f * 3 + j] = bucket;
      }
    }
    for (let f = 0; f < count; f++) for (let j = 0; j < 3; j++) {
      const bucket = corners[f * 3 + j];
      if (!bucket) continue;
      let x = 0, y = 0, z = 0;
      for (let b = 0; b < bucket.length; b += 2) {
        const q = bucket[b] * 3, w = bucket[b + 1];
        const dot = face[f * 3] * face[q] + face[f * 3 + 1] * face[q + 1] + face[f * 3 + 2] * face[q + 2];
        if (dot + 1e-10 < cutoff) continue;
        x += face[q] * w; y += face[q + 1] * w; z += face[q + 2] * w;
      }
      const length = Math.hypot(x, y, z) || 1, o = f * 9 + j * 3;
      out[o] = x / length; out[o + 1] = y / length; out[o + 2] = z / length;
    }
    return out;
  }

  function cameraFrame(frame) {

    const sy = Math.sin(frame.yaw), cy = Math.cos(frame.yaw), sp = Math.sin(frame.pitch), cp = Math.cos(frame.pitch);
    const right = [cy, -sy, 0], up = [sy * sp, cy * sp, cp], depth = [sy * cp, cy * cp, -sp];
    const scale = frame.zoom * Math.min(frame.width, frame.height) * 0.62 / Math.max(frame.span, 1);
    const target = frame.center.map((v, i) => v - right[i] * (frame.panX || 0) / scale + up[i] * (frame.panY || 0) / scale);
    const eyeDist = Math.max(frame.span, 1) / (2 * Math.tan(25 * Math.PI / 180) * frame.zoom * .62);
    const distance = frame.perspective ? eyeDist - (frame.dolly || 0) : Math.max(frame.span, 1) * 3;
    const eye = target.map((v, i) => v - distance * depth[i]);

    const near = Math.max(frame.span * 1e-6, 1e-5, Math.min(frame.span * .001, Math.abs(distance) * .001));
    const bounds = frame.cellCenter || frame.center;
    const far = Math.max(frame.span * 15, Math.hypot(...eye.map((v, i) => v - bounds[i])) + frame.span * 6);
    return { right, up, depth, scale, target, eye, eyeDist, distance, near, far };
  }

  function linkMatrix(frame, offset, base) {

    const r = frame.R, p = frame.p;
    const t = [0, 1, 2].map(k => p[k] + base[k] + r[k * 3] * offset[0] + r[k * 3 + 1] * offset[1] + r[k * 3 + 2] * offset[2]);
    return [r[0], r[1], r[2], t[0], r[3], r[4], r[5], t[1], r[6], r[7], r[8], t[2], 0, 0, 0, 1];
  }

  function create(options) {
    const T = global.WaamThree || global.THREE;
    if (!T || !options.canvas) return null;
    let renderer;
    try {
      renderer = new T.WebGLRenderer({ canvas: options.canvas, antialias: true, alpha: false, powerPreference: "high-performance" });
      return new StudioRenderer(T, renderer, options.state);
    } catch (error) {
      if (renderer) renderer.dispose();
      options.canvas.dataset.renderer = "unavailable";
      console.warn("WAAM Studio: WebGL unavailable; using the diagram renderer.", error);
      return null;
    }
  }

  class StudioRenderer {
    constructor(T, renderer, state) {
      this.T = T; this.renderer = renderer; this.state = state; this.active = true;
      this.scene = new T.Scene(); this.scene.background = new T.Color(0xc3cbd4);
      this.orthoCamera = new T.OrthographicCamera(-1, 1, 1, -1, 0.1, 100000);
      this.perspectiveCamera = new T.PerspectiveCamera(50, 1, 0.1, 100000);
      this.camera = this.orthoCamera;
      this.materials = new Map(); this.parts = new T.Group(); this.scene.add(this.parts);
      this.geometryCache = new Map(); this.cachedGeometries = new Set(); this.usedGeometries = new Set();
      this.links = []; this.sceneMeshes = []; this.size = ""; this.poseKey = "";
      renderer.outputColorSpace = T.SRGBColorSpace;
      renderer.toneMapping = T.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
      renderer.shadowMap.enabled = true; renderer.shadowMap.type = T.PCFShadowMap;
      renderer.shadowMap.autoUpdate = false;
      renderer.domElement.dataset.renderer = "webgl";


      renderer.localClippingEnabled = true;
      this.tipClip = new T.Plane(new T.Vector3(0, 0, 1), 1e6);
      this.onContextLost = event => {
        event.preventDefault(); this.active = false; renderer.domElement.dataset.renderer = "lost";
      };
      this.onContextRestored = () => {
        this.active = true; this.poseKey = ""; this.size = ""; renderer.shadowMap.needsUpdate = true;
        renderer.domElement.dataset.renderer = "webgl";
      };
      renderer.domElement.addEventListener("webglcontextlost", this.onContextLost);
      renderer.domElement.addEventListener("webglcontextrestored", this.onContextRestored);
      this.makeEnvironment(); this.makeLights(); this.rebuild();
    }

    makeEnvironment() {
      const T = this.T, room = new T.Scene();
      room.background = new T.Color(0x77818e);
      const shell = new T.Mesh(new T.BoxGeometry(12, 10, 12), new T.MeshBasicMaterial({ color: 0x7a8595, side: T.BackSide }));
      room.add(shell);


      const panels = [[[-4.8, 2, 0], [0, Math.PI / 2, 0], [6, 5], 5.0, 0xffffff],
                      [[4.8, 1, -1], [0, -Math.PI / 2, 0], [5, 5], 2.0, 0xc8ddff],
                      [[0, 4.8, 0], [Math.PI / 2, 0, 0], [8, 7], 3.0, 0xf1f5ff],
                      [[0, 1, -5.8], [0, 0, 0], [3, 6], 1.3, 0xffeddb]];
      for (const [p, r, size, intensity, color] of panels) {
        const material = new T.MeshBasicMaterial({ color: new T.Color(color).multiplyScalar(intensity), side: T.DoubleSide });
        const panel = new T.Mesh(new T.PlaneGeometry(size[0], size[1]), material);
        panel.position.set(...p); panel.rotation.set(...r); room.add(panel);
      }
      const pmrem = new T.PMREMGenerator(this.renderer);


      this.environment = pmrem.fromScene(room, 0.035, 0.1, 100);
      this.scene.environment = this.environment.texture;
      room.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
      pmrem.dispose();
    }

    makeLights() {
      const T = this.T;
      const sky = new T.HemisphereLight(0xe6efff, 0x555d6a, 1.1); sky.position.set(0, 0, 1); this.scene.add(sky);
      this.key = new T.DirectionalLight(0xfff7e9, 3.1);
      this.key.castShadow = true; this.key.shadow.mapSize.set(2048, 2048);
      this.key.shadow.radius = 3.5; this.key.shadow.bias = -0.00012; this.key.shadow.normalBias = 0.75;
      this.scene.add(this.key, this.key.target);
      this.fill = new T.DirectionalLight(0xc6dcff, 0.65); this.scene.add(this.fill, this.fill.target);
      this.rim = new T.DirectionalLight(0xffffff, 1.1); this.scene.add(this.rim, this.rim.target);
    }

    material(kind, appearance) {
      const key = appearance ? kind + ":" + JSON.stringify(appearance) : kind;
      if (this.materials.has(key)) return this.materials.get(key);
      const T = this.T;

      const specs = {
        robot: [0x1261ae, 0.34, 0.37], hc_robot: [0xe5e2d8, 0.18, 0.4], mh185_headstock: [0x426a8e, 0.43, 0.42],
        charles_platter: [0xa6aeb4, 0.78, 0.38], bead_test_coupon: [0x7796ac, 0.65, 0.4], waam_fixture: [0x858e98, 0.7, 0.4],
        robot_pedestal: [0x303b48, 0.58, 0.47], cell_base: [0x343f4c, 0.52, 0.48],
        reamer_station: [0x505d6b, 0.55, 0.44], yrc1000_cabinet: [0x818c99, 0.32, 0.5],
        lincoln_r450: [0xa51b26, 0.3, 0.4], equipment_rack: [0x424d5b, 0.62, 0.44],
        power_disconnect: [0x8b969f, 0.38, 0.5], fume_hood: [0x485464, 0.4, 0.55],
        cell_frame_v2: [0x253240, 0.6, 0.38], cell_walls: [0x244d83, 0.12, 0.7, 0.22],
        hc_table: [0x7c8792, 0.62, 0.43], hc_back_panel: [0x24282b, 0.3, 0.49],
        cell_panels: [0x24282b, 0.3, 0.49],
        hc_cleaner: [0x8a9298, 0.58, 0.4], hc_fume_extraction: [0x657581, 0.45, 0.52],
        lc_cleaner: [0x8a9298, 0.58, 0.4], lc_cleaner_post: [0x363c40, 0.5, 0.46],
        lc_cleaner_mount: [0x454d53, 0.6, 0.43], lc_cleaner_foot: [0x454d53, 0.6, 0.43],
        lc_cleaner_base: [0x283037, 0.4, 0.52],
        feeder: [0xb5232a, 0.3, 0.32], bracket: [0x909ba4, 0.78, 0.33],
        spool: [0x9e734a, 0.78, 0.4], spool_housing: [0x242d39, 0.24, 0.48],
        spool_support: [0x4a5663, 0.68, 0.37], spool_hub: [0xadb5bd, 0.88, 0.26],
        torch: [0x73808c, 0.85, 0.27], tcp: [0xd39c47, 0.72, 0.3],
        conduit: [0x171f2a, 0.13, 0.55], part: [0xcba368, 0.6, 0.42],
        bead: [0xd9ad60, 0.64, 0.34], ground: [0xaeb7c1, 0.08, 0.88],
        default: [0x748392, 0.42, 0.45]
      };
      const s = specs[kind] || specs.default;


      const a = appearance || {};
      const material = new T.MeshStandardMaterial({ color: a.color === undefined ? s[0] : a.color,
        metalness: a.metalness === undefined ? s[1] : a.metalness,
        roughness: a.roughness === undefined ? s[2] : a.roughness,
        side: T.DoubleSide, envMapIntensity: 0.55, transparent: s[3] !== undefined,
        opacity: s[3] === undefined ? 1 : s[3], depthWrite: s[3] === undefined });
      this.materials.set(key, material); return material;
    }

    geometry(tris, crease, groups) {
      const signature = JSON.stringify([crease, groups || []]);
      const cached = this.geometryCache.get(tris);
      if (cached && cached.signature === signature) {
        this.usedGeometries.add(cached.geometry); return cached.geometry;
      }
      if (cached) { cached.geometry.dispose(); this.cachedGeometries.delete(cached.geometry); }
      const T = this.T, geometry = new T.BufferGeometry();
      const positions = new Float32Array(tris);
      geometry.setAttribute("position", new T.BufferAttribute(positions, 3));
      geometry.setAttribute("normal", new T.BufferAttribute(creaseNormals(positions, crease, groups), 3));
      geometry.computeBoundingBox(); geometry.computeBoundingSphere();
      this.geometryCache.set(tris, {signature, geometry});
      this.cachedGeometries.add(geometry); this.usedGeometries.add(geometry);
      return geometry;
    }

    mesh(tris, kind, groups) {
      const T = this.T, geometry = this.geometry(tris, kind === "part" ? 35 : 48, groups);
      geometry.clearGroups();
      const materials = [this.material(kind)];
      let start = 0;
      for (const group of (groups || []).slice().sort((a, b) => a.start - b.start)) {
        if (group.start > start) geometry.addGroup(start * 3, (group.start - start) * 3, 0);
        materials.push(this.material(group.kind, group.appearance));
        geometry.addGroup(group.start * 3, (group.end - group.start) * 3, materials.length - 1);
        start = group.end;
      }
      if (start < tris.length / 9) geometry.addGroup(start * 3, tris.length / 3 - start * 3, 0);
      const mesh = new T.Mesh(geometry, materials.length > 1 ? materials : materials[0]);
      mesh.castShadow = kind !== "cell_walls"; mesh.receiveShadow = true;
      mesh.userData.kind = kind; this.parts.add(mesh); return mesh;
    }

    clearParts() {
      this.parts.traverse(o => {
        if (o.geometry && !this.cachedGeometries.has(o.geometry)) o.geometry.dispose();
        if (o.material && o.userData.ownedMaterial) o.material.dispose();
      });
      this.parts.clear();
    }

    rebuild() {
      const T = this.T, state = this.state;
      this.usedGeometries.clear();
      this.clearParts(); this.links = []; this.sceneMeshes = [];
      this.refs = [state.scene, state.robot, state.part, state.toolpath, state.violations, state.torch]; this.poseKey = "";
      this.collisions = global.waamCollisionIndex(state);
      const bounds = new T.Box3();
      for (const [name, tris] of Object.entries((state.scene || {}).meshes || {})) {
        if (!tris.length) continue;
        const mesh = this.mesh(tris, name); mesh.name = name; this.sceneMeshes.push(mesh);
        bounds.union(mesh.geometry.boundingBox);
      }
      this.part = state.part && state.part.tris.length ? this.mesh(state.part.tris, "part") : null;
      if (this.part) bounds.union(this.part.geometry.boundingBox);
      const robot = state.robot;
      if (robot && robot.meshes) for (const name of LINK_NAMES) {
        const source = robot.meshes[name];
        if (!source || !source.tris.length) continue;
        const kind = state.meta?.cell_profile?.id === "hc10" ? "hc_robot" : "robot";
        const mesh = this.mesh(source.tris, kind, source.groups);
        mesh.matrixAutoUpdate = false;
        this.links.push({ mesh, source, index: LINK_NAMES.indexOf(name) });
      }

      this.torchMesh = null; this.tcpMarker = null;
      const torchMesh = robot && (robot.torch_mesh_flange || robot.torch_mesh);
      if (torchMesh && torchMesh.length) {
        this.torchMesh = this.mesh(torchMesh.flat(2), "torch");
        this.torchMesh.matrixAutoUpdate = !robot.torch_mesh_flange;
        this.torchMesh.castShadow = true; this.parts.add(this.torchMesh);
      }
      if (robot && (robot.tcp_flange || robot.tcp_marker)) {


        this.tcpMarker = new T.Mesh(new T.SphereGeometry(4, 16, 10),
          new T.MeshBasicMaterial({ color: 0xd39c47, depthTest: false, transparent: true, opacity: 0.95 }));
        this.tcpMarker.userData.ownedMaterial = true; this.tcpMarker.renderOrder = 10;
        this.tcpMarker.position.set(...(robot.tcp_marker || [0, 0, 0])); this.parts.add(this.tcpMarker);
      }
      const moves = state.toolpath.moves;
      for (const p of moves) bounds.expandByPoint(new T.Vector3(p.x, p.y, p.z));


      for (const skeleton of (robot && robot.skeleton) || []) {
        if (skeleton) for (const p of skeleton) bounds.expandByPoint(new T.Vector3(...p));
      }
      if (bounds.isEmpty()) bounds.set(new T.Vector3(-500, -500, -500), new T.Vector3(500, 500, 500));
      const center = bounds.getCenter(new T.Vector3()), size = bounds.getSize(new T.Vector3());
      const extent = Math.max(size.x, size.y, size.z, 500);
      const floor = ((state.scene || {}).boxes || []).find(b => b.name === "floor");


      const floorZ = floor ? floor.max[2] : bounds.min.z - 1;
      this.ground = new T.Mesh(new T.PlaneGeometry(extent * 8, extent * 8), this.material("ground"));
      this.ground.position.set(center.x, center.y, floorZ - 0.2); this.ground.receiveShadow = true; this.parts.add(this.ground);
      this.scene.fog = new T.Fog(0xc3cbd4, extent * 4, extent * 12);
      this.key.position.set(center.x - extent, center.y - extent * 0.8, center.z + extent * 1.5);
      this.key.target.position.copy(center);
      this.fill.position.set(center.x + extent, center.y, center.z + extent * 0.7); this.fill.target.position.copy(center);
      this.rim.position.set(center.x, center.y + extent, center.z + extent); this.rim.target.position.copy(center);
      const shadow = this.key.shadow.camera;
      shadow.left = shadow.bottom = -extent * 0.85; shadow.right = shadow.top = extent * 0.85;
      shadow.near = 1; shadow.far = extent * 5; shadow.updateProjectionMatrix();
      this.renderer.shadowMap.needsUpdate = true;
      this.makePaths(); this.makeTooling(); this.makeDiagnostics();


      this.placementGroup = new T.Group(); this.parts.add(this.placementGroup);
      for (const item of [this.part,this.deposits,this.partial,this.ghost,this.travel,this.partialTravel]) {
        if (item) this.placementGroup.add(item);
      }
      this.conduits = new T.Group(); this.parts.add(this.conduits);
      this.spine = this.lines([], 0x366d9d, 0.8); this.parts.add(this.spine);
      this.signature = "";
      this.pruneGeometry();
    }

    pruneGeometry() {


      for (const [tris, entry] of this.geometryCache) if (!this.usedGeometries.has(entry.geometry)) {
        entry.geometry.dispose(); this.cachedGeometries.delete(entry.geometry); this.geometryCache.delete(tris);
      }
    }

    lines(positions, color, opacity) {
      const T = this.T, geometry = new T.BufferGeometry();
      geometry.setAttribute("position", new T.Float32BufferAttribute(positions, 3));
      const material = new T.LineBasicMaterial({ color, transparent: opacity < 1, opacity, depthWrite: false });
      const lines = new T.LineSegments(geometry, material); lines.userData.ownedMaterial = true; return lines;
    }

    segmentMatrix(a, b, radius, rise = 0, height = radius) {
      const T = this.T, p = new T.Vector3(a.x, a.y, a.z + rise), q = new T.Vector3(b.x, b.y, b.z + rise);
      const direction = q.clone().sub(p), length = direction.length();
      const rotation = new T.Quaternion();
      if (length > 1e-9) rotation.setFromUnitVectors(new T.Vector3(0, 1, 0), direction.multiplyScalar(1 / length));
      return new T.Matrix4().compose(p.add(q).multiplyScalar(0.5), rotation, new T.Vector3(radius, Math.max(length, 1e-9), height));
    }

    makePaths() {
      const T = this.T, moves = this.state.toolpath.moves;
      this.depositEnds = []; this.travelEnds = []; const ghost = [], travel = [];
      for (let i = 1; i < moves.length; i++) {
        const a = moves[i - 1], b = moves[i], array = b.d ? ghost : travel;
        array.push(a.x, a.y, a.z, b.x, b.y, b.z);
        (b.d ? this.depositEnds : this.travelEnds).push(i);
      }
      const params = this.state.meta.params || {};


      this.beadRadius = Math.max(0.6, Math.min(4.5, (Number(params.bead) || 8.6) * 0.32));
      this.beadHalfHeight = Math.max(0.05, Math.min(15, Number(params.lh) || 2.9)) / 2;
      const cylinder = new T.CylinderGeometry(1, 1, 1, 10);
      this.deposits = new T.InstancedMesh(cylinder, this.material("bead", { color: 0xffffff }), Math.max(this.depositEnds.length, 1));
      for (let j = 0; j < this.depositEnds.length; j++) {
        const i = this.depositEnds[j]; this.deposits.setMatrixAt(j, this.segmentMatrix(moves[i - 1], moves[i], this.beadRadius, this.beadHalfHeight, this.beadHalfHeight));
        const event = this.collisions.segments.get(i);
        this.deposits.setColorAt(j, new T.Color(event ? event.color : 0xd9ad60));
      }
      this.deposits.instanceMatrix.needsUpdate = true; this.deposits.frustumCulled = false;
      this.deposits.castShadow = true; this.deposits.receiveShadow = true; this.parts.add(this.deposits);
      if (this.deposits.instanceColor) this.deposits.instanceColor.needsUpdate = true;
      this.acceptedBeadColors = this.deposits.instanceColor?.array.slice();
      this.previewBeadColors = this.acceptedBeadColors?.slice();
      const previewColor = new T.Color(0x3b82a8);
      if (this.previewBeadColors) for (let i=0;i<this.previewBeadColors.length;i+=3)
        previewColor.toArray(this.previewBeadColors,i);
      this.showingPlacementPreview = false;
      this.partial = new T.Mesh(cylinder.clone(), this.material("bead").clone());
      this.partial.userData.ownedMaterial = true;
      this.partial.matrixAutoUpdate = false; this.partial.castShadow = true; this.parts.add(this.partial);
      this.ghost = this.lines(ghost, 0x406f9b, 0.25); this.parts.add(this.ghost);
      this.travel = this.lines(travel, 0x6f8397, 0.6); this.parts.add(this.travel);
      this.partialTravel = this.lines([0, 0, 0, 0, 0, 0], 0x6f8397, 0.6); this.parts.add(this.partialTravel);
      this.tip = new T.Mesh(new T.SphereGeometry(this.beadRadius * 1.12, 16, 10), this.material("tcp")); this.parts.add(this.tip);
    }

    makeTooling() {
      const T = this.T, spec = this.state.torch || {};
      this.torch = new T.Group(); this.parts.add(this.torch);
      this.envelope = new T.Group(); this.torch.add(this.envelope);
      this.toolMaterials = [];
      const material = opacity => {
        const m = new T.MeshBasicMaterial({ color: 0x248fbb, transparent: true,
          opacity, depthWrite: false, side: T.DoubleSide, clippingPlanes: [this.tipClip] });
        this.toolMaterials.push(m); return m;
      };
      const ringMaterial = material(0.09), sphereMaterial = material(0.025), capsuleMaterial = material(0.28);

      [ringMaterial, sphereMaterial, capsuleMaterial].forEach(m => {
        const owner = new T.Group(); owner.material = m; owner.userData.ownedMaterial = true; this.torch.add(owner);
      });
      for (const [axial, radius, lateral] of spec.envelope || []) {


        const limit = lateral < radius ? Math.acos(-lateral / radius) : Math.PI, profile = [];
        for (let j = 0; j <= 24; j++) {
          const angle = -limit + 2 * limit * j / 24;
          profile.push(new T.Vector2(Math.max(0, lateral + radius * Math.cos(angle)), radius * Math.sin(angle)));
        }
        const mesh = new T.Mesh(new T.LatheGeometry(profile, 32), ringMaterial);
        mesh.rotation.x = Math.PI / 2; mesh.position.z = axial; this.envelope.add(mesh);
      }
      const spheres = spec.spheres || [];
      this.sphereVolume = new T.InstancedMesh(new T.SphereGeometry(1, 20, 12), sphereMaterial, Math.max(1, spheres.length));
      this.sphereVolume.count = spheres.length; this.sphereVolume.frustumCulled = false;
      spheres.forEach(([z, r], i) => this.sphereVolume.setMatrixAt(i,
        new T.Matrix4().compose(new T.Vector3(0, 0, z), new T.Quaternion(), new T.Vector3(r, r, r))));
      this.sphereVolume.instanceMatrix.needsUpdate = true; this.envelope.add(this.sphereVolume);
      this.envelope.quaternion.setFromUnitVectors(new T.Vector3(0, 0, 1), new T.Vector3(...(spec.axis || [0, 0, 1])).normalize());
      this.capsuleVolume = new T.Group(); this.torch.add(this.capsuleVolume);
      const tcp = spec.tcp_flange || [0, 0, 0];
      for (const c of spec.capsules || []) {
        const a = new T.Vector3(...c.p0).sub(new T.Vector3(...tcp)), b = new T.Vector3(...c.p1).sub(new T.Vector3(...tcp));
        const length = a.distanceTo(b), mesh = new T.Mesh(new T.CapsuleGeometry(c.r, length, 8, 20), capsuleMaterial);
        mesh.position.copy(a.clone().add(b).multiplyScalar(0.5));
        if (length) mesh.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), b.sub(a).normalize());
        this.capsuleVolume.add(mesh);
      }

      this.referenceToolRotation = new T.Quaternion().setFromUnitVectors(
        new T.Vector3(Math.SQRT1_2, 0, -Math.SQRT1_2), new T.Vector3(...(spec.axis || [0, 0, 1])).normalize());
    }

    makeDiagnostics() {
      const T = this.T, state = this.state, positions = [];
      this.diagnostics = new T.Group(); this.parts.add(this.diagnostics);
      for (const b of (state.scene || {}).boxes || []) {
        const corners = [[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]].map(k => k.map((v, i) => v ? b.max[i] : b.min[i]));
        for (const [a, c] of [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]]) positions.push(...corners[a], ...corners[c]);
      }
      this.diagnostics.add(this.lines(positions, 0x31738f, 0.4));
      for (const c of (state.scene || {}).capsules || []) {
        const a = new T.Vector3(...c.p0), b = new T.Vector3(...c.p1), length = a.distanceTo(b);
        const shape = new T.CapsuleGeometry(c.r, length, 6, 12);
        const material = new T.MeshBasicMaterial({ color: 0x31738f, wireframe: true, transparent: true, opacity: 0.22 });
        const mesh = new T.Mesh(shape, material); mesh.userData.ownedMaterial = true;
        mesh.position.copy(a.add(b).multiplyScalar(0.5));
        if (length > 0) mesh.quaternion.setFromUnitVectors(new T.Vector3(0, 1, 0), new T.Vector3(...c.p1).sub(new T.Vector3(...c.p0)).normalize());
        this.diagnostics.add(mesh);
      }
      const events = this.collisions.events.filter(e => e.point);
      this.markers = new T.InstancedMesh(new T.OctahedronGeometry(4),
        new T.MeshBasicMaterial({ color: 0xffffff, depthTest: false }), Math.max(1, events.length));
      this.markers.userData.ownedMaterial = true; this.markers.count = events.length;
      this.markers.frustumCulled = false; this.markers.renderOrder = 10;
      events.forEach((e, i) => {
        this.markers.setMatrixAt(i, new T.Matrix4().makeTranslation(e.v.x, e.v.y, e.v.z));
        this.markers.setColorAt(i, new T.Color(e.color));
      });
      this.markers.instanceMatrix.needsUpdate = true;
      if (this.markers.instanceColor) this.markers.instanceColor.needsUpdate = true;
      this.parts.add(this.markers);
    }

    updateConduits(frames, visible) {
      const T = this.T, robot = this.state.robot;
      this.conduits.visible = visible;
      if (!visible || !robot || !robot.conduits) return;
      this.conduits.traverse(o => { if (o.geometry) o.geometry.dispose(); }); this.conduits.clear();
      for (const spec of robot.conduits) {
        const points = spec.controls.map(control => {
          const f = frames[control.link], r = f.R, p = control.point;
          return new T.Vector3(...[0, 1, 2].map(k => f.p[k] + robot.base_offset[k] + r[k * 3] * p[0] + r[k * 3 + 1] * p[1] + r[k * 3 + 2] * p[2]));
        });
        const curve = new T.CubicBezierCurve3(...points);
        const mesh = new T.Mesh(new T.TubeGeometry(curve, spec.samples || 32, spec.radius, 12, false), this.material("conduit"));
        mesh.castShadow = true; mesh.receiveShadow = true; this.conduits.add(mesh);
      }
    }

    setState(state) { this.state = state; this.rebuild(); }

    render(frame) {
      if (!this.active) return false;
      const T = this.T, state = this.state, flags = frame.flags || {};
      if ([state.scene, state.robot, state.part, state.toolpath, state.violations, state.torch].some((v, i) => v !== this.refs[i])) this.rebuild();
      const placement = frame.placementPreview;
      if (!!placement !== this.showingPlacementPreview) {
        if (this.deposits.instanceColor) {
          this.deposits.instanceColor.array.set(placement ? this.previewBeadColors : this.acceptedBeadColors);
          this.deposits.instanceColor.needsUpdate = true;
        }
        this.showingPlacementPreview = !!placement;
      }
      const angle = placement ? placement.angle*Math.PI/180 : 0;
      const pivot = placement ? placement.pivot : [0,0], c=Math.cos(angle), s=Math.sin(angle);
      if(placement?.plane_basis){
        const [u,v,n]=placement.plane_basis;
        this.placementGroup.quaternion.setFromAxisAngle(new T.Vector3(...n),angle);
        const rotated=new T.Vector3(...pivot).applyQuaternion(this.placementGroup.quaternion);
        this.placementGroup.position.set(...pivot.map((p,i)=>p-rotated.getComponent(i)+u[i]*placement.dx+v[i]*placement.dy));
      }else{
        this.placementGroup.rotation.set(0,0,angle);
        this.placementGroup.position.set(
          (placement?placement.dx:0)+pivot[0]-c*pivot[0]+s*pivot[1],
          (placement?placement.dy:0)+pivot[1]-s*pivot[0]-c*pivot[1],0);
      }
      this.markers.visible=!placement;
      const width = Math.max(1, Math.round(frame.width)), height = Math.max(1, Math.round(frame.height));
      if (this.size !== width + ":" + height) {
        this.renderer.setSize(width, height, false); this.size = width + ":" + height;
      }
      const view = cameraFrame(frame);
      this.camera = frame.perspective ? this.perspectiveCamera : this.orthoCamera;
      this.camera.position.set(...view.eye);
      this.camera.up.set(...view.up);

      this.camera.lookAt(new T.Vector3(...view.eye).add(new T.Vector3(...view.depth)));
      if (frame.perspective) {
        this.camera.aspect = width / height;
        this.camera.zoom = Math.min(width, height) / height;
      }
      else {
        this.camera.left = -width / (2 * view.scale); this.camera.right = -this.camera.left;
        this.camera.top = height / (2 * view.scale); this.camera.bottom = -this.camera.top;
      }
      this.camera.near = view.near; this.camera.far = view.far; this.camera.updateProjectionMatrix();
      for (const mesh of this.sceneMeshes) mesh.visible = flags.scene !== false &&
        (!["cell_walls", "cell_panels"].includes(mesh.name) || flags.walls) &&
        (mesh.name !== "cell_frame_v2" || flags.frame) &&
        (!["fume_hood", "hc_fume_extraction"].includes(mesh.name) || flags.fumeHood !== false);
      if (this.part) this.part.visible = flags.part !== false;
      const frames = frame.frames, robotVisible = flags.robot !== false && !!frames;
      for (const link of this.links) {
        link.mesh.visible = robotVisible;
        if (robotVisible) {
          link.mesh.matrix.set(...linkMatrix(frames[link.index], link.source.offset, state.robot.base_offset));
          link.mesh.matrixWorldNeedsUpdate = true;
        }
      }
      if (this.torchMesh) this.torchMesh.visible = robotVisible;
      if (this.tcpMarker) this.tcpMarker.visible = robotVisible;
      if (robotVisible && (state.robot.torch_mesh_flange || state.robot.tcp_flange)) {
        const toolMatrix = new T.Matrix4().set(...linkMatrix(frames[6], [0, 0, 0], state.robot.base_offset));
        if (this.torchMesh && state.robot.torch_mesh_flange) {
          this.torchMesh.matrix.copy(toolMatrix);
          this.torchMesh.matrixWorldNeedsUpdate = true;
        }
        if (this.tcpMarker && state.robot.tcp_flange)
          this.tcpMarker.position.set(...state.robot.tcp_flange).applyMatrix4(toolMatrix);
      }
      this.diagnostics.visible = !!flags.boxes && flags.scene !== false;
      const wire = frame.style !== undefined && frame.style !== "solid";
      for (const [kind, material] of this.materials) material.wireframe = wire && !["ground", "bead", "tcp", "conduit"].includes(kind.split(":")[0]);
      const poseKey = JSON.stringify([frame.pose && frame.pose.q, robotVisible, flags.conduits]);
      if (poseKey !== this.poseKey) {
        if (frames) this.updateConduits(frames, robotVisible && flags.conduits !== false);
        else this.conduits.visible = false;
        this.poseKey = poseKey; this.renderer.shadowMap.needsUpdate = true;
      }
      this.spine.visible = flags.robot !== false && !!frame.pose && !this.links.length;
      if (this.spine.visible) {
        const points = []; const sk = frame.pose.sk;
        for (let i = 1; i < sk.length; i++) points.push(...sk[i - 1], ...sk[i]);
        this.spine.geometry.setAttribute("position", new T.Float32BufferAttribute(points, 3));
      }
      const moves = state.toolpath.moves, cursor = Math.max(0, Math.min(moves.length - 1, frame.cursor || 0));
      const index = Math.floor(cursor), fraction = cursor - index;
      const deposits = upperBound(this.depositEnds, index), travels = upperBound(this.travelEnds, index);
      this.deposits.count = deposits;
      this.ghost.visible = !!flags.ghost; this.ghost.geometry.setDrawRange(deposits * 2, (this.depositEnds.length - deposits) * 2);
      this.travel.visible = !!flags.travel; this.travel.geometry.setDrawRange(0, travels * 2);
      const a = moves[index] || {x: 0, y: 0, z: 0}, b = moves[Math.min(index + 1, moves.length - 1)] || a;
      const tip = frame.checkedPoint || { x: a.x + (b.x - a.x) * fraction, y: a.y + (b.y - a.y) * fraction, z: a.z + (b.z - a.z) * fraction };
      this.partial.visible = fraction > 0 && !!b.d;
      if (this.partial.visible) {
        this.partial.matrix.copy(this.segmentMatrix(a, tip, this.beadRadius, this.beadHalfHeight, this.beadHalfHeight));
        const event = this.collisions.segments.get(index + 1);
        this.partial.material.color.set(placement ? 0x3b82a8 : event ? event.color : 0xd9ad60);
      }
      this.partialTravel.visible = fraction > 0 && !b.d && !!flags.travel;
      if (this.partialTravel.visible) {
        const attribute = this.partialTravel.geometry.getAttribute("position");
        attribute.setXYZ(0, a.x, a.y, a.z); attribute.setXYZ(1, tip.x, tip.y, tip.z); attribute.needsUpdate = true;
      }
      this.tip.position.set(tip.x, tip.y, tip.z); this.tip.visible = !!flags.torch && !placement;
      this.tipClip.constant = -this.tip.position.z;
      this.torch.position.copy(this.tip.position);
      this.torch.position.z += this.state.torch?.command_from_physical_z_mm || 0;
      this.torch.visible = !!flags.torch && moves.length > 0 && !placement;
      const flange = frames && frames[6];
      if (flange) this.capsuleVolume.quaternion.setFromRotationMatrix(
        new T.Matrix4().set(...linkMatrix({ R: flange.R, p: [0, 0, 0] }, [0, 0, 0], [0, 0, 0])));
      else this.capsuleVolume.quaternion.copy(this.referenceToolRotation);
      const row = this.collisions.byMove.get(frame.violationMove === undefined ? Math.ceil(cursor) : frame.violationMove);
      const color = row ? row.worst.color : 0x248fbb;
      for (const material of this.toolMaterials) material.color.set(color);
      const signature = JSON.stringify([cursor, flags.scene, flags.frame, flags.walls, flags.fumeHood, flags.part, frame.style, placement]);
      if (signature !== this.signature) { this.renderer.shadowMap.needsUpdate = true; this.signature = signature; }
      this.renderer.render(this.scene, this.camera); return true;
    }

    dispose() {
      this.active = false; this.clearParts();
      this.renderer.domElement.removeEventListener("webglcontextlost", this.onContextLost);
      this.renderer.domElement.removeEventListener("webglcontextrestored", this.onContextRestored);
      this.usedGeometries.clear(); this.pruneGeometry();
      for (const material of this.materials.values()) material.dispose();
      this.environment.dispose(); this.renderer.dispose();
    }
  }

  global.WaamRenderer = { create, StudioRenderer, collisionDisplay: true, math: { creaseNormals, cameraFrame, linkMatrix } };
})(typeof window !== "undefined" ? window : globalThis);
