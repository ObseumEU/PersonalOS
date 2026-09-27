import { markdownSnippet } from "../../markdownText";
import type { Task } from "../../tasksApi";

/** A Markdown text cut at its headings: [{heading, body}], the part before the first heading has none. */
export function sections(md: string): { heading: string | null; body: string }[] {
  const out: { heading: string | null; body: string }[] = [];
  let cur: { heading: string | null; lines: string[] } = { heading: null, lines: [] };
  let fence = false;
  for (const line of (md ?? "").replace(/\r\n/g, "\n").split("\n")) {
    if (/^\s*```/.test(line)) fence = !fence;
    const h = !fence && line.match(/^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$/);
    if (h) {
      if (cur.heading !== null || cur.lines.join("").trim()) out.push({ heading: cur.heading, body: cur.lines.join("\n").trim() });
      cur = { heading: h[1].replace(/\*\*/g, ""), lines: [] };
    } else cur.lines.push(line);
  }
  if (cur.heading !== null || cur.lines.join("").trim()) out.push({ heading: cur.heading, body: cur.lines.join("\n").trim() });
  return out;
}

export type AskOption = { text: string; recommended: boolean };
export type AskInfo = {
  question: string;
  why: string;
  context: string;
  options: AskOption[];
  recommendation: string;
  asker: string | null;
  sourceRef: string | null;
  blocking: boolean;
};

/** An owner ask ticket (pos.asks) read back from its description: the question, why, the options. */
export function parseAsk(notes: string, title: string): AskInfo {
  const parts = sections(notes);
  const get = (name: string) => parts.find((p) => p.heading?.toLowerCase().startsWith(name))?.body ?? "";
  const head = parts.find((p) => p.heading === null)?.body ?? "";
  const options: AskOption[] = [];
  for (const line of get("možnosti").split("\n")) {
    const m = line.match(/^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$/);
    if (!m) continue;
    const recommended = /—\s*\*?doporučuju\*?\s*$/i.test(m[1]);
    options.push({ text: m[1].replace(/\s*—\s*\*?doporučuju\*?\s*$/i, "").trim(), recommended });
  }
  return {
    question: markdownSnippet(get("co potřebuju"), 400) || title,
    why: markdownSnippet(get("proč"), 400),
    context: get("souvislosti"),
    options,
    recommendation: markdownSnippet(get("moje doporučení"), 300),
    asker: head.match(/\*\*Ptá se:\*\*\s*([^·*]+?)\s*·/)?.[1] ?? null,
    sourceRef: head.match(/\*\*K úkolu:\*\*\s*(T-\d+)/)?.[1] ?? null,
    blocking: /\*\*Blokuje:\*\*/.test(head),
  };
}

// Labels agents put at the start of a description ("Purpose:", "**Proč**", "Zdroj:") say nothing in a one-line preview.
const LEAD = /^(?:(?:purpose|source|from|context|účel|proč|odkud|zdroj|kontext|co se stalo|co|stav|zadání)\s*:?\s+)+/i;
// A first paragraph that only says where the task came from ("Zdroj: T-147 …").
const META = /^\s*(?:\*\*)?(?:zdroj|source|odkud|from)(?:\*\*)?\s*:[^\n]*\n+/i;

/** One calm line for the list: the cached summary, else the result or the description's first words. */
export function oneLine(task: Pick<Task, "summary" | "notes" | "progress_note" | "status" | "source" | "description_generated">, max = 160): string {
  if (task.summary) return markdownSnippet(task.summary, max);
  if (task.source === "ask_owner") {
    const who = task.notes?.match(/\*\*Ptá se:\*\*\s*([^·*]+?)\s*·/)?.[1];
    const ref = task.notes?.match(/\*\*K úkolu:\*\*\s*(T-\d+)/)?.[1];
    const why = markdownSnippet(sections(task.notes ?? "").find((p) => p.heading?.toLowerCase().startsWith("proč"))?.body ?? "", max);
    if (why) return why;
    if (who) return `${who}${ref ? ` · ${ref}` : ""}`;
  }
  const result = (task.status === "review" || task.status === "done") && task.progress_note ? task.progress_note : "";
  // A description PersonalOS wrote itself (in English, from the fields) says nothing new.
  const notes = task.description_generated ? "" : (task.notes ?? "").replace(META, "");
  const text = markdownSnippet((result || notes || task.progress_note || "").replace(/^_Generated from the task's fields.*$/m, ""), max + 40);
  return markdownSnippet(text.replace(LEAD, ""), max);
}

/** The definition of done as check items (one per line, bullet or sentence). */
export function doneItems(dod: string | null | undefined): string[] {
  const text = (dod ?? "").trim();
  if (!text) return [];
  const lines = text
    .split(/\n+/)
    .map((l) => l.replace(/^\s*(?:[-*+]|\d+[.)]|\[[ xX]\])\s*/, "").trim())
    .filter(Boolean);
  if (lines.length > 1) return lines;
  return text.split(/;\s+/).map((s) => s.trim()).filter(Boolean);
}

/** A short age: "teď", "5 min", "3 h", "2 d", "4 týd". */
export function shortAge(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "teď";
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} d`;
  return `${Math.round(s / (86400 * 7))} týd`;
}
