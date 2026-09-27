import { Archive, ArrowLeft, Plus } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { confirmDialog, toast } from "../components/overlay";
import AgentPicker from "../components/tasks/AgentPicker";
import { StatePill } from "../components/tasks/bits";
import { refreshTopics } from "../components/TopicInput";
import { PageHeader, Panel } from "../components/ui";
import { type Topic, type TopicDetail, fmtDate, fmtSize, notesApi, topicsApi } from "../filesApi";
import { LOCALE, t } from "../i18n";
import { dueLabel } from "../tasksApi";
import { DropZone } from "./Files";

const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

function Count({ n, label }: { n: number; label: string }) {
  return (
    <span className="flex min-w-0 flex-col">
      <span className={`font-mono text-lg ${n ? "" : "text-ink-3"}`}>{n}</span>
      <span className="truncate text-xs text-ink-2">{label}</span>
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
      const created = await topicsApi.create({ name: name.trim() });
      refreshTopics();
      navigate(`/topics/${created.slug}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader kicker={t("nav.knowledge")} title={t("nav.topics")} sub={t("topics.sub")} />
      <form onSubmit={create} className="flex max-w-md gap-2">
        <label htmlFor="new-topic" className="sr-only">
          {t("topics.new_label")}
        </label>
        <input id="new-topic" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("topics.new_ph")} className={`${input} h-9! min-w-0 flex-1`} />
        <button type="submit" className="btn-accent h-9!">
          <Plus size={14} /> {t("topics.add")}
        </button>
      </form>
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      {topics?.length === 0 && <p className="text-xs text-ink-2">{t("topics.empty")}</p>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {topics?.map((x) => (
          <Link key={x.slug} to={`/topics/${x.slug}`} className="panel fade-in flex min-w-0 flex-col gap-3 p-4 hover:border-accent">
            <span className="flex min-w-0 items-center gap-2">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: x.color ?? "var(--color-accent)" }} />
              <span className="truncate text-base">{x.name}</span>
              <span className="ml-auto max-w-[45%] truncate text-xs text-ink-2">#{x.slug}</span>
            </span>
            {x.description && <span className="line-clamp-2 text-[13px] break-words text-ink-2">{x.description}</span>}
            <span className="grid grid-cols-4 gap-2">
              <Count n={x.open_tasks} label={t("topics.c.open")} />
              <Count n={x.upcoming} label={t("topics.c.dated")} />
              <Count n={x.file_count} label={t("topics.c.files")} />
              <Count n={x.note_count} label={t("topics.c.notes")} />
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

  async function rename() {
    const to = (
      await confirmDialog({
        title: t("topics.rename_title", { slug }),
        body: t("topics.rename_body"),
        confirm: t("topics.rename_ok"),
        reason: t("topics.rename_field"),
      })
    )?.trim();
    if (to && to !== slug)
      topicsApi.rename(slug, to).then(
        (r) => {
          refreshTopics();
          navigate(`/topics/${r.slug}`);
        },
        (e) => setError(e.message),
      );
  }

  if (!topic)
    return (
      <div className="flex flex-col gap-4">
        <Link to="/topics" className="text-xs text-ink-2 hover:text-accent">
          {t("topics.back")}
        </Link>
        {error && <p className="text-xs break-words text-red-400">{error}</p>}
      </div>
    );

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader
        kicker={t("topics.kicker", { slug: topic.slug })}
        title={topic.name}
        sub={
          t("topics.summary", { open: topic.open_tasks, files: topic.file_count, notes: topic.note_count }) +
          (topic.events.length ? t("topics.summary_events", { n: topic.events.length }) : "")
        }
      />
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/topics" className="btn">
          <ArrowLeft size={14} /> {t("topics.all")}
        </Link>
        <input
          key={`n${topic.updated_at}`}
          aria-label={t("topics.name")}
          defaultValue={topic.name}
          onBlur={(e) => e.target.value.trim() && e.target.value !== topic.name && save({ name: e.target.value })}
          className={`${input} w-44 min-w-0`}
        />
        <input
          type="color"
          aria-label={t("topics.color")}
          value={topic.color ?? "#6cc4dc"}
          onChange={(e) => save({ color: e.target.value })}
          className="h-8 w-10 rounded border border-line bg-bg"
        />
        <input
          key={`d${topic.updated_at}`}
          aria-label={t("topics.desc")}
          placeholder={t("topics.desc_ph")}
          defaultValue={topic.description}
          onBlur={(e) => e.target.value !== topic.description && save({ description: e.target.value })}
          className={`${input} min-w-0 flex-1 basis-48`}
        />
        <button type="button" className="btn" title={t("topics.rename_hint")} onClick={rename}>
          {t("topics.rename")}
        </button>
        {topic.id && (
          <button
            type="button"
            className="btn"
            onClick={() =>
              topicsApi.archive(slug).then(() => {
                refreshTopics();
                toast(t("topics.archived", { slug }));
                navigate("/topics");
              }, (e) => setError(e.message))
            }
          >
            <Archive size={14} /> {t("topics.archive")}
          </button>
        )}
      </div>
      {error && <p className="text-xs break-words text-red-400">{error}</p>}

      <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel
          title={t("topics.open")}
          right={
            <Link to={`/tasks?view=next&topic=${slug}`} className="hover:text-accent">
              {t("topics.in_tasks")}
            </Link>
          }
          className="min-w-0"
          bodyClassName="max-h-[360px] overflow-y-auto"
        >
          {topic.open.length === 0 && <p className="px-4 py-4 text-xs text-ink-2">{t("topics.no_open")}</p>}
          {topic.open.map((task) => {
            const due = dueLabel(task);
            return (
              <div key={task.id} className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
                <span className="w-11 shrink-0 font-mono text-xs text-ink-2">{task.ref}</span>
                <TaskLink taskRef={task.ref} className="flex min-w-0 flex-1 basis-40 items-center gap-2 hover:text-accent">
                  <span className="truncate text-sm">{task.title}</span>
                  <StatePill task={task} />
                </TaskLink>
                {/* Same picker and endpoint as Tasks: the new agent is told and starts at once. */}
                <AgentPicker task={task} onReassigned={() => load()} />
                <span className={`w-16 shrink-0 text-right text-xs ${due.urgent ? "text-accent" : "text-ink-2"}`}>{due.text}</span>
              </div>
            );
          })}
        </Panel>

        <Panel title={t("topics.events")} right={t("topics.events_right")} className="min-w-0" bodyClassName="max-h-[360px] overflow-y-auto">
          {topic.events.length === 0 && <p className="px-4 py-4 text-xs text-ink-2">{t("topics.no_events")}</p>}
          {topic.events.map((e) => (
            <div key={e.id} className="flex min-w-0 items-baseline gap-3 border-b border-line px-4 py-2 text-[13px] last:border-0">
              <span className="w-24 shrink-0 text-xs text-accent">
                {new Date(e.start).toLocaleDateString(LOCALE, { day: "numeric", month: "short" })} {e.all_day ? "" : e.start.slice(11, 16)}
              </span>
              <span className="min-w-0 truncate">{e.title}</span>
              {e.location && <span className="ml-auto hidden min-w-0 truncate text-xs text-ink-2 sm:inline">{e.location}</span>}
            </div>
          ))}
        </Panel>

        <Panel
          title={t("nav.files")}
          right={
            <Link to={`/files?topic=${slug}`} className="hover:text-accent">
              {t("topics.in_files")}
            </Link>
          }
          className="min-w-0"
          bodyClassName="max-h-[420px] overflow-y-auto"
        >
          <div className="border-b border-line p-3.5">
            <DropZone topic={slug} onUploaded={() => load()} />
          </div>
          {topic.files.length === 0 && <p className="px-4 py-4 text-xs text-ink-2">{t("topics.no_files")}</p>}
          {topic.files.map((f) => (
            <Link key={f.id} to={`/files?file=${f.id}`} className="flex min-w-0 items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
              <span className="min-w-0 truncate text-sm">{f.name}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-2">
                {fmtSize(f.size)}
                <span className="hidden sm:inline"> · {fmtDate(f.created_at)}</span>
              </span>
            </Link>
          ))}
        </Panel>

        <Panel
          title={t("nav.notes")}
          right={
            <button
              type="button"
              className="hover:text-accent!"
              onClick={() => notesApi.create({ title: t("topics.note_title", { name: topic.name }), topic: slug }).then((n) => navigate(`/notes?note=${n.id}`))}
            >
              {t("topics.new_note")}
            </button>
          }
          className="min-w-0"
          bodyClassName="max-h-[420px] overflow-y-auto"
        >
          {topic.notes.length === 0 && <p className="px-4 py-4 text-xs text-ink-2">{t("topics.no_notes")}</p>}
          {topic.notes.map((n) => (
            <Link key={n.id} to={`/notes?note=${n.id}`} className="flex min-w-0 flex-col gap-0.5 border-b border-line px-4 py-2.5 last:border-0 hover:bg-raised">
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="truncate text-sm">{n.title}</span>
                <span className="ml-auto shrink-0 text-xs text-ink-2">{fmtDate(n.updated_at)}</span>
              </span>
              {n.excerpt && <span className="truncate text-xs text-ink-2">{n.excerpt}</span>}
            </Link>
          ))}
        </Panel>

        {topic.done.length > 0 && (
          <Panel
            title={t("topics.done")}
            right={t("topics.done_right", { n: topic.done_tasks })}
            className="min-w-0 lg:col-span-2"
            bodyClassName="max-h-[240px] overflow-y-auto"
          >
            {topic.done.map((task) => (
              <TaskLink key={task.id} taskRef={task.ref} className="flex min-w-0 items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
                <span className="shrink-0 font-mono text-xs text-ink-2">{task.ref}</span>
                <span className="min-w-0 truncate text-sm text-ink-3 line-through">{task.title}</span>
                {task.completed_at && <span className="ml-auto shrink-0 text-xs text-ink-2">{fmtDate(task.completed_at)}</span>}
              </TaskLink>
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
