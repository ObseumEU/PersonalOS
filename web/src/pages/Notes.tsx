import { Archive, History, Plus, RotateCcw, Search, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Markdown from "../components/Markdown";
import TopicInput, { refreshTopics } from "../components/TopicInput";
import { PageHeader, Panel } from "../components/ui";
import { type Note, fmtDate, histAction, notesApi, parseTags } from "../filesApi";
import { LOCALE, label, t } from "../i18n";
import type { Version } from "../tasksApi";

const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";
const AUTOSAVE_MS = 1500;

function Editor({ id, onChanged, onClose }: { id: number; onChanged: () => void; onClose: () => void }) {
  const [note, setNote] = useState<Note | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [mode, setMode] = useState<"write" | "preview" | "split">("split");
  const [state, setState] = useState<"saved" | "editing" | "saving">("saved");
  const [history, setHistory] = useState<Version[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(
    () =>
      notesApi.get(id).then((n) => {
        setNote(n);
        setTitle(n.title);
        setBody(n.body ?? "");
        setState("saved");
      }, (e) => setError(e.message)),
    [id],
  );
  useEffect(() => {
    setHistory(null);
    setNote(null);
    load();
  }, [load]);

  async function run(p: Promise<Note>) {
    setError(null);
    try {
      const n = await p;
      setNote(n);
      if (history) setHistory(await notesApi.history(id));
      onChanged();
      return n;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const save = useCallback(
    async (ti: string, b: string) => {
      if (!note || (ti === note.title && b === note.body)) return setState("saved");
      if (!ti.trim()) return setError(t("notes.empty_title"));
      setState("saving");
      await run(notesApi.update(id, { title: ti, body: b }));
      setState("saved");
    },
    [note, id, history],
  );

  // Autosave a moment after the last keystroke.
  useEffect(() => {
    if (!note || (title === note.title && body === note.body)) return;
    setState("editing");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => save(title, body), AUTOSAVE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [title, body]);

  if (!note)
    return (
      <Panel title={t("notes.note")} className="w-full">
        {error && <p className="p-4 text-xs text-red-400">{error}</p>}
      </Panel>
    );
  return (
    <Panel
      title={t("notes.note")}
      className="w-full min-w-0"
      bodyClassName="flex flex-col overflow-y-auto"
      right={
        <span className="flex items-center gap-3">
          <span className={state === "saved" ? "" : "text-amber-300!"}>{state === "saved" ? t("notes.saved") : state === "saving" ? t("notes.saving") : t("notes.editing")}</span>
          <button type="button" onClick={onClose} aria-label={t("notes.close")} className="text-ink-3 hover:text-ink">
            <X size={14} />
          </button>
        </span>
      }
    >
      <div className="flex flex-col gap-3 border-b border-line p-4">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label={t("notes.title")}
          className="min-w-0 bg-transparent text-xl font-light tracking-[-0.01em] outline-none focus:border-b focus:border-accent"
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="note-topic" className="text-xs text-ink-2">
              {t("files.topic")}
            </label>
            <TopicInput
              id="note-topic"
              value={note.topic}
              onCommit={(topic) => {
                refreshTopics();
                run(notesApi.update(id, { topic }));
              }}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="note-tags" className="text-xs text-ink-2">
              {t("files.tags")}
            </label>
            <input
              id="note-tags"
              key={note.updated_at}
              className={input}
              defaultValue={note.tags.join(", ")}
              onBlur={(e) => {
                const tags = parseTags(e.target.value);
                if (tags.join(",") !== note.tags.join(",")) run(notesApi.update(id, { tags }));
              }}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="note-visibility" className="text-xs text-ink-2">
              {t("files.visibility")}
            </label>
            <select id="note-visibility" className={input} value={note.visibility} onChange={(e) => run(notesApi.update(id, { visibility: e.target.value as Note["visibility"] }))}>
              {(["team", "private", "public"] as const).map((v) => (
                <option key={v} value={v}>
                  {label("files.vis", v)}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {(["write", "split", "preview"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`rounded px-2 py-1 text-xs ${mode === m ? "bg-raised text-ink" : "text-ink-2 hover:text-ink"}`}
            >
              {t(`notes.mode.${m}`)}
            </button>
          ))}
          <span className="ml-auto hidden text-xs text-ink-2 sm:inline">{t("notes.md_hint")}</span>
        </div>
      </div>
      <div className={`grid min-h-[320px] flex-1 ${mode === "split" ? "lg:grid-cols-2" : ""}`}>
        {mode !== "preview" && (
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "s") {
                e.preventDefault();
                save(title, body);
              }
            }}
            aria-label={t("notes.body")}
            placeholder={t("notes.body_ph")}
            className="min-h-[320px] min-w-0 resize-none bg-bg p-4 font-mono text-[13px] leading-relaxed outline-none placeholder:text-ink-3"
          />
        )}
        {mode !== "write" && (
          <div className={`min-w-0 overflow-y-auto p-4 break-words ${mode === "split" ? "border-t border-line lg:border-t-0 lg:border-l" : ""}`}>
            {body.trim() ? <Markdown text={body} /> : <p className="text-xs text-ink-2">{t("notes.nothing_preview")}</p>}
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-2 border-t border-line p-4">
        <button type="button" className="btn-accent" disabled={state === "saved"} onClick={() => save(title, body)}>
          {t("act.save")}
        </button>
        {note.archived_at ? (
          <button type="button" className="btn" onClick={() => run(notesApi.unarchive(id))}>
            <RotateCcw size={14} /> {t("act.restore")}
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => run(notesApi.archive(id))}>
            <Archive size={14} /> {t("act.archive")}
          </button>
        )}
        <button type="button" className="btn" onClick={() => notesApi.history(id).then(setHistory)}>
          <History size={14} /> {t("files.history")}
        </button>
        <span className="ml-auto self-center text-xs text-ink-2">{t("notes.updated", { date: fmtDate(note.updated_at) })}</span>
      </div>
      {history && (
        <div className="flex flex-col px-4 pb-4">
          {history.map((h) => (
            <div key={h.version} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-t border-line py-1.5 text-xs">
              <span className="font-mono text-ink-2">v{h.version}</span>
              <span>{histAction(h.action)}</span>
              <span className="ml-auto text-ink-2">
                {h.actor_name ?? t("files.system")} · {new Date(h.at).toLocaleString(LOCALE)}
              </span>
              {h.version !== history[history.length - 1].version && (
                <button type="button" className="text-ink-2 hover:text-accent" onClick={() => run(notesApi.restore(id, h.version)).then(() => load())}>
                  {t("notes.restore")}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {error && <p className="px-4 pb-4 text-xs break-words text-red-400">{error}</p>}
    </Panel>
  );
}

export default function Notes() {
  const [params, setParams] = useSearchParams();
  const topic = params.get("topic") ?? undefined;
  const archived = params.get("archived") === "1";
  const selected = params.get("note") ? Number(params.get("note")) : null;
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    setParams(p);
  };
  const refresh = useCallback(() => {
    notesApi.list({ topic, q: query || undefined, archived }).then(setNotes, (e) => setError(e.message));
  }, [topic, query, archived]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    const h = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(h);
  }, [q]);

  async function newNote() {
    try {
      const n = await notesApi.create({ title: t("notes.untitled"), body: "", topic: topic ?? null });
      refresh();
      set({ note: String(n.id), archived: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader kicker={t("nav.knowledge")} title={t("nav.notes")} sub={t("notes.sub")} />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 lg:flex-row">
        <Panel
          title={[archived ? t("files.archive") : t("nav.notes"), topic && `#${topic}`].filter(Boolean).join(" · ")}
          right={
            <span className="flex items-center gap-3">
              <button type="button" className="hover:text-accent!" onClick={() => set({ archived: archived ? null : "1", note: null })}>
                {archived ? t("notes.back") : t("notes.archive")}
              </button>
              {topic && (
                <button type="button" className="hover:text-accent!" onClick={() => set({ topic: null })}>
                  {t("notes.all_topics")}
                </button>
              )}
            </span>
          }
          className="min-w-0 lg:w-[360px] lg:shrink-0"
          bodyClassName="flex flex-col overflow-y-auto"
        >
          <div className="flex gap-2 border-b border-line p-3.5">
            <div className="flex h-9 min-w-0 flex-1 items-center gap-2.5 rounded-md border border-line bg-bg px-3 focus-within:border-accent">
              <Search size={14} className="text-ink-3" />
              <label htmlFor="note-search" className="sr-only">
                {t("notes.search_label")}
              </label>
              <input
                id="note-search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder={t("notes.search_ph")}
                className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-3"
              />
            </div>
            <button type="button" className="btn-accent h-9!" onClick={newNote}>
              <Plus size={14} /> {t("notes.new")}
            </button>
          </div>
          {notes?.length === 0 && (
            <p className="p-6 text-center text-xs text-ink-2">{query ? t("notes.no_match") : archived ? t("files.no_archived") : t("notes.empty")}</p>
          )}
          {notes?.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => set({ note: String(n.id) })}
              className={`flex flex-col gap-1 border-b border-line px-3.5 py-2.5 text-left ${
                n.id === selected ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
              }`}
            >
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="truncate text-sm">{n.title}</span>
                <span className="ml-auto shrink-0 text-xs text-ink-2">{fmtDate(n.updated_at)}</span>
              </span>
              {n.excerpt && <span className="line-clamp-2 text-xs break-words text-ink-2">{n.excerpt}</span>}
              {(n.topic || n.tags.length > 0) && (
                <span className="truncate text-xs text-ink-2">
                  {n.topic && `#${n.topic}`} {n.tags.join(" · ")}
                </span>
              )}
            </button>
          ))}
          {error && <p className="p-3.5 text-xs break-words text-red-400">{error}</p>}
        </Panel>
        <div className="flex min-h-0 min-w-0 flex-1">
          {selected ? (
            <Editor key={selected} id={selected} onChanged={refresh} onClose={() => set({ note: null })} />
          ) : (
            <Panel title={t("notes.note")} className="w-full">
              <p className="p-6 text-center text-xs text-ink-2">{t("notes.pick")}</p>
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
