import { FileText, FolderOpen, Hash, Network, Search } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader, Panel } from "../components/ui";
import { type FileItem, type Note, type Topic, filesApi, fmtDate, notesApi, topicsApi } from "../filesApi";
import { t } from "../i18n";

// The 3D graph (three.js) loads only when asked for.
const KnowledgePanel = lazy(() => import("../components/KnowledgePanel"));

type Kind = "file" | "note" | "topic";
type Entry = { kind: Kind; key: string; title: string; meta: string; at: string; link: string };

const ICON = { file: FolderOpen, note: FileText, topic: Hash } as const;
const FILTERS: { id: Kind | "all"; key: string }[] = [
  { id: "all", key: "act.all" },
  { id: "file", key: "nav.files" },
  { id: "note", key: "nav.notes" },
  { id: "topic", key: "nav.topics" },
];

/** Znalosti: files, notes and topics as one list with filters, and the knowledge graph on demand. */
export default function Knowledge() {
  const [params, setParams] = useSearchParams();
  const filter = (params.get("kind") as Kind | null) ?? "all";
  const [q, setQ] = useState("");
  const [files, setFiles] = useState<FileItem[] | null>(null);
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [topics, setTopics] = useState<Topic[] | null>(null);
  // The 3D graph is shown by default; ?graph=0 or the remembered toggle hides it.
  const [graph, setGraph] = useState(() => {
    if (params.get("graph")) return params.get("graph") !== "0";
    try {
      return localStorage.getItem("pos.knowledge.graph") !== "0";
    } catch {
      return true;
    }
  });
  const toggleGraph = () =>
    setGraph((g) => {
      try {
        localStorage.setItem("pos.knowledge.graph", g ? "0" : "1");
      } catch {
        /* storage blocked: the toggle still works for this visit */
      }
      return !g;
    });

  useEffect(() => {
    filesApi.list().then(setFiles, () => setFiles([]));
    notesApi.list().then(setNotes, () => setNotes([]));
    topicsApi.list().then(setTopics, () => setTopics([]));
  }, []);

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = [
      ...(files ?? []).map((f) => ({
        kind: "file" as const, key: `f${f.id}`, title: f.name, at: f.updated_at,
        meta: [f.topic ? `#${f.topic}` : "", ...f.tags.map((x) => `#${x}`)].filter(Boolean).join(" "),
        link: `/files?file=${f.id}`,
      })),
      ...(notes ?? []).map((n) => ({
        kind: "note" as const, key: `n${n.id}`, title: n.title || t("knowledge.untitled"), at: n.updated_at,
        meta: n.topic ? `#${n.topic}` : "", link: `/notes?note=${n.id}`,
      })),
      ...(topics ?? []).map((x) => ({
        kind: "topic" as const, key: `t${x.slug}`, title: x.name, at: x.updated_at ?? "",
        meta: t("knowledge.topic_meta", { tasks: x.open_tasks, files: x.file_count, notes: x.note_count }),
        link: `/topics/${encodeURIComponent(x.slug)}`,
      })),
    ];
    const needle = q.trim().toLowerCase();
    return out
      .filter((e) => filter === "all" || e.kind === filter)
      .filter((e) => !needle || `${e.title} ${e.meta}`.toLowerCase().includes(needle))
      .sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  }, [files, notes, topics, filter, q]);

  const loading = files === null || notes === null || topics === null;
  const count = (k: Kind) => (k === "file" ? files?.length : k === "note" ? notes?.length : topics?.length) ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("knowledge.kicker")} title={t("nav.knowledge")} sub={t("knowledge.sub")} />
      <div className="flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label={t("knowledge.filter")} className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              role="tab"
              aria-selected={filter === f.id}
              onClick={() => setParams(f.id === "all" ? {} : { kind: f.id }, { replace: true })}
              className={filter === f.id ? "btn-accent" : "btn"}
            >
              {t(f.key)}
              {f.id !== "all" && <span className="font-mono text-xs opacity-80">{count(f.id)}</span>}
            </button>
          ))}
        </div>
        <label className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded border border-line bg-bg px-2.5 focus-within:border-accent sm:max-w-xs">
          <Search size={14} className="shrink-0 text-ink-2" />
          <span className="sr-only">{t("act.search")}</span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t("knowledge.search")}
            className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-3"
          />
        </label>
        <button className={graph ? "btn-accent" : "btn"} onClick={toggleGraph} aria-pressed={graph}>
          <Network size={14} /> {graph ? t("knowledge.graph_hide") : t("knowledge.graph_show")}
        </button>
      </div>

      {graph && (
        <Suspense fallback={<p className="text-sm text-ink-2">{t("act.loading")}</p>}>
          <KnowledgePanel title={t("knowledge.graph")} className="h-[360px] sm:h-[560px]" />
        </Suspense>
      )}

      <Panel title={t("knowledge.list")} right={loading ? t("act.loading") : t("knowledge.items", { n: entries.length })}>
        {!loading && entries.length === 0 && <p className="px-4 py-5 text-sm text-ink-2">{t("knowledge.empty")}</p>}
        {entries.slice(0, 300).map((e) => {
          const Icon = ICON[e.kind];
          return (
            <Link key={e.key} to={e.link} className="flex min-w-0 items-center gap-3 border-b border-line px-4 py-2.5 last:border-0 hover:bg-raised">
              <Icon size={16} strokeWidth={1.5} className="shrink-0 text-ink-2" aria-label={t(`knowledge.kind.${e.kind}`)} />
              <span className="min-w-0 flex-1 truncate text-sm">{e.title}</span>
              {e.meta && <span className="hidden max-w-[40%] truncate text-xs text-ink-2 sm:inline">{e.meta}</span>}
              {e.at && <span className="shrink-0 font-mono text-xs text-ink-2">{fmtDate(e.at)}</span>}
            </Link>
          );
        })}
      </Panel>
    </div>
  );
}
