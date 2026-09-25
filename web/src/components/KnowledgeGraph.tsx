import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import type { KGraph, KNode } from "../knowledgeApi";

type Props = {
  data: KGraph;
  /** Node ids to light up (e.g. the documents an answer cites). */
  highlight?: string[];
  /** How many collections get a label (the biggest ones). */
  labelCollections?: number;
  /** Seconds per full turn of the slow auto-orbit. */
  period?: number;
  /** Camera distance; smaller is closer. */
  distance?: number;
  className?: string;
};

const INK = 0xe6e8eb;
const DIM = 0x4a515b;
const ACCENT = 0x6cc4dc;
const BG = 0x111418;
const NONE: string[] = [];

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

/** A small 3D force layout: linked nodes pull together, all nodes push apart. */
function layout(data: KGraph) {
  const rnd = mulberry32(7);
  const index = new Map(data.nodes.map((n, i) => [n.id, i]));
  const n = data.nodes.length;
  const pos = data.nodes.map(() => new THREE.Vector3(rnd() - 0.5, rnd() - 0.5, rnd() - 0.5).multiplyScalar(1.6));
  const links = data.edges
    .map((e) => [index.get(e.source), index.get(e.target)] as const)
    .filter((l): l is readonly [number, number] => l[0] !== undefined && l[1] !== undefined);
  const rest: Record<string, number> = { workspace: 0.9, source: 0.7, collection: 0.45, document: 0.22 };
  const repel = 0.02 * Math.sqrt(60 / Math.max(n, 1));
  const d = new THREE.Vector3();
  for (let it = 0; it < 220; it++) {
    const cool = 1 - it / 240;
    const force = pos.map(() => new THREE.Vector3());
    for (let i = 0; i < n; i++)
      for (let j = i + 1; j < n; j++) {
        d.subVectors(pos[i], pos[j]);
        const l2 = Math.max(d.lengthSq(), 0.0025);
        d.multiplyScalar(repel / l2);
        force[i].add(d);
        force[j].sub(d);
      }
    for (const [a, b] of links) {
      d.subVectors(pos[b], pos[a]);
      const len = d.length() || 1e-3;
      d.multiplyScalar(((len - rest[data.nodes[b].type]) / len) * 0.08);
      force[a].add(d);
      force[b].sub(d);
    }
    for (let i = 0; i < n; i++) {
      force[i].addScaledVector(pos[i], -0.004); // gentle pull to the centre
      pos[i].addScaledVector(force[i].clampLength(0, 0.08), cool);
    }
  }
  // Centre and fit into a unit ball (by the 95th percentile, so one outlier does not shrink the rest).
  const c = pos.reduce((a, p) => a.add(p), new THREE.Vector3()).divideScalar(Math.max(n, 1));
  pos.forEach((p) => p.sub(c));
  const radii = pos.map((p) => p.length()).sort((a, b) => a - b);
  const r = radii[Math.floor(radii.length * 0.95)] || 1;
  pos.forEach((p) => p.multiplyScalar(1.05 / r));
  return { pos, links };
}

/** Radius by how much a node holds: sqrt of its document count, so a big
channel stands out without swamping the rest. */
function sizer(nodes: KNode[]) {
  const max = Math.max(1, ...nodes.map((n) => n.count ?? 0));
  return (node: KNode) => 0.008 + 0.042 * Math.sqrt((node.count ?? 0) / max);
}

const docs = (n: number) => `${n.toLocaleString("cs-CZ")} dok.`;

