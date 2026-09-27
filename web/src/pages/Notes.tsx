import { Archive, History, Plus, RotateCcw, Search, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Markdown from "../components/Markdown";
import TopicInput, { refreshTopics } from "../components/TopicInput";
import { PageHeader, Panel } from "../components/ui";
import { type Note, fmtDate, notesApi, parseTags } from "../filesApi";
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
    async (t: string, b: string) => {
      if (!note || (t === note.title && b === note.body)) return setState("saved");
      if (!t.trim()) return setError("The title is empty.");
      setState("saving");
      await run(notesApi.update(id, { title: t, body: b }));
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

  if (!note) return <Panel title="Note" className="w-full">{error && <p className="cap p-4 text-red-400!">{error}</p>}</Panel>;
  return (
    <Panel
      title="Note"
      className="w-full"
      bodyClassName="flex flex-col overflow-y-auto"
      right={
        <span className="flex items-center gap-3">
          <span className={state === "saved" ? "" : "text-amber-300!"}>{state === "saved" ? "saved" : state === "saving" ? "saving…" : "editing"}</span>
          <button type="button" onClick={onClose} aria-label="Close note" className="text-ink-3 hover:text-ink">
            <X size={14} />
          </button>
        </span>
      }
    >
      <div className="flex flex-col gap-3 border-b border-line p-4">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Title"
          className="bg-transparent text-xl font-light tracking-[-0.01em] outline-none focus:border-b focus:border-accent"
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="note-topic" className="cap">
              TOPIC
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
            <label htmlFor="note-tags" className="cap">
              TAGS
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
            <label htmlFor="note-visibility" className="cap">
              VISIBILITY
            </label>
            <select id="note-visibility" className={input} value={note.visibility} onChange={(e) => run(notesApi.update(id, { visibility: e.target.value as Note["visibility"] }))}>
              <option value="team">team</option>
              <option value="private">private</option>
              <option value="public">public</option>
            </select>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {(["write", "split", "preview"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`cap rounded px-2 py-1 ${mode === m ? "bg-raised text-ink!" : "hover:text-ink!"}`}
            >
              {m}
            </button>
          ))}
          <span className="cap ml-auto hidden sm:inline">markdown · autosaves</span>
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
            aria-label="Note body (markdown)"
            placeholder={"# Heading\n\nWrite in markdown: **bold**, *italic*, `code`, - lists, > quotes."}
            className="min-h-[320px] resize-none bg-bg p-4 font-mono text-[13px] leading-relaxed outline-none placeholder:text-ink-3"
          />
        )}
        {mode !== "write" && (
          <div className={`overflow-y-auto p-4 ${mode === "split" ? "border-t border-line lg:border-t-0 lg:border-l" : ""}`}>
            {body.trim() ? <Markdown text={body} /> : <p className="cap">Nothing to preview yet.</p>}
          </div>
        )}
      </div>
      <div className="flex flex-wrap gap-2 border-t border-line p-4">
        <button type="button" className="btn-accent" disabled={state === "saved"} onClick={() => save(title, body)}>
          Save
        </button>
        {note.archived_at ? (
          <button type="button" className="btn" onClick={() => run(notesApi.unarchive(id))}>
            <RotateCcw size={14} /> Restore
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => run(notesApi.archive(id))}>
            <Archive size={14} /> Archive
          </button>
        )}
        <button type="button" className="btn" onClick={() => notesApi.history(id).then(setHistory)}>
          <History size={14} /> History
        </button>
        <span className="cap ml-auto self-center">updated {fmtDate(note.updated_at)}</span>
      </div>
      {history && (
        <div className="flex flex-col px-4 pb-4">
          {history.map((h) => (
            <div key={h.version} className="flex items-center gap-2 border-t border-line py-1.5 text-[12px]">
              <span className="cap">v{h.version}</span>
              <span>{h.action}</span>
              <span className="cap ml-auto">
                {h.actor_name ?? "system"} · {new Date(h.at).toLocaleString("en-GB")}
              </span>
              {h.version !== history[history.length - 1].version && (
                <button type="button" className="cap hover:text-accent!" onClick={() => run(notesApi.restore(id, h.version)).then(() => load())}>
                  restore
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {error && <p className="cap px-4 pb-4 text-red-400!">{error}</p>}
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
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  async function newNote() {
    try {
      const n = await notesApi.create({ title: "Untitled note", body: "", topic: topic ?? null });
      refresh();
      set({ note: String(n.id), archived: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader kicker="NOTES · MARKDOWN · BY TOPIC" title="Notes" sub="Markdown notes that belong to your topics. Every save is a version you can go back to." />
      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <Panel
          title={[archived ? "Archive" : "Notes", topic && `#${topic}`].filter(Boolean).join(" · ")}
          right={
            <span className="flex items-center gap-3">
              <button type="button" className="hover:text-accent!" onClick={() => set({ archived: archived ? null : "1", note: null })}>
                {archived ? "notes" : "archive"}
              </button>
              {topic && (
                <button type="button" className="hover:text-accent!" onClick={() => set({ topic: null })}>
                  all topics
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
                Search notes
              </label>
              <input
                id="note-search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search notes…"
                className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-3"
              />
            </div>
            <button type="button" className="btn-accent h-9!" onClick={newNote}>
              <Plus size={14} /> New
            </button>
          </div>
          {notes?.length === 0 && <p className="cap p-6 text-center">{query ? "No note matches." : archived ? "Nothing archived." : "No notes yet."}</p>}
          {notes?.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => set({ note: String(n.id) })}
              className={`flex flex-col gap-1 border-b border-line px-3.5 py-2.5 text-left ${
                n.id === selected ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
              }`}
            >
              <span className="flex items-baseline gap-2">
                <span className="truncate text-sm">{n.title}</span>
                <span className="cap ml-auto shrink-0">{fmtDate(n.updated_at)}</span>
              </span>
              {n.excerpt && <span className="line-clamp-2 text-[12px] text-ink-2">{n.excerpt}</span>}
              {(n.topic || n.tags.length > 0) && (
                <span className="cap truncate">
                  {n.topic && `#${n.topic}`} {n.tags.join(" · ")}
                </span>
              )}
            </button>
          ))}
          {error && <p className="cap p-3.5 text-red-400!">{error}</p>}
        </Panel>
        <div className="flex min-h-0 min-w-0 flex-1">
          {selected ? (
            <Editor key={selected} id={selected} onChanged={refresh} onClose={() => set({ note: null })} />
          ) : (
            <Panel title="Note" className="w-full">
              <p className="cap p-6 text-center">Pick a note on the left, or start a new one.</p>
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
