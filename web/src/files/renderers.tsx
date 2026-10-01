/**
 * The heavy renderers (a lazy chunk: never in the phone app's first download): Mermaid diagrams,
 * Vega-Lite charts, Markdown documents, CSV tables (sortable) and highlighted code. Each loads its
 * library only when such a file is shown. Nothing here runs a file's own code: Mermaid renders in
 * its strict mode (sanitised SVG), Vega-Lite with the expression interpreter (no eval, so the
 * strict CSP of /m holds) and without data URLs, Markdown without HTML.
 */
import { ArrowDown, ArrowUp } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { type FileRef, type PreviewKind, fileText } from "./kinds";

const MAX_TEXT = 2_000_000;

function useText(f: FileRef) {
  const [state, setState] = useState<{ text?: string; error?: string }>({});
  useEffect(() => {
    let live = true;
    setState({});
    fileText(f.id, f.version).then(
      (text) => live && setState({ text: text.slice(0, MAX_TEXT) }),
      (e) => live && setState({ error: String(e?.message ?? e) }),
    );
    return () => {
      live = false;
    };
  }, [f.id, f.version]);
  return state;
}

export function Problem({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-amber-400/40 bg-amber-400/5 p-3 text-[12.5px] text-amber-200">{children}</div>;
}

const Loading = () => <div className="h-24 animate-pulse rounded-lg bg-raised/60" />;

// ------------------------------------------------------------------ Mermaid

let mermaidReady: Promise<typeof import("mermaid").default> | null = null;
function loadMermaid() {
  mermaidReady ??= import("mermaid").then((m) => {
    m.default.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark",
      fontFamily: "IBM Plex Sans, sans-serif",
      themeVariables: { background: "transparent" },
    });
    return m.default;
  });
  return mermaidReady;
}

function MermaidView({ f, full }: { f: FileRef; full: boolean }) {
  const { text, error } = useText(f);
  const [svg, setSvg] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const id = "mmd" + useId().replace(/[^a-zA-Z0-9]/g, "");
  useEffect(() => {
    if (text == null) return;
    let live = true;
    loadMermaid()
      .then((m) => m.render(id, text))
      .then(
        (r) => live && setSvg(r.svg),
        (e) => live && setProblem(String(e?.message ?? e).slice(0, 400)),
      );
    return () => {
      live = false;
    };
  }, [text, id]);
  if (error || problem) return <Problem>Diagram nejde vykreslit: {error ?? problem}</Problem>;
  if (!svg) return <Loading />;
  // Mermaid's strict mode sanitises its output (DOMPurify); the source never reaches the page as HTML.
  return (
    <div
      className={`flex justify-center [&_svg]:h-auto ${full ? "w-[92vw] [&_svg]:max-h-[82dvh] [&_svg]:w-full [&_svg]:!max-w-full" : "[&_svg]:max-w-full"}`}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

// ------------------------------------------------------------------ Vega-Lite

function hasUrl(node: unknown): boolean {
  if (Array.isArray(node)) return node.some(hasUrl);
  if (node && typeof node === "object") return Object.entries(node).some(([k, v]) => (k === "url" && typeof v === "string") || hasUrl(v));
  return false;
}

const VEGA_CONFIG = {
  background: "transparent",
  font: "IBM Plex Sans, sans-serif",
  view: { stroke: "transparent" },
  mark: { color: "#6cc4dc" },
  axis: { labelColor: "#a3a7ad", titleColor: "#d4d6d9", gridColor: "#2a2f36", domainColor: "#3a4049", tickColor: "#3a4049" },
  legend: { labelColor: "#a3a7ad", titleColor: "#d4d6d9" },
  title: { color: "#e6e7e9", subtitleColor: "#a3a7ad" },
  range: { category: ["#6cc4dc", "#eb6834", "#1baf7a", "#d4a72c", "#a879e8", "#e85a8f", "#8a9199"] },
};

function VegaView({ f, full }: { f: FileRef; full: boolean }) {
  const { text, error } = useText(f);
  const ref = useRef<HTMLDivElement>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (text == null || !ref.current) return;
    let spec: Record<string, unknown>;
    try {
      spec = JSON.parse(text);
    } catch (e) {
      setProblem(`neplatný JSON: ${(e as Error).message}`);
      return;
    }
    if (hasUrl(spec)) {
      setProblem("graf načítá data z adresy; data musí být přímo v souboru (data.values)");
      return;
    }
    let view: { finalize: () => void } | null = null;
    let live = true;
    Promise.all([import("vega-embed"), import("vega-interpreter")])
      .then(([embed, interp]) =>
        embed.default(ref.current!, { width: "container", ...spec, config: { ...VEGA_CONFIG, ...(spec.config as object) } } as never, {
          actions: false,
          renderer: "svg",
          ast: true,
          expr: interp.expressionInterpreter,
          tooltip: true,
        }),
      )
      .then(
        (r) => {
          if (live) view = r;
          else r.finalize();
        },
        (e) => live && setProblem(String(e?.message ?? e).slice(0, 400)),
      );
    return () => {
      live = false;
      view?.finalize();
    };
  }, [text]);
  if (error || problem) return <Problem>Graf nejde vykreslit: {error ?? problem}</Problem>;
  return (
    <div className={`w-full ${full ? "min-w-[min(90vw,900px)]" : ""}`}>
      {text == null && <Loading />}
      <div ref={ref} className="w-full [&_svg]:max-w-full" />
    </div>
  );
}

// ------------------------------------------------------------------ Markdown

