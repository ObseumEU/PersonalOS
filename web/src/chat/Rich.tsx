import { Fragment, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { RefChip } from "../components/RefPreview";
import { t } from "../i18n/core";
import { findRefs } from "../refs";
import { ToolChip, stripToolMarkup } from "../toolMarkup";

/*
 * Markdown for a chat bubble, compact: paragraphs, lists, headings (as bold lines), quotes,
 * code (inline and fenced), bold, italic, links, @mentions, T-123 (opens the task panel), the chips
 * for tool calls written as text, and references (a note, a message, a knowledge-base passage, a file:
 * refs.ts) as chips that open a preview in place. Small on purpose: it ships in the /m bundle.
 */

/** Plain text with its references (not tasks: those are linked below) as preview chips. */
function withRefs(s: string, key: string, tone: Tone): ReactNode[] {
  const refs = findRefs(s).filter((r) => r.kind !== "task");
  if (!refs.length) return [<Fragment key={key}>{s}</Fragment>];
  const out: ReactNode[] = [];
  let at = 0;
  refs.forEach((r, i) => {
    if (r.start < at) {
      out.push(<RefChip key={`${key}-r${i}`} kind={r.kind} id={r.id} label={r.id} tone={tone} />);
      return;
    }
    if (r.start > at) out.push(<Fragment key={`${key}-t${i}`}>{s.slice(at, r.start)}</Fragment>);
    out.push(<RefChip key={`${key}-r${i}`} kind={r.kind} id={r.id} label={r.kind === "chunk" ? undefined : r.raw.replace(/\*\*/g, "")} tone={tone} />);
    at = r.end;
  });
  if (at < s.length) out.push(<Fragment key={`${key}-end`}>{s.slice(at)}</Fragment>);
  return out;
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

type Tone = "mine" | "theirs";

function useInline(names: string[], tone: Tone) {
  const re = useMemo(() => {
    const mention = names.length ? `@(?:${[...names].sort((a, b) => b.length - a.length).map(escapeRe).join("|")})` : "@\\w+";
    return new RegExp(
      [
        "\\[nástroj[^\\]\\n]*\\]",
        "`[^`\\n]+`",
        "\\*\\*[^*\\n]+\\*\\*",
        "__[^_\\n]+__",
        "(?<![\\w*])\\*(?![\\s*])[^*\\n]+?\\*(?![\\w*])",
        "(?<![\\w_])_(?![\\s_])[^_\\n]+?_(?![\\w_])",
        "\\[[^\\]\\n]+\\]\\(https?://[^)\\s]+\\)",
        "https?://[^\\s<>)]+[^\\s<>).,;:!?\"']",
        "\\bT-\\d{1,6}\\b",
        mention,
      ]
        .map((p) => `(?:${p})`)
        .join("|")
        .replace(/^(.*)$/s, "($1)"),
      "gi",
    );
  }, [names]);
  const mine = tone === "mine";
  const codeCls = mine ? "bg-black/15" : "bg-bg/70";
  const linkCls = mine ? "underline decoration-current/50" : "text-accent underline decoration-accent/40";
  const chipCls = mine ? "bg-black/15" : "bg-accent/15 text-accent";
  const render = (s: string, key: string): ReactNode[] =>
    s.split(re).map((part, i) => {
      const k = `${key}-${i}`;
      if (i % 2 === 0) return part ? <Fragment key={k}>{withRefs(part, k, tone)}</Fragment> : null;
      if (part.startsWith("[nástroj")) return <ToolChip key={k} label={part.slice(1, -1)} />;
      if (part.startsWith("`")) {
        const inner = part.slice(1, -1).trim();
        const refs = findRefs(inner).filter((r) => r.kind === "chunk");
        if (refs.length && !inner.replace(/[\w.-]+:c\d+(?:\/c\d+)*/g, "").replace(/[\s,;]+/g, ""))
          return <Fragment key={k}>{refs.map((r, j) => <RefChip key={`${k}-${j}`} kind="chunk" id={r.id} tone={tone} />)}</Fragment>;
      }
      if (part.startsWith("`")) return <code key={k} className={`rounded-[4px] px-1 font-mono text-[0.86em] ${codeCls}`}>{part.slice(1, -1)}</code>;
      if (part.startsWith("**") || part.startsWith("__")) return <strong key={k} className="font-semibold">{render(part.slice(2, -2), k)}</strong>;
      if (part.startsWith("*") || part.startsWith("_")) return <em key={k}>{part.slice(1, -1)}</em>;
      if (part.startsWith("[")) {
        const m = /^\[([^\]]+)\]\((.+)\)$/.exec(part);
        if (m) return <a key={k} href={m[2]} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()} className={linkCls}>{m[1]}</a>;
      }
      if (/^https?:/i.test(part))
        return <a key={k} href={part} target="_blank" rel="noreferrer noopener" onClick={(e) => e.stopPropagation()} className={`break-all ${linkCls}`}>{part.replace(/^https?:\/\//, "")}</a>;
      if (/^T-\d+$/i.test(part))
        return <Link key={k} to={`?task=${part.toUpperCase()}`} onClick={(e) => e.stopPropagation()} className={`rounded-[4px] px-1 font-mono text-[0.86em] ${chipCls}`}>{part.toUpperCase()}</Link>;
      return <span key={k} className={`rounded-[4px] px-0.5 ${chipCls}`}>{part}</span>;
    });
  return render;
}

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string }
  | { kind: "quote"; text: string }
  | { kind: "code"; text: string }
  | { kind: "list"; ordered: boolean; start: number; items: string[] };

const LIST = /^\s{0,3}([-*•+]|\d{1,3}[.)])\s+(.*)$/;

