
(function (global) {
  "use strict";
  const meshes = new Map();
  let rendererPromise = null, mode = "auto", light = false;
  try { mode = global.localStorage.getItem("waamPreviewDetail") || "auto"; } catch (_) {}
  if (!["auto", "full", "light"].includes(mode)) mode = "auto";
  function choose(value) {
    mode = value;
    light = mode === "light" || (mode === "auto" &&
      global.matchMedia("(pointer: coarse)").matches && global.matchMedia("(max-width: 800px)").matches);
  }
  choose(mode);
  function url(key) {
    if (!/^(renderer|robot)-[a-f0-9]{64}$/.test(key)) throw new Error("Invalid preview asset");

    const q = new URLSearchParams({visual: key});
    const token = new URLSearchParams(global.location.search).get("k");
    if (token) q.set("k", token);
    return "/?" + q;
  }
  function renderer(key) {
    if (global.WaamRenderer) return Promise.resolve();
    if (!rendererPromise) rendererPromise = new Promise((resolve, reject) => {
      const script = global.document.createElement("script");
      script.src = url(key);
      script.onload = () => global.WaamRenderer ? resolve() : reject(new Error("Preview renderer unavailable"));
      script.onerror = () => { script.remove(); reject(new Error("Preview download failed")); };
      global.document.head.appendChild(script);
    }).catch(error => { rendererPromise = null; throw error; });
    return rendererPromise;
  }
  async function hydrate(state) {
    if (!state.robot || !state.robot.mesh_asset) return;
    if (light) { state.robot.meshes = null; return; }
    const key = state.robot.mesh_asset;
    if (!meshes.has(key)) {
      const pending = global.fetch(url(key), {credentials: "same-origin", cache: "no-store"})
        .then(response => { if (!response.ok) throw new Error("Preview download failed"); return response.json(); })
        .catch(error => { meshes.delete(key); throw error; });

      meshes.set(key, pending);
      if (meshes.size > 2) meshes.delete(meshes.keys().next().value);
    } else {
      const cached = meshes.get(key);
      meshes.delete(key); meshes.set(key, cached);
    }
    const data = await meshes.get(key);

    state.robot.meshes = light ? null : data;
  }
  global.WaamAssets = {
    get light() { return light; }, get mode() { return mode; }, hydrate,


    setMode(value, persist = true) {
      choose(["auto", "full", "light"].includes(value) ? value : "auto");
      if (!persist) return;
      try { global.localStorage.setItem("waamPreviewDetail", mode); } catch (_) {}
    },
    async prepare(state, key) {
      if (light) { await hydrate(state); return; }
      await Promise.all([renderer(key), hydrate(state)]);
    }
  };
})(window);
