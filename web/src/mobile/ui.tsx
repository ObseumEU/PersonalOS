import { ArrowLeft } from "lucide-react";
import { Fragment, useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { LOCALE } from "../i18n/core";
import { ToolChip, stripToolMarkup } from "../toolMarkup";

/** The top bar of a screen: back arrow (optional), title and subtitle, actions on the right. */
export function TopBar({ title, sub, back, right, icon }: { title: ReactNode; sub?: ReactNode; back?: () => void; right?: ReactNode; icon?: ReactNode }) {
  return (
    <header className="sticky top-0 z-20 flex min-h-14 items-center gap-2 border-b border-line bg-bg/95 px-2 pt-[env(safe-area-inset-top)] backdrop-blur">
      {back ? (
        <button onClick={back} aria-label="Zpět" className="grid h-11 w-11 shrink-0 place-items-center rounded-full text-ink-2 active:bg-raised">
          <ArrowLeft size={20} />
        </button>
      ) : (
        <span className="w-2" />
      )}
      {icon}
      <div className="flex min-w-0 flex-1 flex-col py-1.5">
        <h1 className="truncate text-[17px] font-medium leading-tight">{title}</h1>
        {sub && <div className="truncate text-[12px] leading-tight text-ink-2">{sub}</div>}
      </div>
      {right && <div className="flex shrink-0 items-center gap-1 pr-1">{right}</div>}
    </header>
  );
}

const HUES = [196, 160, 32, 280, 340, 220, 120, 12];

/** A round initial, colour from the name (stable). */
export function Avatar({ name, size = 40, human = false }: { name: string; size?: number; human?: boolean }) {
  const hue = HUES[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % HUES.length];
  return (
    <span
      aria-hidden
      className="grid shrink-0 place-items-center rounded-full font-medium"
      style={{
        width: size,
        height: size,
        fontSize: size * 0.4,
        background: human ? "var(--color-raised)" : `hsl(${hue} 45% 22%)`,
        color: human ? "var(--color-ink)" : `hsl(${hue} 70% 78%)`,
        border: "1px solid var(--color-line)",
      }}
    >
      {name.trim().charAt(0).toUpperCase() || "?"}
    </span>
  );
}

export function when(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const days = (now.getTime() - d.getTime()) / 864e5;
  if (days < 6) return d.toLocaleDateString(LOCALE, { weekday: "short" });
  return d.toLocaleDateString(LOCALE, { day: "numeric", month: "numeric" });
}

export function Dots({ soft = false }: { soft?: boolean }) {
  return (
    <span className={`typing-dots ${soft ? "soft" : ""}`} aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Light markdown for chat (like the desktop chat): code, bold, links, @mentions and T-123 (opens the task panel). */
export function Body({ text, names }: { text: string; names: string[] }) {
  const inline = useMemo(() => {
    const mention = names.length ? `@(?:${[...names].sort((a, b) => b.length - a.length).map(escapeRe).join("|")})` : "@\\w+";
    return new RegExp(`(\\[nástroj[^\\]\\n]*\\]|\`[^\`\\n]+\`|\\*\\*[^*\\n]+\\*\\*|https?://[^\\s<]+|\\bT-\\d{1,6}\\b|${mention})`, "gi");
  }, [names]);
  const renderInline = (s: string, key: string): ReactNode[] =>
    s.split(inline).map((part, i) => {
      const k = `${key}-${i}`;
      if (i % 2 === 0) return <Fragment key={k}>{part}</Fragment>;
      if (part.startsWith("[nástroj")) return <ToolChip key={k} label={part.slice(1, -1)} />;
      if (part.startsWith("`")) return <code key={k} className="rounded-[3px] bg-bg/60 px-1 font-mono text-[13px]">{part.slice(1, -1)}</code>;
      if (part.startsWith("**")) return <strong key={k} className="font-medium">{part.slice(2, -2)}</strong>;
      if (part.startsWith("http")) return <a key={k} href={part} target="_blank" rel="noreferrer noopener" className="break-all text-accent underline decoration-accent/40">{part}</a>;
      if (/^T-\d+$/i.test(part))
        return <Link key={k} to={`?task=${part.toUpperCase()}`} className="rounded-[3px] bg-accent/10 px-1 font-mono text-[13px] text-accent">{part.toUpperCase()}</Link>;
      return <span key={k} className="rounded-[3px] bg-accent/15 px-0.5 text-accent">{part}</span>;
    });
  const blocks = stripToolMarkup(text).split(/```(?:\w+\n)?/);
  return (
    <div className="text-[15px] leading-relaxed break-words whitespace-pre-wrap">
      {blocks.map((b, i) =>
        i % 2 === 1 ? (
          <pre key={i} className="my-1 overflow-x-auto rounded border border-line bg-bg p-2 font-mono text-[12px]">{b.replace(/\n$/, "")}</pre>
        ) : (
          <Fragment key={i}>{renderInline(b, String(i))}</Fragment>
        ),
      )}
    </div>
  );
}

/** A bottom sheet with big buttons (the actions on a message). */
export function ActionSheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-40 bg-black/60" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="sheet-backdrop absolute inset-x-0 bottom-0 rounded-t-2xl border-t border-line bg-surface px-3 pt-2 pb-[max(16px,env(safe-area-inset-bottom))]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-line" />
        <div className="flex flex-col">{children}</div>
      </div>
    </div>
  );
}

export function SheetButton({ icon, children, onClick, danger }: { icon?: ReactNode; children: ReactNode; onClick: () => void; danger?: boolean }) {
  return (
    <button onClick={onClick} className={`flex h-13 items-center gap-3 rounded-lg px-3 text-left text-[16px] active:bg-raised ${danger ? "text-amber-300" : "text-ink"}`}>
      <span className="text-ink-2">{icon}</span>
      {children}
    </button>
  );
}
