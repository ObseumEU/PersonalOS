import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";

export type NetNode = {
  id: number;
  name: string;
  kind: "human" | "ai" | "agent" | "hub";
  is_owner: boolean;
  open: number;
  working: number;
  review: number;
  tokens: number;
  status: string;
};
export type NetEdge = { from: number; to: number; type: EdgeType; count: number };
export type NetEvent = { from: number; to: number; type: EdgeType; at: string };
export type EdgeType = "assign" | "message" | "approval" | "mcp" | "run";
export type Network = { window: string; frozen: boolean; nodes: NetNode[]; edges: NetEdge[]; events: NetEvent[] };

export const EDGE_COLOR: Record<EdgeType, number> = {
  assign: 0xe6e8eb,
  message: 0x6cc4dc,
  approval: 0xd9a55b,
  mcp: 0x3e7c8d,
  run: 0x6cc4dc,
};
const INK = 0xe6e8eb;
const DIM = 0x3a4048;
const ACCENT = 0x6cc4dc;
const AMBER = 0xd9a55b;
const BG = 0x111418;

const workload = (n: NetNode) => n.open + 2 * n.working + n.review;

function tag(text: string, sub: string, color: string) {
  const outer = document.createElement("div");
  outer.innerHTML = `<span style="display:block;margin-left:14px;font:11px 'IBM Plex Mono',monospace;color:${color};white-space:nowrap">${text}<br><span style="color:#7d848f;font-size:9px">${sub}</span></span>`;
  const obj = new CSS2DObject(outer);
  obj.center.set(0, 0.5);
  return obj;
}

/** Members as a 3D network. Size and heat = workload, edges = interactions,
 *  particles = the latest events travelling along their edge. */
