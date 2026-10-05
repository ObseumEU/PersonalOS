import { ChevronDown, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { getMe } from "../api";
import { Avatar, StatusChip, fmtMinutes, toneOf } from "../components/tasks/bits";
import { oneLine, shortAge } from "../components/tasks/text";
import { MockDot, PageHeader } from "../components/ui";
import { plural, t } from "../i18n";
import { useNeedsMe } from "../needsMeApi";
import { setSheetOrder, taskHref } from "../taskSheet";
import { type ReviewQueue, type Task, type View, dueLabel, tasksApi } from "../tasksApi";
import { agendaApi } from "./Calendar";

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
          placeholder={t("tk.list.capture")}
          className="min-w-0 flex-1 truncate bg-transparent text-sm outline-none placeholder:text-ink-3"
        />
        <kbd className="hidden rounded-sm border border-line px-1.5 text-xs text-ink-2 sm:inline">Enter</kbd>
      </div>
      {error && <span className="text-xs break-words text-red-400">{error}</span>}
    </form>
  );
}

// Own focused work per day before the calendar sync exists (phase 4).
const DAY_CAPACITY_MIN = 6 * 60;

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
    <div className="flex flex-col gap-2 border-b border-line px-4 py-3">
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
    </div>
  );
}

/* ------------------------------------------------------------------ groups */

// "you" is exactly what Home's "Čeká na tebe" lists (asks, approvals, results to review), so the numbers agree;
// "mine" is my own open work.
type Group = "you" | "mine" | "progress" | "review" | "done" | "later";
const GROUPS: Group[] = ["you", "mine", "progress", "review", "done", "later"];
const FILTERS: Group[] = ["you", "mine", "progress", "review", "done"];

function groupOf(task: Task, meId: number | null, needs: Set<string>): Group {
  if (needs.has(task.ref)) return "you";
  if (task.status === "done") return "done";
  if (task.status === "someday") return "later";
  if (task.status === "review") return "review";
  if (meId != null && (task.assignee_id === meId || (!task.assignee_id && task.owner_id === meId))) return "mine";
  return "progress";
}

// Other lists, for whoever still wants them (GTD views); the default is everything, grouped.
const VIEWS: View[] = ["board", "today", "inbox", "upcoming", "waiting", "someday"];

function Row({ task, meId, selected, href, need }: { task: Task; meId: number | null; selected: boolean; href: string; need?: string }) {
  const due = dueLabel(task);
  const line = oneLine(task);
  const done = task.status === "done";
  return (
    <li>
      <Link
        to={href}
        aria-current={selected ? "true" : undefined}
        className={`grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-line px-4 py-3 outline-none focus-visible:bg-raised sm:grid-cols-[32px_minmax(0,1fr)_auto_52px] ${
          selected ? "bg-accent/[0.08] shadow-[inset_3px_0_0_var(--color-accent)]" : "hover:bg-raised/70"
        }`}
      >
        <Avatar type={task.assignee_type} name={task.assignee_name} size={30} />
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className={`truncate text-[14px] ${done ? "text-ink-2" : "text-ink"}`}>{task.title}</span>
          {line && <span className="truncate text-[13px] text-ink-2">{line}</span>}
        </span>
        <span className="flex flex-col items-end gap-1">
          <StatusChip tone={need === "ask" || need === "approval" ? "you" : toneOf(task, meId)} progress={task.status === "working" ? task.progress : null} />
          {due.urgent && <span className="text-xs text-orange-200">{due.text}</span>}
        </span>
        <span className="hidden text-right text-xs text-ink-2 tabular-nums sm:block" title={new Date(task.updated_at).toLocaleString()}>
          {shortAge(task.updated_at)}
        </span>
      </Link>
    </li>
  );
}

