import { Archive, ArrowLeft, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import AgentPicker from "../components/tasks/AgentPicker";
import { StatePill } from "../components/tasks/bits";
import { refreshTopics } from "../components/TopicInput";
import { PageHeader, Panel } from "../components/ui";
import { type Topic, type TopicDetail, fmtDate, fmtSize, notesApi, topicsApi } from "../filesApi";
import { dueLabel } from "../tasksApi";
import { DropZone } from "./Files";

const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

function Count({ n, label }: { n: number; label: string }) {
  return (
    <span className="flex flex-col">
      <span className={`font-mono text-lg ${n ? "" : "text-ink-3"}`}>{n}</span>
      <span className="cap">{label}</span>
    </span>
  );
}

function TopicGrid() {
  const [topics, setTopics] = useState<Topic[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    topicsApi.list().then(setTopics, (e) => setError(e.message));
  }, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      const t = await topicsApi.create({ name: name.trim() });
      refreshTopics();
      navigate(`/topics/${t.slug}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="TOPICS · ONE PLACE PER AREA"
        title="Topics"
        sub="A client, a project, health, the house: each topic holds its files, notes, tasks and events."
      />
      <form onSubmit={create} className="flex max-w-md gap-2">
        <label htmlFor="new-topic" className="sr-only">
          New topic
        </label>
        <input id="new-topic" value={name} onChange={(e) => setName(e.target.value)} placeholder="New topic, e.g. “House”" className={`${input} h-9! flex-1`} />
        <button type="submit" className="btn-accent h-9!">
          <Plus size={14} /> Add
        </button>
      </form>
      {error && <p className="cap text-red-400!">{error}</p>}
      {topics?.length === 0 && (
        <p className="cap">No topics yet. Add one above, or give a task a topic with #word when you capture it.</p>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {topics?.map((t) => (
          <Link key={t.slug} to={`/topics/${t.slug}`} className="panel fade-in flex flex-col gap-3 p-4 hover:border-accent">
            <span className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full" style={{ background: t.color ?? "var(--color-accent)" }} />
              <span className="text-base">{t.name}</span>
              <span className="cap ml-auto">#{t.slug}</span>
            </span>
            {t.description && <span className="line-clamp-2 text-[13px] text-ink-2">{t.description}</span>}
            <span className="grid grid-cols-4 gap-2">
              <Count n={t.open_tasks} label="open" />
              <Count n={t.upcoming} label="dated" />
              <Count n={t.file_count} label="files" />
              <Count n={t.note_count} label="notes" />
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

function TopicPage({ slug }: { slug: string }) {
  const [topic, setTopic] = useState<TopicDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const load = useCallback(() => topicsApi.get(slug).then(setTopic, (e) => setError(e.message)), [slug]);
  useEffect(() => {
    setTopic(null);
    load();
  }, [load]);

  async function save(changes: Partial<Pick<Topic, "name" | "description" | "color">>) {
    try {
      await topicsApi.update(slug, changes);
      refreshTopics();
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!topic)
    return (
      <div className="flex flex-col gap-4">
        <Link to="/topics" className="cap hover:text-accent!">
          ← all topics
        </Link>
        {error && <p className="cap text-red-400!">{error}</p>}
      </div>
    );

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker={`TOPIC · #${topic.slug}`}
        title={topic.name}
        sub={`${topic.open_tasks} open tasks · ${topic.file_count} files · ${topic.note_count} notes${topic.events.length ? ` · ${topic.events.length} upcoming events` : ""}`}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/topics" className="btn">
          <ArrowLeft size={14} /> All topics
        </Link>
        <input
          key={`n${topic.updated_at}`}
          aria-label="Topic name"
          defaultValue={topic.name}
          onBlur={(e) => e.target.value.trim() && e.target.value !== topic.name && save({ name: e.target.value })}
          className={`${input} w-44`}
        />
        <input
          type="color"
          aria-label="Topic colour"
          value={topic.color ?? "#6cc4dc"}
          onChange={(e) => save({ color: e.target.value })}
          className="h-8 w-10 rounded border border-line bg-bg"
        />
        <input
          key={`d${topic.updated_at}`}
          aria-label="Description"
          placeholder="What this topic is about…"
          defaultValue={topic.description}
          onBlur={(e) => e.target.value !== topic.description && save({ description: e.target.value })}
          className={`${input} min-w-0 flex-1`}
        />
        <button
          type="button"
          className="btn"
          title="Rename the label on every task, file, note and project; into an existing topic it merges"
          onClick={() => {
            const to = window.prompt("New name (an existing topic merges):", slug);
            if (to && to !== slug)
              topicsApi.rename(slug, to).then((t) => {
                refreshTopics();
                navigate(`/topics/${t.slug}`);
              }, (e) => setError(e.message));
          }}
        >
          Rename / merge
        </button>
        {topic.id && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              topicsApi.archive(slug).then(() => {
                refreshTopics();
                navigate("/topics");
              })
            }
          >
            <Archive size={14} /> Archive topic
          </button>
        )}
      </div>
      {error && <p className="cap text-red-400!">{error}</p>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Open tasks" right={<Link to={`/tasks?view=next&topic=${slug}`} className="hover:text-accent">in Tasks →</Link>} bodyClassName="max-h-[360px] overflow-y-auto">
          {topic.open.length === 0 && <p className="cap px-4 py-4">No open tasks.</p>}
          {topic.open.map((t) => {
            const due = dueLabel(t);
            return (
              <div key={t.id} className="grid grid-cols-[44px_minmax(0,1fr)_auto_64px] items-center gap-2.5 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
                <span className="cap">{t.ref}</span>
                <Link to={`/tasks?view=next&task=${t.ref}`} className="flex min-w-0 items-center gap-2 hover:text-accent">
                  <span className="truncate text-sm">{t.title}</span>
                  <StatePill task={t} />
                </Link>
                {/* Same picker and endpoint as Tasks: the new agent is told and starts at once. */}
                <AgentPicker task={t} onReassigned={() => load()} />
                <span className={`cap text-right ${due.urgent ? "text-accent!" : ""}`}>{due.text}</span>
              </div>
            );
          })}
        </Panel>

        <Panel title="Events" right="next 30 days, mentioning the topic" bodyClassName="max-h-[360px] overflow-y-auto">
          {topic.events.length === 0 && <p className="cap px-4 py-4">No calendar events mention this topic.</p>}
          {topic.events.map((e) => (
            <div key={e.id} className="flex items-baseline gap-3 border-b border-line px-4 py-2 text-[13px] last:border-0">
              <span className="cap w-28 shrink-0 text-accent!">
                {new Date(e.start).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} {e.all_day ? "" : e.start.slice(11, 16)}
              </span>
              <span className="truncate">{e.title}</span>
              {e.location && <span className="cap ml-auto truncate">{e.location}</span>}
            </div>
          ))}
        </Panel>

        <Panel title="Files" right={<Link to={`/files?topic=${slug}`} className="hover:text-accent">in Files →</Link>} bodyClassName="max-h-[420px] overflow-y-auto">
          <div className="border-b border-line p-3.5">
            <DropZone topic={slug} onUploaded={() => load()} />
          </div>
          {topic.files.length === 0 && <p className="cap px-4 py-4">No files in this topic.</p>}
          {topic.files.map((f) => (
            <Link key={f.id} to={`/files?file=${f.id}`} className="flex items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
              <span className="truncate text-sm">{f.name}</span>
              <span className="cap ml-auto shrink-0">
                {fmtSize(f.size)} · {fmtDate(f.created_at)}
              </span>
            </Link>
          ))}
        </Panel>

        <Panel
          title="Notes"
          right={
            <button
              type="button"
              className="hover:text-accent!"
              onClick={() => notesApi.create({ title: `${topic.name} note`, topic: slug }).then((n) => navigate(`/notes?note=${n.id}`))}
            >
              + new note
            </button>
          }
          bodyClassName="max-h-[420px] overflow-y-auto"
        >
          {topic.notes.length === 0 && <p className="cap px-4 py-4">No notes in this topic.</p>}
          {topic.notes.map((n) => (
            <Link key={n.id} to={`/notes?note=${n.id}`} className="flex flex-col gap-0.5 border-b border-line px-4 py-2.5 last:border-0 hover:bg-raised">
              <span className="flex items-baseline gap-2">
                <span className="truncate text-sm">{n.title}</span>
                <span className="cap ml-auto shrink-0">{fmtDate(n.updated_at)}</span>
              </span>
              {n.excerpt && <span className="truncate text-[12px] text-ink-2">{n.excerpt}</span>}
            </Link>
          ))}
        </Panel>

        {topic.done.length > 0 && (
          <Panel title="Done" right={`${topic.done_tasks} finished`} className="lg:col-span-2" bodyClassName="max-h-[240px] overflow-y-auto">
            {topic.done.map((t) => (
              <Link key={t.id} to={`/tasks?view=done&task=${t.ref}`} className="flex items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
                <span className="cap">{t.ref}</span>
                <span className="truncate text-sm text-ink-3 line-through">{t.title}</span>
                {t.completed_at && <span className="cap ml-auto">{fmtDate(t.completed_at)}</span>}
              </Link>
            ))}
          </Panel>
        )}
      </div>
    </div>
  );
}

export default function Topics() {
  const { slug } = useParams();
  return slug ? <TopicPage slug={slug} /> : <TopicGrid />;
}
