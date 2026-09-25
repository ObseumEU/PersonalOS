import { Archive, CalendarDays, CheckCheck, Clock, Inbox, ListChecks, Orbit, Sun, Users, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AssigneeChip, Energy, StatePill, fmtMinutes } from "../components/tasks/bits";
import TaskDetail from "../components/tasks/TaskDetail";
import { PageHeader, Panel } from "../components/ui";
import { type Actor, type Counts, PRIORITY_LABEL, type Task, type View, dueLabel, tasksApi } from "../tasksApi";

const VIEWS: { id: View; label: string; icon: LucideIcon }[] = [
  { id: "inbox", label: "Inbox", icon: Inbox },
  { id: "today", label: "Today", icon: Sun },
  { id: "upcoming", label: "Upcoming", icon: CalendarDays },
  { id: "next", label: "Next actions", icon: ListChecks },
  { id: "agents", label: "AI & agents", icon: Orbit },
  { id: "review", label: "Needs review", icon: CheckCheck },
  { id: "waiting", label: "Waiting for", icon: Users },
  { id: "someday", label: "Someday", icon: Archive },
  { id: "done", label: "Logbook", icon: Clock },
];

// Own focused work per day before the calendar sync exists (phase 4).
const DAY_CAPACITY_MIN = 6 * 60;

