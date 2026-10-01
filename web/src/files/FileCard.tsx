/**
 * A file in chat (and in Files): a card with an inline preview, and a full-screen viewer (zoom
 * and pan for pictures and diagrams, download, "Otevřít v Souborech", the version). Used by the
 * desktop chat and the phone app (/m): this file stays small; Mermaid, Vega-Lite, Markdown, CSV
 * and code come from the lazy ./renderers chunk only when one is on screen.
 *
 * Safety: pictures load as <img> (an SVG there runs no script; the server sanitised it anyway),
 * DOT is drawn by the server, an HTML page runs in a sandboxed frame (scripts only, an opaque
 * origin, no network: the server's CSP), PDFs in the browser's viewer.
 */
import { Download, ExternalLink, FileCode2, FileText, Image as ImageIcon, Maximize2, Minus, Network, Plus, RotateCcw, Sheet, X, BarChart3, Globe } from "lucide-react";
import { Suspense, lazy, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { type FileRef, type PreviewKind, HEAVY, KIND_LABEL, ZOOMABLE, fileUrl, fmtBytes, kindOf } from "./kinds";

const Rendered = lazy(() => import("./renderers"));

const ICON: Partial<Record<PreviewKind, typeof FileText>> = {
  image: ImageIcon, svg: ImageIcon, mermaid: Network, dot: Network, vegalite: BarChart3, csv: Sheet, code: FileCode2,
  html: Globe,
};

function DotImage({ f, full }: { f: FileRef; full: boolean }) {
  const [error, setError] = useState<string | null>(null);
  if (error) return <div className="rounded-lg border border-amber-400/40 bg-amber-400/5 p-3 text-[12.5px] text-amber-200">Diagram nejde vykreslit: {error}</div>;
  return (
    <img
      src={fileUrl.render(f.id, f.version)}
      alt={f.name}
      draggable={false}
      className={`${full ? "max-w-none" : "max-h-80 w-full object-contain"} rounded-lg bg-white/95 p-2`}
      onError={() =>
        fetch(fileUrl.render(f.id, f.version), { credentials: "same-origin" })
          .then((r) => r.json())
          .then((b) => setError(String(b.detail ?? "chyba")), () => setError("chyba"))
      }
    />
  );
}

/** The file drawn: compact in a card, large in the viewer. */
export function FilePreview({ file, full = false }: { file: FileRef; full?: boolean }) {
  const kind = kindOf(file);
  if (kind === "image" || kind === "svg")
    return (
      <img
        src={fileUrl.content(file.id, file.version)}
        alt={file.name}
        loading="lazy"
        draggable={false}
        className={full ? "max-w-none" : `max-h-80 w-full rounded-lg object-contain ${kind === "svg" ? "bg-white/95 p-1" : ""}`}
      />
    );
  if (kind === "dot") return <DotImage f={file} full={full} />;
  if (kind === "html")
    return (
      <iframe
        src={fileUrl.page(file.id, file.version)}
        title={file.name}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        className={`w-full rounded-lg border border-line bg-white ${full ? "h-full" : "h-64 pointer-events-none"}`}
      />
    );
  if (kind === "pdf")
    return full ? (
      <iframe src={fileUrl.content(file.id, file.version)} title={file.name} className="h-full w-full rounded-lg bg-white" />
    ) : (
      <div className="flex h-24 items-center justify-center gap-2 rounded-lg border border-line bg-bg/60 text-[13px] text-ink-2">
        <FileText size={22} /> PDF · {fmtBytes(file.size)}
      </div>
    );
  if (HEAVY.includes(kind))
    return (
      <Suspense fallback={<div className="h-24 animate-pulse rounded-lg bg-raised/60" />}>
        <Rendered file={file} kind={kind} full={full} />
      </Suspense>
    );
  return null;
}

/** Zoom (wheel, buttons, pinch) and pan (drag) for pictures and diagrams; double-click resets. */
function ZoomPan({ children }: { children: ReactNode }) {
  const [z, setZ] = useState({ s: 1, x: 0, y: 0 });
  const pts = useRef(new Map<number, { x: number; y: number }>());
  const last = useRef<{ d: number; x: number; y: number } | null>(null);
  const zoom = (f: number) => setZ((v) => ({ ...v, s: Math.min(8, Math.max(0.2, v.s * f)) }));
  return (
    <div className="relative h-full w-full overflow-hidden">
      <div
        className="flex h-full w-full touch-none items-center justify-center select-none"
        onWheel={(e) => zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15)}
        onDoubleClick={() => setZ({ s: 1, x: 0, y: 0 })}
        onPointerDown={(e) => {
          (e.target as Element).setPointerCapture?.(e.pointerId);
          pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
          last.current = null;
        }}
        onPointerMove={(e) => {
          const prev = pts.current.get(e.pointerId);
          if (!prev) return;
          pts.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
          const all = [...pts.current.values()];
          if (all.length === 2) {
            const d = Math.hypot(all[0].x - all[1].x, all[0].y - all[1].y);
            if (last.current) zoom(d / last.current.d);
            last.current = { d, x: 0, y: 0 };
          } else if (all.length === 1) {
            setZ((v) => ({ ...v, x: v.x + e.clientX - prev.x, y: v.y + e.clientY - prev.y }));
          }
        }}
        onPointerUp={(e) => {
          pts.current.delete(e.pointerId);
          last.current = null;
        }}
        onPointerCancel={(e) => pts.current.delete(e.pointerId)}
      >
        <div style={{ transform: `translate(${z.x}px, ${z.y}px) scale(${z.s})`, transformOrigin: "center" }} className="max-w-[95vw]">
          {children}
        </div>
      </div>
      <div className="absolute right-3 bottom-3 flex gap-1 rounded-lg border border-line bg-surface/90 p-1">
        <button type="button" aria-label="Zmenšit" className="p-1.5 hover:text-accent" onClick={() => zoom(1 / 1.25)}><Minus size={16} /></button>
        <button type="button" aria-label="Původní velikost" className="p-1.5 hover:text-accent" onClick={() => setZ({ s: 1, x: 0, y: 0 })}><RotateCcw size={16} /></button>
        <button type="button" aria-label="Zvětšit" className="p-1.5 hover:text-accent" onClick={() => zoom(1.25)}><Plus size={16} /></button>
      </div>
    </div>
  );
}

