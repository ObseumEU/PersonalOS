import { Archive, CalendarDays, CheckCheck, Clock, Inbox, ListChecks, Orbit, Sun, Users, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import AgentPicker from "../components/tasks/AgentPicker";
import { Energy, StatePill, fmtMinutes } from "../components/tasks/bits";
import TaskDetail from "../components/tasks/TaskDetail";
import { agendaApi } from "./Calendar";
import { MockDot, PageHeader, Panel } from "../components/ui";
import { plural, t } from "../i18n";
import {
  type Actor,
  type Counts,
  NO_DESCRIPTION,
  PRIORITY_LABEL,
  type Scope,
  type Task,
  type View,
  descriptionPreview,
  dueLabel,
  tasksApi,
} from "../tasksApi";

const VIEWS: { id: View; icon: LucideIcon }[] = [
  { id: "inbox", icon: Inbox },
  { id: "today", icon: Sun },
  { id: "upcoming", icon: CalendarDays },
  { id: "next", icon: ListChecks },
  { id: "agents", icon: Orbit },
  { id: "review", icon: CheckCheck },
  { id: "waiting", icon: Users },
  { id: "someday", icon: Archive },
  { id: "done", icon: Clock },
];

const viewLabel = (id: View) => t(`work.view.${id}`);

/** The number shown next to a view. "Ke kontrole" counts what waits for me as reviewer (as Home does). */
function viewCount(counts: Counts, id: View): number {
  if (id === "done") return 0;
  if (id === "review") return counts.to_review ?? counts.review;
  return counts[id];
}

// Own focused work per day before the calendar sync exists (phase 4).
const DAY_CAPACITY_MIN = 6 * 60;

export function Capture({ onCaptured }: { onCaptured: (t: Task) => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const value = text.trim();
    if (!value) return;
    setText(""); // clear at once so the next capture can be typed while this one saves
    try {
      onCaptured(await tasksApi.capture(value));
      setError(null);
    } catch (err) {
      setText((prev) => prev || value);
      setError(err instanceof Error ? err.message : String(err));
    }
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-1">
      <div className="flex h-11 min-w-0 items-center gap-2.5 rounded-md border border-line bg-bg pr-1.5 pl-3.5 focus-within:border-accent">
        <span className="font-mono text-accent">+</span>
        <label htmlFor="capture" className="sr-only">
          {t("work.capture.label")}
        </label>
        <input
          id="capture"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("work.capture.placeholder")}
          className="min-w-0 flex-1 truncate bg-transparent text-sm outline-none placeholder:text-ink-3"
        />
        <kbd className="hidden rounded-sm border border-line px-1.5 text-xs text-ink-2 sm:inline">Enter</kbd>
      </div>
      {error && <span className="text-xs break-words text-red-400">{error}</span>}
    </form>
  );
}

