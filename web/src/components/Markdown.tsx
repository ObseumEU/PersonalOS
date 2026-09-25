import type { ReactNode } from "react";

/**
 * A small markdown renderer: headings, paragraphs, lists, quotes, code blocks,
 * and inline code, bold, italic and links. It builds React elements (never
 * raw HTML), so a note cannot inject markup or scripts.
 */
export default function Markdown({ text }: { text: string }) {
  return <div className="flex flex-col gap-3 text-[14px] leading-relaxed">{blocks(text)}</div>;
}

function blocks(text: string): ReactNode[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(
        <pre key={out.length} className="overflow-x-auto rounded border border-line bg-bg p-3 font-mono text-[12px]">
          {code.join("\n")}
        </pre>,
      );
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const size = ["text-2xl font-light", "text-xl font-light", "text-base font-medium", "text-sm font-medium"][h[1].length - 1];
      out.push(
        <p key={out.length} role="heading" aria-level={h[1].length} className={`${size} tracking-[-0.01em]`}>
          {inline(h[2])}
        </p>,
      );
      i++;
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
      const List = ordered ? "ol" : "ul";
      out.push(
        <List key={out.length} className={`flex flex-col gap-1 pl-5 ${ordered ? "list-decimal" : "list-disc"} marker:text-ink-3`}>
          {items.map((it, k) => {
            const task = it.match(/^\[([ xX])\]\s+(.*)$/);
            return (
              <li key={k}>
                {task ? (
                  <span className={task[1] === " " ? "" : "text-ink-3 line-through"}>
                    {task[1] === " " ? "☐ " : "☑ "}
                    {inline(task[2])}
                  </span>
                ) : (
                  inline(it)
                )}
              </li>
            );
          })}
        </List>,
      );
      continue;
    }
    if (line.startsWith(">")) {
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) quote.push(lines[i++].replace(/^>\s?/, ""));
      out.push(
        <blockquote key={out.length} className="border-l-2 border-accent pl-3 text-ink-2">
          {inline(quote.join(" "))}
        </blockquote>,
      );
      continue;
    }
    if (/^(-{3,}|\*{3,})\s*$/.test(line)) {
      out.push(<hr key={out.length} className="border-line" />);
      i++;
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i])) para.push(lines[i++]);
    out.push(<p key={out.length}>{inline(para.join(" "))}</p>);
  }
  return out;
}

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*|_[^_]+_)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    const s = m[0];
    const k = out.length;
    if (m[1]) out.push(<code key={k} className="rounded bg-raised px-1 font-mono text-[12px]">{s.slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={k} className="font-medium">{s.slice(2, -2)}</strong>);
    else if (m[3]) out.push(<em key={k}>{s.slice(1, -1)}</em>);
    else {
      const [, label, href] = s.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/)!;
      // Only web and in-app links; javascript: and the like stay plain text.
      out.push(
        /^(https?:\/\/|\/(?!\/))/.test(href) ? (
          <a key={k} href={href} target={href.startsWith("/") ? undefined : "_blank"} rel="noreferrer noopener" className="text-accent underline-offset-2 hover:underline">
            {label}
          </a>
        ) : (
          s
        ),
      );
    }
    last = m.index! + s.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