export function FileViewer({ file, onClose }: { file: FileRef; onClose: () => void }) {
  const kind = kindOf(file);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);
  const fill = kind === "html" || kind === "pdf";
  return createPortal(
    <div role="dialog" aria-modal="true" aria-label={file.name} className="fixed inset-0 z-[100] flex flex-col bg-bg/97 backdrop-blur-sm">
      <div className="flex min-h-12 items-center gap-2 border-b border-line px-3 pt-[env(safe-area-inset-top)]">
        <span className="min-w-0 flex-1 truncate text-[14px] font-medium">{file.name}</span>
        {file.version ? <span className="rounded border border-line px-1.5 text-[11px] text-ink-2">v{file.version}</span> : null}
        <a href={fileUrl.content(file.id, file.version, true)} aria-label="Stáhnout" title="Stáhnout" className="p-2 text-ink-2 hover:text-accent"><Download size={18} /></a>
        <a href={fileUrl.inFiles(file.id)} aria-label="Otevřít v Souborech" title="Otevřít v Souborech" className="flex items-center gap-1 p-2 text-[12px] text-ink-2 hover:text-accent">
          <ExternalLink size={16} /> <span className="hidden sm:inline">Otevřít v Souborech</span>
        </a>
        <button type="button" aria-label="Zavřít" onClick={onClose} className="p-2 text-ink-2 hover:text-ink"><X size={20} /></button>
      </div>
      {file.description && <p className="border-b border-line px-3 py-1.5 text-[12.5px] text-ink-2">{file.description}</p>}
      <div className={`min-h-0 flex-1 ${fill ? "p-2" : ZOOMABLE.includes(kind) ? "" : "overflow-auto p-3"}`}>
        {ZOOMABLE.includes(kind) ? (
          <ZoomPan>
            <FilePreview file={file} full />
          </ZoomPan>
        ) : kind === "download" ? (
          <p className="p-6 text-center text-ink-2">Tento typ nejde zobrazit. Stáhni si ho.</p>
        ) : (
          <FilePreview file={file} full />
        )}
      </div>
    </div>,
    document.body,
  );
}

/** A file in a chat message: name, kind, version, the preview; a click opens it full screen. */
export function FileCard({ file, mine = false }: { file: FileRef; mine?: boolean }) {
  const [open, setOpen] = useState(false);
  const kind = kindOf(file);
  const Icon = ICON[kind] ?? FileText;
  const stop = (e: { stopPropagation: () => void }) => e.stopPropagation();
  if (kind === "download")
    return (
      <a
        href={fileUrl.content(file.id, file.version, true)}
        onClick={stop}
        className={`flex h-10 max-w-full items-center gap-2 rounded-lg px-3 text-[13px] ${mine ? "bg-black/15" : "border border-line bg-bg/60"}`}
      >
        <FileText size={16} className="shrink-0 opacity-70" /> <span className="truncate">{file.name}</span>
      </a>
    );
  return (
    <div onClick={stop} className={`w-[460px] max-w-full overflow-hidden rounded-xl border text-left ${mine ? "border-black/20 bg-black/10" : "border-line bg-bg/70"} text-ink`}>
      <button type="button" onClick={() => setOpen(true)} className="flex w-full min-w-0 items-center gap-2 px-2.5 py-1.5 text-left text-[12.5px] hover:bg-raised/50" aria-label={`Otevřít ${file.name}`}>
        <Icon size={15} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate font-medium">{file.name}</span>
        <span className="shrink-0 text-[11px] text-ink-2">
          {KIND_LABEL[kind]}
          {file.version ? ` · v${file.version}` : ""}
        </span>
        <Maximize2 size={13} className="shrink-0 text-ink-2" />
      </button>
      <div role="button" tabIndex={0} onClick={() => setOpen(true)} onKeyDown={(e) => e.key === "Enter" && setOpen(true)} className="cursor-zoom-in px-2 pb-2">
        <FilePreview file={file} />
      </div>
      {open && <FileViewer file={file} onClose={() => setOpen(false)} />}
    </div>
  );
}