/** Slowly orbiting 3D graph of our knowledge base (WebGL). Drag to rotate, scroll to zoom, click to open. */
export default function KnowledgeGraph({
  data,
  highlight = NONE,
  labelCollections = 8,
  period = 90,
  distance = 3.4,
  className = "",
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const el = host.current;
    if (!el || data.nodes.length === 0) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setFailed(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.domElement.style.cssText = "display:block;width:100%;height:100%";
    el.appendChild(renderer.domElement);

    const labelRenderer = new CSS2DRenderer();
    labelRenderer.domElement.style.position = "absolute";
    labelRenderer.domElement.style.inset = "0";
    labelRenderer.domElement.style.pointerEvents = "none";
    el.appendChild(labelRenderer.domElement);

    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog(BG, distance - 0.8, distance + 1.2);
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 50);
    camera.position.set(0, 0.16 * distance, distance);

    const { pos, links } = layout(data);
    const size = sizer(data.nodes);
    const hi = new Set(highlight);
    const bigCollections = new Set(
      data.nodes
        .filter((x) => x.type === "collection")
        .sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
        .slice(0, labelCollections)
        .map((x) => x.id),
    );

    const group = new THREE.Group();
    scene.add(group);

    // Edges: faint, the ones leading to a highlighted node in the accent.
    const plain: number[] = [];
    const strong: number[] = [];
    for (const [a, b] of links) {
      const target = hi.has(data.nodes[a].id) || hi.has(data.nodes[b].id) ? strong : plain;
      target.push(...pos[a].toArray(), ...pos[b].toArray());
    }
    const lines = (arr: number[], color: number, opacity: number) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
      return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    };
    group.add(lines(plain, INK, 0.16), lines(strong, ACCENT, 0.8));

    // Nodes: workspaces in the accent, sources and collections in ink, documents dim.
    const sphere = new THREE.SphereGeometry(1, 16, 12);
    const pulsing: [THREE.Mesh, number][] = [];
    const meshes: THREE.Mesh[] = [];
    data.nodes.forEach((node, i) => {
      const isHi = hi.has(node.id);
      const color = isHi || node.type === "workspace" ? ACCENT : node.type === "document" && hi.size === 0 ? DIM : node.type === "document" ? DIM : INK;
      const mesh = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color }));
      mesh.position.copy(pos[i]);
      const s = isHi ? Math.max(size(node), 0.02) : size(node);
      mesh.scale.setScalar(s);
      mesh.userData = node;
      group.add(mesh);
      meshes.push(mesh);
      if (isHi) pulsing.push([mesh, s]);
      const labelled = node.type === "workspace" || node.type === "source" || bigCollections.has(node.id) || isHi;
      if (labelled) {
        // CSS2DRenderer owns the outer element's transform, so offset the text inside it.
        const tag = document.createElement("div");
        const text = document.createElement("span");
        const name = node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label;
        text.textContent = node.type === "document" ? name : `${name} · ${docs(node.count ?? 0)}`;
        const strongLabel = node.type === "workspace" || isHi;
        text.style.cssText = `display: block; margin-left: 10px; font: ${node.type === "workspace" ? "11px" : "10px"} "IBM Plex Mono", monospace; color: ${strongLabel ? "#6cc4dc" : "#a3a9b3"}; white-space: nowrap;`;
        tag.appendChild(text);
        const obj = new CSS2DObject(tag);
        obj.center.set(0, 0.5);
        mesh.add(obj);
      }
    });

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enablePan = false;
    controls.enableDamping = true;
    controls.minDistance = 1.6;
    controls.maxDistance = 6;
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 60 / period; // OrbitControls: 2.0 = one turn per 30 s

    // Click a node to open it (collection or document link), without breaking drag-to-orbit.
    const ray = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let down: [number, number] | null = null;
    const onDown = (e: PointerEvent) => (down = [e.clientX, e.clientY]);
    const onUp = (e: PointerEvent) => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
      const r = renderer.domElement.getBoundingClientRect();
      pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(pointer, camera);
      const hit = ray.intersectObjects(meshes, false)[0];
      const node = hit?.object.userData as KNode | undefined;
      if (node?.url) window.open(node.url, "_blank", "noopener");
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = el;
      if (!w || !h) return;
      renderer.setSize(w, h, false); // size the buffer; CSS keeps it fluid
      labelRenderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(el);

    let frame = 0;
    const clock = new THREE.Clock();
    const tick = () => {
      frame = requestAnimationFrame(tick);
      if (!reduced) {
        const k = 1 + 0.18 * Math.sin(clock.getElapsedTime() * 2);
        pulsing.forEach(([m, s]) => m.scale.setScalar(s * k));
      }
      controls.update();
      renderer.render(scene, camera);
      labelRenderer.render(scene, camera);
    };
    tick();

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onUp);
      scene.traverse((o) => {
        if (o instanceof THREE.Mesh || o instanceof THREE.LineSegments) {
          o.geometry.dispose();
          (o.material as THREE.Material).dispose();
        }
      });
      renderer.dispose();
      el.innerHTML = "";
    };
  }, [data, highlight, labelCollections, period, distance]);

  return (
    <div
      ref={host}
      className={`relative h-full w-full cursor-grab overflow-hidden active:cursor-grabbing ${className}`}
      role="img"
      aria-label={`3D graph of the knowledge base: ${data.nodes.length} nodes. Drag to rotate, click a node to open it.`}
    >
      {failed && <p className="cap absolute inset-0 grid place-items-center">WebGL is not available in this browser.</p>}
    </div>
  );
}
