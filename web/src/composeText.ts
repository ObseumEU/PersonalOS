// The text side of composing (components/compose.tsx), kept free of React so `npm test` checks it.

const pad = (n: number) => String(n).padStart(2, "0");

/** A pasted picture's name ("snimek-20261005-142233.png"): browsers call every one "image.png". */
export function pastedName(mime: string, at: Date = new Date()): string {
  const ext = (mime.split("/")[1] || "png").replace("jpeg", "jpg").replace(/[^a-z0-9]/g, "") || "png";
  return `snimek-${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}.${ext}`;
}

/** `base` + the dictated words, one space between (none at the start of an empty box). */
export function joinDictation(base: string, said: string): string {
  const s = said.replace(/\s+/g, " ").trim();
  if (!s) return base;
  if (!base) return s;
  return /\s$/.test(base) ? base + s : `${base} ${s}`;
}

const FILE_LINE = /^📎 (.+) \(soubor #(\d+)\)$/gm;
export const IMAGE_EXT = /\.(png|jpe?g|gif|webp)$/i;

/** A text's "📎 name (soubor #id)" lines (the server adds one per attached file) apart from the rest. */
export function splitFileLines(body: string): { text: string; files: { id: number; name: string }[] } {
  const files = [...body.matchAll(FILE_LINE)].map((m) => ({ id: Number(m[2]), name: m[1] }));
  return { text: files.length ? body.replace(FILE_LINE, "").trim() : body, files };
}
