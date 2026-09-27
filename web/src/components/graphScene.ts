/**
 * The imperative side of the 3D knowledge graph (three.js), kept apart from the React shell.
 *
 * One scene: instanced spheres for the nodes, additive point sprites for their glow, edge lines,
 * particles flowing along the edges, a starfield and a faint nebula. A small force simulation
 * (edge springs, local repulsion on a spatial hash, a soft anchor to the layout, damping) runs
 * only while something moves and sleeps otherwise. Everything per frame works on preallocated
 * typed arrays, so a few thousand nodes stay smooth. Rendering pauses when the tab is hidden or
 * the graph scrolls out of view.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import type { KGraph, KNode } from "../knowledgeApi";

export type Toggle = "autoRotate" | "physics" | "particles";

export type SceneOptions = {
  stage: HTMLDivElement;
  tooltip: HTMLDivElement;
  fps: HTMLDivElement;
  data: KGraph;
  highlight: string[];
  labelCollections: number;
  period: number;
  distance: number;
  reduced: boolean;
  initial: Record<Toggle, boolean>;
  /** Short texts for the tooltip and the toasts (the shell owns the i18n). */
  text: {
    kind: (type: KNode["type"]) => string;
    docs: (n: number) => string;
    open: string;
    focus: string;
    pinned: string;
    unpinned: string;
    gravityOn: string;
    gravityOff: string;
    fireworks: string;
  };
  onToast: (message: string) => void;
  /** The scene switched a toggle on by itself (shake, gravity and fireworks need physics). */
  onToggle: (toggle: Toggle, on: boolean) => void;
};

export type SceneHandle = {
  set: (toggle: Toggle, on: boolean) => void;
  resetView: () => void;
  shake: () => void;
  dispose: () => void;
};

// Small deterministic PRNG so the layout is stable between renders.
function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const REST: Record<string, number> = { workspace: 0.9, source: 0.7, collection: 0.45, document: 0.22 };

/** A small 3D force layout: linked nodes pull together, all nodes push apart (typed arrays, no per-step allocation). */
function layout(data: KGraph) {
  const rnd = mulberry32(7);
  const index = new Map(data.nodes.map((n, i) => [n.id, i]));
  const n = data.nodes.length;
  const P = new Float32Array(n * 3);
  for (let i = 0; i < n * 3; i++) P[i] = (rnd() - 0.5) * 1.6;
  const pairs: number[] = [];
  for (const e of data.edges) {
    const a = index.get(e.source);
    const b = index.get(e.target);
    if (a !== undefined && b !== undefined && a !== b) pairs.push(a, b);
  }
  const links = Uint32Array.from(pairs);
  const m = links.length / 2;
  const F = new Float32Array(n * 3);
  const repel = 0.02 * Math.sqrt(60 / Math.max(n, 1));
  const iterations = Math.round(Math.min(220, Math.max(30, 6e7 / Math.max(n * n, 1)))); // O(n²) per step: fewer steps for a big graph
  for (let it = 0; it < iterations; it++) {
    const cool = 1 - it / (iterations + 20);
    F.fill(0);
    for (let i = 0; i < n; i++) {
      const ix = i * 3;
      for (let j = i + 1; j < n; j++) {
        const jx = j * 3;
        const dx = P[ix] - P[jx], dy = P[ix + 1] - P[jx + 1], dz = P[ix + 2] - P[jx + 2];
        const k = repel / Math.max(dx * dx + dy * dy + dz * dz, 0.0025);
        F[ix] += dx * k; F[ix + 1] += dy * k; F[ix + 2] += dz * k;
        F[jx] -= dx * k; F[jx + 1] -= dy * k; F[jx + 2] -= dz * k;
      }
    }
    for (let e = 0; e < m; e++) {
      const a = links[e * 2] * 3, b = links[e * 2 + 1] * 3;
      const dx = P[b] - P[a], dy = P[b + 1] - P[a + 1], dz = P[b + 2] - P[a + 2];
      const len = Math.hypot(dx, dy, dz) || 1e-3;
      const k = ((len - REST[data.nodes[links[e * 2 + 1]].type]) / len) * 0.08;
      F[a] += dx * k; F[a + 1] += dy * k; F[a + 2] += dz * k;
      F[b] -= dx * k; F[b + 1] -= dy * k; F[b + 2] -= dz * k;
    }
    for (let i = 0; i < n * 3; i += 3) {
      let fx = F[i] - P[i] * 0.004, fy = F[i + 1] - P[i + 1] * 0.004, fz = F[i + 2] - P[i + 2] * 0.004;
      const l = Math.hypot(fx, fy, fz);
      if (l > 0.08) { const s = 0.08 / l; fx *= s; fy *= s; fz *= s; }
      P[i] += fx * cool; P[i + 1] += fy * cool; P[i + 2] += fz * cool;
    }
  }
  // Centre and fit into a unit ball (by the 95th percentile, so one outlier does not shrink the rest).
  let cx = 0, cy = 0, cz = 0;
  for (let i = 0; i < n * 3; i += 3) { cx += P[i]; cy += P[i + 1]; cz += P[i + 2]; }
  cx /= Math.max(n, 1); cy /= Math.max(n, 1); cz /= Math.max(n, 1);
  const radii: number[] = [];
  for (let i = 0; i < n * 3; i += 3) {
    P[i] -= cx; P[i + 1] -= cy; P[i + 2] -= cz;
    radii.push(Math.hypot(P[i], P[i + 1], P[i + 2]));
  }
  radii.sort((a, b) => a - b);
  const s = 1.05 / (radii[Math.floor(radii.length * 0.95)] || 1);
  for (let i = 0; i < n * 3; i++) P[i] *= s;
  return { P, links };
}

/** Radius by how much a node holds: sqrt of its document count, so a big channel stands out without swamping the rest. */
function sizer(nodes: KNode[]) {
  const max = Math.max(1, ...nodes.map((n) => n.count ?? 0));
  return (node: KNode) => 0.008 + 0.042 * Math.sqrt((node.count ?? 0) / max);
}

/** Point sprites with a per-point size (world units), colour and alpha; soft round falloff; optional twinkle. */
function pointsMaterial(blending: THREE.Blending, sharp: number, twinkle: number) {
  return new THREE.ShaderMaterial({
    uniforms: { uScale: { value: 500 }, uTime: { value: 0 }, uTwinkle: { value: twinkle }, uSharp: { value: sharp } },
    vertexShader: /* glsl */ `
      attribute float aSize;
      attribute float aAlpha;
      attribute vec3 aColor;
      uniform float uScale;
      uniform float uTime;
      uniform float uTwinkle;
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = max(aSize * uScale / -mv.z, 1.0);
        gl_Position = projectionMatrix * mv;
        float phase = fract(sin(dot(position, vec3(12.9898, 78.233, 37.719))) * 43758.5453) * 6.2831;
        vAlpha = aAlpha * (1.0 - uTwinkle * 0.5 * (1.0 + sin(uTime * (1.3 + phase * 0.2) + phase)));
        vColor = aColor;
      }`,
    fragmentShader: /* glsl */ `
      uniform float uSharp;
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        float d = length(gl_PointCoord - 0.5) * 2.0;
        if (d > 1.0) discard;
        float a = pow(1.0 - d, uSharp) * vAlpha;
        gl_FragColor = vec4(vColor, a);
        #include <colorspace_fragment>
      }`,
    transparent: true,
    depthWrite: false,
    blending,
  });
}

function softTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.35)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const easeInOut = (k: number) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const easeOut = (k: number) => 1 - Math.pow(1 - k, 3);
const KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];

export function createGraphScene(o: SceneOptions): SceneHandle | null {
  const { stage, tooltip, data, distance, reduced, text } = o;
  const n = data.nodes.length;
  if (n === 0) return null;

  let renderer: THREE.WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch {
    return null;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const canvas = renderer.domElement;
  canvas.style.cssText = "display:block;width:100%;height:100%;touch-action:none";
  stage.appendChild(canvas);

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.style.cssText = "position:absolute;inset:0;pointer-events:none";
  stage.appendChild(labelRenderer.domElement);

  // Colours from the theme tokens (dark by default; a light surface switches off the additive tricks).
  const css = getComputedStyle(stage);
  const token = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  const accentCss = token("--color-accent", "#6cc4dc");
  const INK = new THREE.Color(token("--color-ink", "#e6e8eb"));
  const DIM = new THREE.Color(token("--color-dim", "#4a515b"));
  const ACCENT = new THREE.Color(accentCss);
  const BG = new THREE.Color(token("--color-surface", "#111418"));
  const light = BG.getHSL({ h: 0, s: 0, l: 0 }).l > 0.5;
  const glowBlend = light ? THREE.NormalBlending : THREE.AdditiveBlending;
  const WHITE = new THREE.Color(light ? 0x000000 : 0xffffff);

  const scene = new THREE.Scene();
  const fog = new THREE.Fog(BG, distance - 0.8, distance + 1.2);
  scene.fog = fog;
  const camera = new THREE.PerspectiveCamera(38, 1, 0.02, 60);
  camera.position.set(0, 0.16 * distance, distance);

  // ---- data ------------------------------------------------------------------------------
  const { P: HOME0, links } = layout(data);
  const m = links.length / 2;
  const P = HOME0.slice(); // live positions (shared with the glow points' buffer)
  const H = HOME0.slice(); // where each node wants to be
  const V = new Float32Array(n * 3);
  const F = new Float32Array(n * 3);
  const pinned = new Uint8Array(n);
  const radius = new Float32Array(n);
  const mass = new Float32Array(n);
  const rest = new Float32Array(m);
  const size = sizer(data.nodes);
  const hi = new Set(o.highlight);
  const isHi = new Uint8Array(n);
  const base = new Float32Array(n * 3);
  const deg = new Uint32Array(n);
  for (let e = 0; e < m; e++) { deg[links[e * 2]]++; deg[links[e * 2 + 1]]++; }
  // Adjacency (CSR): neighbour node and edge id per entry.
  const adjStart = new Uint32Array(n + 1);
  for (let i = 0; i < n; i++) adjStart[i + 1] = adjStart[i] + deg[i];
  const adjNode = new Uint32Array(m * 2);
  const adjEdge = new Uint32Array(m * 2);
  const fill = adjStart.slice(0, n);
  for (let e = 0; e < m; e++) {
    const a = links[e * 2], b = links[e * 2 + 1];
    adjNode[fill[a]] = b; adjEdge[fill[a]++] = e;
    adjNode[fill[b]] = a; adjEdge[fill[b]++] = e;
  }
  const tmpC = new THREE.Color();
  data.nodes.forEach((node, i) => {
    isHi[i] = hi.has(node.id) ? 1 : 0;
    radius[i] = isHi[i] ? Math.max(size(node), 0.02) : size(node);
    mass[i] = 1 + deg[i] * 0.5;
    const c = isHi[i] || node.type === "workspace" ? ACCENT : node.type === "document" ? DIM : INK;
    base[i * 3] = c.r; base[i * 3 + 1] = c.g; base[i * 3 + 2] = c.b;
  });
  for (let e = 0; e < m; e++) {
    const a = links[e * 2] * 3, b = links[e * 2 + 1] * 3;
    rest[e] = Math.hypot(P[b] - P[a], P[b + 1] - P[a + 1], P[b + 2] - P[a + 2]);
  }

  const group = new THREE.Group();
  scene.add(group);

  // ---- nodes: one instanced mesh ----------------------------------------------------------
  const sphere = new THREE.SphereGeometry(1, 16, 12);
  // Lambert with a light that rides on the camera: the spheres read as balls, not flat discs, up close.
  const nodeMat = new THREE.MeshLambertMaterial({ color: 0xffffff });
  scene.add(new THREE.AmbientLight(0xffffff, 1.9));
  const headlight = new THREE.DirectionalLight(0xffffff, 1.5);
  headlight.position.set(-0.4, 0.8, 1);
  camera.add(headlight);
  scene.add(camera);
  const nodes = new THREE.InstancedMesh(sphere, nodeMat, n);
  nodes.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  nodes.setColorAt(0, tmpC.set(0xffffff));
  const nodeColor = nodes.instanceColor!.array as Float32Array;
  nodes.frustumCulled = false; // instances move; the pick below does its own hit test
  group.add(nodes);

  // ---- glow: additive point sprites sharing the position buffer ---------------------------
  const glowGeo = new THREE.BufferGeometry();
  const glowPos = new THREE.BufferAttribute(P, 3).setUsage(THREE.DynamicDrawUsage);
  const glowSize = new Float32Array(n);
  const glowAlpha = new Float32Array(n);
  const glowColor = new Float32Array(n * 3);
  glowGeo.setAttribute("position", glowPos);
  glowGeo.setAttribute("aSize", new THREE.BufferAttribute(glowSize, 1).setUsage(THREE.DynamicDrawUsage));
  glowGeo.setAttribute("aAlpha", new THREE.BufferAttribute(glowAlpha, 1).setUsage(THREE.DynamicDrawUsage));
  glowGeo.setAttribute("aColor", new THREE.BufferAttribute(glowColor, 3).setUsage(THREE.DynamicDrawUsage));
  const glowMat = pointsMaterial(glowBlend, 1.6, 0);
  const glow = new THREE.Points(glowGeo, glowMat);
  glow.frustumCulled = false;
  glow.renderOrder = 2;
  group.add(glow);

  // ---- edges -------------------------------------------------------------------------------
  const edgePos = new Float32Array(m * 6);
  const edgeCol = new Float32Array(m * 6);
  const edgeGeo = new THREE.BufferGeometry();
  const edgePosAttr = new THREE.BufferAttribute(edgePos, 3).setUsage(THREE.DynamicDrawUsage);
  const edgeColAttr = new THREE.BufferAttribute(edgeCol, 3).setUsage(THREE.DynamicDrawUsage);
  edgeGeo.setAttribute("position", edgePosAttr);
  edgeGeo.setAttribute("color", edgeColAttr);
  const edges = new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }));
  edges.frustumCulled = false;
  group.add(edges);
  const edgeHi = new Uint8Array(m);
  for (let e = 0; e < m; e++) edgeHi[e] = isHi[links[e * 2]] || isHi[links[e * 2 + 1]] ? 1 : 0;

  // ---- particles flowing along the edges, toward the bigger node ---------------------------
  const K = Math.min(4000, Math.max(m * 2, 0));
  const flowFrom = new Uint32Array(m);
  const flowTo = new Uint32Array(m);
  for (let e = 0; e < m; e++) {
    const a = links[e * 2], b = links[e * 2 + 1];
    const toB = (data.nodes[b].count ?? 0) >= (data.nodes[a].count ?? 0);
    flowFrom[e] = toB ? a : b;
    flowTo[e] = toB ? b : a;
  }
  const rndP = mulberry32(11);
  const pEdge = new Uint32Array(K);
  const pT = new Float32Array(K);
  const pSpeed = new Float32Array(K);
  const pPos = new Float32Array(K * 3);
  const pAlpha = new Float32Array(K);
  const pSize = new Float32Array(K);
  const pColor = new Float32Array(K * 3);
  const edgeFlow = new Float32Array(m).fill(1); // per-edge particle brightness (hover focus)
  for (let k = 0; k < K; k++) {
    pEdge[k] = Math.floor(rndP() * m);
    pT[k] = rndP();
    pSpeed[k] = 0.18 + rndP() * 0.35;
    pSize[k] = 0.016 + rndP() * 0.016;
    tmpC.copy(ACCENT).lerp(WHITE, rndP() * 0.45);
    pColor[k * 3] = tmpC.r; pColor[k * 3 + 1] = tmpC.g; pColor[k * 3 + 2] = tmpC.b;
  }
  const partGeo = new THREE.BufferGeometry();
  const partPosAttr = new THREE.BufferAttribute(pPos, 3).setUsage(THREE.DynamicDrawUsage);
  const partAlphaAttr = new THREE.BufferAttribute(pAlpha, 1).setUsage(THREE.DynamicDrawUsage);
  partGeo.setAttribute("position", partPosAttr);
  partGeo.setAttribute("aAlpha", partAlphaAttr);
  partGeo.setAttribute("aSize", new THREE.BufferAttribute(pSize, 1));
  partGeo.setAttribute("aColor", new THREE.BufferAttribute(pColor, 3));
  const partMat = pointsMaterial(glowBlend, 1.2, 0);
  const particles = new THREE.Points(partGeo, partMat);
  particles.frustumCulled = false;
  particles.renderOrder = 3;
  group.add(particles);

  // ---- starfield + nebula (dark theme only) -------------------------------------------------
  const sky = new THREE.Group();
  scene.add(sky);
  const disposables: { dispose: () => void }[] = [];
  let starMat: THREE.ShaderMaterial | null = null;
  if (!light) {
    const S = 1600;
    const rs = mulberry32(3);
    const sPos = new Float32Array(S * 3), sSize = new Float32Array(S), sAlpha = new Float32Array(S), sCol = new Float32Array(S * 3);
    const cool = new THREE.Color(0xbfd8ff), warm = new THREE.Color(0xffe2c4);
    for (let i = 0; i < S; i++) {
      const u = rs() * 2 - 1, th = rs() * Math.PI * 2, r = 14 + rs() * 12, q = Math.sqrt(1 - u * u);
      sPos[i * 3] = r * q * Math.cos(th); sPos[i * 3 + 1] = r * u; sPos[i * 3 + 2] = r * q * Math.sin(th);
      sSize[i] = 0.05 + Math.pow(rs(), 3) * 0.2;
      sAlpha[i] = 0.35 + rs() * 0.55;
      tmpC.copy(rs() < 0.5 ? cool : warm).lerp(WHITE, rs() * 0.6);
      sCol[i * 3] = tmpC.r; sCol[i * 3 + 1] = tmpC.g; sCol[i * 3 + 2] = tmpC.b;
    }
    const sGeo = new THREE.BufferGeometry();
    sGeo.setAttribute("position", new THREE.BufferAttribute(sPos, 3));
    sGeo.setAttribute("aSize", new THREE.BufferAttribute(sSize, 1));
    sGeo.setAttribute("aAlpha", new THREE.BufferAttribute(sAlpha, 1));
    sGeo.setAttribute("aColor", new THREE.BufferAttribute(sCol, 3));
    starMat = pointsMaterial(THREE.AdditiveBlending, 2.5, reduced ? 0 : 0.6);
    sky.add(new THREE.Points(sGeo, starMat));
    const tex = softTexture();
    disposables.push(tex);
    const clouds: [number, number, number, number, number][] = [
      [0x6cc4dc, -9, 4, -14, 20],
      [0x7b5cff, 10, -3, -12, 22],
      [0xc05cff, 3, 9, 13, 16],
      [0x2f8f9d, -12, -7, 8, 18],
    ];
    for (const [color, x, y, z, s] of clouds) {
      const mat = new THREE.SpriteMaterial({ map: tex, color, transparent: true, opacity: 0.09, depthWrite: false, blending: THREE.AdditiveBlending, fog: false });
      const sprite = new THREE.Sprite(mat);
      sprite.position.set(x, y, z);
      sprite.scale.setScalar(s);
      sky.add(sprite);
    }
  }

  // ---- ripples / shockwaves ---------------------------------------------------------------
  const ringGeo = new THREE.RingGeometry(0.95, 1, 64);
  const ripples = Array.from({ length: 10 }, () => {
    const mesh = new THREE.Mesh(ringGeo, new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0, depthWrite: false, blending: glowBlend, fog: false, side: THREE.DoubleSide }));
    mesh.visible = false;
    group.add(mesh);
    return { mesh, age: 1, life: 0.9, grow: 0.5 };
  });
  let nextRipple = 0;
  const ripple = (x: number, y: number, z: number, color: THREE.Color = ACCENT, grow = 0.5) => {
    const r = ripples[nextRipple++ % ripples.length];
    r.mesh.position.set(x, y, z);
    (r.mesh.material as THREE.MeshBasicMaterial).color.copy(color);
    r.age = 0;
    r.grow = grow;
    r.mesh.visible = true;
  };

  // ---- pin markers --------------------------------------------------------------------------
  const pinGeo = new THREE.RingGeometry(1.35, 1.6, 40);
  const pinMat = new THREE.MeshBasicMaterial({ color: light ? 0x000000 : 0xffffff, transparent: true, opacity: 0.85, depthWrite: false, fog: false, side: THREE.DoubleSide });
  const pinMarks = new Map<number, THREE.Mesh>();

  // ---- labels ---------------------------------------------------------------------------------
  const bigCollections = new Set(
    data.nodes
      .filter((x) => x.type === "collection")
      .sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
      .slice(0, o.labelCollections)
      .map((x) => x.id),
  );
  const labels: [number, CSS2DObject][] = [];
  data.nodes.forEach((node, i) => {
    if (!(node.type === "workspace" || node.type === "source" || bigCollections.has(node.id) || isHi[i])) return;
    // CSS2DRenderer owns the outer element's transform, so offset the text inside it.
    const tag = document.createElement("div");
    const span = document.createElement("span");
    const name = node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label;
    span.textContent = node.type === "document" ? name : `${name} · ${text.docs(node.count ?? 0)}`;
    const strong = node.type === "workspace" || isHi[i];
    span.style.cssText = `display:block;margin-left:10px;font:${node.type === "workspace" ? "13px" : "12px"} "IBM Plex Mono", monospace;color:${strong ? accentCss : "var(--color-ink-2, #a3a9b3)"};white-space:nowrap;transition:opacity .25s`;
    tag.appendChild(span);
    const obj = new CSS2DObject(tag);
    obj.center.set(0, 0.5);
    obj.position.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
    group.add(obj);
    labels.push([i, obj]);
  });

  // ---- controls -------------------------------------------------------------------------------
  const controls = new OrbitControls(camera, canvas);
  controls.enablePan = false;
  controls.enableDamping = true;
  controls.minDistance = 0.3;
  controls.maxDistance = 7;
  controls.autoRotateSpeed = 60 / o.period; // OrbitControls: 2.0 = one turn per 30 s
  const state = { ...o.initial };

  // ---- colours by hover state -----------------------------------------------------------------
  let hover = -1;
  let hoverAmt = 0; // eases 0→1 for the hovered node's growth
  let prevHover = -1;
  let prevAmt = 0;
  const isNeighbour = new Uint8Array(n);
  let fireworks = 0; // seconds of fireworks left
  let colorsDirty = true;

  const applyColors = () => {
    isNeighbour.fill(0);
    if (hover >= 0) for (let k = adjStart[hover]; k < adjStart[hover + 1]; k++) isNeighbour[adjNode[k]] = 1;
    for (let i = 0; i < n; i++) {
      const j = i * 3;
      tmpC.setRGB(base[j], base[j + 1], base[j + 2]);
      let glowA = isHi[i] ? 0.55 : data.nodes[i].type === "document" ? 0.14 : 0.28;
      if (hover >= 0) {
        if (i === hover) { tmpC.lerp(WHITE, 0.3); glowA = 0.6; }
        else if (isNeighbour[i]) { tmpC.lerp(ACCENT, 0.5); glowA = 0.5; }
        else { tmpC.lerp(BG, 0.72); glowA = 0.04; }
      }
      nodeColor[j] = tmpC.r; nodeColor[j + 1] = tmpC.g; nodeColor[j + 2] = tmpC.b;
      if (pinned[i]) tmpC.lerp(WHITE, 0.5);
      glowColor[j] = tmpC.r; glowColor[j + 1] = tmpC.g; glowColor[j + 2] = tmpC.b;
      glowAlpha[i] = glowA;
    }
    for (let e = 0; e < m; e++) {
      const touches = hover >= 0 && (links[e * 2] === hover || links[e * 2 + 1] === hover);
      let a: number;
      if (hover >= 0) { tmpC.copy(touches ? ACCENT : INK); a = touches ? 0.9 : 0.05; }
      else { tmpC.copy(edgeHi[e] ? ACCENT : INK); a = edgeHi[e] ? 0.8 : 0.16; }
      tmpC.lerp(BG, 1 - a);
      edgeFlow[e] = hover < 0 ? 1 : touches ? 2.2 : 0.15;
      for (let v = 0; v < 2; v++) {
        edgeCol[e * 6 + v * 3] = tmpC.r; edgeCol[e * 6 + v * 3 + 1] = tmpC.g; edgeCol[e * 6 + v * 3 + 2] = tmpC.b;
      }
    }
    for (const [i, obj] of labels) (obj.element.firstChild as HTMLElement).style.opacity = hover < 0 || i === hover || isNeighbour[i] ? "1" : "0.18";
    nodes.instanceColor!.needsUpdate = true;
    glowGeo.attributes.aColor.needsUpdate = true;
    glowGeo.attributes.aAlpha.needsUpdate = true;
    edgeColAttr.needsUpdate = true;
    colorsDirty = false;
  };

  const rainbow = (time: number) => {
    for (let i = 0; i < n; i++) {
      tmpC.setHSL((i * 0.061 + time * 0.35) % 1, 0.9, 0.6, THREE.SRGBColorSpace);
      const j = i * 3;
      nodeColor[j] = glowColor[j] = tmpC.r; nodeColor[j + 1] = glowColor[j + 1] = tmpC.g; nodeColor[j + 2] = glowColor[j + 2] = tmpC.b;
      glowAlpha[i] = 0.8;
    }
    nodes.instanceColor!.needsUpdate = true;
    glowGeo.attributes.aColor.needsUpdate = true;
    glowGeo.attributes.aAlpha.needsUpdate = true;
  };

  // ---- tooltip -----------------------------------------------------------------------------
  const setTooltip = (i: number) => {
    if (i < 0) { tooltip.style.opacity = "0"; return; }
    const node = data.nodes[i];
    tooltip.replaceChildren();
    const title = document.createElement("div");
    title.className = "font-medium text-ink";
    title.textContent = node.label;
    const meta = document.createElement("div");
    meta.className = "cap";
    meta.textContent = node.type === "document" ? text.kind(node.type) : `${text.kind(node.type)} · ${text.docs(node.count ?? 0)}`;
    tooltip.append(title, meta);
    const hint = document.createElement("div");
    hint.className = "cap text-accent";
    hint.textContent = focus === i ? (node.url ? text.open : "") : text.focus;
    if (hint.textContent) tooltip.append(hint);
    tooltip.style.opacity = "1";
  };

  const setHover = (i: number) => {
    if (i === hover) return;
    if (hover >= 0) { prevHover = hover; prevAmt = hoverAmt; }
    hover = i;
    hoverAmt = 0;
    colorsDirty = true;
    canvas.style.cursor = drag >= 0 ? "grabbing" : i >= 0 ? "pointer" : "grab";
    setTooltip(i);
  };

  // ---- picking -----------------------------------------------------------------------------
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const vA = new THREE.Vector3();
  const vB = new THREE.Vector3();
  const plane = new THREE.Plane();
  const toNdc = (cx: number, cy: number) => {
    const r = canvas.getBoundingClientRect();
    ndc.set(((cx - r.left) / r.width) * 2 - 1, -((cy - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
  };
  const pick = (cx: number, cy: number) => {
    toNdc(cx, cy);
    const { origin: or, direction: d } = ray.ray;
    const px = (2 * Math.tan((camera.fov * Math.PI) / 360)) / Math.max(canvas.clientHeight, 1); // world per pixel at depth 1
    // A real hit on a sphere wins (the nearest one); otherwise the node closest to the cursor within ~7 px.
    let best = -1, bestT = Infinity, near = -1, nearPx = 7;
    for (let i = 0; i < n; i++) {
      const j = i * 3;
      const dx = P[j] - or.x, dy = P[j + 1] - or.y, dz = P[j + 2] - or.z;
      const t = dx * d.x + dy * d.y + dz * d.z;
      if (t <= 0) continue;
      const dist = Math.sqrt(Math.max(dx * dx + dy * dy + dz * dz - t * t, 0));
      if (dist < radius[i] * 1.2) {
        if (t < bestT) { best = i; bestT = t; }
      } else if (best < 0) {
        const off = (dist - radius[i]) / (t * px);
        if (off < nearPx) { near = i; nearPx = off; }
      }
    }
    return best >= 0 ? best : near;
  };

  // ---- camera flights ------------------------------------------------------------------------
  let focus = -1;
  const fly = { active: false, t: 0, dur: 1.1, fromPos: new THREE.Vector3(), fromTgt: new THREE.Vector3(), toTgt: new THREE.Vector3(), offset: new THREE.Vector3(), node: -1 };
  const flyTo = (i: number) => {
    focus = i;
    fly.node = i;
    fly.fromPos.copy(camera.position);
    fly.fromTgt.copy(controls.target);
    const dist = Math.min(1.7, Math.max(0.8, radius[i] * 22 + 0.65));
    fly.offset.subVectors(camera.position, controls.target).normalize().multiplyScalar(dist);
    fly.toTgt.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
    fly.t = 0;
    fly.dur = reduced ? 0.35 : 1.1;
    fly.active = true;
  };
  const flyHome = (reset: boolean, above = false) => {
    focus = -1;
    fly.node = -1;
    fly.fromPos.copy(camera.position);
    fly.fromTgt.copy(controls.target);
    fly.toTgt.set(0, 0, 0);
    if (above) fly.offset.set(0, 0.75 * distance, 0.75 * distance); // the galaxy looks best from above
    else if (reset) fly.offset.set(0, 0.16 * distance, distance);
    else fly.offset.subVectors(camera.position, controls.target).normalize().multiplyScalar(distance);
    fly.t = 0;
    fly.dur = reduced ? 0.35 : 1.1;
    fly.active = true;
  };

  // ---- physics -------------------------------------------------------------------------------
  let awake = 0; // frames left before the simulation sleeps
  let gravity = false;
  const wake = () => (awake = 90);
  const CELL = 0.1;
  const HASH = 4096;
  const head = new Int32Array(HASH);
  const next = new Int32Array(n);
  const cellX = new Int32Array(n), cellY = new Int32Array(n), cellZ = new Int32Array(n);
  const hash = (x: number, y: number, z: number) => ((Math.imul(x, 73856093) ^ Math.imul(y, 19349663) ^ Math.imul(z, 83492791)) >>> 0) & (HASH - 1);
  let drag = -1;
  const fling = new Float32Array(3);

  const step = () => {
    const anchor = gravity || fireworks > 2.6 ? 0 : fireworks > 0 ? 0.006 : 0.012;
    const damp = fireworks > 0 ? 0.96 : 0.9;
    F.fill(0);
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const k = anchor * mass[i];
      F[j] += (H[j] - P[j]) * k; F[j + 1] += (H[j + 1] - P[j + 1]) * k; F[j + 2] += (H[j + 2] - P[j + 2]) * k;
      if (gravity) {
        const x = P[j], z = P[j + 2], rho = Math.hypot(x, z) + 0.15;
        F[j] += mass[i] * (-0.0007 * x - (0.0018 * z) / rho);
        F[j + 1] += mass[i] * (-0.003 * P[j + 1]);
        F[j + 2] += mass[i] * (-0.0007 * z + (0.0018 * x) / rho);
      }
      if (fireworks > 0) F[j + 1] -= mass[i] * 0.0007;
    }
    for (let e = 0; e < m; e++) {
      const a = links[e * 2] * 3, b = links[e * 2 + 1] * 3;
      const dx = P[b] - P[a], dy = P[b + 1] - P[a + 1], dz = P[b + 2] - P[a + 2];
      const len = Math.hypot(dx, dy, dz) || 1e-4;
      const k = (0.05 * (len - rest[e])) / len;
      F[a] += dx * k; F[a + 1] += dy * k; F[a + 2] += dz * k;
      F[b] -= dx * k; F[b + 1] -= dy * k; F[b + 2] -= dz * k;
    }
    // Local repulsion on a spatial hash (neighbours within one cell only).
    head.fill(-1);
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const x = Math.floor(P[j] / CELL), y = Math.floor(P[j + 1] / CELL), z = Math.floor(P[j + 2] / CELL);
      cellX[i] = x; cellY[i] = y; cellZ[i] = z;
      const h = hash(x, y, z);
      next[i] = head[h];
      head[h] = i;
    }
    for (let i = 0; i < n; i++) {
      const j = i * 3;
      for (let ox = -1; ox <= 1; ox++)
        for (let oy = -1; oy <= 1; oy++)
          for (let oz = -1; oz <= 1; oz++) {
            for (let q = head[hash(cellX[i] + ox, cellY[i] + oy, cellZ[i] + oz)]; q >= 0; q = next[q]) {
              if (q <= i) continue;
              const k3 = q * 3;
              const dx = P[j] - P[k3], dy = P[j + 1] - P[k3 + 1], dz = P[j + 2] - P[k3 + 2];
              const d2 = dx * dx + dy * dy + dz * dz;
              if (d2 >= CELL * CELL || d2 < 1e-10) continue;
              const d = Math.sqrt(d2);
              const f = (0.0012 * (1 - d / CELL)) / d;
              F[j] += dx * f; F[j + 1] += dy * f; F[j + 2] += dz * f;
              F[k3] -= dx * f; F[k3 + 1] -= dy * f; F[k3 + 2] -= dz * f;
            }
          }
    }
    let maxV2 = 0;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      if (pinned[i] || i === drag) { V[j] = V[j + 1] = V[j + 2] = 0; continue; }
      const im = 1 / mass[i];
      let vx = (V[j] + F[j] * im) * damp, vy = (V[j + 1] + F[j + 1] * im) * damp, vz = (V[j + 2] + F[j + 2] * im) * damp;
      const v2 = vx * vx + vy * vy + vz * vz;
      if (v2 > 0.04) { const s = 0.2 / Math.sqrt(v2); vx *= s; vy *= s; vz *= s; }
      V[j] = vx; V[j + 1] = vy; V[j + 2] = vz;
      P[j] += vx; P[j + 1] += vy; P[j + 2] += vz;
      if (v2 > maxV2) maxV2 = v2;
    }
    return maxV2;
  };

  const impulse = (x: number, y: number, z: number, reach: number, power: number) => {
    if (!state.physics) return;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      if (pinned[i]) continue;
      const dx = P[j] - x, dy = P[j + 1] - y, dz = P[j + 2] - z;
      const d = Math.hypot(dx, dy, dz);
      if (d > reach || d < 1e-5) continue;
      const k = (power * (1 - d / reach)) / d;
      V[j] += dx * k; V[j + 1] += dy * k; V[j + 2] += dz * k;
    }
    wake();
  };

  const needPhysics = () => {
    if (state.physics) return;
    state.physics = true;
    o.onToggle("physics", true);
  };
  const shake = () => {
    needPhysics();
    const r = Math.random;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      if (pinned[i]) continue;
      V[j] += (r() - 0.5) * 0.09; V[j + 1] += (r() - 0.5) * 0.09; V[j + 2] += (r() - 0.5) * 0.09;
    }
    ripple(controls.target.x, controls.target.y, controls.target.z, ACCENT, 1.6);
    wake();
  };

  const launchFireworks = () => {
    fireworks = 4;
    needPhysics();
    const r = Math.random;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      if (pinned[i]) continue;
      const u = r() * 2 - 1, th = r() * Math.PI * 2, q = Math.sqrt(1 - u * u), s = 0.03 + r() * 0.06;
      V[j] += q * Math.cos(th) * s; V[j + 1] += u * s + 0.03; V[j + 2] += q * Math.sin(th) * s;
    }
    for (let k = 0; k < 6; k++) {
      const c = new THREE.Color().setHSL(k / 6, 1, 0.6, THREE.SRGBColorSpace);
      setTimeout(() => frame && ripple((r() - 0.5) * 1.2, (r() - 0.2) * 1.2, (r() - 0.5) * 1.2, c, 0.35 + r() * 0.4), k * 180);
    }
    wake();
    o.onToast(text.fireworks);
  };

  // ---- pointer ------------------------------------------------------------------------------
  const pointer = { x: 0, y: 0, moved: false, inside: false };
  const down = { x: 0, y: 0, id: -1, node: -1, moved: false };
  const grabOffset = new THREE.Vector3();
  let clickTimer = 0;
  let lastClickNode = -2;

  const onPointerDownCapture = (e: PointerEvent) => {
    if (e.target !== canvas) return;
    if (drag >= 0) { e.stopPropagation(); return; } // a second finger while dragging does nothing
    down.x = e.clientX; down.y = e.clientY; down.id = e.pointerId; down.moved = false;
    const i = pick(e.clientX, e.clientY);
    down.node = i;
    if (i < 0 || (e.pointerType === "mouse" && e.button !== 0)) return;
    // Grab the node: the orbit controls never see this pointer.
    e.stopPropagation();
    setHover(i);
    drag = i;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = "grabbing";
    camera.getWorldDirection(vA);
    vB.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
    plane.setFromNormalAndCoplanarPoint(vA, vB);
    toNdc(e.clientX, e.clientY);
    if (ray.ray.intersectPlane(plane, vA)) grabOffset.subVectors(vB, vA);
    else grabOffset.set(0, 0, 0);
    fling.fill(0);
    pointer.x = e.clientX; pointer.y = e.clientY; pointer.moved = false;
    wake();
  };

  const onPointerMove = (e: PointerEvent) => {
    pointer.x = e.clientX; pointer.y = e.clientY; pointer.moved = true; pointer.inside = true;
    if (down.id === e.pointerId && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) down.moved = true;
  };

  const release = (e: PointerEvent) => {
    if (e.pointerId !== down.id) return;
    const wasDrag = drag;
    const moved = down.moved;
    down.id = -1;
    if (wasDrag >= 0) {
      drag = -1;
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
      canvas.style.cursor = hover >= 0 ? "pointer" : "grab";
      const j = wasDrag * 3;
      if (moved) {
        if (pinned[wasDrag] || !state.physics) { H[j] = P[j]; H[j + 1] = P[j + 1]; H[j + 2] = P[j + 2]; }
        if (state.physics && !pinned[wasDrag]) { V[j] = fling[0]; V[j + 1] = fling[1]; V[j + 2] = fling[2]; }
        wake();
        return;
      }
    }
    if (moved || e.type === "pointercancel") return;
    const i = down.node;
    if (i >= 0) {
      ripple(P[i * 3], P[i * 3 + 1], P[i * 3 + 2], ACCENT, Math.max(0.25, radius[i] * 10));
      impulse(P[i * 3], P[i * 3 + 1], P[i * 3 + 2], 0.45, 0.012);
      if (clickTimer && lastClickNode === i) {
        // Double click/tap: pin or unpin.
        clearTimeout(clickTimer);
        clickTimer = 0;
        lastClickNode = -2;
        togglePin(i);
        return;
      }
      clearTimeout(clickTimer);
      lastClickNode = i;
      clickTimer = window.setTimeout(() => {
        clickTimer = 0;
        lastClickNode = -2;
        if (focus === i) {
          const url = data.nodes[i].url;
          if (url) window.open(url, "_blank", "noopener");
        } else {
          flyTo(i);
          setTooltip(hover);
        }
      }, 260);
    } else {
      // Background: a shockwave where you clicked, and back to the overview.
      toNdc(e.clientX, e.clientY);
      camera.getWorldDirection(vA);
      plane.setFromNormalAndCoplanarPoint(vA, controls.target);
      if (ray.ray.intersectPlane(plane, vB)) {
        ripple(vB.x, vB.y, vB.z, ACCENT, 0.8);
        impulse(vB.x, vB.y, vB.z, 0.7, 0.02);
      }
      if (focus >= 0) flyHome(false);
    }
  };

  const togglePin = (i: number) => {
    pinned[i] ^= 1;
    const j = i * 3;
    if (pinned[i]) {
      H[j] = P[j]; H[j + 1] = P[j + 1]; H[j + 2] = P[j + 2];
      const mark = new THREE.Mesh(pinGeo, pinMat);
      mark.scale.setScalar(Math.max(radius[i], 0.012));
      group.add(mark);
      pinMarks.set(i, mark);
      o.onToast(text.pinned);
    } else {
      H[j] = HOME0[j]; H[j + 1] = HOME0[j + 1]; H[j + 2] = HOME0[j + 2];
      const mark = pinMarks.get(i);
      if (mark) group.remove(mark);
      pinMarks.delete(i);
      o.onToast(text.unpinned);
      wake();
    }
    colorsDirty = true;
  };

  const onLeave = () => {
    pointer.inside = false;
    if (drag < 0) setHover(-1);
  };

  stage.addEventListener("pointerdown", onPointerDownCapture, true);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.style.cursor = "grab";

  // ---- keyboard: Esc, g (gravity), f (fps), the Konami code ---------------------------------
  let konami = 0;
  let showFps = false;
  const onKey = (e: KeyboardEvent) => {
    const el = e.target as HTMLElement | null;
    if (!visible || e.ctrlKey || e.metaKey || e.altKey || (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)))) return;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    konami = key === KONAMI[konami] ? konami + 1 : key === KONAMI[0] ? 1 : 0;
    if (konami === KONAMI.length) {
      konami = 0;
      launchFireworks();
      return;
    }
    if (key === "Escape" && focus >= 0) flyHome(false);
    else if (key === "g" && konami === 0) {
      gravity = !gravity;
      if (gravity) {
        needPhysics();
        flyHome(true, true);
      }
      wake();
      o.onToast(gravity ? text.gravityOn : text.gravityOff);
    } else if (key === "f" && konami === 0) {
      showFps = !showFps;
      o.fps.style.display = showFps ? "block" : "none";
    }
  };
  window.addEventListener("keydown", onKey);

  // ---- size, visibility ----------------------------------------------------------------------
  const resize = () => {
    const { clientWidth: w, clientHeight: h } = stage;
    if (!w || !h) return;
    renderer.setSize(w, h, false); // size the buffer; CSS keeps it fluid
    labelRenderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    const scale = (h * renderer.getPixelRatio()) / (2 * Math.tan((camera.fov * Math.PI) / 360));
    glowMat.uniforms.uScale.value = partMat.uniforms.uScale.value = scale;
    if (starMat) starMat.uniforms.uScale.value = scale;
    needsRender = true;
  };
  const ro = new ResizeObserver(resize);
  ro.observe(stage);

  let visible = true;
  let inView = true;
  const io = new IntersectionObserver((entries) => {
    inView = entries.some((x) => x.isIntersecting);
    schedule();
  });
  io.observe(stage);
  const onVisibility = () => schedule();
  document.addEventListener("visibilitychange", onVisibility);

  // ---- frame loop -----------------------------------------------------------------------------
  let frame = 0;
  let needsRender = true;
  let positionsDirty = true;
  let time = 0;
  let last = 0;
  let fpsFrames = 0;
  let fpsTime = 0;
  const M = nodes.instanceMatrix.array as Float32Array;

  const updateNodes = (breathe: boolean) => {
    for (let i = 0; i < n; i++) {
      let s = radius[i];
      if (breathe) s *= 1 + 0.06 * Math.sin(time * 1.4 + i * 2.399);
      if (isHi[i] && !reduced) s *= 1 + 0.18 * Math.sin(time * 2);
      if (i === hover) s *= 1 + 0.6 * easeOut(hoverAmt);
      else if (i === prevHover) s *= 1 + 0.6 * prevAmt;
      const k = i * 16, j = i * 3;
      M[k] = s; M[k + 5] = s; M[k + 10] = s; M[k + 15] = 1;
      M[k + 12] = P[j]; M[k + 13] = P[j + 1]; M[k + 14] = P[j + 2];
      glowSize[i] = s * (i === hover ? 7 : 6);
    }
    nodes.instanceMatrix.needsUpdate = true;
    glowGeo.attributes.aSize.needsUpdate = true;
  };

  const updateEdges = () => {
    for (let e = 0; e < m; e++) {
      const a = links[e * 2] * 3, b = links[e * 2 + 1] * 3, k = e * 6;
      edgePos[k] = P[a]; edgePos[k + 1] = P[a + 1]; edgePos[k + 2] = P[a + 2];
      edgePos[k + 3] = P[b]; edgePos[k + 4] = P[b + 1]; edgePos[k + 5] = P[b + 2];
    }
    edgePosAttr.needsUpdate = true;
    glowPos.needsUpdate = true;
    for (const [i, obj] of labels) obj.position.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
    for (const [i, mark] of pinMarks) mark.position.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]);
  };

  const updateParticles = (dt: number) => {
    for (let k = 0; k < K; k++) {
      const e = pEdge[k];
      let t = pT[k] + pSpeed[k] * dt;
      if (t >= 1) t -= 1;
      pT[k] = t;
      const a = flowFrom[e] * 3, b = flowTo[e] * 3, j = k * 3;
      pPos[j] = P[a] + (P[b] - P[a]) * t;
      pPos[j + 1] = P[a + 1] + (P[b + 1] - P[a + 1]) * t;
      pPos[j + 2] = P[a + 2] + (P[b + 2] - P[a + 2]) * t;
      pAlpha[k] = Math.sin(Math.PI * t) * 0.75 * edgeFlow[e];
    }
    partPosAttr.needsUpdate = true;
    partAlphaAttr.needsUpdate = true;
  };

  const tick = (now: number) => {
    frame = requestAnimationFrame(tick);
    const dt = last ? Math.min((now - last) / 1000, 0.1) : 1 / 60;
    last = now;
    time += dt;
    fpsFrames++;
    fpsTime += dt;
    if (fpsTime >= 0.5) {
      const fps = Math.round(fpsFrames / fpsTime);
      stage.dataset.fps = String(fps);
      if (showFps) o.fps.textContent = `${fps} FPS · ${n} uzlů`;
      fpsFrames = 0;
      fpsTime = 0;
    }

    // Hover (one pick per frame at most).
    if (pointer.moved && drag < 0 && pointer.inside) {
      pointer.moved = false;
      setHover(pick(pointer.x, pointer.y));
    }
    // Drag: the grabbed node follows the cursor on a plane facing the camera.
    if (drag >= 0 && pointer.moved) {
      pointer.moved = false;
      toNdc(pointer.x, pointer.y);
      if (ray.ray.intersectPlane(plane, vA)) {
        vA.add(grabOffset);
        const j = drag * 3;
        fling[0] = (vA.x - P[j]) * 0.6; fling[1] = (vA.y - P[j + 1]) * 0.6; fling[2] = (vA.z - P[j + 2]) * 0.6;
        P[j] = vA.x; P[j + 1] = vA.y; P[j + 2] = vA.z;
        positionsDirty = true;
        wake();
      }
    }

    // Physics.
    if (fireworks > 0) {
      fireworks -= dt;
      rainbow(time);
      if (fireworks <= 0) colorsDirty = true;
      wake();
    }
    if (state.physics && (awake > 0 || drag >= 0 || gravity)) {
      const steps = Math.min(3, Math.max(1, Math.round(dt * 60)));
      let maxV2 = 0;
      for (let s = 0; s < steps; s++) maxV2 = step();
      positionsDirty = true;
      if (maxV2 > 1e-8 || drag >= 0) awake = Math.max(awake, 30);
      else awake--;
    }

    if (colorsDirty && fireworks <= 0) applyColors();
    if (hover >= 0 && hoverAmt < 1) hoverAmt = Math.min(1, hoverAmt + dt * 5);
    if (prevHover >= 0) {
      prevAmt = Math.max(0, prevAmt - dt * 5);
      if (prevAmt === 0) prevHover = -1;
    }
    const breathe = !reduced && state.particles; // breathing belongs to the "motion" eye candy
    const animating = breathe || hi.size > 0 || prevHover >= 0 || (hover >= 0 && hoverAmt < 1);
    if (positionsDirty || animating || needsRender) updateNodes(breathe);
    if (positionsDirty) updateEdges();
    positionsDirty = false;
    particles.visible = state.particles && K > 0;
    if (particles.visible) updateParticles(dt);

    // Camera: flight, then follow the focused node.
    if (fly.active) {
      fly.t += dt / fly.dur;
      const k = easeInOut(Math.min(fly.t, 1));
      if (fly.node >= 0) fly.toTgt.set(P[fly.node * 3], P[fly.node * 3 + 1], P[fly.node * 3 + 2]);
      controls.target.lerpVectors(fly.fromTgt, fly.toTgt, k);
      vA.addVectors(fly.toTgt, fly.offset);
      camera.position.lerpVectors(fly.fromPos, vA, k);
      if (fly.t >= 1) fly.active = false;
    } else if (focus >= 0 && drag !== focus) {
      vA.set(P[focus * 3], P[focus * 3 + 1], P[focus * 3 + 2]).sub(controls.target).multiplyScalar(0.15);
      controls.target.add(vA);
      camera.position.add(vA);
    }
    controls.enabled = !fly.active;
    controls.autoRotate = (state.autoRotate || (focus >= 0 && !reduced)) && hover < 0 && drag < 0 && !fly.active;
    controls.autoRotateSpeed = (60 / o.period) * (focus >= 0 ? 3 : 1);
    const camMoved = controls.update(dt);
    const dist = camera.position.distanceTo(controls.target);
    fog.near = Math.max(0.05, dist - 0.8);
    fog.far = dist + 1.2;

    // Ripples, pin markers face the camera.
    let rippling = false;
    for (const r of ripples) {
      if (r.age >= r.life) continue;
      r.age += dt;
      const k = Math.min(r.age / r.life, 1);
      r.mesh.scale.setScalar(0.01 + r.grow * easeOut(k));
      (r.mesh.material as THREE.MeshBasicMaterial).opacity = (1 - k) * 0.85;
      r.mesh.quaternion.copy(camera.quaternion);
      if (k >= 1) r.mesh.visible = false;
      rippling = true;
    }
    for (const mark of pinMarks.values()) mark.quaternion.copy(camera.quaternion);

    if (!reduced) sky.rotation.y += dt * 0.004;
    if (starMat) starMat.uniforms.uTime.value = time;

    // Tooltip follows its node.
    if (hover >= 0) {
      vA.set(P[hover * 3], P[hover * 3 + 1], P[hover * 3 + 2]).project(camera);
      const x = ((vA.x + 1) / 2) * stage.clientWidth;
      const y = ((1 - vA.y) / 2) * stage.clientHeight;
      const flip = x > stage.clientWidth - 240;
      tooltip.style.transform = `translate(${flip ? x - 16 : x + 16}px, ${y - 12}px) translate(${flip ? "-100%" : "0"}, -100%)`;
    }

    const live = camMoved || animating || particles.visible || rippling || awake > 0 || fly.active || fireworks > 0 || !reduced || needsRender;
    if (live) {
      renderer.render(scene, camera);
      labelRenderer.render(scene, camera);
      needsRender = false;
    }
  };

  const schedule = () => {
    visible = inView && !document.hidden;
    if (visible && !frame) {
      last = 0;
      needsRender = true;
      frame = requestAnimationFrame(tick);
    } else if (!visible && frame) {
      cancelAnimationFrame(frame);
      frame = 0;
    }
  };

  // Dev only: lets a UI test find a node on screen.
  if (import.meta.env.DEV)
    (stage as HTMLDivElement & { kgDebug?: unknown }).kgDebug = {
      screen: (id: string) => {
        const i = data.nodes.findIndex((x) => x.id === id);
        vA.set(P[i * 3], P[i * 3 + 1], P[i * 3 + 2]).project(camera);
        const r = canvas.getBoundingClientRect();
        return [r.left + ((vA.x + 1) / 2) * r.width, r.top + ((1 - vA.y) / 2) * r.height];
      },
      focus: () => focus,
      pinned: () => pinMarks.size,
    };

  resize();
  applyColors();
  updateNodes(false);
  updateEdges();
  schedule();

  return {
    set(toggle, on) {
      state[toggle] = on;
      if (toggle === "physics" && on) wake();
      if (toggle === "physics" && !on) gravity = false;
      needsRender = true;
    },
    resetView() {
      gravity = false;
      for (const i of [...pinMarks.keys()]) togglePin(i);
      H.set(HOME0);
      if (!state.physics) { P.set(HOME0); V.fill(0); positionsDirty = true; }
      wake();
      setHover(-1);
      flyHome(true);
    },
    shake() {
      shake();
    },
    dispose() {
      cancelAnimationFrame(frame);
      frame = 0;
      clearTimeout(clickTimer);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("keydown", onKey);
      stage.removeEventListener("pointerdown", onPointerDownCapture, true);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", release);
      canvas.removeEventListener("pointercancel", release);
      canvas.removeEventListener("pointerleave", onLeave);
      controls.dispose();
      scene.traverse((obj) => {
        if (obj instanceof THREE.Mesh || obj instanceof THREE.LineSegments || obj instanceof THREE.Points) {
          obj.geometry.dispose();
          (obj.material as THREE.Material).dispose();
        } else if (obj instanceof THREE.Sprite) obj.material.dispose(); // sprites share one geometry
      });
      pinGeo.dispose();
      pinMat.dispose();
      disposables.forEach((d) => d.dispose());
      renderer.dispose();
      stage.replaceChildren();
    },
  };
}
