/* Race recap: "Arcade Satellite".
   Pick a run → the server fetches its GPS track, an aerial photo and nearby buildings once → this page builds a
   3D scene from that real data, draws the game HUD on a canvas over each 3D frame, and saves MP4/PNG files in
   the browser. Text from the server is only ever drawn on the canvas or set with textContent. */
"use strict";
(function () {
  /* ================================================================ constants and small helpers */
  const CLIP_S = 14, FPS = 30, RUN_S = 12;
  const TL = { sat: 0.9, ready: 1.7, go: 2.4, run0: 1.9, run1: 11.0, clear: 11.1, score: 11.9 };
  const SIZES = { "916": [1080, 1920], "45": [1080, 1350], "11": [1080, 1080] };
  const COL = { ink: "#0b0d1c", gold: "#ffd23f", cyan: "#4fe3ff", pink: "#ff4fa3", good: "#5cf07a", bad: "#ff6464" };
  const ZONE_CSS = ["#9aa3b5", "#3ec7ff", "#5cf07a", "#ffd23f", "#ff5a5a"];   // Z1–Z5
  const ZONE_PCT = [0.6, 0.7, 0.8, 0.9];                                      // Garmin's default bands, % of max HR
  const PX = '"Press Start 2P", ui-monospace, monospace', PX2 = '"Silkscreen", ui-monospace, monospace';
  const LOWPOWER = (navigator.hardwareConcurrency || 4) <= 4 || Math.min(screen.width, screen.height) < 700;

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, f) => a + (b - a) * f;
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const easeIO = (x) => { x = clamp(x, 0, 1); return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; };
  const easeBack = (x) => { x = clamp(x, 0, 1); const c = 1.7; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
  function mulberry32(a) { return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  const fmtTime = (s) => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`; };
  const fmtPace = (s) => { if (!isFinite(s) || s <= 0) return "-"; s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const progressOf = (h) => { const x = clamp(h / RUN_S, 0, 1); return x < 0.5 ? 2 * x * x * (1.15 - 0.3 * x) : 1 - Math.pow(-2 * x + 2, 2) / 2; };
  const progress = (head) => progressOf(clamp((head - TL.run0) / (TL.run1 - TL.run0), 0, 1) * RUN_S);
  const phaseOf = (h) => h < TL.sat ? "sat" : h < TL.ready ? "ready" : h < TL.go ? "go" : h < TL.clear ? "run" : h < TL.score ? "clear" : "score";
  const blink = (head, period) => Math.floor(head / period) % 2 === 0;
  function search(arr, v) { let lo = 0, hi = arr.length - 1; if (v <= arr[0]) return 0; if (v >= arr[hi]) return hi - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m; else hi = m; } return lo; }
  function interp(xs, ys, v) { const i = search(xs, v), span = xs[i + 1] - xs[i]; const f = span > 0 ? clamp((v - xs[i]) / span, 0, 1) : 0; return ys[i] + (ys[i + 1] - ys[i]) * f; }
  function filled(arr) { const out = arr.slice(); let last = out.find((v) => v != null) ?? null;
    for (let i = 0; i < out.length; i++) { if (out[i] == null) out[i] = last; else last = out[i]; } return out; }
  function smoothArr(arr, win) { if (arr.some((v) => v == null)) return arr; const out = new Array(arr.length); let s = 0, n = 0;
    for (let i = 0; i < arr.length + win; i++) { if (i < arr.length) { s += arr[i]; n++; } if (i - 2 * win - 1 >= 0) { s -= arr[i - 2 * win - 1]; n--; } const j = i - win; if (j >= 0 && j < arr.length) out[j] = s / n; } return out; }

  /* ================================================================ the run, as numbers the animation needs */
  function buildModel(d, opts) {
    const metric = d.units === "metric", unitM = metric ? 1000 : 1609.344;
    const P = d.points, n0 = P.x.length;
    const keep = [];
    for (let i = 0, lx = 0, ly = 0; i < n0; i++) {
      if (i === 0 || i === n0 - 1 || Math.hypot(P.x[i] - lx, P.y[i] - ly) >= 1.5) { keep.push(i); lx = P.x[i]; ly = P.y[i]; }
    }
    const pts = keep.map((i) => new THREE.Vector3(P.x[i] / 100, 0.06, -P.y[i] / 100));
    const dK = keep.map((i) => P.d[i]), tK = keep.map((i) => P.t[i]);
    for (let i = 1; i < dK.length; i++) { if (dK[i] < dK[i - 1]) dK[i] = dK[i - 1]; if (tK[i] < tK[i - 1]) tK[i] = tK[i - 1]; }
    const hrK = smoothArr(filled(keep.map((i) => P.hr[i])), 6), cadK = smoothArr(filled(keep.map((i) => P.cad[i])), 6);
    const altK = smoothArr(filled(keep.map((i) => P.alt[i])), 6);
    const cum = [0]; for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + pts[i].distanceTo(pts[i - 1]));
    const L = cum[cum.length - 1] || 1, uK = cum.map((c) => c / L);
    const curve = new THREE.CatmullRomCurve3(pts, false, "centripetal");
    curve.arcLengthDivisions = clamp(pts.length * 4, 2000, 24000);
    const totalD = d.totals.distance_m, totalT = d.totals.timer_s;
    const cumS = (metric ? d.splits.km : d.splits.mi).slice();
    const splits = cumS.map((c, i) => c - (i ? cumS[i - 1] : 0));
    const restM = totalD - cumS.length * unitM, restS = totalT - (cumS.length ? cumS[cumS.length - 1] : 0);
    const avgPace = totalT / (totalD / unitM);
    const paceAtD = (dm) => { const i = Math.floor(dm / unitM); if (i < splits.length) return splits[i];
      return restM > 30 ? restS / (restM / unitM) : (splits.length ? splits[splits.length - 1] : avgPace); };
    const combo = []; splits.forEach((s, i) => combo.push(i && s < splits[i - 1] ? combo[i - 1] + 1 : 0));
    const best = splits.map((s, i) => ({ s, k: i + 1 })).sort((a, b) => a.s - b.s).slice(0, 3);
    const hideM = clamp(opts.hideM || 0, 0, totalD * 0.2);
    const goal = d.goal && opts.ghost === "goal" ? d.goal : null;
    const secPerM = goal ? goal.goal_s / (goal.km * 1000) : totalT / totalD;
    const hrMax = d.hr_max || d.totals.max_hr || 190;
    const gainM = d.totals.gain_m;
    return {
      d, metric, unitM, curve, L, uK, dK, tK, hrK, cadK, altK, totalD, totalT, cumS, splits, avgPace, paceAtD,
      combo, maxCombo: combo.length ? Math.max(...combo) : 0, best, hideM, goal, secPerM, hrMax, gainM,
      uAtD: (dm) => interp(dK, uK, clamp(dm, 0, dK[dK.length - 1])),
      dAtU: (u) => interp(uK, dK, clamp(u, 0, 1)),
      dAtT: (ts) => interp(tK, dK, clamp(ts, 0, tK[tK.length - 1])),
      atT: (arr, ts) => (arr[0] == null ? null : interp(tK, arr, clamp(ts, 0, tK[tK.length - 1]))),
      zoneOf: (hr) => ZONE_PCT.filter((p) => hr >= p * hrMax).length,
      unitLabel: metric ? "KM" : "MI", unitWord: metric ? "KM" : "MILE",
    };
  }

  /* ================================================================ ground: aerial photo and its arcade version */
  const MAP = [   // [colour in the aerial photo, colour in the game]
    ["#161a18", "#1b1e2e"], ["#30343a", "#353a52"], ["#4e5052", "#4a4f6a"], ["#6e6e6c", "#64687e"], ["#918f8a", "#8f91a6"],
    ["#b4b2aa", "#bdbccb"], ["#d7d4cd", "#e0dde8"], ["#f5f4f0", "#f6f4ff"], ["#6e5848", "#a8623f"], ["#965a46", "#d0583c"],
    ["#263222", "#1e5c34"], ["#3a4a2e", "#2f8a3e"], ["#56683e", "#4fa646"], ["#768052", "#7cc04a"], ["#96916e", "#b4c25a"],
    ["#afa07d", "#e6c47e"], ["#283a3e", "#2a64c8"], ["#3c555c", "#3f8ee8"], ["#5aaabe", "#5cf0ff"], ["#7d1f1f", "#d23a4a"],
  ].map(([a, b]) => [[parseInt(a.slice(1, 3), 16), parseInt(a.slice(3, 5), 16), parseInt(a.slice(5, 7), 16)],
    [parseInt(b.slice(1, 3), 16), parseInt(b.slice(3, 5), 16), parseInt(b.slice(5, 7), 16)]]);
  const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];

  function cpuCanvas(w, h) {
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    // CPU-backed: many reads and small draws. Anything drawn into it must not be a GPU canvas, or it turns slow.
    return [c, c.getContext("2d", { willReadFrequently: true })];
  }

  function arcadeFrom(source, size) {
    const [c, x] = cpuCanvas(size, size);
    x.imageSmoothingEnabled = true; x.imageSmoothingQuality = "high"; x.drawImage(source, 0, 0, size, size);
    const id = x.getImageData(0, 0, size, size), px = id.data;
    const lut = new Int8Array(64 * 64 * 64).fill(-1);
    const nearest = (r, g, b) => { let best = 0, bd = 1e9;
      for (let i = 0; i < MAP.length; i++) { const k = MAP[i][0], dr = r - k[0], dg = g - k[1], db = b - k[2], dd = 2 * dr * dr + 4 * dg * dg + 3 * db * db; if (dd < bd) { bd = dd; best = i; } }
      return best; };
    for (let y = 0; y < size; y++) for (let xx = 0; xx < size; xx++) {
      const o = (y * size + xx) * 4, dth = (BAYER[(y & 3) * 4 + (xx & 3)] / 16 - 0.47) * 22;
      const r = clamp(px[o] + dth, 0, 255) >> 2, g = clamp(px[o + 1] + dth, 0, 255) >> 2, b = clamp(px[o + 2] + dth, 0, 255) >> 2;
      const key = (r << 12) | (g << 6) | b;
      let i = lut[key]; if (i < 0) i = lut[key] = nearest(r * 4 + 2, g * 4 + 2, b * 4 + 2);
      const out = MAP[i][1]; px[o] = out[0]; px[o + 1] = out[1]; px[o + 2] = out[2];
    }
    x.putImageData(id, 0, 0);
    return c;
  }

  function fallbackBoard(size, sideM, dark) {
    // No aerial photo (outside the US mainland or the service was down): a plain game board with a 100 m grid.
    const [c, x] = cpuCanvas(size, size), step = size / (sideM / 100);
    x.fillStyle = dark ? "#0f1224" : "#161a33"; x.fillRect(0, 0, size, size);
    for (let i = 0, k = 0; i <= size + 1; i += step, k++) {
      x.fillStyle = k % 5 === 0 ? "rgba(79,227,255,0.22)" : "rgba(79,227,255,0.08)";
      const w = Math.max(1, size / 1024);
      x.fillRect(Math.round(i), 0, w, size); x.fillRect(0, Math.round(i), size, w);
    }
    return c;
  }

  /* ================================================================ pixel sprites */
  function sheet(frames, pal, outline, cell = [16, 20]) {
    const c = document.createElement("canvas"); c.width = cell[0] * frames.length; c.height = cell[1];
    const x = c.getContext("2d");
    frames.forEach((rows, f) => {
      const w = rows[0].length, ox = f * cell[0] + Math.floor((cell[0] - w) / 2), oy = Math.floor((cell[1] - rows.length) / 2);
      const on = (i, j) => j >= 0 && j < rows.length && i >= 0 && i < w && rows[j][i] !== ".";
      for (let j = -1; j <= rows.length; j++) for (let i = -1; i <= w; i++) {
        if (on(i, j)) { x.fillStyle = pal[rows[j][i]]; x.fillRect(ox + i, oy + j, 1, 1); }
        else if (on(i - 1, j) || on(i + 1, j) || on(i, j - 1) || on(i, j + 1)) { x.fillStyle = outline; x.fillRect(ox + i, oy + j, 1, 1); }
      }
    });
    const t = new THREE.CanvasTexture(c); t.magFilter = t.minFilter = THREE.NearestFilter; t.generateMipmaps = false;
    t.repeat.set(1 / frames.length, 1); return t;
  }
  const RUN_A = ["....hhhh....", "...hhhhhh...", "...hhhhhh...", "....hhhh....", ".....kk.....", "..tttttttt..", ".kttttttttk.", ".k.tttttt.k.",
    "kk.tttttt..k", "...TTTTTT...", "...pppppp...", "...pp..ppp..", "...kk...kk..", "...kk...SS..", "...kk.......", "...ss......."];
  const RUN_B = RUN_A.map((r) => [...r].reverse().join(""));
  const WIN = [".k..hhhh..k.", ".k.hhhhhh.k.", ".kkhhhhhhkk.", "..k.hhhh.k..", "..tt.kk.tt..", "...tttttt...", "...tttttt...", "...tttttt...",
    "...tttttt...", "...TTTTTT...", "...pppppp...", "...pp..pp...", "...kk..kk...", "...kk..kk...", "...kk..kk...", "..sss..sss.."];
  const COIN = ["...rrrr...", ".rrggggrr.", "rgwgggyggr", "rgwgggyggr", "rgwgggyggr", "rgggggyggr", "rgggggyggr", "rgggggyggr", ".rrggggrr.", "...rrrr..."];
  const BODY = { h: "#3a2418", k: "#f0b48a", t: "#ffd23f", T: "#e5a800", p: "#2a3a8c", s: "#ffffff", S: "#ff4fa3" };
  const GHOST = Object.fromEntries(Object.keys(BODY).map((k) => [k, ["t", "s", "k"].includes(k) ? "#b8f6ff" : "#4fe3ff"]));

  /* ================================================================ the 3D stage */
  class Stage {
    constructor() {
      this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, powerPreference: "high-performance" });
      this.renderer.outputEncoding = THREE.LinearEncoding;   // palette colours exactly as written
      this.renderer.setPixelRatio(1);
      this.camera = new THREE.PerspectiveCamera(42, 9 / 16, 0.1, 2000);
      this.scene = null; this.w = 0; this.h = 0;
      this.maxTex = this.renderer.capabilities.maxTextureSize;
    }

    dispose() {
      if (!this.scene) return;
      this.scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        const mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
        for (const m of mats) { for (const k of ["map"]) if (m[k]) m[k].dispose(); if (m.uniforms) for (const u of Object.values(m.uniforms)) if (u.value && u.value.isTexture) u.value.dispose(); m.dispose(); }
      });
      this.scene = null;
    }

    load(m, photo) {
      this.dispose();
      this.m = m;
      const s = this.scene = new THREE.Scene();
      s.background = new THREE.Color(0x0b0d1c);
      const side = m.d.side_m / 100;
      // ground textures
      const big = !LOWPOWER && this.maxTex >= 4096 ? 4096 : 2048, small = LOWPOWER ? 1024 : 2048;
      let photoC, arcC;
      if (photo) {
        photoC = document.createElement("canvas"); photoC.width = photoC.height = big;
        const g = photoC.getContext("2d"); g.imageSmoothingQuality = "high"; g.drawImage(photo, 0, 0, big, big);
        arcC = arcadeFrom(photo, small);
      } else {
        photoC = fallbackBoard(small, m.d.side_m, true); arcC = fallbackBoard(small, m.d.side_m, false);
      }
      const aniso = Math.min(8, this.renderer.capabilities.getMaxAnisotropy());
      const tPhoto = new THREE.CanvasTexture(photoC); tPhoto.anisotropy = aniso;
      const tArc = new THREE.CanvasTexture(arcC); tArc.anisotropy = aniso; tArc.magFilter = THREE.NearestFilter;
      const geo = new THREE.PlaneGeometry(side, side, 1, 1); geo.rotateX(-Math.PI / 2);
      // where the run starts (or where it appears, when the start is hidden)
      this.u0 = m.uAtD(m.hideM); this.u1 = m.uAtD(m.totalD - m.hideM);
      const S0 = m.curve.getPointAt(this.u0);
      this.S0 = S0;
      this.uni = { uPhoto: { value: tPhoto }, uArc: { value: tArc }, uWipe: { value: -5 }, uRing: { value: 0 }, uStart: { value: new THREE.Vector2(S0.x, S0.z) } };
      s.add(new THREE.Mesh(geo, new THREE.ShaderMaterial({ uniforms: this.uni,
        vertexShader: "varying vec2 vUv; varying vec2 vXZ; void main() { vUv = uv; vXZ = position.xz; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
        fragmentShader: `varying vec2 vUv; varying vec2 vXZ; uniform sampler2D uPhoto; uniform sampler2D uArc; uniform float uWipe; uniform float uRing; uniform vec2 uStart;
          void main() {
            float d = distance(vXZ, uStart);
            float a = clamp((uWipe - d) / 1.4, 0.0, 1.0);
            vec3 col = mix(texture2D(uPhoto, vUv).rgb, texture2D(uArc, vUv).rgb, a);
            float ring = exp(-pow((d - uWipe) / 0.8, 2.0)) * uRing;
            col = mix(col, vec3(0.31, 0.89, 1.0), clamp(ring * 0.85, 0.0, 1.0));
            vec2 e = min(vUv, 1.0 - vUv);
            col *= mix(0.25, 1.0, smoothstep(0.0, 0.04, min(e.x, e.y)));
            gl_FragColor = vec4(col, 1.0);
          }` })));
      // buildings: boxes from OpenStreetMap outlines, raised by the shockwave
      const B = m.d.buildings || [];
      this.bld = null;
      if (B.length) {
        const box = new THREE.BoxGeometry(1, 1, 1); box.translate(0, 0.5, 0);
        this.bld = new THREE.InstancedMesh(box, new THREE.MeshLambertMaterial(), B.length);
        this.out = new THREE.InstancedMesh(box, new THREE.MeshBasicMaterial({ color: 0x0b0d1c, side: THREE.BackSide }), B.length);
        const pal = { low: ["#d2b896", "#c8c0b0", "#b9a58c", "#a7b0b8", "#d9d5ca"], mid: ["#b7b1a2", "#9aa6b8", "#c9b79a", "#8fa0b8", "#e2ddcf"],
          tower: ["#6f8fc0", "#8fb8d0", "#c9c2b0", "#9aa6c4", "#5c7ab0", "#dcd4c2"] };
        const col = new THREE.Color(), rnd = mulberry32(7);
        this.bdata = B.map(([x, y, w, dd, ang, h], i) => {
          const kind = h > 30 ? "tower" : h > 9 ? "mid" : "low";
          this.bld.setColorAt(i, col.set(pal[kind][Math.floor(rnd() * pal[kind].length)]));
          const X = x / 100, Z = -y / 100, W = w / 100, D = dd / 100, Hh = Math.max(0.05, h / 100);
          return { X, Z, W, D, H: Hh, q: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), ang),
            o: Math.min(0.03, 0.15 * Math.min(W, D)), dist: Math.hypot(X - S0.x, Z - S0.z) };
        });
        this.bld.instanceColor.needsUpdate = true;
        s.add(this.bld, this.out);
      }
      s.add(new THREE.AmbientLight(0xffffff, 0.46));
      const sun = new THREE.DirectionalLight(0xffffff, 0.66); sun.position.set(50, 100, 35); s.add(sun);
      this.lastWipe = null;
      // trail: gold, pink where you were faster than your average pace
      const segs = clamp(Math.round(m.L * 12), 600, 6000);
      const tubeC = (r) => {
        const tg = new THREE.TubeGeometry(m.curve, segs, r, 6, false), cnt = tg.attributes.position.count, cc = new Float32Array(cnt * 3);
        const gold = new THREE.Color(0xffd23f), pink = new THREE.Color(0xff4fa3);
        for (let v = 0; v < cnt; v++) { const c = m.paceAtD(m.dAtU(Math.floor(v / 7) / segs)) < m.avgPace ? pink : gold; cc.set([c.r, c.g, c.b], v * 3); }
        tg.setAttribute("color", new THREE.BufferAttribute(cc, 3)); return tg;
      };
      this.segs = segs;
      this.trail = new THREE.Mesh(tubeC(0.13), new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true }));
      this.glow = new THREE.Mesh(tubeC(0.24), new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.26, blending: THREE.AdditiveBlending, depthWrite: false }));
      this.big = new THREE.Mesh(tubeC(Math.max(0.6, m.L / 280)), new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true }));
      s.add(this.big, this.trail, this.glow);
      // coins at every mile (or km), pellets in between
      this.coins = [];
      const coinTex = sheet([COIN], { r: "#b8770a", g: "#ffd23f", w: "#fff3b0", y: "#e5a800" }, "#0b0d1c", [14, 14]);
      m.cumS.forEach((tAt, i) => {
        const dm = (i + 1) * m.unitM;
        if (dm < m.hideM || dm > m.totalD - m.hideM) return;
        const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: coinTex, transparent: true }));
        const p = m.curve.getPointAt(m.uAtD(dm)); p.y += 0.85; s.add(sp);
        this.coins.push({ sp, i, p, t: tAt });
      });
      const pel = [], pu = [], step = 0.62 / m.L;
      for (let u = this.u0 + step; u < this.u1; u += step) {
        const dm = m.dAtU(u);
        if (Math.abs(dm / m.unitM - Math.round(dm / m.unitM)) * m.unitM < 40) continue;
        const p = m.curve.getPointAt(u); pel.push(p.x, p.y + 0.14, p.z); pu.push(u);
      }
      const pg = new THREE.BufferGeometry(); pg.setAttribute("position", new THREE.Float32BufferAttribute(pel, 3));
      this.pellets = new THREE.Points(pg, new THREE.PointsMaterial({ color: 0xfff1b8, size: 0.24, transparent: true })); this.pelU = pu; s.add(this.pellets);
      this.spark = Array.from({ length: 6 }, () => { const sp = new THREE.Sprite(new THREE.SpriteMaterial({ color: 0xfff3b0, transparent: true })); sp.scale.setScalar(0.2); s.add(sp); return sp; });
      // boost exhaust
      const fc = []; for (let k = 0; k < 12; k++) { const c = new THREE.Color(0xfff1b8).lerp(new THREE.Color(k < 5 ? 0xff9f43 : 0xff4fa3), Math.min(1, k / 6)); fc.push(c.r, c.g, c.b); }
      const fg = new THREE.BufferGeometry(); fg.setAttribute("position", new THREE.Float32BufferAttribute(new Float32Array(36), 3)); fg.setAttribute("color", new THREE.Float32BufferAttribute(fc, 3));
      this.fire = new THREE.Points(fg, new THREE.PointsMaterial({ size: 0.34, vertexColors: true, transparent: true })); s.add(this.fire);
      // start and finish lines (only when the ends aren't hidden)
      this.lines = [];
      if (!m.hideM) {
        const chk = document.createElement("canvas"); chk.width = 8; chk.height = 2; const cx = chk.getContext("2d");
        for (let i = 0; i < 8; i++) for (let j = 0; j < 2; j++) { cx.fillStyle = (i + j) % 2 ? "#0b0d1c" : "#ffffff"; cx.fillRect(i, j, 1, 1); }
        const ct = new THREE.CanvasTexture(chk); ct.magFilter = ct.minFilter = THREE.NearestFilter; ct.generateMipmaps = false;
        const ends = [0]; if (m.curve.getPointAt(0).distanceTo(m.curve.getPointAt(1)) > 1.2) ends.push(1);
        for (const u of ends) {
          const ln = new THREE.Mesh(new THREE.PlaneGeometry(1.5, 0.38), new THREE.MeshBasicMaterial({ map: ct, transparent: true }));
          const h = this.heading(u); ln.rotation.set(-Math.PI / 2, Math.atan2(h.x, h.z), 0, "YXZ");
          ln.position.copy(m.curve.getPointAt(u)).add(new THREE.Vector3(0, 0.02, 0)); s.add(ln); this.lines.push(ln);
        }
      }
      // you and the ghost
      const mk = (pal, outline, op) => { const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: sheet([RUN_A, RUN_B, WIN], pal, outline), transparent: true, opacity: op, sizeAttenuation: false, depthTest: false }));
        sp.center.set(0.5, 0.1); sp.renderOrder = 10; sp.scale.set(0.048, 0.06, 1); s.add(sp); return sp; };
      this.ghost = mk(GHOST, "#0b3a52", 0.62); this.player = mk(BODY, "#0b0d1c", 1);
      this.shadow = new THREE.Mesh(new THREE.CircleGeometry(0.42, 20), new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35, depthWrite: false }));
      this.shadow.rotation.x = -Math.PI / 2; s.add(this.shadow);
      // clip seconds at which the run reaches a given elapsed time
      this.hT = []; for (let h = 0; h <= CLIP_S + 1e-9; h += 0.01) this.hT.push([h, progress(h)]);
      this.coins.forEach((c) => { c.head = this.headOfP(c.t / m.totalT); });
      // the visible part of the course, for the whole-course shot
      const e = { x0: 1e9, x1: -1e9, z0: 1e9, z1: -1e9 };
      for (let u = this.u0; u <= this.u1 + 1e-9; u += (this.u1 - this.u0) / 400 || 1) { const p = m.curve.getPointAt(Math.min(u, 1)); e.x0 = Math.min(e.x0, p.x); e.x1 = Math.max(e.x1, p.x); e.z0 = Math.min(e.z0, p.z); e.z1 = Math.max(e.z1, p.z); }
      this.ext = e;
      this.chase = clamp(8 + m.L * 0.03, 10, 22);  // camera distance grows with the course (L is in 100 m units)
    }

    headOfP(pq) { const a = this.hT; let lo = 0, hi = a.length - 1; while (lo < hi) { const k = (lo + hi) >> 1; if (a[k][1] >= pq) hi = k; else lo = k + 1; } return a[lo][0]; }

    heading(u) { const m = this.m, v = new THREE.Vector3(), du = 0.4 / m.L;
      for (let k = -3; k <= 3; k++) v.add(m.curve.getTangentAt(clamp(u + k * du, 0, 1))); v.y = 0; return v.lengthSq() ? v.normalize() : new THREE.Vector3(0, 0, -1); }

    setSize(w, h) {
      if (w === this.w && h === this.h) return;
      this.w = w; this.h = h; this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    }

    /* Draw the 3D frame for clip time `head` and return the HUD state to paint over it. */
    render(head) {
      const m = this.m, ph = phaseOf(head), cam = this.camera;
      const p01 = progress(head), tau = p01 * m.totalT;
      const dNow = p01 >= 1 ? m.totalD : m.dAtT(tau);
      const dShown = clamp(dNow, m.hideM, m.totalD - m.hideM), uNow = m.uAtD(dShown);
      const visible = !m.hideM || (dNow >= m.hideM && dNow <= m.totalD - m.hideM);
      // shockwave: photo → game board
      const wipe = head < TL.sat ? -5 : head < TL.ready ? 32 * easeIO((head - TL.sat) / (TL.ready - TL.sat)) : lerp(32, Math.max(170, m.d.side_m / 70), smooth(TL.ready, 2.7, head));
      this.uni.uWipe.value = wipe; this.uni.uRing.value = head < TL.sat ? 0 : 1 - smooth(2.1, 2.7, head);
      if (this.bld && this.lastWipe !== wipe) {
        this.lastWipe = wipe;
        const M = new THREE.Matrix4(), O = new THREE.Matrix4(), pos = new THREE.Vector3(), sc = new THREE.Vector3();
        this.bdata.forEach((b, i) => {
          const k = clamp((wipe - b.dist) / 3.2, 0, 1);
          if (k <= 0) { M.makeScale(0, 0, 0); this.bld.setMatrixAt(i, M); this.out.setMatrixAt(i, M); return; }
          const sy = easeBack(k);
          M.compose(pos.set(b.X, 0, b.Z), b.q, sc.set(b.W, b.H * sy, b.D)); this.bld.setMatrixAt(i, M);
          O.compose(pos.set(b.X, -b.o / 2, b.Z), b.q, sc.set(b.W + b.o * 2, b.H * sy + b.o, b.D + b.o * 2)); this.out.setMatrixAt(i, O);
        });
        this.bld.instanceMatrix.needsUpdate = this.out.instanceMatrix.needsUpdate = true;
      }
      const game = smooth(1.0, 1.5, head);
      // camera: straight down (north up) on the start → tilt into a chase → back up over the whole course
      let boost = 0; for (let k = -4; k <= 4; k++) boost += m.paceAtD(clamp(dShown + k * 25, 0, m.totalD)) < m.avgPace ? 1 / 9 : 0;
      const K = this.chase / 14.5;
      const north = new THREE.Vector3(0, 0, -1);
      const intro = { T: this.S0.clone(), u: north, pitch: Math.PI / 2, dist: lerp(31, 24, smooth(0, TL.ready, head)) * K };
      const p = m.curve.getPointAt(uNow), u = this.heading(uNow);
      const chase = { T: p.clone().addScaledVector(u, 1.8 * K), u, pitch: 0.94, dist: this.chase - 1.6 * boost * K };
      const e = this.ext, tanV = Math.tan(THREE.MathUtils.degToRad(21));
      const fitD = Math.max((e.z1 - e.z0) / 2 / tanV, (e.x1 - e.x0) / 2 / (tanV * cam.aspect)) * (cam.aspect < 0.7 ? 1.3 : 1.2) + 2;
      const fin = { T: new THREE.Vector3((e.x0 + e.x1) / 2, 0, (e.z0 + e.z1) / 2), u: north, pitch: Math.PI / 2, dist: fitD * (1 + 0.03 * smooth(TL.score, CLIP_S, head)) };
      const mix = (a, b, f) => { const uu = a.u.clone().lerp(b.u, f); if (uu.lengthSq() < 1e-6) uu.copy(b.u); return { T: a.T.clone().lerp(b.T, f), u: uu.normalize(), pitch: lerp(a.pitch, b.pitch, f), dist: lerp(a.dist, b.dist, f) }; };
      const wc = easeIO((head - TL.ready) / 1.3), wf = easeIO((head - 10.6) / 1.3);
      let P = wc <= 0 ? intro : wc >= 1 ? chase : mix(intro, chase, wc);
      if (wf > 0) P = mix(P, fin, wf);
      const cp = Math.cos(P.pitch), spp = Math.sin(P.pitch);
      cam.position.set(P.T.x - P.u.x * P.dist * cp, P.T.y + P.dist * spp, P.T.z - P.u.z * P.dist * cp);
      cam.up.copy(P.u); cam.lookAt(P.T);
      cam.far = Math.max(600, P.dist * 4);
      cam.fov = 42 + 5 * boost * wc * (1 - wf); cam.updateProjectionMatrix();
      // trail between the hidden ends
      const a0 = Math.floor(this.u0 * this.segs), a1 = Math.max(a0, Math.floor(uNow * this.segs));
      for (const t of [this.trail, this.glow, this.big]) t.geometry.setDrawRange(a0 * 36, (a1 - a0) * 36);
      this.big.material.opacity = smooth(28 * K, 60 * K, P.dist); this.trail.material.opacity = 1 - this.big.material.opacity * 0.9;
      this.big.visible = this.big.material.opacity > 0.01;
      let first = this.pelU.findIndex((x) => x > uNow + 1e-4); if (first < 0) first = this.pelU.length;
      this.pellets.geometry.setDrawRange(first, this.pelU.length - first);
      this.pellets.material.opacity = game * (1 - smooth(28 * K, 45 * K, P.dist));
      // coins
      let pop = null;
      for (const c of this.coins) {
        const got = tau >= c.t || p01 >= 1, age = head - c.head;
        if (!got) {
          c.sp.visible = game > 0; c.sp.material.opacity = game;
          c.sp.position.copy(c.p).add(new THREE.Vector3(0, 0.12 * Math.sin(head * 3 + c.i), 0));
          c.sp.scale.set(0.95 * Math.max(0.14, Math.abs(Math.cos(head * 3.2 + c.i * 0.7))), 0.95, 1);
        } else if (age < 0.5 && age >= 0) {
          c.sp.visible = true; c.sp.material.opacity = 1 - age / 0.5;
          c.sp.position.copy(c.p).add(new THREE.Vector3(0, age * 3.2, 0)); c.sp.scale.set(0.95 + age, 0.95 + age, 1); pop = { c, age };
        } else c.sp.visible = false;
      }
      this.spark.forEach((sp, k) => {
        sp.visible = !!pop && pop.age < 0.45; if (!sp.visible) return;
        const a = k / 6 * Math.PI * 2 + 0.4, q = 0.4 + pop.age * 5;
        sp.position.copy(pop.c.p).add(new THREE.Vector3(Math.cos(a) * q, 0.4 + Math.sin(pop.age * 6) * 0.6, Math.sin(a) * q)); sp.material.opacity = 1 - pop.age / 0.45;
      });
      // you
      const running = head >= TL.run0 && p01 < 1;
      this.player.visible = head >= 1.05 && visible; this.shadow.visible = this.player.visible && P.dist < 30 * K;
      this.player.position.copy(p).setY(0.08); this.shadow.position.copy(p).setY(0.07);
      this.player.material.map.offset.x = (p01 >= 1 ? 2 : running ? Math.floor(head * 9) % 2 : 0) / 3;
      this.player.material.opacity = head < 1.4 ? (Math.floor(head * 14) % 2 ? 1 : 0.35) : 1;
      const fp = this.fire.geometry.attributes.position;
      for (let k = 0; k < 12; k++) { const q = m.curve.getPointAt(clamp(uNow - (k + 1) * 0.11 / m.L, 0, 1)); fp.setXYZ(k, q.x, 0.2 + 0.05 * Math.sin(head * 30 + k), q.z); }
      fp.needsUpdate = true; this.fire.visible = running && visible && boost > 0.5; this.fire.material.opacity = boost;
      // the ghost
      const dG = clamp(tau / m.secPerM, 0, m.totalD);
      const ghostIn = !m.hideM || (dG >= m.hideM && dG <= m.totalD - m.hideM);
      this.ghost.visible = head >= TL.ready && ph !== "clear" && ph !== "score" && ghostIn;
      this.ghost.position.copy(m.curve.getPointAt(m.uAtD(clamp(dG, m.hideM, m.totalD - m.hideM)))).setY(0.08);
      this.ghost.material.map.offset.x = (running && dG < m.totalD ? Math.floor(head * 9 + 1) % 2 : 0) / 3;
      for (const ln of this.lines) ln.material.opacity = game;
      this.renderer.render(this.scene, cam);
      // what the HUD needs
      const proj = (o) => { const v = o.position.clone().project(cam); return { x: (v.x + 1) / 2, y: (1 - v.y) / 2, on: v.z < 1 && Math.abs(v.x) < 1.05 && Math.abs(v.y) < 1.05 }; };
      const fin1 = p01 >= 0.985;
      const done = p01 >= 1 ? m.cumS.length : m.cumS.filter((c) => tau >= c).length;
      return {
        head, ph, p01, tau: fin1 ? m.totalT : tau, d: fin1 ? m.totalD : dNow, units: done,
        pace: fin1 ? m.avgPace : m.paceAtD(dNow),
        hr: fin1 ? m.d.totals.avg_hr : m.atT(m.hrK, tau), cad: fin1 ? m.d.totals.avg_cadence : m.atT(m.cadK, tau),
        alt: m.atT(m.altK, tau), boost: running && visible && boost > 0.5,
        gap: tau - dNow * m.secPerM,
        player: this.player.visible && ph !== "score" ? proj(this.player) : null,
        ghost: this.ghost.visible && P.dist <= 30 * K ? proj(this.ghost) : null,
      };
    }
  }

  /* ================================================================ HUD, drawn on a 2D canvas over the 3D frame */
  let scanTile = null;
  function scanPattern(ctx, W) {
    const t = Math.max(1, Math.round(W / 360));
    if (!scanTile || scanTile.t !== t) {
      const c = document.createElement("canvas"); c.width = 1; c.height = t * 3; const x = c.getContext("2d");
      x.fillStyle = "rgba(5,6,16,0.2)"; x.fillRect(0, 0, 1, t); scanTile = { t, c };
    }
    return ctx.createPattern(scanTile.c, "repeat");
  }

  function txt(ctx, s, x, y, o) {
    ctx.font = `${o.bold ? "700 " : ""}${o.size.toFixed(1)}px ${o.font || PX}`;
    ctx.textAlign = o.align || "left"; ctx.textBaseline = "top";
    if ("letterSpacing" in ctx) ctx.letterSpacing = o.ls ? `${(o.size * o.ls).toFixed(2)}px` : "0px";
    if (o.shadow) { ctx.fillStyle = o.shadowColor || COL.ink; ctx.fillText(s, x + o.shadow, y + o.shadow); }
    ctx.fillStyle = o.color || "#fff"; ctx.fillText(s, x, y);
    return ctx.measureText(s).width;
  }
  function fitSize(ctx, s, size, maxW, font) { ctx.font = `${size}px ${font || PX}`; const w = ctx.measureText(s).width; return w > maxW ? size * maxW / w : size; }

  function coinIcon(ctx, x, y, s) {
    const px = s / 8, rows = ["..oooo..", ".oyyyyo.", "oywyycyo", "oywyycyo", "oyyyycyo", "oyyyycyo", ".oyyyyo.", "..oooo.."];
    const pal = { o: COL.ink, y: COL.gold, w: "#fff3b0", c: "#c98a00" };
    rows.forEach((r, j) => [...r].forEach((ch, i) => { if (ch !== ".") { ctx.fillStyle = pal[ch]; ctx.fillRect(x + i * px, y + j * px, Math.ceil(px), Math.ceil(px)); } }));
  }

  function drawHUD(ctx, W, H, fmt, m, st, title) {
    const u = W / 100, top = fmt === "916" ? 0.14 * H : 4.5 * u, bot = fmt === "916" ? 0.14 * H : 4.5 * u;
    const ph = st.ph, head = st.head;
    // scrims, scanlines, vignette
    const g1 = ctx.createLinearGradient(0, 0, 0, top + 34 * u);
    g1.addColorStop(0, "rgba(11,13,28,0.72)"); g1.addColorStop(Math.min(0.99, (top + 20 * u) / (top + 34 * u)), "rgba(11,13,28,0.55)"); g1.addColorStop(1, "rgba(11,13,28,0)");
    const g2 = ctx.createLinearGradient(0, H, 0, H - bot - 38 * u);
    g2.addColorStop(0, "rgba(11,13,28,0.78)"); g2.addColorStop(Math.min(0.99, (bot + 24 * u) / (bot + 38 * u)), "rgba(11,13,28,0.55)"); g2.addColorStop(1, "rgba(11,13,28,0)");
    ctx.save(); ctx.globalAlpha = ph === "sat" ? 0.45 : 1;
    ctx.fillStyle = g1; ctx.fillRect(0, 0, W, top + 34 * u); ctx.fillStyle = g2; ctx.fillRect(0, H - bot - 38 * u, W, bot + 38 * u);
    ctx.restore();
    if (ph !== "sat") {
      ctx.fillStyle = scanPattern(ctx, W); ctx.fillRect(0, 0, W, H);
      const v = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.hypot(W, H) / 2);
      v.addColorStop(0, "rgba(5,6,16,0)"); v.addColorStop(1, "rgba(5,6,16,0.5)"); ctx.fillStyle = v; ctx.fillRect(0, 0, W, H);
    }
    const sh = 0.5 * u, L = 5 * u, R = W - 5 * u;
    // credits always show on the video; wrap them onto two lines when one would be too long
    const cs = Math.max(6, 2 * u);
    ctx.font = `${cs}px ${PX2}`;
    if ("letterSpacing" in ctx) ctx.letterSpacing = `${(cs * 0.06).toFixed(2)}px`;
    const credit = ctx.measureText(st.credit).width <= R - L ? [st.credit] : st.credit.split(" · ");
    const creditH = credit.length * 2.6 * u;
    // labels over you and the ghost
    if (st.player && st.player.on && ph !== "score") txt(ctx, "1P", st.player.x * W, st.player.y * H - 0.104 * H, { size: 2.4 * u, color: COL.gold, align: "center", shadow: 0.4 * u });
    if (st.ghost && st.ghost.on && st.ghost.y < 0.67) txt(ctx, m.goal ? "GOAL" : "PACER", st.ghost.x * W, st.ghost.y * H + 0.024 * H, { size: 2.4 * u, color: COL.cyan, align: "center", shadow: 0.4 * u });
    // intro
    if (ph === "sat" || ph === "ready") {
      txt(ctx, "PLAYER 1", W / 2, top, { size: 2.5 * u, font: PX2, bold: true, color: COL.pink, align: "center", ls: 0.06 });
      const words = title.toUpperCase().split(/\s+/).filter(Boolean), lines = [];
      ctx.font = `${7.2 * u}px ${PX}`;
      for (const w of words) { const last = lines[lines.length - 1]; if (last && ctx.measureText(last + " " + w).width <= 88 * u) lines[lines.length - 1] = last + " " + w; else lines.push(w); }
      const shown = lines.slice(0, 2); if (lines.length > 2) shown[1] = shown[1] + "…";
      let y = top + 5.1 * u;
      for (const ln of shown) { const s = fitSize(ctx, ln, 7.2 * u, 90 * u); txt(ctx, ln, W / 2, y, { size: s, align: "center", shadow: 0.7 * u }); y += 9 * u; }
      txt(ctx, st.date, W / 2, y + 0.4 * u, { size: 2.5 * u, font: PX2, bold: true, color: COL.gold, align: "center", ls: 0.1 });
      if (ph === "sat") {
        const sc = 1 + 0.7 * (1 - smooth(0, 0.55, head)), half = 17 * u * sc, arm = 7 * u * sc, lw = 0.8 * u, cx = W / 2, cy = H / 2;
        ctx.fillStyle = "#fff";
        for (const [sx, sy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
          const x0 = cx + sx * half, y0 = cy + sy * half;
          ctx.fillRect(sx < 0 ? x0 : x0 - arm, sy < 0 ? y0 : y0 - lw, arm, lw);
          ctx.fillRect(sx < 0 ? x0 : x0 - lw, sy < 0 ? y0 : y0 - arm, lw, arm);
        }
        ctx.fillRect(cx - 2 * u, cy - lw / 2, 4 * u, lw); ctx.fillRect(cx - lw / 2, cy - 2 * u, lw, 4 * u);
      }
      const msg = head < 0.55 ? "ACQUIRING..." : ph === "sat" ? "LOCKED" : "READY?";
      if (blink(head, ph === "sat" ? 0.4 : 0.25)) txt(ctx, msg, W / 2, H - bot - 14.4 * u, { size: 4.4 * u, align: "center", color: ph === "sat" ? "#fff" : COL.gold, shadow: sh });
    }
    if (ph === "go") {
      const k = clamp((head - TL.ready) / 0.7, 0, 1), e = 1 - Math.pow(1 - k, 3), s = 16 * u * (2.4 - 1.4 * e);
      ctx.save(); ctx.globalAlpha = clamp(k / 0.35, 0, 1);
      txt(ctx, "GO!", W / 2, 0.4 * H - (s - 16 * u) / 2, { size: s, align: "center", color: COL.gold, shadow: 1.2 * u });
      ctx.restore();
    }
    // the game HUD
    if (ph === "go" || ph === "run" || ph === "clear") {
      const unit = m.unitLabel;
      txt(ctx, `1UP · ${m.metric ? "KM" : "MILES"}`, L, top, { size: 2.5 * u, font: PX2, bold: true, color: COL.pink, ls: 0.06 });
      txt(ctx, (st.d / m.unitM).toFixed(2).padStart(5, "0"), L, top + 4 * u, { size: 4.6 * u, shadow: sh });
      txt(ctx, "TIME", W / 2, top, { size: 2.5 * u, font: PX2, bold: true, color: COL.cyan, align: "center", ls: 0.06 });
      txt(ctx, fmtTime(st.tau), W / 2, top + 4 * u, { size: 4.6 * u, align: "center", shadow: sh });
      txt(ctx, m.goal ? "VS GOAL" : "VS EVEN", R, top, { size: 2.5 * u, font: PX2, bold: true, color: COL.cyan, align: "right", ls: 0.06 });
      const gap = st.gap, gs = Math.abs(gap) < 0.5 ? "0:00" : (gap > 0 ? "+" : "-") + fmtTime(Math.abs(gap));
      txt(ctx, gs, R, top + 4 * u, { size: 4.6 * u, align: "right", shadow: sh, color: gap <= -0.5 ? COL.good : gap >= 0.5 ? COL.bad : "#fff" });
      const y2 = top + 11.4 * u;
      coinIcon(ctx, L, y2 - 0.4 * u, 4.2 * u);
      txt(ctx, "x" + String(st.units).padStart(2, "0"), L + 5.6 * u, y2, { size: 3.4 * u, color: COL.gold, shadow: 0.45 * u });
      const combo = st.units ? m.combo[st.units - 1] : 0;
      for (let i = 0; i < 5; i++) { const x = R - (5 - i) * 3 * u + 0.8 * u; ctx.fillStyle = COL.ink; ctx.fillRect(x - 0.4 * u, y2 + 0.2 * u, 3 * u, 3 * u); ctx.fillStyle = i < combo ? COL.pink : "rgba(255,255,255,0.18)"; ctx.fillRect(x, y2 + 0.6 * u, 2.2 * u, 2.2 * u); }
      const cw = txt(ctx, "x" + combo, R - 15.4 * u, y2, { size: 3.4 * u, align: "right", shadow: 0.45 * u });
      txt(ctx, "COMBO", R - 16.8 * u - cw, y2 + 0.5 * u, { size: 2.5 * u, font: PX2, bold: true, color: COL.cyan, align: "right", ls: 0.06 });
      // last split call-out
      if (ph === "run" && st.units) {
        const i = st.units - 1, age = head - st.unitHead[i], dl = i ? m.splits[i] - m.splits[i - 1] : 0;
        const sc = 1 + 0.3 * Math.max(0, 1 - age / 0.22), flash = age < 0.18;
        const t1 = `${m.unitWord} ${String(st.units).padStart(2, "0")}`, t2 = fmtPace(m.splits[i]);
        const t3 = !i ? `FIRST ${m.unitWord}` : dl < 0 ? `FASTER ${fmtPace(-dl)}` : dl > 0 ? `SLOWER ${fmtPace(dl)}` : "SAME";
        ctx.font = `${5.4 * u}px ${PX}`; const bw = Math.max(ctx.measureText(t2).width, 26 * u) + 7.2 * u, bh = 15.2 * u;
        ctx.save(); ctx.translate(W / 2, top + 21 * u); ctx.scale(sc, sc);
        ctx.fillStyle = COL.gold; ctx.fillRect(-bw / 2 - 1.2 * u, -1.2 * u, bw + 2.4 * u, bh + 2.4 * u);
        ctx.fillStyle = COL.ink; ctx.fillRect(-bw / 2 - 0.6 * u, -0.6 * u, bw + 1.2 * u, bh + 1.2 * u);
        ctx.fillStyle = flash ? COL.gold : "rgba(11,13,28,0.86)"; ctx.fillRect(-bw / 2, 0, bw, bh);
        txt(ctx, t1, 0, 2 * u, { size: 2.5 * u, font: PX2, bold: true, color: flash ? COL.ink : COL.cyan, align: "center", ls: 0.06 });
        txt(ctx, t2, 0, 5.3 * u, { size: 5.4 * u, color: flash ? COL.ink : COL.gold, align: "center" });
        txt(ctx, t3, 0, 11.9 * u, { size: 2.5 * u, font: PX2, bold: true, color: flash ? COL.ink : !i ? "rgba(255,255,255,0.8)" : dl < 0 ? COL.good : COL.bad, align: "center", ls: 0.06 });
        ctx.restore();
      }
      // bottom: heart, pace, power meter, cadence, elevation
      const yMini = H - bot - 4.5 * u - creditH, yMeter = yMini - 6.8 * u, yVal = yMeter - 7 * u, yLab = yVal - 4 * u;
      const hr = st.hr ? Math.round(st.hr) : null, z = hr ? m.zoneOf(hr) : 0;
      const hw = txt(ctx, "HEART ", L, yLab, { size: 2.5 * u, font: PX2, bold: true, color: COL.cyan, ls: 0.06 });
      if (hr) txt(ctx, "Z" + (z + 1), L + hw, yLab, { size: 2.5 * u, font: PX2, bold: true, color: ZONE_CSS[z], ls: 0.06 });
      const hv = txt(ctx, hr ? String(hr) : "-", L, yVal, { size: 4.6 * u, shadow: sh });
      txt(ctx, "BPM", L + hv + 1.2 * u, yVal + 2.3 * u, { size: 2.3 * u, color: "rgba(255,255,255,0.72)" });
      const pw = txt(ctx, "/" + unit, R, yVal + 2.3 * u, { size: 2.3 * u, color: "rgba(255,255,255,0.72)", align: "right" });
      txt(ctx, fmtPace(st.pace), R - pw - 1.2 * u, yVal, { size: 4.6 * u, align: "right", shadow: sh });
      const pl = txt(ctx, "PACE", R, yLab, { size: 2.5 * u, font: PX2, bold: true, color: COL.cyan, align: "right", ls: 0.06 });
      if (st.boost && blink(head, 0.15)) txt(ctx, "BOOST!", R - pl - 1.6 * u, yLab, { size: 2.5 * u, font: PX2, bold: true, color: COL.pink, align: "right", ls: 0.06 });
      ctx.fillStyle = COL.ink; ctx.fillRect(L, yMeter, R - L, 4.4 * u);
      const segW = (R - L - 1.8 * u - 19 * 0.7 * u) / 20;
      for (let k = 0; k < 20; k++) {
        const frac = 0.5 + (k + 0.5) / 20 * 0.5, zone = ZONE_PCT.filter((pp) => frac >= pp).length;
        ctx.globalAlpha = hr && hr >= frac * m.hrMax ? 1 : 0.2; ctx.fillStyle = ZONE_CSS[zone];
        ctx.fillRect(L + 0.9 * u + k * (segW + 0.7 * u), yMeter + 0.9 * u, segW, 2.6 * u);
      }
      ctx.globalAlpha = 1;
      const cw2 = txt(ctx, "CADENCE ", L, yMini, { size: 2.5 * u, font: PX2, bold: true, color: "rgba(255,255,255,0.75)", ls: 0.06 });
      txt(ctx, st.cad ? String(Math.round(st.cad)) : "-", L + cw2, yMini, { size: 2.6 * u });
      const elev = st.alt == null ? "-" : String(Math.round(m.metric ? st.alt : st.alt * 3.28084));
      const ew = txt(ctx, m.metric ? " M" : " FT", R, yMini, { size: 2.5 * u, font: PX2, bold: true, color: "rgba(255,255,255,0.75)", align: "right", ls: 0.06 });
      const ev = txt(ctx, elev, R - ew, yMini, { size: 2.6 * u, align: "right" });
      txt(ctx, "ELEV ", R - ew - ev, yMini, { size: 2.5 * u, font: PX2, bold: true, color: "rgba(255,255,255,0.75)", align: "right", ls: 0.06 });
    }
    if (ph === "clear" && blink(head - TL.clear, 0.2)) {
      txt(ctx, "COURSE", W / 2, H / 2 - 12 * u, { size: 10 * u, align: "center", color: COL.gold, shadow: u });
      txt(ctx, "CLEAR!", W / 2, H / 2 + 0 * u, { size: 10 * u, align: "center", color: COL.gold, shadow: u });
    }
    if (ph === "score") drawScore(ctx, W, H, fmt, m, st, title);
    credit.forEach((line, i) => txt(ctx, line, L, H - bot - 2 * u - (credit.length - 1 - i) * 2.6 * u,
      { size: cs, font: PX2, color: "rgba(255,255,255,0.6)", ls: 0.06 }));
  }

  function drawScore(ctx, W, H, fmt, m, st, title) {
    const u = W / 100, k = clamp((st.head - TL.score) / 0.3, 0, 1);
    ctx.save(); ctx.globalAlpha = Math.round(k * 3) / 3;
    ctx.fillStyle = "rgba(11,13,28,0.62)"; ctx.fillRect(0, 0, W, H);
    const sq = fmt === "11", pw = (sq ? 76 : 84) * u, f = sq ? 0.85 : 1;
    const rows = [["TIME", fmtTime(m.totalT), ""], [`AVG PACE`, fmtPace(m.avgPace), "/" + m.unitLabel]];
    if (m.d.totals.avg_hr) rows.push(["AVG HEART", String(m.d.totals.avg_hr), "BPM"]);
    if (!sq && m.d.totals.avg_cadence) rows.push(["CADENCE", String(m.d.totals.avg_cadence), "SPM"]);
    if (!sq && m.gainM != null) rows.push(["CLIMB", "+" + Math.round(m.metric ? m.gainM : m.gainM * 3.28084), m.metric ? "M" : "FT"]);
    if (m.goal) rows.push([`VS GOAL ${fmtTime(m.goal.goal_s)}`, (m.totalT - m.goal.goal_s <= 0 ? "-" : "+") + fmtTime(Math.abs(m.totalT - m.goal.goal_s)), ""]);
    const best = m.best;
    const ph = 4.6 * u + 7.6 * u * f + rows.length * 5.5 * u * f + (best.length ? 5 * u * f + best.length * 6.2 * u * f : 0) + 7.4 * u * f + 4 * u;
    const x0 = (W - pw) / 2, y0 = (H - ph) / 2;
    ctx.fillStyle = COL.ink; ctx.fillRect(x0 - 2.4 * u, y0 - 2.4 * u, pw + 4.8 * u, ph + 4.8 * u);
    ctx.fillStyle = COL.gold; ctx.fillRect(x0 - 1.6 * u, y0 - 1.6 * u, pw + 3.2 * u, ph + 3.2 * u);
    ctx.fillStyle = COL.ink; ctx.fillRect(x0 - 0.8 * u, y0 - 0.8 * u, pw + 1.6 * u, ph + 1.6 * u);
    ctx.fillStyle = "rgba(11,13,28,0.94)"; ctx.fillRect(x0, y0, pw, ph);
    const L = x0 + 5 * u, R = x0 + pw - 5 * u; let y = y0 + 4.6 * u;
    const name = title.toUpperCase();
    txt(ctx, name, W / 2, y, { size: fitSize(ctx, name, 2.5 * u, pw - 10 * u, PX2), font: PX2, bold: true, color: COL.cyan, align: "center", ls: 0.06 });
    txt(ctx, "COURSE CLEAR", W / 2, y + 4.4 * u * f, { size: 5.6 * u * f, color: COL.gold, align: "center", shadow: 0.6 * u, shadowColor: "#3a2a00" });
    y += 7.6 * u * f + 4.4 * u;
    for (const [label, val, unit] of rows) {
      txt(ctx, label, L, y + 0.8 * u, { size: 2.6 * u * f, font: PX2, bold: true, color: COL.cyan, ls: 0.06 });
      const uw = unit ? txt(ctx, unit, R, y + 1.4 * u * f, { size: 2.1 * u * f, color: "rgba(255,255,255,0.7)", align: "right" }) + 1.2 * u : 0;
      txt(ctx, val, R - uw, y, { size: 3.6 * u * f, align: "right", color: label.startsWith("VS GOAL") ? (m.totalT <= m.goal.goal_s ? COL.good : COL.bad) : "#fff" });
      y += 5.5 * u * f;
    }
    if (best.length) {
      txt(ctx, `BEST ${m.metric ? "KM" : "MILES"}`, W / 2, y + 1 * u, { size: 2.6 * u * f, font: PX2, bold: true, color: COL.pink, align: "center", ls: 0.12 });
      y += 5 * u * f;
      best.forEach((b, i) => {
        const top = i === 0;
        if (top) { ctx.fillStyle = blink(st.head, 0.3) ? COL.gold : "#fff3b0"; ctx.fillRect(L - 1.4 * u, y - 1 * u, R - L + 2.8 * u, 5.4 * u * f); }
        txt(ctx, ["1ST", "2ND", "3RD"][i], L, y, { size: 3.4 * u * f, color: top ? COL.ink : COL.cyan });
        txt(ctx, `${m.unitWord} ${String(b.k).padStart(2, "0")}`, L + 13 * u, y, { size: 3.4 * u * f, color: top ? COL.ink : "#fff" });
        txt(ctx, fmtPace(b.s), R, y, { size: 3.4 * u * f, color: top ? COL.ink : "#fff", align: "right" });
        y += 6.2 * u * f;
      });
    }
    y += 1.2 * u;
    ctx.fillStyle = "rgba(255,255,255,0.25)"; for (let x = L; x < R; x += 2 * u) ctx.fillRect(x, y, u, 0.5 * u);
    y += 2.6 * u;
    txt(ctx, `COINS ${m.cumS.length}/${m.cumS.length}`, L, y, { size: 2.5 * u * f, font: PX2, bold: true, color: "rgba(255,255,255,0.8)", ls: 0.06 });
    txt(ctx, `MAX COMBO x${m.maxCombo}`, R, y, { size: 2.5 * u * f, font: PX2, bold: true, color: "rgba(255,255,255,0.8)", align: "right", ls: 0.06 });
    ctx.restore();
  }

  /* ================================================================ page */
  const $ = (s) => document.querySelector(s);
  const view = $("#view"), vctx = view.getContext("2d");
  const ui = { list: $("#runList"), search: $("#runSearch"), listMsg: $("#listMsg"), title: $("#runTitle"), sub: $("#runSub"),
    cover: $("#cover"), coverText: $("#coverText"), retry: $("#retryBtn"), play: $("#playBtn"), replay: $("#replayBtn"), scrub: $("#scrub"),
    clock: $("#clock"), video: $("#saveVideo"), still: $("#saveStill"), share: $("#shareBtn"), bar: $("#exportBar"), msg: $("#exportMsg"),
    credits: $("#credits"), frame: $("#frame") };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let stage = null, model = null, data = null, photo = null, runs = [], units = "imperial";
  let fmt = "916", ghostMode = "even", hideIdx = 1, head = 0, playing = false, last = performance.now(), exporting = false, sharedFile = null;
  const store = { get(k, d) { try { return localStorage.getItem("gd-recap-" + k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("gd-recap-" + k, v); } catch { /* private mode */ } } };
  fmt = SIZES[store.get("fmt", "916")] ? store.get("fmt", "916") : "916";
  hideIdx = Number(store.get("hide", "1")); if (![0, 1, 2].includes(hideIdx)) hideIdx = 1;
  const hideOpts = () => (units === "metric" ? [[0, "Off"], [200, "200 m"], [400, "400 m"]] : [[0, "Off"], [161, "0.1 mi"], [402, "0.25 mi"]]);

  function say(text, kind) { ui.msg.textContent = text || ""; ui.msg.className = "msg" + (kind ? " " + kind : ""); }
  function cover(text, retry) { ui.cover.hidden = !text; ui.coverText.textContent = text || ""; ui.retry.hidden = !retry; }
  function controls(on) { for (const b of [ui.play, ui.replay, ui.scrub, ui.video, ui.still]) b.disabled = !on || exporting; }
  function dateLabel(s) { if (!s) return ""; const d = new Date(s.replace(" ", "T")); return isNaN(d) ? s.slice(0, 10)
    : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" }); }
  function hudDate(s) { const d = new Date((s || "").replace(" ", "T")); return isNaN(d) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }).toUpperCase(); }
  function distLabel(m) { return units === "metric" ? `${(m / 1000).toFixed(2)} km` : `${(m / 1609.344).toFixed(2)} mi`; }

  function segButtons() {
    document.querySelectorAll("#fmtSeg [data-fmt]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.fmt === fmt)));
    document.querySelectorAll("#ghostSeg [data-ghost]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.ghost === ghostMode)));
    const hs = $("#hideSeg");
    if (hs.childElementCount !== 3) {
      hs.textContent = "";
      hideOpts().forEach(([, label], i) => { const b = document.createElement("button"); b.type = "button"; b.dataset.hide = String(i); b.textContent = label; hs.append(b); });
    } else hideOpts().forEach(([, label], i) => { hs.children[i].textContent = label; });
    hs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.hide) === hideIdx)));
    const goalBtn = document.querySelector('#ghostSeg [data-ghost="goal"]');
    goalBtn.disabled = !(data && data.goal);
    goalBtn.title = data && data.goal ? `Your goal: ${fmtTime(data.goal.goal_s)} for ${data.goal.name}` : "Set a goal time in Settings → General to race your goal pace";
    ui.frame.dataset.fmt = fmt;
  }

  /* ---------------------------------------------------------------- run list */
  function renderList() {
    const q = ui.search.value.trim().toLowerCase();
    ui.list.textContent = "";
    const shown = runs.filter((r) => !q || (r.name || "").toLowerCase().includes(q) || (r.start_local || "").includes(q) || dateLabel(r.start_local).toLowerCase().includes(q));
    for (const r of shown) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "run-item"; b.setAttribute("role", "option"); b.dataset.id = String(r.id);
      b.setAttribute("aria-selected", String(data && data.id === r.id));
      const t = document.createElement("span"); t.className = "ri-name"; t.textContent = r.name || "Run";
      const meta = document.createElement("span"); meta.className = "ri-meta";
      meta.textContent = [dateLabel(r.start_local), distLabel(r.distance_m || 0), fmtTime(r.moving_s || r.duration_s || 0)].filter(Boolean).join(" · ");
      b.append(t, meta);
      if (r.ready) { const k = document.createElement("span"); k.className = "ri-ready"; k.textContent = "Ready"; b.append(k); }
      b.addEventListener("click", () => choose(r.id));
      ui.list.append(b);
    }
    ui.listMsg.textContent = runs.length ? (shown.length ? "" : "No runs match.") : "No outdoor runs yet. They appear here after your next sync.";
  }

  async function loadRuns() {
    try {
      const r = await api("/api/recap/runs");
      runs = r.runs; units = r.units; renderList(); segButtons();
      const want = Number(new URLSearchParams(location.hash.slice(1)).get("run"));
      if (want && runs.some((x) => x.id === want)) choose(want);
    } catch (e) { ui.listMsg.textContent = e.message; }
  }

  /* ---------------------------------------------------------------- choosing a run */
  let chooseSeq = 0;
  async function choose(id, retry = false) {
    if (exporting) return;
    const seq = ++chooseSeq;
    history.replaceState(null, "", "#run=" + id);
    const run = runs.find((r) => r.id === id);
    ui.title.textContent = run ? run.name || "Run" : "Run";
    ui.sub.textContent = run ? `${dateLabel(run.start_local)} · ${distLabel(run.distance_m || 0)}` : "";
    data = null; model = null; setPlaying(false); controls(false); say("");
    ui.list.querySelectorAll(".run-item").forEach((b) => b.setAttribute("aria-selected", String(Number(b.dataset.id) === id)));
    cover("Getting this run ready…");
    try {
      let r = await api(`/api/recap/${id}`);
      if (!r.prepared || retry) {
        const st = await api(`/api/recap/${id}/prepare`, { method: "POST", body: { retry } });
        let state = st;
        while (state.state === "running") {
          if (seq !== chooseSeq) return;
          cover((state.step || "Working") + "…");
          await new Promise((res) => setTimeout(res, 900));
          state = await api(`/api/recap/${id}/status`);
        }
        if (state.state === "error") throw new Error(state.error || "Couldn't prepare this run.");
        r = await api(`/api/recap/${id}`);
        if (!r.prepared) throw new Error("Couldn't prepare this run.");
        if (run) { run.ready = true; renderList(); }
      }
      if (seq !== chooseSeq) return;
      data = r.recap;
      photo = null;
      if (data.imagery.available) {
        cover("Loading the aerial photo…");
        const resp = await fetch(`/api/recap/${id}/photo`, { credentials: "same-origin" });
        if (resp.ok) photo = await createImageBitmap(await resp.blob());
      }
      if (seq !== chooseSeq) return;
      if (data.goal && Math.abs(data.totals.distance_m / 1000 - data.goal.km) / data.goal.km < 0.15) ghostMode = "goal";
      else ghostMode = "even";
      cover("Building the scene…");
      await new Promise((res) => setTimeout(res, 30));
      rebuild();
      const notes = [];
      if (!data.imagery.available) notes.push(data.imagery.reason ? `No aerial photo: ${data.imagery.reason}` : "No aerial photo for this run.");
      if (!data.buildings.length && data.buildings_reason) notes.push(`No buildings: ${data.buildings_reason}`);
      if (notes.length) { say(notes.join(" "), ""); ui.retry.hidden = true; }
      showRetryExtras(notes.length > 0);
    } catch (e) {
      if (seq !== chooseSeq) return;
      cover(e.message, true);
      ui.retry.onclick = () => choose(id, true);
    }
  }

  function showRetryExtras(show) {
    let b = document.getElementById("retryExtras");
    if (!show) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement("button"); b.id = "retryExtras"; b.type = "button"; b.className = "link-btn"; b.textContent = "Try downloading them again"; ui.msg.after(b); }
    b.onclick = () => { b.remove(); choose(data.id, true); };
  }

  function rebuild() {
    if (!data) return;
    try {
      if (!stage) stage = new Stage();
    } catch (e) { cover("This browser can't show 3D graphics (WebGL is off or unsupported).", false); return; }
    model = buildModel(data, { hideM: hideOpts()[hideIdx][0], ghost: ghostMode });
    stage.load(model, photo);
    const credits = [data.imagery.available ? data.imagery.credit : "No aerial photo", data.buildings_credit].filter(Boolean);
    ui.credits.textContent = credits.join(" · ") + ". The video shows these credits in its corner.";
    stage.credit = credits.join(" · ").toUpperCase();
    segButtons(); cover(""); controls(true);
    head = reduce ? CLIP_S - 0.01 : 0; setPlaying(!reduce);
    paint();
  }

  /* ---------------------------------------------------------------- drawing */
  function hudState(st) {
    st.date = hudDate(data.start_local); st.credit = stage.credit;
    st.unitHead = model.cumS.map((t) => stage.headOfP(t / model.totalT));
    return st;
  }
  function frameTo(ctx, W, H, h) {
    stage.setSize(W, H);
    const st = hudState(stage.render(h));
    ctx.drawImage(stage.renderer.domElement, 0, 0, W, H);
    drawHUD(ctx, W, H, fmt, model, st, data.name || "Run");
  }
  function previewSize() {
    const r = view.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    const [EW, EH] = SIZES[fmt];
    const w = Math.max(2, Math.min(EW, Math.round(r.width * dpr)));
    return [w, Math.round(w * EH / EW)];
  }
  function paint() {
    if (!model || exporting) return;
    const [w, h] = previewSize();
    if (view.width !== w || view.height !== h) { view.width = w; view.height = h; }
    frameTo(vctx, w, h, head);
  }
  function setPlaying(p) {
    playing = p; $("#icoPause").hidden = !p; $("#icoPlay").hidden = p; ui.play.setAttribute("aria-label", p ? "Pause" : "Play");
  }
  function loop(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (model && !exporting) {
      if (playing) { head += dt; if (head >= CLIP_S) head = 0; }
      ui.scrub.value = String(Math.round(head / CLIP_S * 1000));
      ui.clock.textContent = `0:${String(Math.floor(head)).padStart(2, "0")} / 0:${CLIP_S}`;
      if (playing || paint.dirty) { paint(); paint.dirty = false; }
    }
    requestAnimationFrame(loop);
  }

  /* ---------------------------------------------------------------- saving */
  function fileName(ext) { return `recap-${(data.start_local || "run").slice(0, 10)}-${fmt === "916" ? "9x16" : fmt === "45" ? "4x5" : "1x1"}.${ext}`; }
  function offer(blob, name) {
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    sharedFile = new File([blob], name, { type: blob.type });
    ui.share.hidden = !(navigator.canShare && navigator.canShare({ files: [sharedFile] }));
  }
  async function saveStill() {
    const [W, H] = SIZES[fmt], c = document.createElement("canvas"); c.width = W; c.height = H;
    frameTo(c.getContext("2d"), W, H, head);
    const blob = await new Promise((res) => c.toBlob(res, "image/png"));
    if (!blob) throw new Error("Couldn't make the picture.");
    offer(blob, fileName("png"));
    paint.dirty = true;
  }
  async function saveVideo() {
    if (!("VideoEncoder" in window)) throw new Error("This browser can't make videos. Use a recent Chrome, Edge or Safari.");
    const M = await import("/static/vendor/mediabunny.min.mjs");
    const [W, H] = SIZES[fmt];
    if (!(await M.canEncodeVideo("avc", { width: W, height: H }))) throw new Error("This browser can't make MP4 video (H.264). Use a recent Chrome, Edge or Safari.");
    const c = document.createElement("canvas"); c.width = W; c.height = H; const ctx = c.getContext("2d");
    const output = new M.Output({ format: new M.Mp4OutputFormat({ fastStart: "in-memory" }), target: new M.BufferTarget() });
    const src = new M.CanvasSource(c, { codec: "avc", bitrate: M.QUALITY_HIGH, keyFrameInterval: 2 });
    output.addVideoTrack(src, { frameRate: FPS });
    await output.start();
    const frames = CLIP_S * FPS;
    ui.bar.hidden = false; ui.bar.value = 0;
    for (let i = 0; i < frames; i++) {
      frameTo(ctx, W, H, i / FPS);
      await src.add(i / FPS, 1 / FPS);
      if (i % 6 === 0) { ui.bar.value = i / frames; say(`Rendering ${Math.round(i / frames * 100)}%`); await new Promise((r) => setTimeout(r, 0)); }
    }
    say("Finishing…");
    await output.finalize();
    offer(new Blob([output.target.buffer], { type: "video/mp4" }), fileName("mp4"));
  }
  async function exportWith(fn, doneMsg) {
    if (exporting || !model) return;
    exporting = true; controls(false); setPlaying(false); ui.share.hidden = true;
    try { await fn(); say(doneMsg, "ok"); }
    catch (e) { say(e && e.message ? e.message : "Couldn't save the file.", "err"); }
    finally { exporting = false; ui.bar.hidden = true; controls(true); paint.dirty = true; }
  }

  /* ---------------------------------------------------------------- wiring */
  ui.search.addEventListener("input", renderList);
  ui.play.addEventListener("click", () => { if (!playing && head >= CLIP_S - 0.02) head = 0; setPlaying(!playing); });
  ui.replay.addEventListener("click", () => { head = 0; setPlaying(true); });
  ui.scrub.addEventListener("input", () => { head = ui.scrub.value / 1000 * CLIP_S; setPlaying(false); paint.dirty = true; });
  document.querySelectorAll("#fmtSeg [data-fmt]").forEach((b) => b.addEventListener("click", () => { if (exporting) return; fmt = b.dataset.fmt; store.set("fmt", fmt); segButtons(); paint.dirty = true; }));
  document.querySelectorAll("#ghostSeg [data-ghost]").forEach((b) => b.addEventListener("click", () => { if (exporting || b.disabled) return; ghostMode = b.dataset.ghost; segButtons(); if (data) { rebuild(); } }));
  $("#hideSeg").addEventListener("click", (e) => { const b = e.target.closest("[data-hide]"); if (!b || exporting) return; hideIdx = Number(b.dataset.hide); store.set("hide", String(hideIdx)); segButtons(); if (data) rebuild(); });
  ui.video.addEventListener("click", () => exportWith(saveVideo, "Video saved. On a phone, use Share to post it."));
  ui.still.addEventListener("click", () => exportWith(saveStill, "Picture saved."));
  ui.share.addEventListener("click", async () => { try { await navigator.share({ files: [sharedFile] }); } catch { /* cancelled */ } });
  new ResizeObserver(() => { paint.dirty = true; }).observe(view);

  segButtons();
  document.fonts.load(`16px ${PX}`).then(() => document.fonts.load(`700 16px ${PX2}`)).catch(() => {}).finally(() => { paint.dirty = true; });
  loadRuns();
  requestAnimationFrame(loop);
})();