function Row({
  task,
  selected,
  hideTopic,
  onSelect,
  onToggle,
  onReassigned,
}: {
  task: Task;
  selected: boolean;
  hideTopic: boolean;
  onSelect: () => void;
  onToggle: () => void;
  onReassigned: () => void;
}) {
  const due = dueLabel(task);
  const done = task.status === "done";
  const preview = descriptionPreview(task.notes);
  return (
    <div
      className={`grid grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-1 border-b border-line px-3.5 py-2 sm:grid-cols-[18px_52px_minmax(0,1fr)_auto_64px_18px_76px] ${
        selected ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
      }`}
    >
      <input
        type="checkbox"
        checked={done}
        onChange={onToggle}
        aria-label={t("work.row.complete", { title: task.title })}
        className="h-[15px] w-[15px] accent-accent"
      />
      <span className="hidden truncate font-mono text-xs text-ink-2 sm:inline">{task.ref}</span>
      <button type="button" onClick={onSelect} className="flex min-w-0 flex-col gap-0.5 text-left">
        <span className="flex min-w-0 items-center gap-2">
          <span className={`truncate text-sm ${done ? "text-ink-3 line-through" : ""}`}>{task.title}</span>
          {task.steps_total ? (
            <span className="shrink-0 text-xs text-ink-2 tabular-nums">
              {task.steps_done}/{task.steps_total}
            </span>
          ) : null}
          <StatePill task={task} />
          {task.topic && !hideTopic && <span className="hidden shrink-0 text-xs text-ink-2 xl:inline">#{task.topic}</span>}
        </span>
        {preview ? (
          <span
            className="line-clamp-2 text-xs leading-snug break-words text-ink-2"
            title={task.description_generated ? t("work.row.generated") : undefined}
          >
            {preview}
          </span>
        ) : (
          <span className="text-xs text-ink-3 italic">{NO_DESCRIPTION}</span>
        )}
        {due.text && <span className={`text-xs sm:hidden ${due.urgent ? "text-accent" : "text-ink-2"}`}>{due.text}</span>}
      </button>
      <AgentPicker task={task} onReassigned={onReassigned} />
      <span className="hidden text-xs text-ink-2 sm:inline" title={t("work.row.estimate")}>
        {task.assignee_type === "human" || !task.assignee_type ? fmtMinutes(task.estimate_min) : "—"}
      </span>
      <span className="hidden sm:inline">
        <Energy level={task.energy} />
      </span>
      <span className={`hidden text-right text-xs sm:inline ${due.urgent ? "text-accent" : "text-ink-2"}`}>{due.text}</span>
    </div>
  );
}

function CapacityBar({ tasks }: { tasks: Task[] }) {
  const [free, setFree] = useState<number | null>(null);
  const [meetings, setMeetings] = useState(0);
  useEffect(() => {
    const d = new Date();
    agendaApi(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`, 1).then(
      (a) => {
        if (a.configured && a.capacity) {
          setFree(a.capacity.free_min);
          setMeetings(a.capacity.meetings_min);
        }
      },
      () => undefined,
    );
  }, []);
  const capacity = free ?? DAY_CAPACITY_MIN;
  const mine = tasks
    .filter((task) => (task.assignee_type === "human" || !task.assignee_type) && task.status !== "done")
    .reduce((s, task) => s + (task.estimate_min ?? 0), 0);
  const pct = capacity ? Math.min(100, (mine / capacity) * 100) : 100;
  return (
    <div className="flex flex-col gap-2 border-b border-line px-3.5 py-3">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="text-[13px] font-medium">{t("work.cap.title")}</span>
        {free === null && <MockDot why={t("work.cap.mock")} />}
        <span className="text-xs text-ink-2">
          {t("work.cap.planned", { mine: fmtMinutes(mine) })} ·{" "}
          {free === null
            ? t("work.cap.no_calendar", { cap: fmtMinutes(DAY_CAPACITY_MIN) })
            : t("work.cap.free", { free: fmtMinutes(free), meetings: fmtMinutes(meetings) })}
        </span>
      </div>
      <div className="relative h-2 rounded-[1px] bg-line">
        <span className={`absolute inset-y-0 left-0 ${pct > 80 ? "bg-amber-300" : "bg-ink"}`} style={{ width: `${pct}%` }} />
        <span className="absolute -inset-y-1 left-[80%] w-px bg-accent" />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        <span className="text-xs text-ink">{t("work.cap.legend_work")}</span>
        <span className="text-xs text-accent">{t("work.cap.legend_limit")}</span>
        <span className="ml-auto hidden text-xs text-ink-2 sm:inline">{t("work.cap.agents_free")}</span>
      </div>
    </div>
  );
}

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "today";
  const topic = params.get("topic") ?? undefined;
  const scope = (params.get("scope") as Scope) || "all";
  const selected = params.get("task");
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [topics, setTopics] = useState<{ topic: string; open: number }[]>([]);
  const [actors, setActors] = useState<Actor[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    tasksApi.list(view, topic, scope).then(setTasks, (e) => setError(e.message));
    tasksApi.counts().then(setCounts);
    tasksApi.topics().then(setTopics);
  }, [view, topic, scope]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    tasksApi.actors().then(setActors);
  }, []);

  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    setParams(p);
  };
  const toggle = (task: Task) =>
    (task.status === "done" ? tasksApi.update(task.ref, { status: "next" }) : tasksApi.complete(task.ref)).then(refresh, (e) =>
      setError(e.message),
    );

  const groups =
    view === "today"
      ? ([1, 2, 3, null] as const)
          .map((p) => ({ p, items: (tasks ?? []).filter((task) => task.priority === p) }))
          .filter((g) => g.items.length)
      : [{ p: undefined, items: tasks ?? [] }];
  const currentLabel = viewLabel(VIEWS.find((v) => v.id === view)?.id ?? "today");
  const toReview = counts ? (counts.to_review ?? counts.review) : 0;
  const n = tasks?.length ?? 0;

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker={t("work.tasks.kicker")}
        title={t("nav.tasks")}
        sub={
          counts &&
          [
            t("work.tasks.sub", { today: counts.today, inbox: counts.inbox, agents: counts.agents, waiting: counts.waiting }),
            toReview ? t("work.tasks.sub_review", { n: toReview }) : "",
          ]
            .filter(Boolean)
            .join(" · ")
        }
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 lg:flex-row">
        <nav aria-label={t("work.tasks.views_aria")} className="flex min-w-0 shrink-0 gap-1 overflow-x-auto lg:w-48 lg:flex-col lg:overflow-visible">
          <span className="hidden px-2.5 pb-1 text-xs text-ink-2 lg:block">{t("work.tasks.views")}</span>
          {VIEWS.map(({ id, icon: Icon }) => {
            const c = counts ? viewCount(counts, id) : 0;
            return (
              <button
                key={id}
                type="button"
                onClick={() => set({ view: id, task: null, topic: null })}
                title={id === "review" ? t("work.tasks.review_count_title") : undefined}
                className={`flex h-[34px] shrink-0 items-center gap-2.5 rounded px-2.5 text-[13px] ${
                  id === view ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : "text-ink-2 hover:bg-raised"
                }`}
              >
                <Icon size={15} strokeWidth={1.5} className={id === view ? "text-accent" : "text-ink-3"} />
                {viewLabel(id)}
                {c > 0 && (
                  <span className={`ml-auto pl-2 text-xs tabular-nums ${id === "inbox" || id === "review" ? "text-accent" : "text-ink-2"}`}>
                    {c}
                  </span>
                )}
              </button>
            );
          })}
          {topics.length > 0 && (
            <>
              <span className="hidden px-2.5 pt-4 pb-1 text-xs text-ink-2 lg:block">{t("nav.topics")}</span>
              {topics.map((tp) => (
                <button
                  key={tp.topic}
                  type="button"
                  onClick={() => set({ topic: topic === tp.topic ? null : tp.topic, view: view === "today" ? "next" : view })}
                  className={`hidden h-[30px] min-w-0 items-center gap-2.5 rounded px-2.5 text-[13px] lg:flex ${
                    tp.topic === topic ? "bg-raised text-ink" : "text-ink-2 hover:bg-raised"
                  }`}
                >
                  <span className="text-xs text-ink-2">#</span>
                  <span className="min-w-0 truncate">{tp.topic}</span>
                  <span className="ml-auto text-xs text-ink-2 tabular-nums">{tp.open}</span>
                </button>
              ))}
            </>
          )}
        </nav>

        <Panel
          title={topic ? `${currentLabel} · #${topic}` : currentLabel}
          right={
            <span className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
              {(["mine", "team", "all"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => set({ scope: s === "all" ? null : s })}
                  className={`text-xs ${s === scope ? "text-accent" : "text-ink-2 hover:text-ink"}`}
                >
                  {t(`work.scope.${s}`)}
                </button>
              ))}
              <span className="hidden text-xs text-ink-2 sm:inline">
                ·{" "}
                {view === "today"
                  ? t("work.tasks.sort_today")
                  : view === "review" && counts
                    ? t("work.tasks.review_right", { mine: toReview, all: counts.review })
                    : t("work.tasks.count", { n, word: plural(n, t("work.word.task1"), t("work.word.task2"), t("work.word.task5")) })}
              </span>
              <Link to="/weekly-review" className="hidden text-xs text-ink-2 hover:text-accent md:inline">
                {t("work.tasks.weekly")}
              </Link>
            </span>
          }
          className="min-w-0 flex-1"
          bodyClassName="flex flex-col overflow-y-auto"
        >
          <div className="border-b border-line p-3.5">
            <Capture onCaptured={() => refresh()} />
          </div>
          {view === "review" && counts && (
            <p className="border-b border-line px-3.5 py-2 text-xs text-ink-2 sm:hidden">
              {t("work.tasks.review_right", { mine: toReview, all: counts.review })}
            </p>
          )}
          {view === "inbox" && n > 0 && (
            <Link to="/tasks/inbox" className="border-b border-line px-3.5 py-2.5 text-xs text-accent hover:bg-raised">
              {t("work.tasks.clarify_link")}
            </Link>
          )}
          {view === "today" && tasks && <CapacityBar tasks={tasks} />}
          {tasks?.length === 0 && (
            <p className="p-6 text-center text-sm text-ink-2">
              {view === "inbox" ? t("work.tasks.empty.inbox") : view === "today" ? t("work.tasks.empty.today") : t("work.tasks.empty")}
            </p>
          )}
          {groups.map((g) => (
            <div key={String(g.p)}>
              {g.p !== undefined && (
                <div className="flex flex-wrap items-baseline gap-x-2.5 px-3.5 pt-3.5 pb-2">
                  <span className="text-[13px] font-medium">{g.p ? PRIORITY_LABEL[g.p] : t("work.priority.none")}</span>
                  {g.p ? <span className="text-xs text-ink-2">{t(`work.priority.hint.${g.p}`)}</span> : null}
                </div>
              )}
              {g.items.map((task) => (
                <Row
                  key={task.id}
                  task={task}
                  selected={task.ref === selected}
                  hideTopic={!!selected}
                  onSelect={() => set({ task: task.ref })}
                  onToggle={() => toggle(task)}
                  onReassigned={refresh}
                />
              ))}
            </div>
          ))}
          {error && <p className="p-3.5 text-xs break-words text-red-400">{error}</p>}
        </Panel>

        {selected && (
          <div className="flex min-h-0 min-w-0 lg:w-[380px] lg:shrink-0">
            <TaskDetail taskRef={selected} actors={actors} onChanged={refresh} onClose={() => set({ task: null })} />
          </div>
        )}
      </div>
    </div>
  );
}
