import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";

type Props = {
  nodes?: number;
  seed?: number;
  labels: string[];
  labelEvery?: number;
  highlight?: number[];
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
const NONE: number[] = [];

// Small deterministic PRNG so the sample graph is stable between renders.
function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function buildGraph(n: number, seed: number) {
  const rnd = mulberry32(seed);
  const golden = Math.PI * (3 - Math.sqrt(5));
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = golden * i + (rnd() - 0.5) * 0.6;
    const k = 0.55 + rnd() * 0.45;
    pts.push(new THREE.Vector3(k * r * Math.cos(th), k * y * 0.82, k * r * Math.sin(th)));
  }
  const edges = new Set<string>();
  pts.forEach((a, i) => {
    const near = pts
      .map((b, j) => ({ j, d: a.distanceToSquared(b) }))
      .sort((p, q) => p.d - q.d)
      .slice(1, 2 + (i % 2) + 1);
    near.forEach(({ j }) => edges.add(i < j ? `${i}-${j}` : `${j}-${i}`));
  });
  return { pts, edges: [...edges].map((e) => e.split("-").map(Number) as [number, number]) };
}

/** Slowly orbiting 3D knowledge graph (WebGL). Drag to rotate, scroll to zoom. */
export default function KnowledgeGraph({
  nodes = 52,
  seed = 7,
  labels,
  labelEvery,
  highlight = NONE,
  period = 90,
  distance = 3.4,
  className = "",
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
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

    const { pts, edges } = buildGraph(nodes, seed);
    const hi = new Set(highlight);
    const every = labelEvery ?? Math.max(1, Math.floor(nodes / labels.length));
    const labelled = new Map<number, string>();
    for (let i = 0, k = 0; i < nodes && k < labels.length; i += every, k++) labelled.set(i, labels[k]);

    const group = new THREE.Group();
    scene.add(group);

    // Edges: plain ones faint, ones between highlighted nodes in the accent.
    const plain: number[] = [];
    const strong: number[] = [];
    for (const [a, b] of edges) {
      const target = hi.has(a) && hi.has(b) ? strong : plain;
      target.push(...pts[a].toArray(), ...pts[b].toArray());
    }
    const lines = (arr: number[], color: number, opacity: number) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
      return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    };
    group.add(lines(plain, INK, 0.16), lines(strong, ACCENT, 0.8));

    // Nodes.
    const sphere = new THREE.SphereGeometry(1, 16, 12);
    const pulsing: THREE.Mesh[] = [];
    pts.forEach((p, i) => {
      const isHi = hi.has(i);
      const isLab = labelled.has(i);
      const color = isHi ? ACCENT : isLab || hi.size === 0 ? INK : DIM;
      const mesh = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color }));
      mesh.position.copy(p);
      mesh.scale.setScalar(isHi ? 0.024 : isLab ? 0.019 : 0.011);
      group.add(mesh);
      if (isHi) pulsing.push(mesh);
      if (isLab) {
        // CSS2DRenderer owns the outer element's transform, so offset the text inside it.
        const tag = document.createElement("div");
        const text = document.createElement("span");
        text.textContent = labelled.get(i)!;
        text.style.cssText = `display: block; margin-left: 10px; font: 10px "IBM Plex Mono", monospace; color: ${isHi ? "#6cc4dc" : "#a3a9b3"}; white-space: nowrap;`;
        tag.appendChild(text);
        const obj = new CSS2DObject(tag);
        obj.center.set(0, 0.5);
        mesh.add(obj);
      }
    });

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enablePan = false;
    controls.enableDamping = true;
    controls.minDistance = 2;
    controls.maxDistance = 6;
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 60 / period; // OrbitControls: 2.0 = one turn per 30 s

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
        const s = 0.024 * (1 + 0.18 * Math.sin(clock.getElapsedTime() * 2));
        pulsing.forEach((m) => m.scale.setScalar(s));
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
      scene.traverse((o) => {
        if (o instanceof THREE.Mesh || o instanceof THREE.LineSegments) {
          o.geometry.dispose();
          (o.material as THREE.Material).dispose();
        }
      });
      renderer.dispose();
      el.innerHTML = "";
    };
  }, [nodes, seed, labels, labelEvery, highlight, period, distance]);

  return (
    <div
      ref={host}
      className={`relative h-full w-full cursor-grab overflow-hidden active:cursor-grabbing ${className}`}
      role="img"
      aria-label="3D knowledge graph. Drag to rotate."
    >
      {failed && (
        <p className="cap absolute inset-0 grid place-items-center">WebGL is not available in this browser.</p>
      )}
    </div>
  );
}
