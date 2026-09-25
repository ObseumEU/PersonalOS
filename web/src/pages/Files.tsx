import { Archive, Download, FileText, History, Image, RotateCcw, Search, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import TopicInput, { refreshTopics } from "../components/TopicInput";
import { PageHeader, Panel } from "../components/ui";
import { type FileItem, filesApi, fmtDate, fmtSize, parseTags } from "../filesApi";
import type { Version } from "../tasksApi";

const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

function Field({ label, htmlFor, children }: { label: string; htmlFor?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="cap">
        {label}
      </label>
      {children}
    </div>
  );
}

function TypeIcon({ file }: { file: FileItem }) {
  const Icon = file.preview === "image" ? Image : FileText;
  return <Icon size={15} strokeWidth={1.5} className={file.preview === "download" ? "text-ink-3" : "text-accent"} />;
}

function Preview({ file }: { file: FileItem }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    setText(null);
    if (file.preview === "text") filesApi.text(file.id).then((t) => setText(t.slice(0, 20000)), () => setText(""));
  }, [file.id, file.preview]);
  const url = filesApi.contentUrl(file.id);
  if (file.preview === "image")
    return <img src={url} alt={file.name} className="max-h-[320px] w-full rounded border border-line bg-bg object-contain" />;
  if (file.preview === "pdf")
    return <iframe src={url} title={file.name} className="h-[420px] w-full rounded border border-line bg-white" />;
  if (file.preview === "text")
    return (
      <pre className="max-h-[320px] overflow-auto rounded border border-line bg-bg p-3 font-mono text-[12px] whitespace-pre-wrap">
        {text ?? "loading…"}
      </pre>
    );
  return <p className="cap rounded border border-dashed border-line p-4 text-center">No preview for this type. Download it to open.</p>;
}

