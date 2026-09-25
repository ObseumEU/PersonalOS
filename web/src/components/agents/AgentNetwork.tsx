import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import type { EngineView } from "../../agentsApi";

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
  engine_view?: EngineView | null;
  role?: string | null;
  team?: string | null;
  reports_to?: number | null;
};
/** peer = between members, platform = to the PersonalOS hub, org = who reports to whom. */
export type EdgeScope = "peer" | "platform" | "org";
export type NetEdge = { from: number; to: number; type: EdgeType; kind?: EdgeType; scope?: EdgeScope; count: number };
export type NetEvent = { from: number; to: number; type: EdgeType; scope?: EdgeScope; at: string };
export type EdgeType = "assign" | "handoff" | "message" | "approval" | "mcp" | "run" | "org";
export type Network = { window: string; frozen: boolean; nodes: NetNode[]; edges: NetEdge[]; events: NetEvent[] };

// Agent-to-agent traffic is bright (handoffs) or dashed (messages); calls to the
// platform stay dim so the team's own work reads first.
export const EDGE_COLOR: Record<EdgeType, number> = {
  assign: 0xe6e8eb,
  handoff: 0x9be3f5,
  message: 0x6cc4dc,
  approval: 0xd9a55b,
  mcp: 0x4a515b,
  run: 0x3e7c8d,
  org: 0xe6e8eb,
};
const scopeOf = (e: NetEdge | NetEvent): EdgeScope => e.scope ?? (e.type === "org" ? "org" : e.to === 0 ? "platform" : "peer");
const INK = 0xe6e8eb;
const DIM = 0x3a4048;
const ACCENT = 0x6cc4dc;
const AMBER = 0xd9a55b;
const BG = 0x111418;

const workload = (n: NetNode) => n.open + 2 * n.working + n.review;

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);

