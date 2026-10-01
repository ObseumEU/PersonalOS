/**
 * How a file shows (docs/FILES.md, "Visuals"): the server says the kind (`preview`), the app
 * renders it. Small on purpose: the phone app (/m) imports it; the heavy renderers (Mermaid,
 * Vega-Lite, Markdown, highlighted code) live in ./renderers, loaded only when one is shown.
 */

export type PreviewKind =
  | "image" | "svg" | "pdf" | "markdown" | "csv" | "mermaid" | "dot" | "vegalite" | "html" | "code" | "text" | "download";

/** A file as a chat attachment, or a FileItem (it has these fields too). */
export type FileRef = {
  id: number;
  name: string;
  mime?: string | null;
  preview?: string;
  size?: number | null;
  version?: number | null;
  description?: string | null;
};

const q = (v?: number | null, extra?: string) => {
  const p = [v ? `v=${v}` : "", extra ?? ""].filter(Boolean).join("&");
  return p ? `?${p}` : "";
};

export const fileUrl = {
  content: (id: number, v?: number | null, download = false) => `/api/files/${id}/content${q(v, download ? "download=1" : "")}`,
  render: (id: number, v?: number | null) => `/api/files/${id}/render.svg${q(v)}`,
  page: (id: number, v?: number | null) => `/api/files/${id}/page${q(v)}`,
  inFiles: (id: number) => `/files?file=${id}`,
};

export async function fileText(id: number, v?: number | null): Promise<string> {
  const res = await fetch(fileUrl.content(id, v), { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.text();
}

/** The kind, also for attachments from before a kind existed (by type and name). */
export function kindOf(f: FileRef): PreviewKind {
  const p = f.preview as PreviewKind | undefined;
  if (p && p !== "text" && p !== "download") return p;
  const name = f.name.toLowerCase();
  const mime = f.mime ?? "";
  if (mime === "image/svg+xml" || name.endsWith(".svg")) return "svg";
  if (mime.startsWith("image/")) return "image";
  if (name.endsWith(".mmd") || name.endsWith(".mermaid")) return "mermaid";
  if (name.endsWith(".dot") || name.endsWith(".gv")) return "dot";
  if (name.endsWith(".vl.json")) return "vegalite";
  if (name.endsWith(".md")) return "markdown";
  if (name.endsWith(".csv") || name.endsWith(".tsv")) return "csv";
  return p ?? "download";
}

/** Kinds that are pictures: zoom and pan in the full-screen viewer. */
export const ZOOMABLE: PreviewKind[] = ["image", "svg", "dot", "mermaid"];
/** Kinds that need the lazy renderers chunk. */
export const HEAVY: PreviewKind[] = ["mermaid", "vegalite", "markdown", "csv", "code", "text"];

export function fmtBytes(n?: number | null): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} kB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export const KIND_LABEL: Record<PreviewKind, string> = {
  image: "Obrázek", svg: "SVG", pdf: "PDF", markdown: "Dokument", csv: "Tabulka", mermaid: "Diagram",
  dot: "Diagram", vegalite: "Graf", html: "Stránka", code: "Kód", text: "Text", download: "Soubor",
};