function FileDetail({ id, onChanged, onClose }: { id: number; onChanged: () => void; onClose: () => void }) {
  const [file, setFile] = useState<FileItem | null>(null);
  const [history, setHistory] = useState<Version[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => filesApi.get(id).then(setFile, (e) => setError(e.message)), [id]);
  useEffect(() => {
    setHistory(null);
    setFile(null);
    load();
  }, [load]);

  async function run(p: Promise<unknown>) {
    setError(null);
    try {
      await p;
      await load();
      if (history) setHistory(await filesApi.history(id));
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!file) return <Panel title="Detail" className="w-full">{error && <p className="cap p-4 text-red-400!">{error}</p>}</Panel>;
  return (
    <Panel
      fig={`F-${file.id}`}
      title="Detail"
      className="w-full"
      bodyClassName="overflow-y-auto"
      right={
        <button type="button" onClick={onClose} aria-label="Close detail" className="text-ink-3 hover:text-ink">
          <X size={14} />
        </button>
      }
    >
      <div className="flex flex-col gap-4 p-4">
        <input
          key={file.updated_at}
          defaultValue={file.name}
          aria-label="File name"
          onBlur={(e) => e.target.value.trim() && e.target.value !== file.name && run(filesApi.update(file.id, { name: e.target.value }))}
          className="bg-transparent text-lg font-light tracking-[-0.01em] outline-none focus:border-b focus:border-accent"
        />
        <span className="cap">
          {file.mime} · {fmtSize(file.size)} · added {fmtDate(file.created_at)}
          {file.archived_at && <span className="text-amber-300!"> · archived</span>}
        </span>
        <Preview file={file} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="TOPIC" htmlFor="file-topic">
            <TopicInput
              id="file-topic"
              value={file.topic}
              onCommit={(topic) => {
                refreshTopics();
                run(filesApi.update(file.id, { topic }));
              }}
            />
          </Field>
          <Field label="VISIBILITY" htmlFor="file-visibility">
            <select
              id="file-visibility"
              className={input}
              value={file.visibility}
              onChange={(e) => run(filesApi.update(file.id, { visibility: e.target.value as FileItem["visibility"] }))}
            >
              <option value="team">team</option>
              <option value="private">private</option>
              <option value="public">public</option>
            </select>
          </Field>
        </div>
        <Field label="TAGS (COMMA SEPARATED)" htmlFor="file-tags">
          <input
            id="file-tags"
            key={`t${file.updated_at}`}
            className={input}
            defaultValue={file.tags.join(", ")}
            onBlur={(e) => {
              const tags = parseTags(e.target.value);
              if (tags.join(",") !== file.tags.join(",")) run(filesApi.update(file.id, { tags }));
            }}
          />
        </Field>
        {file.text_chars > 0 && <span className="cap">{file.text_chars.toLocaleString("en-GB")} characters of text indexed for search</span>}
        <div className="flex flex-wrap gap-2 border-t border-line pt-3">
          <a className="btn" href={filesApi.contentUrl(file.id, true)}>
            <Download size={14} /> Download
          </a>
          {file.archived_at ? (
            <button type="button" className="btn" onClick={() => run(filesApi.restore(file.id))}>
              <RotateCcw size={14} /> Restore
            </button>
          ) : (
            <button type="button" className="btn" onClick={() => run(filesApi.archive(file.id))}>
              <Archive size={14} /> Archive
            </button>
          )}
          <button type="button" className="btn" onClick={() => filesApi.history(file.id).then(setHistory)}>
            <History size={14} /> History
          </button>
        </div>
        {history && (
          <div className="flex flex-col">
            {history.map((h) => (
              <div key={h.version} className="flex items-center gap-2 border-t border-line py-1.5 text-[12px]">
                <span className="cap">v{h.version}</span>
                <span>{h.action}</span>
                <span className="cap ml-auto">
                  {h.actor_name ?? "system"} · {new Date(h.at).toLocaleString("en-GB")}
                </span>
              </div>
            ))}
          </div>
        )}
        {error && <p className="cap text-red-400!">{error}</p>}
      </div>
    </Panel>
  );
}

export function DropZone({ topic, onUploaded }: { topic?: string; onUploaded: (f: FileItem[]) => void }) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  async function send(list: FileList | null) {
    if (!list?.length) return;
    setError(null);
    const done: FileItem[] = [];
    for (const f of Array.from(list)) {
      setBusy(`Uploading ${f.name}…`);
      try {
        done.push(await filesApi.upload(f, { topic }));
      } catch (e) {
        setError(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    setBusy(null);
    if (done.length) onUploaded(done);
  }

  return (
    <div
      onDragOver={(e: DragEvent) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e: DragEvent) => {
        e.preventDefault();
        setOver(false);
        send(e.dataTransfer.files);
      }}
      className={`flex flex-wrap items-center gap-3 rounded-md border border-dashed px-4 py-3.5 ${over ? "border-accent bg-accent/5" : "border-line"}`}
    >
      <Upload size={16} strokeWidth={1.5} className="text-accent" />
      <span className="text-[13px] text-ink-2">{busy ?? `Drop files here${topic ? ` to add them to #${topic}` : ""}, or`}</span>
      <button type="button" className="btn-accent" disabled={!!busy} onClick={() => picker.current?.click()}>
        Choose files
      </button>
      <input
        ref={picker}
        type="file"
        multiple
        className="hidden"
        aria-label="Upload files"
        onChange={(e) => {
          send(e.target.files);
          e.target.value = "";
        }}
      />
      {error && <span className="cap w-full text-red-400!">{error}</span>}
    </div>
  );
}

export default function Files() {
  const [params, setParams] = useSearchParams();
  const topic = params.get("topic") ?? undefined;
  const tag = params.get("tag") ?? undefined;
  const archived = params.get("archived") === "1";
  const selected = params.get("file") ? Number(params.get("file")) : null;
  const [q, setQ] = useState(params.get("q") ?? "");
  const [files, setFiles] = useState<FileItem[] | null>(null);
  const [tags, setTags] = useState<{ tag: string; n: number }[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [grid, setGrid] = useState(false);

  const query = params.get("q") ?? undefined;
  const refresh = useCallback(() => {
    filesApi.list({ topic, tag, q: query, archived }).then(setFiles, (e) => setError(e.message));
    filesApi.tags().then(setTags, () => undefined);
  }, [topic, tag, query, archived]);
  useEffect(refresh, [refresh]);
  // Search as you type, a moment after the last key.
  useEffect(() => {
    const t = setTimeout(() => {
      if ((params.get("q") ?? "") !== q.trim()) set({ q: q.trim() || null });
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    setParams(p);
  };
  const total = files?.reduce((s, f) => s + f.size, 0) ?? 0;

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="FILES · UPLOAD → TAG → FIND"
        title="Files"
        sub="Every document in one place. Text, markdown, CSV, JSON and PDFs are searchable in full text."
      />
      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <nav aria-label="File filters" className="flex shrink-0 gap-1 overflow-x-auto lg:w-44 lg:flex-col lg:overflow-visible">
          <span className="cap hidden px-2.5 pb-1 lg:block">SHOW</span>
          {[
            ["All files", false],
            ["Archive", true],
          ].map(([label, arch]) => (
            <button
              key={String(label)}
              type="button"
              onClick={() => set({ archived: arch ? "1" : null, file: null })}
              className={`flex h-[34px] shrink-0 items-center gap-2.5 rounded px-2.5 text-[13px] ${
                archived === arch ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : "text-ink-2 hover:bg-raised"
              }`}
            >
              {arch ? <Archive size={15} strokeWidth={1.5} className="text-ink-3" /> : <FileText size={15} strokeWidth={1.5} className="text-ink-3" />}
              {label}
            </button>
          ))}
          {topic && (
            <button type="button" onClick={() => set({ topic: null })} className="flex h-[30px] shrink-0 items-center gap-2 rounded bg-raised px-2.5 text-[13px]">
              <span className="cap">#</span>
              {topic}
              <X size={12} className="ml-auto text-ink-3" />
            </button>
          )}
          {tags.length > 0 && (
            <>
              <span className="cap hidden px-2.5 pt-4 pb-1 lg:block">TAGS</span>
              {tags.map((t) => (
                <button
                  key={t.tag}
                  type="button"
                  onClick={() => set({ tag: tag === t.tag ? null : t.tag })}
                  className={`hidden h-[30px] items-center gap-2.5 rounded px-2.5 text-[13px] lg:flex ${
                    t.tag === tag ? "bg-raised text-ink" : "text-ink-2 hover:bg-raised"
                  }`}
                >
                  {t.tag}
                  <span className="cap ml-auto">{t.n}</span>
                </button>
              ))}
            </>
          )}
        </nav>

        <Panel
          fig="TAB. 1"
          title={[archived ? "Archive" : "Files", topic && `#${topic}`, tag && `tag ${tag}`].filter(Boolean).join(" · ")}
          right={
            <span className="flex items-center gap-3">
              {files ? `${files.length} files · ${fmtSize(total)}` : "loading…"}
              <button type="button" className="hover:text-accent!" onClick={() => setGrid(!grid)}>
                {grid ? "list" : "grid"}
              </button>
            </span>
          }
          className="min-w-0 flex-1"
          bodyClassName="flex flex-col overflow-y-auto"
        >
          <div className="flex flex-col gap-3 border-b border-line p-3.5">
            {!archived && (
              <DropZone
                topic={topic}
                onUploaded={(done) => {
                  const dup = done.filter((f) => f.duplicate).length;
                  setNotice(`${done.length} uploaded${dup ? ` · ${dup} already here (kept once)` : ""}`);
                  refreshTopics();
                  refresh();
                  if (done.length === 1) set({ file: String(done[0].id) });
                }}
              />
            )}
            <div className="flex h-9 items-center gap-2.5 rounded-md border border-line bg-bg px-3 focus-within:border-accent">
              <Search size={14} className="text-ink-3" />
              <label htmlFor="file-search" className="sr-only">
                Search files
              </label>
              <input
                id="file-search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search names and the text inside…"
                className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-3"
              />
            </div>
            {notice && <span className="cap text-accent!">{notice}</span>}
          </div>
          {files?.length === 0 && <p className="cap p-6 text-center">{query || tag || topic ? "No file matches." : archived ? "Nothing archived." : "No files yet. Drop one above."}</p>}
          {grid ? (
            <div className="grid grid-cols-2 gap-3 p-3.5 sm:grid-cols-3 xl:grid-cols-4">
              {files?.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => set({ file: String(f.id) })}
                  className={`flex flex-col gap-2 rounded border p-2 text-left ${f.id === selected ? "border-accent" : "border-line hover:border-ink-3"}`}
                >
                  <div className="grid h-24 place-items-center overflow-hidden rounded bg-bg">
                    {f.preview === "image" ? <img src={filesApi.contentUrl(f.id)} alt="" className="h-full w-full object-cover" /> : <TypeIcon file={f} />}
                  </div>
                  <span className="truncate text-[13px]">{f.name}</span>
                  <span className="cap truncate">
                    {fmtSize(f.size)}
                    {f.topic && ` · #${f.topic}`}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            files?.map((f) => (
              <button
                key={f.id}
                type="button"
                onClick={() => set({ file: String(f.id) })}
                className={`grid grid-cols-[18px_minmax(0,1fr)_auto_64px_84px] items-center gap-2.5 border-b border-line px-3.5 py-2 text-left ${
                  f.id === selected ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
                }`}
              >
                <TypeIcon file={f} />
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-sm">{f.name}</span>
                  {f.tags.slice(0, 3).map((t) => (
                    <span key={t} className="cap hidden shrink-0 rounded-sm border border-line px-1 xl:inline">
                      {t}
                    </span>
                  ))}
                </span>
                <span className="cap">{f.topic ? `#${f.topic}` : ""}</span>
                <span className="cap text-right">{fmtSize(f.size)}</span>
                <span className="cap text-right">{fmtDate(f.created_at)}</span>
              </button>
            ))
          )}
          {error && <p className="cap p-3.5 text-red-400!">{error}</p>}
        </Panel>

        {selected && (
          <div className="flex min-h-0 lg:w-[400px] lg:shrink-0">
            <FileDetail id={selected} onChanged={refresh} onClose={() => set({ file: null })} />
          </div>
        )}
      </div>
      {topic && (
        <Link to={`/topics/${topic}`} className="cap hover:text-accent!">
          → everything in #{topic}
        </Link>
      )}
    </div>
  );
}