function tag(text: string, sub: string, color: string, engine?: { label: string; fallback: boolean }) {
  const outer = document.createElement("div");
  const line = engine ? `<br><span style="color:${engine.fallback ? "#d9a55b" : "#3e7c8d"};font-size:9px">${esc(engine.label)}</span>` : "";
  outer.innerHTML = `<span style="display:block;margin-left:14px;font:11px 'IBM Plex Mono',monospace;color:${color};white-space:nowrap">${esc(text)}<br><span style="color:#7d848f;font-size:9px">${sub}</span>${line}</span>`;
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
  orgMode = false,
}: {
  data: Network;
  onSelect?: (id: number) => void;
  compact?: boolean;
  /** Lay members out by reporting level and draw the reports_to lines. */
  orgMode?: boolean;
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
    const rings: [number, number][] = [];
    if (orgMode) {
      // Org chart: one horizontal ring per reporting level, the owner on top.
      const byId = new Map(data.nodes.map((n) => [n.id, n]));
      const level = (n: NetNode) => {
        let d = 0;
        const seen = new Set([n.id]);
        for (let up = n.reports_to; up != null && byId.has(up) && !seen.has(up); up = byId.get(up)!.reports_to) {
          seen.add(up);
          d++;
        }
        return d;
      };
      const levels = new Map<number, NetNode[]>();
      for (const n of data.nodes) {
        if (n.kind === "hub" || n.status === "archived") continue;
        const l = level(n);
        levels.set(l, [...(levels.get(l) ?? []), n]);
      }
      const top = Math.max(1, ...levels.keys());
      levels.forEach((ns, l) => {
        const y = 0.75 - (l * 1.2) / top;
        const r = ns.length === 1 ? 0 : Math.min(0.4 + 0.15 * ns.length, 1.6);
        if (r) rings.push([r, y]);
        ns.forEach((n, i) => {
          const a = (i / ns.length) * Math.PI * 2 + l * 0.4;
          pos.set(n.id, new THREE.Vector3(Math.cos(a) * r, y, Math.sin(a) * r));
        });
      });
    } else {
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
      rings.push([0.95, 0], [1.8, 0]);
    }

    const maxLoad = Math.max(1, ...data.nodes.map(workload));
    const clickable: { mesh: THREE.Mesh; id: number }[] = [];
    const pulses: { mesh: THREE.Mesh; base: number; phase: number }[] = [];

    // Quiet reference rings.
    for (const [r, y] of rings) {
      const pts = Array.from({ length: 129 }, (_, i) => new THREE.Vector3(Math.cos((i / 128) * Math.PI * 2) * r, y, Math.sin((i / 128) * Math.PI * 2) * r));
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
      const loadText = `${n.working} working · ${n.open} next${n.review ? ` · ${n.review} review` : ""}`;
      const roleText = n.role ? `${n.role.replace(/_/g, " ")}${n.team ? ` · ${n.team}` : ""}` : loadText;
      const sub = n.kind === "hub" ? (data.frozen ? "FROZEN" : "platform") : orgMode ? roleText : loadText;
      const ev = n.engine_view;
      const shown = ev ? (ev.last_run?.running ? ev.last_run : ev.now) : null;
      const engine = shown ? { label: shown.label, fallback: "fallback" in shown && !!shown.fallback } : undefined;
      mesh.add(tag(name, sub, n.kind === "human" || n.kind === "hub" ? "#e6e8eb" : dimmed ? "#7d848f" : "#6cc4dc", engine));
    }

    // Edges by scope: agent-to-agent arcs high above the plane (handoffs as a
    // bright tube, messages dashed), calls to the platform low and dim, and in
    // org mode the reports_to hierarchy as straight lines.
    const shown = data.edges.filter((e) => (orgMode ? scopeOf(e) !== "platform" : scopeOf(e) !== "org"));
    const maxCount = Math.max(1, ...shown.filter((e) => scopeOf(e) !== "org").map((e) => e.count));
    const curves = new Map<string, THREE.QuadraticBezierCurve3>();
    for (const e of shown) {
      const a = pos.get(e.from);
      const b = pos.get(e.to);
      if (!a || !b) continue;
      const scope = scopeOf(e);
      const mid = a.clone().add(b).multiplyScalar(0.5);
      if (scope === "peer") mid.y += orgMode ? 0.14 : 0.34;
      else if (scope === "platform") mid.y += 0.1;
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      curves.set(`${e.from}-${e.to}-${e.type}`, curve);
      const weight = e.count / maxCount;
      if (e.type === "handoff") {
        group.add(
          new THREE.Mesh(
            new THREE.TubeGeometry(curve, 32, 0.0045 + 0.004 * weight, 6, false),
            new THREE.MeshBasicMaterial({ color: EDGE_COLOR.handoff, transparent: true, opacity: 0.6 + 0.4 * weight }),
          ),
        );
        continue;
      }
      const opacity = scope === "org" ? 0.5 : scope === "platform" ? 0.06 + 0.2 * weight : 0.25 + 0.55 * weight;
      const material =
        e.type === "message"
          ? new THREE.LineDashedMaterial({ color: EDGE_COLOR.message, transparent: true, opacity, dashSize: 0.05, gapSize: 0.035 })
          : new THREE.LineBasicMaterial({ color: EDGE_COLOR[e.type], transparent: true, opacity });
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(32)), material);
      if (e.type === "message") line.computeLineDistances();
      group.add(line);
    }

    // Particles: the latest events flow along their curves.
    const particles = data.events
      .slice(0, compact ? 24 : 48)
      .map((ev, i) => {
        const curve = curves.get(`${ev.from}-${ev.to}-${ev.type}`);
        if (!curve) return null;
        const peer = scopeOf(ev) === "peer";
        const m = new THREE.Mesh(sphere, new THREE.MeshBasicMaterial({ color: EDGE_COLOR[ev.type], transparent: !peer, opacity: peer ? 1 : 0.5 }));
        m.scale.setScalar(peer ? 0.016 : 0.01);
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
  }, [data, onSelect, compact, orgMode]);

  return (
    <div
      ref={host}
      className="relative h-full w-full cursor-grab overflow-hidden active:cursor-grabbing"
      role="img"
      aria-label={
        orgMode
          ? "Org chart: members layered by whom they report to, with handoffs and messages between them. Drag to orbit, click a member to open it."
          : "Agent network: members sized by workload, bright lines for handoffs and dashed lines for messages between agents, dim lines for calls to the platform. Drag to orbit, click a member to open it."
      }
    >
      {failed && <p className="cap absolute inset-0 grid place-items-center">WebGL is not available in this browser.</p>}
    </div>
  );
}