const safeUrl = (url: string) => {
  const u = defaultUrlTransform(url);
  return /^(https?:|mailto:|\/(?!\/)|#)/i.test(u) ? u : "";
};

function MarkdownView({ f, full }: { f: FileRef; full: boolean }) {
  const { text, error } = useText(f);
  if (error) return <Problem>{error}</Problem>;
  if (text == null) return <Loading />;
  return (
    <div className={`md ${full ? "mx-auto max-w-3xl" : "md-compact"}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeUrl}
        components={{
          a: ({ href, children }) => (href ? <a href={href} target="_blank" rel="noreferrer noopener">{children}</a> : <span>{children}</span>),
          img: ({ src, alt }) => (typeof src === "string" && src ? <a href={src} target="_blank" rel="noreferrer noopener">{alt || src}</a> : null),
          table: ({ children }) => (
            <div className="md-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

// ------------------------------------------------------------------ CSV

export function parseCsv(text: string, delimiter?: string): string[][] {
  const first = text.slice(0, 2000).split("\n")[0] ?? "";
  const d = delimiter ?? ([";", "\t", ","].map((c) => [c, first.split(c).length] as const).sort((a, b) => b[1] - a[1])[0][0]);
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') (cell += '"'), i++;
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"' && cell === "") quoted = true;
    else if (ch === d) row.push(cell), (cell = "");
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell), rows.push(row), (row = []), (cell = "");
    } else cell += ch;
  }
  if (cell !== "" || row.length) row.push(cell), rows.push(row);
  return rows.filter((r) => r.length > 1 || r[0] !== "");
}

const num = (s: string) => {
  const v = Number(s.replace(/\s/g, "").replace(",", "."));
  return s.trim() !== "" && Number.isFinite(v) ? v : null;
};

function CsvView({ f, full }: { f: FileRef; full: boolean }) {
  const { text, error } = useText(f);
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);
  const rows = useMemo(() => (text == null ? null : parseCsv(text, f.name.toLowerCase().endsWith(".tsv") ? "\t" : undefined)), [text, f.name]);
  const body = useMemo(() => {
    if (!rows) return [];
    const data = rows.slice(1);
    if (!sort) return data;
    return [...data].sort((a, b) => {
      const x = a[sort.col] ?? "";
      const y = b[sort.col] ?? "";
      const nx = num(x);
      const ny = num(y);
      return (nx != null && ny != null ? nx - ny : x.localeCompare(y, "cs")) * sort.dir;
    });
  }, [rows, sort]);
  if (error) return <Problem>{error}</Problem>;
  if (!rows) return <Loading />;
  const head = rows[0] ?? [];
  const shown = body.slice(0, full ? 5000 : 50);
  return (
    <div className={`overflow-auto rounded-lg border border-line ${full ? "max-h-[80dvh]" : "max-h-72"}`}>
      <table className="w-full border-collapse text-[12.5px]">
        <thead className="sticky top-0 bg-surface">
          <tr>
            {head.map((h, i) => (
              <th key={i} className="border-b border-line px-2 py-1.5 text-left font-medium whitespace-nowrap">
                <button
                  type="button"
                  className="flex items-center gap-1 hover:text-accent"
                  onClick={(e) => {
                    e.stopPropagation();
                    setSort((s) => (s?.col === i ? (s.dir === 1 ? { col: i, dir: -1 } : null) : { col: i, dir: 1 }));
                  }}
                >
                  {h}
                  {sort?.col === i && (sort.dir === 1 ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i} className="odd:bg-raised/40">
              {head.map((_, j) => (
                <td key={j} className={`border-b border-line/50 px-2 py-1 ${num(r[j] ?? "") != null ? "text-right font-mono" : ""}`}>
                  {r[j] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > shown.length && <p className="p-2 text-[12px] text-ink-2">… a dalších {body.length - shown.length} řádků</p>}
    </div>
  );
}

// ------------------------------------------------------------------ code and text

const LANG: Record<string, string> = {
  py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", sh: "bash", bash: "bash",
  yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", conf: "ini", sql: "sql", xml: "xml", html: "xml", css: "css",
  go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", rb: "ruby", php: "php", ps1: "powershell",
  json: "json", diff: "diff", patch: "diff", dockerfile: "dockerfile", dot: "plaintext", mmd: "plaintext",
};

function CodeView({ f, full, kind }: { f: FileRef; full: boolean; kind: PreviewKind }) {
  const { text, error } = useText(f);
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    if (text == null || kind !== "code" || text.length > 300_000) return;
    let live = true;
    import("highlight.js/lib/common").then(({ default: hljs }) => {
      const ext = f.name.toLowerCase().split(".").pop() ?? "";
      const lang = LANG[ext];
      const out = lang && hljs.getLanguage(lang) ? hljs.highlight(text, { language: lang }) : hljs.highlightAuto(text.slice(0, 50_000));
      if (live) setHtml(out.value); // hljs escapes the source; only its own spans are markup
    });
    return () => {
      live = false;
    };
  }, [text, kind, f.name]);
  if (error) return <Problem>{error}</Problem>;
  if (text == null) return <Loading />;
  const cls = `hljs overflow-auto rounded-lg border border-line bg-bg p-3 font-mono text-[12px] leading-relaxed ${full ? "max-h-[85dvh]" : "max-h-72"} ${kind === "text" ? "whitespace-pre-wrap break-words" : ""}`;
  return html ? <pre className={cls} dangerouslySetInnerHTML={{ __html: html }} /> : <pre className={cls}>{text}</pre>;
}

export default function Rendered({ file, kind, full = false }: { file: FileRef; kind: PreviewKind; full?: boolean }) {
  if (kind === "mermaid") return <MermaidView f={file} full={full} />;
  if (kind === "vegalite") return <VegaView f={file} full={full} />;
  if (kind === "markdown") return <MarkdownView f={file} full={full} />;
  if (kind === "csv") return <CsvView f={file} full={full} />;
  return <CodeView f={file} full={full} kind={kind} />;
}
