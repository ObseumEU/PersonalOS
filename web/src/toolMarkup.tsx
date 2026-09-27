import { Fragment, type ReactNode } from "react";

/**
 * Tool calls a model wrote as text ("pseudo tool calls", pos.pseudo_tools on the server):
 * `<function_calls>`, `<invoke name="bash">`, `<parameter name="command">…`, with or without
 * an `antml:` prefix. Nothing of it ran, so it never shows as content: each block becomes a
 * small muted chip "nástroj: bash …". Markup inside Markdown code (``` or `inline`) is kept.
 */

const P = "(?:antml:)?";
const END = "(?:$(?![\\s\\S]))";
const NAMES = "(?:function_calls|function_results?|tool_calls?|tool_use|tool_results?)";
const BLOCK = new RegExp(`<${P}(function_calls|tool_calls?|tool_use)\\b[^>]*>[\\s\\S]*?(?:</${P}\\1\\s*>|${END})`, "gi");
const RESULTS = new RegExp(`<${P}(function_results?|tool_results?)\\b[^>]*>[\\s\\S]*?(?:</${P}\\1\\s*>|${END})`, "gi");
const INVOKE = new RegExp(`<${P}invoke\\s+name\\s*=[^>]*>[\\s\\S]*?(?:</${P}invoke\\s*>|${END})`, "gi");
const PARAM = new RegExp(`<${P}parameter\\s+name\\s*=[^>]*>[\\s\\S]*?(?:</${P}parameter\\s*>|${END})`, "gi");
const STRAY = new RegExp(
  `</?${P}(?:${NAMES}|invoke|parameter)\\b[^>]*>?|</?${P}(?:fun|func|funct|functi|functio|function|function_|inv|invo|invok|par|para|param|parame|paramet|paramete)…?$`,
  "gi",
);
const DETECT = new RegExp(`<\\s*/?\\s*${P}(?:${NAMES}\\b|invoke\\s+name\\s*=|parameter\\s+name\\s*=)`, "i");
const CODE = /(```[\s\S]*?(?:```|$(?![\s\S]))|`[^`\n]*`)/;
const TOOL = new RegExp(`<${P}invoke\\s+name\\s*=\\s*["']?([\\w.:-]+)`, "i");

// A private-use character pair around a tool name marks where a chip goes.
const OPEN = "";
const CLOSE = "";
const MARK = new RegExp(`${OPEN}([^${CLOSE}]*)${CLOSE}`, "g");

export function hasToolMarkup(text: string | null | undefined): boolean {
  if (!text || !text.includes("<")) return false;
  return text.split(CODE).some((part, i) => i % 2 === 0 && DETECT.test(part));
}

function toolOf(block: string): string {
  return (block.match(TOOL)?.[1] ?? "").replace(/^mcp__/, "").slice(0, 40);
}

/** The text with each tool-call block replaced by an internal marker (see chipLabel). */
function marked(text: string): string {
  if (!hasToolMarkup(text)) return text ?? "";
  const mark = (m: string) => ` ${OPEN}${toolOf(m)}${CLOSE} `;
  const out = text
    .split(CODE)
    .map((part, i) =>
      i % 2
        ? part
        : part.replace(BLOCK, mark).replace(RESULTS, " ").replace(INVOKE, mark).replace(PARAM, " ").replace(STRAY, " "),
    )
    .join("");
  return out
    .replace(new RegExp(`(${OPEN}[^${CLOSE}]*${CLOSE})(\\s*${OPEN}[^${CLOSE}]*${CLOSE})+`, "g"), "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function chipLabel(tool: string): string {
  return tool ? `nástroj: ${tool} …` : "nástroj …";
}

/** Plain text: each tool call becomes "[nástroj: bash …]". */
export function stripToolMarkup(text: string | null | undefined): string {
  return marked(text ?? "").replace(MARK, (_, tool: string) => `[${chipLabel(tool)}]`);
}

/** Markdown: each tool call becomes a link to #pos-tool, which components/Markdown shows as a chip. */
export function toolMarkupToMarkdown(text: string | null | undefined): string {
  return marked(text ?? "").replace(MARK, (_, tool: string) => `[${chipLabel(tool)}](#pos-tool)`);
}

export function ToolChip({ label }: { label: string }) {
  return (
    <span
      className="mx-0.5 inline-block rounded-[3px] border border-line px-1 align-baseline font-mono text-[11px] text-ink-2 opacity-80"
      title="Model napsal volání nástroje jako text; nic se nespustilo."
    >
      {label}
    </span>
  );
}

/** Plain agent-authored text with chips in place of tool calls written as text. */
export function ToolText({ text }: { text: string | null | undefined }): ReactNode {
  const src = marked(text ?? "");
  if (!src.includes(OPEN)) return src;
  const parts = src.split(MARK);
  return parts.map((p, i) => (i % 2 ? <ToolChip key={i} label={chipLabel(p)} /> : <Fragment key={i}>{p}</Fragment>));
}