export default function AgentNetwork({
  data,
  onSelect,
  compact = false,
}: {
  data: Network;
  onSelect?: (id: number) => void;
  compact?: boolean;
}) {
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
    const labels = new CSS2DRenderer();
    labels.domElement.style.cssText = "position:absolute;inset:0;pointer-events:none";
    el.appendChild(labels.domElement);

    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog(BG, 3.4, 7.5);
    const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 50);
    camera.position.set(0, 1.35, compact ? 4.1 : 4.4);
    const group = new THREE.Group();
    scene.add(group);
    const sphere = new THREE.SphereGeometry(1, 24, 16);

    // Layout: the hub in the middle, people on an inner ring, agents on an outer
    // ring, gently staggered in height so the structure reads in 3D.
    const pos = new Map<number, THREE.Vector3>();
    const people = data.nodes.filter((n) => n.kind === "human");
    const agents = data.nodes.filter((n) => n.kind === "ai" || n.kind === "agent");
    pos.set(0, new THREE.Vector3(0, 0, 0));
    people.forEach((n, i) => {
      const a = (i / Math.max(people.length, 1)) * Math.PI * 2 + Math.PI / 2;
      pos.set(n.id, new THREE.Vector3(Math.cos(a) * 0.95, 0.62, Math.sin(a) * 0.95));
    });
    agents.forEach((n, i) => {
      const a = (i / Math.max(agents.length, 1)) * Math.PI * 2;
      pos.set(n.id, new THREE.Vector3(Math.cos(a) * 1.8, (i % 2 ? 0.3 : -0.3) - 0.1, Math.sin(a) * 1.8));
    });

    const maxLoad = Math.max(1, ...data.nodes.map(workload));
    const clickable: { mesh: THREE.Mesh; id: number }[] = [];
    const pulses: { mesh: THREE.Mesh; base: number; phase: number }[] = [];

    // Quiet reference rings.
    for (const r of [0.95, 1.8]) {
      const pts = Array.from({ length: 129 }, (_, i) => new THREE.Vector3(Math.cos((i / 128) * Math.PI * 2) * r, 0, Math.sin((i / 128) * Math.PI * 2) * r));
      group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.06 })));
    }

    for (const n of data.nodes) {
      const p = pos.get(n.id);
      if (!p) continue;
      const load = workload(n);
      const dimmed = n.status === "archived" || n.status === "paused" || n.status === "frozen";
      const heat = load / maxLoad;
      const color = n.kind === "hub" ? INK : dimmed ? DIM : n.review > 0 ? AMBER : n.kind === "human" ? INK : new THREE.Color(DIM).lerp(new THREE.Color(ACCENT), 0.35 + 0.65 * heat).getHex();
      const size = n.kind === "hub" ? 0.05 : 0.045 + 0.05 * Math.sqrt(heat);
      const mesh = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color, transparent: dimmed, opacity: dimmed ? 0.5 : 1 }));
      mesh.position.copy(p);
      mesh.scale.setScalar(size);
      group.add(mesh);
      if (n.kind === "hub") {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(1, 0.02, 8, 64), new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.5 }));
        ring.scale.setScalar(0.11);
        ring.rotation.x = Math.PI / 2;
        ring.position.copy(p);
        group.add(ring);
      } else clickable.push({ mesh, id: n.id });
      if (n.working > 0 && !dimmed) pulses.push({ mesh, base: size, phase: n.id });
      // Soft halo proportional to workload.
      if (load > 0 && !dimmed) {
        const halo = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.05, depthWrite: false }));
        halo.position.copy(p);
        halo.scale.setScalar(size * 2.4);
        group.add(halo);
      }
      const name = n.is_owner ? "You" : n.name;
      const sub = n.kind === "hub" ? (data.frozen ? "FROZEN" : "platform") : `${n.working} working · ${n.open} next${n.review ? ` · ${n.review} review` : ""}`;
      mesh.add(tag(name, sub, n.kind === "human" || n.kind === "hub" ? "#e6e8eb" : dimmed ? "#7d848f" : "#6cc4dc"));
    }

    // Edges: straight thin lines, opacity by volume; curved slightly upward so
    // two-way traffic stays readable.
    const maxCount = Math.max(1, ...data.edges.map((e) => e.count));
    const curves = new Map<string, THREE.QuadraticBezierCurve3>();
    for (const e of data.edges) {
      const a = pos.get(e.from);
      const b = pos.get(e.to);
      if (!a || !b) continue;
      const mid = a.clone().add(b).multiplyScalar(0.5);
      mid.y += 0.18 + (e.type === "mcp" || e.type === "run" ? 0 : 0.12);
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      curves.set(`${e.from}-${e.to}-${e.type}`, curve);
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(curve.getPoints(32)),
        new THREE.LineBasicMaterial({ color: EDGE_COLOR[e.type], transparent: true, opacity: 0.12 + 0.5 * (e.count / maxCount) }),
      );
      group.add(line);
    }

    // Particles: the latest events flow along their curves.
    const particles = data.events
      .slice(0, compact ? 24 : 48)
      .map((ev, i) => {
        const curve = curves.get(`${ev.from}-${ev.to}-${ev.type}`);
        if (!curve) return null;
        const m = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color: EDGE_COLOR[ev.type] }));
        m.scale.setScalar(0.012);
        group.add(m);
        return { m, curve, offset: (i * 0.137) % 1, speed: 0.12 + (i % 5) * 0.03 };
      })
      .filter(Boolean) as { m: THREE.Mesh; curve: THREE.QuadraticBezierCurve3; offset: number; speed: number }[];

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enablePan = false;
    controls.enableDamping = true;
    controls.minDistance = 2.2;
    controls.maxDistance = 8;
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 0.45;

    const raycaster = new THREE.Raycaster();
    const onClick = (e: MouseEvent) => {
      const r = renderer.domElement.getBoundingClientRect();
      raycaster.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
      const hit = raycaster.intersectObjects(clickable.map((c) => c.mesh))[0];
      if (hit && onSelect) onSelect(clickable.find((c) => c.mesh === hit.object)!.id);
    };
    renderer.domElement.addEventListener("click", onClick);

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = el;
      if (!w || !h) return;
      renderer.setSize(w, h, false); // size the buffer; CSS keeps it fluid
      labels.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(el);

    const clock = new THREE.Clock();
    let frame = 0;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const t = clock.getElapsedTime();
      if (!reduced) {
        pulses.forEach(({ mesh, base, phase }) => mesh.scale.setScalar(base * (1 + 0.18 * Math.sin(t * 2.2 + phase))));
        particles.forEach((p) => p.m.position.copy(p.curve.getPoint((p.offset + t * p.speed) % 1)));
      } else particles.forEach((p) => p.m.position.copy(p.curve.getPoint(p.offset)));
      controls.update();
      renderer.render(scene, camera);
      labels.render(scene, camera);
    };
    tick();

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      renderer.domElement.removeEventListener("click", onClick);
      controls.dispose();
      scene.traverse((o) => {
        if (o instanceof THREE.Mesh || o instanceof THREE.Line) {
          o.geometry.dispose();
          (o.material as THREE.Material).dispose();
        }
      });
      renderer.dispose();
      el.innerHTML = "";
    };
  }, [data, onSelect, compact]);

  return (
    <div
      ref={host}
      className="relative h-full w-full cursor-grab overflow-hidden active:cursor-grabbing"
      role="img"
      aria-label="Agent network: members sized by workload, lines for hand-offs, messages, approvals and platform calls. Drag to orbit, click a member to open it."
    >
      {failed && <p className="cap absolute inset-0 grid place-items-center">WebGL is not available in this browser.</p>}
    </div>
  );
}