function GroupList({
  g,
  items,
  meId,
  selected,
  hrefOf,
  collapsed,
  needKinds,
}: {
  needKinds: Map<string, string>;
  g: Group;
  items: Task[];
  meId: number | null;
  selected: string | null;
  hrefOf: (ref: string) => string;
  collapsed: boolean;
}) {
  const limit = g === "done" ? 6 : g === "later" ? 0 : Infinity;
  const [open, setOpen] = useState(!collapsed);
  const shown = open || !Number.isFinite(limit) ? items : items.slice(0, limit);
  const hidden = items.length - shown.length;
  return (
    <section aria-labelledby={`grp-${g}`}>
      <h2
        id={`grp-${g}`}
        className="sticky top-0 z-[1] flex items-center gap-2 border-b border-line bg-surface/95 px-4 pt-4 pb-2 backdrop-blur"
      >
        <span className={`text-[13px] font-medium ${g === "you" ? "text-orange-200" : "text-ink"}`}>{t(`tk.group.${g}`)}</span>
        <span className="rounded-full bg-raised px-2 text-xs text-ink-2 tabular-nums">{items.length}</span>
      </h2>
      <ul>
        {shown.map((task) => (
          <Row key={task.id} task={task} meId={meId} selected={task.ref === selected} href={hrefOf(task.ref)} need={needKinds.get(task.ref)} />
        ))}
      </ul>
      {hidden > 0 && (
        <button type="button" onClick={() => setOpen(true)} className="flex w-full items-center gap-1.5 px-4 py-2.5 text-[13px] text-accent hover:bg-raised">
          <ChevronDown size={14} /> {t("tk.list.show_more", { n: hidden })}
        </button>
      )}
    </section>
  );
}

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const loc = useLocation();
  const { ref: selectedRef } = useParams();
  const selected = selectedRef?.toUpperCase() ?? null;
  const view = (params.get("view") as View) || "board";
  const topic = params.get("topic") ?? "";
  const who = params.get("who") ?? "";
  const show = (params.get("show") as Group | null) ?? null;
  const [q, setQ] = useState("");
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [meId, setMeId] = useState<number | null>(null);
  const needs = useNeedsMe();

  useEffect(() => {
    getMe().then((m) => setMeId(m.actor_id ?? null), () => undefined);
  }, []);
  const refresh = useCallback(() => {
    tasksApi.list(view, topic || undefined).then(
      (x) => {
        setTasks(x);
        setError(null);
      },
      (e) => setError(e.message),
    );
  }, [view, topic]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    window.addEventListener("pos:tasks", refresh);
    return () => window.removeEventListener("pos:tasks", refresh);
  }, [refresh]);

  const set = (next: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v ? p.set(k, v) : p.delete(k)));
    setParams(p);
  };

  // What waits for me, by task: an ask or an approval reads "Čeká na tebe", a result to review "Ke kontrole".
  const needKinds = useMemo(() => {
    const m = new Map<string, string>();
    (needs?.items ?? []).forEach((i) => i.ref && i.kind !== "mention" && m.set(i.ref, i.kind));
    return m;
  }, [needs]);
  const needRefs = useMemo(() => new Set(needKinds.keys()), [needKinds]);
  const people = useMemo(() => {
    const m = new Map<number, string>();
    (tasks ?? []).forEach((x) => x.assignee_id && x.assignee_name && m.set(x.assignee_id, x.assignee_name));
    return [...m.entries()].sort((a, b) => a[1].localeCompare(b[1], "cs"));
  }, [tasks]);
  const topics = useMemo(() => [...new Set((tasks ?? []).map((x) => x.topic).filter(Boolean) as string[])].sort(), [tasks]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (tasks ?? []).filter((x) => {
      if (who === "me" ? x.assignee_id !== meId : who && String(x.assignee_id) !== who) return false;
      if (needle && !`${x.ref} ${x.title} ${x.summary ?? ""} ${x.notes}`.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [tasks, q, who, meId]);

  const grouped = useMemo(() => {
    const by: Record<Group, Task[]> = { you: [], mine: [], progress: [], review: [], done: [], later: [] };
    filtered.forEach((x) => by[groupOf(x, meId, needRefs)].push(x));
    return by;
  }, [filtered, meId, needRefs]);
  const visible = GROUPS.filter((g) => grouped[g].length && (!show || show === g));

  // ← → in the panel walk this list, in the order shown.
  useEffect(() => {
    setSheetOrder(visible.flatMap((g) => grouped[g].map((x) => x.ref)));
  }, [grouped, visible.join()]);
  useEffect(() => () => setSheetOrder([]), []);

  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  useEffect(() => {
    tasksApi.reviewQueue().then(setQueue, () => setQueue(null));
  }, [tasks]);
  const hrefOf = (r: string) => taskHref(loc, r);
  const n = (g: Group) => grouped[g].length;
  const summaryLine = [
    n("you") && `${n("you")} ${plural(n("you"), t("tk.sub.you1"), t("tk.sub.you2"), t("tk.sub.you5"))}`,
    n("mine") && t("tk.sub.mine", { n: n("mine") }),
    n("progress") && t("tk.sub.progress", { n: n("progress") }),
    // The queue from one definition (pos.review_queue), with whose it is; not this list's own count.
    queue ? queue.text : n("review") && t("tk.sub.review", { n: n("review") }),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("tk.list.kicker")} title={t("nav.tasks")} sub={tasks ? summaryLine || t("tk.list.calm") : undefined} />
      <section className="panel flex min-w-0 flex-col">
        <div className="flex flex-col gap-3 border-b border-line p-4">
          <Capture onCaptured={() => refresh()} />
          <div className="flex flex-col gap-3">
            <div role="group" aria-label={t("tk.list.filter")} className="-mx-1 flex gap-1.5 overflow-x-auto px-1 sm:flex-wrap sm:overflow-visible">
              {[null, ...FILTERS].map((g) => (
                <button
                  key={g ?? "all"}
                  type="button"
                  aria-pressed={show === g}
                  onClick={() => set({ show: g })}
                  className={`flex h-8 shrink-0 items-center gap-1.5 rounded-full border px-3 text-[13px] ${
                    show === g ? "border-accent bg-accent/10 text-ink" : "border-line text-ink-2 hover:text-ink"
                  }`}
                >
                  {g ? t(`tk.group.${g}`) : t("tk.list.all")}
                  <span className={`text-xs tabular-nums ${g === "you" && n("you") ? "text-orange-200" : "text-ink-2"}`}>
                    {g ? n(g) : filtered.length}
                  </span>
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-md border border-line bg-bg px-2.5 focus-within:border-accent sm:w-52 sm:flex-none">
                <Search size={14} className="shrink-0 text-ink-3" aria-hidden />
                <span className="sr-only">{t("tk.list.search")}</span>
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("tk.list.search")} className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-ink-3" />
                {q && (
                  <button type="button" onClick={() => setQ("")} aria-label={t("tk.list.clear")} className="text-ink-2 hover:text-ink">
                    <X size={13} />
                  </button>
                )}
              </label>
              <label className="sr-only" htmlFor="who">
                {t("tk.list.who")}
              </label>
              <select id="who" value={who} onChange={(e) => set({ who: e.target.value || null })} className="h-8 max-w-[11rem] rounded-md border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent">
                <option value="">{t("tk.list.who_all")}</option>
                <option value="me">{t("tk.list.who_me")}</option>
                {people
                  .filter(([id]) => id !== meId)
                  .map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
              </select>
              {topics.length > 1 || topic ? (
                <>
                  <label className="sr-only" htmlFor="topic">
                    {t("tk.list.topic")}
                  </label>
                  <select id="topic" value={topic} onChange={(e) => set({ topic: e.target.value || null })} className="h-8 max-w-[10rem] rounded-md border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent">
                    <option value="">{t("tk.list.topic_all")}</option>
                    {(topic && !topics.includes(topic) ? [topic, ...topics] : topics).map((x) => (
                      <option key={x} value={x}>
                        #{x}
                      </option>
                    ))}
                  </select>
                </>
              ) : null}
              <label className="sr-only" htmlFor="view">
                {t("tk.list.view")}
              </label>
              <select id="view" value={view} onChange={(e) => set({ view: e.target.value === "board" ? null : e.target.value })} className="h-8 rounded-md border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent">
                {VIEWS.map((v) => (
                  <option key={v} value={v}>
                    {t(`tk.view.${v}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
        {view === "inbox" && (tasks?.length ?? 0) > 0 && (
          <Link to="/tasks/inbox" className="border-b border-line px-4 py-2.5 text-[13px] text-accent hover:bg-raised">
            {t("work.tasks.clarify_link")}
          </Link>
        )}
        {view === "today" && tasks && <CapacityBar tasks={tasks} />}
        {!tasks && !error && <p className="p-6 text-sm text-ink-2">{t("act.loading")}</p>}
        {tasks && visible.length === 0 && <p className="p-8 text-center text-sm text-ink-2">{q || who ? t("tk.list.none_filtered") : t("tk.list.none")}</p>}
        {visible.map((g) => (
          <GroupList key={`${g}${view}`} g={g} items={grouped[g]} meId={meId} selected={selected} hrefOf={hrefOf} collapsed={g === "done" || g === "later"} needKinds={needKinds} />
        ))}
        {error && <p className="p-4 text-xs break-words text-red-400">{error}</p>}
        <div className="flex justify-end px-4 py-3">
          <Link to="/weekly-review" className="text-xs text-ink-2 hover:text-accent">
            {t("work.tasks.weekly")}
          </Link>
        </div>
      </section>
    </div>
  );
}
