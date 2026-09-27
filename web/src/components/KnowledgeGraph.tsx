import { Atom, LocateFixed, Maximize2, Minimize2, RotateCw, Sparkles, Zap, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { LOCALE, t } from "../i18n";
import type { KGraph } from "../knowledgeApi";
import { createGraphScene, type SceneHandle, type Toggle } from "./graphScene";

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

const NONE: string[] = [];
const docs = (n: number) => t("kg.docs_short", { n: n.toLocaleString(LOCALE) });
const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Our knowledge base as a living 3D graph (WebGL): drag nodes (the linked ones follow on springs),
 * hover for details, click to fly to a node (again to open it), double-click to pin, Esc to go back.
 * Hidden extras: "g" spins it into a galaxy, "f" shows the frame rate, the Konami code sets off fireworks.
 */
export default function KnowledgeGraph({ data, highlight = NONE, labelCollections = 8, period = 90, distance = 3.4, className = "" }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const tooltip = useRef<HTMLDivElement>(null);
  const fps = useRef<HTMLDivElement>(null);
  const scene = useRef<SceneHandle | null>(null);
  const [reduced] = useState(reducedMotion);
  const [toggles, setToggles] = useState<Record<Toggle, boolean>>(() => ({ autoRotate: !reduced, physics: true, particles: !reduced }));
  const current = useRef(toggles);
  current.current = toggles;
  const [failed, setFailed] = useState(false);
  const [full, setFull] = useState(false);
  const [toast, setToast] = useState<{ text: string; key: number } | null>(null);

  useEffect(() => {
    if (!stage.current || !tooltip.current || !fps.current || data.nodes.length === 0) return;
    const handle = createGraphScene({
      stage: stage.current,
      tooltip: tooltip.current,
      fps: fps.current,
      data,
      highlight,
      labelCollections,
      period,
      distance,
      reduced,
      initial: current.current,
      text: {
        kind: (type) => t(`kg.kind.${type}`),
        docs,
        open: t("kg.tip_open"),
        focus: t("kg.tip_focus"),
        pinned: t("kg.pinned"),
        unpinned: t("kg.unpinned"),
        gravityOn: t("kg.gravity_on"),
        gravityOff: t("kg.gravity_off"),
        fireworks: t("kg.fireworks"),
      },
      onToast: (text) => setToast({ text, key: Date.now() }),
      onToggle: (toggle, on) => setToggles((s) => ({ ...s, [toggle]: on })),
    });
    setFailed(!handle);
    scene.current = handle;
    return () => {
      handle?.dispose();
      scene.current = null;
    };
  }, [data, highlight, labelCollections, period, distance, reduced]);

  useEffect(() => {
    for (const k of Object.keys(toggles) as Toggle[]) scene.current?.set(k, toggles[k]);
  }, [toggles]);

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 1800);
    return () => clearTimeout(id);
  }, [toast]);

  useEffect(() => {
    const onChange = () => setFull(document.fullscreenElement === root.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const flip = (k: Toggle) => setToggles((s) => ({ ...s, [k]: !s[k] }));
  const toggleFull = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void root.current?.requestFullscreen?.().catch(() => undefined);
  };
  const canFull = typeof document !== "undefined" && document.fullscreenEnabled;

  const tools: { icon: LucideIcon; label: string; on?: boolean; act: () => void }[] = [
    { icon: LocateFixed, label: t("kg.tb.reset"), act: () => scene.current?.resetView() },
    { icon: RotateCw, label: t("kg.tb.rotate"), on: toggles.autoRotate, act: () => flip("autoRotate") },
    { icon: Atom, label: t("kg.tb.physics"), on: toggles.physics, act: () => flip("physics") },
    { icon: Sparkles, label: t("kg.tb.particles"), on: toggles.particles, act: () => flip("particles") },
    { icon: Zap, label: t("kg.tb.shake"), act: () => scene.current?.shake() },
  ];
  if (canFull) tools.push({ icon: full ? Minimize2 : Maximize2, label: full ? t("kg.tb.exit_fullscreen") : t("kg.tb.fullscreen"), act: toggleFull });

  return (
    <div
      ref={root}
      className={`relative h-full w-full overflow-hidden ${full ? "bg-surface" : ""} ${className}`}
      role="application"
      aria-label={t("kg.aria", { n: data.nodes.length })}
    >
      <div ref={stage} className="absolute inset-0 isolate select-none" />
      <div
        ref={tooltip}
        className="pointer-events-none absolute top-0 left-0 z-10 max-w-[260px] rounded border border-line bg-raised/90 px-2.5 py-1.5 text-[13px] leading-snug opacity-0 shadow-lg backdrop-blur-sm transition-opacity duration-150"
        aria-hidden
      />
      <div ref={fps} className="cap pointer-events-none absolute top-2 left-3 z-10 hidden" aria-hidden />
      {!failed && (
        <div className="absolute top-2 right-2 z-10 flex gap-1 rounded-md border border-line bg-surface/80 p-1 backdrop-blur-sm" role="toolbar" aria-label={t("kg.tb.label")}>
          {tools.map(({ icon: Icon, label, on, act }) => (
            <button
              key={label}
              type="button"
              onClick={act}
              title={label}
              aria-label={label}
              aria-pressed={on}
              className={`grid h-8 w-8 place-items-center rounded transition-colors ${on === false ? "text-dim hover:text-ink-2" : on ? "bg-accent/15 text-accent hover:bg-accent/25" : "text-ink-2 hover:bg-raised hover:text-ink"}`}
            >
              <Icon size={16} strokeWidth={1.75} />
            </button>
          ))}
        </div>
      )}
      {toast && (
        <div key={toast.key} className="kg-toast pointer-events-none absolute top-3 left-1/2 z-10 rounded-full border border-accent/40 bg-surface/90 px-3 py-1 text-xs text-accent" role="status">
          {toast.text}
        </div>
      )}
      {failed && <p className="absolute inset-0 grid place-items-center text-xs text-ink-2">{t("kg.no_webgl")}</p>}
    </div>
  );
}