export function parseBlocks(src: string): Block[] {
  const out: Block[] = [];
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push({ kind: "p", text: para.join("\n") });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code: string[] = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
      out.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    const h = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line);
    if (h) {
      flush();
      out.push({ kind: "h", text: h[1].replace(/\s*#+\s*$/, "") });
      continue;
    }
    if (/^\s{0,3}>/.test(line)) {
      flush();
      const q: string[] = [];
      for (; i < lines.length && /^\s{0,3}>/.test(lines[i]); i++) q.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
      i--;
      out.push({ kind: "quote", text: q.join("\n") });
      continue;
    }
    const li = LIST.exec(line);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const items: string[] = [];
      const start = ordered ? parseInt(li[1], 10) : 1;
      for (; i < lines.length; i++) {
        const m = LIST.exec(lines[i]);
        if (m && /\d/.test(m[1]) === ordered) items.push(m[2]);
        else if (items.length && /^\s{2,}\S/.test(lines[i])) items[items.length - 1] += `\n${lines[i].trim()}`;
        else break;
      }
      i--;
      out.push({ kind: "list", ordered, start, items });
      continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flush();
      continue; // a horizontal rule: a paragraph break is enough in a bubble
    }
    para.push(line);
  }
  flush();
  return out;
}

/** Long messages open collapsed ("zobrazit víc"). */
export const isLong = (text: string) => text.length > 700 || text.split("\n").length > 14;

export function Rich({ text, names, tone = "theirs", collapsible = true }: { text: string; names: string[]; tone?: Tone; collapsible?: boolean }) {
  const inline = useInline(names, tone);
  const clean = useMemo(() => stripToolMarkup(text), [text]);
  const blocks = useMemo(() => parseBlocks(clean), [clean]);
  const long = collapsible && isLong(clean);
  const [open, setOpen] = useState(false);
  const body = (
    <div className="flex flex-col gap-1.5 text-[15px] leading-[1.45] break-words [overflow-wrap:anywhere]">
      {blocks.map((b, i) => {
        const k = String(i);
        switch (b.kind) {
          case "code":
            return <pre key={k} className={`overflow-x-auto rounded-md p-2 font-mono text-[12.5px] leading-snug ${tone === "mine" ? "bg-black/15" : "bg-bg/70"}`}>{b.text}</pre>;
          case "h":
            return <p key={k} className="font-semibold">{inline(b.text, k)}</p>;
          case "quote":
            return <p key={k} className="border-l-2 border-current/30 pl-2 whitespace-pre-line opacity-80">{inline(b.text, k)}</p>;
          case "list": {
            const Tag = b.ordered ? "ol" : "ul";
            return (
              <Tag key={k} start={b.ordered ? b.start : undefined} className={`flex flex-col gap-0.5 pl-5 ${b.ordered ? "list-decimal" : "list-disc"} marker:opacity-60`}>
                {b.items.map((it, j) => <li key={j} className="whitespace-pre-line pl-0.5">{inline(it, `${k}-${j}`)}</li>)}
              </Tag>
            );
          }
          default:
            return <p key={k} className="whitespace-pre-line">{inline(b.text, k)}</p>;
        }
      })}
    </div>
  );
  if (!long) return body;
  return (
    <div>
      <div className={open ? "" : "relative max-h-[15.5rem] overflow-hidden [mask-image:linear-gradient(to_bottom,black_70%,transparent)]"}>{body}</div>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        className={`mt-1 text-[13px] font-medium ${tone === "mine" ? "underline" : "text-accent"}`}
      >
        {open ? t("m.chat.show_less") : t("m.chat.show_more")}
      </button>
    </div>
  );
}
