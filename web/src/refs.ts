/**
 * References in agents' text (the same patterns as backend pos/refs.py): "Poznámka 23", "note:23",
 * "msg 1095", "zpráva 504", knowledge-base chunk ids (`firma.gh_…:c13`, `k834E6OTGTM:c17`,
 * `kniha.gh_…:c0/c2`), "T-431" and "file:12". They become chips that open a preview in place
 * (components/RefPreview.tsx), so nothing points somewhere the reader cannot see.
 *
 * Small and dependency-free: the chat bubble renderer in the /m bundle uses it too.
 */

export type RefKind = "note" | "msg" | "chunk" | "task" | "file";
export type Ref = { kind: RefKind; id: string; raw: string; start: number; end: number };

const CHUNK = String.raw`(?<![\w/.:#-])((?:[a-z][a-z0-9]{1,20}\.)?[A-Za-z0-9_-]{6,64}):c(\d{1,5})((?:/c\d{1,5})*)\b`;
const NOTE = String.raw`(?<![\w-])(?:note|notes|pozn(?:á|a)m(?:k|ek)\w*|pozn\.)\s*(?::|#|č\.)?\s*\**\s*#?(\d{1,6})\**((?:\s*(?:,|a|and|i|/)\s*\**#?\d{1,6}\**)*)`;
const MSG = String.raw`(?<![\w-])(?:(?:msg|message|messages)\s*(?::|#|č\.)?\s*\**#?(\d{1,9})|zpr(?:á|a)v\w*\s*(?:#|č\.\s*)?\**(\d{3,9}))\b`;
const TASK = String.raw`(?<![\w-])T-(\d{1,6})\b`;
const FILE = String.raw`(?<![\w-])(?:file\s*[:#]\s*|soubor\w*\s+#)(\d{1,9})\b`;

// \w in JS is ASCII-only: Czech words after "pozn" / "zpráv" need the unicode letters too.
const fix = (s: string) => s.replace(/\\w\*/g, "[\\p{L}\\d_]*");

/** Every reference in the text, in order (several numbers of one mention each give a ref). */
export function findRefs(text: string): Ref[] {
  const out: Ref[] = [];
  const src = text ?? "";
  for (const m of src.matchAll(new RegExp(CHUNK, "g"))) {
    const doc = m[1];
    if (/^(http|www)/i.test(doc)) continue;
    const nums = [m[2], ...((m[3] ?? "").match(/\d+/g) ?? [])];
    for (const n of nums) out.push({ kind: "chunk", id: `${doc}:c${n}`, raw: m[0], start: m.index!, end: m.index! + m[0].length });
  }
  for (const m of src.matchAll(new RegExp(fix(NOTE), "giu"))) {
    const nums = [m[1], ...((m[2] ?? "").match(/\d+/g) ?? [])];
    for (const n of nums) out.push({ kind: "note", id: n, raw: m[0], start: m.index!, end: m.index! + m[0].length });
  }
  for (const m of src.matchAll(new RegExp(fix(MSG), "giu")))
    out.push({ kind: "msg", id: m[1] ?? m[2], raw: m[0], start: m.index!, end: m.index! + m[0].length });
  for (const m of src.matchAll(new RegExp(TASK, "g")))
    out.push({ kind: "task", id: `T-${Number(m[1])}`, raw: m[0], start: m.index!, end: m.index! + m[0].length });
  for (const m of src.matchAll(new RegExp(fix(FILE), "giu")))
    out.push({ kind: "file", id: m[1], raw: m[0], start: m.index!, end: m.index! + m[0].length });
  out.sort((a, b) => a.start - b.start);
  const seen = new Set<string>();
  return out.filter((r) => {
    const k = `${r.kind}:${r.id}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

export const refKey = (r: { kind: RefKind; id: string }) => `${r.kind}:${r.id}`;
export const REF_HREF = "#pos-ref/";

/** The chip's visible label: what kind of thing, in words, plus the number. */
export function refLabel(kind: RefKind, id: string): string {
  switch (kind) {
    case "note":
      return `poznámka ${id}`;
    case "msg":
      return `zpráva ${id}`;
    case "file":
      return `soubor ${id}`;
    case "task":
      return id;
    default:
      return "citace";
  }
}

/**
 * Markdown with each reference outside code and links turned into a link `[label](#pos-ref/kind/id)`;
 * an inline code span that is only references (`k834E6OTGTM:c17`) becomes those links too. Task refs
 * are left to the task linker (they open the task panel).
 */
export function linkRefsMarkdown(md: string): string {
  return md
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`|\[[^\]\n]*\]\([^)\n]*\))/)
    .map((part, i) => {
      if (i % 2) {
        if (!part.startsWith("`") || part.startsWith("```")) return part;
        const inner = part.slice(1, -1).trim();
        const found = findRefs(inner).filter((r) => r.kind !== "task");
        const covered = found.length && inner.replace(new RegExp(CHUNK, "g"), "").replace(/[\s,;/]+/g, "") === "";
        if (!covered) return part;
        return found.map((r) => `[${refLabel(r.kind, r.id)}](${REF_HREF}${r.kind}/${encodeURIComponent(r.id)})`).join(" ");
      }
      const refs = findRefs(part).filter((r) => r.kind !== "task");
      if (!refs.length) return part;
      // Replace each mention once, right to left so the offsets stay valid.
      const spans = new Map<number, Ref[]>();
      for (const r of refs) spans.set(r.start, [...(spans.get(r.start) ?? []), r]);
      let out = part;
      [...spans.entries()]
        .sort((a, b) => b[0] - a[0])
        .forEach(([start, rs]) => {
          const raw = rs[0].raw;
          const links = rs.map((r) => `[${r.kind === "chunk" ? refLabel(r.kind, r.id) : rs.length > 1 ? r.id : raw.replace(/\*\*/g, "").trim()}](${REF_HREF}${r.kind}/${encodeURIComponent(r.id)})`);
          const text = rs.length > 1 && rs[0].kind === "note" ? `${raw.replace(/\s*\**#?\d[\s\S]*$/, "")} ${links.join(" a ")}` : links.join(" ");
          out = out.slice(0, start) + text + out.slice(start + raw.length);
        });
      return out;
    })
    .join("");
}

export function parseRefHref(href: string | undefined): { kind: RefKind; id: string } | null {
  if (!href?.startsWith(REF_HREF)) return null;
  const [kind, ...rest] = href.slice(REF_HREF.length).split("/");
  if (!["note", "msg", "chunk", "task", "file"].includes(kind)) return null;
  return { kind: kind as RefKind, id: decodeURIComponent(rest.join("/")) };
}