export function Capture({ onCaptured }: { onCaptured: (t: Task) => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    try {
      onCaptured(await tasksApi.capture(text));
      setText("");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-1">
      <div className="flex h-11 items-center gap-2.5 rounded-md border border-line bg-bg pr-1.5 pl-3.5 focus-within:border-accent">
        <span className="font-mono text-accent">+</span>
        <label htmlFor="capture" className="sr-only">
          Capture a task
        </label>
        <input
          id="capture"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Capture… “Call the bank tomorrow 15m #finance !high” or “Summarise the lease @ai”"
          className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-3"
        />
        <kbd className="cap rounded-sm border border-line px-1.5">Enter</kbd>
      </div>
      {error && <span className="cap text-red-400!">{error}</span>}
    </form>
  );
}

function Row({ task, selected, onSelect, onToggle }: { task: Task; selected: boolean; onSelect: () => void; onToggle: () => void }) {
  const due = dueLabel(task);
  const done = task.status === "done";
  return (
    <div
      className={`grid grid-cols-[18px_44px_minmax(0,1fr)_auto_40px_18px_64px] items-center gap-2.5 border-b border-line px-3.5 py-2 ${
        selected ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
      }`}
    >
      <input
        type="checkbox"
        checked={done}
        onChange={onToggle}
        aria-label={`Complete ${task.title}`}
        className="h-[15px] w-[15px] accent-accent"
      />
      <span className="cap">{task.ref}</span>
      <button type="button" onClick={onSelect} className="flex min-w-0 items-center gap-2 text-left">
        <span className={`truncate text-sm ${done ? "text-ink-3 line-through" : ""}`}>{task.title}</span>
        {task.steps_total ? (
          <span className="cap shrink-0">
            {task.steps_done}/{task.steps_total}
          </span>
        ) : null}
        <StatePill task={task} />
        {task.topic && <span className="cap hidden shrink-0 xl:inline">#{task.topic}</span>}
      </button>
      <AssigneeChip type={task.assignee_type} name={task.assignee_name} />
      <span className="cap text-ink-2!">{task.assignee_type === "human" || !task.assignee_type ? fmtMinutes(task.estimate_min) : "—"}</span>
      <Energy level={task.energy} />
      <span className={`cap text-right ${due.urgent ? "text-accent!" : ""}`}>{due.text}</span>
    </div>
  );
}

function CapacityBar({ tasks }: { tasks: Task[] }) {
  const mine = tasks
    .filter((t) => (t.assignee_type === "human" || !t.assignee_type) && t.status !== "done")
    .reduce((s, t) => s + (t.estimate_min ?? 0), 0);
  const pct = Math.min(100, (mine / DAY_CAPACITY_MIN) * 100);
  return (
    <div className="flex flex-col gap-2 border-b border-line px-3.5 py-3">
      <div className="flex flex-wrap items-baseline gap-2.5">
        <span className="text-[13px] font-medium">Today’s plan</span>
        <span className="cap">
          {fmtMinutes(mine)} of your time planned · {fmtMinutes(DAY_CAPACITY_MIN)} focus day until calendar sync
        </span>
      </div>
      <div className="relative h-2 rounded-[1px] bg-line">
        <span className={`absolute inset-y-0 left-0 ${pct > 80 ? "bg-amber-300" : "bg-ink"}`} style={{ width: `${pct}%` }} />
        <span className="absolute -inset-y-1 left-[80%] w-px bg-accent" />
      </div>
      <div className="flex gap-3">
        <span className="cap text-ink!">■ your work</span>
        <span className="cap text-accent!">| 80 % focus limit</span>
        <span className="cap ml-auto hidden sm:inline">AI and agents do not use your time</span>
      </div>
    </div>
  );
}

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "today";
  const topic = params.get("topic") ?? undefined;
  const selected = params.get("task");
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [topics, setTopics] = useState<{ topic: string; open: number }[]>([]);
  const [actors, setActors] = useState<Actor[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    tasksApi.list(view, topic).then(setTasks, (e) => setError(e.message));
    tasksApi.counts().then(setCounts);
    tasksApi.topics().then(setTopics);
  }, [view, topic]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    tasksApi.actors().then(setActors);
  }, []);

  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    setParams(p);
  };
  const toggle = (t: Task) =>
    (t.status === "done" ? tasksApi.update(t.ref, { status: "next" }) : tasksApi.complete(t.ref)).then(refresh, (e) => setError(e.message));

  const groups =
    view === "today"
      ? ([1, 2, 3, null] as const)
          .map((p) => ({ p, items: (tasks ?? []).filter((t) => t.priority === p) }))
          .filter((g) => g.items.length)
      : [{ p: undefined, items: tasks ?? [] }];
  const current = VIEWS.find((v) => v.id === view)!;

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="TASKS · CAPTURE → CLARIFY → ORGANISE → REVIEW"
        title="Tasks"
        sub={
          counts &&
          `${counts.today} today · ${counts.inbox} in the inbox · ${counts.agents} with AI and agents · ${counts.waiting} waiting on people${
            counts.review ? ` · ${counts.review} to review` : ""
          }`
        }
      />
      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <nav aria-label="Task views" className="flex shrink-0 gap-1 overflow-x-auto lg:w-44 lg:flex-col lg:overflow-visible">
          <span className="cap hidden px-2.5 pb-1 lg:block">VIEWS</span>
          {VIEWS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => set({ view: id, task: null, topic: null })}
              className={`flex h-[34px] shrink-0 items-center gap-2.5 rounded px-2.5 text-[13px] ${
                id === view ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : "text-ink-2 hover:bg-raised"
              }`}
            >
              <Icon size={15} strokeWidth={1.5} className={id === view ? "text-accent" : "text-ink-3"} />
              {label}
              {counts && id !== "done" && counts[id] > 0 && (
                <span className={`cap ml-auto pl-2 ${id === "inbox" || id === "review" ? "text-accent!" : ""}`}>{counts[id]}</span>
              )}
            </button>
          ))}
          {topics.length > 0 && (
            <>
              <span className="cap hidden px-2.5 pt-4 pb-1 lg:block">TOPICS</span>
              {topics.map((t) => (
                <button
                  key={t.topic}
                  type="button"
                  onClick={() => set({ topic: topic === t.topic ? null : t.topic, view: view === "today" ? "next" : view })}
                  className={`hidden h-[30px] items-center gap-2.5 rounded px-2.5 text-[13px] lg:flex ${
                    t.topic === topic ? "bg-raised text-ink" : "text-ink-2 hover:bg-raised"
                  }`}
                >
                  <span className="cap">#</span>
                  {t.topic}
                  <span className="cap ml-auto">{t.open}</span>
                </button>
              ))}
            </>
          )}
        </nav>

        <Panel
          fig="TAB. 1"
          title={topic ? `${current.label} · #${topic}` : current.label}
          right={view === "today" ? "priority, then energy" : `${tasks?.length ?? 0} tasks`}
          className="min-w-0 flex-1"
          bodyClassName="flex flex-col overflow-y-auto"
        >
          <div className="border-b border-line p-3.5">
            <Capture onCaptured={() => refresh()} />
          </div>
          {view === "inbox" && (tasks?.length ?? 0) > 0 && (
            <Link to="/tasks/inbox" className="cap border-b border-line px-3.5 py-2.5 text-accent! hover:bg-raised">
              → Clarify the inbox one item at a time, with AI suggestions
            </Link>
          )}
          {view === "today" && tasks && <CapacityBar tasks={tasks} />}
          {tasks?.length === 0 && (
            <p className="cap p-6 text-center">
              {view === "inbox" ? "Inbox zero." : view === "today" ? "Nothing planned for today. Give a task a do date." : "Nothing here."}
            </p>
          )}
          {groups.map((g) => (
            <div key={String(g.p)}>
              {g.p !== undefined && (
                <div className="flex items-baseline gap-2.5 px-3.5 pt-3.5 pb-2">
                  <span className="text-[13px] font-medium">{g.p ? PRIORITY_LABEL[g.p] : "No priority"}</span>
                  <span className="cap">
                    {g.p === 1 ? "P1 · important and urgent" : g.p === 2 ? "P2 · important" : g.p === 3 ? "P3 · if there is time" : ""}
                  </span>
                </div>
              )}
              {g.items.map((t) => (
                <Row key={t.id} task={t} selected={t.ref === selected} onSelect={() => set({ task: t.ref })} onToggle={() => toggle(t)} />
              ))}
            </div>
          ))}
          {error && <p className="cap p-3.5 text-red-400!">{error}</p>}
        </Panel>

        {selected && (
          <div className="flex min-h-0 lg:w-[380px] lg:shrink-0">
            <TaskDetail
              taskRef={selected}
              actors={actors}
              onChanged={refresh}
              onClose={() => set({ task: null })}
            />
          </div>
        )}
      </div>
    </div>
  );
}
