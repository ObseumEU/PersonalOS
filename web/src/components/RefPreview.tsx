import { Download, ExternalLink, FileText, MessageSquare, Quote, SquareCheck, StickyNote, X } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { register, t } from "../i18n/core";
import report from "../i18n/cs/report";
import { closeRef, openRef, useCurrentRef } from "../refStore";
import { type RefKind, refLabel } from "../refs";
import { loadRef, type RefPreview as Preview } from "../reportApi";
import { TaskLink } from "../taskSheet";

/*
 * A reference in text (a note, a chat message, a knowledge-base passage, a file) as a chip; clicking
 * it opens a preview drawer over whatever is showing (the task panel, a note, the chat), so nothing
 * points somewhere the reader cannot see. Tasks open in the task panel itself.
 */

// The words for chips and the preview (the installed app loads this module only when needed).
register(report);

const Markdown = lazy(() => import("./Markdown"));

export { openRef } from "../refStore";

const ICON: Record<RefKind, typeof StickyNote> = { note: StickyNote, msg: MessageSquare, chunk: Quote, task: SquareCheck, file: FileText };

/** The chip's text once the reference is loaded: "Poznámka · Obseum AI: kontrola…", "$100M Leads · 33:39". */
function chipText(p: Preview | null, kind: RefKind, id: string, fallback: string): string {
  if (!p?.ok || !p.title) return fallback;
  if (kind === "chunk") return p.at ? `${p.title} · ${p.at}` : p.title;
  if (kind === "msg") return `${fallback} · ${p.author ?? ""}`.trim();
  return `${fallback} · ${p.title}`;
}

export function RefChip({ kind, id, label, tone = "theirs" }: { kind: RefKind; id: string; label?: string; tone?: "mine" | "theirs" }) {
  const [p, setP] = useState<Preview | null>(null);
  useEffect(() => {
    let alive = true;
    loadRef(kind, id).then((x) => alive && setP(x));
    return () => {
      alive = false;
    };
  }, [kind, id]);
  const base = label?.trim() || refLabel(kind, id);
  const text = chipText(p, kind, id, base);
  const Icon = ICON[kind];
  const cls = `inline-flex max-w-full items-baseline gap-1 rounded-md px-1.5 py-px align-baseline text-[0.92em] leading-snug no-underline ${
    tone === "mine" ? "bg-black/15 text-current" : "bg-accent/10 text-accent hover:bg-accent/20"
  } ${p && !p.ok ? "opacity-70" : ""}`;
  const inner = (
    <>
      <Icon size={12} aria-hidden className="shrink-0 translate-y-[1px]" />
      <span className="truncate">{text}</span>
    </>
  );
  if (kind === "task")
    return (
      <TaskLink taskRef={id} className={cls} title={p?.title}>
        {inner}
      </TaskLink>
    );
  return (
    <button
      type="button"
      className={cls}
      title={p && !p.ok ? t("ref.failed", { why: p.error ?? "" }) : t("ref.kind." + kind)}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        openRef(kind, id);
      }}
    >
      {inner}
    </button>
  );
}

function Body({ p }: { p: Preview }): ReactNode {
  if (!p.ok) return <p className="text-sm text-amber-200">{t("ref.failed", { why: p.error ?? "" })}</p>;
  const md = (text: string, compact = false) => (
    <Suspense fallback={<p className="text-sm whitespace-pre-wrap">{text}</p>}>
      <Markdown text={text} compact={compact} className="text-[14px]" />
    </Suspense>
  );
  if (p.kind === "chunk")
    return <blockquote className="border-l-2 border-accent/50 pl-3 text-[15px] leading-relaxed text-ink">{md(p.quote ?? p.text ?? "")}</blockquote>;
  if (p.kind === "msg")
    return (
      <div className="flex flex-col gap-3">
        {!!p.context?.length && (
          <div className="flex flex-col gap-2 border-l border-line pl-3 opacity-75">
            <span className="text-xs text-ink-2">{t("ref.before")}</span>
            {p.context.map((c) => (
              <div key={c.id} className="text-[13px]">
                <span className="font-medium text-ink-2">{c.author}: </span>
                <span className="whitespace-pre-wrap">{c.text.length > 400 ? `${c.text.slice(0, 400)}…` : c.text}</span>
              </div>
            ))}
          </div>
        )}
        <div className="rounded-lg border border-accent/40 bg-accent/[0.05] p-3">
          <p className="pb-1 text-xs text-ink-2">{p.author}</p>
          {md(p.text ?? "")}
        </div>
      </div>
    );
  if (p.kind === "file")
    return <pre className="max-h-[60vh] overflow-auto rounded-md border border-line bg-bg p-3 text-[12.5px] whitespace-pre-wrap">{p.text || "—"}</pre>;
  return md(p.text ?? "");
}

/** The drawer: rendered once (Shell, the installed app); on top of the task panel. */
export function RefPreviewHost() {
  const cur = useCurrentRef();
  const [p, setP] = useState<Preview | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setP(null);
    if (!cur) return;
    let alive = true;
    loadRef(cur.kind, cur.id).then((x) => alive && setP(x));
    const before = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        e.preventDefault();
        closeRef();
      }
    };
    window.addEventListener("keydown", onKey, true);
    setTimeout(() => box.current?.focus(), 0);
    return () => {
      alive = false;
      window.removeEventListener("keydown", onKey, true);
      if (before && document.body.contains(before)) before.focus();
    };
  }, [cur]);
  if (!cur) return null;
  const Icon = ICON[cur.kind];
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="presentation">
      <button type="button" aria-label={t("ref.close")} className="absolute inset-0 bg-black/50" onClick={closeRef} />
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        data-ref-preview=""
        aria-labelledby="ref-title"
        tabIndex={-1}
        className="relative flex h-full w-full max-w-[640px] flex-col border-l border-line bg-surface shadow-2xl outline-none"
      >
        <div className="flex items-start gap-3 border-b border-line px-4 py-3 sm:px-5">
          <Icon size={18} className="mt-1 shrink-0 text-accent" aria-hidden />
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="text-xs text-ink-2">
              {t("ref.kind." + cur.kind)}
              {p?.channel ? ` · ${p.channel}` : ""}
              {p?.at && cur.kind !== "chunk" ? ` · ${new Date(p.at).toLocaleString("cs-CZ", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" })}` : ""}
              {p?.at && cur.kind === "chunk" ? ` · ${p.at}` : ""}
            </span>
            <h2 id="ref-title" className="text-[17px] leading-snug break-words">
              {p?.title ?? refLabel(cur.kind, cur.id)}
            </h2>
          </div>
          <button type="button" className="btn h-9! px-2.5!" onClick={closeRef} aria-label={t("ref.close")}>
            <X size={16} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">{p ? <Body p={p} /> : <p className="text-sm text-ink-2">{t("ref.loading")}</p>}</div>
        {p?.ok && (p.link || p.download) && (
          <div className="flex flex-wrap gap-2 border-t border-line px-4 py-3 sm:px-5">
            {p.link &&
              (/^https?:/.test(p.link) ? (
                <a className="btn h-9!" href={p.link} target="_blank" rel="noreferrer noopener">
                  <ExternalLink size={14} /> {t("rp.open_source")}
                </a>
              ) : (
                <Link className="btn h-9!" to={p.link} onClick={closeRef}>
                  <ExternalLink size={14} /> {t("ref.open")}
                </Link>
              ))}
            {p.download && (
              <a className="btn h-9!" href={p.download}>
                <Download size={14} /> {t("ref.download")}
              </a>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
